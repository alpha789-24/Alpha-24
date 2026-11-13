"""Phase 5 Backtest Simulator & Real-Time Paper Trading Worker Engine (Backend).

Uses the exact same shared `ConditionEngine` (`condition_engine.py`) and `ExitEngine` (`exit_engine.py`)
as paper and live trading — zero duplicate condition, sequence, re-entry, or exit logic:
1. Candle-by-Candle Backtest Simulator (`run_backtest`):
   - Strictly no look-ahead:
     - At bar `i` close, `ConditionEngine` and `ExitEngine` see ONLY closed candles `0 .. i`.
     - By default (`executionFillMode = "NEXT_CANDLE_OPEN"`), an entry signal formed on bar `i` close
       is queued as a pending order and filled at bar `i + 1`'s `OPEN` price (if bar `i + 1` is on the
       same trading day and within session hours). Configurable `"SAME_CANDLE_CLOSE"` is also supported.
   - Configurable friction & contract parameters:
     - `slippagePoints` and `slippagePercent`
     - `brokeragePerOrder` (applied per entry and exit fill) and `chargesPercent` (exchange/STT/GST percentage of turnover)
     - `lotSize` and `initialCapital`
     - `expiryHandling`: `"SESSION_END_SQUARE_OFF"` (default intraday square-off at session end),
       `"SQUARE_OFF_ON_EXPIRY"` (squares off on `expiryDate`), or `"HOLD_ACROSS_DAYS"` / `"ROLLOVER_NEXT_EXPIRY"`.
   - Enforces daily `risk.maxTradesPerDay` and `risk.maxDailyLoss` guards.
   - Computes comprehensive metrics:
     `totalTrades`, `winners`, `losers`, `breakeven`, `winRate`, `grossProfit`, `totalCharges`,
     `totalSlippageCost`, `netProfit`, `maxDrawdown`, `maxDrawdownPercent`, `avgProfit`, `avgLoss`,
     `avgTradeNetPnl`, `riskToReward`, `profitFactor`, `equityCurve`, and `trades` (each with
     `entryReason`, `exitReason`, `slHistory`, `partialExits`, `intrabarResolutionUsed`).
2. Paper Trading Worker State Machine (`new_paper_worker_run`, `step_paper_worker_run`):
   - Supports `RUNNING`, `PAUSED`, `STOPPED`, `STOPPED_RISK_LIMIT`, and `ERROR` states.
   - Enforces daily `maxTradesPerDay` and `maxDailyLoss` guards.
   - Uses the exact same single-bar step function as `run_backtest`.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence
from uuid import uuid4

from .condition_engine import (
    IST,
    ConditionEngine,
    EvaluationState,
    candle_close_time_ist,
    candle_open_time_ist,
    check_session_and_holiday,
    parse_ist_datetime,
)
from .exit_engine import (
    ExitEngineError,
    open_managed_position,
    step_open_position,
)
from .levels import LevelBook
from .strategy import validate_strategy
from .broker_execution import compute_strategy_version_hash
from .market_service import _resolve_lot_size


class BacktestError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _resolve_lot_size_with_warning(
    strategy: dict,
    lot_size_override: Optional[int] = None,
) -> tuple[int, list[str]]:
    exec_cfg = strategy.get("execution") or {}
    inst_cfg = exec_cfg.get("instrument") or {}
    symbol = inst_cfg.get("underlying") or (strategy.get("meta") or {}).get("symbol") or ""
    warnings: list[str] = []

    if lot_size_override is not None:
        raw = lot_size_override
    else:
        master_lot = _resolve_lot_size(symbol)
        strat_lot = inst_cfg.get("lotSize")
        if master_lot is not None:
            raw = master_lot
            if strat_lot is not None:
                try:
                    if int(strat_lot) != int(master_lot):
                        warnings.append(
                            f"Strategy lotSize ({strat_lot}) differs from contract master ({master_lot}) for {symbol}; using contract master value {master_lot}."
                        )
                except (TypeError, ValueError):
                    pass
        else:
            raw = strat_lot

    try:
        lot_int = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        lot_int = 0
    if lot_int < 1:
        raise BacktestError("Lot size is unknown (contract master missing or stale); cannot start without a valid lot size.")
    return lot_int, warnings


def _require_lot_size(strategy: dict, lot_size_override: Optional[int] = None) -> int:
    lot_int, _ = _resolve_lot_size_with_warning(strategy, lot_size_override=lot_size_override)
    return lot_int


def _apply_slippage(raw_price: float, side: str, is_entry: bool, slippage_points: float, slippage_pct: float) -> tuple[float, float]:
    """Return (slipped_price, adverse_slippage_points_per_unit).

    For BUY entry or SELL exit (buying back): price slips UP.
    For SELL entry or BUY exit (selling out): price slips DOWN.
    """
    pts = float(slippage_points or 0.0) + abs(float(raw_price)) * (float(slippage_pct or 0.0) / 100.0)
    if pts <= 0.0:
        return round(float(raw_price), 4), 0.0
    side_u = side.upper()
    buying = (side_u == "BUY" and is_entry) or (side_u == "SELL" and not is_entry)
    slipped = (raw_price + pts) if buying else max(0.01, raw_price - pts)
    return round(float(slipped), 4), round(float(pts), 4)


def _compute_order_charges(turnover: float, brokerage_per_order: float, charges_pct: float) -> float:
    return round(float(brokerage_per_order or 0.0) + abs(float(turnover)) * (float(charges_pct or 0.0) / 100.0), 2)


def _finalize_closed_trade_record(
    managed_pos: dict,
    trade_no: int,
    slippage_points: float,
    slippage_pct: float,
    brokerage_per_order: float,
    charges_pct: float,
) -> dict[str, Any]:
    """Build a finalized trade-by-trade record with slippage and brokerage/charges accounted for."""
    side = managed_pos["side"]
    lot_size = int(managed_pos.get("lotSize", 1))
    initial_qty = int(managed_pos.get("initialQuantity", 1))
    total_units = initial_qty * lot_size
    raw_entry = float(managed_pos.get("rawEntryPrice", managed_pos["entryPrice"]))
    eff_entry = float(managed_pos["entryPrice"])
    entry_slip_pts = float(managed_pos.get("entrySlippagePoints", 0.0))

    entry_turnover = eff_entry * total_units
    total_charges = _compute_order_charges(entry_turnover, brokerage_per_order, charges_pct)
    total_slippage_cost = round(entry_slip_pts * total_units, 2)

    gross_pnl = 0.0
    adjusted_partials = []
    weighted_exit_sum = 0.0
    exited_units_sum = 0

    for pe in managed_pos.get("partialExits") or []:
        pe_copy = copy.deepcopy(pe)
        raw_exit_p = float(pe_copy["exitPrice"])
        eff_exit_p, exit_slip_pts = _apply_slippage(raw_exit_p, side, is_entry=False, slippage_points=slippage_points, slippage_pct=slippage_pct)
        units = int(pe_copy.get("units") or (int(pe_copy["quantity"]) * lot_size))
        leg_gross = ((eff_exit_p - eff_entry) if side == "BUY" else (eff_entry - eff_exit_p)) * units
        leg_charges = _compute_order_charges(eff_exit_p * units, brokerage_per_order, charges_pct)
        leg_slip_cost = round(exit_slip_pts * units, 2)

        pe_copy["rawExitPrice"] = round(raw_exit_p, 4)
        pe_copy["exitPrice"] = round(eff_exit_p, 4)
        pe_copy["grossPnl"] = round(leg_gross, 2)
        pe_copy["charges"] = leg_charges
        pe_copy["slippageCost"] = leg_slip_cost
        pe_copy["pnl"] = round(leg_gross - leg_charges, 2)
        adjusted_partials.append(pe_copy)

        gross_pnl += leg_gross
        total_charges += leg_charges
        total_slippage_cost += leg_slip_cost
        weighted_exit_sum += eff_exit_p * units
        exited_units_sum += units

    avg_exit_price = round(weighted_exit_sum / exited_units_sum, 4) if exited_units_sum > 0 else eff_entry
    net_pnl = round(gross_pnl - total_charges, 2)
    gross_pnl = round(gross_pnl, 2)
    total_charges = round(total_charges, 2)

    initial_sl = float(managed_pos.get("initialStopLoss", eff_entry))
    initial_risk_pts = abs(eff_entry - initial_sl)
    initial_risk_rupees = round(initial_risk_pts * total_units, 2)
    r_multiple = round(net_pnl / initial_risk_rupees, 2) if initial_risk_rupees > 0 else 0.0

    resolutions = [
        pe.get("intrabarResolutionUsed")
        for pe in adjusted_partials
        if pe.get("intrabarResolutionUsed") and pe.get("intrabarResolutionUsed") != "NONE"
    ]
    primary_resolution = resolutions[-1] if resolutions else "NONE"

    return {
        "tradeNo": trade_no,
        "strategyId": managed_pos.get("strategyId"),
        "side": side,
        "isReentry": bool(managed_pos.get("isReentry", False)),
        "reentryNumber": int(managed_pos.get("reentryNumber", 0)),
        "entryRuleId": managed_pos.get("entryRuleId"),
        "entryReason": managed_pos.get("entryReason", ""),
        "signalAtIst": managed_pos.get("signalAtIst", managed_pos.get("entryAtIst")),
        "entryAtIst": managed_pos.get("entryAtIst"),
        "rawEntryPrice": round(raw_entry, 4),
        "entryPrice": round(eff_entry, 4),
        "exitAtIst": managed_pos.get("exitAtIst"),
        "exitPrice": avg_exit_price,
        "exitReason": managed_pos.get("exitReason"),
        "quantity": initial_qty,
        "lotSize": lot_size,
        "totalUnits": total_units,
        "initialStopLoss": initial_sl,
        "finalStopLoss": managed_pos.get("currentStopLoss"),
        "stopLossMethod": managed_pos.get("stopLossMethod"),
        "slHistory": copy.deepcopy(managed_pos.get("slHistory") or []),
        "partialExits": adjusted_partials,
        "intrabarResolutionUsed": primary_resolution,
        "intrabarResolutionLog": copy.deepcopy(managed_pos.get("intrabarResolutionLog") or []),
        "grossPnl": gross_pnl,
        "charges": total_charges,
        "slippageCost": round(total_slippage_cost, 2),
        "netPnl": net_pnl,
        "initialRiskRupees": initial_risk_rupees,
        "rMultiple": r_multiple,
    }


def _force_close_position(
    managed_pos: dict,
    close_price: float,
    close_time_iso: str,
    reason: str,
) -> None:
    """Force-close any remaining open quantity (e.g. session end, expiry, or daily risk guard)."""
    rem_qty = int(managed_pos.get("remainingQuantity", 0))
    if rem_qty <= 0 or managed_pos.get("status") != "OPEN":
        return
    lot_size = int(managed_pos.get("lotSize", 1))
    entry_price = float(managed_pos["entryPrice"])
    side_u = managed_pos["side"].upper()
    units = rem_qty * lot_size
    leg_pnl = round(((close_price - entry_price) if side_u == "BUY" else (entry_price - close_price)) * units, 2)

    managed_pos.setdefault("partialExits", []).append({
        "exitType": reason,
        "targetId": None,
        "label": reason,
        "quantity": rem_qty,
        "lotSize": lot_size,
        "units": units,
        "exitPrice": round(float(close_price), 4),
        "timeIst": close_time_iso,
        "pnl": leg_pnl,
        "intrabarResolutionUsed": "FORCED_EXIT",
    })
    managed_pos["realizedPnl"] = round(float(managed_pos.get("realizedPnl", 0.0)) + leg_pnl, 2)
    managed_pos["remainingQuantity"] = 0
    managed_pos["status"] = "CLOSED"
    managed_pos["exitReason"] = reason
    managed_pos["exitAtIst"] = close_time_iso
    managed_pos["exitPrice"] = round(float(close_price), 4)


def compute_backtest_metrics(
    trades: Sequence[dict],
    initial_capital: float,
    equity_curve: Sequence[dict],
) -> dict[str, Any]:
    """Compute summary performance statistics from finalized trades and equity curve."""
    total_trades = len(trades)
    winners = [t for t in trades if float(t["netPnl"]) > 0]
    losers = [t for t in trades if float(t["netPnl"]) < 0]
    breakeven = [t for t in trades if float(t["netPnl"]) == 0]

    win_rate = round((len(winners) / total_trades) * 100.0, 2) if total_trades > 0 else 0.0
    gross_profit = round(sum(float(t["grossPnl"]) for t in trades), 2)
    total_charges = round(sum(float(t["charges"]) for t in trades), 2)
    total_slippage = round(sum(float(t["slippageCost"]) for t in trades), 2)
    net_profit = round(sum(float(t["netPnl"]) for t in trades), 2)

    avg_win = round(sum(float(t["netPnl"]) for t in winners) / len(winners), 2) if winners else 0.0
    avg_loss = round(sum(float(t["netPnl"]) for t in losers) / len(losers), 2) if losers else 0.0
    avg_trade_pnl = round(net_profit / total_trades, 2) if total_trades > 0 else 0.0

    if avg_loss < 0 and avg_win > 0:
        risk_to_reward = round(avg_win / abs(avg_loss), 2)
    else:
        risk_to_reward = 0.0

    sum_wins = sum(float(t["netPnl"]) for t in winners)
    sum_losses = abs(sum(float(t["netPnl"]) for t in losers))
    profit_factor = round(sum_wins / sum_losses, 2) if sum_losses > 0 else (999.0 if sum_wins > 0 else 0.0)

    # Max drawdown across equity curve
    peak = float(initial_capital)
    max_dd = 0.0
    max_dd_pct = 0.0
    for pt in equity_curve:
        eq = float(pt["equity"])
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0

    return {
        "initialCapital": round(float(initial_capital), 2),
        "finalEquity": round(float(initial_capital) + net_profit, 2),
        "totalTrades": total_trades,
        "winners": len(winners),
        "losers": len(losers),
        "breakeven": len(breakeven),
        "winRate": win_rate,
        "grossProfit": gross_profit,
        "totalCharges": total_charges,
        "totalSlippageCost": total_slippage,
        "netProfit": net_profit,
        "maxDrawdown": round(max_dd, 2),
        "maxDrawdownPercent": round(max_dd_pct, 2),
        "avgProfit": avg_win,
        "avgLoss": avg_loss,
        "avgTradeNetPnl": avg_trade_pnl,
        "riskToReward": risk_to_reward,
        "profitFactor": profit_factor,
    }


def run_backtest(
    strategy: dict,
    candles_by_tf: dict[str, Sequence[dict]],
    level_book: Optional[LevelBook] = None,
    primary_timeframe: str = "5m",
    lower_tf_candles: Optional[Sequence[dict]] = None,
    manual_levels_by_day: Optional[dict[str, dict[str, float]]] = None,
    execution_fill_mode: str = "NEXT_CANDLE_OPEN",
    slippage_points: float = 0.0,
    slippage_percent: float = 0.0,
    brokerage_per_order: float = 0.0,
    charges_percent: float = 0.0,
    lot_size_override: Optional[int] = None,
    initial_capital_override: Optional[float] = None,
    expiry_handling: str = "SESSION_END_SQUARE_OFF",
    expiry_date: Optional[str] = None,
) -> dict[str, Any]:
    """Run a deterministic candle-by-candle backtest using the shared ConditionEngine and ExitEngine."""
    errors = validate_strategy(strategy)
    if errors:
        raise BacktestError("Strategy validation failed: " + "; ".join(errors))

    primary_candles = list(candles_by_tf.get(primary_timeframe) or [])
    if not primary_candles:
        raise BacktestError(f"No candles supplied for primaryTimeframe '{primary_timeframe}'.")

    fill_mode = str(execution_fill_mode or "NEXT_CANDLE_OPEN").strip().upper()
    if fill_mode not in ("NEXT_CANDLE_OPEN", "SAME_CANDLE_CLOSE"):
        raise BacktestError("executionFillMode must be NEXT_CANDLE_OPEN or SAME_CANDLE_CLOSE.")

    exp_mode = str(expiry_handling or "SESSION_END_SQUARE_OFF").strip().upper()
    if exp_mode not in ("SESSION_END_SQUARE_OFF", "SQUARE_OFF_ON_EXPIRY", "ROLLOVER_NEXT_EXPIRY", "HOLD_ACROSS_DAYS"):
        raise BacktestError(f"Unsupported expiryHandling '{expiry_handling}'.")

    risk_cfg = strategy.get("risk") or {}
    exec_cfg = strategy.get("execution") or {}
    inst_cfg = exec_cfg.get("instrument") or {}
    session_cfg = strategy.get("session") or {}

    max_trades_per_day = int(risk_cfg.get("maxTradesPerDay", 3))
    max_daily_loss = float(risk_cfg.get("maxDailyLoss", 2000.0))
    initial_capital = float(initial_capital_override or risk_cfg.get("maxCapital", 100000.0))
    lot_size, lot_warnings = _resolve_lot_size_with_warning(strategy, lot_size_override=lot_size_override)

    engine = ConditionEngine(level_book=level_book)
    eval_state = EvaluationState()

    open_position: Optional[dict[str, Any]] = None
    pending_signal: Optional[dict[str, Any]] = None
    completed_trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    daily_stats: dict[str, dict[str, Any]] = {}
    blocked_events: list[dict[str, Any]] = []

    cumulative_net_pnl = 0.0

    def _day_tracker(d_str: str) -> dict[str, Any]:
        if d_str not in daily_stats:
            daily_stats[d_str] = {"tradesOpened": 0, "realizedNetPnl": 0.0, "riskHalted": False}
        return daily_stats[d_str]

    def _close_and_record(pos: dict, exit_time_iso: str) -> dict[str, Any]:
        nonlocal cumulative_net_pnl
        trade_rec = _finalize_closed_trade_record(
            pos,
            trade_no=len(completed_trades) + 1,
            slippage_points=slippage_points,
            slippage_pct=slippage_percent,
            brokerage_per_order=brokerage_per_order,
            charges_pct=charges_percent,
        )
        completed_trades.append(trade_rec)
        cumulative_net_pnl = round(cumulative_net_pnl + float(trade_rec["netPnl"]), 2)
        d_str = parse_ist_datetime(exit_time_iso).strftime("%Y-%m-%d")
        dt_stat = _day_tracker(d_str)
        dt_stat["realizedNetPnl"] = round(dt_stat["realizedNetPnl"] + float(trade_rec["netPnl"]), 2)
        if dt_stat["realizedNetPnl"] <= -abs(max_daily_loss):
            dt_stat["riskHalted"] = True
        eval_state.record_trade_exit(
            exit_reason=str(pos.get("exitReason") or "MANUAL"),
            timestamp_ist=exit_time_iso,
            side=pos.get("side"),
        )
        return trade_rec

    for i, bar in enumerate(primary_candles):
        bar_open_dt = candle_open_time_ist(bar)
        bar_close_dt = candle_close_time_ist(bar, primary_timeframe)
        day_str = bar_open_dt.strftime("%Y-%m-%d")
        day_levels = (manual_levels_by_day or {}).get(day_str)
        d_stat = _day_tracker(day_str)

        # Check if previous day left an open position that must be closed on day change
        if open_position is not None:
            pos_day = parse_ist_datetime(open_position["entryAtIst"]).strftime("%Y-%m-%d")
            if pos_day != day_str and exp_mode == "SESSION_END_SQUARE_OFF":
                _force_close_position(open_position, float(bar["open"]), bar_open_dt.isoformat(), "SESSION_END_SQUARE_OFF")
                _close_and_record(open_position, bar_open_dt.isoformat())
                open_position = None

        # Discard stale pending signals from a prior trading day
        if pending_signal is not None:
            sig_day = parse_ist_datetime(pending_signal["evaluatedAtIst"]).strftime("%Y-%m-%d")
            if sig_day != day_str:
                pending_signal = None

        # Build no-look-ahead candle slice up to bar `i`
        # Note: at bar `i` open (when filling a pending signal from bar `i - 1`), only bars `0 .. i - 1` are closed!
        closed_before_bar = {
            tf: [c for c in tf_bars if candle_close_time_ist(c, tf) <= bar_open_dt]
            for tf, tf_bars in candles_by_tf.items()
        }

        # 1. Execute queued pending signal at bar `i` OPEN (`NEXT_CANDLE_OPEN`)
        if pending_signal is not None and open_position is None:
            sig = pending_signal
            pending_signal = None
            sess_chk = check_session_and_holiday(session_cfg, bar_open_dt)
            if not sess_chk["allowed"]:
                blocked_events.append({
                    "timeIst": bar_open_dt.isoformat(),
                    "reason": f"Pending signal dropped: {sess_chk['reason']}",
                })
            elif d_stat["riskHalted"] or d_stat["tradesOpened"] >= max_trades_per_day:
                blocked_events.append({
                    "timeIst": bar_open_dt.isoformat(),
                    "reason": "Daily risk limit or maxTradesPerDay reached before open fill.",
                })
            else:
                raw_open = float(bar["open"])
                eff_entry, slip_pts = _apply_slippage(
                    raw_open, sig["side"], is_entry=True, slippage_points=slippage_points, slippage_pct=slippage_percent
                )
                open_position = open_managed_position(
                    strategy=strategy,
                    side=sig["side"],
                    entry_price=eff_entry,
                    entry_time=bar_open_dt,
                    candles_by_tf=closed_before_bar,
                    level_book=level_book,
                    manual_levels_override=day_levels,
                    primary_timeframe=primary_timeframe,
                    is_reentry=bool(sig.get("isReentry", False)),
                    entry_rule_id=sig.get("ruleId"),
                    lot_size=lot_size,
                )
                open_position["rawEntryPrice"] = raw_open
                open_position["entrySlippagePoints"] = slip_pts
                open_position["entryReason"] = f"{sig.get('bucket', 'ENTRY')}: {sig.get('label', sig.get('ruleId'))}"
                open_position["signalAtIst"] = sig.get("evaluatedAtIst")
                open_position["reentryNumber"] = int(sig.get("reentryNumber", 0))
                d_stat["tradesOpened"] += 1
                eval_state.record_trade_entry(
                    side=sig["side"],
                    timestamp_ist=bar_open_dt,
                    is_reentry=bool(sig.get("isReentry", False)),
                    rule_id=sig.get("ruleId"),
                )

        # Now build closed candle slice at bar `i` CLOSE (`bar_close_dt`)
        closed_at_bar_close = {
            tf: [c for c in tf_bars if candle_close_time_ist(c, tf) <= bar_close_dt]
            for tf, tf_bars in candles_by_tf.items()
        }

        # 2. If a position is open during bar `i`, step it across bar `i` (SL, partial targets, intrabar ambiguity, trailing SL)
        if open_position is not None:
            step_open_position(
                position=open_position,
                candle=bar,
                strategy=strategy,
                condition_engine=engine,
                candles_by_tf=closed_at_bar_close,
                eval_time=bar_close_dt,
                eval_state=eval_state,
                lower_tf_candles=lower_tf_candles,
                manual_levels_override=day_levels,
                primary_timeframe=primary_timeframe,
            )
            if open_position["status"] == "CLOSED":
                _close_and_record(open_position, bar_close_dt.isoformat())
                open_position = None

        # 3. Evaluate strategy rules at bar `i` close
        eval_res = engine.evaluate_strategy(
            strategy=strategy,
            candles_by_tf=closed_at_bar_close,
            eval_time=bar_close_dt,
            state=eval_state,
            manual_levels_override=day_levels,
            primary_timeframe=primary_timeframe,
            auto_record_entry=False,
        )

        # Check explicit EXIT rule signals if position is still open
        if open_position is not None:
            exit_sigs = [s for s in eval_res.get("signals") or [] if s.get("bucket") == "EXIT"]
            if exit_sigs:
                _force_close_position(
                    open_position,
                    float(bar["close"]),
                    bar_close_dt.isoformat(),
                    f"RULE_EXIT:{exit_sigs[0]['ruleId']}",
                )
                _close_and_record(open_position, bar_close_dt.isoformat())
                open_position = None

        # Check session end / expiry handling at the end of the trading day
        is_last_bar_of_day = (
            i == len(primary_candles) - 1
            or candle_open_time_ist(primary_candles[i + 1]).strftime("%Y-%m-%d") != day_str
        )
        if open_position is not None and is_last_bar_of_day:
            if exp_mode == "SQUARE_OFF_ON_EXPIRY" and expiry_date == day_str:
                _force_close_position(open_position, float(bar["close"]), bar_close_dt.isoformat(), "EXPIRY_SQUARE_OFF")
                _close_and_record(open_position, bar_close_dt.isoformat())
                open_position = None
            elif exp_mode == "ROLLOVER_NEXT_EXPIRY" and expiry_date == day_str:
                open_position.setdefault("slHistory", []).append({
                    "timeIst": bar_close_dt.isoformat(),
                    "oldSl": open_position["currentStopLoss"],
                    "newSl": open_position["currentStopLoss"],
                    "ruleId": "ROLLOVER",
                    "label": "Rolled over to next expiry contract",
                    "triggerPrice": float(bar["close"]),
                    "moveKind": "rollover",
                    "applied": True,
                    "reason": f"Rolled over contract on expiry {day_str}",
                })
            elif exp_mode == "SESSION_END_SQUARE_OFF":
                _force_close_position(open_position, float(bar["close"]), bar_close_dt.isoformat(), "SESSION_END_SQUARE_OFF")
                _close_and_record(open_position, bar_close_dt.isoformat())
                open_position = None

        # 4. If flat (no open position), check for ENTRY / REENTRY signals formed at bar `i` close
        if open_position is None and pending_signal is None:
            entry_sigs = [s for s in eval_res.get("signals") or [] if s.get("bucket") in ("ENTRY", "REENTRY")]
            if entry_sigs:
                sig = entry_sigs[0]
                if d_stat["riskHalted"]:
                    blocked_events.append({
                        "timeIst": bar_close_dt.isoformat(),
                        "ruleId": sig.get("ruleId"),
                        "reason": f"Blocked by daily max loss guard (realized={d_stat['realizedNetPnl']} <= -{max_daily_loss})",
                    })
                elif d_stat["tradesOpened"] >= max_trades_per_day:
                    blocked_events.append({
                        "timeIst": bar_close_dt.isoformat(),
                        "ruleId": sig.get("ruleId"),
                        "reason": f"Blocked by maxTradesPerDay guard ({d_stat['tradesOpened']}/{max_trades_per_day})",
                    })
                elif fill_mode == "SAME_CANDLE_CLOSE" and not is_last_bar_of_day:
                    raw_close = float(bar["close"])
                    eff_entry, slip_pts = _apply_slippage(
                        raw_close, sig["side"], is_entry=True, slippage_points=slippage_points, slippage_pct=slippage_percent
                    )
                    open_position = open_managed_position(
                        strategy=strategy,
                        side=sig["side"],
                        entry_price=eff_entry,
                        entry_time=bar_close_dt,
                        candles_by_tf=closed_at_bar_close,
                        level_book=level_book,
                        manual_levels_override=day_levels,
                        primary_timeframe=primary_timeframe,
                        is_reentry=bool(sig.get("isReentry", False)),
                        entry_rule_id=sig.get("ruleId"),
                        lot_size=lot_size,
                    )
                    open_position["rawEntryPrice"] = raw_close
                    open_position["entrySlippagePoints"] = slip_pts
                    open_position["entryReason"] = f"{sig.get('bucket', 'ENTRY')}: {sig.get('label', sig.get('ruleId'))}"
                    open_position["signalAtIst"] = bar_close_dt.isoformat()
                    open_position["reentryNumber"] = int(sig.get("reentryNumber", 0))
                    d_stat["tradesOpened"] += 1
                    eval_state.record_trade_entry(
                        side=sig["side"],
                        timestamp_ist=bar_close_dt,
                        is_reentry=bool(sig.get("isReentry", False)),
                        rule_id=sig.get("ruleId"),
                    )
                elif fill_mode == "NEXT_CANDLE_OPEN" and not is_last_bar_of_day:
                    pending_signal = sig

        # Record equity curve point at bar close
        unrealized = 0.0
        if open_position is not None:
            rem_units = int(open_position["remainingQuantity"]) * lot_size
            c_price = float(bar["close"])
            e_price = float(open_position["entryPrice"])
            unrealized = ((c_price - e_price) if open_position["side"] == "BUY" else (e_price - c_price)) * rem_units
            # Include any already-realized partial exits on the currently open position
            partial_realized = sum(float(pe.get("pnl", 0.0)) for pe in open_position.get("partialExits") or [])
            unrealized += partial_realized

        equity_curve.append({
            "timestampIst": bar_close_dt.isoformat(),
            "day": day_str,
            "close": float(bar["close"]),
            "realizedNetPnl": cumulative_net_pnl,
            "unrealizedPnl": round(unrealized, 2),
            "equity": round(initial_capital + cumulative_net_pnl + unrealized, 2),
        })

    # If a position is still open at the very end of the dataset, close it at the final bar's close
    if open_position is not None:
        last_bar = primary_candles[-1]
        last_close_dt = candle_close_time_ist(last_bar, primary_timeframe)
        _force_close_position(open_position, float(last_bar["close"]), last_close_dt.isoformat(), "END_OF_BACKTEST")
        _close_and_record(open_position, last_close_dt.isoformat())
        open_position = None
        if equity_curve:
            equity_curve[-1]["realizedNetPnl"] = cumulative_net_pnl
            equity_curve[-1]["unrealizedPnl"] = 0.0
            equity_curve[-1]["equity"] = round(initial_capital + cumulative_net_pnl, 2)

    metrics = compute_backtest_metrics(completed_trades, initial_capital, equity_curve)
    return {
        "id": str(uuid4()),
        "strategyId": strategy.get("id"),
        "strategyName": strategy.get("name"),
        "versionHash": compute_strategy_version_hash(strategy),
        "primaryTimeframe": primary_timeframe,
        "executionFillMode": fill_mode,
        "intrabarConflictOrder": str(exec_cfg.get("intrabarConflictOrder") or "SL_FIRST").upper(),
        "expiryHandling": exp_mode,
        "slippagePoints": slippage_points,
        "slippagePercent": slippage_percent,
        "brokeragePerOrder": brokerage_per_order,
        "chargesPercent": charges_percent,
        "lotSize": lot_size,
        "warnings": lot_warnings,
        "metrics": metrics,
        "equityCurve": equity_curve,
        "trades": completed_trades,
        "dailyStats": daily_stats,
        "blockedEvents": blocked_events,
        "createdAt": datetime.now(IST).isoformat(),
    }


# ==================== PAPER TRADING WORKER STATE MACHINE ====================

PAPER_FINAL_STATUSES = {"STOPPED", "STOPPED_RISK_LIMIT", "ERROR"}


def new_paper_worker_run(
    strategy: dict,
    primary_timeframe: str = "5m",
    slippage_points: float = 0.0,
    brokerage_per_order: float = 0.0,
    charges_percent: float = 0.0,
) -> dict[str, Any]:
    """Create a new background paper trading worker state record for a `RULE_BUILDER` strategy."""
    errors = validate_strategy(strategy)
    if errors:
        raise BacktestError("Cannot start paper worker on invalid strategy: " + "; ".join(errors))

    now_iso = datetime.now(IST).isoformat()
    risk_cfg = strategy.get("risk") or {}
    exec_cfg = strategy.get("execution") or {}
    inst_cfg = exec_cfg.get("instrument") or {}
    symbol = inst_cfg.get("underlying") or (strategy.get("meta") or {}).get("symbol") or "NIFTY 50"
    lot_size, lot_warnings = _resolve_lot_size_with_warning(strategy)

    return {
        "id": str(uuid4()),
        "strategyId": strategy.get("id"),
        "strategyName": strategy.get("name"),
        "versionHash": compute_strategy_version_hash(strategy),
        "symbol": symbol,
        "primaryTimeframe": primary_timeframe,
        "status": "RUNNING",  # RUNNING | PAUSED | STOPPED | STOPPED_RISK_LIMIT | ERROR
        "strategy": copy.deepcopy(strategy),
        "evalState": EvaluationState().to_dict(),
        "openPosition": None,
        "closedTrades": [],
        "currentDay": None,
        "tradesToday": 0,
        "realizedPnlToday": 0.0,
        "totalRealizedPnl": 0.0,
        "unrealizedPnl": 0.0,
        "maxTradesPerDay": int(risk_cfg.get("maxTradesPerDay", 3)),
        "maxDailyLoss": float(risk_cfg.get("maxDailyLoss", 2000.0)),
        "slippagePoints": float(slippage_points),
        "brokeragePerOrder": float(brokerage_per_order),
        "chargesPercent": float(charges_percent),
        "lastProcessedCandleKey": None,
        "lastEvaluatedAtIst": None,
        "lotSize": lot_size,
        "warnings": lot_warnings,
        "message": (
            f"Paper worker started ({lot_warnings[0]})" if lot_warnings
            else "Paper worker started and monitoring market candles"
        ),
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }


def step_paper_worker_run(
    worker_run: dict,
    candles_by_tf: dict[str, Sequence[dict]],
    eval_time: Any,
    level_book: Optional[LevelBook] = None,
    manual_levels_override: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """Advance a running paper-trading worker by one evaluation cycle using the shared engine."""
    status = worker_run.get("status")
    if status != "RUNNING":
        return {"status": status, "events": [], "message": f"Worker is {status}"}

    eval_dt_ist = parse_ist_datetime(eval_time)
    eval_iso = eval_dt_ist.isoformat()
    day_str = eval_dt_ist.strftime("%Y-%m-%d")
    worker_run["updatedAt"] = eval_iso
    worker_run["lastEvaluatedAtIst"] = eval_iso

    # Reset daily counters on IST day change
    if worker_run.get("currentDay") != day_str:
        worker_run["currentDay"] = day_str
        worker_run["tradesToday"] = 0
        worker_run["realizedPnlToday"] = 0.0

    strategy = worker_run["strategy"]
    primary_tf = worker_run.get("primaryTimeframe", "5m")
    primary_bars = [
        c for c in (candles_by_tf.get(primary_tf) or [])
        if candle_close_time_ist(c, primary_tf) <= eval_dt_ist
    ]
    if not primary_bars:
        worker_run["message"] = f"Waiting for closed {primary_tf} candles"
        return {"status": worker_run["status"], "events": [], "message": worker_run["message"]}

    latest_bar = primary_bars[-1]
    engine = ConditionEngine(level_book=level_book)
    eval_state = EvaluationState(worker_run.get("evalState"))
    events: list[dict[str, Any]] = []

    exec_cfg = strategy.get("execution") or {}
    inst_cfg = exec_cfg.get("instrument") or {}
    lot_size = _require_lot_size(strategy)
    max_trades = int(worker_run.get("maxTradesPerDay", 3))
    max_loss = float(worker_run.get("maxDailyLoss", 2000.0))

    # 1. Step existing open position first
    open_pos = worker_run.get("openPosition")
    if open_pos is not None:
        step_res = step_open_position(
            position=open_pos,
            candle=latest_bar,
            strategy=strategy,
            condition_engine=engine,
            candles_by_tf=candles_by_tf,
            eval_time=eval_dt_ist,
            eval_state=eval_state,
            manual_levels_override=manual_levels_override,
            primary_timeframe=primary_tf,
        )
        for ex in step_res.get("exitsThisStep") or []:
            events.append({"type": "PARTIAL_OR_FULL_EXIT", "exit": ex})

        if open_pos["status"] == "CLOSED":
            trade_rec = _finalize_closed_trade_record(
                open_pos,
                trade_no=len(worker_run.get("closedTrades") or []) + 1,
                slippage_points=float(worker_run.get("slippagePoints", 0.0)),
                slippage_pct=0.0,
                brokerage_per_order=float(worker_run.get("brokeragePerOrder", 0.0)),
                charges_pct=float(worker_run.get("chargesPercent", 0.0)),
            )
            worker_run.setdefault("closedTrades", []).append(trade_rec)
            worker_run["realizedPnlToday"] = round(float(worker_run.get("realizedPnlToday", 0.0)) + float(trade_rec["netPnl"]), 2)
            worker_run["totalRealizedPnl"] = round(float(worker_run.get("totalRealizedPnl", 0.0)) + float(trade_rec["netPnl"]), 2)
            worker_run["openPosition"] = None
            worker_run["unrealizedPnl"] = 0.0
            eval_state.record_trade_exit(
                exit_reason=str(open_pos.get("exitReason") or "MANUAL"),
                timestamp_ist=eval_dt_ist,
                side=open_pos.get("side"),
            )
            events.append({"type": "POSITION_CLOSED", "trade": trade_rec})

            # Check daily max-loss guard after trade close
            if worker_run["realizedPnlToday"] <= -abs(max_loss):
                worker_run["status"] = "STOPPED_RISK_LIMIT"
                worker_run["message"] = (
                    f"Stopped by daily max-loss guard: realized P&L today ₹{worker_run['realizedPnlToday']:,.2f} "
                    f"exceeded limit -₹{max_loss:,.2f}"
                )
                worker_run["evalState"] = eval_state.to_dict()
                events.append({"type": "RISK_GUARD_HALT", "reason": worker_run["message"]})
                return {"status": worker_run["status"], "events": events, "message": worker_run["message"]}
        else:
            # Update live unrealized P&L and check intrabar max daily loss guard
            rem_units = int(open_pos["remainingQuantity"]) * lot_size
            c_price = float(latest_bar["close"])
            e_price = float(open_pos["entryPrice"])
            unrealized = ((c_price - e_price) if open_pos["side"] == "BUY" else (e_price - c_price)) * rem_units
            worker_run["unrealizedPnl"] = round(unrealized, 2)
            if (worker_run["realizedPnlToday"] + unrealized) <= -abs(max_loss):
                _force_close_position(open_pos, c_price, eval_iso, "MAX_DAILY_LOSS_GUARD")
                trade_rec = _finalize_closed_trade_record(
                    open_pos,
                    trade_no=len(worker_run.get("closedTrades") or []) + 1,
                    slippage_points=float(worker_run.get("slippagePoints", 0.0)),
                    slippage_pct=0.0,
                    brokerage_per_order=float(worker_run.get("brokeragePerOrder", 0.0)),
                    charges_pct=float(worker_run.get("chargesPercent", 0.0)),
                )
                worker_run.setdefault("closedTrades", []).append(trade_rec)
                worker_run["realizedPnlToday"] = round(float(worker_run.get("realizedPnlToday", 0.0)) + float(trade_rec["netPnl"]), 2)
                worker_run["totalRealizedPnl"] = round(float(worker_run.get("totalRealizedPnl", 0.0)) + float(trade_rec["netPnl"]), 2)
                worker_run["openPosition"] = None
                worker_run["unrealizedPnl"] = 0.0
                worker_run["status"] = "STOPPED_RISK_LIMIT"
                worker_run["message"] = (
                    f"Position force-closed and worker stopped by daily max-loss guard (-₹{max_loss:,.2f})"
                )
                worker_run["evalState"] = eval_state.to_dict()
                events.append({"type": "RISK_GUARD_HALT", "trade": trade_rec, "reason": worker_run["message"]})
                return {"status": worker_run["status"], "events": events, "message": worker_run["message"]}

    # 2. Evaluate conditions at bar close
    eval_res = engine.evaluate_strategy(
        strategy=strategy,
        candles_by_tf=candles_by_tf,
        eval_time=eval_dt_ist,
        state=eval_state,
        manual_levels_override=manual_levels_override,
        primary_timeframe=primary_tf,
        auto_record_entry=False,
    )

    # 3. If flat, check entry / re-entry signals and daily guards
    if worker_run.get("openPosition") is None:
        entry_sigs = [s for s in eval_res.get("signals") or [] if s.get("bucket") in ("ENTRY", "REENTRY")]
        if entry_sigs:
            sig = entry_sigs[0]
            if int(worker_run.get("tradesToday", 0)) >= max_trades:
                worker_run["message"] = f"Entry signal blocked by daily maxTradesPerDay guard ({worker_run['tradesToday']}/{max_trades})"
                events.append({"type": "MAX_TRADES_GUARD_BLOCKED", "signal": sig, "reason": worker_run["message"]})
            elif float(worker_run.get("realizedPnlToday", 0.0)) <= -abs(max_loss):
                worker_run["status"] = "STOPPED_RISK_LIMIT"
                worker_run["message"] = f"Entry signal blocked: daily max-loss limit (-₹{max_loss:,.2f}) reached"
                events.append({"type": "RISK_GUARD_HALT", "signal": sig, "reason": worker_run["message"]})
            else:
                raw_price = float(latest_bar["close"])
                eff_entry, slip_pts = _apply_slippage(
                    raw_price, sig["side"], is_entry=True, slippage_points=float(worker_run.get("slippagePoints", 0.0)), slippage_pct=0.0
                )
                new_pos = open_managed_position(
                    strategy=strategy,
                    side=sig["side"],
                    entry_price=eff_entry,
                    entry_time=eval_dt_ist,
                    candles_by_tf=candles_by_tf,
                    level_book=level_book,
                    manual_levels_override=manual_levels_override,
                    primary_timeframe=primary_tf,
                    is_reentry=bool(sig.get("isReentry", False)),
                    entry_rule_id=sig.get("ruleId"),
                    lot_size=lot_size,
                )
                new_pos["rawEntryPrice"] = raw_price
                new_pos["entrySlippagePoints"] = slip_pts
                new_pos["entryReason"] = f"{sig.get('bucket', 'ENTRY')}: {sig.get('label', sig.get('ruleId'))}"
                new_pos["signalAtIst"] = eval_iso
                new_pos["reentryNumber"] = int(sig.get("reentryNumber", 0))

                worker_run["openPosition"] = new_pos
                worker_run["tradesToday"] = int(worker_run.get("tradesToday", 0)) + 1
                worker_run["message"] = f"Opened {sig['side']} position @ ₹{eff_entry:,.2f} ({new_pos['entryReason']})"
                eval_state.record_trade_entry(
                    side=sig["side"],
                    timestamp_ist=eval_dt_ist,
                    is_reentry=bool(sig.get("isReentry", False)),
                    rule_id=sig.get("ruleId"),
                )
                events.append({"type": "POSITION_OPENED", "position": copy.deepcopy(new_pos)})

    worker_run["evalState"] = eval_state.to_dict()
    return {
        "status": worker_run["status"],
        "events": events,
        "message": worker_run["message"],
        "tradesToday": worker_run["tradesToday"],
        "realizedPnlToday": worker_run["realizedPnlToday"],
        "unrealizedPnl": worker_run["unrealizedPnl"],
    }
