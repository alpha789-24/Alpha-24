"""Phase 4 Exit, Stop Loss, Multi-Target, and Trailing Stop Loss Engine (Backend).

Shared deterministic exit and position-management engine for backtesting, paper trading, and live trading:
1. Stop Loss Types (`resolve_stop_loss_price`):
   - `fixed_points`: `entryPrice - points` (BUY) / `entryPrice + points` (SELL)
   - `percentage`: `entryPrice * (1 - percent/100)` (BUY) / `entryPrice * (1 + percent/100)` (SELL)
   - `indicator`: value of `indicatorId` on the closed candle at trade entry (or trailing trigger)
   - `candle`: N-candle lookback (`candleCount` >= 1) on closed candles using comparison price
     `CLOSE`, `HIGH_LOW` (lowest LOW for BUY, highest HIGH for SELL), or `OPEN`, plus `bufferPercent`
   - `level`: manual FC/RC level value for the trading day
2. Multi-Target Partial Exits (`resolve_targets`):
   - Three or more targets ordered by `order`
   - Optional targets can be disabled; the final target (highest `order`) stays enabled
   - Supports `points`, `percent`, and `price_level` (any FC/RC level)
   - Validates that enabled target quantities sum to position quantity
   - Executes partial exits per target and reduces `remainingQuantity`
3. Trailing Stop Loss Engine (`evaluate_trailing_sl`):
   - Each step is `IF <condition> THEN MOVE SL TO <level|price|points|indicator>`, independently enabled and ordered
   - Monotonic guard: LONG (`BUY`) SL never moves down (`new_sl > current_sl`); SHORT (`SELL`) SL never moves up (`new_sl < current_sl`)
   - Idempotent: each trailing step fires at most once per trade (`firedTrailingStepIds`)
   - Records complete modification history (`slHistory`: `timeIst`, `oldSl`, `newSl`, `ruleId`, `label`, `triggerPrice`, `moveKind`, `reason`)
4. Intrabar Ambiguity Resolution (`step_open_position`):
   - When a single candle touches both SL and one or more targets, resolves execution order via configurable
     `intrabarConflictOrder`: `SL_FIRST` (default), `TARGET_FIRST`, or `LOWER_TIMEFRAME` (inspects chronological
     1m/lower-TF sub-candles inside the bar when available, falling back to `SL_FIRST` if sub-candles are absent
     or also ambiguous)
   - Records `intrabarResolutionUsed` on every exit event and on the trade log.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from .condition_engine import (
    FLOAT_EPSILON,
    IST,
    ConditionEngine,
    EvaluationState,
    candle_close_time_ist,
    candle_open_time_ist,
    filter_closed_candles,
    parse_ist_datetime,
)
from .indicators import evaluate_indicator_config
from .levels import FormulaNotProvided, LevelBook, LevelError
from .schema_v2 import LEVEL_NAMES


class ExitEngineError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _lookup_day_level(
    level_book: Optional[LevelBook],
    strategy: dict,
    day_str: str,
    level_name: str,
    manual_levels_override: Optional[dict[str, float]] = None,
) -> float:
    lvl = str(level_name or "").strip().upper()
    if lvl not in LEVEL_NAMES:
        raise ExitEngineError(f"Unknown FC/RC level '{level_name}'.")
    if manual_levels_override and lvl in manual_levels_override:
        val = manual_levels_override[lvl]
        if val is not None and not isinstance(val, bool) and math.isfinite(float(val)):
            return float(val)

    provider = str((strategy.get("levels") or {}).get("provider", "MANUAL")).upper()
    if provider == "FORMULA":
        raise ExitEngineError("formula not provided")
    if level_book is None:
        raise ExitEngineError(f"LevelBook is required to resolve level '{lvl}' on {day_str}.")

    symbol = (strategy.get("meta") or {}).get("symbol") or strategy.get("symbol") or "NIFTY 50"
    if (strategy.get("execution") or {}).get("instrument", {}).get("underlying"):
        symbol = strategy["execution"]["instrument"]["underlying"]
    try:
        quote = level_book.lookup(provider, symbol, day_str, lvl)
        if quote.value is None:
            raise ExitEngineError(f"Level '{lvl}' has no value on {day_str}.")
        return float(quote.value)
    except (FormulaNotProvided, LevelError) as exc:
        raise ExitEngineError(str(exc)) from exc


def _resolve_indicator_latest_value(
    indicator_id: str,
    strategy: dict,
    candles_by_tf: dict[str, Sequence[dict]],
    eval_dt_ist: datetime,
    primary_timeframe: str = "5m",
) -> float:
    indicators = strategy.get("indicators") or []
    ind_cfg = next((i for i in indicators if isinstance(i, dict) and i.get("id") == indicator_id), None)
    if ind_cfg is None:
        raise ExitEngineError(f"Unknown indicator '{indicator_id}'.")
    tf = ind_cfg.get("timeframe", primary_timeframe)
    raw_candles = candles_by_tf.get(tf) or candles_by_tf.get(primary_timeframe) or []
    closed_candles = filter_closed_candles(raw_candles, tf, eval_dt_ist)
    if not closed_candles:
        raise ExitEngineError(f"No closed candles available on {tf} to compute indicator '{indicator_id}'.")
    series_map = evaluate_indicator_config(ind_cfg, closed_candles)
    series = series_map.get("value") or []
    if not series or series[-1] is None:
        raise ExitEngineError(f"Indicator '{indicator_id}' is still in warm-up period at {eval_dt_ist.isoformat()}.")
    return float(series[-1])


def resolve_stop_loss_price(
    stop_cfg: dict,
    side: str,
    entry_price: float,
    strategy: dict,
    candles_by_tf: dict[str, Sequence[dict]],
    eval_time: Any,
    level_book: Optional[LevelBook] = None,
    manual_levels_override: Optional[dict[str, float]] = None,
    primary_timeframe: str = "5m",
) -> dict[str, Any]:
    """Compute the initial numeric stop-loss price from a `StopLoss` configuration."""
    if not stop_cfg or not stop_cfg.get("enabled", True):
        raise ExitEngineError("Stop loss must be enabled.")
    side_u = str(side or "BUY").upper()
    if side_u not in ("BUY", "SELL"):
        raise ExitEngineError(f"Invalid trade side '{side}'.")
    eval_dt_ist = parse_ist_datetime(eval_time)
    day_str = eval_dt_ist.strftime("%Y-%m-%d")
    method = str(stop_cfg.get("method", "")).lower()

    if method == "fixed_points":
        pts = float(stop_cfg["points"])
        sl_price = (entry_price - pts) if side_u == "BUY" else (entry_price + pts)
        reason = f"fixed_points ({pts} pts from entry {entry_price})"

    elif method == "percentage":
        pct = float(stop_cfg["percent"])
        delta = entry_price * (pct / 100.0)
        sl_price = (entry_price - delta) if side_u == "BUY" else (entry_price + delta)
        reason = f"percentage ({pct}% from entry {entry_price})"

    elif method == "indicator":
        ind_id = stop_cfg["indicatorId"]
        sl_price = _resolve_indicator_latest_value(
            ind_id, strategy, candles_by_tf, eval_dt_ist, primary_timeframe=primary_timeframe
        )
        reason = f"indicator ({ind_id} = {round(sl_price, 4)})"

    elif method == "candle":
        count = int(stop_cfg["candleCount"])
        comp_price = str(stop_cfg["candlePrice"]).upper()
        buffer_pct = float(stop_cfg.get("bufferPercent") or 0.0)
        raw_candles = candles_by_tf.get(primary_timeframe) or []
        closed = filter_closed_candles(raw_candles, primary_timeframe, eval_dt_ist)
        if len(closed) < count:
            raise ExitEngineError(
                f"Candle stop loss requires {count} closed candles on {primary_timeframe}, found {len(closed)}."
            )
        window = closed[-count:]
        if comp_price == "CLOSE":
            ref_vals = [float(c["close"]) for c in window]
            base_val = min(ref_vals) if side_u == "BUY" else max(ref_vals)
        elif comp_price == "OPEN":
            ref_vals = [float(c["open"]) for c in window]
            base_val = min(ref_vals) if side_u == "BUY" else max(ref_vals)
        elif comp_price == "HIGH_LOW":
            base_val = min(float(c["low"]) for c in window) if side_u == "BUY" else max(float(c["high"]) for c in window)
        else:
            raise ExitEngineError(f"Unsupported candlePrice '{comp_price}'.")

        buf_delta = abs(base_val) * (buffer_pct / 100.0)
        sl_price = (base_val - buf_delta) if side_u == "BUY" else (base_val + buf_delta)
        reason = f"candle ({count}-bar {comp_price} base={round(base_val, 4)}, buffer={buffer_pct}%)"

    elif method == "level":
        lvl_name = stop_cfg["level"]
        sl_price = _lookup_day_level(level_book, strategy, day_str, lvl_name, manual_levels_override)
        reason = f"level ({lvl_name} = {round(sl_price, 4)})"

    else:
        raise ExitEngineError(f"Unsupported stop loss method '{method}'.")

    return {
        "id": stop_cfg.get("id", "sl"),
        "method": method,
        "price": round(float(sl_price), 4),
        "reason": reason,
    }


def resolve_targets(
    targets_cfg: Sequence[dict],
    side: str,
    entry_price: float,
    total_quantity: int,
    strategy: dict,
    eval_time: Any,
    level_book: Optional[LevelBook] = None,
    manual_levels_override: Optional[dict[str, float]] = None,
    require_three_minimum: bool = True,
) -> list[dict[str, Any]]:
    """Resolve and validate ordered targets for a position."""
    if require_three_minimum and len(targets_cfg) < 3:
        raise ExitEngineError("At least three targets are required.")
    if not targets_cfg:
        raise ExitEngineError("At least one target is required.")

    ordered = sorted(targets_cfg, key=lambda t: int(t.get("order", 1)))
    final_target = ordered[-1]
    if not final_target.get("enabled", True):
        raise ExitEngineError("The final target (highest order) must stay enabled.")

    enabled_targets = [t for t in ordered if t.get("enabled", True)]
    qty_sum = sum(int(t.get("quantity", 0)) for t in enabled_targets)
    if qty_sum != int(total_quantity):
        raise ExitEngineError(
            f"Enabled target quantities sum to {qty_sum}, which does not equal position quantity {total_quantity}."
        )

    side_u = str(side or "BUY").upper()
    eval_dt_ist = parse_ist_datetime(eval_time)
    day_str = eval_dt_ist.strftime("%Y-%m-%d")

    resolved: list[dict[str, Any]] = []
    for idx, tgt in enumerate(ordered):
        enabled = bool(tgt.get("enabled", True))
        unit = str(tgt.get("unit", "points")).lower()
        val = float(tgt.get("value", 0.0))
        qty = int(tgt.get("quantity", 0))
        lvl_name = tgt.get("level")
        is_final = (idx == len(ordered) - 1)

        if not enabled:
            resolved.append({
                "id": tgt.get("id", f"tgt-{idx+1}"),
                "label": tgt.get("label", f"Target {idx+1}"),
                "order": int(tgt.get("order", idx + 1)),
                "enabled": False,
                "isFinal": is_final,
                "unit": unit,
                "value": val,
                "level": lvl_name,
                "quantity": 0,
                "targetPrice": None,
                "filled": False,
                "filledAtIst": None,
                "fillPrice": None,
            })
            continue

        if unit == "points":
            t_price = (entry_price + val) if side_u == "BUY" else (entry_price - val)
        elif unit == "percent":
            delta = entry_price * (val / 100.0)
            t_price = (entry_price + delta) if side_u == "BUY" else (entry_price - delta)
        elif unit == "price_level":
            if not lvl_name:
                raise ExitEngineError(f"Target '{tgt.get('id')}' requires an FC/RC level.")
            t_price = _lookup_day_level(level_book, strategy, day_str, lvl_name, manual_levels_override)
        else:
            raise ExitEngineError(f"Unsupported target unit '{unit}'.")

        resolved.append({
            "id": tgt.get("id", f"tgt-{idx+1}"),
            "label": tgt.get("label", f"Target {idx+1}"),
            "order": int(tgt.get("order", idx + 1)),
            "enabled": True,
            "isFinal": is_final,
            "unit": unit,
            "value": val,
            "level": lvl_name,
            "quantity": qty,
            "targetPrice": round(float(t_price), 4),
            "filled": False,
            "filledAtIst": None,
            "fillPrice": None,
        })
    return resolved


def open_managed_position(
    strategy: dict,
    side: str,
    entry_price: float,
    entry_time: Any,
    candles_by_tf: dict[str, Sequence[dict]],
    level_book: Optional[LevelBook] = None,
    manual_levels_override: Optional[dict[str, float]] = None,
    primary_timeframe: str = "5m",
    is_reentry: bool = False,
    entry_rule_id: Optional[str] = None,
    lot_size: Optional[int] = None,
) -> dict[str, Any]:
    """Create a managed position state with resolved initial SL, partial targets, and empty trailing SL history."""
    eval_dt_ist = parse_ist_datetime(entry_time)
    exec_cfg = strategy.get("execution") or {}
    inst_cfg = exec_cfg.get("instrument") or {}
    total_qty = int(exec_cfg.get("quantity", 1))
    from .market_service import _resolve_lot_size
    symbol = inst_cfg.get("underlying") or (strategy.get("meta") or {}).get("symbol") or ""
    lot_warnings: list[str] = []
    if lot_size is not None:
        raw_lot = lot_size
    else:
        master_lot = _resolve_lot_size(symbol)
        strat_lot = inst_cfg.get("lotSize")
        if master_lot is not None:
            raw_lot = master_lot
            if strat_lot is not None:
                try:
                    if int(strat_lot) != int(master_lot):
                        lot_warnings.append(
                            f"Strategy lotSize ({strat_lot}) differs from contract master ({master_lot}) for {symbol}; using contract master value {master_lot}."
                        )
                except (TypeError, ValueError):
                    pass
        else:
            raw_lot = strat_lot
    try:
        eff_lot_size = int(raw_lot) if raw_lot is not None else 0
    except (TypeError, ValueError):
        eff_lot_size = 0
    if eff_lot_size < 1:
        raise ExitEngineError("Lot size is unknown (contract master missing or stale); cannot size position.")

    reentry_cfg = strategy.get("reentry") or {}
    if is_reentry and reentry_cfg.get("stopLoss"):
        sl_cfg = reentry_cfg["stopLoss"]
    else:
        sl_cfg = strategy.get("stopLoss")

    if is_reentry and reentry_cfg.get("targets"):
        raw_targets = reentry_cfg["targets"]
        require_three = len(raw_targets) >= 3
    else:
        raw_targets = strategy.get("targets") or []
        require_three = True

    sl_res = resolve_stop_loss_price(
        sl_cfg,
        side=side,
        entry_price=entry_price,
        strategy=strategy,
        candles_by_tf=candles_by_tf,
        eval_time=eval_dt_ist,
        level_book=level_book,
        manual_levels_override=manual_levels_override,
        primary_timeframe=primary_timeframe,
    )
    targets_res = resolve_targets(
        raw_targets,
        side=side,
        entry_price=entry_price,
        total_quantity=total_qty,
        strategy=strategy,
        eval_time=eval_dt_ist,
        level_book=level_book,
        manual_levels_override=manual_levels_override,
        require_three_minimum=require_three,
    )

    intrabar_mode = str(exec_cfg.get("intrabarConflictOrder") or "SL_FIRST").upper()

    return {
        "strategyId": strategy.get("id"),
        "side": str(side).upper(),
        "isReentry": bool(is_reentry),
        "entryRuleId": entry_rule_id,
        "entryPrice": round(float(entry_price), 4),
        "entryAtIst": eval_dt_ist.isoformat(),
        "initialQuantity": total_qty,
        "remainingQuantity": total_qty,
        "lotSize": eff_lot_size,
        "warnings": lot_warnings,
        "initialStopLoss": sl_res["price"],
        "currentStopLoss": sl_res["price"],
        "stopLossMethod": sl_res["method"],
        "stopLossReason": sl_res["reason"],
        "targets": targets_res,
        "firedTrailingStepIds": [],
        "slHistory": [],
        "partialExits": [],
        "realizedPnl": 0.0,
        "status": "OPEN",
        "exitReason": None,
        "exitAtIst": None,
        "exitPrice": None,
        "intrabarConflictOrder": intrabar_mode,
        "intrabarResolutionLog": [],
    }


def _resolve_move_sl_target_price(
    move_cfg: dict,
    side: str,
    entry_price: float,
    current_sl: float,
    trigger_price: float,
    strategy: dict,
    candles_by_tf: dict[str, Sequence[dict]],
    eval_dt_ist: datetime,
    level_book: Optional[LevelBook],
    manual_levels_override: Optional[dict[str, float]],
    primary_timeframe: str,
) -> tuple[float, str]:
    kind = str(move_cfg.get("kind", "")).lower()
    day_str = eval_dt_ist.strftime("%Y-%m-%d")
    side_u = side.upper()

    if kind == "level":
        lvl_name = move_cfg["level"]
        val = _lookup_day_level(level_book, strategy, day_str, lvl_name, manual_levels_override)
        return round(float(val), 4), f"level:{lvl_name} ({round(float(val), 4)})"

    if kind == "price":
        val = float(move_cfg["price"])
        return round(val, 4), f"price:{round(val, 4)}"

    if kind == "points":
        pts = float(move_cfg["points"])
        # Move SL positively by `pts` relative to initial SL / current SL (or lock in +pts from current SL)
        val = (current_sl + pts) if side_u == "BUY" else (current_sl - pts)
        return round(val, 4), f"points:+{pts} from {current_sl}"

    if kind == "indicator":
        ind_id = move_cfg["indicatorId"]
        val = _resolve_indicator_latest_value(
            ind_id, strategy, candles_by_tf, eval_dt_ist, primary_timeframe=primary_timeframe
        )
        return round(float(val), 4), f"indicator:{ind_id} ({round(float(val), 4)})"

    raise ExitEngineError(f"Unsupported moveSlTo.kind '{kind}'.")


def evaluate_trailing_steps(
    position: dict,
    strategy: dict,
    condition_engine: ConditionEngine,
    candles_by_tf: dict[str, Sequence[dict]],
    eval_time: Any,
    eval_state: Optional[EvaluationState] = None,
    live_tick: Optional[dict] = None,
    manual_levels_override: Optional[dict[str, float]] = None,
    primary_timeframe: str = "5m",
) -> list[dict[str, Any]]:
    """Evaluate all enabled, unfired `TrailingStep` rules in `order` for an open position.

    Enforces:
    - Independent enable/disable and ascending `order`.
    - Idempotency: each step ID in `position['firedTrailingStepIds']` never fires a second time on the same trade.
    - Monotonicity:
      - LONG (`BUY`): `new_sl` must be strictly greater than `currentStopLoss` (`new_sl > current_sl + FLOAT_EPSILON`).
      - SHORT (`SELL`): `new_sl` must be strictly less than `currentStopLoss` (`new_sl < current_sl - FLOAT_EPSILON`).
      If a step's condition matches cleanly, the step is marked as fired (`idempotent`) and if the candidate SL
      would loosen the stop (move down for LONG or move up for SHORT), the move is rejected by the monotonic guard
      while logging the attempt, or if it tightens the stop, `currentStopLoss` is updated and appended to `slHistory`.
    """
    if position.get("status") != "OPEN":
        return []

    trailing_steps = sorted(strategy.get("trailing") or [], key=lambda s: int(s.get("order", 1)))
    if not trailing_steps:
        return []

    eval_dt_ist = parse_ist_datetime(eval_time)
    day_str = eval_dt_ist.strftime("%Y-%m-%d")
    state = eval_state or EvaluationState()
    session_start_hhmm = (strategy.get("session") or {}).get("startTime", "09:15")
    indicator_defs = {ind["id"]: ind for ind in (strategy.get("indicators") or []) if isinstance(ind, dict) and "id" in ind}
    indicator_cache: dict[tuple[str, int], dict[str, list[Optional[float]]]] = {}

    # Determine trigger reference price from live_tick or latest closed primary candle
    trigger_price = position.get("entryPrice", 0.0)
    if live_tick and live_tick.get("ltp") is not None:
        trigger_price = float(live_tick["ltp"])
    else:
        closed_primary = filter_closed_candles(candles_by_tf.get(primary_timeframe) or [], primary_timeframe, eval_dt_ist)
        if closed_primary:
            trigger_price = float(closed_primary[-1]["close"])

    fired_set = set(position.get("firedTrailingStepIds") or [])
    side_u = str(position.get("side", "BUY")).upper()
    modifications: list[dict[str, Any]] = []

    for step in trailing_steps:
        if not step.get("enabled", True):
            continue
        step_id = step.get("id", "trail")
        if step_id in fired_set:
            # Idempotent: already fired on this trade
            continue

        cond_res = condition_engine.evaluate_node(
            step["condition"],
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
        if not cond_res.get("matched", False):
            continue

        # Condition matched! Compute candidate new SL
        old_sl = float(position["currentStopLoss"])
        candidate_sl, move_desc = _resolve_move_sl_target_price(
            step["moveSlTo"],
            side=side_u,
            entry_price=float(position["entryPrice"]),
            current_sl=old_sl,
            trigger_price=trigger_price,
            strategy=strategy,
            candles_by_tf=candles_by_tf,
            eval_dt_ist=eval_dt_ist,
            level_book=condition_engine.level_book,
            manual_levels_override=manual_levels_override,
            primary_timeframe=primary_timeframe,
        )

        # Mark step as fired (once per trade idempotency)
        fired_set.add(step_id)
        position["firedTrailingStepIds"] = list(fired_set)

        # Enforce monotonic SL rule: LONG SL never moves down, SHORT SL never moves up
        is_monotonic_improvement = (
            (side_u == "BUY" and candidate_sl > old_sl + FLOAT_EPSILON)
            or (side_u == "SELL" and candidate_sl < old_sl - FLOAT_EPSILON)
        )
        if is_monotonic_improvement:
            position["currentStopLoss"] = candidate_sl
            mod_entry = {
                "timeIst": eval_dt_ist.isoformat(),
                "oldSl": old_sl,
                "newSl": candidate_sl,
                "ruleId": step_id,
                "label": step.get("label", ""),
                "triggerPrice": round(trigger_price, 4),
                "moveKind": step["moveSlTo"].get("kind"),
                "applied": True,
                "reason": f"Moved SL from {old_sl} to {candidate_sl} via {move_desc}",
            }
            position.setdefault("slHistory", []).append(mod_entry)
            modifications.append(mod_entry)
        else:
            mod_entry = {
                "timeIst": eval_dt_ist.isoformat(),
                "oldSl": old_sl,
                "newSl": old_sl,
                "candidateSl": candidate_sl,
                "ruleId": step_id,
                "label": step.get("label", ""),
                "triggerPrice": round(trigger_price, 4),
                "moveKind": step["moveSlTo"].get("kind"),
                "applied": False,
                "reason": (
                    f"Monotonic SL guard kept SL at {old_sl} (candidate {candidate_sl} would move "
                    f"{'down on LONG' if side_u == 'BUY' else 'up on SHORT'})."
                ),
            }
            position.setdefault("slHistory", []).append(mod_entry)
            modifications.append(mod_entry)

    return modifications


def _check_candle_touches(
    side: str,
    candle_high: float,
    candle_low: float,
    sl_price: float,
    unfilled_targets: list[dict],
) -> tuple[bool, list[dict]]:
    side_u = side.upper()
    if side_u == "BUY":
        sl_hit = candle_low <= sl_price + FLOAT_EPSILON
        hit_targets = [
            t for t in unfilled_targets
            if t.get("targetPrice") is not None and candle_high >= float(t["targetPrice"]) - FLOAT_EPSILON
        ]
    else:
        sl_hit = candle_high >= sl_price - FLOAT_EPSILON
        hit_targets = [
            t for t in unfilled_targets
            if t.get("targetPrice") is not None and candle_low <= float(t["targetPrice"]) + FLOAT_EPSILON
        ]
    return sl_hit, hit_targets


def _execute_sl_exit(
    position: dict,
    sl_price: float,
    eval_iso: str,
    resolution_used: str,
) -> dict[str, Any]:
    rem_qty = int(position["remainingQuantity"])
    lot_size = int(position.get("lotSize", 1))
    entry_price = float(position["entryPrice"])
    side_u = position["side"].upper()
    units = rem_qty * lot_size
    leg_pnl = round(((sl_price - entry_price) if side_u == "BUY" else (entry_price - sl_price)) * units, 2)

    exit_rec = {
        "exitType": "STOP_LOSS",
        "targetId": None,
        "label": "Stop Loss",
        "quantity": rem_qty,
        "lotSize": lot_size,
        "units": units,
        "exitPrice": round(sl_price, 4),
        "timeIst": eval_iso,
        "pnl": leg_pnl,
        "intrabarResolutionUsed": resolution_used,
    }
    position.setdefault("partialExits", []).append(exit_rec)
    position["realizedPnl"] = round(float(position.get("realizedPnl", 0.0)) + leg_pnl, 2)
    position["remainingQuantity"] = 0
    position["status"] = "CLOSED"
    position["exitReason"] = "STOP_LOSS"
    position["exitAtIst"] = eval_iso
    position["exitPrice"] = round(sl_price, 4)
    return exit_rec


def _execute_target_fill(
    position: dict,
    target: dict,
    eval_iso: str,
    resolution_used: str,
) -> Optional[dict[str, Any]]:
    rem_qty = int(position["remainingQuantity"])
    if rem_qty <= 0 or target.get("filled"):
        return None
    fill_qty = min(rem_qty, int(target["quantity"]))
    if target.get("isFinal"):
        # Final target closes any remaining quantity
        fill_qty = rem_qty
    t_price = float(target["targetPrice"])
    lot_size = int(position.get("lotSize", 1))
    entry_price = float(position["entryPrice"])
    side_u = position["side"].upper()
    units = fill_qty * lot_size
    leg_pnl = round(((t_price - entry_price) if side_u == "BUY" else (entry_price - t_price)) * units, 2)

    target["filled"] = True
    target["filledAtIst"] = eval_iso
    target["fillPrice"] = round(t_price, 4)

    exit_rec = {
        "exitType": "TARGET",
        "targetId": target["id"],
        "label": target.get("label", target["id"]),
        "order": target.get("order"),
        "isFinal": bool(target.get("isFinal")),
        "quantity": fill_qty,
        "lotSize": lot_size,
        "units": units,
        "exitPrice": round(t_price, 4),
        "timeIst": eval_iso,
        "pnl": leg_pnl,
        "intrabarResolutionUsed": resolution_used,
    }
    position.setdefault("partialExits", []).append(exit_rec)
    position["realizedPnl"] = round(float(position.get("realizedPnl", 0.0)) + leg_pnl, 2)
    position["remainingQuantity"] = rem_qty - fill_qty
    if position["remainingQuantity"] <= 0 or target.get("isFinal"):
        position["remainingQuantity"] = 0
        position["status"] = "CLOSED"
        position["exitReason"] = "TARGET"
        position["exitAtIst"] = eval_iso
        position["exitPrice"] = round(t_price, 4)
    return exit_rec


def step_open_position(
    position: dict,
    candle: dict,
    strategy: dict,
    condition_engine: ConditionEngine,
    candles_by_tf: dict[str, Sequence[dict]],
    eval_time: Any,
    eval_state: Optional[EvaluationState] = None,
    lower_tf_candles: Optional[Sequence[dict]] = None,
    manual_levels_override: Optional[dict[str, float]] = None,
    primary_timeframe: str = "5m",
) -> dict[str, Any]:
    """Process one candle (and optional lower-timeframe sub-candles) against an open position.

    Order of operations:
    1. Check if the candle touches `currentStopLoss` and/or any enabled unfilled `targets`.
    2. If BOTH SL and at least one target are touched inside the same candle (intrabar ambiguity):
       - Inspect `intrabarConflictOrder` (`SL_FIRST` default, `TARGET_FIRST`, or `LOWER_TIMEFRAME`):
         - `LOWER_TIMEFRAME`: if `lower_tf_candles` are provided for this bar's time window, iterate through
           them chronologically to establish the exact sequence of target fills vs SL hit. If a single
           lower-timeframe sub-candle still touches both simultaneously (or if no lower-timeframe data is
           available), fall back to `SL_FIRST` and record `"LOWER_TIMEFRAME_FALLBACK_SL_FIRST"`.
         - `TARGET_FIRST`: fill touched targets in ascending `order` first; if position still has remaining
           quantity after partial target fills, execute SL on the remainder.
         - `SL_FIRST` (default): execute SL immediately on all `remainingQuantity` before any target fills.
    3. If the position remains `OPEN` at the close of the candle, evaluate ordered idempotent `TrailingStep`
       rules at the candle close so the updated SL applies from the next candle/tick onward.
    """
    if position.get("status") != "OPEN" or int(position.get("remainingQuantity", 0)) <= 0:
        return {
            "position": position,
            "exitsThisStep": [],
            "trailingModifications": [],
            "intrabarAmbiguous": False,
            "intrabarResolutionUsed": "NONE",
        }

    eval_dt_ist = parse_ist_datetime(eval_time)
    eval_iso = eval_dt_ist.isoformat()
    side_u = position["side"].upper()
    c_high = float(candle["high"])
    c_low = float(candle["low"])
    sl_price = float(position["currentStopLoss"])

    unfilled_targets = [
        t for t in sorted(position.get("targets") or [], key=lambda x: int(x.get("order", 1)))
        if t.get("enabled", True) and not t.get("filled", False)
    ]

    sl_hit, hit_targets = _check_candle_touches(side_u, c_high, c_low, sl_price, unfilled_targets)
    exits_this_step: list[dict[str, Any]] = []
    ambiguous = bool(sl_hit and hit_targets)
    configured_mode = str(
        position.get("intrabarConflictOrder")
        or (strategy.get("execution") or {}).get("intrabarConflictOrder")
        or "SL_FIRST"
    ).upper()
    resolution_used = "NONE"

    if ambiguous:
        if configured_mode == "LOWER_TIMEFRAME":
            # Filter lower_tf_candles that fall inside this candle's time window
            c_open_dt = candle_open_time_ist(candle)
            c_close_dt = candle_close_time_ist(candle, primary_timeframe)
            sub_bars = []
            for sb in (lower_tf_candles or []):
                sb_open = candle_open_time_ist(sb)
                if c_open_dt <= sb_open < c_close_dt:
                    sub_bars.append(sb)

            if sub_bars:
                resolution_used = "LOWER_TIMEFRAME"
                for sb in sub_bars:
                    if position["status"] != "OPEN":
                        break
                    sb_high = float(sb["high"])
                    sb_low = float(sb["low"])
                    sb_iso = candle_open_time_ist(sb).isoformat()
                    rem_targets = [
                        t for t in sorted(position.get("targets") or [], key=lambda x: int(x.get("order", 1)))
                        if t.get("enabled", True) and not t.get("filled", False)
                    ]
                    sb_sl_hit, sb_hit_targets = _check_candle_touches(
                        side_u, sb_high, sb_low, float(position["currentStopLoss"]), rem_targets
                    )
                    if sb_sl_hit and sb_hit_targets:
                        # Sub-candle itself is ambiguous -> default to SL_FIRST on sub-candle
                        resolution_used = "LOWER_TIMEFRAME_SUBBAR_SL_FIRST"
                        ex = _execute_sl_exit(position, float(position["currentStopLoss"]), sb_iso, resolution_used)
                        exits_this_step.append(ex)
                        break
                    elif sb_sl_hit:
                        ex = _execute_sl_exit(position, float(position["currentStopLoss"]), sb_iso, resolution_used)
                        exits_this_step.append(ex)
                        break
                    elif sb_hit_targets:
                        for tgt in sb_hit_targets:
                            if position["status"] != "OPEN":
                                break
                            ex = _execute_target_fill(position, tgt, sb_iso, resolution_used)
                            if ex:
                                exits_this_step.append(ex)
            else:
                resolution_used = "LOWER_TIMEFRAME_FALLBACK_SL_FIRST"
                ex = _execute_sl_exit(position, sl_price, eval_iso, resolution_used)
                exits_this_step.append(ex)

        elif configured_mode == "TARGET_FIRST":
            resolution_used = "TARGET_FIRST"
            for tgt in hit_targets:
                if position["status"] != "OPEN":
                    break
                ex = _execute_target_fill(position, tgt, eval_iso, resolution_used)
                if ex:
                    exits_this_step.append(ex)
            if position["status"] == "OPEN" and sl_hit:
                ex = _execute_sl_exit(position, sl_price, eval_iso, resolution_used)
                exits_this_step.append(ex)

        else:
            # Default: SL_FIRST
            resolution_used = "SL_FIRST"
            ex = _execute_sl_exit(position, sl_price, eval_iso, resolution_used)
            exits_this_step.append(ex)

        position.setdefault("intrabarResolutionLog", []).append({
            "timeIst": eval_iso,
            "configuredMode": configured_mode,
            "resolutionUsed": resolution_used,
            "candleHigh": c_high,
            "candleLow": c_low,
            "stopLossAtBarStart": sl_price,
            "touchedTargetIds": [t["id"] for t in hit_targets],
        })

    elif sl_hit:
        resolution_used = "UNAMBIGUOUS_SL"
        ex = _execute_sl_exit(position, sl_price, eval_iso, resolution_used)
        exits_this_step.append(ex)

    elif hit_targets:
        resolution_used = "UNAMBIGUOUS_TARGET"
        for tgt in hit_targets:
            if position["status"] != "OPEN":
                break
            ex = _execute_target_fill(position, tgt, eval_iso, resolution_used)
            if ex:
                exits_this_step.append(ex)

    # Evaluate trailing stop-loss steps if the position is still open at the end of the bar
    trailing_mods: list[dict[str, Any]] = []
    if position["status"] == "OPEN":
        trailing_mods = evaluate_trailing_steps(
            position=position,
            strategy=strategy,
            condition_engine=condition_engine,
            candles_by_tf=candles_by_tf,
            eval_time=eval_dt_ist,
            eval_state=eval_state,
            manual_levels_override=manual_levels_override,
            primary_timeframe=primary_timeframe,
        )

    return {
        "position": position,
        "exitsThisStep": exits_this_step,
        "trailingModifications": trailing_mods,
        "intrabarAmbiguous": ambiguous,
        "intrabarResolutionUsed": resolution_used,
    }
