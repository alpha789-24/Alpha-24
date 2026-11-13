"""
Unit tests for sar_engine.py — tests worked examples 1-6.
Run: python tests_sar.py from the backend directory.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone, timedelta
from app.sar_engine import parse_config, validate_sar, build_ladder, new_run, step, compute_levels

IST = timezone(timedelta(hours=5, minutes=30))

passed = 0
failed = 0

def make_time(hh, mm):
    """Create a datetime in IST at 2026-11-06 HH:MM."""
    return datetime(2026, 11, 6, hh, mm, 0, tzinfo=IST)


def base_strategy(upper=330, lower=325, sl=1, tgt=1.5, qty=100, mults=None, max_legs=4,
                  level_source="MANUAL", entry_style="REVERSAL", max_daily_loss=999999,
                  start_time="09:15", end_time="15:00", after_target="STOP", max_restarts=2):
    if mults is None:
        mults = [3, 2, 2]
    return {
        "id": "test-strat",
        "name": "Test SAR",
        "instrument": "SBIN",
        "strategyType": "LEVEL_SAR",
        "sar": {
            "levelSource": level_source,
            "entryStyle": entry_style,
            "manual": {"upper": upper, "lower": lower},
            "candle": {"timeframe": "15m", "upperSource": "HIGH", "lowerSource": "LOW"},
            "slPoints": sl,
            "tgtPoints": tgt,
            "baseQuantity": qty,
            "multipliers": mults,
            "maxLegs": max_legs,
            "startTime": start_time,
            "endTime": end_time,
            "afterTarget": after_target,
            "maxRestarts": max_restarts
        },
        "risk": {"maxDailyLoss": max_daily_loss, "maxCapital": 999999}
    }


def feed_prices(run, cfg, prices, t=None):
    """Feed a list of prices through step(), return all events."""
    if t is None:
        t = make_time(9, 30)
    all_events = []
    for p in prices:
        evts = step(run, cfg, p, t)
        all_events.extend(evts)
    return all_events


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"PASS {name}")
        passed += 1
    else:
        print(f"FAIL {name}: {detail}")
        failed += 1


# ===== Example 1: Manual, REVERSAL, upper triggers first =====
def test_example_1():
    s = base_strategy()
    cfg = parse_config(s)
    run = new_run(s, cfg)
    t = make_time(9, 30)

    # Feed prices: 329 (arm, first tick), 330 (trigger SELL), 331 (SL), 330 (SL), 331 (SL), 332.5 (TGT)
    prices = [329, 330, 331, 330, 331, 332.5]
    all_events = []
    for p in prices:
        evts = step(run, cfg, p, t)
        all_events.extend(evts)

    legs = run["legs"]
    check("Ex1: 4 legs closed", len(legs) == 4,
          f"got {len(legs)} legs: {[(l['legNo'], l['side'], l['pnl']) for l in legs]}")

    if len(legs) >= 4:
        # Leg 1: SELL 330, SL 331, TGT 328.5, qty 100, pnl -100
        check("Ex1: Leg1 SELL entry 330", legs[0]["side"] == "SELL" and legs[0]["entry"] == 330)
        check("Ex1: Leg1 SL 331", legs[0]["sl"] == 331)
        check("Ex1: Leg1 TGT 328.5", legs[0]["tgt"] == 328.5)
        check("Ex1: Leg1 qty 100", legs[0]["qty"] == 100)
        check("Ex1: Leg1 pnl -100", legs[0]["pnl"] == -100, f"got {legs[0]['pnl']}")

        # Leg 2: BUY 331, SL 330, TGT 332.5, qty 300, pnl -300
        check("Ex1: Leg2 BUY entry 331", legs[1]["side"] == "BUY" and legs[1]["entry"] == 331)
        check("Ex1: Leg2 SL 330", legs[1]["sl"] == 330)
        check("Ex1: Leg2 TGT 332.5", legs[1]["tgt"] == 332.5)
        check("Ex1: Leg2 qty 300", legs[1]["qty"] == 300)
        check("Ex1: Leg2 pnl -300", legs[1]["pnl"] == -300, f"got {legs[1]['pnl']}")

        # Leg 3: SELL 330, SL 331, TGT 328.5, qty 600, pnl -600
        check("Ex1: Leg3 SELL entry 330", legs[2]["side"] == "SELL" and legs[2]["entry"] == 330)
        check("Ex1: Leg3 SL 331", legs[2]["sl"] == 331)
        check("Ex1: Leg3 TGT 328.5", legs[2]["tgt"] == 328.5)
        check("Ex1: Leg3 qty 600", legs[2]["qty"] == 600)
        check("Ex1: Leg3 pnl -600", legs[2]["pnl"] == -600, f"got {legs[2]['pnl']}")

        # Leg 4: BUY 331, SL 330, TGT 332.5, qty 1200, pnl +1800
        check("Ex1: Leg4 BUY entry 331", legs[3]["side"] == "BUY" and legs[3]["entry"] == 331)
        check("Ex1: Leg4 SL 330", legs[3]["sl"] == 330)
        check("Ex1: Leg4 TGT 332.5", legs[3]["tgt"] == 332.5)
        check("Ex1: Leg4 qty 1200", legs[3]["qty"] == 1200)
        check("Ex1: Leg4 pnl +1800", legs[3]["pnl"] == 1800, f"got {legs[3]['pnl']}")

    check("Ex1: total pnl 800", run["realizedPnl"] == 800, f"got {run['realizedPnl']}")
    check("Ex1: status COMPLETED", run["status"] == "COMPLETED", f"got {run['status']}")


# ===== Example 2: Support triggers first =====
def test_example_2():
    s = base_strategy()
    cfg = parse_config(s)
    run = new_run(s, cfg)
    t = make_time(9, 30)

    # Price path: 326 (first tick), 325 (trigger BUY at lower), 324 (SL), 325 (SL), 326.5 (TGT)
    prices = [326, 325, 324, 325, 326.5]
    all_events = []
    for p in prices:
        evts = step(run, cfg, p, t)
        all_events.extend(evts)

    legs = run["legs"]
    check("Ex2: 3 legs closed", len(legs) == 3,
          f"got {len(legs)} legs: {[(l['legNo'], l['side'], l['pnl']) for l in legs]}")

    if len(legs) >= 3:
        # Leg 1: BUY 325, SL 324, TGT 326.5, qty 100
        check("Ex2: Leg1 BUY entry 325", legs[0]["side"] == "BUY" and legs[0]["entry"] == 325)
        check("Ex2: Leg1 SL 324", legs[0]["sl"] == 324)
        check("Ex2: Leg1 TGT 326.5", legs[0]["tgt"] == 326.5)
        check("Ex2: Leg1 qty 100", legs[0]["qty"] == 100)

        # Leg 2: SELL 324, SL 325, TGT 322.5, qty 300
        check("Ex2: Leg2 SELL entry 324", legs[1]["side"] == "SELL" and legs[1]["entry"] == 324)
        check("Ex2: Leg2 SL 325", legs[1]["sl"] == 325)
        check("Ex2: Leg2 TGT 322.5", legs[1]["tgt"] == 322.5)
        check("Ex2: Leg2 qty 300", legs[1]["qty"] == 300)

        # Leg 3: BUY 325, SL 324, TGT 326.5, qty 600
        check("Ex2: Leg3 BUY entry 325", legs[2]["side"] == "BUY" and legs[2]["entry"] == 325)
        check("Ex2: Leg3 qty 600", legs[2]["qty"] == 600)
        check("Ex2: Leg3 TGT hit pnl", legs[2]["pnl"] == 900, f"got {legs[2]['pnl']}")

    check("Ex2: status COMPLETED", run["status"] == "COMPLETED", f"got {run['status']}")
    # Upper level 330 must be completely ignored
    check("Ex2: activeLevelSide LOWER", run["activeLevelSide"] == "LOWER")


# ===== Example 3: Candle levels, BREAKOUT =====
def test_example_3():
    s = base_strategy(level_source="CANDLE", entry_style="BREAKOUT")
    cfg = parse_config(s)
    run = new_run(s, cfg)
    t = make_time(9, 30)

    # Provide level candle data (low=325, high=328)
    level_candle = {"open": 326, "high": 328, "low": 325, "close": 327, "complete": True}
    levels = compute_levels(cfg, level_candle=level_candle)

    # Manually set levels on run
    run["levels"] = levels
    run["status"] = "ARMED"

    check("Ex3: upper=328 lower=325", levels["upper"] == 328 and levels["lower"] == 325,
          f"got upper={levels['upper']} lower={levels['lower']}")

    # Price path: 327 (first tick), 324.9 (crosses below 325 -> SELL)
    # BREAKOUT SELL: prev >= LOWER and cmp < LOWER -> prev=327 >= 325, cmp=324.9 < 325 -> trigger
    # Leg1 SELL entry 325 SL 326 TGT 323.5 qty 100
    prices = [327, 324.9, 326, 325, 326, 327.5]
    all_events = []
    for p in prices:
        evts = step(run, cfg, p, t)
        all_events.extend(evts)

    legs = run["legs"]
    check("Ex3: 4 legs closed", len(legs) == 4,
          f"got {len(legs)}: {[(l['legNo'], l['side'], l['entry'], l['pnl']) for l in legs]}")

    if len(legs) >= 4:
        check("Ex3: Leg1 SELL entry 325", legs[0]["side"] == "SELL" and legs[0]["entry"] == 325)
        check("Ex3: Leg1 SL 326 TGT 323.5", legs[0]["sl"] == 326 and legs[0]["tgt"] == 323.5)
        check("Ex3: Leg1 qty 100", legs[0]["qty"] == 100)

        check("Ex3: Leg2 BUY entry 326", legs[1]["side"] == "BUY" and legs[1]["entry"] == 326)
        check("Ex3: Leg2 SL 325 TGT 327.5", legs[1]["sl"] == 325 and legs[1]["tgt"] == 327.5)
        check("Ex3: Leg2 qty 300", legs[1]["qty"] == 300)

        check("Ex3: Leg3 SELL entry 325", legs[2]["side"] == "SELL" and legs[2]["entry"] == 325)
        check("Ex3: Leg3 qty 600", legs[2]["qty"] == 600)

        check("Ex3: Leg4 BUY entry 326", legs[3]["side"] == "BUY" and legs[3]["entry"] == 326)
        check("Ex3: Leg4 SL 325 TGT 327.5", legs[3]["sl"] == 325 and legs[3]["tgt"] == 327.5)
        check("Ex3: Leg4 qty 1200", legs[3]["qty"] == 1200)
        check("Ex3: Leg4 TGT hit", legs[3]["exitReason"] == "TARGET")

    check("Ex3: status COMPLETED", run["status"] == "COMPLETED", f"got {run['status']}")
    check("Ex3: activeLevelSide LOWER", run["activeLevelSide"] == "LOWER")


# ===== Example 4: SL hit on leg 4 -> STOPPED_MAX_LEGS =====
def test_example_4():
    s = base_strategy()
    cfg = parse_config(s)
    run = new_run(s, cfg)
    t = make_time(9, 30)

    # Price path that hits SL on all 4 legs:
    # 329 (first tick), 330 (trigger SELL), 331 (SL), 330 (SL), 331 (SL), 330 (SL on leg4)
    prices = [329, 330, 331, 330, 331, 330]
    all_events = []
    for p in prices:
        evts = step(run, cfg, p, t)
        all_events.extend(evts)

    legs = run["legs"]
    check("Ex4: 4 legs closed", len(legs) == 4,
          f"got {len(legs)}: {[(l['legNo'], l['side'], l['exitReason']) for l in legs]}")
    check("Ex4: status STOPPED_MAX_LEGS", run["status"] == "STOPPED_MAX_LEGS", f"got {run['status']}")
    check("Ex4: no currentLeg", run["currentLeg"] is None)

    if len(legs) >= 4:
        for leg in legs:
            check(f"Ex4: Leg{leg['legNo']} reason STOP_LOSS", leg["exitReason"] == "STOP_LOSS")


# ===== Example 5: Risk cap blocks leg 4 =====
def test_example_5():
    s = base_strategy(max_daily_loss=2000)
    cfg = parse_config(s)
    run = new_run(s, cfg)
    t = make_time(9, 30)

    # Same price path: SLs accumulate.
    # Leg 1: loss 100. Leg 2: loss 300. Leg 3: loss 600. Total realizedLoss = 1000.
    # Leg 4 would risk 1200 (slPoints 1 * qty 1200). 1000 + 1200 = 2200 > 2000 -> BLOCKED.
    prices = [329, 330, 331, 330, 331]
    all_events = []
    for p in prices:
        evts = step(run, cfg, p, t)
        all_events.extend(evts)

    legs = run["legs"]
    check("Ex5: 3 legs closed", len(legs) == 3,
          f"got {len(legs)}: {[(l['legNo'], l['side'], l['pnl']) for l in legs]}")
    check("Ex5: realizedLoss 1000", run["realizedLoss"] == 1000, f"got {run['realizedLoss']}")
    check("Ex5: status STOPPED_RISK_LIMIT", run["status"] == "STOPPED_RISK_LIMIT", f"got {run['status']}")

    # Check that a RISK_BLOCKED event was generated
    blocked_events = [e for e in all_events if e.get("type") == "RISK_BLOCKED"]
    check("Ex5: RISK_BLOCKED event exists", len(blocked_events) >= 1)


# ===== Example 6: End time exit =====
def test_example_6():
    s = base_strategy(end_time="10:00")
    cfg = parse_config(s)
    run = new_run(s, cfg)

    # Arm at 09:30
    step(run, cfg, 329, make_time(9, 30))
    # Trigger at 09:31
    step(run, cfg, 330, make_time(9, 31))

    check("Ex6: leg open", run["currentLeg"] is not None and run["currentLeg"]["legNo"] == 1)

    # Time passes, at 10:00 -> TIME_EXIT
    evts = step(run, cfg, 329.5, make_time(10, 0))

    close_events = [e for e in evts if e.get("type") == "CLOSE_LEG"]
    check("Ex6: CLOSE_LEG event", len(close_events) == 1)
    if close_events:
        check("Ex6: reason TIME_EXIT", close_events[0]["reason"] == "TIME_EXIT")
        check("Ex6: exit at cmp 329.5", close_events[0]["exitPrice"] == 329.5)
    check("Ex6: status COMPLETED", run["status"] == "COMPLETED", f"got {run['status']}")


# ===== Additional: validate_sar tests =====
def test_validation():
    # Valid strategy
    s = base_strategy()
    errs, warns = validate_sar(s)
    check("Validate: no errors on valid", len(errs) == 0, f"errors: {errs}")

    # upper <= lower
    s2 = base_strategy(upper=325, lower=330)
    errs2, _ = validate_sar(s2)
    check("Validate: upper <= lower error", any("Upper level must be greater" in e for e in errs2), f"errors: {errs2}")

    # slPoints <= 0
    s3 = base_strategy(sl=0)
    errs3, _ = validate_sar(s3)
    check("Validate: slPoints <= 0 error", any("Stop-loss points" in e for e in errs3))

    # maxLegs out of range
    s4 = base_strategy(max_legs=1, mults=[])
    errs4, _ = validate_sar(s4)
    check("Validate: maxLegs < 2 error", any("between 2 and 6" in e for e in errs4))

    # Wrong multipliers count
    s5 = base_strategy(max_legs=4, mults=[2, 2])
    errs5, _ = validate_sar(s5)
    check("Validate: wrong mult count", any("exactly 3" in e for e in errs5), f"errors: {errs5}")


# ===== Additional: build_ladder test =====
def test_build_ladder():
    s = base_strategy()
    cfg = parse_config(s)
    sell_ladder = build_ladder(cfg, "SELL", 330)

    check("Ladder: 4 legs", len(sell_ladder) == 4)
    if len(sell_ladder) == 4:
        check("Ladder: entries 330/331/330/331",
              [l["entry"] for l in sell_ladder] == [330, 331, 330, 331],
              f"got {[l['entry'] for l in sell_ladder]}")
        check("Ladder: qty 100/300/600/1200",
              [l["qty"] for l in sell_ladder] == [100, 300, 600, 1200],
              f"got {[l['qty'] for l in sell_ladder]}")
        check("Ladder: sides SELL/BUY/SELL/BUY",
              [l["side"] for l in sell_ladder] == ["SELL", "BUY", "SELL", "BUY"])

    buy_ladder = build_ladder(cfg, "BUY", 325)
    check("Ladder BUY: 4 legs", len(buy_ladder) == 4)
    if len(buy_ladder) == 4:
        check("Ladder BUY: entries 325/324/325/324",
              [l["entry"] for l in buy_ladder] == [325, 324, 325, 324],
              f"got {[l['entry'] for l in buy_ladder]}")


# ===== Additional: expired test =====
def test_expired():
    s = base_strategy(end_time="10:00")
    cfg = parse_config(s)
    run = new_run(s, cfg)

    # First tick, arm
    step(run, cfg, 327, make_time(9, 30))
    check("Expired: armed", run["status"] == "ARMED")

    # End time without trade
    step(run, cfg, 327, make_time(10, 0))
    check("Expired: status EXPIRED", run["status"] == "EXPIRED", f"got {run['status']}")


# ===== Run all tests =====
if __name__ == "__main__":
    test_validation()
    test_build_ladder()
    test_example_1()
    test_example_2()
    test_example_3()
    test_example_4()
    test_example_5()
    test_example_6()
    test_expired()

    print(f"\n{'='*50}")
    print(f"Results: {passed} PASS, {failed} FAIL")
    if failed:
        print("SOME TESTS FAILED!")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED!")
        sys.exit(0)

