import atexit, json, sys, os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from app.contract_master import contract_master_service
from app.main import app

_CM_SNAPSHOT = (
    dict(contract_master_service._meta),
    dict(contract_master_service._rows_by_segment),
    dict(contract_master_service._by_id),
    dict(contract_master_service._by_spec),
)


def _restore_contract_master():
    contract_master_service._meta.clear()
    contract_master_service._meta.update(_CM_SNAPSHOT[0])
    contract_master_service._rows_by_segment.clear()
    contract_master_service._rows_by_segment.update(_CM_SNAPSHOT[1])
    contract_master_service._by_id.clear()
    contract_master_service._by_id.update(_CM_SNAPSHOT[2])
    contract_master_service._by_spec.clear()
    contract_master_service._by_spec.update(_CM_SNAPSHOT[3])


atexit.register(_restore_contract_master)

_NSEEQ_FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "data" / "fixtures" / "nseeq_sample.json").read_text(encoding="utf-8")
)
contract_master_service.load_rows_into_cache("NSEEQ", _NSEEQ_FIXTURE, persist=False)

client = TestClient(app)

# Test B7: Check preview with Example 1 settings
ex1_strategy = {
    "id": "ex1",
    "name": "Example 1",
    "instrument": "SBIN",
    "strategyType": "LEVEL_SAR",
    "sar": {
        "levelSource": "MANUAL",
        "entryStyle": "REVERSAL",
        "manual": {"upper": 330, "lower": 325},
        "slPoints": 1,
        "tgtPoints": 1.5,
        "baseQuantity": 100,
        "multipliers": [3, 2, 2],
        "maxLegs": 4,
        "startTime": "09:15",
        "endTime": "15:00",
        "afterTarget": "STOP",
        "maxRestarts": 2
    },
    "risk": {"maxDailyLoss": 2000, "maxCapital": 25000}
}

resp = client.post("/api/sar/preview", json={"strategy": ex1_strategy})
print("Preview status:", resp.status_code)
data = resp.json()
sell_ladder = data["ladders"]["SELL"]
entries = [l["entry"] for l in sell_ladder]
qtys = [l["qty"] for l in sell_ladder]
print("SELL ladder entries:", entries)
print("SELL ladder qtys:", qtys)
print("Worst case loss:", data["worstCaseLoss"])
print("Blocked legs:", data["blockedLegs"])
print("Warnings:", data["warnings"])

assert entries == [330, 331, 330, 331], f"Expected entries [330, 331, 330, 331], got {entries}"
assert qtys == [100, 300, 600, 1200], f"Expected qtys [100, 300, 600, 1200], got {qtys}"
assert data["worstCaseLoss"] == 2200, f"Expected worstCaseLoss 2200, got {data['worstCaseLoss']}"
assert data["blockedLegs"] == [4], f"Expected blockedLegs [4], got {data['blockedLegs']}"
print("B7 Preview test PASSED!")

# Test B4: Start SAR run
start_resp = client.post("/api/sar/start", json={"strategy": ex1_strategy})
print("Start status:", start_resp.status_code)
run_data = start_resp.json()
run_id = run_data["id"]
print("Run ID:", run_id)
assert run_data["status"] == "ARMED" or run_data["status"] == "WAITING_START"

# Test duplicate start refused
dup_resp = client.post("/api/sar/start", json={"strategy": ex1_strategy})
assert dup_resp.status_code == 400, "Duplicate start should be 400"
print("Duplicate start rejected with 400: PASSED")

# Test GET /api/sar/runs
runs_resp = client.get("/api/sar/runs")
assert runs_resp.status_code == 200
assert any(r["id"] == run_id for r in runs_resp.json())
print("GET /api/sar/runs: PASSED")

# Test POST /api/sar/stop
stop_resp = client.post("/api/sar/stop", json={"runId": run_id})
assert stop_resp.status_code == 200
assert stop_resp.json()["status"] == "STOPPED_MANUAL"
print("POST /api/sar/stop: PASSED")

# Test close paper trade refusal for SAR trade
import secrets
from app.store import read, write
trades = read("paper_trades.json", [])
fake_trade = {
    "id": "test_sar_trade",
    "sarRunId": run_id,
    "status": "OPEN",
    "symbol": "SBIN",
    "side": "BUY",
    "entryPrice": 330,
    "quantity": 100,
    "lotSize": 1,
    "totalUnits": 100
}
trades.insert(0, fake_trade)
write("paper_trades.json", trades)

close_resp = client.post("/api/paper/close", json={"tradeId": "test_sar_trade"})
assert close_resp.status_code == 400
print("Close SAR trade refused with 400: PASSED")

# Cleanup fake trade and test run
trades = [t for t in read("paper_trades.json", []) if t.get("id") != "test_sar_trade"]
write("paper_trades.json", trades)
runs = [r for r in read("sar_runs.json", []) if r.get("id") != run_id]
write("sar_runs.json", runs)

print("All Phase B tests PASSED cleanly!")

