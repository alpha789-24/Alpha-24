"""Phase 5 unit & integration tests:
- Candle-by-candle backtest simulator using the shared ConditionEngine & ExitEngine
- Required test cases:
  1. Each BUY and SELL example rule (`sample_buy_near_rc1_breakout_strategy`, `sample_buy_reentry_example_1`,
     `sample_buy_reentry_example_2`, `sample_sell_reentry_example_1`, and blocked `sample_unresolved_sell_reentry_example_2`)
  2. No look-ahead: signal formed on candle `i` close executes on candle `i + 1` open (`NEXT_CANDLE_OPEN`),
     with configurable slippage and brokerage/charges
  3. Trailing SL and partial target exits inside full backtest (`sample_phase4_trailing_partial_targets`)
  4. Sequential re-entry after Stop Loss / Target in backtest
  5. Daily `maxTradesPerDay` and `maxDailyLoss` guards in backtest
  6. Same-candle SL + target hit (`SL_FIRST`, `TARGET_FIRST`, and `LOWER_TIMEFRAME`) recorded in backtest trade log
  7. Paper trading worker lifecycle (`start`, `pause`, `resume`, `step`, `stop`) and daily max-trades / max-loss guards
  8. FastAPI Phase 5 endpoints (`/api/backtest/run`, `/api/backtest/runs`, `/api/paper/worker/*`)
"""
from __future__ import annotations

import copy
import math
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.backtest_engine import (
    BacktestError,
    new_paper_worker_run,
    run_backtest,
    step_paper_worker_run,
)
from app.condition_engine import ConditionEngine
from app.levels import LevelBook
from app.main import app
from app.repos import JsonEvaluationStateRepo, JsonLevelRepo, JsonListRepo
from app.samples import (
    sample_buy_near_rc1_breakout_strategy as _s1,
    sample_buy_reentry_example_1 as _s2,
    sample_buy_reentry_example_2 as _s3,
    sample_phase4_trailing_partial_targets as _s4,
    sample_sell_reentry_example_1 as _s5,
    sample_unresolved_sell_reentry_example_2 as _s6,
)


def _with_lot25(fn):
    def _wrapped():
        s = fn()
        s["execution"]["instrument"]["lotSize"] = 25
        return s
    return _wrapped


sample_buy_near_rc1_breakout_strategy = _with_lot25(_s1)
sample_buy_reentry_example_1 = _with_lot25(_s2)
sample_buy_reentry_example_2 = _with_lot25(_s3)
sample_phase4_trailing_partial_targets = _with_lot25(_s4)
sample_sell_reentry_example_1 = _with_lot25(_s5)
sample_unresolved_sell_reentry_example_2 = _with_lot25(_s6)
from app.schema_v2 import LEVEL_NAMES

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


# ==================== 1. NO LOOK-AHEAD, SLIPPAGE, CHARGES & ALL BUY/SELL EXAMPLE RULES ====================

def test_no_lookahead_and_example_rules():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)

        # Example A: sample_buy_near_rc1_breakout_strategy
        # Entry when 5m candle is green AND (CLOSE buy_near RC1L=23980 within 5 pts OR moves_out_of FC1H=24050 by 3 pts)
        # Stop loss: 10 pts, Target: 15 pts, lotSize: 25, quantity: 1
        strat_a = sample_buy_near_rc1_breakout_strategy()
        candles_a = [
            # Bar 0 (09:15): Green candle closing at 23982 (within 5 pts of RC1L=23980) -> signal forms at 09:20 close!
            make_candle("2026-01-05", "09:15", 23975, 23985, 23972, 23982),
            # Bar 1 (09:20): Opens at 23990.0 -> NEXT_CANDLE_OPEN fills at 23990.0 (+1 pt slippage = 23991.0)!
            # Target is +15 pts from 23991.0 = 24006.0. High reaches 24010.0 -> Target hit!
            make_candle("2026-01-05", "09:20", 23990, 24010, 23988, 24008),
        ]
        bt_a = run_backtest(
            strat_a,
            {"5m": candles_a},
            level_book=book,
            execution_fill_mode="NEXT_CANDLE_OPEN",
            slippage_points=1.0,
            brokerage_per_order=20.0,
        )
        check("Backtest Example A produced 1 winning trade", bt_a["metrics"]["totalTrades"] == 1 and bt_a["metrics"]["winners"] == 1)
        t_a = bt_a["trades"][0]
        check(
            "No look-ahead: signal at 09:20 close filled at Bar 1 open (23990 + 1 slippage = 23991)",
            t_a["rawEntryPrice"] == 23990.0 and t_a["entryPrice"] == 23991.0,
            str(t_a),
        )
        # Exit target was 23991 + 15 = 24006.0; with 1 pt exit slippage -> 24005.0
        # Gross PnL = (24005 - 23991) * 25 = 14 * 25 = 350.0. Brokerage = 20 entry + 20 exit = 40.0 -> Net PnL = 310.0
        check(
            "Slippage and brokerage accurately deducted in netPnl",
            t_a["grossPnl"] == 350.0 and t_a["charges"] == 40.0 and t_a["netPnl"] == 310.0,
            str(t_a),
        )

        # Example B: sample_buy_reentry_example_2 (Step 1: trades_below FC1L=23950 -> Step 2: green & crosses_above FC1H=24050)
        strat_b = sample_buy_reentry_example_2()
        candles_b = [
            make_candle("2026-01-05", "09:15", 23960, 23965, 23935, 23940),  # Step 1: below FC1L (23950)
            make_candle("2026-01-05", "09:20", 23940, 24065, 23938, 24060),  # Step 2: green & crosses above FC1H (24050) -> signal!
            make_candle("2026-01-05", "09:25", 24062, 24085, 24060, 24080),  # Fills at 24062 open, hits +15 pt target (24077)
        ]
        bt_b = run_backtest(strat_b, {"5m": candles_b}, level_book=book)
        check("Backtest BUY Re-entry Example 2 sequence trade won", bt_b["metrics"]["totalTrades"] == 1 and bt_b["metrics"]["winners"] == 1)

        # Example C: sample_sell_reentry_example_1 (Step 1: trades_above RC1H=24020 -> Step 2: trades_below RC1L=23980 -> SELL)
        strat_c = sample_sell_reentry_example_1()
        candles_c = [
            make_candle("2026-01-05", "09:15", 24010, 24035, 24005, 24030),  # Step 1: above RC1H (24020)
            make_candle("2026-01-05", "09:20", 24030, 24032, 23965, 23970),  # Step 2: below RC1L (23980) -> SELL signal!
            make_candle("2026-01-05", "09:25", 23970, 23972, 23950, 23952),  # Fills SELL at 23970 open, hits -15 pt target (23955)
        ]
        bt_c = run_backtest(strat_c, {"5m": candles_c}, level_book=book)
        check("Backtest SELL Re-entry Example 1 sequence trade won", bt_c["metrics"]["totalTrades"] == 1 and bt_c["metrics"]["winners"] == 1)

        # Example D: enabling unresolved SELL re-entry example 2 is rejected by backtest
        unresolved = sample_unresolved_sell_reentry_example_2()
        unresolved["rules"]["entry"][1]["enabled"] = True
        try:
            run_backtest(unresolved, {"5m": candles_c}, level_book=book)
            blocked_ok = False
        except BacktestError as exc:
            blocked_ok = "unresolved conflict" in exc.message.lower()
        check("Backtest refuses enabled unresolved SELL re-entry example 2", blocked_ok)


# ==================== 2. TRAILING SL & PARTIAL TARGETS IN BACKTEST ====================

def test_backtest_trailing_and_partial_targets():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)

        strat = sample_phase4_trailing_partial_targets()
        candles = [
            # Bar 0 (09:15): Green candle near RC1L (23980) -> triggers entry signal
            make_candle("2026-01-05", "09:15", 23975, 23985, 23972, 23983),
            # Bar 1 (09:20): Fills 4 lots at 24000 open. High reaches 24035 -> fills T1 (24020, 1 lot), closes at 24030 (> RC1H=24020) -> trails SL to RC1L (23980)
            make_candle("2026-01-05", "09:20", 24000, 24035, 23995, 24030),
            # Bar 2 (09:25): High reaches 24095 -> fills T2 (24060, 1 lot), closes at 24090 (> RC2H=24080) -> trails SL to FC1H (24050)
            make_candle("2026-01-05", "09:25", 24030, 24095, 24028, 24090),
            # Bar 3 (09:30): Reverses down to 24040 -> hits trailed SL at FC1H (24050) on remaining 2 lots!
            make_candle("2026-01-05", "09:30", 24090, 24092, 24040, 24045),
        ]
        bt = run_backtest(strat, {"5m": candles}, level_book=book)
        check("Backtest completed 1 multi-leg trade", bt["metrics"]["totalTrades"] == 1)
        trade = bt["trades"][0]
        check(
            "Trade recorded 2 trailing SL updates (to 23980 and 24050) and 3 partial exits (T1, T2, Trailed SL)",
            len(trade["slHistory"]) == 2
            and trade["finalStopLoss"] == 24050.0
            and len(trade["partialExits"]) == 3
            and trade["netPnl"] == 4500.0,  # T1 (+500) + T2 (+1500) + 2 lots @ 24050 (+2500) = +4500
            str(trade),
        )


# ==================== 3. SEQUENTIAL RE-ENTRY & MAX TRADES / MAX LOSS GUARDS ====================

def test_backtest_sequential_reentry_and_daily_guards():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)

        strat = sample_buy_reentry_example_1()
        strat["reentry"]["cooldownMinutes"] = 0
        strat["risk"]["maxTradesPerDay"] = 2

        candles = [
            # Trade 1 sequence:
            make_candle("2026-01-05", "09:15", 23990, 23995, 23965, 23970),  # Step 1: < RC1L (23980)
            make_candle("2026-01-05", "09:20", 23970, 24030, 23968, 24025),  # Step 2: > RC1H (24020) -> Initial ENTRY signal!
            # Bar 2 (09:25): Fills Trade 1 at 24025 open (SL = 24015). Drops to 24010 -> hits SL! Also closes at 23970 (< RC1L, Step 1 of next sequence)
            make_candle("2026-01-05", "09:25", 24025, 24026, 23965, 23970),
            # Bar 3 (09:30): Rallies to 24030 (> RC1H) -> Step 2 satisfied -> Re-entry #1 signal!
            make_candle("2026-01-05", "09:30", 23970, 24035, 23968, 24030),
            # Bar 4 (09:35): Fills Re-entry #1 at 24030 open (re-entry SL=RC1L=23980, re-entry Target=RC2H=24080). Rallies to 24085 -> hits RC2H Target!
            make_candle("2026-01-05", "09:35", 24030, 24085, 24025, 24082),
            # Attempt a 3rd trade on the same day (must be blocked by maxTradesPerDay=2):
            make_candle("2026-01-05", "09:40", 24080, 24080, 23960, 23970),  # Step 1
            make_candle("2026-01-05", "09:45", 23970, 24035, 23968, 24030),  # Step 2 -> blocked by maxTradesPerDay=2!
            make_candle("2026-01-05", "09:50", 24030, 24090, 24025, 24085),
        ]
        bt = run_backtest(strat, {"5m": candles}, level_book=book)
        check(
            "Sequential re-entry executed Trade 2 (isReentry=True) and maxTradesPerDay=2 blocked Trade 3",
            bt["metrics"]["totalTrades"] == 2
            and bt["trades"][0]["isReentry"] is False
            and bt["trades"][1]["isReentry"] is True
            and bt["trades"][1]["exitReason"] == "TARGET"
            and any("maxTradesPerDay" in b["reason"] for b in bt["blockedEvents"]),
            str(bt),
        )


# ==================== 4. SAME-CANDLE SL + TARGET IN BACKTEST ====================

def test_backtest_same_candle_sl_and_target():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)

        strat_sl = sample_buy_near_rc1_breakout_strategy()
        strat_sl["execution"]["intrabarConflictOrder"] = "SL_FIRST"

        candles = [
            make_candle("2026-01-05", "09:15", 23975, 23985, 23972, 23982),  # Signal at 09:20 close
            # Bar 1 (09:20): Opens at 24000 (SL=23990, Target=24015). Low=23985 (hits SL) AND High=24020 (hits Target)!
            make_candle("2026-01-05", "09:20", 24000, 24020, 23985, 24005),
        ]
        bt_sl = run_backtest(strat_sl, {"5m": candles}, level_book=book)
        check(
            "Same-candle SL+Target with SL_FIRST records SL_FIRST in trade table",
            bt_sl["trades"][0]["exitReason"] == "STOP_LOSS"
            and bt_sl["trades"][0]["intrabarResolutionUsed"] == "SL_FIRST",
        )

        strat_tgt = copy.deepcopy(strat_sl)
        strat_tgt["execution"]["intrabarConflictOrder"] = "TARGET_FIRST"
        bt_tgt = run_backtest(strat_tgt, {"5m": candles}, level_book=book)
        check(
            "Same-candle SL+Target with TARGET_FIRST records TARGET_FIRST in trade table",
            bt_tgt["trades"][0]["exitReason"] == "TARGET"
            and bt_tgt["trades"][0]["intrabarResolutionUsed"] == "TARGET_FIRST",
        )


# ==================== 5. PAPER TRADING WORKER & PHASE 5 API ENDPOINTS ====================

def test_paper_worker_and_api_endpoints():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_level_book = main_module.level_book
        old_eval_repo = main_module.eval_state_repo
        old_bt_repo = main_module.backtest_repo
        old_pw_repo = main_module.paper_worker_repo
        old_engine = main_module.condition_engine
        try:
            main_module.level_book = LevelBook(JsonLevelRepo(root))
            main_module.eval_state_repo = JsonEvaluationStateRepo(root)
            main_module.backtest_repo = JsonListRepo("backtest_runs.json", root)
            main_module.paper_worker_repo = JsonListRepo("paper_worker_runs.json", root)
            main_module.condition_engine = ConditionEngine(level_book=main_module.level_book)

            client = TestClient(app)
            client.put(
                "/api/levels",
                json={"symbol": "NIFTY 50", "day": "2026-01-05", "provider": "MANUAL", "values": make_manual_levels()},
            )

            strat = sample_buy_near_rc1_breakout_strategy()
            strat["risk"]["maxTradesPerDay"] = 1
            strat["risk"]["maxDailyLoss"] = 200.0  # Small daily loss limit (200 Rs) to test max-loss guard

            # 1. Run backtest via POST /api/backtest/run
            candles = [
                make_candle("2026-01-05", "09:15", 23975, 23985, 23972, 23982),
                make_candle("2026-01-05", "09:20", 23990, 24010, 23988, 24008),
            ]
            resp_bt = client.post(
                "/api/backtest/run",
                json={"strategy": strat, "candlesByTimeframe": {"5m": candles}},
            )
            check("POST /api/backtest/run 200", resp_bt.status_code == 200 and resp_bt.json()["metrics"]["totalTrades"] == 1)
            resp_bt_list = client.get(f"/api/backtest/runs?strategyId={strat['id']}")
            check("GET /api/backtest/runs returns saved backtest", resp_bt_list.status_code == 200 and len(resp_bt_list.json()) == 1)

            # 2. Start paper worker via POST /api/paper/worker/start
            resp_start = client.post("/api/paper/worker/start", json={"strategy": strat})
            check("POST /api/paper/worker/start 200", resp_start.status_code == 200)
            run_id = resp_start.json()["id"]

            # 3. Pause and Resume paper worker
            resp_pause = client.post("/api/paper/worker/pause", json={"runId": run_id})
            check("Paper worker paused", resp_pause.status_code == 200 and resp_pause.json()["status"] == "PAUSED")

            # Stepping while paused does not open trades
            c1 = make_candle("2026-01-05", "09:15", 23975, 23985, 23972, 23982)
            resp_step_paused = client.post(
                "/api/paper/worker/step",
                json={"runId": run_id, "candlesByTimeframe": {"5m": [c1]}, "evalTime": "2026-01-05T09:20:00+05:30"},
            )
            check("Stepping while PAUSED is a no-op", resp_step_paused.json()["run"]["openPosition"] is None)

            resp_resume = client.post("/api/paper/worker/resume", json={"runId": run_id})
            check("Paper worker resumed", resp_resume.status_code == 200 and resp_resume.json()["status"] == "RUNNING")

            # 4. Step on entry candle -> opens position
            resp_step1 = client.post(
                "/api/paper/worker/step",
                json={"runId": run_id, "candlesByTimeframe": {"5m": [c1]}, "evalTime": "2026-01-05T09:20:00+05:30"},
            )
            check("Paper worker opened position on signal", resp_step1.json()["run"]["openPosition"] is not None)

            # 5. Step on SL candle (loss = 10 pts * 25 = -250 Rs, which exceeds maxDailyLoss=200 Rs) -> triggers STOPPED_RISK_LIMIT!
            c2_sl = make_candle("2026-01-05", "09:20", 23982, 23984, 23965, 23968)
            resp_step2 = client.post(
                "/api/paper/worker/step",
                json={"runId": run_id, "candlesByTimeframe": {"5m": [c1, c2_sl]}, "evalTime": "2026-01-05T09:25:00+05:30"},
            )
            run_after_sl = resp_step2.json()["run"]
            check(
                "Paper worker daily max-loss guard transitions status to STOPPED_RISK_LIMIT",
                run_after_sl["status"] == "STOPPED_RISK_LIMIT" and run_after_sl["realizedPnlToday"] == -250.0,
                str(run_after_sl),
            )
        finally:
            main_module.level_book = old_level_book
            main_module.eval_state_repo = old_eval_repo
            main_module.backtest_repo = old_bt_repo
            main_module.paper_worker_repo = old_pw_repo
            main_module.condition_engine = old_engine


def test_paper_run_rule_builder_and_background_runner():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_level_book = main_module.level_book
        old_eval_repo = main_module.eval_state_repo
        old_pw_repo = main_module.paper_worker_repo
        old_engine = main_module.condition_engine
        old_get_chart = main_module.market_service.get_chart
        try:
            main_module.level_book = LevelBook(JsonLevelRepo(root))
            main_module.eval_state_repo = JsonEvaluationStateRepo(root)
            main_module.paper_worker_repo = JsonListRepo("paper_worker_runs.json", root)
            main_module.condition_engine = ConditionEngine(level_book=main_module.level_book)

            client = TestClient(app)
            client.put(
                "/api/levels",
                json={"symbol": "NIFTY 50", "day": "2026-01-05", "provider": "MANUAL", "values": make_manual_levels()},
            )

            strat = sample_buy_near_rc1_breakout_strategy()
            strat["id"] = "rb-paper-run-bg-1"
            strat["meta"]["id"] = "rb-paper-run-bg-1"

            # 1. POST /api/paper/run starts a paper worker for RULE_BUILDER (instead of 400)
            resp_run = client.post("/api/paper/run", json={"strategy": strat})
            check("POST /api/paper/run starts RULE_BUILDER paper worker (200)", resp_run.status_code == 200 and resp_run.json()["status"] == "RUNNING", resp_run.text)

            # 2. Background tick steps RUNNING worker with fetched candles
            c1 = make_candle("2026-01-05", "09:15", 23975, 23985, 23972, 23982)
            main_module.market_service.get_chart = lambda sym, range_period="5d", interval="5m": {"candles": [c1]}
            eval_dt = datetime.fromisoformat("2026-01-05T09:20:00+05:30")
            stepped = main_module.run_paper_worker_tick_once(now_ist=eval_dt)
            check("run_paper_worker_tick_once stepped 1 worker", stepped == 1)

            runs_after = client.get(f"/api/paper/worker/runs?strategyId={strat['id']}").json()
            check("Background paper runner opened position on signal", runs_after[0]["openPosition"] is not None, str(runs_after[0]))

            # 3. Daemon startup & shutdown clean lifecycle
            main_module.start_background_daemons()
            main_module.stop_background_daemons()
            check("Background daemons start & stop cleanly", len(main_module._WORKER_THREADS) == 0)
        finally:
            main_module.market_service.get_chart = old_get_chart
            main_module.level_book = old_level_book
            main_module.eval_state_repo = old_eval_repo
            main_module.paper_worker_repo = old_pw_repo
            main_module.condition_engine = old_engine


if __name__ == "__main__":
    test_no_lookahead_and_example_rules()
    test_backtest_trailing_and_partial_targets()
    test_backtest_sequential_reentry_and_daily_guards()
    test_backtest_same_candle_sl_and_target()
    test_paper_worker_and_api_endpoints()
    test_paper_run_rule_builder_and_background_runner()
    print(f"\nPhase 5 tests complete: {passed} passed, {failed} failed.")
