"""Offline tests for IIFL Market Data Client & Session Invalidation (Part 3a).

Uses mocked responses built from docs/iifl/iifl-api-reference.md samples. Zero network calls.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import app.main as main_mod
from app.broker_execution import BrokerSessionExpiredError, IIFLBrokerAdapter
from app.iifl_market_data import (
    IIFLMarketDataClient,
    IIFLMarketDataError,
    SharedEndpointRateLimiter,
)


class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _QueueHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP POST (possible retry!)")
        return self.responses.pop(0)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None, headers))
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP GET (possible retry!)")
        return self.responses.pop(0)


def _new_md(http):
    return IIFLMarketDataClient(
        session_token="secret-tok",
        http_client=http,
        rate_limiter=SharedEndpointRateLimiter(sleep_fn=lambda _: None),
    )


def test_marketquotes_bes_ask_price_fallback():
    http = _QueueHttp([_Resp(200, {
        "status": "Ok",
        "message": "Success",
        "result": [{
            "exchange": "NSEEQ",
            "instrumentId": 1594,
            "ltp": 1412.95,
            "bestBidPrice": 2994.25,
            "besAskPrice": 2994.75,
            "tickTimestamp": "19-Sep-2024 09:15:00",
        }],
    })])
    quotes = _new_md(http).get_market_quotes([{"exchange": "NSEEQ", "instrumentId": "1594"}])
    assert len(quotes) == 1
    assert quotes[0]["bestAskPrice"] == 2994.75
    print("ok - marketquotes besAskPrice typo fallback")


def test_marketquotes_missing_tick_timestamp():
    http = _QueueHttp([_Resp(200, {
        "status": "Ok",
        "message": "Success",
        "result": [{"exchange": "NSEFO", "instrumentId": "35036", "ltp": 215.5}],
    })])
    quotes = _new_md(http).get_market_quotes([{"exchange": "NSEFO", "instrumentId": "35036"}])
    assert quotes[0]["tickTimestamp"] is None
    assert quotes[0]["tickTimestampIst"] is None
    print("ok - marketquotes missing tickTimestamp")


def test_marketquotes_numeric_strings_and_timestamps():
    http = _QueueHttp([_Resp(200, {
        "status": "Ok",
        "message": "Success",
        "result": [
            {
                "exchange": "NSEEQ",
                "instrumentId": 1594,
                "ltp": "1412.95",
                "tradedVolume": "7360198",
                "tickTimestamp": "19-Sep-2024 09:15:00",
            },
            {
                "exchange": "NSEEQ",
                "instrumentId": "2885",
                "ltp": 1300.0,
                "tickTimestamp": 45554.385416666664,
            },
        ],
    })])
    quotes = _new_md(http).get_market_quotes([
        {"exchange": "NSEEQ", "instrumentId": "1594"},
        {"exchange": "NSEEQ", "instrumentId": "2885"},
    ])
    assert quotes[0]["ltp"] == 1412.95
    assert quotes[0]["tradedVolume"] == 7360198
    assert quotes[0]["tickTimestampIst"] == "2024-09-19T09:15:00+05:30"
    assert quotes[1]["tickTimestampIst"].startswith("2024-09-19T09:15:")
    print("ok - marketquotes numeric strings & timestamp formats")


def test_historicaldata_string_wrapped_json():
    wrapped = json.dumps({
        "status": "Ok",
        "message": "Success",
        "result": [{
            "candles": [
                ["2024-11-11T09:15:00", "68.37", 68.7, 68.15, 68.18, "1494"],
                ["2024-11-11T09:16:00", 295.0, 296.3, 293.75, 295.3, 11315876],
            ]
        }],
    })
    http = _QueueHttp([_Resp(200, json_data=wrapped, text=wrapped)])
    candles = _new_md(http).get_historical_data("NSEEQ", "1594", "1 minute", "11-Nov-2024", "11-Nov-2024")
    assert len(candles) == 2
    assert candles[0]["open"] == 68.37 and candles[0]["volume"] == 1494
    assert candles[0]["timestamp"] == "2024-11-11T09:15:00+05:30"
    print("ok - historicaldata string-wrapped JSON response")


def test_historicaldata_ec802_rejects_disallowed_intervals():
    client = _new_md(_QueueHttp([]))
    for bad_iv in ("weekly", "monthly", "2 minutes"):
        try:
            client.get_historical_data("NSEEQ", "1594", bad_iv, "11-Nov-2024", "12-Nov-2024")
            raise AssertionError(f"Expected EC802 rejection for {bad_iv}")
        except IIFLMarketDataError as exc:
            assert "EC802" in exc.message
    print("ok - historicaldata EC802 7-interval whitelist")


def test_historicaldata_ec809_date_range_chunking():
    r1 = {"status": "Ok", "result": [{"candles": [["2024-11-11T09:15:00", 68.37, 68.7, 68.15, 68.18, 1494]]}]}
    r2 = {"status": "Ok", "result": [{"candles": [["2024-12-12T09:15:00", 300.0, 305.0, 299.0, 304.0, 5000]]}]}
    http = _QueueHttp([_Resp(200, r1), _Resp(200, r2)])
    client = IIFLMarketDataClient(
        session_token="tok-123",
        http_client=http,
        rate_limiter=SharedEndpointRateLimiter(sleep_fn=lambda _: None),
        max_days_per_chunk=30,
    )
    candles = client.get_historical_data("NSEEQ", "1594", "1 minute", "11-Nov-2024", "20-Dec-2024")
    assert len(http.calls) == 2
    assert http.calls[0][2]["fromDate"] == "11-Nov-2024" and http.calls[0][2]["toDate"] == "10-Dec-2024"
    assert http.calls[1][2]["fromDate"] == "11-Dec-2024" and http.calls[1][2]["toDate"] == "20-Dec-2024"
    assert len(candles) == 2
    print("ok - historicaldata EC809 date-range chunking")


def test_shared_rate_limiter_enforces_documented_limits():
    clock = [100.0]
    sleeps = []
    limiter = SharedEndpointRateLimiter(
        now_fn=lambda: clock[0],
        sleep_fn=lambda sec: (sleeps.append(sec), clock.__setitem__(0, clock[0] + sec)),
    )
    limiter.acquire("marketquotes")
    limiter.acquire("marketquotes")
    assert len(sleeps) == 1 and abs(sleeps[0] - 0.1) < 1e-6
    print("ok - shared rate limiter enforces 10 req/sec")


def _assert_session_invalid_signal(bad_resp, label):
    md_http = _QueueHttp([bad_resp])
    md = _new_md(md_http)
    try:
        md.get_market_quotes([{"exchange": "NSEEQ", "instrumentId": "1594"}])
        raise AssertionError(f"Expected BrokerSessionExpiredError for {label}")
    except BrokerSessionExpiredError:
        pass
    assert md.session_expired is True and md.session_token is None
    assert len(md_http.calls) == 1

    br_http = _QueueHttp([bad_resp])
    adapter = IIFLBrokerAdapter(
        base_url="https://api.iiflcapital.com/v1",
        session_token="secret-tok",
        http_client=br_http,
        sync_after_place=False,
    )
    try:
        adapter.place_order(
            {
                "exchange": "NSE",
                "instrument": "EQUITY",
                "instrumentId": "1594",
                "quantity": 1,
                "lotSize": 1,
                "product": "INTRADAY",
                "orderType": "MARKET",
                "validity": "DAY",
            },
            side="BUY",
            idempotency_key=f"K-{label}",
        )
        raise AssertionError(f"Expected BrokerSessionExpiredError on broker for {label}")
    except BrokerSessionExpiredError:
        pass
    assert adapter.session_expired is True and adapter.session_token is None
    assert len(br_http.calls) == 1


def test_session_invalid_http_401():
    _assert_session_invalid_signal(_Resp(401, {"status": "Error", "message": "Unauthorized"}), "HTTP 401")
    print("ok - session invalid HTTP 401")


def test_session_invalid_ec044():
    _assert_session_invalid_signal(_Resp(200, {"status": "Error", "message": "EC044 Error : Token is not valid."}), "EC044")
    print("ok - session invalid EC044")


def test_session_invalid_ec055():
    _assert_session_invalid_signal(_Resp(200, {"status": "Error", "message": "EC055 Error : Unauthorized access."}), "EC055")
    print("ok - session invalid EC055")


def test_session_invalid_ec075():
    _assert_session_invalid_signal(_Resp(200, {"status": "Error", "message": "EC075 Error : Unauthorized access."}), "EC075")
    print("ok - session invalid EC075")


def test_api_endpoint_session_invalidation_clears_app_state_and_blocks_orders():
    orig_http = main_mod.IIFL_HTTP_CLIENT
    orig_token = getattr(main_mod.app.state, "iifl_token", None)
    orig_exp = getattr(main_mod.app.state, "iifl_session_expired", False)
    try:
        main_mod.app.state.iifl_token = "live-session-tok"
        main_mod.app.state.iifl_session_expired = False
        mock_http = _QueueHttp([
            _Resp(200, {"status": "Error", "message": "EC044 Error : Token is not valid."}, text='{"message":"EC044"}')
        ])
        main_mod.IIFL_HTTP_CLIENT = mock_http
        client = TestClient(main_mod.app)

        # Call API endpoint (/api/iifl/profile) that triggers EC044
        r_prof = client.get("/api/iifl/profile")
        assert r_prof.status_code == 401
        assert "Session expired, reconnect" in r_prof.text
        assert main_mod.app.state.iifl_token is None
        assert main_mod.app.state.iifl_session_expired is True

        # Status endpoint reflects Session expired, reconnect
        r_stat = client.get("/api/iifl/status")
        assert r_stat.status_code == 200
        st = r_stat.json()
        assert st["sessionPresent"] is False
        assert st["sessionExpired"] is True
        assert st["sessionMessage"] == "Session expired, reconnect"

        # Subsequent broker order/sync call is refused without hitting IIFL HTTP again
        r_sync = client.post("/api/broker/orders/sync", json={
            "orderId": "nonexistent",
            "action": "resolve",
            "resolutionStatus": "CANCELLED",
        })
        # Also verify adapter built per-request sees no token
        assert main_mod._get_iifl_adapter(getattr(main_mod.app.state, "iifl_token", None)) is None
        assert len(mock_http.calls) == 1
    finally:
        main_mod.IIFL_HTTP_CLIENT = orig_http
        main_mod.app.state.iifl_token = orig_token
        main_mod.app.state.iifl_session_expired = orig_exp
    print("ok - API endpoint session invalidation clears app.state & blocks subsequent calls")


if __name__ == "__main__":
    test_marketquotes_bes_ask_price_fallback()
    test_marketquotes_missing_tick_timestamp()
    test_marketquotes_numeric_strings_and_timestamps()
    test_historicaldata_string_wrapped_json()
    test_historicaldata_ec802_rejects_disallowed_intervals()
    test_historicaldata_ec809_date_range_chunking()
    test_shared_rate_limiter_enforces_documented_limits()
    test_session_invalid_http_401()
    test_session_invalid_ec044()
    test_session_invalid_ec055()
    test_session_invalid_ec075()
    test_api_endpoint_session_invalidation_clears_app_state_and_blocks_orders()
    print("ALL IIFL MARKET DATA TESTS PASSED")
