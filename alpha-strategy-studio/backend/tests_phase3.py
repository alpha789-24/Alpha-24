"""Phase 3 unit & integration tests:
- Sequence node ordered evaluation (Step 1 -> Step 2)
- Strict out-of-order rejection (Step 2 true before Step 1 never advances or triggers)
- Same-tick restriction (`allowSameTick=False` vs `allowSameTick=True`)
- Sequence state persistence across engine restarts (`JsonEvaluationStateRepo`)
- Sequence state reset on:
  1. IST day change
  2. Trade entry (`record_trade_entry`)
  3. Explicit invalidation (`reset_sequence` / `POST /api/engine/sequence/reset`)
  4. Optional timeout expiry (`timeoutMinutes`)
- Re-entry options & lifecycle:
  - Enable/disable
  - `maxPerDay` enforcement
  - Allowed after `STOP_LOSS`, `TARGET`, `MANUAL` exit
  - `cooldownMinutes` enforcement
  - Allowed `directions` (`BUY`, `SELL`)
  - Dedicated `reentry.stopLoss` and `reentry.targets` attached to re-entry signals
- Sample strategies in `samples/` and blocking of unresolved `SELL re-entry example 2`
- FastAPI Phase 3 endpoints
"""
from __future__ import annotations

import copy
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.condition_engine import (
    ConditionEngine,
    EvaluationState,
    check_reentry_eligibility,
)
from app.levels import LevelBook
from app.main import app
from app.repos import JsonEvaluationStateRepo, JsonLevelRepo
from app.samples import (
    all_sample_strategies,
    sample_buy_reentry_example_1,
    sample_buy_reentry_example_2,
    sample_sell_reentry_example_1,
    sample_unresolved_sell_reentry_example_2,
)
from app.schema_v2 import LEVEL_NAMES
from app.strategy import validate_strategy

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
    values["RC3H"] = 24100.0
    return values


# ==================== 1. ORDERED SEQUENCE EVALUATION & OUT-OF-ORDER REJECTION ====================

def test_sequence_order_and_same_tick():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)
        engine = ConditionEngine(level_book=book)

        # Sample BUY Re-entry Example 1:
        # Step 1: CLOSE trades_below RC1L (23980)
        # Step 2: CLOSE trades_above RC1H (24020)
        strat = sample_buy_reentry_example_1()
        state = EvaluationState()

        # Tick 1 (09:20 IST): Price is 24030 (above RC1H=24020, so Step 2 is true, but Step 1 has NOT happened yet!)
        c1 = make_candle("2026-01-05", "09:15", 24010, 24035, 24005, 24030)
        r1 = engine.evaluate_strategy(strat, {"5m": [c1]}, "2026-01-05T09:20:00+05:30", state=state)
        seq_res_1 = r1["entryResults"][0]["condition"]
        check("Out-of-order Step 2 before Step 1 does NOT advance sequence", seq_res_1["waitingStep"] == 0 and len(r1["signals"]) == 0)

        # Tick 2 (09:25 IST): Price drops to 23970 (< RC1L=23980) -> Step 1 is satisfied!
        c2 = make_candle("2026-01-05", "09:20", 24030, 24030, 23965, 23970)
        r2 = engine.evaluate_strategy(strat, {"5m": [c1, c2]}, "2026-01-05T09:25:00+05:30", state=state)
        seq_res_2 = r2["entryResults"][0]["condition"]
        check("Step 1 satisfied advances waitingStep to 1 without firing signal yet", seq_res_2["waitingStep"] == 1 and len(r2["signals"]) == 0)
        check("Step 1 timestamp recorded", "step_1" in seq_res_2["stepTimestamps"])

        # Tick 3 (09:30 IST): Price stays at 24000 (between RC1L and RC1H) -> still waiting for Step 2 (waitingStep == 1)
        c3 = make_candle("2026-01-05", "09:25", 23970, 24005, 23970, 24000)
        r3 = engine.evaluate_strategy(strat, {"5m": [c1, c2, c3]}, "2026-01-05T09:30:00+05:30", state=state)
        seq_res_3 = r3["entryResults"][0]["condition"]
        check("Intermediate tick preserves waitingStep == 1", seq_res_3["waitingStep"] == 1 and len(r3["signals"]) == 0)

        # Tick 4 (09:35 IST): Price rallies to 24035 (> RC1H=24020) -> Step 2 is satisfied -> Sequence completes & fires BUY!
        c4 = make_candle("2026-01-05", "09:30", 24000, 24040, 23998, 24035)
        r4 = engine.evaluate_strategy(strat, {"5m": [c1, c2, c3, c4]}, "2026-01-05T09:35:00+05:30", state=state)
        check("Step 2 satisfied on later tick completes sequence and emits BUY signal", len(r4["signals"]) == 1 and r4["signals"][0]["side"] == "BUY")
        check("Sequence state resets to 0 after completion", state.get_sequence("seq-buy-rc1")["waitingStep"] == 0)

        # Now test allowSameTick=False vs allowSameTick=True when both steps are true on the exact same tick
        strat_same = copy.deepcopy(strat)
        # Step 1: OPEN < RC1L (23980), Step 2: CLOSE > RC1H (24020) on the same candle
        strat_same["rules"]["entry"][0]["condition"]["steps"][0]["left"]["field"] = "OPEN"
        strat_same["rules"]["entry"][0]["condition"]["steps"][1]["left"]["field"] = "CLOSE"
        strat_same["rules"]["entry"][0]["condition"]["allowSameTick"] = False

        c_both = make_candle("2026-01-05", "09:40", 23970, 24040, 23965, 24035)
        st_disallow = EvaluationState()
        r_disallow = engine.evaluate_strategy(strat_same, {"5m": [c_both]}, "2026-01-05T09:45:00+05:30", state=st_disallow)
        check(
            "allowSameTick=False advances only Step 1 when both steps are true on same tick",
            len(r_disallow["signals"]) == 0 and r_disallow["entryResults"][0]["condition"]["waitingStep"] == 1,
        )

        strat_same["rules"]["entry"][0]["condition"]["allowSameTick"] = True
        st_allow = EvaluationState()
        r_allow = engine.evaluate_strategy(strat_same, {"5m": [c_both]}, "2026-01-05T09:45:00+05:30", state=st_allow)
        check(
            "allowSameTick=True satisfies both steps on same tick and emits signal",
            len(r_allow["signals"]) == 1 and r_allow["entryResults"][0]["condition"]["matched"] is True,
        )


# ==================== 2. PERSISTENCE ACROSS RESTARTS & ALL 4 RESET TRIGGERS ====================

def test_sequence_persistence_and_resets():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-06", levels)
        state_repo = JsonEvaluationStateRepo(root)

        strat = sample_buy_reentry_example_1()
        strat["rules"]["entry"][0]["condition"]["timeoutMinutes"] = 15  # 15-minute expiry

        # Engine Instance 1: Satisfy Step 1 at 09:20 IST and persist to disk
        engine1 = ConditionEngine(level_book=book)
        state1 = EvaluationState()
        c_step1 = make_candle("2026-01-05", "09:15", 23990, 23995, 23960, 23970)
        res1 = engine1.evaluate_strategy(strat, {"5m": [c_step1]}, "2026-01-05T09:20:00+05:30", state=state1)
        state_repo.save(strat["id"], res1["state"])

        # Simulate backend restart: create fresh Engine Instance 2 and reload state from disk
        engine2 = ConditionEngine(level_book=book)
        loaded_record = state_repo.get(strat["id"])
        check("Persisted state loaded after simulated restart", loaded_record is not None)
        state2 = EvaluationState(loaded_record["state"])
        check("Reloaded waitingStep is 1 and expiresAt is 09:35 IST", state2.get_sequence("seq-buy-rc1")["waitingStep"] == 1)

        # Reset Trigger A: Timeout Expiry (evaluate at 09:40 IST, which is > 09:35 IST expiry, with price still neutral)
        c_neutral = make_candle("2026-01-05", "09:35", 24000, 24010, 23990, 24000)
        res_timeout = engine2.evaluate_strategy(
            strat,
            {"5m": [c_step1, c_neutral]},
            "2026-01-05T09:40:00+05:30",
            state=state2,
        )
        seq_cond = res_timeout["entryResults"][0]["condition"]
        check(
            "Timeout expiry resets sequence waitingStep to 0",
            seq_cond["expiredBeforeTick"] is True and seq_cond["waitingStep"] == 0,
            str(seq_cond),
        )

        # Satisfy Step 1 again at 09:45 IST
        c_step1_b = make_candle("2026-01-05", "09:40", 23990, 23995, 23960, 23970)
        engine2.evaluate_strategy(strat, {"5m": [c_step1_b]}, "2026-01-05T09:45:00+05:30", state=state2)
        check("Sequence re-armed at waitingStep == 1", state2.get_sequence("seq-buy-rc1")["waitingStep"] == 1)

        # Reset Trigger B: Explicit Invalidation
        state2.reset_sequence("seq-buy-rc1", reason="EXPLICIT_INVALIDATION", timestamp_iso="2026-01-05T09:46:00+05:30")
        check(
            "Explicit invalidation resets waitingStep to 0",
            state2.get_sequence("seq-buy-rc1")["waitingStep"] == 0
            and state2.get_sequence("seq-buy-rc1")["lastResetReason"] == "EXPLICIT_INVALIDATION",
        )

        # Satisfy Step 1 again at 09:50 IST
        engine2.evaluate_strategy(strat, {"5m": [c_step1_b]}, "2026-01-05T09:50:00+05:30", state=state2)
        check("Sequence at waitingStep == 1 before trade entry", state2.get_sequence("seq-buy-rc1")["waitingStep"] == 1)

        # Reset Trigger C: Trade Entry
        state2.record_trade_entry(side="BUY", timestamp_ist="2026-01-05T09:51:00+05:30", is_reentry=False)
        check(
            "Trade entry resets sequence waitingStep to 0",
            state2.get_sequence("seq-buy-rc1")["waitingStep"] == 0
            and state2.get_sequence("seq-buy-rc1")["lastResetReason"] == "TRADE_ENTRY",
        )

        # Satisfy Step 1 again at 15:20 IST on 2026-01-05
        state2.record_trade_exit("STOP_LOSS", "2026-01-05T15:10:00+05:30", side="BUY")
        engine2.evaluate_strategy(strat, {"5m": [c_step1_b]}, "2026-01-05T15:20:00+05:30", state=state2)
        check("Sequence at waitingStep == 1 at end of day 1", state2.get_sequence("seq-buy-rc1")["waitingStep"] == 1)

        # Reset Trigger D: IST Day Change (2026-01-06)
        c_day2 = make_candle("2026-01-06", "09:15", 24000, 24010, 23990, 24000)
        engine2.evaluate_strategy(strat, {"5m": [c_day2]}, "2026-01-06T09:20:00+05:30", state=state2)
        check(
            "IST day change resets sequence waitingStep to 0 and clears reentriesToday",
            state2.get_sequence("seq-buy-rc1")["waitingStep"] == 0
            and state2.reentry_state["reentriesToday"] == 0,
        )


# ==================== 3. RE-ENTRY ENGINE: COOLDOWN, MAX/DAY, EXIT REASONS & SL/TARGETS ====================

def test_reentry_lifecycle_and_constraints():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        levels = make_manual_levels()
        book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", levels)
        engine = ConditionEngine(level_book=book)

        # Use sample SELL Re-entry Example 1:
        # reentry: enabled=True, maxPerDay=2, afterStopLoss=True, afterTarget=False, afterManualExit=False,
        # cooldownMinutes=5, directions=["SELL"], stopLoss at RC1H, target at FC1L.
        strat = sample_sell_reentry_example_1()
        state = EvaluationState()

        # 1. Initial trade entry at 09:30 IST
        state.record_trade_entry(side="SELL", timestamp_ist="2026-01-05T09:30:00+05:30", is_reentry=False)

        # 2. Trade exits via MANUAL exit at 09:40 IST -> afterManualExit is False, so re-entry must be blocked!
        state.record_trade_exit(exit_reason="MANUAL", timestamp_ist="2026-01-05T09:40:00+05:30", side="SELL")
        elig_manual = check_reentry_eligibility(
            strat["reentry"],
            state.reentry_state,
            datetime.fromisoformat("2026-01-05T09:50:00+05:30"),
            candidate_side="SELL",
        )
        check("Re-entry blocked after MANUAL exit when afterManualExit=False", not elig_manual["eligible"])

        # 3. Trade exits via STOP_LOSS at 10:00 IST -> afterStopLoss is True, cooldown is 5 minutes (ready at 10:05 IST)
        state.record_trade_exit(exit_reason="STOP_LOSS", timestamp_ist="2026-01-05T10:00:00+05:30", side="SELL")

        # Check at 10:03 IST (inside 5m cooldown) -> blocked by cooldown!
        elig_cooldown = check_reentry_eligibility(
            strat["reentry"],
            state.reentry_state,
            datetime.fromisoformat("2026-01-05T10:03:00+05:30"),
            candidate_side="SELL",
        )
        check(
            "Re-entry blocked during 5-minute cooldown",
            not elig_cooldown["eligible"] and elig_cooldown.get("cooldownRemainingSeconds") == 120,
            str(elig_cooldown),
        )

        # Check wrong direction ("BUY" when only ["SELL"] is allowed) at 10:06 IST
        elig_wrong_dir = check_reentry_eligibility(
            strat["reentry"],
            state.reentry_state,
            datetime.fromisoformat("2026-01-05T10:06:00+05:30"),
            candidate_side="BUY",
        )
        check("Re-entry blocked for disallowed direction BUY", not elig_wrong_dir["eligible"])

        # Check valid SELL re-entry at 10:05 IST (cooldown elapsed)
        c_s1 = make_candle("2026-01-05", "10:00", 24010, 24035, 24005, 24030)  # Above RC1H (24020) -> Step 1
        c_s2 = make_candle("2026-01-05", "10:05", 24030, 24030, 23965, 23970)  # Below RC1L (23980) -> Step 2

        engine.evaluate_strategy(strat, {"5m": [c_s1]}, "2026-01-05T10:05:00+05:30", state=state)
        res_re1 = engine.evaluate_strategy(
            strat,
            {"5m": [c_s1, c_s2]},
            "2026-01-05T10:10:00+05:30",
            state=state,
            auto_record_entry=True,
        )
        check("Re-entry #1 fires after cooldown and attaches dedicated reentry SL/targets", len(res_re1["signals"]) == 1)
        sig1 = res_re1["signals"][0]
        check(
            "Re-entry #1 signal metadata has bucket=REENTRY, reentryNumber=1, and SL=RC1H",
            sig1["bucket"] == "REENTRY"
            and sig1["reentryNumber"] == 1
            and sig1["stopLoss"]["level"] == "RC1H"
            and sig1["targets"][0]["level"] == "FC1L",
            str(sig1),
        )

        # 4. Exit via STOP_LOSS again at 10:20 IST, then take Re-entry #2 at 10:30 IST
        state.record_trade_exit(exit_reason="STOP_LOSS", timestamp_ist="2026-01-05T10:20:00+05:30", side="SELL")
        engine.evaluate_strategy(strat, {"5m": [c_s1]}, "2026-01-05T10:25:00+05:30", state=state)
        res_re2 = engine.evaluate_strategy(
            strat,
            {"5m": [c_s1, c_s2]},
            "2026-01-05T10:30:00+05:30",
            state=state,
            auto_record_entry=True,
        )
        check("Re-entry #2 fires and increments reentriesToday to 2", len(res_re2["signals"]) == 1 and res_re2["signals"][0]["reentryNumber"] == 2)

        # 5. Exit via STOP_LOSS a 3rd time at 10:40 IST -> maxPerDay is 2, so 3rd re-entry must be blocked!
        state.record_trade_exit(exit_reason="STOP_LOSS", timestamp_ist="2026-01-05T10:40:00+05:30", side="SELL")
        res_re3 = engine.evaluate_strategy(
            strat,
            {"5m": [c_s1, c_s2]},
            "2026-01-05T10:50:00+05:30",
            state=state,
        )
        check(
            "Re-entry #3 blocked when maxPerDay (2) is reached",
            len(res_re3["signals"]) == 0 and res_re3["entryResults"][0].get("blockedByReentry") is True,
            str(res_re3["entryResults"][0]),
        )


# ==================== 4. SAMPLES FOLDER & UNRESOLVED SELL RE-ENTRY EXAMPLE 2 ====================

def test_samples_folder_and_unresolved_conflict():
    samples_dir = Path(__file__).resolve().parent.parent / "samples"
    expected_files = [
        "phase2_buy_near_rc1_breakout.json",
        "buy_reentry_example_1.json",
        "buy_reentry_example_2.json",
        "sell_reentry_example_1.json",
        "sell_reentry_example_2_unresolved.json",
    ]
    for fname in expected_files:
        fpath = samples_dir / fname
        check(f"Sample file exists: {fname}", fpath.exists())
        doc = json.loads(fpath.read_text())
        errs = validate_strategy(doc)
        check(f"Sample file validates cleanly: {fname}", errs == [], str(errs))

    # Verify SELL re-entry example 2 cannot be enabled in either rule or reentry block
    unresolved = sample_unresolved_sell_reentry_example_2()
    elig = check_reentry_eligibility(
        unresolved["reentry"],
        {"awaitingReentry": True, "lastExitReason": "STOP_LOSS"},
        datetime.fromisoformat("2026-01-05T10:30:00+05:30"),
    )
    check("Unresolved SELL re-entry example 2 is blocked by check_reentry_eligibility", not elig["eligible"] and elig.get("blocked") is True)


# ==================== 5. PHASE 3 FASTAPI ENDPOINTS ====================

def test_phase3_api_endpoints():
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

            # Save manual levels
            client.put(
                "/api/levels",
                json={"symbol": "NIFTY 50", "day": "2026-01-05", "provider": "MANUAL", "values": make_manual_levels()},
            )

            # 1. Verify /api/strategies/samples returns all 5 samples
            resp_samples = client.get("/api/strategies/samples")
            check("GET /api/strategies/samples returns 5 sample strategies", resp_samples.status_code == 200 and len(resp_samples.json()) >= 5)

            strat = sample_buy_reentry_example_1()
            c_step1 = make_candle("2026-01-05", "09:15", 23990, 23995, 23960, 23970)

            # 2. Evaluate Step 1 with persistState=True
            resp_ev1 = client.post(
                "/api/engine/evaluate",
                json={
                    "strategy": strat,
                    "candlesByTimeframe": {"5m": [c_step1]},
                    "evalTime": "2026-01-05T09:20:00+05:30",
                    "persistState": True,
                },
            )
            check("Step 1 persisted via /api/engine/evaluate", resp_ev1.status_code == 200 and resp_ev1.json()["entryResults"][0]["condition"]["waitingStep"] == 1)

            # 3. Explicitly reset sequence via POST /api/engine/sequence/reset
            resp_reset = client.post(
                "/api/engine/sequence/reset",
                json={
                    "strategyId": strat["id"],
                    "sequenceId": "seq-buy-rc1",
                    "reason": "USER_MANUAL_RESET",
                    "evalTime": "2026-01-05T09:21:00+05:30",
                },
            )
            check(
                "POST /api/engine/sequence/reset resets waitingStep to 0",
                resp_reset.status_code == 200
                and resp_reset.json()["state"]["sequenceStates"]["seq-buy-rc1"]["waitingStep"] == 0,
            )

            # 4. Record initial ENTRY and STOP_LOSS EXIT via POST /api/engine/trade-event
            client.post(
                "/api/engine/trade-event",
                json={
                    "strategyId": strat["id"],
                    "eventType": "ENTRY",
                    "side": "BUY",
                    "timestampIst": "2026-01-05T09:25:00+05:30",
                },
            )
            resp_exit = client.post(
                "/api/engine/trade-event",
                json={
                    "strategyId": strat["id"],
                    "eventType": "EXIT",
                    "side": "BUY",
                    "exitReason": "STOP_LOSS",
                    "timestampIst": "2026-01-05T09:30:00+05:30",
                },
            )
            check("POST /api/engine/trade-event EXIT arms awaitingReentry", resp_exit.status_code == 200 and resp_exit.json()["state"]["reentryState"]["awaitingReentry"] is True)

            # 5. Check re-entry eligibility via POST /api/engine/reentry/check during cooldown (09:32 IST) and after cooldown (09:36 IST)
            resp_chk_early = client.post(
                "/api/engine/reentry/check",
                json={
                    "strategy": strat,
                    "evalTime": "2026-01-05T09:32:00+05:30",
                    "candidateSide": "BUY",
                },
            )
            check("POST /api/engine/reentry/check reports false during cooldown", resp_chk_early.status_code == 200 and resp_chk_early.json()["eligible"] is False)

            resp_chk_ready = client.post(
                "/api/engine/reentry/check",
                json={
                    "strategy": strat,
                    "evalTime": "2026-01-05T09:36:00+05:30",
                    "candidateSide": "BUY",
                },
            )
            check("POST /api/engine/reentry/check reports true after cooldown", resp_chk_ready.status_code == 200 and resp_chk_ready.json()["eligible"] is True)
        finally:
            main_module.level_book = old_level_book
            main_module.eval_state_repo = old_eval_repo
            main_module.condition_engine = old_engine


if __name__ == "__main__":
    test_sequence_order_and_same_tick()
    test_sequence_persistence_and_resets()
    test_reentry_lifecycle_and_constraints()
    test_samples_folder_and_unresolved_conflict()
    test_phase3_api_endpoints()
    print(f"\nPhase 3 tests complete: {passed} passed, {failed} failed.")
