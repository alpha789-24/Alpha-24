"""IIFL Capital Market Data REST Client (Part 3a).

Supports:
- POST /marketdata/marketquotes (single or bulk)
- POST /marketdata/historicaldata (with EC802 7-interval check & EC809 date-chunking)

Does NOT switch the app's live data source yet; used standalone and by Part 3a tests/tools.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

import requests

from .broker_execution import BrokerSessionExpiredError

IST = timezone(timedelta(hours=5, minutes=30))

ALLOWED_HISTORICAL_INTERVALS: tuple[str, ...] = (
    "1 minute",
    "5 minutes",
    "10 minutes",
    "15 minutes",
    "30 minutes",
    "60 minutes",
    "1 day",
)

SESSION_INVALID_CODES: tuple[str, ...] = ("EC044", "EC055", "EC075")

# Documented per-API rate limits (requests/sec, per session) from docs/iifl-api-reference.md 6.2
IIFL_API_RATE_LIMITS_RPS: dict[str, float] = {
    "marketquotes": 10.0,
    "historicaldata": 10.0,
    "marketdepth": 10.0,
    "openinterest": 10.0,
}

_EC_PATTERN = re.compile(r"\b(EC\d{3})\b", re.IGNORECASE)


class IIFLMarketDataError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400, error_code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


class SharedEndpointRateLimiter:
    """Thread-safe shared rate limiter enforcing documented per-API RPS limits."""

    def __init__(
        self,
        limits_rps: Optional[dict[str, float]] = None,
        now_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        self._limits = dict(limits_rps or IIFL_API_RATE_LIMITS_RPS)
        self._now_fn = now_fn or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}

    def acquire(self, api_key: str) -> float:
        rps = float(self._limits.get(api_key, 10.0))
        min_interval = (1.0 / rps) if rps > 0 else 0.0
        slept = 0.0
        with self._lock:
            now = float(self._now_fn())
            last = self._last_call.get(api_key)
            if last is not None and min_interval > 0:
                elapsed = now - last
                if elapsed < min_interval:
                    wait_sec = min_interval - elapsed
                    self._sleep_fn(wait_sec)
                    slept = wait_sec
                    now = float(self._now_fn())
            self._last_call[api_key] = now
        return slept


SHARED_MARKETDATA_RATE_LIMITER = SharedEndpointRateLimiter()


def _coerce_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _coerce_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_iifl_timestamp(value: Any) -> Optional[datetime]:
    """Parse any documented IIFL timestamp into a timezone-aware IST datetime, or None if missing/invalid."""
    if value is None or isinstance(value, bool):
        return None

    # Spreadsheet serial number or epoch numeric
    if isinstance(value, (int, float)):
        num = float(value)
        if num <= 0:
            return None
        # Excel serial days (typically 20,000 .. 80,000 for 1954..2119)
        if 10_000 <= num <= 100_000:
            base = datetime(1899, 12, 30, tzinfo=IST)
            return base + timedelta(days=num)
        # Unix epoch milliseconds vs seconds
        if num > 1_000_000_000_000:
            return datetime.fromtimestamp(num / 1000.0, tz=IST)
        if num > 1_000_000_000:
            return datetime.fromtimestamp(num, tz=IST)
        return None

    s = str(value).strip()
    if not s:
        return None

    # Numeric string (serial or epoch)
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_iifl_timestamp(float(s))

    # ISO-8601 (with optional Z or offset)
    iso_candidate = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except ValueError:
        pass

    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%B-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d-%b-%Y",
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse_date_input(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise IIFLMarketDataError(f"EC804/EC806: invalid date '{value}'; expected DD-MMM-YYYY or YYYY-MM-DD.")


def format_iifl_date(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _extract_error_code(payload: Any, raw_text: str = "") -> Optional[str]:
    candidates: list[str] = []
    if isinstance(payload, dict):
        for k in ("errorCode", "code", "ecCode", "message", "status", "error"):
            v = payload.get(k)
            if v is not None:
                candidates.append(str(v))
    if raw_text:
        candidates.append(raw_text)
    for text in candidates:
        m = _EC_PATTERN.search(text)
        if m:
            return m.group(1).upper()
    return None


def _decode_json_defensively(raw_body: Any) -> Any:
    """Decode dict/list or string-wrapped JSON (including double-encoded JSON strings)."""
    if isinstance(raw_body, (dict, list)):
        return raw_body
    if isinstance(raw_body, str):
        s = raw_body.strip()
        if not s:
            raise ValueError("Empty response body")
        parsed = json.loads(s)
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        return parsed
    raise ValueError(f"Unsupported response type: {type(raw_body).__name__}")


def normalize_market_quote(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single IIFL marketquotes record defensively (including besAskPrice typo)."""
    best_ask = item.get("bestAskPrice")
    if best_ask is None:
        best_ask = item.get("besAskPrice")

    raw_ts = item.get("tickTimestamp")
    parsed_ts = parse_iifl_timestamp(raw_ts)

    return {
        "exchange": str(item.get("exchange") or "").strip().upper(),
        "instrumentId": str(item.get("instrumentId") or "").strip(),
        "ltp": _coerce_float(item.get("ltp")),
        "lastTradedQuantity": _coerce_int(item.get("lastTradedQuantity")),
        "averageTradedPrice": _coerce_float(item.get("averageTradedPrice")),
        "tradedVolume": _coerce_int(item.get("tradedVolume")),
        "open": _coerce_float(item.get("open")),
        "high": _coerce_float(item.get("high")),
        "low": _coerce_float(item.get("low")),
        "close": _coerce_float(item.get("close")),
        "bestBidPrice": _coerce_float(item.get("bestBidPrice")),
        "bestBidQuantity": _coerce_int(item.get("bestBidQuantity")),
        "bestAskPrice": _coerce_float(best_ask),
        "bestAskQuantity": _coerce_int(item.get("bestAskQuantity")),
        "totalBidQuantity": _coerce_int(item.get("totalBidQuantity")),
        "totalAskQuantity": _coerce_int(item.get("totalAskQuantity")),
        "tickTimestamp": raw_ts if raw_ts not in ("", None) else None,
        "tickTimestampIst": parsed_ts.isoformat() if parsed_ts is not None else None,
        "tickDatetimeIst": parsed_ts,
    }


class IIFLMarketDataClient:
    """IIFL Market Data REST client with shared rate limiting, defensive parsing, and session invalidation."""

    def __init__(
        self,
        base_url: str = "https://api.iiflcapital.com/v1",
        session_token: Optional[str] = None,
        http_client: Any = requests,
        rate_limiter: Optional[SharedEndpointRateLimiter] = None,
        timeout: float = 15.0,
        max_days_per_chunk: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.session_token = session_token
        self.http = http_client
        self.rate_limiter = rate_limiter or SHARED_MARKETDATA_RATE_LIMITER
        self.timeout = float(timeout)
        self.max_days_per_chunk = max(1, int(max_days_per_chunk))
        self.session_expired: bool = False
        self.session_expired_reason: Optional[str] = None

    def _invalidate_session(self, reason: str) -> None:
        self.session_token = None
        self.session_expired = True
        self.session_expired_reason = reason

    def _headers(self) -> dict[str, str]:
        if self.session_expired or not self.session_token:
            raise BrokerSessionExpiredError(
                self.session_expired_reason or "IIFL session expired or missing token; re-login required."
            )
        return {
            "Authorization": f"Bearer {self.session_token}",
            "Content-Type": "application/json",
        }

    def _post_marketdata(self, api_key: str, path: str, payload: Any) -> Any:
        headers = self._headers()  # fails immediately if session_expired or token cleared
        self.rate_limiter.acquire(api_key)

        try:
            resp = self.http.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise IIFLMarketDataError(
                f"IIFL {api_key} network error: {type(exc).__name__}",
                status_code=502,
            )

        status_code = getattr(resp, "status_code", 0)
        if status_code == 401:
            self._invalidate_session("IIFL session expired (HTTP 401 Unauthorized).")
            raise BrokerSessionExpiredError(self.session_expired_reason)

        raw_text = getattr(resp, "text", "") or ""
        try:
            raw_json = resp.json()
            body = _decode_json_defensively(raw_json)
        except Exception:
            try:
                body = _decode_json_defensively(raw_text)
            except Exception as exc:
                ec = _extract_error_code(None, raw_text)
                if ec in SESSION_INVALID_CODES:
                    self._invalidate_session(f"IIFL session invalid ({ec}).")
                    raise BrokerSessionExpiredError(self.session_expired_reason)
                raise IIFLMarketDataError(
                    f"IIFL {api_key} returned invalid JSON (HTTP {status_code}): {exc}",
                    status_code=502,
                )

        if isinstance(body, dict):
            ec = _extract_error_code(body, raw_text)
            if ec in SESSION_INVALID_CODES:
                msg = body.get("message") or ec
                self._invalidate_session(f"IIFL session invalid ({ec}): {msg}")
                raise BrokerSessionExpiredError(self.session_expired_reason)
            if status_code >= 400 or str(body.get("status", "")).lower() not in ("ok", "success", ""):
                if str(body.get("status", "")).lower() != "ok":
                    msg = body.get("message") or f"HTTP {status_code}"
                    raise IIFLMarketDataError(
                        f"IIFL {api_key} error: {msg}",
                        status_code=400 if status_code < 500 else status_code,
                        error_code=ec,
                    )

        if status_code >= 400:
            raise IIFLMarketDataError(f"IIFL {api_key} HTTP {status_code}", status_code=status_code)
        return body

    def get_market_quotes(self, instruments: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fetch bulk market quotes via POST /marketdata/marketquotes."""
        req_items = []
        for inst in instruments:
            ex = str(inst.get("exchange") or "").strip().upper()
            iid = str(inst.get("instrumentId") or "").strip()
            if not ex or not iid:
                raise IIFLMarketDataError("Each quote item requires non-empty 'exchange' and 'instrumentId'.")
            req_items.append({"exchange": ex, "instrumentId": iid})
        if not req_items:
            return []

        body = self._post_marketdata("marketquotes", "/marketdata/marketquotes", req_items)
        raw_res = body.get("result") if isinstance(body, dict) else body
        if isinstance(raw_res, str):
            raw_res = _decode_json_defensively(raw_res)
        if isinstance(raw_res, dict):
            raw_res = [raw_res]
        if not isinstance(raw_res, list):
            raise IIFLMarketDataError("Unexpected marketquotes result shape.", status_code=502)

        return [normalize_market_quote(r) for r in raw_res if isinstance(r, dict)]

    def get_historical_data(
        self,
        exchange: str,
        instrument_id: str,
        interval: str,
        from_date: Any,
        to_date: Any,
    ) -> list[dict[str, Any]]:
        """Fetch historical OHLCV candles via POST /marketdata/historicaldata with EC802 & EC809 handling."""
        ex = str(exchange or "").strip().upper()
        iid = str(instrument_id or "").strip()
        iv = str(interval or "").strip()
        if not ex or not iid:
            raise IIFLMarketDataError("exchange and instrumentId are required.")
        if iv not in ALLOWED_HISTORICAL_INTERVALS:
            raise IIFLMarketDataError(
                f"EC802: invalid interval '{iv}'. Allowed: {', '.join(ALLOWED_HISTORICAL_INTERVALS)}."
            )

        start_d = parse_date_input(from_date)
        end_d = parse_date_input(to_date)
        if start_d > end_d:
            raise IIFLMarketDataError("EC807: fromDate cannot be greater than toDate.")

        chunk_days = 365 if iv == "1 day" else self.max_days_per_chunk
        candles_by_ts: dict[str, dict[str, Any]] = {}

        cur = start_d
        while cur <= end_d:
            chunk_end = min(end_d, cur + timedelta(days=chunk_days - 1))
            payload = {
                "exchange": ex,
                "instrumentId": iid,
                "interval": iv,
                "fromDate": format_iifl_date(cur),
                "toDate": format_iifl_date(chunk_end),
            }
            body = self._post_marketdata("historicaldata", "/marketdata/historicaldata", payload)
            raw_res = body.get("result") if isinstance(body, dict) else body
            if isinstance(raw_res, str):
                raw_res = _decode_json_defensively(raw_res)

            entries = raw_res if isinstance(raw_res, list) else ([raw_res] if isinstance(raw_res, dict) else [])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                raw_candles = entry.get("candles")
                if isinstance(raw_candles, str):
                    raw_candles = _decode_json_defensively(raw_candles)
                if not isinstance(raw_candles, list):
                    continue
                for row in raw_candles:
                    if not isinstance(row, (list, tuple)) or len(row) < 5:
                        continue
                    ts_dt = parse_iifl_timestamp(row[0])
                    if ts_dt is None:
                        continue
                    o = _coerce_float(row[1])
                    h = _coerce_float(row[2])
                    l = _coerce_float(row[3])
                    c = _coerce_float(row[4])
                    v = _coerce_int(row[5]) if len(row) > 5 else 0
                    if None in (o, h, l, c):
                        continue
                    ts_iso = ts_dt.isoformat()
                    candles_by_ts[ts_iso] = {
                        "timestamp": ts_iso,
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v if v is not None else 0,
                    }

            cur = chunk_end + timedelta(days=1)

        return [candles_by_ts[k] for k in sorted(candles_by_ts.keys())]

