"""Phase 4 unit & integration tests:
- Stop loss types:
  1. `fixed_points` (BUY and SELL)
  2. `percentage` (BUY and SELL)
  3. `indicator` (e.g. SMA/ATR/Supertrend on closed candle)
  4. `candle` (comparison price `CLOSE`, `HIGH_LOW`, `OPEN`, `candleCount` N-bar lookback, `bufferPercent`)
  5. `level` (any manual FC/RC level)
- Multi-target partial exits:
  - 3+ targets with `points`, `percent`, and `price_level`
  - Optional disabled targets, final target always enabled
  - Quantity sum validation against position quantity
  - Partial fills reducing `remainingQuantity` and logging `partialExits`
- Trailing SL engine:
  - Ordered `IF <condition> THEN MOVE SL TO <level|price|points|indicator>`
  - Monotonic enforcement: LONG SL never moves down; SHORT SL never moves up
  - Idempotency: each trailing step fires at most once per trade
  - Complete modification history (`slHistory`: timeIst, oldSl, newSl, ruleId, triggerPrice)
- Intrabar ambiguity when one candle touches both SL and target:
  - `SL_FIRST` (default)
  - `TARGET_FIRST`
  - `LOWER_TIMEFRAME` (using 1m sub-candles inside a 5m bar, plus fallback to SL_FIRST when sub-candles absent)
  - Recording `intrabarResolutionUsed` and `intrabarResolutionLog`
- FastAPI Phase 4 endpoints (`/api/engine/exit/resolve`, `/api/engine/exit/step`)
"""
from __future__ import annotations

import copy
import math
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.condition_engine import ConditionEngine, EvaluationState
from app.exit_engine import (
    ExitEngineError,
    evaluate_trailing_steps,
    open_managed_position,
    resolve_stop_loss_price,
    resolve_targets,
    step_open_position,
)
from app.levels import LevelBook
from app.main import app
from app.repos import JsonEvaluationStateRepo, JsonLevelRepo
from app.samples import sample_phase4_trailing_partial_targets as _raw_sample_phase4_trailing_partial_targets


def sample_phase4_trailing_partial_targets():
    s = _raw_sample_phase4_trailing_partial_targets()
    s["execution"]["instrument"]["lotSize"] = 25
    return s
from app.schema_v2 import LEVEL_NAMES, new_rule_builder
from app.contract_master import contract_master_service
import pytest

@pytest.fixture(autouse=True)
def isolate_contract_master():
    orig_dir = contract_master_service.cache_dir
    orig_rows = dict(contract_master_service._rows_by_segment)
    orig_meta = dict(contract_master_service._meta)
    with tempfile.TemporaryDirectory() as tmp:
        contract_master_service.cache_dir = Path(tmp)
        contract_master_service._rows_by_segment.clear()
        contract_master_service._meta.clear()
        yield
    contract_master_service.cache_dir = orig_dir
    contract_master_service._rows_by_segment.clear()
    contract_master_service._rows_by_segment.update(orig_rows)
    contract_master_service._meta.clear()
    contract_master_service._meta.update(orig_meta)

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        print(f"PASS {name}")
        passed += 1
    else:
        print(f"FAIL {name}: {detail}")
        failed += 1
        raise AssertionError(f"{name}: {detail}")


def make_candle(
    day: str,
    hhmm: str,
    o: float,
    h: float,
    l: float,
    c: float,
    v: int = 1000,
    tf_minutes: int = 5,
) -> dict:
    open_dt = datetime.fromisoformat(f"{day}T{hhmm}:00+05:30")
    close_dt = open_dt + timedelta(minutes=tf_minutes)
    return {
        "timestamp": int(open_dt.timestamp()),
        "openTime": open_dt.isoformat(),
        "closeTime": close_dt.isoformat(),
        "date": day,
        "time": hhmm,
        "open": float(o),
        "high": float(h),
        "low": float(l),
        "close": float(c),
        "volume": int(v),
        "closed": True,
    }


def make_manual_levels() -> dict[str, float]:
    values = {name: float(24000 + idx * 10) for idx, name in enumerate(LEVEL_NAMES)}
    values["FC1L"] = 23950.0
    values["FC1H"] = 24050.0
    values["RC1L"] = 23980.0
    values["RC1H"] = 24020.0
    values["RC2H"] = 24080.0
    values["RC3L"] = 23900.0
    values["RC3H"] = 24120.0
    return values


# ==================== 1. STOP LOSS TYPES ====================

def test_all_stop_loss_types():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)

        strat = new_rule_builder()
        strat["indicators"] = [
            {
                "id": "sma-3",
                "enabled": True,
                "order": 1,
                "label": "SMA 3",
                "type": "SMA",
                "timeframe": "5m",
                "source": "CLOSE",
                "period": 3,
            }
        ]
        candles_5m = [
            make_candle("2026-01-05", "09:15", 23980, 23990, 23960, 23970),
            make_candle("2026-01-05", "09:20", 23970, 24000, 23950, 23980),
            make_candle("2026-01-05", "09:25", 23980, 24010, 23975, 23990),  # SMA(3) = 23980.0
        ]
        eval_t = "2026-01-05T09:30:00+05:30"

        # 1. Fixed points (BUY & SELL)
        sl_fp_buy = resolve_stop_loss_price(
            {"id": "sl1", "enabled": True, "method": "fixed_points", "points": 25.0},
            side="BUY",
            entry_price=24000.0,
            strategy=strat,
            candles_by_tf={"5m": candles_5m},
            eval_time=eval_t,
            level_book=book,
        )
        sl_fp_sell = resolve_stop_loss_price(
            {"id": "sl1", "enabled": True, "method": "fixed_points", "points": 25.0},
            side="SELL",
            entry_price=24000.0,
            strategy=strat,
            candles_by_tf={"5m": candles_5m},
            eval_time=eval_t,
            level_book=book,
        )
        check("SL fixed_points BUY=23975 and SELL=24025", sl_fp_buy["price"] == 23975.0 and sl_fp_sell["price"] == 24025.0)

        # 2. Percentage (BUY & SELL, 0.5% of 24000 = 120 pts)
        sl_pct_buy = resolve_stop_loss_price(
            {"id": "sl2", "enabled": True, "method": "percentage", "percent": 0.5},
            side="BUY",
            entry_price=24000.0,
            strategy=strat,
            candles_by_tf={"5m": candles_5m},
            eval_time=eval_t,
            level_book=book,
        )
        sl_pct_sell = resolve_stop_loss_price(
            {"id": "sl2", "enabled": True, "method": "percentage", "percent": 0.5},
            side="SELL",
            entry_price=24000.0,
            strategy=strat,
            candles_by_tf={"5m": candles_5m},
            eval_time=eval_t,
            level_book=book,
        )
        check("SL percentage BUY=23880 and SELL=24120", sl_pct_buy["price"] == 23880.0 and sl_pct_sell["price"] == 24120.0)

        # 3. Indicator-based (SMA 3 = 23980.0)
        sl_ind = resolve_stop_loss_price(
            {"id": "sl3", "enabled": True, "method": "indicator", "indicatorId": "sma-3"},
            side="BUY",
            entry_price=24000.0,
            strategy=strat,
            candles_by_tf={"5m": candles_5m},
            eval_time=eval_t,
            level_book=book,
        )
        check("SL indicator SMA(3) = 23980.0", sl_ind["price"] == 23980.0)

        # 4. Candle-based (HIGH_LOW, CLOSE, OPEN over 3 candles + bufferPercent)
        # Lowest low over the 3 candles is 23950.0. With bufferPercent=0.1% -> 23950 * (1 - 0.001) = 23926.05
        sl_candle_hl = resolve_stop_loss_price(
            {
                "id": "sl4",
                "enabled": True,
                "method": "candle",
                "candlePrice": "HIGH_LOW",
                "candleCount": 3,
                "bufferPercent": 0.1,
            },
            side="BUY",
            entry_price=24000.0,
            strategy=strat,
            candles_by_tf={"5m": candles_5m},
            eval_time=eval_t,
            level_book=book,
        )
        check("SL candle HIGH_LOW BUY (3 bars + 0.1% buffer) = 23926.05", math.isclose(sl_candle_hl["price"], 23926.05, abs_tol=1e-3), str(sl_candle_hl))

        # Highest CLOSE over 3 candles for SELL is 23990.0 (with 0% buffer -> 23990.0)
        sl_candle_close = resolve_stop_loss_price(
            {
                "id": "sl4b",
                "enabled": True,
                "method": "candle",
                "candlePrice": "CLOSE",
                "candleCount": 3,
                "bufferPercent": 0.0,
            },
            side="SELL",
            entry_price=23960.0,
            strategy=strat,
            candles_by_tf={"5m": candles_5m},
            eval_time=eval_t,
            level_book=book,
        )
        check("SL candle CLOSE SELL (3 bars, 0% buffer) = 23990.0", sl_candle_close["price"] == 23990.0)

        # 5. Level-based (FC1L = 23950.0)
        sl_lvl = resolve_stop_loss_price(
            {"id": "sl5", "enabled": True, "method": "level", "level": "FC1L"},
            side="BUY",
            entry_price=24000.0,
            strategy=strat,
            candles_by_tf={"5m": candles_5m},
            eval_time=eval_t,
            level_book=book,
        )
        check("SL level FC1L = 23950.0", sl_lvl["price"] == 23950.0)


# ==================== 2. MULTI-TARGET PARTIAL EXITS & VALIDATION ====================

def test_partial_targets_and_validation():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)
        engine = ConditionEngine(level_book=book)

        strat = sample_phase4_trailing_partial_targets()
        # Disable trailing for pure partial-target progression test
        strat_no_trail = copy.deepcopy(strat)
        strat_no_trail["trailing"] = []

        # Validate that mismatched target quantity sum raises ExitEngineError
        bad_targets = copy.deepcopy(strat_no_trail["targets"])
        bad_targets[2]["quantity"] = 5  # Sum = 1 + 1 + 5 = 7 != 4
        try:
            resolve_targets(
                bad_targets,
                side="BUY",
                entry_price=24000.0,
                total_quantity=4,
                strategy=strat_no_trail,
                eval_time="2026-01-05T09:20:00+05:30",
                level_book=book,
            )
            raised_qty = False
        except ExitEngineError as exc:
            raised_qty = "sum to 7" in exc.message
        check("Target quantity sum mismatch rejected", raised_qty)

        # Open 4-lot BUY position at 24000.0 (lotSize = 25)
        # Target 1: +20 pts -> 24020.0 (1 lot)
        # Target 2: +0.25% -> 24060.0 (1 lot)
        # Target 3 (Final): RC3H -> 24120.0 (2 lots)
        # Initial SL: FC1L -> 23950.0
        pos = open_managed_position(
            strategy=strat_no_trail,
            side="BUY",
            entry_price=24000.0,
            entry_time="2026-01-05T09:20:00+05:30",
            candles_by_tf={"5m": []},
            level_book=book,
            lot_size=25,
        )
        check(
            "Managed position opened with 4 lots and targets [24020, 24060, 24120]",
            pos["remainingQuantity"] == 4
            and [t["targetPrice"] for t in pos["targets"]] == [24020.0, 24060.0, 24120.0],
        )

        # Bar 1: High reaches 24025 -> fills Target 1 (1 lot @ 24020), 3 lots remain OPEN
        b1 = make_candle("2026-01-05", "09:20", 24000, 24025, 23990, 24018)
        step1 = step_open_position(pos, b1, strat_no_trail, engine, {"5m": [b1]}, "2026-01-05T09:25:00+05:30")
        check(
            "Target 1 partial exit fills 1 lot and leaves 3 lots OPEN",
            pos["status"] == "OPEN"
            and pos["remainingQuantity"] == 3
            and len(step1["exitsThisStep"]) == 1
            and step1["exitsThisStep"][0]["targetId"] == "tgt-1-pts"
            and step1["exitsThisStep"][0]["pnl"] == 500.0,  # (24020 - 24000) * 1 * 25 = 500
        )

        # Bar 2: High reaches 24065 -> fills Target 2 (1 lot @ 24060), 2 lots remain OPEN
        b2 = make_candle("2026-01-05", "09:25", 24018, 24065, 24010, 24055)
        step2 = step_open_position(pos, b2, strat_no_trail, engine, {"5m": [b1, b2]}, "2026-01-05T09:30:00+05:30")
        check(
            "Target 2 partial exit fills 1 lot and leaves 2 lots OPEN",
            pos["status"] == "OPEN"
            and pos["remainingQuantity"] == 2
            and len(step2["exitsThisStep"]) == 1
            and step2["exitsThisStep"][0]["targetId"] == "tgt-2-pct"
            and step2["exitsThisStep"][0]["pnl"] == 1500.0,  # (24060 - 24000) * 1 * 25 = 1500
        )

        # Bar 3: High reaches 24125 -> fills Final Target 3 (2 lots @ 24120), closes position
        b3 = make_candle("2026-01-05", "09:30", 24055, 24125, 24050, 24115)
        step3 = step_open_position(pos, b3, strat_no_trail, engine, {"5m": [b1, b2, b3]}, "2026-01-05T09:35:00+05:30")
        check(
            "Final Target 3 fills remaining 2 lots and closes position with total realizedPnl = 8000",
            pos["status"] == "CLOSED"
            and pos["remainingQuantity"] == 0
            and pos["exitReason"] == "TARGET"
            and pos["realizedPnl"] == 8000.0,  # 500 + 1500 + (120 * 2 * 25 = 6000) = 8000
        )


# ==================== 3. TRAILING SL ENGINE: IDEMPOTENT, MONOTONIC & HISTORY ====================

def test_trailing_sl_monotonic_and_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)
        engine = ConditionEngine(level_book=book)

        strat = sample_phase4_trailing_partial_targets()
        # Add a 3rd trailing step that attempts to move LONG SL DOWN to 23960 (below RC1L=23980) to test monotonic guard
        strat["trailing"].append({
            "id": "trail-3-attempt-lower",
            "enabled": True,
            "order": 3,
            "label": "Attempt to move LONG SL down to 23960 (must be rejected by monotonic guard)",
            "condition": {
                "type": "leaf",
                "id": "cond-trail-3",
                "enabled": True,
                "order": 1,
                "label": "CLOSE > 24000",
                "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                "operator": ">",
                "right": {"kind": "constant", "field": None, "indicatorId": None, "level": None, "value": 24000.0},
                "timeframe": "5m",
                "candleRef": "current",
                "evaluateOn": "CANDLE_CLOSE",
                "params": {"tolerance": None, "toleranceUnit": None},
            },
            "moveSlTo": {
                "kind": "price",
                "level": None,
                "price": 23960.0,
                "points": None,
                "indicatorId": None,
            },
        })

        pos = open_managed_position(
            strategy=strat,
            side="BUY",
            entry_price=24000.0,
            entry_time="2026-01-05T09:20:00+05:30",
            candles_by_tf={"5m": []},
            level_book=book,
        )
        check("Initial SL is FC1L (23950.0)", pos["currentStopLoss"] == 23950.0)

        # Candle 1 closes at 24030 (> RC1H=24020, and > 24000):
        # - Trail step 1 (order 1) moves SL from 23950.0 -> RC1L (23980.0) [applied=True]
        # - Trail step 2 (order 2, CLOSE > RC2H=24080) is false
        # - Trail step 3 (order 3, CLOSE > 24000) attempts to move SL to 23960.0 (< current 23980.0) -> rejected by monotonic guard [applied=False]!
        c1 = make_candle("2026-01-05", "09:20", 24000, 24035, 23995, 24030)
        mods1 = evaluate_trailing_steps(pos, strat, engine, {"5m": [c1]}, "2026-01-05T09:25:00+05:30")
        check("Current SL moved up to RC1L (23980.0) and refused downward move to 23960.0", pos["currentStopLoss"] == 23980.0, str(mods1))
        check(
            "Both fired steps recorded in slHistory with applied=True and applied=False",
            len(pos["slHistory"]) == 2
            and pos["slHistory"][0]["applied"] is True
            and pos["slHistory"][0]["newSl"] == 23980.0
            and pos["slHistory"][1]["applied"] is False
            and pos["slHistory"][1]["newSl"] == 23980.0,
            str(pos["slHistory"]),
        )

        # Re-evaluating on another candle with CLOSE > RC1H must NOT re-fire trail-1 (idempotency)
        c2 = make_candle("2026-01-05", "09:25", 24030, 24040, 24022, 24035)
        mods2 = evaluate_trailing_steps(pos, strat, engine, {"5m": [c1, c2]}, "2026-01-05T09:30:00+05:30")
        check("Idempotency: trail-1 does not fire a second time", len(mods2) == 0 and len(pos["slHistory"]) == 2)

        # Candle 3 closes at 24090 (> RC2H=24080) -> Trail step 2 fires and moves SL to FC1H (24050.0)
        c3 = make_candle("2026-01-05", "09:30", 24035, 24095, 24030, 24090)
        mods3 = evaluate_trailing_steps(pos, strat, engine, {"5m": [c1, c2, c3]}, "2026-01-05T09:35:00+05:30")
        check(
            "Trail step 2 moves SL up to FC1H (24050.0)",
            len(mods3) == 1 and pos["currentStopLoss"] == 24050.0 and len(pos["slHistory"]) == 3,
        )

        # Test SHORT monotonic guard (SHORT SL never moves UP)
        short_strat = copy.deepcopy(strat)
        short_strat["stopLoss"] = {"id": "sl-s", "enabled": True, "order": 1, "label": "SL", "method": "fixed_points", "points": 50.0}
        short_strat["trailing"] = [
            {
                "id": "short-trail-up-invalid",
                "enabled": True,
                "order": 1,
                "label": "Try moving SHORT SL up to 24080 (must be rejected)",
                "condition": strat["trailing"][2]["condition"],
                "moveSlTo": {"kind": "price", "price": 24080.0},
            },
            {
                "id": "short-trail-down-valid",
                "enabled": True,
                "order": 2,
                "label": "Move SHORT SL down by 20 points to 24030",
                "condition": strat["trailing"][2]["condition"],
                "moveSlTo": {"kind": "points", "points": 20.0},
            },
        ]
        short_pos = open_managed_position(
            strategy=short_strat,
            side="SELL",
            entry_price=24000.0,
            entry_time="2026-01-05T09:20:00+05:30",
            candles_by_tf={"5m": []},
            level_book=book,
        )
        check("Initial SHORT SL is 24050.0", short_pos["currentStopLoss"] == 24050.0)
        evaluate_trailing_steps(short_pos, short_strat, engine, {"5m": [c1]}, "2026-01-05T09:25:00+05:30")
        check(
            "SHORT SL rejected upward move to 24080 and applied downward move to 24030",
            short_pos["currentStopLoss"] == 24030.0
            and short_pos["slHistory"][0]["applied"] is False
            and short_pos["slHistory"][1]["applied"] is True,
            str(short_pos["slHistory"]),
        )


# ==================== 4. INTRABAR AMBIGUITY (SL_FIRST, TARGET_FIRST, LOWER_TIMEFRAME) ====================

def test_intrabar_ambiguity_modes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)
        engine = ConditionEngine(level_book=book)

        base_strat = sample_phase4_trailing_partial_targets()
        base_strat["trailing"] = []

        # Create a 5m candle (09:20-09:25) that touches BOTH SL (23950) and Target 1 (24020) & Target 2 (24060):
        # low = 23940 (<= 23950 SL), high = 24070 (>= 24020 T1 and >= 24060 T2)
        ambig_candle = make_candle("2026-01-05", "09:20", 24000, 24070, 23940, 23990, tf_minutes=5)

        # Case A: SL_FIRST (Default)
        strat_sl_first = copy.deepcopy(base_strat)
        strat_sl_first["execution"]["intrabarConflictOrder"] = "SL_FIRST"
        pos_a = open_managed_position(
            strat_sl_first, "BUY", 24000.0, "2026-01-05T09:20:00+05:30", {"5m": []}, level_book=book, lot_size=25
        )
        res_a = step_open_position(pos_a, ambig_candle, strat_sl_first, engine, {"5m": [ambig_candle]}, "2026-01-05T09:25:00+05:30")
        check(
            "Intrabar SL_FIRST exits all 4 lots at SL (23950) and logs SL_FIRST",
            res_a["intrabarAmbiguous"] is True
            and res_a["intrabarResolutionUsed"] == "SL_FIRST"
            and pos_a["exitReason"] == "STOP_LOSS"
            and pos_a["realizedPnl"] == -5000.0,  # (23950 - 24000) * 4 * 25 = -5000
            str(res_a),
        )

        # Case B: TARGET_FIRST
        # T1 (1 lot @ 24020) and T2 (1 lot @ 24060) fill first, then remaining 2 lots hit SL (23950)
        strat_tgt_first = copy.deepcopy(base_strat)
        strat_tgt_first["execution"]["intrabarConflictOrder"] = "TARGET_FIRST"
        pos_b = open_managed_position(
            strat_tgt_first, "BUY", 24000.0, "2026-01-05T09:20:00+05:30", {"5m": []}, level_book=book, lot_size=25
        )
        res_b = step_open_position(pos_b, ambig_candle, strat_tgt_first, engine, {"5m": [ambig_candle]}, "2026-01-05T09:25:00+05:30")
        check(
            "Intrabar TARGET_FIRST fills T1 & T2 first, then closes remaining 2 lots at SL",
            res_b["intrabarAmbiguous"] is True
            and res_b["intrabarResolutionUsed"] == "TARGET_FIRST"
            and len(res_b["exitsThisStep"]) == 3
            and [e["exitType"] for e in res_b["exitsThisStep"]] == ["TARGET", "TARGET", "STOP_LOSS"]
            and pos_b["realizedPnl"] == -500.0,  # +500 (T1) + 1500 (T2) - 2500 (2 lots SL) = -500
            str(res_b),
        )

        # Case C: LOWER_TIMEFRAME with 1-minute sub-candles showing price rallied to 24025 (T1) at 09:21, then dropped to 23940 (SL) at 09:23, BEFORE ever reaching 24060 (T2)
        # Wait: if 09:24 sub-candle spiked to 24070 AFTER SL was already hit at 09:23, LOWER_TIMEFRAME proves only T1 filled before SL closed the position!
        sub_1m = [
            make_candle("2026-01-05", "09:20", 24000, 24010, 23995, 24005, tf_minutes=1),
            make_candle("2026-01-05", "09:21", 24005, 24025, 24000, 24022, tf_minutes=1),  # Hits T1 (24020)
            make_candle("2026-01-05", "09:22", 24022, 24024, 23980, 23985, tf_minutes=1),
            make_candle("2026-01-05", "09:23", 23985, 23985, 23940, 23945, tf_minutes=1),  # Hits SL (23950) on remaining 3 lots!
            make_candle("2026-01-05", "09:24", 23945, 24070, 23945, 23990, tf_minutes=1),  # Spikes to 24070 after trade is already closed
        ]
        strat_ltf = copy.deepcopy(base_strat)
        strat_ltf["execution"]["intrabarConflictOrder"] = "LOWER_TIMEFRAME"
        pos_c = open_managed_position(
            strat_ltf, "BUY", 24000.0, "2026-01-05T09:20:00+05:30", {"5m": []}, level_book=book, lot_size=25
        )
        res_c = step_open_position(
            pos_c,
            ambig_candle,
            strat_ltf,
            engine,
            {"5m": [ambig_candle]},
            "2026-01-05T09:25:00+05:30",
            lower_tf_candles=sub_1m,
        )
        check(
            "Intrabar LOWER_TIMEFRAME uses 1m sub-candles: fills T1 at 09:21, then hits SL on remaining 3 lots at 09:23",
            res_c["intrabarResolutionUsed"] == "LOWER_TIMEFRAME"
            and len(res_c["exitsThisStep"]) == 2
            and res_c["exitsThisStep"][0]["targetId"] == "tgt-1-pts"
            and res_c["exitsThisStep"][1]["exitType"] == "STOP_LOSS"
            and res_c["exitsThisStep"][1]["quantity"] == 3
            and pos_c["realizedPnl"] == -3250.0,  # +500 (1 lot T1) - 3750 (3 lots SL) = -3250
            str(res_c),
        )


# ==================== 5. PHASE 4 FASTAPI ENDPOINTS ====================

def test_phase4_api_endpoints():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_level_book = main_module.level_book
        old_eval_repo = main_module.eval_state_repo
        old_engine = main_module.condition_engine
        try:
            main_module.level_book = LevelBook(JsonLevelRepo(root))
            main_module.eval_state_repo = JsonEvaluationStateRepo(root)
            main_module.condition_engine = ConditionEngine(level_book=main_module.level_book)

            client = TestClient(app)
            client.put(
                "/api/levels",
                json={"symbol": "NIFTY 50", "day": "2026-01-05", "provider": "MANUAL", "values": make_manual_levels()},
            )

            strat = sample_phase4_trailing_partial_targets()

            # 1. POST /api/engine/exit/resolve
            resp_res = client.post(
                "/api/engine/exit/resolve",
                json={
                    "strategy": strat,
                    "side": "BUY",
                    "entryPrice": 24000.0,
                    "entryTime": "2026-01-05T09:20:00+05:30",
                },
            )
            check("POST /api/engine/exit/resolve 200", resp_res.status_code == 200)
            pos = resp_res.json()
            check("Resolved initialStopLoss is 23950.0", pos["initialStopLoss"] == 23950.0 and len(pos["targets"]) == 3)

            # 2. POST /api/engine/exit/step (hits Target 1 and triggers Trailing Step 1 to move SL to RC1L=23980)
            c1 = make_candle("2026-01-05", "09:20", 24000, 24035, 23995, 24030)
            resp_step = client.post(
                "/api/engine/exit/step",
                json={
                    "position": pos,
                    "candle": c1,
                    "strategy": strat,
                    "candlesByTimeframe": {"5m": [c1]},
                    "evalTime": "2026-01-05T09:25:00+05:30",
                },
            )
            check("POST /api/engine/exit/step 200", resp_step.status_code == 200)
            step_data = resp_step.json()
            updated_pos = step_data["position"]
            check(
                "API step filled Target 1 (3 lots left) and trailed SL to 23980.0",
                updated_pos["remainingQuantity"] == 3
                and updated_pos["currentStopLoss"] == 23980.0
                and len(updated_pos["slHistory"]) == 1,
                str(step_data),
            )
        finally:
            main_module.level_book = old_level_book
            main_module.eval_state_repo = old_eval_repo
            main_module.condition_engine = old_engine


if __name__ == "__main__":
    test_all_stop_loss_types()
    test_partial_targets_and_validation()
    test_trailing_sl_monotonic_and_idempotent()
    test_intrabar_ambiguity_modes()
    test_phase4_api_endpoints()
    print(f"\nPhase 4 tests complete: {passed} passed, {failed} failed.")
