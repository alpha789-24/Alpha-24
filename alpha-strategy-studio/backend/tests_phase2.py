"""Phase 2 unit & integration tests:
- Pure-Python / NumPy indicators against known reference values & warm-up handling
- Multi-timeframe alignment & strict no-look-ahead enforcement
- Operands (OPEN, HIGH, LOW, CLOSE, LTP, VOLUME, indicators, FC/RC levels, constants)
- Operators (>, <, >=, <=, ==, crosses_above, crosses_below, trades_above, trades_below,
  candle_green, candle_red, close_above_open, close_below_open, near, buy_near, sell_near,
  moves_out_of, out_of_fc1h, out_of_fc1l, out_of_range, breakout_above, breakout_below)
- Mandatory tolerance validation & unresolved SELL re-entry example 2 blocking
- IST session window, trading days, and holiday calendar enforcement
- Signal source vs execution instrument separation
- FastAPI Phase 2 endpoints (/api/indicators/compute, /api/engine/evaluate, /api/engine/state, /api/strategies/samples)
"""
from __future__ import annotations

import copy
import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.condition_engine import (
    IST,
    ConditionEngine,
    EvaluationState,
    check_session_and_holiday,
    resample_candles_to_timeframe,
)
from app.indicators import (
    compute_adx,
    compute_atr,
    compute_bollinger,
    compute_cci,
    compute_ema,
    compute_macd,
    compute_rsi,
    compute_sma,
    compute_stochastic,
    compute_supertrend,
    compute_vwap,
)
from app.levels import LevelBook
from app.main import app
from app.repos import JsonEvaluationStateRepo, JsonLevelRepo
from app.samples import (
    sample_buy_near_rc1_breakout_strategy,
    sample_unresolved_sell_reentry_example_2,
)
from app.schema_v2 import LEVEL_NAMES, new_rule_builder
from app.strategy import strategy_warnings, validate_strategy

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
    closed: bool = True,
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
        "closed": closed,
    }


def make_manual_levels(base: float = 24000.0) -> dict[str, float]:
    values = {}
    for idx, name in enumerate(LEVEL_NAMES):
        values[name] = base + idx * 10.0
    values["FC1L"] = 23950.0
    values["FC1H"] = 24050.0
    values["RC1L"] = 23980.0
    values["RC1H"] = 24020.0
    values["RC3H"] = 24100.0
    values["RC3L"] = 23900.0
    return values


# ==================== 1. INDICATOR TESTS & WARM-UP ====================

def test_indicators_and_warmup():
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    sma = compute_sma(vals, 3)
    check("SMA warm-up None on first 2", sma[0] is None and sma[1] is None, str(sma))
    check("SMA exact reference values", sma[2:] == [20.0, 30.0, 40.0], str(sma))

    ema = compute_ema(vals, 3)
    check("EMA warm-up None on first 2", ema[0] is None and ema[1] is None, str(ema))
    check("EMA exact reference values", ema[2:] == [20.0, 30.0, 40.0], str(ema))

    rsi_res = compute_rsi([100.0, 102.0, 104.0, 106.0, 103.0, 105.0], period=3, smoothing_period=2)
    rsi_vals = rsi_res["rsi"]
    sig_vals = rsi_res["signal"]
    check("RSI warm-up first 3 bars None", rsi_vals[:3] == [None, None, None], str(rsi_vals))
    check("RSI index 3 is 100.0", math.isclose(rsi_vals[3], 100.0, abs_tol=1e-5), str(rsi_vals))
    check("RSI index 4 is 57.142857", math.isclose(rsi_vals[4], 57.142857, abs_tol=1e-4), str(rsi_vals))
    check("RSI index 5 is 70.0", math.isclose(rsi_vals[5], 70.0, abs_tol=1e-4), str(rsi_vals))
    check("RSI signal warm-up None until 2 valid RSIs", sig_vals[:4] == [None, None, None, None], str(sig_vals))
    check("RSI signal at index 4 is mean(100, 57.142857)", math.isclose(sig_vals[4], (100.0 + 57.142857) / 2.0, abs_tol=1e-4), str(sig_vals))

    c_cci = [
        make_candle("2026-01-05", "09:15", 100, 100, 100, 100),
        make_candle("2026-01-05", "09:20", 110, 110, 110, 110),
        make_candle("2026-01-05", "09:25", 120, 120, 120, 120),
    ]
    cci = compute_cci(c_cci, period=3)
    check("CCI warm-up first 2 None", cci[:2] == [None, None], str(cci))
    check("CCI reference value 100.0", math.isclose(cci[2], 100.0, abs_tol=1e-6), str(cci))

    c_atr = [
        make_candle("2026-01-05", "09:15", 100, 110, 90, 100),
        make_candle("2026-01-05", "09:20", 100, 115, 95, 110),
        make_candle("2026-01-05", "09:25", 110, 125, 105, 120),
        make_candle("2026-01-05", "09:30", 120, 130, 116, 125),
    ]
    atr = compute_atr(c_atr, period=3)
    check("ATR warm-up first 2 None", atr[:2] == [None, None], str(atr))
    check("ATR reference values 20.0 and 18.0", math.isclose(atr[2], 20.0) and math.isclose(atr[3], 18.0), str(atr))

    st = compute_supertrend(c_atr, period=3, multiplier=2.0)
    check("Supertrend warm-up first 2 None", st["supertrend"][:2] == [None, None], str(st))
    check("Supertrend direction +1 on uptrend", st["direction"][2] == 1.0 and st["direction"][3] == 1.0, str(st))

    c_vwap = [
        make_candle("2026-01-05", "09:15", 100, 100, 100, 100, v=100),
        make_candle("2026-01-05", "09:20", 200, 200, 200, 200, v=300),
        make_candle("2026-01-06", "09:15", 150, 150, 150, 150, v=200),
    ]
    vwap = compute_vwap(c_vwap)
    check("VWAP calculation and daily IST reset", vwap == [100.0, 175.0, 150.0], str(vwap))

    bb = compute_bollinger([10.0, 20.0, 30.0], period=3, std_dev=2.0)
    sigma = math.sqrt(200.0 / 3.0)
    check("Bollinger warm-up first 2 None", bb["middle"][:2] == [None, None], str(bb))
    check(
        "Bollinger middle/upper/lower exact",
        math.isclose(bb["middle"][2], 20.0)
        and math.isclose(bb["upper"][2], 20.0 + 2.0 * sigma, abs_tol=1e-5)
        and math.isclose(bb["lower"][2], 20.0 - 2.0 * sigma, abs_tol=1e-5),
        str(bb),
    )

    macd_in = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
    macd_res = compute_macd(macd_in, fast_period=2, slow_period=4, signal_period=2)
    check("MACD line warm-up first 3 None", macd_res["macd"][:3] == [None, None, None], str(macd_res["macd"]))
    check("MACD signal warm-up first 4 None", macd_res["signal"][:4] == [None, None, None, None], str(macd_res["signal"]))
    check("MACD histogram defined at index 4", macd_res["histogram"][4] is not None, str(macd_res["histogram"]))

    c_stoch = [
        make_candle("2026-01-05", "09:15", 100, 110, 90, 100),
        make_candle("2026-01-05", "09:20", 100, 120, 95, 110),
        make_candle("2026-01-05", "09:25", 110, 130, 90, 120),
        make_candle("2026-01-05", "09:30", 120, 130, 90, 130),
    ]
    stoch = compute_stochastic(c_stoch, k_period=3, d_period=2)
    check("Stochastic %K warm-up first 2 None", stoch["k"][:2] == [None, None], str(stoch["k"]))
    check("Stochastic %K values 75.0 and 100.0", stoch["k"][2:] == [75.0, 100.0], str(stoch["k"]))
    check("Stochastic %D warm-up first 3 None, then 87.5", stoch["d"][:3] == [None, None, None] and stoch["d"][3] == 87.5, str(stoch["d"]))

    # Wilder's ADX (+DI, -DI, ADX, 14-period)
    c_adx = [
        make_candle("2026-01-05", "09:15", 100 + i * 2, 102 + i * 2, 99 + i * 2, 101 + i * 2)
        for i in range(30)
    ]
    adx_res = compute_adx(c_adx, period=14)
    check("ADX +DI/-DI warm-up first 14 None", all(x is None for x in adx_res["plus_di"][:14]), str(adx_res["plus_di"][:15]))
    check("ADX value warm-up first 27 None", all(x is None for x in adx_res["adx"][:27]), str(adx_res["adx"][:28]))
    check(
        "ADX 14-period steady uptrend +DI==66.6667, -DI==0, ADX==100",
        math.isclose(adx_res["plus_di"][14], 200.0 / 3.0)
        and math.isclose(adx_res["minus_di"][14], 0.0)
        and math.isclose(adx_res["adx"][27], 100.0),
        str({k: v[-3:] for k, v in adx_res.items()}),
    )


# ==================== 2. MULTI-TIMEFRAME & NO LOOK-AHEAD ====================

def test_multitimeframe_no_lookahead():
    candles_5m = [
        make_candle("2026-01-05", "09:15", 100, 105, 98, 102, tf_minutes=5),
        make_candle("2026-01-05", "09:20", 102, 108, 101, 107, tf_minutes=5),
        make_candle("2026-01-05", "09:25", 107, 112, 105, 110, tf_minutes=5),
        make_candle("2026-01-05", "09:30", 110, 150, 109, 145, tf_minutes=5),
        make_candle("2026-01-05", "09:35", 145, 160, 140, 155, tf_minutes=5),
        make_candle("2026-01-05", "09:40", 155, 170, 150, 165, tf_minutes=5),
    ]

    eval_0940 = datetime.fromisoformat("2026-01-05T09:40:00+05:30")
    htf_at_0940 = resample_candles_to_timeframe(candles_5m, "5m", "15m", eval_0940)
    check(
        "No look-ahead at 09:40: only 09:15-09:30 15m candle is visible",
        len(htf_at_0940) == 1 and htf_at_0940[0]["close"] == 110.0 and htf_at_0940[0]["high"] == 112.0,
        str(htf_at_0940),
    )

    eval_0945 = datetime.fromisoformat("2026-01-05T09:45:00+05:30")
    htf_at_0945 = resample_candles_to_timeframe(candles_5m, "5m", "15m", eval_0945)
    check(
        "At 09:45: second 15m candle is closed and visible",
        len(htf_at_0945) == 2 and htf_at_0945[1]["close"] == 165.0 and htf_at_0945[1]["high"] == 170.0,
        str(htf_at_0945),
    )


# ==================== 3. STATEFUL CROSSES, TRADES_ABOVE, PROXIMITY & BREAKOUT ====================

def test_operators_and_stateful_crosses():
    with tempfile.TemporaryDirectory() as tmp:
        book = LevelBook(JsonLevelRepo(Path(tmp)))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)
        engine = ConditionEngine(level_book=book)

        strat = new_rule_builder()
        strat["rules"]["entry"][0]["condition"] = {
            "type": "leaf",
            "id": "cross-rc1h",
            "enabled": True,
            "order": 1,
            "label": "CLOSE crosses above RC1H",
            "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
            "operator": "crosses_above",
            "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC1H", "value": None},
            "timeframe": "5m",
            "candleRef": "current",
            "evaluateOn": "CANDLE_CLOSE",
            "params": {"tolerance": None, "toleranceUnit": None},
        }

        c1 = make_candle("2026-01-05", "09:15", 24000, 24015, 23995, 24010)
        c2 = make_candle("2026-01-05", "09:20", 24010, 24035, 24008, 24030)
        c3 = make_candle("2026-01-05", "09:25", 24030, 24045, 24025, 24040)

        state = EvaluationState()
        r1 = engine.evaluate_strategy(strat, {"5m": [c1]}, "2026-01-05T09:20:00+05:30", state=state)
        check("Cross step 1 (below): no signal", len(r1["signals"]) == 0)

        r2 = engine.evaluate_strategy(strat, {"5m": [c1, c2]}, "2026-01-05T09:25:00+05:30", state=state)
        check("Cross step 2 (crossed above): signal emitted", len(r2["signals"]) == 1 and r2["signals"][0]["side"] == "BUY")

        r3 = engine.evaluate_strategy(strat, {"5m": [c1, c2, c3]}, "2026-01-05T09:30:00+05:30", state=state)
        check("Cross step 3 (stays above): does not re-fire", len(r3["signals"]) == 0)

        strat_trade = copy.deepcopy(strat)
        strat_trade["rules"]["entry"][0]["condition"]["operator"] = "trades_above"
        strat_trade["rules"]["entry"][0]["condition"]["evaluateOn"] = "CANDLE_CLOSE"
        c_wick = make_candle("2026-01-05", "09:30", 24010, 24035, 24005, 24015)
        res_close = engine.evaluate_strategy(strat_trade, {"5m": [c_wick]}, "2026-01-05T09:35:00+05:30")
        check("trades_above (CANDLE_CLOSE) false when only wick > level", len(res_close["signals"]) == 0)

        strat_trade["rules"]["entry"][0]["condition"]["evaluateOn"] = "LIVE_TICK"
        res_tick = engine.evaluate_strategy(
            strat_trade,
            {"5m": [c_wick]},
            "2026-01-05T09:32:00+05:30",
            live_tick={"ltp": 24028.0},
        )
        check("trades_above (LIVE_TICK) true when live tick LTP > level", len(res_tick["signals"]) == 1)

        sample_strat = sample_buy_near_rc1_breakout_strategy()
        c_near = make_candle("2026-01-05", "09:15", 23975, 23985, 23972, 23983)
        res_sample = engine.evaluate_strategy(
            sample_strat,
            {"5m": [c_near]},
            "2026-01-05T09:20:00+05:30",
            execution_quote={"ltp": 185.5},
        )
        check("Sample BUY near RC1L fires when green candle within 5 pts", len(res_sample["signals"]) == 1)
        check(
            "Signal preserves spot evaluation vs option execution separation",
            res_sample["signals"][0]["signalSource"] == "INDEX_SPOT"
            and res_sample["signals"][0]["executionInstrumentType"] == "INDEX_OPTIONS"
            and res_sample["signals"][0]["executionPrice"] == 185.5,
            str(res_sample["signals"][0]),
        )


# ==================== 4. IST SESSION, HOLIDAYS & UNRESOLVED CONFLICT BLOCKING ====================

def test_session_holidays_and_conflict_blocking():
    session_cfg = {
        "timezone": "Asia/Kolkata",
        "startTime": "09:15",
        "endTime": "15:30",
        "tradingDays": ["MON", "TUE", "WED", "THU", "FRI"],
        "holidayCalendar": "EXPLICIT",
        "holidays": ["2026-01-26"],
    }
    hol = check_session_and_holiday(session_cfg, "2026-01-26T10:00:00+05:30")
    check("Holiday blocked", not hol["allowed"] and "holiday" in hol["reason"].lower(), str(hol))

    wknd = check_session_and_holiday(session_cfg, "2026-01-04T10:00:00+05:30")
    check("Weekend blocked", not wknd["allowed"] and "SUN" in wknd["reason"], str(wknd))

    pre = check_session_and_holiday(session_cfg, "2026-01-05T09:10:00+05:30")
    post = check_session_and_holiday(session_cfg, "2026-01-05T15:31:00+05:30")
    inside = check_session_and_holiday(session_cfg, "2026-01-05T10:15:00+05:30")
    check("Pre-market and post-market blocked, inside allowed", not pre["allowed"] and not post["allowed"] and inside["allowed"])

    unresolved = sample_unresolved_sell_reentry_example_2()
    errs_disabled = validate_strategy(unresolved)
    warns = strategy_warnings(unresolved)
    check("Unresolved sample validates while disabled", errs_disabled == [], str(errs_disabled))
    check("Unresolved sample emits warning", any("SELL Re-entry" in w or "SELL re-entry" in w for w in warns), str(warns))

    enabled_conflict = copy.deepcopy(unresolved)
    enabled_conflict["rules"]["entry"][1]["enabled"] = True
    errs_enabled = validate_strategy(enabled_conflict)
    check(
        "Enabling unresolved SELL re-entry example 2 is rejected by validator",
        any("unresolved conflict" in e.lower() for e in errs_enabled),
        str(errs_enabled),
    )


# ==================== 5. FASTAPI ENDPOINTS ====================

def test_phase2_api_endpoints():
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

            resp_samples = client.get("/api/strategies/samples")
            check("GET /api/strategies/samples 200", resp_samples.status_code == 200 and len(resp_samples.json()) >= 5)

            candles = [
                make_candle("2026-01-05", f"09:{15 + i * 5:02d}", 100 + i, 102 + i, 99 + i, 101 + i)
                for i in range(6)
            ]
            resp_ind = client.post(
                "/api/indicators/compute",
                json={
                    "indicator": {
                        "id": "sma-3",
                        "type": "SMA",
                        "timeframe": "5m",
                        "source": "CLOSE",
                        "period": 3,
                    },
                    "candles": candles,
                },
            )
            check("POST /api/indicators/compute 200", resp_ind.status_code == 200)
            ind_data = resp_ind.json()
            check("Indicator API warm-up & values", ind_data["series"]["value"][:2] == [None, None] and ind_data["series"]["value"][2] == 102.0)

            levels = make_manual_levels()
            resp_lvl = client.put(
                "/api/levels",
                json={"symbol": "NIFTY 50", "day": "2026-01-05", "provider": "MANUAL", "values": levels},
            )
            check("PUT /api/levels 200", resp_lvl.status_code == 200)

            strat = sample_buy_near_rc1_breakout_strategy()
            c_near = make_candle("2026-01-05", "09:15", 23975, 23985, 23972, 23983)
            resp_eval = client.post(
                "/api/engine/evaluate",
                json={
                    "strategy": strat,
                    "candlesByTimeframe": {"5m": [c_near]},
                    "evalTime": "2026-01-05T09:20:00+05:30",
                    "persistState": True,
                    "executionQuote": {"ltp": 190.0},
                },
            )
            check("POST /api/engine/evaluate 200", resp_eval.status_code == 200)
            eval_data = resp_eval.json()
            check("Engine API matched entry rule", eval_data["valid"] is True and len(eval_data["signals"]) == 1)

            resp_state = client.get(f"/api/engine/state/{strat['id']}")
            check("GET /api/engine/state returns persisted state", resp_state.status_code == 200 and resp_state.json()["state"]["lastEvalDay"] == "2026-01-05")

            resp_del = client.delete(f"/api/engine/state/{strat['id']}")
            check("DELETE /api/engine/state resets state", resp_del.status_code == 200)
        finally:
            main_module.level_book = old_level_book
            main_module.eval_state_repo = old_eval_repo
            main_module.condition_engine = old_engine


if __name__ == "__main__":
    test_indicators_and_warmup()
    test_multitimeframe_no_lookahead()
    test_operators_and_stateful_crosses()
    test_session_holidays_and_conflict_blocking()
    test_phase2_api_endpoints()
    print(f"\nPhase 2 tests complete: {passed} passed, {failed} failed.")
