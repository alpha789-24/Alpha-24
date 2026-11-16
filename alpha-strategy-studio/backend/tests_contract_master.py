"""Offline tests for IIFL Contract Master Service & Instrument Search (Part 2).

Uses ONLY saved fixtures under backend/data/fixtures/ and mocks. Zero network calls.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime
from fastapi.testclient import TestClient

from app.broker_execution import IIFLBrokerAdapter, get_broker_capabilities
from app.contract_master import (
    IST,
    ContractMasterError,
    ContractMasterService,
    contract_master_service,
    normalize_contract_row,
    normalize_expiry_date,
    normalize_option_type,
    normalize_strike,
)
from app.main import app


FIXTURES_DIR = ROOT / "data" / "fixtures"
NSEEQ_FIXTURE = json.loads((FIXTURES_DIR / "nseeq_sample.json").read_text(encoding="utf-8"))
NSEFO_FIXTURE = json.loads((FIXTURES_DIR / "nsefo_sample.json").read_text(encoding="utf-8"))
INDICES_FIXTURE = json.loads((FIXTURES_DIR / "indices_sample.json").read_text(encoding="utf-8"))


class _NoNetworkHttp:
    def get(self, *args, **kwargs):
        raise AssertionError("Unexpected network GET in offline test")

    def post(self, *args, **kwargs):
        raise AssertionError("Unexpected network POST in offline test")

    def put(self, *args, **kwargs):
        raise AssertionError("Unexpected network PUT in offline test")


class _MockBrokerHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        raise AssertionError("Unexpected GET")

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        return _FakeResponse(200, {"status": "Ok", "result": [{"status": "Success", "brokerOrderId": "MOCK123"}]})

    def put(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("PUT", url, json))
        return _FakeResponse(200, {"status": "Ok", "result": [{"status": "Success", "brokerOrderId": "MOCK123"}]})


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _ist_ts(date_str: str) -> float:
    return datetime.strptime(f"{date_str} 10:00", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp()


def _make_service(tmpdir: str, today_ist: str = "2026-11-12") -> ContractMasterService:
    svc = ContractMasterService(
        cache_dir=Path(tmpdir),
        http_client=_NoNetworkHttp(),
        now_fn=lambda: _ist_ts(today_ist),
    )
    svc.load_rows_into_cache("NSEEQ", NSEEQ_FIXTURE, downloaded_date_ist=today_ist, persist=True)
    svc.load_rows_into_cache("NSEFO", NSEFO_FIXTURE, downloaded_date_ist=today_ist, persist=True)
    svc.load_rows_into_cache("INDICES", INDICES_FIXTURE, downloaded_date_ist=today_ist, persist=True)
    return svc


def test_column_and_value_normalization():
    # CSV variant 1: "Formatted Ins Name", missing "Series"
    row_csv_a = {
        "Exchange": "NSEFO",
        "Underlying Instrument Symbol": "NIFTY",
        "Instrument ID": "35036",
        "Instrument Type": "OPTIDX",
        "Option Type": "CE",
        "Strike Price": "17550.00",
        "Formatted Ins Name": "NIFTY 01 DEC 2026 17550 CE",
        "Trading Symbol": "NIFTY26D0117550CE",
        "Expiry": "01-Dec-2026 23:59",
        "Lot Size": "65",
        "Tick Size": "0.05",
    }
    norm_a = normalize_contract_row(row_csv_a, default_segment="NSEFO")
    assert norm_a["formattedInstrumentName"] == "NIFTY 01 DEC 2026 17550 CE"
    assert norm_a["series"] == ""
    assert norm_a["expiry"] == "2026-12-01"
    assert norm_a["strikePrice"] == "17550"
    assert norm_a["lotSize"] == 65

    # CSV variant 2: "Formatted Instrument Name", XX option type -> FUT
    row_csv_b = {
        "Exchange": "NSEFO",
        "Underlying Instrument Symbol": "NIFTY",
        "Instrument ID": "54749",
        "Instrument Type": "FUTIDX",
        "Option Type": "XX",
        "Strike Price": "",
        "Formatted Instrument Name": "NIFTY 25 JAN 2027 FUT",
        "Trading Symbol": "NIFTY27JANFUT",
        "Expiry": "25-Jan-2027 23:59",
        "Lot Size": "65",
        "Tick Size": "0.05",
    }
    norm_b = normalize_contract_row(row_csv_b, default_segment="NSEFO")
    assert norm_b["normalizedOptionType"] == "FUT"
    assert norm_b["expiry"] == "2027-01-25"

    assert normalize_strike("207.50") == "207.5"
    assert normalize_strike(207.500) == "207.5"
    assert normalize_expiry_date("01-Dec-2026 23:59") == "2026-12-01"
    assert normalize_option_type("XX", "FUTIDX") == "FUT"
    print("ok - column and value normalization")


def test_exact_lookups_ce_pe_fut_eq_indices_and_decimal_strike():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = _make_service(tmpdir)

        # Weekly CE vs second weekly expiry CE
        ce_w1 = svc.lookup_contract(
            segment="NSEFO",
            underlying="NIFTY",
            instrument_type="OPTIDX",
            expiry="01-Dec-2026",
            strike="17550",
            option_type="CE",
        )
        assert ce_w1["instrumentId"] == "35036"
        assert ce_w1["lotSize"] == 65

        ce_w2 = svc.lookup_contract(
            segment="NSEFO",
            underlying="NIFTY",
            instrument_type="OPTIDX",
            expiry="2026-12-08",
            strike=30350,
            option_type="CE",
        )
        assert ce_w2["instrumentId"] == "39777"

        # PE lookup
        pe_bn = svc.lookup_contract(
            segment="NSEFO",
            underlying="BANKNIFTY",
            instrument_type="OPTIDX",
            expiry="23-Nov-2026",
            strike="74800",
            option_type="CE",
        )
        assert pe_bn["instrumentId"] == "35008"
        assert pe_bn["lotSize"] == 30

        # Future lookup (XX normalized to FUT)
        fut = svc.lookup_contract(
            segment="NSEFO",
            underlying="NIFTY",
            instrument_type="FUTIDX",
            expiry="25-Jan-2027 23:59",
            option_type="FUT",
        )
        assert fut["instrumentId"] == "54749"
        assert fut["lotSize"] == 65

        # Decimal strike 207.5 matched via 207.50
        bandhan_pe = svc.lookup_contract(
            segment="NSEFO",
            underlying="BANDHANBNK",
            instrument_type="OPTSTK",
            expiry="2026-12-29",
            strike="207.50",
            option_type="PE",
        )
        assert bandhan_pe["instrumentId"] == "35243"
        assert bandhan_pe["lotSize"] == 3600

        # Equity EQ series lookup
        rel_eq = svc.lookup_contract(
            segment="NSEEQ",
            underlying="RELIANCE",
            series="EQ",
        )
        assert rel_eq["instrumentId"] == "2885"
        assert rel_eq["lotSize"] == 1

        # INDICES lookup
        nifty_idx = svc.lookup_contract(segment="INDICES", underlying="NIFTY 50")
        assert nifty_idx["instrumentId"] == "999920000"
        assert nifty_idx["exchange"] == "NSEEQ"
    print("ok - exact lookups CE/PE/FUT/EQ/INDICES & 207.5 strike")


def test_refusals_ambiguous_not_found_unsupported_and_stale():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = _make_service(tmpdir, today_ist="2026-11-12")

        # Ambiguous (multiple expiries/strikes for NIFTY) -> 409
        try:
            svc.lookup_contract(segment="NSEFO", underlying="NIFTY")
            raise AssertionError("Expected ContractMasterError for ambiguous lookup")
        except ContractMasterError as exc:
            assert exc.status_code == 409

        # Not found -> 404
        try:
            svc.lookup_contract(segment="NSEFO", underlying="DOESNOTEXIST")
            raise AssertionError("Expected ContractMasterError for missing symbol")
        except ContractMasterError as exc:
            assert exc.status_code == 404

        # Unsupported segments NCDEXCOMM / BSECOMM -> 400
        for bad_seg in ("NCDEXCOMM", "BSECOMM"):
            try:
                svc.lookup_contract(segment=bad_seg, underlying="GOLD")
                raise AssertionError(f"Expected refusal for {bad_seg}")
            except ContractMasterError as exc:
                assert exc.status_code == 400

        # Stale cache (>1 trading day old) -> 503
        stale_svc = ContractMasterService(
            cache_dir=Path(tmpdir),
            http_client=_NoNetworkHttp(),
            now_fn=lambda: _ist_ts("2026-11-13"),
        )
        try:
            stale_svc.lookup_contract(segment="NSEFO", underlying="NIFTY", instrument_type="FUTIDX", expiry="2027-01-25")
            raise AssertionError("Expected refusal for stale cache")
        except ContractMasterError as exc:
            assert exc.status_code == 503
            assert "stale" in exc.message.lower()

        # Missing cache -> 503
        with tempfile.TemporaryDirectory() as empty_dir:
            missing_svc = ContractMasterService(
                cache_dir=Path(empty_dir),
                http_client=_NoNetworkHttp(),
                now_fn=lambda: _ist_ts("2026-11-12"),
            )
            try:
                missing_svc.get_lot_size(segment="NSEFO", instrument_id="35036")
                raise AssertionError("Expected refusal for missing cache")
            except ContractMasterError as exc:
                assert exc.status_code == 503
    print("ok - refusals (ambiguous, not-found, NCDEXCOMM/BSECOMM, stale, missing)")


def test_ec954_ec990_quantity_multiple_precheck_before_broker_call():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = _make_service(tmpdir)
        mock_http = _MockBrokerHttp()
        adapter = IIFLBrokerAdapter(
            base_url="https://api.iiflcapital.com/v1",
            session_token="mock-session",
            http_client=mock_http,
            sync_after_place=False,
            contract_master=svc,
        )

        # Invalid quantity (70 is not a multiple of NIFTY lotSize 65) -> must refuse with 0 HTTP calls
        bad_res = adapter.place_order(
            {
                "exchange": "NFO",
                "instrument": "INDEX_OPTIONS",
                "instrumentId": "35036",
                "quantity": 70,
                "lotSize": 1,
                "product": "NORMAL",
                "orderType": "MARKET",
                "validity": "DAY",
            },
            side="BUY",
            idempotency_key="IDEMP-BAD-70",
        )
        assert bad_res["status"] == "REJECTED"
        assert "EC954" in bad_res["rejectionReason"] or "EC990" in bad_res["rejectionReason"]
        assert len(mock_http.calls) == 0

        # Non-positive quantity on modify_order -> 0 HTTP calls
        bad_mod = adapter.modify_order("ORD1", {"quantity": 0}, exchange="NSEFO", instrument_id="35036")
        assert bad_mod["ok"] is False
        assert "EC949" in bad_mod["error"]
        assert len(mock_http.calls) == 0

        # Non-multiple quantity on modify_order -> 0 HTTP calls
        bad_mod_mult = adapter.modify_order("ORD1", {"quantity": 70}, exchange="NSEFO", instrument_id="35036")
        assert bad_mod_mult["ok"] is False
        assert "EC954" in bad_mod_mult["error"] or "EC990" in bad_mod_mult["error"]
        assert len(mock_http.calls) == 0

        # Valid multiple (2 lots * 65 = 130) -> passes pre-check and calls mock HTTP
        res = adapter.place_order(
            {
                "exchange": "NFO",
                "instrument": "INDEX_OPTIONS",
                "instrumentId": "35036",
                "quantity": 2,
                "lotSize": 65,
                "product": "NORMAL",
                "orderType": "MARKET",
                "validity": "DAY",
            },
            side="BUY",
            idempotency_key="IDEMP-OK-130",
        )
        assert res["status"] == "OPEN", f"Unexpected place_order result: {res}"
        assert res["brokerOrderId"] == "MOCK123"
        assert len(mock_http.calls) == 1
    print("ok - EC954/EC990 local pre-check before broker call")


def test_rate_limit_two_requests_per_second_per_segment():
    fake_clock = [100.0]
    slept = []

    class _FakeDownloadHttp:
        def get(self, url, timeout=30):
            if url.endswith("NSEEQ.json"):
                return _FakeResponse(200, {"result": NSEEQ_FIXTURE})
            if url.endswith("NSEFO.json"):
                return _FakeResponse(200, {"result": NSEFO_FIXTURE})
            return _FakeResponse(404, {})

    import app.contract_master as cm_mod
    orig_sleep = cm_mod.time.sleep
    try:
        cm_mod.time.sleep = lambda sec: (slept.append(sec), fake_clock.__setitem__(0, fake_clock[0] + sec))
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = ContractMasterService(
                cache_dir=Path(tmpdir),
                http_client=_FakeDownloadHttp(),
                now_fn=lambda: fake_clock[0],
            )
            svc.download_segment("NSEEQ", force=True)
            svc.download_segment("NSEEQ", force=True)
            assert any(abs(s - 0.5) < 1e-6 for s in slept), f"Expected 0.5s rate-limit sleep, got {slept}"
    finally:
        cm_mod.time.sleep = orig_sleep
    print("ok - 2 req/sec per-segment rate limit")


def test_api_search_lot_size_and_capabilities_endpoints():
    with tempfile.TemporaryDirectory() as tmpdir:
        orig_dir = contract_master_service.cache_dir
        orig_today = contract_master_service._today_ist
        try:
            contract_master_service.cache_dir = Path(tmpdir)
            contract_master_service._today_ist = lambda: "2026-11-12"
            contract_master_service.load_rows_into_cache("NSEEQ", NSEEQ_FIXTURE, downloaded_date_ist="2026-11-12", persist=True)
            contract_master_service.load_rows_into_cache("NSEFO", NSEFO_FIXTURE, downloaded_date_ist="2026-11-12", persist=True)
            contract_master_service.load_rows_into_cache("INDICES", INDICES_FIXTURE, downloaded_date_ist="2026-11-12", persist=True)

            client = TestClient(app)

            r_search = client.get("/api/contracts/search?q=RELIANCE")
            assert r_search.status_code == 200
            data_s = r_search.json()
            assert data_s["available"] is True
            assert len(data_s["items"]) > 0
            assert any(item["instrumentId"] == "2885" for item in data_s["items"])

            r_lot = client.get("/api/contracts/lot-size?underlying=NIFTY%2050&segment=NSEFO")
            assert r_lot.status_code == 200
            assert r_lot.json()["lotSize"] == 65

            r_exact_lot = client.get(
                "/api/contracts/lot-size?segment=NSEFO&underlying=BANDHANBNK&instrumentType=OPTSTK&expiry=2026-12-29&strike=207.50&optionType=PE"
            )
            assert r_exact_lot.status_code == 200
            assert r_exact_lot.json()["lotSize"] == 3600

            caps = get_broker_capabilities()
            assert caps["lotSizes"].get("NIFTY") == 65
            assert caps["lotSizes"].get("BANKNIFTY") == 30
        finally:
            contract_master_service._meta.clear()
            contract_master_service._rows_by_segment.clear()
            contract_master_service._by_id.clear()
            contract_master_service._by_spec.clear()
            # Stale date so it cannot reload persisted tmpdir cache
            contract_master_service._today_ist = lambda: "2099-01-01"
            caps_empty = get_broker_capabilities()
            contract_master_service.cache_dir = orig_dir
            contract_master_service._today_ist = orig_today

        # After clearing, capabilities must NOT fall back to hardcoded 25/15
        assert caps_empty["lotSizes"] == {}
    print("ok - /api/contracts/search, /api/contracts/lot-size & /api/broker/capabilities")


def test_weekly_vs_monthly_expiry_pe_and_noneq_series():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = _make_service(tmpdir, today_ist="2026-11-12")
        # NIFTY PE lookup
        nifty_pe = svc.lookup_contract(
            segment="NSEFO",
            underlying="NIFTY",
            instrument_type="OPTIDX",
            expiry="01-Dec-2026",
            strike="17550",
            option_type="PE",
        )
        assert nifty_pe["instrumentId"] == "35037"

        # Weekly (01-Dec, 08-Dec) vs Monthly futures (29-Dec, 25-Jan)
        weekly1 = svc.lookup_contract("NSEFO", "NIFTY", "OPTIDX", "01-Dec-2026", "17550", "CE")
        weekly2 = svc.lookup_contract("NSEFO", "NIFTY", "OPTIDX", "08-Dec-2026", "30350", "CE")
        monthly_fut = svc.lookup_contract("NSEFO", "NIFTY", "FUTIDX", "29-Dec-2026", option_type="XX")
        assert weekly1["instrumentId"] != weekly2["instrumentId"]
        assert monthly_fut["instrumentId"] == "58875"
        assert monthly_fut["normalizedOptionType"] == "FUT"

        # NSEEQ non-EQ series lookup
        non_eq_rows = [r for r in NSEEQ_FIXTURE if (r.get("series") or "").upper() not in ("", "EQ")]
        assert len(non_eq_rows) > 0
        sample_non_eq = non_eq_rows[0]
        rec = svc.lookup_contract(
            segment="NSEEQ",
            underlying=sample_non_eq["underlyingInstrumentSymbol"],
            series=sample_non_eq["series"],
        )
        assert rec["instrumentId"] == str(sample_non_eq["instrumentId"])
    print("ok - NIFTY PE, weekly vs monthly expiry & NSEEQ non-EQ series")


def test_csv_normalization_both_nseeq_and_nsefo():
    nseeq_csv_row = {
        "Exchange": "NSEEQ",
        "Underlying Instrument Symbol": "INFY",
        "Instrument ID": "1594",
        "Instrument Type": "0",
        "Option Type": "",
        "Strike Price": "",
        "Formatted Ins Name": "INFOSYS LIMITED",
        "Trading Symbol": "INFY-EQ",
        "Expiry": "",
        "Lot Size": "1",
        "Tick Size": "0.05",
    }
    norm_eq = normalize_contract_row(nseeq_csv_row, default_segment="NSEEQ")
    assert norm_eq["formattedInstrumentName"] == "INFOSYS LIMITED"
    assert norm_eq["series"] == ""
    assert norm_eq["lotSize"] == 1
    assert norm_eq["instrumentId"] == "1594"
    print("ok - column normalization for both NSEEQ and NSEFO")


def test_production_code_never_loads_fixtures():
    app_dir = ROOT / "app"
    for py_file in app_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "data/fixtures" not in content and "data\\fixtures" not in content, f"{py_file} references fixtures"
        assert "nseeq_sample.json" not in content and "nsefo_sample.json" not in content
    with tempfile.TemporaryDirectory() as empty_dir:
        fresh_svc = ContractMasterService(cache_dir=Path(empty_dir), http_client=_NoNetworkHttp())
        assert fresh_svc.is_fresh("NSEFO") is False
    print("ok - production code never loads fixture files")


def test_missing_or_stale_blocks_sar_paper_backtest_and_sizing():
    from app.backtest_engine import BacktestError, new_paper_worker_run, run_backtest
    from app.exit_engine import ExitEngineError, open_managed_position
    from app.market_service import _resolve_lot_size
    from app.schema_v2 import new_rule_builder

    # Ensure contract_master_service is empty/unloaded and restore afterward
    orig_dir = contract_master_service.cache_dir
    snap_meta = dict(contract_master_service._meta)
    snap_rows = dict(contract_master_service._rows_by_segment)
    snap_id = dict(contract_master_service._by_id)
    snap_spec = dict(contract_master_service._by_spec)
    try:
        with tempfile.TemporaryDirectory() as empty_dir:
            contract_master_service._meta.clear()
            contract_master_service._rows_by_segment.clear()
            contract_master_service._by_id.clear()
            contract_master_service._by_spec.clear()
            contract_master_service.cache_dir = Path(empty_dir)

            assert _resolve_lot_size("NIFTY 50") is None
            caps = get_broker_capabilities()
            assert caps["lotSizes"] == {}
            assert caps["lotSizeSource"] == "UNAVAILABLE"

            strat = new_rule_builder()
            strat["name"] = "No Lot Test"
            strat["execution"].setdefault("instrument", {})["underlying"] = "NIFTY 50"
            assert strat["execution"]["instrument"].get("lotSize") is None

            try:
                new_paper_worker_run(strat)
                raise AssertionError("Expected BacktestError when starting paper worker without lot size")
            except BacktestError:
                pass

            try:
                run_backtest(strat, {"5m": []})
                raise AssertionError("Expected BacktestError when running backtest without lot size")
            except BacktestError:
                pass

            try:
                open_managed_position(strat, "BUY", 24000.0, "2026-11-12T09:20:00+05:30", {"5m": []})
                raise AssertionError("Expected ExitEngineError when sizing position without lot size")
            except ExitEngineError:
                pass

            client = TestClient(app)
            sar_res = client.post("/api/sar/start", json={
                "strategy": {
                    "id": "sar-no-lot",
                    "name": "SAR No Lot",
                    "instrument": "NIFTY 50",
                    "strategyType": "LEVEL_SAR",
                    "sar": {
                        "levelSource": "MANUAL",
                        "entryStyle": "REVERSAL",
                        "manual": {"upper": 24100, "lower": 24000},
                        "slPoints": 20,
                        "tgtPoints": 40,
                        "baseQuantity": 1,
                        "multipliers": [2],
                        "maxLegs": 2,
                        "startTime": "09:15",
                        "endTime": "15:00",
                        "afterTarget": "STOP",
                        "maxRestarts": 1,
                    },
                    "risk": {"maxDailyLoss": 2000, "maxCapital": 25000},
                }
            })
            assert sar_res.status_code == 400
    finally:
        contract_master_service._meta.clear()
        contract_master_service._meta.update(snap_meta)
        contract_master_service._rows_by_segment.clear()
        contract_master_service._rows_by_segment.update(snap_rows)
        contract_master_service._by_id.clear()
        contract_master_service._by_id.update(snap_id)
        contract_master_service._by_spec.clear()
        contract_master_service._by_spec.update(snap_spec)
        contract_master_service.cache_dir = orig_dir
    print("ok - missing/stale contract master blocks SAR, paper worker, backtest & sizing")


def test_past_expiry_refused_in_lookup_and_excluded_from_search():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Advance "today" past 01-Dec-2026 so 01-Dec-2026 is expired, while 08-Dec-2026 is active
        svc = _make_service(tmpdir, today_ist="2026-12-05")
        try:
            svc.lookup_contract("NSEFO", "NIFTY", "OPTIDX", "01-Dec-2026", "17550", "CE")
            raise AssertionError("Expected refusal for past expiry lookup")
        except ContractMasterError as exc:
            assert exc.status_code == 400

        try:
            svc.get_by_instrument_id("NSEFO", "35036")
            raise AssertionError("Expected refusal for past expiry instrumentId lookup")
        except ContractMasterError as exc:
            assert exc.status_code == 400

        hits = svc.search("NIFTY", segment="NSEFO", limit=50)
        assert len(hits) > 0
        assert all(not h["expiry"] or h["expiry"] >= "2026-12-05" for h in hits)
        assert not any(h["instrumentId"] == "35036" for h in hits)
    print("ok - past-expiry contracts refused in lookups and excluded from search")


def test_search_guards_case_insensitive_and_limit():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = _make_service(tmpdir, today_ist="2026-11-12")
        # Query length guard: < 2 chars returns []
        assert svc.search("N", segment="NSEFO") == []
        # > 64 chars raises 400
        try:
            svc.search("N" * 65, segment="NSEFO")
            raise AssertionError("Expected 400 for query > 64 chars")
        except ContractMasterError as exc:
            assert exc.status_code == 400
        # Case-insensitive & capped limit
        lower_hits = svc.search("nifty", segment="NSEFO", limit=5)
        upper_hits = svc.search("NIFTY", segment="NSEFO", limit=5)
        assert len(lower_hits) == 5
        assert [h["instrumentId"] for h in lower_hits] == [h["instrumentId"] for h in upper_hits]
    print("ok - search query length guard, case-insensitivity & limit")


def test_contract_master_overrides_stale_strategy_lot_size_with_warning():
    from app.backtest_engine import new_paper_worker_run, run_backtest
    from app.exit_engine import open_managed_position
    from app.schema_v2 import new_rule_builder

    orig_dir = contract_master_service.cache_dir
    orig_today = contract_master_service._today_ist
    snap_meta = dict(contract_master_service._meta)
    snap_rows = dict(contract_master_service._rows_by_segment)
    snap_id = dict(contract_master_service._by_id)
    snap_spec = dict(contract_master_service._by_spec)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            contract_master_service.cache_dir = Path(tmpdir)
            contract_master_service._today_ist = lambda: "2026-11-12"
            contract_master_service.load_rows_into_cache("NSEFO", NSEFO_FIXTURE, downloaded_date_ist="2026-11-12", persist=False)

            strat = new_rule_builder()
            strat["id"] = "nifty-stale-25"
            strat["name"] = "NIFTY Stale Lot Test"
            strat["meta"]["id"] = strat["id"]
            strat["meta"]["name"] = strat["name"]
            strat["execution"]["instrument"] = {
                "underlying": "NIFTY 50",
                "signalSource": "INDEX_SPOT",
                "instrumentType": "INDEX_OPTIONS",
                "optionType": "CE",
                "strike": "ATM",
                "expiry": "CURRENT_WEEK",
                "lotSize": 25,
            }

            pw = new_paper_worker_run(strat)
            assert pw["lotSize"] == 65
            assert any("25" in w and "65" in w for w in pw.get("warnings", []))

            bt = run_backtest(strat, {"5m": [
                {"timestamp": "2026-11-12T09:15:00+05:30", "open": 24000, "high": 24020, "low": 23990, "close": 24010, "volume": 100}
            ]})
            assert bt["lotSize"] == 65
            assert any("25" in w and "65" in w for w in bt.get("warnings", []))

            pos = open_managed_position(strat, "BUY", 24000.0, "2026-11-12T09:20:00+05:30", {"5m": []})
            assert pos["lotSize"] == 65
            assert any("25" in w and "65" in w for w in pos.get("warnings", []))

            client = TestClient(app)
            sar_res = client.post("/api/sar/start", json={
                "strategy": {
                    "id": "sar-nifty-stale-25",
                    "name": "SAR NIFTY Stale 25",
                    "instrument": "NIFTY 50",
                    "lotSize": 25,
                    "strategyType": "LEVEL_SAR",
                    "sar": {
                        "levelSource": "MANUAL",
                        "entryStyle": "REVERSAL",
                        "manual": {"upper": 24100, "lower": 24000},
                        "slPoints": 20,
                        "tgtPoints": 40,
                        "baseQuantity": 1,
                        "multipliers": [2],
                        "maxLegs": 2,
                        "startTime": "09:15",
                        "endTime": "15:00",
                        "afterTarget": "STOP",
                        "maxRestarts": 1,
                    },
                    "risk": {"maxDailyLoss": 2000, "maxCapital": 25000},
                }
            })
            assert sar_res.status_code == 200
            sar_data = sar_res.json()
            assert sar_data["lotSize"] == 65
            assert any("25" in w and "65" in w for w in sar_data.get("warnings", []))
            client.post("/api/sar/stop", json={"runId": sar_data["id"]})
    finally:
        contract_master_service._meta.clear()
        contract_master_service._meta.update(snap_meta)
        contract_master_service._rows_by_segment.clear()
        contract_master_service._rows_by_segment.update(snap_rows)
        contract_master_service._by_id.clear()
        contract_master_service._by_id.update(snap_id)
        contract_master_service._by_spec.clear()
        contract_master_service._by_spec.update(snap_spec)
        contract_master_service.cache_dir = orig_dir
        contract_master_service._today_ist = orig_today
    print("ok - contract master overrides stale lotSize 25 with 65 and visible warning")


if __name__ == "__main__":
    test_column_and_value_normalization()
    test_exact_lookups_ce_pe_fut_eq_indices_and_decimal_strike()
    test_refusals_ambiguous_not_found_unsupported_and_stale()
    test_ec954_ec990_quantity_multiple_precheck_before_broker_call()
    test_rate_limit_two_requests_per_second_per_segment()
    test_api_search_lot_size_and_capabilities_endpoints()
    test_weekly_vs_monthly_expiry_pe_and_noneq_series()
    test_csv_normalization_both_nseeq_and_nsefo()
    test_production_code_never_loads_fixtures()
    test_missing_or_stale_blocks_sar_paper_backtest_and_sizing()
    test_past_expiry_refused_in_lookup_and_excluded_from_search()
    test_search_guards_case_insensitive_and_limit()
    test_contract_master_overrides_stale_strategy_lot_size_with_warning()
    print("ALL CONTRACT MASTER TESTS PASSED")
