"""Phase 7 Test Suite: Broker Execution, Capabilities, Go-Live Gates, Idempotency & Kill Switch.

Runnable directly with `python tests_phase7.py` (no pytest or fastapi dependency required
for core engine checks; FastAPI endpoint tests also run when fastapi/starlette are installed).
"""
from __future__ import annotations

import copy
from pathlib import Path

from app.backtest_engine import new_paper_worker_run, run_backtest
from app.broker_execution import (
    approve_strategy_for_live,
    compute_strategy_version_hash,
    evaluate_go_live_gates,
    get_broker_capabilities,
    place_broker_order,
    validate_execution_settings,
)
from app.schema_v2 import new_rule_builder as default_rule_builder_strategy


def _check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL: {label} ({detail})")
    print(f"ok {label}")


def _sample_candles_for_day(day: str = "2026-01-06") -> dict[str, list[dict]]:
    # RC1L = 23980, RC1H = 24020, FC1H = 24050
    return {
        "5m": [
            {"date": day, "time": "09:15", "open": 23975.0, "high": 24030.0, "low": 23970.0, "close": 24025.0, "volume": 1500, "closed": True},
            {"date": day, "time": "09:20", "open": 24025.0, "high": 24060.0, "low": 24020.0, "close": 24055.0, "volume": 2100, "closed": True},
            {"date": day, "time": "09:25", "open": 24055.0, "high": 24090.0, "low": 24045.0, "close": 24080.0, "volume": 2400, "closed": True},
        ]
    }


def _sample_levels() -> dict[str, float]:
    return {
        "FC1H": 24050.0, "FC1L": 23950.0, "FC2H": 24070.0, "FC2L": 23930.0,
        "FC3H": 24090.0, "FC3L": 23910.0, "FC4H": 24110.0, "FC4L": 23890.0,
        "RC1H": 24020.0, "RC1L": 23980.0, "RC2H": 24040.0, "RC2L": 23960.0,
        "RC3H": 24060.0, "RC3L": 23940.0, "RC4H": 24080.0, "RC4L": 23920.0,
    }


def _strategy_with_triggered_entry() -> dict:
    from app.samples import sample_buy_near_rc1_breakout_strategy
    strat = sample_buy_near_rc1_breakout_strategy()
    strat["execution"]["instrument"]["lotSize"] = 25
    strat["risk"]["maxQuantity"] = 10
    return strat


def test_broker_capabilities_and_validation() -> None:
    caps = get_broker_capabilities()
    _check("p7 capabilities catalog keys", all(k in caps for k in (
        "brokers", "exchanges", "symbols", "instruments", "expiries",
        "strikes", "optionTypes", "products", "orderTypes", "validities", "modes", "lotSizes"
    )))

    valid_cfg = {
        "broker": "IIFL",
        "exchange": "NFO",
        "symbol": "NIFTY 50",
        "instrument": "INDEX_OPTIONS",
        "expiry": "CURRENT_WEEK",
        "strike": "ATM",
        "optionType": "CE",
        "quantity": 3,
        "lotSize": 25,
        "product": "INTRADAY",
        "orderType": "MARKET",
        "validity": "DAY",
        "amo": False,
        "mode": "PAPER",
    }
    errs = validate_execution_settings(valid_cfg)
    _check("p7 valid execution settings accepted", len(errs) == 0, str(errs))

    bad_cfg = copy.deepcopy(valid_cfg)
    bad_cfg["broker"] = "UNSUPPORTED_BROKER"
    bad_errs = validate_execution_settings(bad_cfg)
    _check("p7 unsupported broker rejected", any("broker" in e for e in bad_errs), str(bad_errs))

    bad_cfg2 = copy.deepcopy(valid_cfg)
    bad_cfg2["strike"] = "DEEP_OTM_99"
    bad_cfg2["orderType"] = "LIMIT"
    bad_cfg2["limitPrice"] = None
    bad_errs2 = validate_execution_settings(bad_cfg2)
    _check("p7 unsupported strike rejected", any("strike" in e for e in bad_errs2), str(bad_errs2))
    _check("p7 limit order without limitPrice rejected", any("limitPrice" in e for e in bad_errs2), str(bad_errs2))


def test_version_hash_and_go_live_gates_with_reset_on_edit() -> None:
    strat = _strategy_with_triggered_entry()
    v_hash_1 = compute_strategy_version_hash(strat)
    _check("p7 versionHash deterministic", v_hash_1 == compute_strategy_version_hash(copy.deepcopy(strat)))

    # Initially no backtest and no paper trading run -> liveTradingUnlocked False
    g0 = evaluate_go_live_gates(
        strategy=strat,
        backtest_runs=[],
        paper_worker_runs=[],
        live_approvals=[],
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        eval_time="2026-01-06T10:00:00+05:30",
    )
    _check("p7 live locked without backtest/paper/approval", g0["liveTradingUnlocked"] is False)
    _check("p7 backtest gate fails initially", g0["gates"]["successfulBacktestOnVersion"]["passed"] is False)
    _check("p7 paper gate fails initially", g0["gates"]["minimumPaperTradingOnVersion"]["passed"] is False)

    # Run backtest and create paper worker run (with 1 tradeToday) on this exact strategy version
    bt = run_backtest(
        strategy=strat,
        candles_by_tf=_sample_candles_for_day("2026-01-06"),
        primary_timeframe="5m",
        manual_levels_by_day={"2026-01-06": _sample_levels()},
    )
    pw = new_paper_worker_run(strat)
    pw["tradesToday"] = 1
    _check("p7 backtest records versionHash", bt.get("versionHash") == v_hash_1)
    _check("p7 paper worker records versionHash", pw.get("versionHash") == v_hash_1)

    g_prereq = evaluate_go_live_gates(
        strategy=strat,
        backtest_runs=[bt],
        paper_worker_runs=[pw],
        live_approvals=[],
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        eval_time="2026-01-06T10:10:00+05:30",
    )
    _check("p7 readyForApproval True once backtest+paper exist", g_prereq["readyForApproval"] is True, str(g_prereq))

    # Attempt live approval with wrong confirmation phrase -> raises ValueError
    wrong_rejected = False
    try:
        approve_strategy_for_live(
            strategy=strat,
            confirmation_text="YES GO LIVE",
            gate_status=g_prereq,
        )
    except ValueError:
        wrong_rejected = True
    _check("p7 wrong confirmation phrase rejected", wrong_rejected is True)

    # Approve with exact confirmation phrase
    expected_phrase = f"ENABLE LIVE TRADING FOR {strat['name']}"
    approval_rec = approve_strategy_for_live(
        strategy=strat,
        confirmation_text=expected_phrase,
        gate_status=g_prereq,
        eval_time="2026-01-06T10:12:00+05:30",
    )
    _check("p7 exact confirmation phrase unlocks version", approval_rec["approved"] is True, str(approval_rec))

    # Verify liveTradingUnlocked remains False when live adapter is not connected,
    # and True only when live_execution_enabled + live_adapter_connected are both True.
    g_blocked = evaluate_go_live_gates(
        strategy=strat,
        backtest_runs=[bt],
        paper_worker_runs=[pw],
        live_approvals=[approval_rec],
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        eval_time="2026-01-06T10:15:00+05:30",
    )
    _check("p7 liveTradingUnlocked False when live adapter not connected", g_blocked["liveTradingUnlocked"] is False)

    g_unlocked = evaluate_go_live_gates(
        strategy=strat,
        backtest_runs=[bt],
        paper_worker_runs=[pw],
        live_approvals=[approval_rec],
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        eval_time="2026-01-06T10:15:00+05:30",
        live_execution_enabled=True,
        live_adapter_connected=True,
    )
    _check("p7 liveTradingUnlocked True when all gates pass and adapter enabled", g_unlocked["liveTradingUnlocked"] is True, str(g_unlocked))

    # Modify the strategy (e.g. change stop loss points) -> versionHash changes -> live approval automatically resets!
    edited_strat = copy.deepcopy(strat)
    edited_strat["stopLoss"]["points"] = 45.0
    v_hash_2 = compute_strategy_version_hash(edited_strat)
    _check("p7 strategy edit changes versionHash", v_hash_2 != v_hash_1)

    g_after_edit = evaluate_go_live_gates(
        strategy=edited_strat,
        backtest_runs=[bt],
        paper_worker_runs=[pw],
        live_approvals=[approval_rec],
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        eval_time="2026-01-06T10:15:00+05:30",
    )
    _check("p7 editing strategy resets liveTradingUnlocked to False", g_after_edit["liveTradingUnlocked"] is False)
    _check("p7 versionChangedReset flagged True", g_after_edit["versionChangedReset"] is True)


def test_order_lifecycle_idempotency_and_market_hours() -> None:
    strat = _strategy_with_triggered_entry()
    bt = run_backtest(
        strategy=strat,
        candles_by_tf=_sample_candles_for_day("2026-01-06"),
        primary_timeframe="5m",
        manual_levels_by_day={"2026-01-06": _sample_levels()},
    )
    pw = new_paper_worker_run(strat)
    pw["tradesToday"] = 1

    g_prereq = evaluate_go_live_gates(
        strategy=strat,
        backtest_runs=[bt],
        paper_worker_runs=[pw],
        live_approvals=[],
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        eval_time="2026-01-06T10:10:00+05:30",
    )
    approval_rec = approve_strategy_for_live(
        strategy=strat,
        confirmation_text=f"ENABLE LIVE TRADING FOR {strat['name']}",
        gate_status=g_prereq,
        eval_time="2026-01-06T10:12:00+05:30",
    )
    g_unlocked = evaluate_go_live_gates(
        strategy=strat,
        backtest_runs=[bt],
        paper_worker_runs=[pw],
        live_approvals=[approval_rec],
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        eval_time="2026-01-06T10:30:00+05:30",
        live_execution_enabled=True,
        live_adapter_connected=True,
    )

    existing_orders: list[dict] = []
    base_exec = {
        "broker": "IIFL",
        "exchange": "NFO",
        "symbol": "NIFTY 50",
        "instrument": "INDEX_OPTIONS",
        "expiry": "CURRENT_WEEK",
        "strike": "ATM",
        "optionType": "CE",
        "quantity": 3,
        "lotSize": 25,
        "product": "INTRADAY",
        "orderType": "MARKET",
        "validity": "DAY",
        "amo": False,
        "mode": "LIVE",
    }

    # 0. LIVE order WITHOUT real broker adapter MUST be rejected ("Live execution not connected") and NEVER marked FILLED
    ord_blocked_live, _ = place_broker_order(
        strategy=strat,
        execution_settings=base_exec,
        side="BUY",
        idempotency_key="idem-live-blocked-001",
        existing_orders=existing_orders,
        gate_status=g_unlocked,
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        reference_price=24050.0,
        eval_time="2026-01-06T10:30:00+05:30",
    )
    _check(
        "p7 LIVE order blocked without real adapter",
        ord_blocked_live["status"] == "REJECTED" and ord_blocked_live.get("rejectionReason") == "Live execution not connected",
        str(ord_blocked_live),
    )

    # 1. Place LIVE MARKET order with a mocked broker adapter confirmation -> FILLED with brokerOrderId
    def _mock_live_adapter(**kwargs):
        return {"status": "FILLED", "fillPrice": kwargs["reference_price"], "brokerOrderId": "MOCK-IIFL-999"}

    ord1, is_dup1 = place_broker_order(
        strategy=strat,
        execution_settings=base_exec,
        side="BUY",
        idempotency_key="idem-key-001",
        existing_orders=existing_orders,
        gate_status=g_unlocked,
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        reference_price=24050.0,
        eval_time="2026-01-06T10:30:00+05:30",
        live_execution_enabled=True,
        live_broker_adapter=_mock_live_adapter,
    )
    existing_orders.append(ord1)
    _check("p7 LIVE MARKET order FILLED via mocked adapter inside market hours", ord1["status"] == "FILLED" and ord1.get("brokerOrderId") == "MOCK-IIFL-999" and not is_dup1, str(ord1))

    # 2. Submit duplicate order with same idempotencyKey -> blocked, returns original order
    dup_ord, is_dup2 = place_broker_order(
        strategy=strat,
        execution_settings=base_exec,
        side="BUY",
        idempotency_key="idem-key-001",
        existing_orders=existing_orders,
        gate_status=g_unlocked,
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        reference_price=24055.0,
        eval_time="2026-01-06T10:31:00+05:30",
        live_execution_enabled=True,
        live_broker_adapter=_mock_live_adapter,
    )
    _check("p7 duplicate idempotencyKey blocked", is_dup2 is True and dup_ord.get("duplicateBlocked") is True)
    _check("p7 duplicate returns original order id", dup_ord["id"] == ord1["id"])

    # 3. Place PAPER LIMIT order inside market hours -> starts OPEN
    limit_exec = copy.deepcopy(base_exec)
    limit_exec["mode"] = "PAPER"
    limit_exec["orderType"] = "LIMIT"
    limit_exec["limitPrice"] = 23990.0
    ord_limit, _ = place_broker_order(
        strategy=strat,
        execution_settings=limit_exec,
        side="BUY",
        idempotency_key="idem-key-002",
        existing_orders=existing_orders,
        gate_status=g_unlocked,
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        reference_price=24050.0,
        eval_time="2026-01-06T10:35:00+05:30",
    )
    existing_orders.append(ord_limit)
    _check("p7 LIMIT order starts OPEN", ord_limit["status"] == "OPEN")

    # 4. Place non-AMO PAPER order outside market hours (18:00 IST) -> REJECTED
    g_after_hours = evaluate_go_live_gates(
        strategy=strat,
        backtest_runs=[bt],
        paper_worker_runs=[pw],
        live_approvals=[approval_rec],
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        eval_time="2026-01-06T18:00:00+05:30",
    )
    paper_exec = copy.deepcopy(base_exec)
    paper_exec["mode"] = "PAPER"
    ord_after_hours, _ = place_broker_order(
        strategy=strat,
        execution_settings=paper_exec,
        side="BUY",
        idempotency_key="idem-key-003",
        existing_orders=existing_orders,
        gate_status=g_after_hours,
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        reference_price=24050.0,
        eval_time="2026-01-06T18:00:00+05:30",
    )
    _check("p7 non-AMO order rejected outside market hours", ord_after_hours["status"] == "REJECTED")
    _check("p7 rejection reason mentions Market closed", "Market closed" in (ord_after_hours.get("rejectionReason") or ""))

    # 5. Place AMO order outside market hours (18:00 IST) -> OPEN (queued AMO)
    amo_exec = copy.deepcopy(base_exec)
    amo_exec["amo"] = True
    amo_exec["mode"] = "PAPER"
    ord_amo, _ = place_broker_order(
        strategy=strat,
        execution_settings=amo_exec,
        side="BUY",
        idempotency_key="idem-key-004",
        existing_orders=existing_orders,
        gate_status=g_after_hours,
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        reference_price=24050.0,
        eval_time="2026-01-06T18:00:00+05:30",
    )
    _check("p7 AMO order accepted as OPEN outside market hours", ord_amo["status"] == "OPEN")

    # 6. Order blocked when Kill Switch is active
    ord_ks, _ = place_broker_order(
        strategy=strat,
        execution_settings=amo_exec,
        side="BUY",
        idempotency_key="idem-key-005",
        existing_orders=existing_orders,
        gate_status=g_unlocked,
        kill_switch_state={"active": True, "reason": "Emergency test"},
        iifl_status={"configured": True, "sessionPresent": True},
        reference_price=24050.0,
        eval_time="2026-01-06T10:30:00+05:30",
    )
    _check("p7 order rejected when kill switch is active", ord_ks["status"] == "REJECTED")
    _check("p7 kill switch rejection reason explicit", "Kill switch is active" in (ord_ks.get("rejectionReason") or ""))


def test_fastapi_phase7_endpoints_and_kill_switch() -> None:
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except Exception as exc:
        print(f"skipping FastAPI TestClient checks ({exc})")
        return

    client = TestClient(app)

    # Reset kill switch first
    r_reset = client.post("/api/broker/kill-switch/reset")
    _check("p7 api kill-switch reset 200", r_reset.status_code == 200)

    # 1. Capabilities endpoint
    r_caps = client.get("/api/broker/capabilities")
    _check("p7 api /broker/capabilities 200", r_caps.status_code == 200)
    caps_json = r_caps.json()
    _check("p7 api capabilities has brokers", "IIFL" in caps_json.get("brokers", []))
    _check("p7 api liveExecutionEnabled False by default", caps_json.get("liveExecutionEnabled") is False)

    # 2. Create strategy, run backtest, start & step paper worker so it records >=1 trade
    from uuid import uuid4
    strat = _strategy_with_triggered_entry()
    strat["id"] = f"p7-strat-{uuid4().hex[:8]}"
    strat["meta"]["id"] = strat["id"]
    candles = _sample_candles_for_day("2026-01-06")
    levels = _sample_levels()

    r_bt = client.post("/api/backtest/run", json={
        "strategy": strat,
        "candlesByTimeframe": candles,
        "primaryTimeframe": "5m",
        "manualLevelsByDay": {"2026-01-06": levels},
        "persistResult": True,
    })
    _check("p7 api backtest run 200", r_bt.status_code == 200)

    r_pw = client.post("/api/paper/worker/start", json={
        "strategy": strat,
        "primaryTimeframe": "5m",
    })
    _check("p7 api paper worker start 200", r_pw.status_code == 200)
    worker_id = r_pw.json()["id"]

    r_step = client.post("/api/paper/worker/step", json={
        "runId": worker_id,
        "candlesByTimeframe": candles,
        "evalTime": "2026-01-06T09:25:00+05:30",
        "manualLevelsOverride": levels,
    })
    _check("p7 api paper worker step 200", r_step.status_code == 200, r_step.text)

    # 3. Approve strategy for live
    r_app = client.post("/api/broker/approve-live", json={
        "strategy": strat,
        "confirmationText": f"ENABLE LIVE TRADING FOR {strat['name']}",
        "evalTime": "2026-01-06T10:30:00+05:30",
    })
    _check("p7 api approve-live 200", r_app.status_code == 200, r_app.text)
    _check("p7 api liveTradingUnlocked still False while live execution disconnected", r_app.json()["gateStatus"]["liveTradingUnlocked"] is False)

    # 3b. Attempt LIVE order -> rejected with 400 "Live execution not connected"
    r_live_block = client.post("/api/broker/orders", json={
        "strategy": strat,
        "side": "BUY",
        "idempotencyKey": f"api-idem-live-block-{uuid4().hex[:8]}",
        "evalTime": "2026-01-06T10:30:00+05:30",
        "referencePrice": 24050.0,
        "executionSettings": {
            "broker": "IIFL",
            "exchange": "NFO",
            "symbol": "NIFTY 50",
            "instrument": "INDEX_OPTIONS",
            "expiry": "CURRENT_WEEK",
            "strike": "ATM",
            "optionType": "CE",
            "quantity": 3,
            "lotSize": 25,
            "product": "INTRADAY",
            "orderType": "MARKET",
            "validity": "DAY",
            "amo": False,
            "mode": "LIVE",
        },
    })
    _check("p7 api LIVE order blocked 400", r_live_block.status_code == 400, r_live_block.text)
    _check("p7 api LIVE order blocked message", r_live_block.json().get("detail") == "Live execution not connected", r_live_block.text)

    # 4. Place an OPEN limit order (PAPER mode)
    r_ord = client.post("/api/broker/orders", json={
        "strategy": strat,
        "side": "BUY",
        "idempotencyKey": "api-idem-limit-1",
        "evalTime": "2026-01-06T10:30:00+05:30",
        "referencePrice": 24050.0,
        "executionSettings": {
            "broker": "IIFL",
            "exchange": "NFO",
            "symbol": "NIFTY 50",
            "instrument": "INDEX_OPTIONS",
            "expiry": "CURRENT_WEEK",
            "strike": "ATM",
            "optionType": "CE",
            "quantity": 3,
            "lotSize": 25,
            "product": "INTRADAY",
            "orderType": "LIMIT",
            "validity": "DAY",
            "amo": False,
            "mode": "PAPER",
            "limitPrice": 23990.0,
        },
    })
    _check("p7 api place limit order 200", r_ord.status_code == 200, r_ord.text)
    ord_data = r_ord.json()
    _check("p7 api limit order is OPEN", ord_data["status"] == "OPEN")

    # 5. Trigger EMERGENCY KILL SWITCH -> stops worker, cancels open order, blocks new orders
    r_ks = client.post("/api/broker/kill-switch", json={"reason": "Integration test emergency stop"})
    _check("p7 api trigger kill switch 200", r_ks.status_code == 200, r_ks.text)
    ks_data = r_ks.json()
    _check("p7 api kill switch active True", ks_data["killSwitch"]["active"] is True)
    _check("p7 api kill switch stopped paper worker", ks_data["stoppedPaperWorkers"] >= 1)
    _check("p7 api kill switch cancelled open order", ks_data["cancelledOrders"] >= 1)

    # Verify worker is now STOPPED
    r_pw_list = client.get(f"/api/paper/worker/runs?strategyId={strat['id']}")
    matched_pw = next(r for r in r_pw_list.json() if r["id"] == worker_id)
    _check("p7 api worker status STOPPED after kill switch", matched_pw["status"] == "STOPPED")

    # Verify new order is blocked while kill switch is active
    r_blocked = client.post("/api/broker/orders", json={
        "strategy": strat,
        "side": "BUY",
        "idempotencyKey": "api-idem-after-ks",
        "evalTime": "2026-01-06T10:35:00+05:30",
        "referencePrice": 24050.0,
        "executionSettings": {
            "broker": "IIFL",
            "exchange": "NFO",
            "symbol": "NIFTY 50",
            "instrument": "INDEX_OPTIONS",
            "expiry": "CURRENT_WEEK",
            "strike": "ATM",
            "optionType": "CE",
            "quantity": 3,
            "lotSize": 25,
            "product": "INTRADAY",
            "orderType": "MARKET",
            "validity": "DAY",
            "amo": False,
            "mode": "PAPER",
        },
    })
    _check("p7 api new order REJECTED while kill switch active", r_blocked.json()["status"] == "REJECTED")

    # Clean up kill switch state
    client.post("/api/broker/kill-switch/reset")


def test_iifl_adapter_mocked_lifecycle() -> None:
    import requests as req_lib
    from app.broker_execution import BrokerSessionExpiredError, IIFLBrokerAdapter

    class FakeResp:
        def __init__(self, status_code: int, payload: dict | list):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class MockHttp:
        def __init__(self, post_fn, get_fn=None, delete_fn=None, put_fn=None):
            self.post_calls = []
            self.put_calls = []
            self.post_fn = post_fn
            self.get_fn = get_fn
            self.delete_fn = delete_fn
            self.put_fn = put_fn

        def post(self, url, json=None, headers=None, timeout=None):
            self.post_calls.append((url, json, headers, timeout))
            return self.post_fn(url, json, headers, timeout)

        def get(self, url, headers=None, timeout=None):
            return self.get_fn(url, headers, timeout)

        def put(self, url, json=None, headers=None, timeout=None):
            self.put_calls.append((url, json, headers, timeout))
            return self.put_fn(url, json, headers, timeout)

        def delete(self, url, headers=None, timeout=None):
            return self.delete_fn(url, headers, timeout)

    strat = _strategy_with_triggered_entry()
    bt = run_backtest(
        strategy=strat,
        candles_by_tf=_sample_candles_for_day("2026-01-06"),
        primary_timeframe="5m",
        manual_levels_by_day={"2026-01-06": _sample_levels()},
    )
    pw = new_paper_worker_run(strat)
    pw["tradesToday"] = 1
    g_pre = evaluate_go_live_gates(strat, [bt], [pw], [], {"active": False}, {"configured": True, "sessionPresent": True}, "2026-01-06T10:00:00+05:30")
    app_rec = approve_strategy_for_live(strat, f"ENABLE LIVE TRADING FOR {strat['name']}", g_pre, eval_time="2026-01-06T10:05:00+05:30")
    g_un = evaluate_go_live_gates(strat, [bt], [pw], [app_rec], {"active": False}, {"configured": True, "sessionPresent": True}, "2026-01-06T10:15:00+05:30", live_execution_enabled=True, live_adapter_connected=True)

    exec_cfg = {
        "broker": "IIFL",
        "exchange": "NFO",
        "symbol": "NIFTY 50",
        "instrument": "INDEX_OPTIONS",
        "instrumentId": "48123",
        "expiry": "CURRENT_WEEK",
        "strike": "ATM",
        "optionType": "CE",
        "quantity": 2,
        "lotSize": 25,
        "product": "INTRADAY",
        "orderType": "MARKET",
        "validity": "DAY",
        "amo": False,
        "mode": "LIVE",
        "maxSlippagePercent": 3.0,
        "expectedPrice": 24050.0,
    }

    # 1. Successful placement + order-lite confirmation -> FILLED with real brokerOrderId & short deterministic orderTag
    long_idem_key = "very-long-strategy-idempotency-key-0123456789-abcdef"
    http_ok = MockHttp(
        post_fn=lambda u, j, h, t: FakeResp(200, {
            "status": "Ok", "message": "Success",
            "result": [{"status": "Success", "message": "Success", "brokerOrderId": "240919000000041"}]
        }),
        get_fn=lambda u, h, t: FakeResp(200, [{
            "brokerOrderId": "240919000000041",
            "exchangeOrderId": "1100000000089930",
            "orderStatus": "complete",
            "filledQuantity": "50",
            "pendingQuantity": "0",
            "cancelledQuantity": "0",
            "averageTradedPrice": "24052.5",
        }]),
        delete_fn=lambda u, h, t: FakeResp(200, {
            "status": "Ok", "message": "Success",
            "result": {"status": "Success", "message": "Success", "brokerOrderId": "240919000000041"}
        }),
        put_fn=lambda u, j, h, t: FakeResp(200, {
            "status": "Ok", "message": "Success",
            "result": {"status": "Success", "message": "Success", "brokerOrderId": "240919000000041"}
        }),
    )
    adapter_ok = IIFLBrokerAdapter("https://api.iiflcapital.com/v1", "tok-123", http_client=http_ok)
    existing: list[dict] = []
    ord_ok, dup = place_broker_order(
        strategy=strat, execution_settings=exec_cfg, side="BUY", idempotency_key=long_idem_key,
        existing_orders=existing, gate_status=g_un, kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True}, reference_price=24050.0,
        eval_time="2026-01-06T10:30:00+05:30", live_execution_enabled=True, live_broker_adapter=adapter_ok,
    )
    existing.append(ord_ok)
    sent_item = http_ok.post_calls[0][1][0]
    _check("p7 iifl adapter sends Bearer header & array payload", http_ok.post_calls[0][2]["Authorization"] == "Bearer tok-123" and isinstance(http_ok.post_calls[0][1], list))
    _check("p7 iifl adapter sends NSEFO & qty=50", sent_item["exchange"] == "NSEFO" and sent_item["quantity"] == "50")
    _check("p7 iifl orderTag deterministic ~19-char alphanumeric & mapped", sent_item["orderTag"].isalnum() and 16 <= len(sent_item["orderTag"]) <= 24 and adapter_ok.tag_to_idempotency.get(sent_item["orderTag"]) == long_idem_key)
    _check("p7 iifl marketProtectionPercent omitted unless explicitly set", "marketProtectionPercent" not in sent_item)
    _check("p7 iifl adapter stores brokerOrderId, filledQty=50 & confirmed fillPrice", ord_ok["status"] == "FILLED" and ord_ok["brokerOrderId"] == "240919000000041" and ord_ok["fillPrice"] == 24052.5 and ord_ok["filledQuantity"] == 50 and not dup)

    # Cancel call check
    cancel_res = adapter_ok.cancel_order("240919000000041")
    _check("p7 iifl adapter cancel_order succeeds", cancel_res["ok"] is True)

    # 2. EC004 ("User blocked") on POST /orders -> REJECTED
    http_ec004 = MockHttp(
        post_fn=lambda u, j, h, t: FakeResp(200, {
            "status": "Ok", "message": "Success",
            "result": [{"status": "EC004", "message": "User blocked"}]
        })
    )
    adapter_ec004 = IIFLBrokerAdapter("https://api.iiflcapital.com/v1", "tok-123", http_client=http_ec004)
    ord_ec004, _ = place_broker_order(
        strategy=strat, execution_settings=exec_cfg, side="BUY", idempotency_key="iifl-ec004-1",
        existing_orders=existing, gate_status=g_un, kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True}, reference_price=24050.0,
        eval_time="2026-01-06T10:31:00+05:30", live_execution_enabled=True, live_broker_adapter=adapter_ec004,
    )
    _check("p7 iifl EC004 User blocked rejection recorded", ord_ec004["status"] == "REJECTED" and "User blocked" in (ord_ec004.get("rejectionReason") or ""))

    # 2b. Margin rejection via GET /orders/lite -> orderStatus "rejected" + rejectionReason "RMS:Margin Exceeds,****duct"
    http_rms = MockHttp(
        post_fn=lambda u, j, h, t: FakeResp(200, {
            "status": "Ok", "message": "Success",
            "result": [{"status": "Success", "message": "Success", "brokerOrderId": "240807000000068"}]
        }),
        get_fn=lambda u, h, t: FakeResp(200, [{
            "brokerOrderId": "240807000000068",
            "orderStatus": "rejected",
            "rejectionReason": "RMS:Margin Exceeds,****duct",
            "filledQuantity": "0",
            "pendingQuantity": "50",
        }]),
    )
    adapter_rms = IIFLBrokerAdapter("https://api.iiflcapital.com/v1", "tok-123", http_client=http_rms)
    ord_rms, _ = place_broker_order(
        strategy=strat, execution_settings=exec_cfg, side="BUY", idempotency_key="iifl-rms-1",
        existing_orders=existing, gate_status=g_un, kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True}, reference_price=24050.0,
        eval_time="2026-01-06T10:31:30+05:30", live_execution_enabled=True, live_broker_adapter=adapter_rms,
    )
    _check("p7 iifl RMS margin rejection detected on sync", ord_rms["status"] == "REJECTED" and "RMS:Margin Exceeds" in (ord_rms.get("rejectionReason") or ""))

    # 3a. Timeout reconcile NOT FOUND in GET /orders -> TIMEOUT_UNCONFIRMED (never REJECTED/FILLED) + retry blocked
    def _raise_timeout(u, j, h, t):
        raise req_lib.Timeout("read timed out")
    http_to_missing = MockHttp(
        post_fn=_raise_timeout,
        get_fn=lambda u, h, t: FakeResp(200, []),
    )
    adapter_to_missing = IIFLBrokerAdapter("https://api.iiflcapital.com/v1", "tok-123", http_client=http_to_missing)
    ord_to, _ = place_broker_order(
        strategy=strat, execution_settings=exec_cfg, side="BUY", idempotency_key="iifl-to-1",
        existing_orders=existing, gate_status=g_un, kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True}, reference_price=24050.0,
        eval_time="2026-01-06T10:32:00+05:30", live_execution_enabled=True, live_broker_adapter=adapter_to_missing,
    )
    existing.append(ord_to)
    _check("p7 iifl adapter timeout unconfirmed marked TIMEOUT_UNCONFIRMED", ord_to["status"] == "TIMEOUT_UNCONFIRMED" and ord_to.get("timedOut") is True)
    dup_to, is_dup_to = place_broker_order(
        strategy=strat, execution_settings=exec_cfg, side="BUY", idempotency_key="iifl-to-1",
        existing_orders=existing, gate_status=g_un, kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True}, reference_price=24050.0,
        eval_time="2026-01-06T10:32:05+05:30", live_execution_enabled=True, live_broker_adapter=adapter_to_missing,
    )
    _check("p7 iifl duplicate after timeout blocked", is_dup_to is True and dup_to["id"] == ord_to["id"])

    # Manual resolve path through /api/broker/orders/sync (with active token & GET /orders returning empty)
    from fastapi.testclient import TestClient
    import app.main as main_mod
    old_orders = main_mod.broker_order_repo.list()
    old_tok = getattr(main_mod.app.state, "iifl_token", None)
    old_http = main_mod.IIFL_HTTP_CLIENT
    try:
        main_mod.app.state.iifl_token = "tok-123"
        main_mod.IIFL_HTTP_CLIENT = http_to_missing
        main_mod.broker_order_repo.save_all([ord_to])
        c_sync = TestClient(main_mod.app)
        r_res = c_sync.post("/api/broker/orders/sync", json={
            "orderId": ord_to["id"], "action": "resolve", "resolutionStatus": "CANCELLED", "reason": "Confirmed absent in IIFL"
        })
        _check("p7 manual resolve TIMEOUT_UNCONFIRMED via sync endpoint", r_res.status_code == 200 and r_res.json()["resolved"]["status"] == "CANCELLED")
    finally:
        main_mod.IIFL_HTTP_CLIENT = old_http
        main_mod.app.state.iifl_token = old_tok
        main_mod.broker_order_repo.save_all(old_orders)

    # 3b. Timeout reconcile FOUND in GET /orders by orderTag -> recovers real brokerOrderId & syncs status
    from app.broker_execution import make_order_tag
    found_tag = make_order_tag(strat["id"], "iifl-to-found-1")
    http_to_found = MockHttp(
        post_fn=_raise_timeout,
        get_fn=lambda u, h, t: FakeResp(200, [{
            "brokerOrderId": "240920000000888",
            "exchangeOrderId": "1100000000089999",
            "orderTag": found_tag,
            "orderStatus": "Complete",
            "filledQuantity": "50",
            "pendingQuantity": "0",
            "averageTradedPrice": "24049.0",
        }]),
    )
    adapter_to_found = IIFLBrokerAdapter("https://api.iiflcapital.com/v1", "tok-123", http_client=http_to_found)
    ord_to_found, _ = place_broker_order(
        strategy=strat, execution_settings=exec_cfg, side="BUY", idempotency_key="iifl-to-found-1",
        existing_orders=existing, gate_status=g_un, kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True}, reference_price=24050.0,
        eval_time="2026-01-06T10:32:10+05:30", live_execution_enabled=True, live_broker_adapter=adapter_to_found,
    )
    _check("p7 iifl timeout reconcile recovered brokerOrderId & status", ord_to_found["brokerOrderId"] == "240920000000888" and ord_to_found["status"] == "FILLED" and ord_to_found.get("reconciledFromOrderBook") is True)

    # 3c. Partial fill & strict status rules ("complete" + pending==0 vs assumed aliases)
    http_partial = MockHttp(
        post_fn=lambda u, j, h, t: FakeResp(200, {
            "status": "Ok", "message": "Success",
            "result": [{"status": "Success", "message": "Success", "brokerOrderId": "240920000000777"}]
        }),
        get_fn=lambda u, h, t: FakeResp(200, [{
            "brokerOrderId": "240920000000777",
            "orderStatus": "open",
            "filledQuantity": "25",
            "pendingQuantity": "25",
            "cancelledQuantity": "0",
            "averageTradedPrice": "24051.0",
        }]),
    )
    adapter_partial = IIFLBrokerAdapter("https://api.iiflcapital.com/v1", "tok-123", http_client=http_partial)
    ord_part, _ = place_broker_order(
        strategy=strat, execution_settings=exec_cfg, side="BUY", idempotency_key="iifl-part-1",
        existing_orders=existing, gate_status=g_un, kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True}, reference_price=24050.0,
        eval_time="2026-01-06T10:32:20+05:30", live_execution_enabled=True, live_broker_adapter=adapter_partial,
    )
    _check("p7 iifl partial fill status & qty tracked", ord_part["status"] == "PARTIALLY_FILLED" and ord_part["filledQuantity"] == 25 and ord_part["pendingQuantity"] == 25 and ord_part["fillPrice"] == 24051.0)
    # Assumed alias "filled" must NOT become FILLED
    parsed_non_doc = IIFLBrokerAdapter._parse_iifl_order_record({"orderStatus": "filled", "filledQuantity": "0", "pendingQuantity": "0"})
    _check("p7 iifl unverified alias 'filled' not marked FILLED", parsed_non_doc["status"] == "OPEN")

    # 3d. Multi-item result[] validation -> fails if item[1] fails & reports per-item results
    http_multi_fail = MockHttp(
        post_fn=lambda u, j, h, t: FakeResp(200, {
            "status": "Ok", "message": "Partial",
            "result": [
                {"status": "Success", "message": "Success", "brokerOrderId": "240920000000111"},
                {"status": "EC004", "message": "User blocked"},
            ]
        })
    )
    adapter_multi = IIFLBrokerAdapter("https://api.iiflcapital.com/v1", "tok-123", http_client=http_multi_fail)
    res_multi = adapter_multi.place_order(exec_cfg, "BUY", "iifl-multi-1")
    _check("p7 iifl multi-item result[] checks all items", res_multi["status"] == "REJECTED" and len(res_multi.get("itemResults") or []) == 2 and res_multi["itemResults"][1]["status"] == "EC004")

    # 3e. get_order_history (datetime parsing, NOT string sort), get_trade_book, and modify_order (25-limit)
    http_hist = MockHttp(
        post_fn=lambda u, j, h, t: FakeResp(200, {}),
        get_fn=lambda u, h, t: FakeResp(200, [
            {"brokerOrderId": "240919000000041", "orderStatus": "open", "filledQuantity": "0", "pendingQuantity": "50", "brokerUpdateTime": "17-Feb-2025 06:48:39"},
            {"brokerOrderId": "240919000000041", "orderStatus": "complete", "quantity": "50", "filledQuantity": "50", "pendingQuantity": "0", "averageTradedPrice": "24060.0", "brokerUpdateTime": "02-Mar-2025 06:48:30"},
        ]) if "/orders/" in u else FakeResp(200, [{"brokerOrderId": "240919000000041", "tradedPrice": "24060.0", "filledQuantity": "50"}]),
        put_fn=lambda u, j, h, t: FakeResp(200, {"status": "Ok", "message": "Success", "result": {"status": "Success", "brokerOrderId": "240919000000041"}}),
    )
    adapter_hist = IIFLBrokerAdapter("https://api.iiflcapital.com/v1", "tok-123", http_client=http_hist)
    hist_latest = adapter_hist.sync_from_history("240919000000041")
    trades_bk = adapter_hist.get_trade_book()
    mod_ok = adapter_hist.modify_order("240919000000041", {"quantity": "25"})
    mod_cap = adapter_hist.modify_order("240919000000041", {"quantity": "25"}, current_modify_count=25)
    _check("p7 iifl get_order_history selects latest by brokerUpdateTime", hist_latest and hist_latest["status"] == "FILLED" and hist_latest["fillPrice"] == 24060.0)
    _check("p7 iifl get_trade_book parses array", len(trades_bk) == 1 and trades_bk[0]["tradedPrice"] == "24060.0")
    _check("p7 iifl modify_order succeeds & enforces 25-modify limit", mod_ok["ok"] is True and mod_cap["ok"] is False and "25" in mod_cap["error"])

    # 4. Price-band / max-slippage rejection -> blocked before HTTP call (and default 1.5% checked when omitted)
    bad_slip = copy.deepcopy(exec_cfg)
    bad_slip.pop("maxSlippagePercent", None)
    bad_slip["expectedPrice"] = 23500.0  # ~2.34% deviation > default 1.5%
    ord_slip, _ = place_broker_order(
        strategy=strat, execution_settings=bad_slip, side="BUY", idempotency_key="iifl-slip-1",
        existing_orders=existing, gate_status=g_un, kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True}, reference_price=24050.0,
        eval_time="2026-01-06T10:33:00+05:30", live_execution_enabled=True, live_broker_adapter=adapter_ok,
    )
    _check("p7 iifl default 1.5% slippage violation rejected before broker call", ord_slip["status"] == "REJECTED" and "1.50%" in (ord_slip.get("rejectionReason") or ""))

    # 5. Missing instrumentId refused clearly
    no_inst = copy.deepcopy(exec_cfg)
    no_inst.pop("instrumentId", None)
    res_no_inst = adapter_ok.place_order(no_inst, "BUY", "iifl-no-inst-1")
    _check("p7 iifl missing instrumentId rejected cleanly", res_no_inst["status"] == "REJECTED" and "instrumentId is required" in res_no_inst["rejectionReason"])

    # 6. 401 unauthorized raises BrokerSessionExpiredError
    http_401 = MockHttp(post_fn=lambda u, j, h, t: FakeResp(401, {"status": "Error", "message": "Unauthorized"}))
    adapter_401 = IIFLBrokerAdapter("https://api.iiflcapital.com/v1", "expired", http_client=http_401)
    raised_401 = False
    try:
        adapter_401.place_order(exec_cfg, "BUY", "iifl-401-1")
    except BrokerSessionExpiredError:
        raised_401 = True
    _check("p7 iifl 401 raises BrokerSessionExpiredError", raised_401 is True)


def test_iifl_step_a_hardening() -> None:
    from fastapi.testclient import TestClient
    import app.main as main_mod
    from app.broker_execution import IIFLBrokerAdapter, make_order_tag

    # 1. Status parsing edge cases (Item 3)
    # 1a. Cancelled with filledQuantity > 0 retains filledQuantity and averageTradedPrice
    canc_partial = IIFLBrokerAdapter._parse_iifl_order_record({
        "brokerOrderId": "B1",
        "orderStatus": "cancelled",
        "quantity": "50",
        "filledQuantity": "20",
        "pendingQuantity": "30",
        "cancelledQuantity": "30",
        "averageTradedPrice": "24010.5",
    })
    _check(
        "step_a cancelled order keeps filledQuantity>0 & avg price",
        canc_partial["status"] == "CANCELLED"
        and canc_partial["filledQuantity"] == 20
        and canc_partial["averageTradedPrice"] == 24010.5
        and canc_partial["fillPrice"] == 24010.5,
        str(canc_partial),
    )

    # 1b. Cancelled with pendingQuantity=1 (docs example) and filledQuantity=0 stays CANCELLED
    canc_doc = IIFLBrokerAdapter._parse_iifl_order_record({
        "brokerOrderId": "B2",
        "orderStatus": "cancelled",
        "quantity": "1",
        "filledQuantity": "0",
        "pendingQuantity": "1",
    })
    _check("step_a docs cancelled example pendingQuantity=1 stays CANCELLED", canc_doc["status"] == "CANCELLED")

    # 1c. "complete" with non-zero pendingQuantity OR filledQuantity < quantity must NOT become FILLED
    comp_pending = IIFLBrokerAdapter._parse_iifl_order_record({
        "brokerOrderId": "B3",
        "orderStatus": "complete",
        "quantity": "50",
        "filledQuantity": "25",
        "pendingQuantity": "25",
        "averageTradedPrice": "24020.0",
    })
    _check("step_a complete with pendingQuantity>0 is PARTIALLY_FILLED, not FILLED", comp_pending["status"] == "PARTIALLY_FILLED", str(comp_pending))

    comp_mismatch_qty = IIFLBrokerAdapter._parse_iifl_order_record({
        "brokerOrderId": "B4",
        "orderStatus": "complete",
        "quantity": "50",
        "filledQuantity": "30",
        "pendingQuantity": "0",
        "averageTradedPrice": "24020.0",
    })
    _check("step_a complete with filledQuantity<quantity is NOT FILLED", comp_mismatch_qty["status"] == "PARTIALLY_FILLED", str(comp_mismatch_qty))

    comp_exact = IIFLBrokerAdapter._parse_iifl_order_record({
        "brokerOrderId": "B5",
        "orderStatus": "complete",
        "quantity": "50",
        "filledQuantity": "50",
        "pendingQuantity": "0",
        "averageTradedPrice": "24022.0",
    })
    _check("step_a complete with filledQuantity==quantity & pending==0 is FILLED", comp_exact["status"] == "FILLED" and comp_exact["fillPrice"] == 24022.0)

    # 2. Manual resolve guards, Kill switch reconciliation, and App Restart orderTag persistence (Items 1, 2, 5)
    class FakeResp:
        def __init__(self, status_code: int, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class MockSyncHttp:
        def __init__(self, order_book_rows, fail_get=False):
            self.order_book_rows = order_book_rows
            self.fail_get = fail_get
            self.deleted_urls = []

        def get(self, url, headers=None, timeout=None):
            if self.fail_get:
                raise ConnectionError("network down")
            if url.endswith("/orders"):
                return FakeResp(200, self.order_book_rows)
            return FakeResp(200, [])

        def delete(self, url, headers=None, timeout=None):
            self.deleted_urls.append(url)
            oid = url.rsplit("/", 1)[-1]
            return FakeResp(200, {"status": "Ok", "message": "Success", "result": {"status": "Success", "brokerOrderId": oid}})

    old_orders = main_mod.broker_order_repo.list()
    old_http = main_mod.IIFL_HTTP_CLIENT
    old_token = getattr(main_mod.app.state, "iifl_token", None)
    old_ks = copy.deepcopy(main_mod.kill_switch_state)
    try:
        strat = _strategy_with_triggered_entry()
        tag_found = make_order_tag(strat["id"], "restart-idem-found")
        tag_missing = make_order_tag(strat["id"], "restart-idem-missing")

        unconfirmed_found = {
            "id": "ord-stepa-found",
            "strategyId": strat["id"],
            "idempotencyKey": "restart-idem-found",
            "orderTag": tag_found,
            "mode": "LIVE",
            "status": "TIMEOUT_UNCONFIRMED",
            "timedOut": True,
        }
        unconfirmed_missing = {
            "id": "ord-stepa-missing",
            "strategyId": strat["id"],
            "idempotencyKey": "restart-idem-missing",
            "orderTag": tag_missing,
            "mode": "LIVE",
            "status": "TIMEOUT_UNCONFIRMED",
            "timedOut": True,
        }
        # Persisted on disk (simulating app restart with fresh adapter instance)
        main_mod.broker_order_repo.save_all([copy.deepcopy(unconfirmed_found), copy.deepcopy(unconfirmed_missing)])
        main_mod.app.state.iifl_token = "mock-session-tok"
        mock_http = MockSyncHttp(order_book_rows=[{
            "brokerOrderId": "240920000099901",
            "exchangeOrderId": "110000000099901",
            "orderTag": tag_found,
            "orderStatus": "open",
            "quantity": "50",
            "filledQuantity": "0",
            "pendingQuantity": "50",
        }])
        main_mod.IIFL_HTTP_CLIENT = mock_http
        client = TestClient(main_mod.app)

        # 2a. Never allow manual resolution to FILLED
        r_bad_fill = client.post("/api/broker/orders/sync", json={
            "orderId": "ord-stepa-missing", "action": "resolve", "resolutionStatus": "FILLED"
        })
        _check("step_a manual resolve refuses FILLED", r_bad_fill.status_code == 400)

        # 2b. Manual resolve refuses CANCELLED if GET /orders finds orderTag match, and syncs real status instead!
        r_refuse = client.post("/api/broker/orders/sync", json={
            "orderId": "ord-stepa-found", "action": "resolve", "resolutionStatus": "CANCELLED"
        })
        _check(
            "step_a manual resolve refuses when found in GET /orders & syncs OPEN + brokerOrderId",
            r_refuse.status_code == 409
            and r_refuse.json()["order"]["status"] == "OPEN"
            and r_refuse.json()["order"]["brokerOrderId"] == "240920000099901",
            r_refuse.text,
        )

        # 2c. Restart simulation: reset ord-stepa-found back to TIMEOUT_UNCONFIRMED, trigger Kill Switch:
        # Must reconcile ord-stepa-found -> cancel via DELETE /orders/240920000099901,
        # and report ord-stepa-missing in unresolvedOrders!
        main_mod.broker_order_repo.save_all([copy.deepcopy(unconfirmed_found), copy.deepcopy(unconfirmed_missing)])
        r_ks = client.post("/api/broker/kill-switch", json={"reason": "Step A emergency check"})
        ks_json = r_ks.json()
        _check(
            "step_a kill switch reconciles found TIMEOUT_UNCONFIRMED, cancels it, and reports unresolvedOrders",
            r_ks.status_code == 200
            and ks_json["cancelledOrders"] == 1
            and "ord-stepa-missing" in ks_json["unresolvedOrders"]
            and any("240920000099901" in u for u in mock_http.deleted_urls),
            str(ks_json),
        )
    finally:
        main_mod.broker_order_repo.save_all(old_orders)
        main_mod.IIFL_HTTP_CLIENT = old_http
        main_mod.app.state.iifl_token = old_token
        main_mod.kill_switch_state.clear()
        main_mod.kill_switch_state.update(old_ks)


def test_phase3_reliability_and_security() -> None:
    from fastapi.testclient import TestClient
    import app.main as main_mod
    import app.market_service as ms_mod

    # 1. CORS wildcard removed
    cors_mw = [m for m in main_mod.app.user_middleware if "CORS" in str(m.cls)]
    _check("p3 cors '*' origin removed", cors_mw and "*" not in (cors_mw[0].kwargs.get("allow_origins") or []))

    # 2. Centralized MarketDataService cache & 429 backoff (mocked requests.get)
    svc = ms_mod.MarketDataService(cache_ttl_seconds=10.0)
    calls = {"n": 0, "code": 200}

    class FakeMarketResp:
        def __init__(self, code: int):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests as _r
                raise _r.HTTPError(f"HTTP {self.status_code}")

        def json(self):
            return {
                "chart": {
                    "result": [{
                        "meta": {"regularMarketPrice": 24100.0, "chartPreviousClose": 24000.0},
                        "timestamp": [],
                        "indicators": {"quote": [{}]},
                    }]
                }
            }

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return FakeMarketResp(calls["code"])

    old_req_get = ms_mod.requests.get
    try:
        ms_mod.requests.get = fake_get
        q1 = svc.get_quote("NIFTY 50")
        q2 = svc.get_quote("NIFTY 50")
        _check("p3 shared market cache serves subsequent readers without extra HTTP", calls["n"] == 1 and q1["ltp"] == 24100.0 and q2["ltp"] == 24100.0)

        calls["code"] = 429
        calls["n"] = 0
        q_429 = svc.get_quote("NIFTY 50", force_refresh=True)
        q_bo = svc.get_quote("NIFTY 50")
        _check("p3 429 triggers backoff & falls back to cached quote", calls["n"] == 1 and q_429["ltp"] == 24100.0 and svc._is_backed_off("^NSEI", __import__("time").time()))
        _check("p3 subsequent read during backoff skips HTTP", calls["n"] == 1 and q_bo["ltp"] == 24100.0)
    finally:
        ms_mod.requests.get = old_req_get

    # 3. IIFL 401 clears stale token, marks sessionExpired=True & status reflects it
    class Fake401Http:
        def get(self, url, headers=None, timeout=None):
            class _R:
                status_code = 401
                def raise_for_status(self): pass
                def json(self): return {}
            return _R()

    old_http = main_mod.IIFL_HTTP_CLIENT
    old_tok = getattr(main_mod.app.state, "iifl_token", None)
    old_exp = getattr(main_mod.app.state, "iifl_session_expired", False)
    try:
        main_mod.app.state.iifl_token = "stale-tok"
        main_mod.app.state.iifl_session_expired = False
        main_mod.IIFL_HTTP_CLIENT = Fake401Http()
        c = TestClient(main_mod.app)
        r_prof = c.get("/api/iifl/profile")
        r_st = c.get("/api/iifl/status").json()
        _check(
            "p3 iifl 401 clears token & sets sessionExpired=True",
            r_prof.status_code == 401
            and "Session expired, reconnect" in r_prof.text
            and r_st["sessionPresent"] is False
            and r_st["sessionExpired"] is True,
            str(r_st),
        )
    finally:
        main_mod.IIFL_HTTP_CLIENT = old_http
        main_mod.app.state.iifl_token = old_tok
        main_mod.app.state.iifl_session_expired = old_exp


def test_app_state_token_visibility_regression() -> None:
    from fastapi.testclient import TestClient
    import app.main as main_mod

    class CaptureAuthHttp:
        def __init__(self):
            self.seen_auth = []

        def get(self, url, headers=None, timeout=None):
            self.seen_auth.append(("GET", url, (headers or {}).get("Authorization")))
            class _R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self):
                    if url.endswith("/profile"):
                        return {"status": "Ok", "clientId": "CL1"}
                    if "/orders/lite/" in url:
                        return [{"brokerOrderId": "B-TOK-1", "orderStatus": "open", "quantity": "50", "filledQuantity": "0", "pendingQuantity": "50"}]
                    return []
            return _R()

        def delete(self, url, headers=None, timeout=None):
            self.seen_auth.append(("DELETE", url, (headers or {}).get("Authorization")))
            class _R:
                status_code = 200
                def json(self):
                    return {"status": "Ok", "message": "Success", "result": {"status": "Success", "brokerOrderId": "B-TOK-1"}}
            return _R()

    old_http = main_mod.IIFL_HTTP_CLIENT
    old_tok = getattr(main_mod.app.state, "iifl_token", None)
    old_orders = main_mod.broker_order_repo.list()
    old_ks = copy.deepcopy(main_mod.kill_switch_state)
    try:
        cap = CaptureAuthHttp()
        main_mod.IIFL_HTTP_CLIENT = cap
        main_mod.app.state.iifl_token = "x"
        main_mod.broker_order_repo.save_all([{
            "id": "ord-tok-reg",
            "strategyId": "s1",
            "mode": "LIVE",
            "status": "OPEN",
            "brokerOrderId": "B-TOK-1",
            "orderTag": "a24toktest123",
        }])
        c = TestClient(main_mod.app)
        r_prof = c.get("/api/iifl/profile")
        r_sync = c.post("/api/broker/orders/sync")
        r_ks = c.post("/api/broker/kill-switch", json={"reason": "token visibility check"})
        _check(
            "regression app.state.iifl_token='x' seen by profile, order sync, and kill-switch",
            r_prof.status_code == 200
            and r_sync.status_code == 200
            and r_ks.status_code == 200
            and any(m == "GET" and u.endswith("/profile") and a == "Bearer x" for m, u, a in cap.seen_auth)
            and any(m == "GET" and "/orders/lite/B-TOK-1" in u and a == "Bearer x" for m, u, a in cap.seen_auth)
            and any(m == "DELETE" and "/orders/B-TOK-1" in u and a == "Bearer x" for m, u, a in cap.seen_auth),
            str(cap.seen_auth),
        )
    finally:
        main_mod.IIFL_HTTP_CLIENT = old_http
        main_mod.app.state.iifl_token = old_tok
        main_mod.broker_order_repo.save_all(old_orders)
        main_mod.kill_switch_state.clear()
        main_mod.kill_switch_state.update(old_ks)


def test_iifl_step_b_hardening() -> None:
    """Step B regression checks:
    1. Manual resolve on TIMEOUT_UNCONFIRMED fails closed on no-token, 401, and timeout.
    2. CANCELLED order with filledQuantity > 0 counts in positions and P&L.
    3. Reduced disk-write logic in run_paper_worker_tick_once persists all restart-critical state.
    4. Slippage guard rejects fallback/mock/offline and stale reference quotes.
    5. Missing token / failed broker cancel never falsely marks LIVE orders CANCELLED.
    """
    import copy
    import tempfile
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    import httpx
    from fastapi.testclient import TestClient
    import app.main as main_mod
    from app.broker_execution import (
        check_price_band_slippage,
        compute_broker_positions_and_pnl,
        place_broker_order,
    )
    from app.backtest_engine import new_paper_worker_run
    from app.repos import JsonListRepo

    ist = timezone(timedelta(hours=5, minutes=30))
    strategy = _strategy_with_triggered_entry()
    strategy.setdefault("execution", {}).update({
        "mode": "LIVE",
        "orderType": "MARKET",
        "maxSlippagePercent": 1.0,
    })
    strategy["execution"].setdefault("instrument", {})["underlying"] = "NIFTY 50"

    # 1. Manual resolve fail-closed + 5. Fail-closed cancel / kill-switch
    class _FailResolveHttp:
        def __init__(self, mode: str):
            self.mode = mode  # "401" | "timeout"

        def get(self, url, headers=None, timeout=None):
            if self.mode == "timeout":
                raise httpx.TimeoutException("GET /orders timed out")
            class _R401:
                status_code = 401
                def raise_for_status(self):
                    req = httpx.Request("GET", url)
                    resp = httpx.Response(401, request=req)
                    raise httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp)
                def json(self):
                    return {"status": "Error"}
            return _R401()

        def delete(self, url, headers=None, timeout=None):
            class _R500:
                status_code = 500
                def json(self):
                    return {"status": "Error", "message": "broker delete down"}
            return _R500()

    old_http = main_mod.IIFL_HTTP_CLIENT
    old_tok = getattr(main_mod.app.state, "iifl_token", None)
    old_orders = main_mod.broker_order_repo.list()
    old_ks = copy.deepcopy(main_mod.kill_switch_state)
    old_live_flag = main_mod.settings.live_execution_enabled
    old_adapter_conn = main_mod.LIVE_ADAPTER_CONNECTED

    try:
        base_timeout_order = {
            "id": "ord-stepb-to-1",
            "strategyId": strategy["id"],
            "mode": "LIVE",
            "status": "TIMEOUT_UNCONFIRMED",
            "brokerOrderId": None,
            "orderTag": "a24stepbtag123",
            "idempotencyKey": "idem-stepb-to-1",
            "symbol": "NIFTY 50",
            "side": "BUY",
            "quantity": 1,
            "lotSize": 25,
            "totalUnits": 25,
        }
        client = TestClient(main_mod.app)

        # 1a. No IIFL token -> 401, stays TIMEOUT_UNCONFIRMED
        main_mod.app.state.iifl_token = None
        main_mod.broker_order_repo.save_all([copy.deepcopy(base_timeout_order)])
        r_no_tok = client.post("/api/broker/orders/sync", json={
            "orderId": "ord-stepb-to-1",
            "action": "resolve",
            "resolutionStatus": "CANCELLED",
        })
        stored_after_notok = main_mod.broker_order_repo.list()[0]
        _check(
            "p7 stepB resolve no-token returns 401 and keeps TIMEOUT_UNCONFIRMED",
            r_no_tok.status_code == 401 and stored_after_notok["status"] == "TIMEOUT_UNCONFIRMED",
            f"status={r_no_tok.status_code} body={r_no_tok.text} stored={stored_after_notok}",
        )

        # 1b. GET /orders returns 401 -> 401, session cleared, stays TIMEOUT_UNCONFIRMED
        main_mod.app.state.iifl_token = "tok-expired"
        main_mod.IIFL_HTTP_CLIENT = _FailResolveHttp("401")
        r_401 = client.post("/api/broker/orders/sync", json={
            "orderId": "ord-stepb-to-1",
            "action": "resolve",
            "resolutionStatus": "CANCELLED",
        })
        stored_after_401 = main_mod.broker_order_repo.list()[0]
        _check(
            "p7 stepB resolve 401 fails closed, clears session, keeps TIMEOUT_UNCONFIRMED",
            r_401.status_code == 401
            and getattr(main_mod.app.state, "iifl_token", None) is None
            and stored_after_401["status"] == "TIMEOUT_UNCONFIRMED",
            f"status={r_401.status_code} body={r_401.text}",
        )

        # 1c. GET /orders times out -> 502, stays TIMEOUT_UNCONFIRMED
        main_mod.app.state.iifl_token = "tok-live"
        main_mod.IIFL_HTTP_CLIENT = _FailResolveHttp("timeout")
        r_to = client.post("/api/broker/orders/sync", json={
            "orderId": "ord-stepb-to-1",
            "action": "resolve",
            "resolutionStatus": "REJECTED",
        })
        stored_after_to = main_mod.broker_order_repo.list()[0]
        _check(
            "p7 stepB resolve timeout returns 502 and keeps TIMEOUT_UNCONFIRMED",
            r_to.status_code == 502 and stored_after_to["status"] == "TIMEOUT_UNCONFIRMED",
            f"status={r_to.status_code} body={r_to.text}",
        )

        # Verify blocked idempotency key remains blocked after failed manual resolve
        r_retry = client.post("/api/broker/orders", json={
            "strategy": strategy,
            "executionSettings": strategy["execution"],
            "side": "BUY",
            "idempotencyKey": "idem-stepb-to-1",
            "referencePrice": 24000.0,
        })
        _check(
            "p7 stepB idem key still blocked after failed resolve",
            r_retry.status_code == 200 and r_retry.json().get("duplicateBlocked") is True,
            r_retry.text,
        )

        # 5a. Cancel LIVE order with brokerOrderId when token missing -> 401 fail closed, order remains OPEN
        open_live_ord = {
            "id": "ord-stepb-live-cancel",
            "strategyId": strategy["id"],
            "mode": "LIVE",
            "status": "OPEN",
            "brokerOrderId": "B-LIVE-999",
            "orderTag": "a24cancel999",
        }
        main_mod.app.state.iifl_token = None
        main_mod.broker_order_repo.save_all([copy.deepcopy(open_live_ord)])
        r_cancel_no_tok = client.post("/api/broker/orders/cancel", json={"orderId": "ord-stepb-live-cancel"})
        _check(
            "p7 stepB cancel LIVE order without token fails closed 401",
            r_cancel_no_tok.status_code == 401 and main_mod.broker_order_repo.list()[0]["status"] == "OPEN",
            f"status={r_cancel_no_tok.status_code} body={r_cancel_no_tok.text}",
        )

        # 5b. Kill switch when broker DELETE fails -> stays OPEN (not CANCELLED) & appears in unresolvedOrders
        main_mod.app.state.iifl_token = "tok-live"
        main_mod.IIFL_HTTP_CLIENT = _FailResolveHttp("timeout")
        main_mod.kill_switch_state.clear()
        main_mod.kill_switch_state.update({"active": False, "reason": None, "activatedAtIst": None, "cancelledOrderIds": []})
        r_ks_fail = client.post("/api/broker/kill-switch", json={"reason": "test cancel failure"})
        ks_body = r_ks_fail.json()
        stored_live_after_ks = main_mod.broker_order_repo.list()[0]
        _check(
            "p7 stepB kill-switch keeps failed-cancel LIVE order OPEN and reports unresolvedOrders",
            r_ks_fail.status_code == 200
            and stored_live_after_ks["status"] == "OPEN"
            and bool(stored_live_after_ks.get("brokerCancelError"))
            and "ord-stepb-live-cancel" in (ks_body.get("unresolvedOrders") or []),
            f"ks={ks_body} stored={stored_live_after_ks}",
        )

        # 2. CANCELLED order with filledQuantity > 0 counts in positions and P&L (newest-first order)
        pos_orders = [
            {
                "id": "ord-sell-close-half",
                "symbol": "NIFTY 50",
                "contract": "NIFTY 50 FUT",
                "mode": "LIVE",
                "side": "SELL",
                "status": "FILLED",
                "quantity": 10,
                "filledQuantity": 10,
                "pendingQuantity": 0,
                "averageTradedPrice": 24030.5,
                "fillPrice": 24030.5,
            },
            {
                "id": "ord-partial-cancel-buy",
                "symbol": "NIFTY 50",
                "contract": "NIFTY 50 FUT",
                "mode": "LIVE",
                "side": "BUY",
                "status": "CANCELLED",
                "quantity": 50,
                "filledQuantity": 20,
                "pendingQuantity": 0,
                "cancelledQuantity": 30,
                "averageTradedPrice": 24010.5,
                "fillPrice": None,
            },
        ]
        pos_summary = compute_broker_positions_and_pnl(pos_orders, ltp_by_symbol={"NIFTY 50": 24020.5})
        p_rows = pos_summary["positions"]
        _check(
            "p7 stepB cancelled-with-fill counted in netUnits, avgPrice, realized & unrealized PnL",
            len(p_rows) == 1
            and p_rows[0]["netUnits"] == 10
            and p_rows[0]["filledUnitsTotal"] == 30
            and abs(p_rows[0]["avgEntryPrice"] - 24010.5) < 1e-6
            and abs(pos_summary["totalRealizedPnl"] - 200.0) < 1e-6
            and abs(pos_summary["totalUnrealizedPnl"] - 100.0) < 1e-6,
            str(pos_summary),
        )
        main_mod.broker_order_repo.save_all(pos_orders)
        r_pos = client.get("/api/broker/positions")
        _check(
            "p7 stepB GET /api/broker/positions reflects partially-filled cancelled order",
            r_pos.status_code == 200
            and r_pos.json()["positions"][0]["netUnits"] == 10
            and r_pos.json()["positions"][0]["filledUnitsTotal"] == 30,
            r_pos.text,
        )
    finally:
        main_mod.IIFL_HTTP_CLIENT = old_http
        main_mod.app.state.iifl_token = old_tok
        main_mod.broker_order_repo.save_all(old_orders)
        main_mod.kill_switch_state.clear()
        main_mod.kill_switch_state.update(old_ks)
        main_mod.settings.live_execution_enabled = old_live_flag
        main_mod.LIVE_ADAPTER_CONNECTED = old_adapter_conn

    # 3. Reduced disk-write logic in run_paper_worker_tick_once persists restart-critical fields
    from app.exit_engine import open_managed_position
    old_pw_repo = main_mod.paper_worker_repo
    old_market_svc = main_mod.market_service
    with tempfile.TemporaryDirectory() as tmpdir:
        base_repo = JsonListRepo("paper_worker_runs.json", data_dir=Path(tmpdir))
        save_calls = {"count": 0}

        class _CountingRepo:
            def list(self):
                return base_repo.list()
            def save_all(self, runs):
                save_calls["count"] += 1
                return base_repo.save_all(runs)

        pw_strat = _strategy_with_triggered_entry()
        pw_strat["id"] = "strat-stepb-pw"
        pw_strat["name"] = "Step B Restart Persistence"
        pw_strat["meta"]["id"] = "strat-stepb-pw"
        pw_strat["meta"]["name"] = "Step B Restart Persistence"
        pw_strat["execution"]["instrument"]["underlying"] = "NIFTY 50"
        pw_strat["execution"]["instrument"]["lotSize"] = 25
        pw_strat["execution"]["quantity"] = len(pw_strat["targets"])
        pw_strat["stopLoss"].update({"enabled": True, "method": "fixed_points", "points": 20.0})
        for idx, tgt in enumerate(pw_strat["targets"]):
            tgt.update({"enabled": True, "unit": "points", "value": 100.0 + idx * 50.0, "quantity": 1})
        pw_strat["trailing"] = [
            {
                "id": "tr-stepb-1",
                "order": 1,
                "enabled": True,
                "label": "Move SL to breakeven",
                "condition": {
                    "type": "leaf",
                    "id": "leaf-tr-1",
                    "enabled": True,
                    "order": 1,
                    "label": "Close >= 24010",
                    "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                    "operator": "trades_above",
                    "right": {"kind": "constant", "field": None, "indicatorId": None, "level": None, "value": 24010.0},
                    "timeframe": "5m",
                    "candleRef": "current",
                    "evaluateOn": "CANDLE_CLOSE",
                    "params": {},
                },
                "moveSlTo": {"kind": "price", "price": 24000.0},
            }
        ]
        pw_strat["rules"]["entry"] = [
            {
                "id": "r_never",
                "order": 1,
                "label": "Never enter during test",
                "side": "BUY",
                "enabled": True,
                "condition": {
                    "type": "leaf",
                    "id": "leaf-never-1",
                    "enabled": True,
                    "order": 1,
                    "label": "Close > 99999",
                    "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                    "operator": "trades_above",
                    "right": {"kind": "constant", "field": None, "indicatorId": None, "level": None, "value": 99999.0},
                    "timeframe": "5m",
                    "candleRef": "current",
                    "evaluateOn": "CANDLE_CLOSE",
                    "params": {},
                },
            }
        ]
        pw_strat["rules"]["exit"] = []
        candles = [
            {
                "date": "2026-05-11",
                "time": "09:50",
                "open": 23998.0,
                "high": 24005.0,
                "low": 23995.0,
                "close": 24002.0,
                "volume": 1000,
                "closed": True,
            },
            {
                "date": "2026-05-11",
                "time": "10:00",
                "open": 24002.0,
                "high": 24020.0,
                "low": 24001.0,
                "close": 24015.0,
                "volume": 1200,
                "closed": True,
            },
        ]
        run_init = new_paper_worker_run(pw_strat, primary_timeframe="5m")
        run_init["id"] = "pw-stepb-1"
        run_init["status"] = "RUNNING"
        run_init["currentDay"] = "2026-05-10"  # Triggers day rollover update
        run_init["totalRealizedPnl"] = 1250.5
        run_init["openPosition"] = open_managed_position(
            pw_strat,
            side="BUY",
            entry_price=24000.0,
            entry_time=datetime(2026, 5, 11, 9, 55, tzinfo=ist),
            candles_by_tf={"5m": candles[:1]},
            primary_timeframe="5m",
            lot_size=25,
        )
        run_init["evalState"] = {"lastEvalDay": "2026-05-11", "reentryState": {"reentriesToday": 1, "lastExitTimeIst": "2026-05-11T09:40:00+05:30"}}
        base_repo.save_all([run_init])

        class _FakeMarket:
            def __init__(self):
                self.bars = list(candles)
            def get_chart(self, symbol, range_period="5d", interval="5m"):
                return {"candles": list(self.bars)}

        fake_mkt = _FakeMarket()
        try:
            main_mod.paper_worker_repo = _CountingRepo()
            main_mod.market_service = fake_mkt

            # Tick 1: Day rolled over + trailing step moved SL from 23980 -> 24000 -> MUST save to disk
            main_mod.run_paper_worker_tick_once(now_ist=datetime(2026, 5, 11, 10, 6, tzinfo=ist))
            saves_after_t1 = save_calls["count"]

            # Tick 2: Identical state & bars -> MUST skip disk write
            main_mod.run_paper_worker_tick_once(now_ist=datetime(2026, 5, 11, 10, 6, 30, tzinfo=ist))
            saves_after_t2 = save_calls["count"]

            # Verify reload from disk kept currentDay, totalRealizedPnl, trailing SL (initialStopLoss/currentStopLoss/
            # firedTrailingStepIds/slHistory), unrealizedPnl, and evalState.reentryState.reentriesToday
            reloaded = JsonListRepo("paper_worker_runs.json", data_dir=Path(tmpdir)).list()[0]
            pos_rel = reloaded["openPosition"]
            _check(
                "p7 stepB paper worker persists trailing SL, slHistory, unrealizedPnl, currentDay, totalRealizedPnl & skips idle save",
                saves_after_t1 == 1
                and saves_after_t2 == 1
                and reloaded["currentDay"] == "2026-05-11"
                and abs(reloaded["totalRealizedPnl"] - 1250.5) < 1e-6
                and abs(reloaded["unrealizedPnl"] - 1125.0) < 1e-6
                and (reloaded["evalState"].get("reentryState") or {}).get("reentriesToday") == 1
                and pos_rel["initialStopLoss"] == 23980.0
                and pos_rel["currentStopLoss"] == 24000.0
                and "tr-stepb-1" in (pos_rel.get("firedTrailingStepIds") or [])
                and len(pos_rel.get("slHistory") or []) == 1
                and pos_rel["slHistory"][0]["oldSl"] == 23980.0
                and pos_rel["slHistory"][0]["newSl"] == 24000.0,
                f"saves=({saves_after_t1},{saves_after_t2}) reloaded={reloaded}",
            )

            # Separate Tick 3 case: next closed bar dips to low=23999.0 and hits the persisted trailed SL (24000.0)
            fake_mkt.bars.append({
                "date": "2026-05-11",
                "time": "10:05",
                "open": 24015.0,
                "high": 24016.0,
                "low": 23999.0,
                "close": 24002.0,
                "volume": 900,
                "closed": True,
            })
            main_mod.run_paper_worker_tick_once(now_ist=datetime(2026, 5, 11, 10, 11, tzinfo=ist))
            saves_after_t3 = save_calls["count"]
            reloaded_hit = JsonListRepo("paper_worker_runs.json", data_dir=Path(tmpdir)).list()[0]
            _check(
                "p7 stepD paper worker hits persisted trailed SL 24000 on subsequent bar and saves close",
                saves_after_t3 == 2
                and reloaded_hit["openPosition"] is None
                and len(reloaded_hit.get("closedTrades") or []) == 1
                and reloaded_hit["closedTrades"][0]["exitReason"] == "STOP_LOSS"
                and reloaded_hit["closedTrades"][0]["exitPrice"] == 24000.0
                and reloaded_hit["closedTrades"][0]["finalStopLoss"] == 24000.0,
                f"saves_t3={saves_after_t3} reloaded_hit={reloaded_hit}",
            )
        finally:
            main_mod.paper_worker_repo = old_pw_repo
            main_mod.market_service = old_market_svc

    # 4. Slippage guard rejects fallback/mock and stale quotes, accepts fresh live quote
    now_epoch = 1778000000.0
    live_exec_cfg = {
        "broker": "IIFL",
        "exchange": "NFO",
        "symbol": "NIFTY 50",
        "instrument": "INDEX_OPTIONS",
        "expiry": "CURRENT_WEEK",
        "strike": "ATM",
        "optionType": "CE",
        "quantity": 1,
        "lotSize": 25,
        "product": "INTRADAY",
        "orderType": "MARKET",
        "validity": "DAY",
        "amo": False,
        "mode": "LIVE",
        "maxSlippagePercent": 1.0,
    }
    err_fb = check_price_band_slippage(
        live_exec_cfg,
        24000.0,
        reference_quote={"ltp": 24000.0, "source": "OFFLINE FALLBACK", "updatedAt": now_epoch},
        now_ts=now_epoch,
    )
    err_mock = check_price_band_slippage(
        live_exec_cfg,
        24000.0,
        reference_quote={"ltp": 24000.0, "source": "MOCK", "updatedAt": now_epoch},
        now_ts=now_epoch,
    )
    err_stale = check_price_band_slippage(
        live_exec_cfg,
        24000.0,
        reference_quote={"ltp": 24000.0, "source": "LIVE YAHOO/NSE FEED", "updatedAt": now_epoch - 30.0},
        now_ts=now_epoch,
    )
    ok_fresh = check_price_band_slippage(
        live_exec_cfg,
        24000.0,
        reference_quote={"ltp": 24000.0, "source": "LIVE YAHOO/NSE FEED", "updatedAt": now_epoch - 2.0},
        now_ts=now_epoch,
    )
    # Also verify place_broker_order rejects LIVE order when quote is stale
    gate_ok = {
        "strategyId": strategy["id"],
        "versionHash": compute_strategy_version_hash(strategy),
        "liveTradingUnlocked": True,
        "gates": {"marketHoursOpen": {"passed": True, "detail": "Open"}},
    }
    rejected_stale, _ = place_broker_order(
        strategy=strategy,
        execution_settings=live_exec_cfg,
        side="BUY",
        idempotency_key="idem-stepb-stale-1",
        existing_orders=[],
        gate_status=gate_ok,
        kill_switch_state={"active": False},
        iifl_status={"configured": True, "sessionPresent": True},
        reference_price=24000.0,
        eval_time="2026-05-11T10:15:00+05:30",
        live_execution_enabled=True,
        live_broker_adapter=lambda **kw: {"status": "OPEN", "brokerOrderId": "SHOULD-NOT-RUN"},
        reference_quote={"ltp": 24000.0, "source": "LIVE YAHOO/NSE FEED", "updatedAt": now_epoch - 45.0},
        now_ts=now_epoch,
    )
    _check(
        "p7 stepB slippage guard rejects fallback/mock/stale quotes and accepts fresh live quote",
        bool(err_fb) and bool(err_mock) and bool(err_stale) and ok_fresh is None
        and rejected_stale["status"] == "REJECTED"
        and "stale" in (rejected_stale.get("rejectionReason") or "").lower(),
        f"fb={err_fb} mock={err_mock} stale={err_stale} fresh={ok_fresh} ord={rejected_stale}",
    )


def test_step_d_hardening() -> None:
    """Step D regression checks:
    1. compute_broker_positions_and_pnl: SELL-first short, flip through zero, commodity lot size (+ refusal if missing), flat position.
    2. check_price_band_slippage: rejects quotes with stale regularMarketTime even when local updatedAt is fresh.
    """
    from fastapi.testclient import TestClient
    import app.main as main_mod
    from app.broker_execution import check_price_band_slippage, compute_broker_positions_and_pnl

    # 1a. SELL-first position + partial cover (newest-first order)
    sell_first_orders = [
        {"id": "sf-2", "symbol": "NIFTY 50", "contract": "NIFTY 50 FUT", "side": "BUY", "status": "FILLED", "filledQuantity": 10, "averageTradedPrice": 24050.0},
        {"id": "sf-1", "symbol": "NIFTY 50", "contract": "NIFTY 50 FUT", "side": "SELL", "status": "FILLED", "filledQuantity": 20, "averageTradedPrice": 24100.0},
    ]
    sf_res = compute_broker_positions_and_pnl(sell_first_orders, ltp_by_symbol={"NIFTY 50": 24060.0})
    _check(
        "p7 stepD SELL-first position & partial cover",
        sf_res["positions"][0]["netUnits"] == -10
        and abs(sf_res["positions"][0]["avgEntryPrice"] - 24100.0) < 1e-6
        and abs(sf_res["totalRealizedPnl"] - 500.0) < 1e-6
        and abs(sf_res["totalUnrealizedPnl"] - 400.0) < 1e-6,
        str(sf_res),
    )

    # 1b. Position flipping through zero (BUY 10 @ 24000, then SELL 25 @ 24020 -> short 15 @ 24020)
    flip_orders = [
        {"id": "fl-2", "symbol": "NIFTY 50", "contract": "NIFTY 50 FUT", "side": "SELL", "status": "FILLED", "filledQuantity": 25, "averageTradedPrice": 24020.0},
        {"id": "fl-1", "symbol": "NIFTY 50", "contract": "NIFTY 50 FUT", "side": "BUY", "status": "FILLED", "filledQuantity": 10, "averageTradedPrice": 24000.0},
    ]
    fl_res = compute_broker_positions_and_pnl(flip_orders, ltp_by_symbol={"NIFTY 50": 24010.0})
    _check(
        "p7 stepD position flip through zero updates netUnits, realizedPnl, and avgEntryPrice",
        fl_res["positions"][0]["netUnits"] == -15
        and abs(fl_res["positions"][0]["avgEntryPrice"] - 24020.0) < 1e-6
        and abs(fl_res["totalRealizedPnl"] - 200.0) < 1e-6
        and abs(fl_res["totalUnrealizedPnl"] - 150.0) < 1e-6,
        str(fl_res),
    )

    # 1c. Commodity segment lot multiplier (MCXCOMM: 2 lots * lotSize 100 = 200 units)
    comm_orders = [
        {
            "id": "mcx-1",
            "exchange": "MCXCOMM",
            "symbol": "CRUDEOIL",
            "contract": "CRUDEOIL FUT",
            "side": "BUY",
            "status": "FILLED",
            "quantity": 2,
            "lotSize": 100,
            "filledQuantity": 2,
            "averageTradedPrice": 6000.0,
        }
    ]
    comm_res = compute_broker_positions_and_pnl(comm_orders, ltp_by_symbol={"CRUDEOIL": 6010.0})
    _check(
        "p7 stepD commodity segment applies lotSize multiplier",
        comm_res["positions"][0]["netUnits"] == 200
        and comm_res["positions"][0]["filledUnitsTotal"] == 200
        and abs(comm_res["totalUnrealizedPnl"] - 2000.0) < 1e-6,
        str(comm_res),
    )

    # Commodity order without positive lotSize must be refused (ValueError + 400 HTTP)
    comm_bad = [{"id": "mcx-bad", "exchange": "MCXCOMM", "symbol": "CRUDEOIL", "side": "BUY", "status": "FILLED", "filledQuantity": 2, "lotSize": 0, "averageTradedPrice": 6000.0}]
    refused = False
    try:
        compute_broker_positions_and_pnl(comm_bad)
    except ValueError:
        refused = True
    old_orders = main_mod.broker_order_repo.list()
    try:
        main_mod.broker_order_repo.save_all(comm_bad)
        r_comm_bad = TestClient(main_mod.app).get("/api/broker/positions")
    finally:
        main_mod.broker_order_repo.save_all(old_orders)
    _check(
        "p7 stepD commodity order missing lotSize refused with ValueError & 400",
        refused and r_comm_bad.status_code == 400,
        f"refused={refused} status={r_comm_bad.status_code} body={r_comm_bad.text}",
    )

    # 1d. Flat position (BUY 15 @ 24000, SELL 15 @ 24040 -> netUnits=0, avgEntryPrice=0, unrealizedPnl=0)
    flat_orders = [
        {"id": "flat-2", "symbol": "NIFTY 50", "contract": "NIFTY 50 FUT", "side": "SELL", "status": "FILLED", "filledQuantity": 15, "averageTradedPrice": 24040.0},
        {"id": "flat-1", "symbol": "NIFTY 50", "contract": "NIFTY 50 FUT", "side": "BUY", "status": "FILLED", "filledQuantity": 15, "averageTradedPrice": 24000.0},
    ]
    flat_res = compute_broker_positions_and_pnl(flat_orders, ltp_by_symbol={"NIFTY 50": 24500.0})
    _check(
        "p7 stepD flat position resets avgEntryPrice & unrealizedPnl and retains realizedPnl",
        flat_res["positions"][0]["netUnits"] == 0
        and flat_res["positions"][0]["avgEntryPrice"] == 0.0
        and abs(flat_res["totalRealizedPnl"] - 600.0) < 1e-6
        and flat_res["totalUnrealizedPnl"] == 0.0,
        str(flat_res),
    )

    # 2. Slippage guard rejects stale regularMarketTime even when updatedAt is fresh
    now_epoch = 1778000000.0
    exec_cfg = {"maxSlippagePercent": 1.0}
    err_reg_stale = check_price_band_slippage(
        exec_cfg,
        24000.0,
        reference_quote={
            "ltp": 24000.0,
            "source": "LIVE YAHOO/NSE FEED",
            "updatedAt": now_epoch - 1.0,
            "regularMarketTime": now_epoch - 30.0,
        },
        now_ts=now_epoch,
    )
    ok_reg_fresh = check_price_band_slippage(
        exec_cfg,
        24000.0,
        reference_quote={
            "ltp": 24000.0,
            "source": "LIVE YAHOO/NSE FEED",
            "updatedAt": now_epoch - 1.0,
            "regularMarketTime": now_epoch - 3.0,
        },
        now_ts=now_epoch,
    )
    _check(
        "p7 stepD slippage guard rejects stale regularMarketTime even with fresh updatedAt",
        bool(err_reg_stale) and "regularMarketTime" in err_reg_stale and ok_reg_fresh is None,
        f"err={err_reg_stale} ok={ok_reg_fresh}",
    )


def test_frontend_phase7_ui_wiring() -> None:
    root = Path(__file__).resolve().parent.parent
    panels_src = (root / "frontend" / "src" / "RuleBuilderPanels.jsx").read_text(encoding="utf-8")
    main_src = (root / "frontend" / "src" / "main.jsx").read_text(encoding="utf-8")

    _check("p7 frontend capabilities fetch wired", "/broker/capabilities" in panels_src)
    _check("p7 frontend live-gates fetch wired", "/broker/live-gates" in panels_src)
    _check("p7 frontend approve-live wired", "/broker/approve-live" in panels_src)
    _check("p7 frontend broker orders wired", "/broker/orders" in panels_src)
    _check("p7 frontend kill-switch wired in panels & topbar", "/broker/kill-switch" in panels_src and "/broker/kill-switch" in main_src)


if __name__ == "__main__":
    test_broker_capabilities_and_validation()
    test_version_hash_and_go_live_gates_with_reset_on_edit()
    test_order_lifecycle_idempotency_and_market_hours()
    test_fastapi_phase7_endpoints_and_kill_switch()
    test_iifl_adapter_mocked_lifecycle()
    test_iifl_step_a_hardening()
    test_phase3_reliability_and_security()
    test_app_state_token_visibility_regression()
    test_iifl_step_b_hardening()
    test_step_d_hardening()
    test_frontend_phase7_ui_wiring()
    print("ALL PHASE 7 TESTS PASSED")
