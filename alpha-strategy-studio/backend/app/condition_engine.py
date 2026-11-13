"""Phase 2 & Phase 3 Condition, Sequence, and Re-entry Evaluation Engine (Backend).

Shared deterministic evaluation engine for backtesting, paper trading, and live trading:
- Evaluates price fields (OPEN, HIGH, LOW, CLOSE, LTP, VOLUME), indicators, FC/RC levels, and constants.
- Supports all Phase 2 operators: >, <, >=, <=, ==, crosses_above, crosses_below,
  trades_above, trades_below, candle_green, candle_red, close_above_open, close_below_open,
  near, buy_near, sell_near, moves_out_of, out_of_fc1h, out_of_fc1l, out_of_range,
  breakout_above, breakout_below.
- Phase 3 Sequence Node Engine:
  - Tracks per-strategy, per-sequence-node ordered state (`waitingStep`, `stepTimestamps`,
    `startedAt`, `expiresAt`, `lastStepEvalKey`, `resetReason`).
  - Steps never fire out of order.
  - Steps never advance more than one step in the same evaluation tick unless `allowSameTick=True`.
  - Automatically resets sequence progress on: IST day change, trade entry, explicit invalidation,
    or `timeoutMinutes` expiry.
- Phase 3 Re-entry Engine:
  - Tracks daily re-entry count (`reentriesToday`), last exit metadata (`lastExitReason`,
    `lastExitTimeIst`, `lastExitSide`), and active trade state.
  - Enforces `enabled`, `maxPerDay`, `afterStopLoss`, `afterTarget`, `afterManualExit`,
    `cooldownMinutes`, allowed `directions`, and resolves dedicated `reentry.stopLoss` and `reentry.targets`.
  - Blocks any rule or re-entry configuration flagged with `unresolvedConflict=True`.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from .indicators import evaluate_indicator_config
from .levels import FormulaNotProvided, LevelBook, LevelError
from .schema_v2 import (
    BREAKOUT_OPERATORS,
    COMPARISON_OPERATORS,
    CROSS_OPERATORS,
    LEVEL_NAMES,
    PROXIMITY_OPERATORS,
    TOLERANCE_OPERATORS,
    TRADE_OPERATORS,
    UNARY_OPERATORS,
    normalize_eval_mode,
    validate_rule_builder,
)

IST = timezone(timedelta(hours=5, minutes=30))

WEEKDAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "1h": 3600,
    "1d": 86400,
}

FLOAT_EPSILON = 1e-9


def parse_ist_datetime(value: Any) -> datetime:
    """Convert a timestamp (epoch seconds, ISO string, or datetime) to an IST-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value.astimezone(IST)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=IST)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return datetime.fromtimestamp(float(cleaned), tz=IST)
        try:
            dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=IST)
            return dt.astimezone(IST)
        except ValueError as exc:
            raise ValueError(f"Cannot parse timestamp '{value}' as IST datetime.") from exc
    raise ValueError(f"Unsupported timestamp value: {value!r}")


def candle_open_time_ist(candle: dict) -> datetime:
    """Return the IST open timestamp of a candle."""
    if "timestamp" in candle and candle["timestamp"] is not None:
        return parse_ist_datetime(candle["timestamp"])
    if "openTime" in candle and candle["openTime"] is not None:
        return parse_ist_datetime(candle["openTime"])
    if "date" in candle and "time" in candle:
        return parse_ist_datetime(f"{candle['date']}T{candle['time']}:00+05:30")
    if "date" in candle:
        return parse_ist_datetime(f"{candle['date']}T09:15:00+05:30")
    raise ValueError("Candle is missing a recognizable timestamp/date field.")


def candle_close_time_ist(candle: dict, timeframe: str) -> datetime:
    """Return the IST close timestamp of a candle for a given timeframe."""
    if "closeTime" in candle and candle["closeTime"] is not None:
        return parse_ist_datetime(candle["closeTime"])
    open_dt = candle_open_time_ist(candle)
    dur = TIMEFRAME_SECONDS.get(timeframe, 300)
    return open_dt + timedelta(seconds=dur)


def is_candle_closed(candle: dict, timeframe: str, eval_dt_ist: datetime) -> bool:
    """Determine if a candle is closed at `eval_dt_ist` (strictly no look-ahead)."""
    if "closed" in candle and isinstance(candle["closed"], bool):
        if not candle["closed"]:
            return False
    close_dt = candle_close_time_ist(candle, timeframe)
    return close_dt <= eval_dt_ist


def filter_closed_candles(
    candles: Sequence[dict],
    timeframe: str,
    eval_dt_ist: datetime,
) -> list[dict]:
    """Return only candles whose close timestamp is <= `eval_dt_ist`."""
    return [c for c in candles if is_candle_closed(c, timeframe, eval_dt_ist)]


def resample_candles_to_timeframe(
    base_candles: Sequence[dict],
    base_timeframe: str,
    target_timeframe: str,
    eval_dt_ist: datetime,
    session_start_hhmm: str = "09:15",
) -> list[dict]:
    """Aggregate lower-timeframe candles into closed higher-timeframe candles up to `eval_dt_ist`."""
    base_sec = TIMEFRAME_SECONDS.get(base_timeframe)
    target_sec = TIMEFRAME_SECONDS.get(target_timeframe)
    if not base_sec or not target_sec:
        raise ValueError(f"Unsupported timeframe in resample: {base_timeframe} -> {target_timeframe}")
    if target_sec == base_sec:
        return filter_closed_candles(base_candles, base_timeframe, eval_dt_ist)
    if target_sec < base_sec:
        raise ValueError(f"Cannot resample from coarser {base_timeframe} to finer {target_timeframe}.")

    sh, sm = [int(x) for x in session_start_hhmm.split(":")]
    closed_base = filter_closed_candles(base_candles, base_timeframe, eval_dt_ist)

    buckets: dict[tuple[str, int], list[dict]] = {}
    bucket_bounds: dict[tuple[str, int], tuple[datetime, datetime]] = {}

    for c in closed_base:
        open_dt = candle_open_time_ist(c)
        day_str = open_dt.strftime("%Y-%m-%d")
        session_anchor = open_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
        offset_sec = int((open_dt - session_anchor).total_seconds())
        bucket_idx = offset_sec // target_sec if offset_sec >= 0 else -1
        key = (day_str, bucket_idx)
        if key not in buckets:
            b_start = session_anchor + timedelta(seconds=bucket_idx * target_sec)
            b_end = b_start + timedelta(seconds=target_sec)
            buckets[key] = []
            bucket_bounds[key] = (b_start, b_end)
        buckets[key].append(c)

    aggregated: list[dict] = []
    for key in sorted(buckets.keys()):
        b_start, b_end = bucket_bounds[key]
        if b_end > eval_dt_ist:
            continue
        group = buckets[key]
        if not group:
            continue
        agg = {
            "timestamp": int(b_start.timestamp()),
            "openTime": b_start.isoformat(),
            "closeTime": b_end.isoformat(),
            "date": b_start.strftime("%Y-%m-%d"),
            "time": b_start.strftime("%H:%M"),
            "open": float(group[0]["open"]),
            "high": max(float(x["high"]) for x in group),
            "low": min(float(x["low"]) for x in group),
            "close": float(group[-1]["close"]),
            "volume": sum(int(x.get("volume", 0) or 0) for x in group),
            "closed": True,
        }
        aggregated.append(agg)
    return aggregated


def check_session_and_holiday(session_cfg: dict, eval_dt_ist: datetime) -> dict[str, Any]:
    """Verify whether `eval_dt_ist` falls within an active IST session and trading day."""
    ist_dt = parse_ist_datetime(eval_dt_ist)
    day_str = ist_dt.strftime("%Y-%m-%d")
    time_str = ist_dt.strftime("%H:%M")
    weekday = WEEKDAY_NAMES[ist_dt.weekday()]

    trading_days = session_cfg.get("tradingDays") or ["MON", "TUE", "WED", "THU", "FRI"]
    if weekday not in trading_days:
        return {
            "allowed": False,
            "reason": f"Non-trading weekday ({weekday})",
            "day": day_str,
            "time": time_str,
            "weekday": weekday,
        }

    holiday_cal = session_cfg.get("holidayCalendar", "NONE")
    holidays = session_cfg.get("holidays") or []
    if holiday_cal == "EXPLICIT" and day_str in holidays:
        return {
            "allowed": False,
            "reason": f"Exchange holiday ({day_str})",
            "day": day_str,
            "time": time_str,
            "weekday": weekday,
        }

    start_hhmm = session_cfg.get("startTime", "09:15")
    end_hhmm = session_cfg.get("endTime", "15:30")
    sh, sm = [int(x) for x in start_hhmm.split(":")]
    eh, em = [int(x) for x in end_hhmm.split(":")]
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    cur_min = ist_dt.hour * 60 + ist_dt.minute
    if cur_min < start_min or cur_min > end_min:
        return {
            "allowed": False,
            "reason": f"Outside session hours ({time_str} not in {start_hhmm}–{end_hhmm} IST)",
            "day": day_str,
            "time": time_str,
            "weekday": weekday,
        }
    if cur_min == end_min and ist_dt.second > 0:
        return {
            "allowed": False,
            "reason": f"Outside session hours ({ist_dt.strftime('%H:%M:%S')} > {end_hhmm} IST)",
            "day": day_str,
            "time": time_str,
            "weekday": weekday,
        }

    return {
        "allowed": True,
        "reason": None,
        "day": day_str,
        "time": time_str,
        "weekday": weekday,
    }


def resolve_candle_ref_index(length: int, candle_ref: str) -> Optional[int]:
    """Resolve `current`, `previous`, or `N-ago` to a 0-based index into a candle array of `length`."""
    if length <= 0:
        return None
    ref = str(candle_ref or "current").strip().lower()
    if ref == "current":
        offset = 0
    elif ref == "previous":
        offset = 1
    elif ref.endswith("-ago"):
        try:
            offset = int(ref.split("-", 1)[0])
        except ValueError:
            return None
    else:
        return None
    idx = length - 1 - offset
    return idx if idx >= 0 else None


def _default_reentry_state() -> dict[str, Any]:
    return {
        "initialEntryTakenToday": False,
        "inPosition": False,
        "reentriesToday": 0,
        "lastExitReason": None,      # "STOP_LOSS" | "TARGET" | "MANUAL" | None
        "lastExitTimeIst": None,     # ISO string in IST
        "lastExitSide": None,        # "BUY" | "SELL" | None
        "awaitingReentry": False,
        "history": [],
    }


class EvaluationState:
    """Per-strategy persistent state for:
    - Phase 2 stateful crossing operators (`nodeHistory`)
    - Phase 3 ordered sequence nodes (`sequenceStates`)
    - Phase 3 re-entry tracking & cooldowns (`reentryState`)
    """

    def __init__(self, initial: Optional[dict] = None):
        raw = copy.deepcopy(initial) if isinstance(initial, dict) else {}
        self.node_history: dict[str, dict[str, Any]] = raw.get("nodeHistory", {})
        self.sequence_states: dict[str, dict[str, Any]] = raw.get("sequenceStates", {})
        self.reentry_state: dict[str, Any] = {
            **_default_reentry_state(),
            **(raw.get("reentryState") if isinstance(raw.get("reentryState"), dict) else {}),
        }
        self.last_eval_day: Optional[str] = raw.get("lastEvalDay")

    def to_dict(self) -> dict[str, Any]:
        return {
            "lastEvalDay": self.last_eval_day,
            "nodeHistory": copy.deepcopy(self.node_history),
            "sequenceStates": copy.deepcopy(self.sequence_states),
            "reentryState": copy.deepcopy(self.reentry_state),
        }

    def reset_for_new_day(self, new_day: str) -> None:
        """Reset all crossing, sequence, and daily re-entry counters on IST day change."""
        self.node_history.clear()
        for seq_id, seq_st in list(self.sequence_states.items()):
            self.sequence_states[seq_id] = {
                "sequenceId": seq_id,
                "waitingStep": 0,
                "stepTimestamps": {},
                "startedAt": None,
                "expiresAt": None,
                "lastStepEvalKey": None,
                "lastResetReason": "DAY_CHANGE",
                "lastResetAt": new_day,
            }
        self.reentry_state = _default_reentry_state()
        self.last_eval_day = new_day

    def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        return self.node_history.get(node_id)

    def update_node(
        self,
        node_id: str,
        left_val: Optional[float],
        right_val: Optional[float],
        timestamp_iso: str,
        candle_key: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        entry = {
            "left": left_val,
            "right": right_val,
            "updatedAt": timestamp_iso,
            "candleKey": candle_key,
        }
        if extra:
            entry.update(extra)
        self.node_history[node_id] = entry

    def get_sequence(self, seq_id: str) -> dict[str, Any]:
        if seq_id not in self.sequence_states:
            self.sequence_states[seq_id] = {
                "sequenceId": seq_id,
                "waitingStep": 0,
                "stepTimestamps": {},
                "startedAt": None,
                "expiresAt": None,
                "lastStepEvalKey": None,
                "lastResetReason": None,
                "lastResetAt": None,
            }
        return self.sequence_states[seq_id]

    def reset_sequence(self, seq_id: Optional[str], reason: str, timestamp_iso: str) -> None:
        """Reset a specific sequence node (or all sequence nodes if `seq_id` is None)."""
        target_ids = [seq_id] if seq_id else list(self.sequence_states.keys())
        for sid in target_ids:
            if sid is None:
                continue
            self.sequence_states[sid] = {
                "sequenceId": sid,
                "waitingStep": 0,
                "stepTimestamps": {},
                "startedAt": None,
                "expiresAt": None,
                "lastStepEvalKey": None,
                "lastResetReason": reason,
                "lastResetAt": timestamp_iso,
            }

    def record_trade_entry(
        self,
        side: str,
        timestamp_ist: Any,
        is_reentry: bool = False,
        rule_id: Optional[str] = None,
    ) -> None:
        """Record a trade entry: resets all active sequence states and updates re-entry counters."""
        dt_ist = parse_ist_datetime(timestamp_ist)
        day_str = dt_ist.strftime("%Y-%m-%d")
        if self.last_eval_day and self.last_eval_day != day_str:
            self.reset_for_new_day(day_str)
        self.last_eval_day = day_str

        self.reset_sequence(None, reason="TRADE_ENTRY", timestamp_iso=dt_ist.isoformat())
        self.reentry_state["inPosition"] = True
        if is_reentry:
            self.reentry_state["reentriesToday"] = int(self.reentry_state.get("reentriesToday", 0)) + 1
            self.reentry_state["awaitingReentry"] = False
        else:
            self.reentry_state["initialEntryTakenToday"] = True
            self.reentry_state["awaitingReentry"] = False

        self.reentry_state.setdefault("history", []).append({
            "event": "REENTRY" if is_reentry else "INITIAL_ENTRY",
            "side": side,
            "ruleId": rule_id,
            "timestampIst": dt_ist.isoformat(),
            "reentriesToday": self.reentry_state["reentriesToday"],
        })

    def record_trade_exit(
        self,
        exit_reason: str,
        timestamp_ist: Any,
        side: Optional[str] = None,
    ) -> None:
        """Record a trade exit (`STOP_LOSS`, `TARGET`, `MANUAL`) to arm re-entry checks and cooldown."""
        dt_ist = parse_ist_datetime(timestamp_ist)
        day_str = dt_ist.strftime("%Y-%m-%d")
        if self.last_eval_day and self.last_eval_day != day_str:
            self.reset_for_new_day(day_str)
        self.last_eval_day = day_str

        norm_reason = str(exit_reason or "MANUAL").strip().upper()
        self.reentry_state["inPosition"] = False
        self.reentry_state["lastExitReason"] = norm_reason
        self.reentry_state["lastExitTimeIst"] = dt_ist.isoformat()
        if side:
            self.reentry_state["lastExitSide"] = str(side).upper()
        self.reentry_state["awaitingReentry"] = True
        self.reset_sequence(None, reason=f"TRADE_EXIT_{norm_reason}", timestamp_iso=dt_ist.isoformat())
        self.reentry_state.setdefault("history", []).append({
            "event": "EXIT",
            "exitReason": norm_reason,
            "side": side,
            "timestampIst": dt_ist.isoformat(),
        })


def check_reentry_eligibility(
    reentry_cfg: dict,
    reentry_state: dict,
    eval_dt_ist: datetime,
    candidate_side: Optional[str] = None,
) -> dict[str, Any]:
    """Evaluate whether a strategy is eligible for re-entry at `eval_dt_ist`.

    Checks:
    1. `reentry.unresolvedConflict` must not be True.
    2. `reentry.enabled` must be True.
    3. Not currently in an open position (`inPosition == False`) and `awaitingReentry == True`.
    4. `reentriesToday < maxPerDay`.
    5. `lastExitReason` matches `afterStopLoss`, `afterTarget`, or `afterManualExit`.
    6. `cooldownMinutes` has elapsed since `lastExitTimeIst`.
    7. `candidate_side` (if provided) is in `reentry.directions`.
    """
    if reentry_cfg.get("unresolvedConflict"):
        return {
            "eligible": False,
            "blocked": True,
            "reason": reentry_cfg.get("conflictNote")
            or "Blocked: re-entry has an unresolved specification conflict.",
        }
    if not reentry_cfg.get("enabled", False):
        return {
            "eligible": False,
            "blocked": False,
            "reason": "Re-entry is disabled.",
        }
    if reentry_state.get("inPosition"):
        return {
            "eligible": False,
            "blocked": False,
            "reason": "Position is currently open.",
        }
    if not reentry_state.get("awaitingReentry") or not reentry_state.get("lastExitReason"):
        return {
            "eligible": False,
            "blocked": False,
            "reason": "No prior closed trade awaiting re-entry today.",
        }

    max_per_day = int(reentry_cfg.get("maxPerDay", 0))
    taken = int(reentry_state.get("reentriesToday", 0))
    if taken >= max_per_day:
        return {
            "eligible": False,
            "blocked": False,
            "reason": f"Max re-entries per day reached ({taken}/{max_per_day}).",
        }

    last_reason = str(reentry_state.get("lastExitReason") or "").upper()
    reason_allowed = (
        (last_reason in ("STOP_LOSS", "SL") and reentry_cfg.get("afterStopLoss", False))
        or (last_reason in ("TARGET", "TGT") and reentry_cfg.get("afterTarget", False))
        or (last_reason in ("MANUAL", "MANUAL_EXIT") and reentry_cfg.get("afterManualExit", False))
    )
    if not reason_allowed:
        return {
            "eligible": False,
            "blocked": False,
            "reason": f"Re-entry is not enabled after exit reason '{last_reason}'.",
        }

    cooldown_min = int(reentry_cfg.get("cooldownMinutes", 0))
    last_exit_iso = reentry_state.get("lastExitTimeIst")
    if cooldown_min > 0 and last_exit_iso:
        exit_dt = parse_ist_datetime(last_exit_iso)
        ready_dt = exit_dt + timedelta(minutes=cooldown_min)
        if eval_dt_ist < ready_dt:
            rem_sec = int((ready_dt - eval_dt_ist).total_seconds())
            return {
                "eligible": False,
                "blocked": False,
                "cooldownRemainingSeconds": rem_sec,
                "cooldownReadyAtIst": ready_dt.isoformat(),
                "reason": f"Re-entry cooldown active ({rem_sec}s remaining until {ready_dt.strftime('%H:%M:%S')} IST).",
            }

    allowed_dirs = [str(d).upper() for d in (reentry_cfg.get("directions") or [])]
    if candidate_side and str(candidate_side).upper() not in allowed_dirs:
        return {
            "eligible": False,
            "blocked": False,
            "reason": f"Direction '{candidate_side}' is not permitted by reentry.directions {allowed_dirs}.",
        }

    return {
        "eligible": True,
        "blocked": False,
        "reentriesToday": taken,
        "maxPerDay": max_per_day,
        "lastExitReason": last_reason,
        "allowedDirections": allowed_dirs,
        "stopLoss": reentry_cfg.get("stopLoss"),
        "targets": reentry_cfg.get("targets"),
        "reason": f"Eligible for re-entry ({taken + 1}/{max_per_day} after {last_reason}).",
    }


class ConditionEngine:
    """Deterministic Phase 2 & Phase 3 condition, sequence, and re-entry evaluation engine."""

    def __init__(self, level_book: Optional[LevelBook] = None):
        self.level_book = level_book

    def _get_Visible_candles(
        self,
        timeframe: str,
        candles_by_tf: dict[str, Sequence[dict]],
        eval_dt_ist: datetime,
        eval_mode: str,
        session_start_hhmm: str = "09:15",
        primary_timeframe: str = "5m",
    ) -> list[dict]:
        """Return visible candles for `timeframe` at `eval_dt_ist`."""
        is_higher_tf = TIMEFRAME_SECONDS.get(timeframe, 300) > TIMEFRAME_SECONDS.get(primary_timeframe, 300)
        raw_candles = candles_by_tf.get(timeframe)

        if raw_candles is None:
            for src_tf in ("1m", "3m", "5m", primary_timeframe):
                if src_tf in candles_by_tf and TIMEFRAME_SECONDS.get(src_tf, 999999) < TIMEFRAME_SECONDS.get(timeframe, 0):
                    return resample_candles_to_timeframe(
                        candles_by_tf[src_tf],
                        src_tf,
                        timeframe,
                        eval_dt_ist,
                        session_start_hhmm=session_start_hhmm,
                    )
            return []

        if is_higher_tf or eval_mode == "CANDLE_CLOSE":
            return filter_closed_candles(raw_candles, timeframe, eval_dt_ist)

        visible: list[dict] = []
        for c in raw_candles:
            open_dt = candle_open_time_ist(c)
            if open_dt <= eval_dt_ist:
                visible.append(c)
        return visible

    def _lookup_level(
        self,
        strategy: dict,
        day_str: str,
        level_name: str,
        manual_levels_override: Optional[dict[str, float]] = None,
    ) -> tuple[Optional[float], Optional[str], Optional[str]]:
        """Return (value, warning, error) for an FC/RC level."""
        lvl = str(level_name or "").strip().upper()
        if lvl not in LEVEL_NAMES:
            return None, None, f"Unknown level '{level_name}'."

        levels_cfg = strategy.get("levels") or {}
        provider = str(levels_cfg.get("provider", "MANUAL")).upper()
        if provider == "FORMULA":
            return None, None, "formula not provided"

        if manual_levels_override and lvl in manual_levels_override:
            val = manual_levels_override[lvl]
            if val is not None and not isinstance(val, bool) and math.isfinite(float(val)):
                return float(val), None, None

        if self.level_book is None:
            return None, None, f"No level book configured to look up {lvl} on {day_str}."

        symbol = (strategy.get("meta") or {}).get("symbol") or strategy.get("symbol") or "NIFTY 50"
        if strategy.get("execution", {}).get("instrument", {}).get("underlying"):
            symbol = strategy["execution"]["instrument"]["underlying"]

        try:
            quote = self.level_book.lookup(provider, symbol, day_str, lvl)
            return quote.value, quote.warning, None
        except FormulaNotProvided as exc:
            return None, None, str(exc)
        except LevelError as exc:
            return None, None, exc.message

    def _resolve_operand(
        self,
        operand: dict,
        candles: list[dict],
        candle_idx: int,
        indicator_cache: dict[tuple[str, int], dict[str, list[Optional[float]]]],
        indicator_defs: dict[str, dict],
        strategy: dict,
        day_str: str,
        live_tick: Optional[dict],
        eval_mode: str,
        candles_by_tf: dict[str, Sequence[dict]],
        eval_dt_ist: datetime,
        session_start_hhmm: str,
        primary_timeframe: str,
        manual_levels_override: Optional[dict[str, float]] = None,
    ) -> tuple[Optional[float], Optional[str]]:
        """Resolve an operand at `candle_idx` to `(numeric_value, reason_if_none)`."""
        kind = operand.get("kind")
        if kind == "constant":
            val = operand.get("value")
            return (float(val), None) if val is not None else (None, "Constant value is null")

        if kind == "level":
            lvl_name = operand.get("level", "")
            val, _warn, err = self._lookup_level(strategy, day_str, lvl_name, manual_levels_override)
            if err:
                return None, err
            return val, None

        if kind == "price":
            field = str(operand.get("field") or "CLOSE").upper()
            if field == "LTP":
                if eval_mode in ("LIVE_TICK", "INTRABAR") and live_tick and live_tick.get("ltp") is not None:
                    return float(live_tick["ltp"]), None
                if 0 <= candle_idx < len(candles):
                    c = candles[candle_idx]
                    if c.get("ltp") is not None:
                        return float(c["ltp"]), None
                    if c.get("close") is not None:
                        return float(c["close"]), None
                return None, "LTP unavailable"

            if not (0 <= candle_idx < len(candles)):
                return None, f"Candle index {candle_idx} out of range (have {len(candles)} candles)"
            candle = candles[candle_idx]
            key = field.lower()
            raw = candle.get(key)
            if raw is None:
                return None, f"Candle missing price field {field}"
            return float(raw), None

        if kind == "indicator":
            ind_id = operand.get("indicatorId")
            if not ind_id or ind_id not in indicator_defs:
                return None, f"Unknown indicator '{ind_id}'"
            ind_cfg = indicator_defs[ind_id]
            if not ind_cfg.get("enabled", True):
                return None, f"Indicator '{ind_id}' is disabled"
            ind_tf = ind_cfg.get("timeframe", primary_timeframe)
            ind_eval_mode = "CANDLE_CLOSE" if ind_tf != primary_timeframe else eval_mode
            ind_candles = self._get_Visible_candles(
                ind_tf,
                candles_by_tf,
                eval_dt_ist,
                ind_eval_mode,
                session_start_hhmm=session_start_hhmm,
                primary_timeframe=primary_timeframe,
            )
            if not ind_candles:
                return None, f"No closed candles for indicator '{ind_id}' on timeframe {ind_tf}"

            offset_from_end = (len(candles) - 1 - candle_idx) if candles else 0
            ind_idx = len(ind_candles) - 1 - offset_from_end
            if ind_idx < 0 or ind_idx >= len(ind_candles):
                return None, f"Indicator '{ind_id}' candle index out of range"

            cache_key = (ind_id, len(ind_candles))
            if cache_key not in indicator_cache:
                indicator_cache[cache_key] = evaluate_indicator_config(ind_cfg, ind_candles)
            series_map = indicator_cache[cache_key]
            out_name = str(operand.get("output") or "value").lower()
            series = series_map.get(out_name) or series_map.get("value")
            if series is None or ind_idx >= len(series):
                return None, f"Indicator '{ind_id}' output '{out_name}' unavailable"
            val = series[ind_idx]
            if val is None:
                return None, f"Indicator '{ind_id}' is in warm-up period (insufficient candles)"
            return float(val), None

        return None, f"Unsupported operand kind '{kind}'"

    @staticmethod
    def _compute_tolerance_delta(ref_value: float, tolerance: float, unit: str) -> float:
        if unit == "percent":
            return abs(ref_value) * (tolerance / 100.0)
        return tolerance

    def _evaluate_leaf(
        self,
        leaf: dict,
        strategy: dict,
        candles_by_tf: dict[str, Sequence[dict]],
        eval_dt_ist: datetime,
        day_str: str,
        state: EvaluationState,
        indicator_cache: dict[tuple[str, int], dict[str, list[Optional[float]]]],
        indicator_defs: dict[str, dict],
        live_tick: Optional[dict],
        session_start_hhmm: str,
        primary_timeframe: str,
        manual_levels_override: Optional[dict[str, float]] = None,
    ) -> dict[str, Any]:
        node_id = leaf.get("id", "leaf")
        if not leaf.get("enabled", True):
            return {
                "id": node_id,
                "type": "leaf",
                "enabled": False,
                "matched": False,
                "reason": "Condition disabled",
            }

        tf = leaf.get("timeframe", primary_timeframe)
        eval_mode = normalize_eval_mode(
            leaf.get("evaluateOn")
            or (strategy.get("execution") or {}).get("defaultEvaluationMode")
            or "CANDLE_CLOSE"
        )
        op = leaf.get("operator", "")
        candle_ref = leaf.get("candleRef", "current")
        params = leaf.get("params") or {}

        candles = self._get_Visible_candles(
            tf,
            candles_by_tf,
            eval_dt_ist,
            eval_mode,
            session_start_hhmm=session_start_hhmm,
            primary_timeframe=primary_timeframe,
        )
        idx = resolve_candle_ref_index(len(candles), candle_ref)
        if idx is None:
            return {
                "id": node_id,
                "type": "leaf",
                "enabled": True,
                "matched": False,
                "operator": op,
                "timeframe": tf,
                "evaluateOn": eval_mode,
                "reason": f"Insufficient closed candles on {tf} for candleRef '{candle_ref}'",
            }

        target_candle = candles[idx]
        candle_key = f"{tf}:{target_candle.get('date', '')}:{target_candle.get('time', target_candle.get('timestamp', idx))}"

        if op in UNARY_OPERATORS:
            o_val = float(target_candle["open"])
            c_val = float(target_candle["close"])
            if op in ("candle_green", "close_above_open"):
                matched = c_val > o_val
            elif op in ("candle_red", "close_below_open"):
                matched = c_val < o_val
            else:
                matched = False
            return {
                "id": node_id,
                "type": "leaf",
                "enabled": True,
                "matched": matched,
                "operator": op,
                "timeframe": tf,
                "candleRef": candle_ref,
                "evaluateOn": eval_mode,
                "leftValue": c_val,
                "rightValue": o_val,
                "reason": f"{op}: close={c_val} vs open={o_val}",
            }

        if op in TOLERANCE_OPERATORS:
            tol = params.get("tolerance")
            tol_unit = params.get("toleranceUnit")
            if tol is None or float(tol) <= 0 or tol_unit not in ("points", "percent"):
                return {
                    "id": node_id,
                    "type": "leaf",
                    "enabled": True,
                    "matched": False,
                    "blocked": True,
                    "operator": op,
                    "reason": f"Operator '{op}' blocked: mandatory tolerance and toleranceUnit must be configured.",
                }

        left_op = leaf.get("left")
        right_op = leaf.get("right")
        if not left_op or not right_op:
            return {
                "id": node_id,
                "type": "leaf",
                "enabled": True,
                "matched": False,
                "operator": op,
                "reason": "Missing left or right operand",
            }

        left_val, left_err = self._resolve_operand(
            left_op, candles, idx, indicator_cache, indicator_defs, strategy,
            day_str, live_tick, eval_mode, candles_by_tf, eval_dt_ist,
            session_start_hhmm, primary_timeframe, manual_levels_override,
        )
        right_val, right_err = self._resolve_operand(
            right_op, candles, idx, indicator_cache, indicator_defs, strategy,
            day_str, live_tick, eval_mode, candles_by_tf, eval_dt_ist,
            session_start_hhmm, primary_timeframe, manual_levels_override,
        )
        if left_val is None or right_val is None:
            return {
                "id": node_id,
                "type": "leaf",
                "enabled": True,
                "matched": False,
                "operator": op,
                "timeframe": tf,
                "candleRef": candle_ref,
                "evaluateOn": eval_mode,
                "leftValue": left_val,
                "rightValue": right_val,
                "reason": left_err or right_err or "Operand value unavailable",
            }

        prev_state = state.get_node(node_id)
        prev_left: Optional[float] = None
        prev_right: Optional[float] = None
        if prev_state is not None and prev_state.get("left") is not None and prev_state.get("right") is not None:
            if eval_mode == "CANDLE_CLOSE" and prev_state.get("candleKey") == candle_key and "prevLeft" in prev_state:
                prev_left = prev_state.get("prevLeft")
                prev_right = prev_state.get("prevRight")
            else:
                prev_left = float(prev_state["left"])
                prev_right = float(prev_state["right"])
        elif idx >= 1:
            p_l, _ = self._resolve_operand(
                left_op, candles, idx - 1, indicator_cache, indicator_defs, strategy,
                day_str, None, "CANDLE_CLOSE", candles_by_tf, eval_dt_ist,
                session_start_hhmm, primary_timeframe, manual_levels_override,
            )
            p_r, _ = self._resolve_operand(
                right_op, candles, idx - 1, indicator_cache, indicator_defs, strategy,
                day_str, None, "CANDLE_CLOSE", candles_by_tf, eval_dt_ist,
                session_start_hhmm, primary_timeframe, manual_levels_override,
            )
            prev_left, prev_right = p_l, p_r

        matched = False
        reason = ""
        if op == ">":
            matched = left_val > right_val
            reason = f"{left_val} > {right_val}"
        elif op == "<":
            matched = left_val < right_val
            reason = f"{left_val} < {right_val}"
        elif op == ">=":
            matched = left_val >= right_val - FLOAT_EPSILON
            reason = f"{left_val} >= {right_val}"
        elif op == "<=":
            matched = left_val <= right_val + FLOAT_EPSILON
            reason = f"{left_val} <= {right_val}"
        elif op == "==":
            matched = math.isclose(left_val, right_val, rel_tol=1e-7, abs_tol=1e-6)
            reason = f"{left_val} == {right_val}"

        elif op in CROSS_OPERATORS:
            if prev_left is None or prev_right is None:
                matched = False
                reason = f"{op}: waiting for previous evaluation state (first observation)"
            elif op == "crosses_above":
                matched = (prev_left <= prev_right + FLOAT_EPSILON) and (left_val > right_val + FLOAT_EPSILON)
                reason = f"crosses_above: prev({prev_left} <= {prev_right}) -> cur({left_val} > {right_val})"
            else:
                matched = (prev_left >= prev_right - FLOAT_EPSILON) and (left_val < right_val - FLOAT_EPSILON)
                reason = f"crosses_below: prev({prev_left} >= {prev_right}) -> cur({left_val} < {right_val})"

        elif op in TRADE_OPERATORS:
            trade_basis = str(params.get("tradeBasis") or eval_mode).upper()
            probe_val = left_val
            if trade_basis in ("LIVE_TICK", "TICK"):
                if live_tick and live_tick.get("ltp") is not None:
                    probe_val = float(live_tick["ltp"])
                elif target_candle.get("ltp") is not None:
                    probe_val = float(target_candle["ltp"])
            elif trade_basis in ("INTRABAR", "HIGH_LOW"):
                if left_op.get("kind") == "price":
                    probe_val = float(target_candle["high"] if op == "trades_above" else target_candle["low"])
            if op == "trades_above":
                matched = probe_val > right_val
                reason = f"trades_above ({trade_basis}): {probe_val} > {right_val}"
            else:
                matched = probe_val < right_val
                reason = f"trades_below ({trade_basis}): {probe_val} < {right_val}"
            left_val = probe_val

        elif op in PROXIMITY_OPERATORS:
            tol = float(params["tolerance"])
            tol_unit = str(params["toleranceUnit"])
            delta = self._compute_tolerance_delta(right_val, tol, tol_unit)
            diff = abs(left_val - right_val)
            matched = diff <= delta + FLOAT_EPSILON
            reason = f"{op}: |{left_val} - {right_val}| = {round(diff, 4)} <= tolerance {round(delta, 4)} ({tol} {tol_unit})"

        elif op in BREAKOUT_OPERATORS:
            tol = float(params["tolerance"])
            tol_unit = str(params["toleranceUnit"])
            crossing_mode = str(params.get("crossingMode") or "close_cross")
            delta = self._compute_tolerance_delta(right_val, tol, tol_unit)

            direction = str(params.get("breakoutDirection") or "").upper()
            if not direction:
                if op in ("out_of_fc1h", "breakout_above"):
                    direction = "ABOVE"
                elif op in ("out_of_fc1l", "breakout_below"):
                    direction = "BELOW"
                elif op == "out_of_range":
                    direction = "OUTSIDE_RANGE"
                elif right_op.get("kind") == "level" and str(right_op.get("level", "")).endswith("L"):
                    direction = "BELOW"
                else:
                    direction = "ABOVE"

            if direction == "OUTSIDE_RANGE" or op == "out_of_range":
                upper_lvl_name = params.get("upperLevel") or "FC1H"
                lower_lvl_name = params.get("lowerLevel") or "FC1L"
                up_val, _, up_err = self._lookup_level(strategy, day_str, upper_lvl_name, manual_levels_override)
                lo_val, _, lo_err = self._lookup_level(strategy, day_str, lower_lvl_name, manual_levels_override)
                if up_val is None or lo_val is None:
                    return {
                        "id": node_id,
                        "type": "leaf",
                        "enabled": True,
                        "matched": False,
                        "operator": op,
                        "reason": up_err or lo_err or "Range levels unavailable",
                    }
                up_delta = self._compute_tolerance_delta(up_val, tol, tol_unit)
                lo_delta = self._compute_tolerance_delta(lo_val, tol, tol_unit)
                broke_up = left_val >= (up_val + up_delta - FLOAT_EPSILON)
                broke_dn = left_val <= (lo_val - lo_delta + FLOAT_EPSILON)
                if crossing_mode == "close_cross":
                    if prev_left is None:
                        matched = False
                        reason = f"{op}: waiting for previous value to confirm range breakout"
                    else:
                        was_inside_or_below_up = prev_left <= up_val + up_delta + FLOAT_EPSILON
                        was_inside_or_above_lo = prev_left >= lo_val - lo_delta - FLOAT_EPSILON
                        matched = (broke_up and was_inside_or_below_up and prev_left <= up_val + FLOAT_EPSILON) or (
                            broke_dn and was_inside_or_above_lo and prev_left >= lo_val - FLOAT_EPSILON
                        )
                        reason = f"{op} (close_cross): prev={prev_left}, cur={left_val}, range=[{lo_val - lo_delta}, {up_val + up_delta}]"
                else:
                    matched = broke_up or broke_dn
                    reason = f"{op} ({crossing_mode}): cur={left_val}, range=[{lo_val - lo_delta}, {up_val + up_delta}]"
            elif direction == "ABOVE":
                threshold = right_val + delta
                probe = float(target_candle["high"]) if crossing_mode == "high_low_break" and left_op.get("kind") == "price" else left_val
                if crossing_mode in ("close_cross", "ltp_cross"):
                    if prev_left is None:
                        matched = False
                        reason = f"{op} ABOVE ({crossing_mode}): waiting for previous observation"
                    else:
                        matched = (prev_left <= right_val + FLOAT_EPSILON) and (probe >= threshold - FLOAT_EPSILON)
                        reason = f"{op} ABOVE ({crossing_mode}): prev={prev_left} <= {right_val} and cur={probe} >= {threshold}"
                else:
                    matched = probe >= threshold - FLOAT_EPSILON
                    reason = f"{op} ABOVE ({crossing_mode}): cur={probe} >= {threshold}"
            else:
                threshold = right_val - delta
                probe = float(target_candle["low"]) if crossing_mode == "high_low_break" and left_op.get("kind") == "price" else left_val
                if crossing_mode in ("close_cross", "ltp_cross"):
                    if prev_left is None:
                        matched = False
                        reason = f"{op} BELOW ({crossing_mode}): waiting for previous observation"
                    else:
                        matched = (prev_left >= right_val - FLOAT_EPSILON) and (probe <= threshold + FLOAT_EPSILON)
                        reason = f"{op} BELOW ({crossing_mode}): prev={prev_left} >= {right_val} and cur={probe} <= {threshold}"
                else:
                    matched = probe <= threshold + FLOAT_EPSILON
                    reason = f"{op} BELOW ({crossing_mode}): cur={probe} <= {threshold}"

        state.update_node(
            node_id,
            left_val=left_val,
            right_val=right_val,
            timestamp_iso=eval_dt_ist.isoformat(),
            candle_key=candle_key,
            extra={"prevLeft": prev_left, "prevRight": prev_right},
        )

        return {
            "id": node_id,
            "type": "leaf",
            "enabled": True,
            "matched": bool(matched),
            "operator": op,
            "timeframe": tf,
            "candleRef": candle_ref,
            "evaluateOn": eval_mode,
            "leftValue": left_val,
            "rightValue": right_val,
            "prevLeftValue": prev_left,
            "prevRightValue": prev_right,
            "reason": reason,
        }

    def _evaluate_sequence(
        self,
        node: dict,
        strategy: dict,
        candles_by_tf: dict[str, Sequence[dict]],
        eval_dt_ist: datetime,
        day_str: str,
        state: EvaluationState,
        indicator_cache: dict[tuple[str, int], dict[str, list[Optional[float]]]],
        indicator_defs: dict[str, dict],
        live_tick: Optional[dict],
        session_start_hhmm: str,
        primary_timeframe: str,
        manual_levels_override: Optional[dict[str, float]] = None,
    ) -> dict[str, Any]:
        """Evaluate a Phase 3 ordered `SequenceCondition` node.

        Rules enforced:
        - Ordered steps `steps[0], steps[1], ..., steps[N-1]` sorted by `order`.
        - Stores per-sequence state (`waitingStep`, `stepTimestamps`, `startedAt`, `expiresAt`).
        - If `timeoutMinutes` is configured and `eval_dt_ist > expiresAt`, the sequence state
          resets to `waitingStep = 0` before checking step 0 on the current tick.
        - Steps are NEVER satisfied out of order: while `waitingStep == k`, only `steps[k]`
          can advance the sequence. Even if `steps[k+1]` is true on an earlier tick, it is ignored.
        - Steps are NEVER satisfied in the same evaluation tick unless `allowSameTick == True`.
        """
        node_id = node.get("id", "sequence")
        all_steps = sorted(node.get("steps") or [], key=lambda s: s.get("order", 1))
        enabled_steps = [s for s in all_steps if s.get("enabled", True)]
        allow_same_tick = bool(node.get("allowSameTick", False))
        timeout_minutes = node.get("timeoutMinutes")

        if len(enabled_steps) < 2:
            return {
                "id": node_id,
                "type": "sequence",
                "enabled": True,
                "matched": False,
                "steps": [],
                "reason": "Sequence needs at least two enabled steps.",
            }

        seq_state = state.get_sequence(node_id)
        eval_iso = eval_dt_ist.isoformat()
        expired_reset = False

        # 1. Check timeout expiry if sequence is already in progress (waitingStep > 0)
        if seq_state.get("waitingStep", 0) > 0 and seq_state.get("expiresAt"):
            exp_dt = parse_ist_datetime(seq_state["expiresAt"])
            if eval_dt_ist > exp_dt:
                state.reset_sequence(node_id, reason="TIMEOUT_EXPIRED", timestamp_iso=eval_iso)
                seq_state = state.get_sequence(node_id)
                expired_reset = True

        # Evaluate all steps so crossing history (`node_history`) stays up to date and diagnostics are complete
        step_results: list[dict[str, Any]] = []
        for idx, st in enumerate(enabled_steps):
            res = self.evaluate_node(
                st,
                strategy,
                candles_by_tf,
                eval_dt_ist,
                day_str,
                state,
                indicator_cache,
                indicator_defs,
                live_tick,
                session_start_hhmm,
                primary_timeframe,
                manual_levels_override,
            )
            res["stepIndex"] = idx
            res["stepOrder"] = st.get("order", idx + 1)
            step_results.append(res)

        waiting_step = int(seq_state.get("waitingStep", 0))
        if waiting_step < 0 or waiting_step >= len(enabled_steps):
            waiting_step = 0
            seq_state["waitingStep"] = 0

        steps_advanced_this_tick = 0
        sequence_completed = False

        while waiting_step < len(enabled_steps):
            current_step_res = step_results[waiting_step]
            if current_step_res.get("blocked"):
                return {
                    "id": node_id,
                    "type": "sequence",
                    "enabled": True,
                    "blocked": True,
                    "matched": False,
                    "waitingStep": waiting_step,
                    "stepTimestamps": copy.deepcopy(seq_state.get("stepTimestamps", {})),
                    "steps": step_results,
                    "reason": f"Sequence blocked at step {waiting_step + 1}: {current_step_res.get('reason')}",
                }

            if current_step_res.get("matched", False):
                step_key = f"step_{waiting_step + 1}"
                seq_state.setdefault("stepTimestamps", {})[step_key] = eval_iso
                if waiting_step == 0:
                    seq_state["startedAt"] = eval_iso
                    if timeout_minutes is not None and int(timeout_minutes) >= 1:
                        exp_dt = eval_dt_ist + timedelta(minutes=int(timeout_minutes))
                        seq_state["expiresAt"] = exp_dt.isoformat()
                    else:
                        seq_state["expiresAt"] = None

                waiting_step += 1
                seq_state["waitingStep"] = waiting_step
                seq_state["lastStepEvalKey"] = eval_iso
                steps_advanced_this_tick += 1

                if waiting_step >= len(enabled_steps):
                    sequence_completed = True
                    seq_state["completedAt"] = eval_iso
                    break

                if not allow_same_tick:
                    # Must wait for a subsequent evaluation tick for the next step
                    break
            else:
                break

        total_steps = len(enabled_steps)
        if sequence_completed:
            completed_timestamps = copy.deepcopy(seq_state.get("stepTimestamps", {}))
            # Reset sequence state on completion so it requires a fresh Step 1 -> Step N cycle next time
            state.reset_sequence(node_id, reason="COMPLETED", timestamp_iso=eval_iso)
            reason_msg = (
                f"Sequence completed all {total_steps} steps in order "
                f"({'same-tick allowed' if allow_same_tick else 'multi-tick'})."
            )
            return {
                "id": node_id,
                "type": "sequence",
                "enabled": True,
                "matched": True,
                "allowSameTick": allow_same_tick,
                "timeoutMinutes": timeout_minutes,
                "waitingStep": total_steps,
                "completedStepTimestamps": completed_timestamps,
                "expiredBeforeTick": expired_reset,
                "steps": step_results,
                "reason": reason_msg,
            }

        prefix = "Timeout expired and reset; " if expired_reset else ""
        return {
            "id": node_id,
            "type": "sequence",
            "enabled": True,
            "matched": False,
            "allowSameTick": allow_same_tick,
            "timeoutMinutes": timeout_minutes,
            "waitingStep": waiting_step,
            "startedAt": seq_state.get("startedAt"),
            "expiresAt": seq_state.get("expiresAt"),
            "stepTimestamps": copy.deepcopy(seq_state.get("stepTimestamps", {})),
            "expiredBeforeTick": expired_reset,
            "steps": step_results,
            "reason": f"{prefix}Sequence waiting for step {waiting_step + 1} of {total_steps} ({steps_advanced_this_tick} advanced this tick).",
        }

    def evaluate_node(
        self,
        node: dict,
        strategy: dict,
        candles_by_tf: dict[str, Sequence[dict]],
        eval_dt_ist: datetime,
        day_str: str,
        state: EvaluationState,
        indicator_cache: dict[tuple[str, int], dict[str, list[Optional[float]]]],
        indicator_defs: dict[str, dict],
        live_tick: Optional[dict],
        session_start_hhmm: str,
        primary_timeframe: str,
        manual_levels_override: Optional[dict[str, float]] = None,
    ) -> dict[str, Any]:
        node_type = node.get("type")
        node_id = node.get("id", "node")
        if not node.get("enabled", True):
            return {
                "id": node_id,
                "type": node_type,
                "enabled": False,
                "matched": False,
                "reason": "Node disabled",
            }

        if node_type == "leaf":
            return self._evaluate_leaf(
                node,
                strategy,
                candles_by_tf,
                eval_dt_ist,
                day_str,
                state,
                indicator_cache,
                indicator_defs,
                live_tick,
                session_start_hhmm,
                primary_timeframe,
                manual_levels_override,
            )

        if node_type == "group":
            logic = str(node.get("logic", "AND")).upper()
            children = sorted(node.get("children") or [], key=lambda c: c.get("order", 1))
            enabled_children = [c for c in children if c.get("enabled", True)]
            if not enabled_children:
                return {
                    "id": node_id,
                    "type": "group",
                    "logic": logic,
                    "enabled": True,
                    "matched": False,
                    "children": [],
                    "reason": "Group has no enabled children",
                }
            child_results = [
                self.evaluate_node(
                    child,
                    strategy,
                    candles_by_tf,
                    eval_dt_ist,
                    day_str,
                    state,
                    indicator_cache,
                    indicator_defs,
                    live_tick,
                    session_start_hhmm,
                    primary_timeframe,
                    manual_levels_override,
                )
                for child in children
            ]
            active_results = [r for r in child_results if r.get("enabled", True)]
            if any(r.get("blocked") for r in active_results):
                matched = False
                reason = "Blocked by child condition missing mandatory parameter"
            elif logic == "AND":
                matched = all(r.get("matched", False) for r in active_results)
                reason = f"AND group ({sum(1 for r in active_results if r.get('matched'))}/{len(active_results)} matched)"
            else:
                matched = any(r.get("matched", False) for r in active_results)
                reason = f"OR group ({sum(1 for r in active_results if r.get('matched'))}/{len(active_results)} matched)"
            return {
                "id": node_id,
                "type": "group",
                "logic": logic,
                "enabled": True,
                "matched": matched,
                "children": child_results,
                "reason": reason,
            }

        if node_type == "sequence":
            return self._evaluate_sequence(
                node,
                strategy,
                candles_by_tf,
                eval_dt_ist,
                day_str,
                state,
                indicator_cache,
                indicator_defs,
                live_tick,
                session_start_hhmm,
                primary_timeframe,
                manual_levels_override,
            )

        return {
            "id": node_id,
            "type": str(node_type),
            "enabled": True,
            "matched": False,
            "reason": f"Unsupported condition node type '{node_type}'",
        }

    def evaluate_strategy(
        self,
        strategy: dict,
        candles_by_tf: dict[str, Sequence[dict]],
        eval_time: Any,
        state: Optional[EvaluationState] = None,
        live_tick: Optional[dict] = None,
        execution_quote: Optional[dict] = None,
        manual_levels_override: Optional[dict[str, float]] = None,
        primary_timeframe: str = "5m",
        auto_record_entry: bool = False,
    ) -> dict[str, Any]:
        """Evaluate all entry, re-entry, and exit rules of a RULE_BUILDER strategy at `eval_time` (IST)."""
        eval_dt_ist = parse_ist_datetime(eval_time)
        eval_state = state or EvaluationState()
        day_str = eval_dt_ist.strftime("%Y-%m-%d")

        if eval_state.last_eval_day and eval_state.last_eval_day != day_str:
            eval_state.reset_for_new_day(day_str)
        eval_state.last_eval_day = day_str

        validation_errors = validate_rule_builder(strategy)
        if validation_errors:
            return {
                "valid": False,
                "errors": validation_errors,
                "sessionAllowed": False,
                "signals": [],
                "entryResults": [],
                "exitResults": [],
                "reentryEligibility": None,
                "state": eval_state.to_dict(),
            }

        session_cfg = strategy.get("session") or {}
        session_check = check_session_and_holiday(session_cfg, eval_dt_ist)
        session_start_hhmm = session_cfg.get("startTime", "09:15")

        if not session_check["allowed"]:
            return {
                "valid": True,
                "errors": [],
                "evaluatedAtIst": eval_dt_ist.isoformat(),
                "day": day_str,
                "sessionAllowed": False,
                "sessionReason": session_check["reason"],
                "signals": [],
                "entryResults": [],
                "exitResults": [],
                "reentryEligibility": None,
                "state": eval_state.to_dict(),
            }

        indicator_defs = {ind["id"]: ind for ind in (strategy.get("indicators") or []) if isinstance(ind, dict) and "id" in ind}
        indicator_cache: dict[tuple[str, int], dict[str, list[Optional[float]]]] = {}

        exec_cfg = strategy.get("execution") or {}
        inst_cfg = exec_cfg.get("instrument") or {}
        underlying_sym = inst_cfg.get("underlying") or (strategy.get("meta") or {}).get("symbol") or "NIFTY 50"
        signal_source = inst_cfg.get("signalSource", "INDEX_SPOT")
        exec_inst_type = inst_cfg.get("instrumentType", "INDEX_SPOT")
        exec_contract = inst_cfg.get("contractSymbol") or underlying_sym
        exec_price = (execution_quote or {}).get("ltp") or (live_tick or {}).get("executionLtp") or (live_tick or {}).get("ltp")

        reentry_cfg = strategy.get("reentry") or {}
        is_reentry_phase = bool(eval_state.reentry_state.get("awaitingReentry"))
        reentry_Overview = check_reentry_eligibility(reentry_cfg, eval_state.reentry_state, eval_dt_ist)

        rules_cfg = strategy.get("rules") or {}
        entry_rules = sorted(rules_cfg.get("entry") or [], key=lambda r: r.get("order", 1))
        exit_rules = sorted(rules_cfg.get("exit") or [], key=lambda r: r.get("order", 1))

        entry_results = []
        exit_results = []
        signals = []

        for rule in entry_rules:
            r_id = rule.get("id", "entry")
            r_side = rule.get("side", "BUY")
            if rule.get("unresolvedConflict"):
                entry_results.append({
                    "ruleId": r_id,
                    "label": rule.get("label", ""),
                    "side": r_side,
                    "enabled": False,
                    "blocked": True,
                    "matched": False,
                    "reason": rule.get("conflictNote") or "Blocked: unresolved specification conflict",
                })
                continue
            if not rule.get("enabled", True):
                entry_results.append({
                    "ruleId": r_id,
                    "label": rule.get("label", ""),
                    "side": r_side,
                    "enabled": False,
                    "matched": False,
                    "reason": "Rule disabled",
                })
                continue

            # If we are in a re-entry cycle (after an earlier trade exited today), check re-entry eligibility for this side
            if is_reentry_phase:
                side_elig = check_reentry_eligibility(
                    reentry_cfg,
                    eval_state.reentry_state,
                    eval_dt_ist,
                    candidate_side=r_side,
                )
                if not side_elig["eligible"]:
                    entry_results.append({
                        "ruleId": r_id,
                        "label": rule.get("label", ""),
                        "side": r_side,
                        "enabled": True,
                        "blockedByReentry": True,
                        "matched": False,
                        "reentryCheck": side_elig,
                        "reason": side_elig["reason"],
                    })
                    continue

            cond_res = self.evaluate_node(
                rule["condition"],
                strategy,
                candles_by_tf,
                eval_dt_ist,
                day_str,
                eval_state,
                indicator_cache,
                indicator_defs,
                live_tick,
                session_start_hhmm,
                primary_timeframe,
                manual_levels_override,
            )
            matched = bool(cond_res.get("matched", False))
            res_item = {
                "ruleId": r_id,
                "label": rule.get("label", ""),
                "side": r_side,
                "enabled": True,
                "isReentry": is_reentry_phase,
                "matched": matched,
                "condition": cond_res,
            }
            entry_results.append(res_item)
            if matched:
                active_sl = reentry_cfg.get("stopLoss") if (is_reentry_phase and reentry_cfg.get("stopLoss")) else strategy.get("stopLoss")
                active_targets = reentry_cfg.get("targets") if (is_reentry_phase and reentry_cfg.get("targets")) else strategy.get("targets")
                sig = {
                    "bucket": "REENTRY" if is_reentry_phase else "ENTRY",
                    "isReentry": is_reentry_phase,
                    "reentryNumber": (int(eval_state.reentry_state.get("reentriesToday", 0)) + 1) if is_reentry_phase else 0,
                    "ruleId": r_id,
                    "label": rule.get("label", ""),
                    "side": r_side,
                    "evaluatedAtIst": eval_dt_ist.isoformat(),
                    "signalSource": signal_source,
                    "underlyingSymbol": underlying_sym,
                    "executionInstrumentType": exec_inst_type,
                    "executionContract": exec_contract,
                    "executionPrice": exec_price,
                    "quantity": exec_cfg.get("quantity", 1),
                    "lotSize": inst_cfg.get("lotSize"),
                    "stopLoss": active_sl,
                    "targets": active_targets,
                }
                signals.append(sig)
                if auto_record_entry:
                    eval_state.record_trade_entry(
                        side=r_side,
                        timestamp_ist=eval_dt_ist,
                        is_reentry=is_reentry_phase,
                        rule_id=r_id,
                    )
                    break

        for rule in exit_rules:
            r_id = rule.get("id", "exit")
            if rule.get("unresolvedConflict") or not rule.get("enabled", True):
                exit_results.append({
                    "ruleId": r_id,
                    "label": rule.get("label", ""),
                    "side": rule.get("side"),
                    "enabled": False,
                    "blocked": bool(rule.get("unresolvedConflict")),
                    "matched": False,
                    "reason": rule.get("conflictNote") if rule.get("unresolvedConflict") else "Rule disabled",
                })
                continue

            cond_res = self.evaluate_node(
                rule["condition"],
                strategy,
                candles_by_tf,
                eval_dt_ist,
                day_str,
                eval_state,
                indicator_cache,
                indicator_defs,
                live_tick,
                session_start_hhmm,
                primary_timeframe,
                manual_levels_override,
            )
            matched = bool(cond_res.get("matched", False))
            exit_results.append({
                "ruleId": r_id,
                "label": rule.get("label", ""),
                "side": rule.get("side"),
                "enabled": True,
                "matched": matched,
                "condition": cond_res,
            })
            if matched:
                signals.append({
                    "bucket": "EXIT",
                    "ruleId": r_id,
                    "label": rule.get("label", ""),
                    "side": rule.get("side"),
                    "evaluatedAtIst": eval_dt_ist.isoformat(),
                    "signalSource": signal_source,
                    "underlyingSymbol": underlying_sym,
                    "executionInstrumentType": exec_inst_type,
                    "executionContract": exec_contract,
                    "executionPrice": exec_price,
                    "quantity": exec_cfg.get("quantity", 1),
                    "lotSize": inst_cfg.get("lotSize"),
                })

        return {
            "valid": True,
            "errors": [],
            "evaluatedAtIst": eval_dt_ist.isoformat(),
            "day": day_str,
            "sessionAllowed": True,
            "sessionReason": None,
            "signalSource": signal_source,
            "underlyingSymbol": underlying_sym,
            "executionInstrument": {
                "instrumentType": exec_inst_type,
                "contractSymbol": exec_contract,
                "optionType": inst_cfg.get("optionType"),
                "strike": inst_cfg.get("strike"),
                "expiry": inst_cfg.get("expiry"),
                "lotSize": inst_cfg.get("lotSize"),
                "executionPrice": exec_price,
            },
            "reentryEligibility": reentry_Overview,
            "signals": signals,
            "entryResults": entry_results,
            "exitResults": exit_results,
            "state": eval_state.to_dict(),
        }
