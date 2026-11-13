"""Phase 7 Broker Execution, Capabilities Catalog, Order Lifecycle, Idempotency & Go-Live Safety Gates.

Enforces all Phase 7 requirements:
1. `BROKER_CAPABILITIES`:
   - Canonical dictionary of supported values for `broker`, `exchange`, `symbol`, `instrument`,
     `expiry`, `strike`, `optionType` (`CE`/`PE`), `product`, `orderType`, `validity`, `amo`, and `mode`.
   - `validate_execution_settings` rejects any value not present in `BROKER_CAPABILITIES`.
2. Strategy Version Hashing & Automatic Approval Reset (`compute_strategy_version_hash`):
   - Computes a deterministic SHA-256 hash over the strategy's rules, indicators, levels, stopLoss,
     targets, trailing, reentry, session, risk, and execution settings.
   - Any edit to the strategy changes its `versionHash`, automatically invalidating any prior live approval.
3. Live Trading Gates (`evaluate_go_live_gates`, `approve_live_trading`):
   - Gate 1 (Backtest): A successful backtest (`totalTrades >= 1`) recorded on this exact `versionHash`.
   - Gate 2 (Paper Trading): A completed or active paper-trading run meeting the minimum paper-trading
     duration/trades requirement on this exact `versionHash`.
   - Gate 3 (Risk Limits): Positive `maxDailyLoss`, `maxCapital`, `maxTradesPerDay >= 1`, `maxQuantity >= 1`,
     and `execution.quantity <= risk.maxQuantity`.
   - Gate 4 (Explicit Typed Confirmation): User must type `"ENABLE LIVE TRADING FOR <STRATEGY_NAME>"`
     (or `"I CONFIRM LIVE TRADING"`).
   - Gate 5 (Kill Switch Disarmed): Global kill switch must not be active.
   - Gate 6 (Market Hours Check): Order placement checks IST session & holiday calendar (unless `amo=True`
     when AMO is allowed for that broker/exchange).
   - Gate 7 (Broker Session): For `mode == "LIVE"`, requires backend env credentials configured and active session.
4. Order Lifecycle & Duplicate-Order Protection (`place_broker_order`, `cancel_broker_order`):
   - Every signal/order carries an `idempotencyKey`. Submitting a duplicate `idempotencyKey` returns
     the original order with `duplicateBlocked: True` and never sends a duplicate order.
   - Tracks `OPEN`, `FILLED`, `REJECTED` (with explicit `rejectionReason`), and `CANCELLED`.
5. Working Kill Switch (`activate_kill_switch`, `reset_kill_switch`):
   - Stops all active `paper_worker_runs` and `sar_runs`, cancels all open/pending `broker_orders`,
     marks strategies `STOPPED`, and blocks any new order placement until reset.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence
from uuid import uuid4

import requests

from .condition_engine import IST, check_session_and_holiday, parse_ist_datetime
from .contract_master import ContractMasterError, contract_master_service
from .strategy import validate_strategy

BROKER_CAPABILITIES: dict[str, Any] = {
    "brokers": ["IIFL", "PAPER_SIMULATOR"],
    "exchanges": {
        "IIFL": ["NSE", "NFO", "BSE", "BFO"],
        "PAPER_SIMULATOR": ["NSE", "NFO", "BSE", "BFO"],
    },
    "symbols": [
        "NIFTY", "NIFTY 50", "BANKNIFTY", "FINNIFTY", "SENSEX",
        "NIFTY IT", "NIFTY AUTO", "NIFTY FMCG",
        "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL", "TATAMOTORS", "ZOMATO",
    ],
    "instruments": ["INDEX_OPTIONS", "INDEX_FUTURES", "INDEX_SPOT", "EQUITY"],
    "expiries": ["CURRENT_WEEK", "NEXT_WEEK", "MONTHLY"],
    "strikes": ["ATM", "ITM 1", "ITM 2", "OTM 1", "OTM 2"],
    "optionTypes": ["CE", "PE", "NA"],
    "products": {
        "IIFL": ["INTRADAY", "MARGIN", "DELIVERY", "PAPER"],
        "PAPER_SIMULATOR": ["PAPER", "INTRADAY", "MARGIN"],
    },
    "orderTypes": ["MARKET", "LIMIT", "SL_LIMIT", "SL_MARKET"],
    "validities": ["DAY", "IOC"],
    "amoAllowed": [True, False],
    "modes": ["PAPER", "LIVE"],
    "lotSizes": {},
}

REQUIRED_CONFIRMATION_PREFIX = "ENABLE LIVE TRADING FOR "
FALLBACK_CONFIRMATION_PHRASE = "I CONFIRM LIVE TRADING"
DEFAULT_MAX_SLIPPAGE_PERCENT = 1.5
MAX_MODIFY_PER_ORDER = 25


def make_order_tag(strategy_id: str, idempotency_key: str) -> str:
    """Return a deterministic ~19-char alphanumeric tag unique per (strategy_id, idempotency_key)."""
    raw = f"{strategy_id}|{idempotency_key}".encode("utf-8")
    return "a24" + hashlib.sha256(raw).hexdigest()[:16]


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _safe_pos_float(val: Any) -> Optional[float]:
    try:
        f = float(val)
        return round(f, 2) if f > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_broker_update_time(val: Any) -> datetime:
    s = str(val or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.min

IIFL_EXCHANGE_MAP = {
    ("NSE", "EQUITY"): "NSEEQ",
    ("NSE", "INDEX_SPOT"): "NSEEQ",
    ("NSE", "INDEX_OPTIONS"): "NSEFO",
    ("NSE", "INDEX_FUTURES"): "NSEFO",
    ("NFO", "INDEX_OPTIONS"): "NSEFO",
    ("NFO", "INDEX_FUTURES"): "NSEFO",
    ("NFO", "EQUITY"): "NSEFO",
    ("BSE", "EQUITY"): "BSEEQ",
    ("BSE", "INDEX_SPOT"): "BSEEQ",
    ("BSE", "INDEX_OPTIONS"): "BSEFO",
    ("BSE", "INDEX_FUTURES"): "BSEFO",
    ("BFO", "INDEX_OPTIONS"): "BSEFO",
    ("BFO", "INDEX_FUTURES"): "BSEFO",
}

IIFL_VALID_EXCHANGES = {
    "NSEEQ", "NSEFO", "BSEEQ", "BSEFO", "NSECURR",
    "BSECURR", "MCXCOMM", "NSECOMM", "BSECOMM", "NCDEXCOMM",
}

IIFL_PRODUCT_MAP = {
    "INTRADAY": "INTRADAY",
    "MARGIN": "NORMAL",
    "NORMAL": "NORMAL",
    "DELIVERY": "DELIVERY",
    "BNPL": "BNPL",
}

IIFL_ORDER_TYPE_MAP = {
    "MARKET": "MARKET",
    "LIMIT": "LIMIT",
    "SL_LIMIT": "SL",
    "SL": "SL",
    "SL_MARKET": "SLM",
    "SLM": "SLM",
}


class BrokerSessionExpiredError(RuntimeError):
    """Raised when the broker API returns HTTP 401 Unauthorized."""


NON_LIVE_QUOTE_SOURCES = {
    "OFFLINE FALLBACK",
    "CACHED FEED (OFFLINE)",
    "CACHED FEED (BACKOFF)",
    "NO DATA",
    "MOCK",
    "DEMO",
}


def check_price_band_slippage(
    execution_settings: dict,
    reference_price: float,
    risk_cfg: Optional[dict] = None,
    reference_quote: Optional[dict] = None,
    max_quote_age_seconds: float = 15.0,
    now_ts: Optional[float] = None,
) -> Optional[str]:
    """Validate reference quote freshness/liveness and order price against LTP within slippage band.

    Returns an error reason string if violated, or None if valid.
    """
    if reference_quote is not None:
        source = str(reference_quote.get("source") or "").strip().upper()
        if reference_quote.get("noData") or not source or source in NON_LIVE_QUOTE_SOURCES or "OFFLINE" in source or "BACKOFF" in source or "MOCK" in source or "FALLBACK" in source:
            return f"Slippage guard rejected non-live/fallback reference quote (source='{source or 'MISSING'}')."
        updated_at = reference_quote.get("updatedAt")
        if updated_at is None:
            return "Slippage guard rejected reference quote missing updatedAt timestamp."
        try:
            ref_ts = float(updated_at)
        except (TypeError, ValueError):
            return "Slippage guard rejected reference quote with invalid updatedAt timestamp."
        curr_ts = float(now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp())
        if curr_ts - ref_ts > float(max_quote_age_seconds):
            return f"Slippage guard rejected stale reference quote (age {curr_ts - ref_ts:.1f}s > {max_quote_age_seconds:.1f}s)."
        # Also validate source exchange/market timestamp when provided (updatedAt is only local fetch time;
        # the real fix for LIVE execution is subscribing directly to IIFL market data).
        reg_mkt_time = reference_quote.get("regularMarketTime")
        if reg_mkt_time is not None:
            try:
                mkt_ts = float(reg_mkt_time)
            except (TypeError, ValueError):
                return "Slippage guard rejected reference quote with invalid regularMarketTime timestamp."
            if curr_ts - mkt_ts > float(max_quote_age_seconds):
                return (
                    f"Slippage guard rejected quote with stale source market timestamp "
                    f"(regularMarketTime age {curr_ts - mkt_ts:.1f}s > {max_quote_age_seconds:.1f}s)."
                )
        if reference_quote.get("ltp") is not None:
            reference_price = reference_quote["ltp"]

    try:
        ltp = float(reference_price)
    except (TypeError, ValueError):
        return "Valid positive reference LTP is required for price-band check."
    if ltp <= 0:
        return "Valid positive reference LTP is required for price-band check."

    max_slip = execution_settings.get("maxSlippagePercent")
    if max_slip is None and isinstance(risk_cfg, dict):
        max_slip = risk_cfg.get("maxSlippagePercent")
    try:
        max_slip_pct = float(DEFAULT_MAX_SLIPPAGE_PERCENT if max_slip is None else max_slip)
    except (TypeError, ValueError):
        max_slip_pct = DEFAULT_MAX_SLIPPAGE_PERCENT
    if max_slip_pct <= 0:
        return "maxSlippagePercent must be positive."

    order_type = str(execution_settings.get("orderType") or "MARKET").upper()
    if order_type in ("LIMIT", "SL_LIMIT"):
        limit_p = float(execution_settings.get("limitPrice") or 0.0)
        if limit_p > 0:
            dev_pct = abs(limit_p - ltp) / ltp * 100.0
            if dev_pct > max_slip_pct:
                return (
                    f"Price-band violation: limitPrice (₹{limit_p}) deviates {dev_pct:.2f}% "
                    f"from LTP (₹{ltp}), exceeding max slippage {max_slip_pct:.2f}%."
                )

    if order_type in ("SL_LIMIT", "SL_MARKET"):
        trig_p = float(execution_settings.get("triggerPrice") or 0.0)
        if trig_p > 0:
            dev_pct = abs(trig_p - ltp) / ltp * 100.0
            if dev_pct > max_slip_pct:
                return (
                    f"Price-band violation: triggerPrice (₹{trig_p}) deviates {dev_pct:.2f}% "
                    f"from LTP (₹{ltp}), exceeding max slippage {max_slip_pct:.2f}%."
                )

    if order_type == "MARKET" and execution_settings.get("expectedPrice") is not None:
        try:
            exp_p = float(execution_settings["expectedPrice"])
            if exp_p > 0:
                dev_pct = abs(ltp - exp_p) / exp_p * 100.0
                if dev_pct > max_slip_pct:
                    return (
                        f"Max-slippage violation: LTP (₹{ltp}) slipped {dev_pct:.2f}% "
                        f"from expectedPrice (₹{exp_p}), exceeding {max_slip_pct:.2f}%."
                    )
        except (TypeError, ValueError):
            pass

    return None


class IIFLBrokerAdapter:
    """Official IIFL Capital v1 Order Management REST API adapter.

    Implements all 7 Order Management endpoints documented in IIFL Capital v1:
    - POST /orders (place_order)
    - PUT /orders/{brokerOrderId} (modify_order, max 25 modifications/order)
    - DELETE /orders/{brokerOrderId} (cancel_order)
    - GET /orders (get_order_book)
    - GET /orders/{brokerOrderId} (get_order_history)
    - GET /orders/lite/{brokerOrderId} (sync_order_status)
    - GET /trades (get_trade_book)
    """

    def __init__(
        self,
        base_url: str,
        session_token: str,
        http_client: Any = requests,
        timeout_seconds: float = 10.0,
        sync_after_place: bool = True,
        contract_master: Optional[Any] = None,
        on_session_invalid: Optional[Any] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_token = session_token
        self.http = http_client
        self.timeout = timeout_seconds
        self.sync_after_place = sync_after_place
        self.contract_master = contract_master
        self.on_session_invalid = on_session_invalid
        self.session_expired = False
        self.session_expired_reason: Optional[str] = None
        self.tag_to_idempotency: dict[str, str] = {}
        self.modification_counts: dict[str, int] = {}
        self._order_instruments: dict[str, tuple[str, str]] = {}

    def _invalidate_session(self, reason: str) -> None:
        self.session_token = None
        self.session_expired = True
        self.session_expired_reason = reason
        if callable(self.on_session_invalid):
            self.on_session_invalid()

    def _check_session_invalid(self, resp: Any, body: Any = None) -> None:
        if getattr(resp, "status_code", None) == 401:
            self._invalidate_session("IIFL session expired (401 Unauthorized).")
            raise BrokerSessionExpiredError(self.session_expired_reason)
        texts: list[str] = []
        if isinstance(body, dict):
            for k in ("errorCode", "code", "ecCode", "message", "status", "error"):
                if body.get(k) is not None:
                    texts.append(str(body.get(k)))
        raw_text = getattr(resp, "text", None)
        if isinstance(raw_text, str) and raw_text:
            texts.append(raw_text)
        for t in texts:
            for code in ("EC044", "EC055", "EC075"):
                if code in t.upper():
                    self._invalidate_session(f"IIFL session invalid ({code}).")
                    raise BrokerSessionExpiredError(self.session_expired_reason)

    def _headers(self) -> dict[str, str]:
        if self.session_expired or not self.session_token:
            raise BrokerSessionExpiredError(
                self.session_expired_reason or "IIFL session expired or token cleared; re-login required."
            )
        return {
            "Authorization": f"Bearer {self.session_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def map_exchange(exchange: str, instrument: str) -> str:
        ex_u = str(exchange or "").upper().strip()
        if ex_u in IIFL_VALID_EXCHANGES:
            return ex_u
        inst_u = str(instrument or "").upper().strip()
        mapped = IIFL_EXCHANGE_MAP.get((ex_u, inst_u))
        if not mapped:
            raise ValueError(f"Unsupported exchange/instrument combination for IIFL: {ex_u}/{inst_u}")
        return mapped

    @staticmethod
    def map_product(product: str) -> str:
        prod_u = str(product or "").upper().strip()
        mapped = IIFL_PRODUCT_MAP.get(prod_u)
        if not mapped:
            raise ValueError(f"Unsupported product for IIFL live order: '{prod_u}'. Expected NORMAL, INTRADAY, DELIVERY, or BNPL.")
        return mapped

    @staticmethod
    def map_order_type(order_type: str) -> str:
        ot_u = str(order_type or "").upper().strip()
        mapped = IIFL_ORDER_TYPE_MAP.get(ot_u)
        if not mapped:
            raise ValueError(f"Unsupported orderType for IIFL: '{ot_u}'.")
        return mapped

    def build_place_order_payload(
        self,
        execution_settings: dict,
        side: str,
        idempotency_key: str,
        strategy_id: str = "",
    ) -> list[dict[str, Any]]:
        instrument_id = str(execution_settings.get("instrumentId") or "").strip()
        if not instrument_id:
            raise ValueError(
                "instrumentId is required for IIFL live order placement; no contract-master lookup is configured."
            )

        exchange = self.map_exchange(
            execution_settings.get("exchange", "NFO"),
            execution_settings.get("instrument", "INDEX_OPTIONS"),
        )
        product = self.map_product(execution_settings.get("product", "INTRADAY"))
        order_type = self.map_order_type(execution_settings.get("orderType", "MARKET"))
        qty = int(execution_settings.get("quantity", 1))
        lot_size = int(execution_settings.get("lotSize", 1))
        total_units = qty if exchange in ("MCXCOMM", "NSECOMM", "BSECOMM", "NCDEXCOMM") else qty * lot_size

        amo = bool(execution_settings.get("amo", False))
        validity = str(execution_settings.get("validity") or "DAY").upper()

        price = "0"
        if order_type in ("LIMIT", "SL") and execution_settings.get("limitPrice") is not None:
            price = str(execution_settings["limitPrice"])

        sl_trigger = "0"
        if order_type in ("SL", "SLM") and execution_settings.get("triggerPrice") is not None:
            sl_trigger = str(execution_settings["triggerPrice"])

        order_tag = make_order_tag(strategy_id, idempotency_key)
        self.tag_to_idempotency[order_tag] = idempotency_key

        item: dict[str, Any] = {
            "instrumentId": instrument_id,
            "exchange": exchange,
            "transactionType": str(side).upper(),
            "quantity": str(total_units),
            "orderComplexity": "AMO" if amo else "REGULAR",
            "product": product,
            "orderType": order_type,
            "validity": validity,
            "price": price,
            "slTriggerPrice": sl_trigger,
            "orderTag": order_tag,
        }
        # Only forward marketProtectionPercent when explicitly supplied; its unit (percent vs absolute price,
        # since IIFL docs show "1400" on a "1501.55" order) is unverified against IIFL sandbox.
        if execution_settings.get("marketProtectionPercent") is not None:
            item["marketProtectionPercent"] = str(execution_settings["marketProtectionPercent"])
        return [item]

    def __call__(
        self,
        strategy: dict,
        execution_settings: dict,
        side: str,
        idempotency_key: str,
        reference_price: float,
        eval_iso: str,
    ) -> dict[str, Any]:
        return self.place_order(
            execution_settings=execution_settings,
            side=side,
            idempotency_key=idempotency_key,
            strategy_id=str(strategy.get("id") or ""),
        )

    @staticmethod
    def _parse_iifl_order_record(latest: dict, fallback_broker_order_id: str = "") -> dict[str, Any]:
        raw_st = str(latest.get("orderStatus") or "").strip().lower()
        avg_price = _safe_pos_float(latest.get("averageTradedPrice"))
        total_qty = _safe_int(latest.get("quantity"), 0)
        filled_qty = _safe_int(latest.get("filledQuantity"), 0)
        pending_qty = _safe_int(latest.get("pendingQuantity"), 0)
        cancelled_qty = _safe_int(latest.get("cancelledQuantity"), 0)
        broker_oid = str(latest.get("brokerOrderId") or fallback_broker_order_id)

        if raw_st == "rejected":
            status = "REJECTED"
            fill_price = None
            reason = latest.get("rejectionReason") or "Rejected by broker/exchange"
        elif raw_st in ("cancelled", "canceled"):
            # Cancelled order keeps filledQuantity > 0 and averageTradedPrice for audit; pendingQuantity is ignored for status.
            status = "CANCELLED"
            fill_price = avg_price if filled_qty > 0 else None
            reason = latest.get("rejectionReason") or "Cancelled on broker"
        elif filled_qty > 0 and (pending_qty > 0 or (total_qty > 0 and filled_qty < total_qty)):
            status = "PARTIALLY_FILLED"
            fill_price = avg_price
            reason = None
        elif raw_st == "complete":
            # Never rely solely on pendingQuantity; when quantity is present require filledQuantity == quantity and pendingQuantity == 0.
            is_fully_filled = (filled_qty == total_qty and pending_qty == 0) if total_qty > 0 else (pending_qty == 0)
            if is_fully_filled:
                status = "FILLED"
                fill_price = avg_price
            else:
                status = "PARTIALLY_FILLED" if filled_qty > 0 else "OPEN"
                fill_price = avg_price if filled_qty > 0 else None
            reason = None
        else:
            status = "OPEN"
            fill_price = None
            reason = None

        return {
            "status": status,
            "brokerOrderId": broker_oid,
            "exchangeOrderId": latest.get("exchangeOrderId"),
            "quantity": total_qty if total_qty > 0 else None,
            "fillPrice": fill_price,
            "averageTradedPrice": avg_price,
            "filledQuantity": filled_qty,
            "pendingQuantity": pending_qty,
            "cancelledQuantity": cancelled_qty,
            "rejectionReason": reason,
        }

    def _get_json(self, path: str) -> Any:
        resp = self.http.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if getattr(resp, "status_code", None) == 401:
            self._invalidate_session("IIFL session expired (401 Unauthorized).")
            raise BrokerSessionExpiredError(self.session_expired_reason)
        if getattr(resp, "status_code", 200) >= 400:
            self._check_session_invalid(resp)
            return None
        try:
            body = resp.json()
        except Exception:
            self._check_session_invalid(resp)
            return None
        self._check_session_invalid(resp, body)
        return body

    def get_order_book(self) -> list[dict[str, Any]]:
        data = self._get_json("/orders")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            return [r for r in data["result"] if isinstance(r, dict)]
        return []

    def get_order_history(self, broker_order_id: str) -> list[dict[str, Any]]:
        data = self._get_json(f"/orders/{broker_order_id}")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            return [r for r in data["result"] if isinstance(r, dict)]
        return []

    def get_trade_book(self) -> list[dict[str, Any]]:
        data = self._get_json("/trades")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            return [r for r in data["result"] if isinstance(r, dict)]
        return []

    def try_reconcile_by_order_tag(self, order_tag: str) -> tuple[bool, Optional[dict[str, Any]]]:
        """Query GET /orders and return `(book_fetch_ok, parsed_match_or_None)`."""
        try:
            data = self._get_json("/orders")
        except BrokerSessionExpiredError:
            raise
        except requests.RequestException:
            return False, None
        if data is None:
            return False, None
        if isinstance(data, list):
            book = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict) and isinstance(data.get("result"), list):
            book = [r for r in data["result"] if isinstance(r, dict)]
        else:
            return False, None
        matched = next((o for o in book if str(o.get("orderTag") or "").strip() == order_tag), None)
        if not matched:
            return True, None
        parsed = self._parse_iifl_order_record(matched)
        parsed["orderTag"] = order_tag
        parsed["reconciledFromOrderBook"] = True
        return True, parsed

    def reconcile_by_order_tag(self, order_tag: str) -> Optional[dict[str, Any]]:
        """Query GET /orders and return parsed state if an order matching `order_tag` exists."""
        ok, matched = self.try_reconcile_by_order_tag(order_tag)
        return matched if ok else None

    def place_order(
        self,
        execution_settings: dict,
        side: str,
        idempotency_key: str,
        strategy_id: str = "",
    ) -> dict[str, Any]:
        try:
            payload = self.build_place_order_payload(
                execution_settings, side, idempotency_key, strategy_id=strategy_id
            )
        except ValueError as exc:
            return {"status": "REJECTED", "rejectionReason": str(exc)}

        order_tag = str(payload[0]["orderTag"])
        if self.contract_master is not None:
            try:
                self.contract_master.check_quantity_multiple(
                    payload[0]["exchange"],
                    payload[0]["instrumentId"],
                    int(payload[0]["quantity"]),
                )
            except ContractMasterError as exc:
                return {"status": "REJECTED", "orderTag": order_tag, "rejectionReason": exc.message}

        try:
            resp = self.http.post(
                f"{self.base_url}/orders",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.Timeout:
            reconciled = self.reconcile_by_order_tag(order_tag)
            if reconciled and reconciled.get("brokerOrderId"):
                return reconciled
            return {
                "status": "TIMEOUT_UNCONFIRMED",
                "orderTag": order_tag,
                "timedOut": True,
                "rejectionReason": "IIFL order request timed out and was not found in GET /orders; retry blocked until resolved via sync.",
            }
        except requests.RequestException as exc:
            return {
                "status": "REJECTED",
                "orderTag": order_tag,
                "rejectionReason": f"IIFL order network error: {type(exc).__name__}",
            }

        self._check_session_invalid(resp)

        try:
            body = resp.json()
        except Exception:
            return {
                "status": "REJECTED",
                "orderTag": order_tag,
                "rejectionReason": f"IIFL returned invalid JSON (HTTP {getattr(resp, 'status_code', '?')})",
            }

        self._check_session_invalid(resp, body)

        if getattr(resp, "status_code", 200) >= 400 or str(body.get("status", "")).lower() != "ok":
            err_msg = body.get("message") or f"HTTP {getattr(resp, 'status_code', '?')}"
            return {"status": "REJECTED", "orderTag": order_tag, "rejectionReason": f"IIFL rejected order: {err_msg}"}

        raw_results = body.get("result")
        results = raw_results if isinstance(raw_results, list) else ([raw_results] if isinstance(raw_results, dict) else [])
        if not results:
            return {
                "status": "REJECTED",
                "orderTag": order_tag,
                "rejectionReason": "IIFL returned an empty result array.",
            }

        item_results: list[dict[str, Any]] = []
        failed_items: list[str] = []
        broker_order_ids: list[str] = []

        for idx, res_item in enumerate(results):
            item_dict = res_item if isinstance(res_item, dict) else {}
            r_status = str(item_dict.get("status") or "")
            r_msg = str(item_dict.get("message") or "")
            r_oid = str(item_dict.get("brokerOrderId") or "").strip()
            item_results.append({
                "index": idx,
                "status": r_status,
                "message": r_msg,
                "brokerOrderId": r_oid or None,
            })
            if r_status.lower() != "success" or not r_oid:
                failed_items.append(f"item[{idx}] ({r_status or 'ERR'}): {r_msg or body.get('message') or 'Missing brokerOrderId'}")
            else:
                broker_order_ids.append(r_oid)
                self._order_instruments[r_oid] = (str(payload[0]["exchange"]), str(payload[0]["instrumentId"]))

        if failed_items:
            return {
                "status": "REJECTED",
                "orderTag": order_tag,
                "brokerOrderId": broker_order_ids[0] if broker_order_ids else None,
                "itemResults": item_results,
                "rejectionReason": "IIFL rejection: " + "; ".join(failed_items),
            }

        broker_order_id = broker_order_ids[0]

        # Never assume immediate FILL from POST /orders alone; confirm via GET /orders/lite/{brokerOrderId}
        if self.sync_after_place:
            synced = self.sync_order_status(broker_order_id)
            if synced:
                synced.setdefault("brokerOrderId", broker_order_id)
                synced["orderTag"] = order_tag
                synced["itemResults"] = item_results
                return synced

        return {
            "status": "OPEN",
            "brokerOrderId": broker_order_id,
            "orderTag": order_tag,
            "itemResults": item_results,
            "fillPrice": None,
        }

    def sync_order_status(self, broker_order_id: str) -> Optional[dict[str, Any]]:
        try:
            data = self._get_json(f"/orders/lite/{broker_order_id}")
        except requests.RequestException:
            return None
        if data is None:
            return None

        rows = data if isinstance(data, list) else (data.get("result") if isinstance(data, dict) else [])
        latest = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
        if not isinstance(latest, dict) or not latest:
            return None
        return self._parse_iifl_order_record(latest, fallback_broker_order_id=broker_order_id)

    def sync_from_history(self, broker_order_id: str) -> Optional[dict[str, Any]]:
        """Fetch GET /orders/{brokerOrderId} and parse the latest entry ordered by brokerUpdateTime."""
        try:
            history = self.get_order_history(broker_order_id)
        except requests.RequestException:
            return None
        if not history:
            return None
        latest = max(history, key=lambda r: _parse_broker_update_time(r.get("brokerUpdateTime")))
        return self._parse_iifl_order_record(latest, fallback_broker_order_id=broker_order_id)

    def modify_order(
        self,
        broker_order_id: str,
        modifications: dict[str, Any],
        current_modify_count: Optional[int] = None,
        exchange: Optional[str] = None,
        instrument_id: Optional[str] = None,
    ) -> dict[str, Any]:
        used = current_modify_count if current_modify_count is not None else self.modification_counts.get(broker_order_id, 0)
        if used >= MAX_MODIFY_PER_ORDER:
            return {"ok": False, "error": f"Modification limit reached ({MAX_MODIFY_PER_ORDER} per order)."}

        if self.contract_master is not None and "quantity" in modifications:
            stored = self._order_instruments.get(broker_order_id)
            ex_val = exchange or (stored[0] if stored else None)
            iid_val = instrument_id or (stored[1] if stored else None)
            if not ex_val or not iid_val:
                return {"ok": False, "error": "Cannot validate EC990 quantity multiple: instrumentId/exchange unknown."}
            try:
                self.contract_master.check_quantity_multiple(ex_val, iid_val, modifications["quantity"])
            except ContractMasterError as exc:
                return {"ok": False, "error": exc.message}

        payload = [modifications]
        try:
            resp = self.http.put(
                f"{self.base_url}/orders/{broker_order_id}",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.Timeout:
            return {"ok": False, "error": "IIFL modify request timed out"}
        except requests.RequestException as exc:
            return {"ok": False, "error": f"IIFL modify network error: {type(exc).__name__}"}

        self._check_session_invalid(resp)

        try:
            body = resp.json()
        except Exception:
            return {"ok": False, "error": f"Invalid response from IIFL (HTTP {getattr(resp, 'status_code', '?')})"}

        self._check_session_invalid(resp, body)

        res = body.get("result") or {}
        if getattr(resp, "status_code", 200) < 400 and str(body.get("status", "")).lower() == "ok" and str(res.get("status", "")).lower() == "success":
            new_count = used + 1
            self.modification_counts[broker_order_id] = new_count
            return {"ok": True, "brokerOrderId": str(res.get("brokerOrderId") or broker_order_id), "modificationCount": new_count}
        return {"ok": False, "error": res.get("message") or body.get("message") or "IIFL modify failed"}

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        try:
            resp = self.http.delete(
                f"{self.base_url}/orders/{broker_order_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.Timeout:
            return {"ok": False, "error": "IIFL cancel request timed out"}
        except requests.RequestException as exc:
            return {"ok": False, "error": f"IIFL cancel network error: {type(exc).__name__}"}

        self._check_session_invalid(resp)

        try:
            body = resp.json()
        except Exception:
            return {"ok": False, "error": f"Invalid response from IIFL (HTTP {getattr(resp, 'status_code', '?')})"}

        self._check_session_invalid(resp, body)

        res = body.get("result") or {}
        if getattr(resp, "status_code", 200) < 400 and str(body.get("status", "")).lower() == "ok" and str(res.get("status", "")).lower() == "success":
            return {"ok": True, "brokerOrderId": str(res.get("brokerOrderId") or broker_order_id)}
        return {"ok": False, "error": res.get("message") or body.get("message") or "IIFL cancel failed"}


def get_broker_capabilities(live_execution_enabled: bool = False) -> dict[str, Any]:
    caps = copy.deepcopy(BROKER_CAPABILITIES)
    lot_sizes = contract_master_service.get_lot_sizes_summary()
    caps["lotSizes"] = lot_sizes
    caps["lotSizeSource"] = "CONTRACT_MASTER" if lot_sizes else "UNAVAILABLE"
    caps["liveExecutionEnabled"] = bool(live_execution_enabled)
    caps["liveExecutionReason"] = (
        "Live execution enabled"
        if live_execution_enabled
        else "Live execution not connected"
    )
    return caps


def compute_strategy_version_hash(strategy: dict) -> str:
    """Compute a deterministic SHA-256 hash of a strategy's logic and risk/execution parameters.

    Excludes mutable metadata like `status`, `compiledAt`, or UI timestamps so that any substantive
    change to conditions, indicators, levels, SL, targets, trailing, re-entry, session, risk, or
    execution parameters produces a new hash and automatically invalidates prior live approval.
    """
    if not isinstance(strategy, dict):
        return ""
    core = {
        "schemaVersion": strategy.get("schemaVersion"),
        "strategyType": strategy.get("strategyType"),
        "id": strategy.get("id"),
        "session": strategy.get("session"),
        "indicators": strategy.get("indicators"),
        "levels": strategy.get("levels"),
        "rules": strategy.get("rules"),
        "stopLoss": strategy.get("stopLoss"),
        "targets": strategy.get("targets"),
        "trailing": strategy.get("trailing"),
        "reentry": strategy.get("reentry"),
        "risk": strategy.get("risk"),
        "execution": {
            k: v
            for k, v in (strategy.get("execution") or {}).items()
            if k not in ("mode", "liveRequested")
        },
    }
    serialized = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def validate_execution_settings(settings_payload: dict) -> list[str]:
    """Validate execution settings panel against `BROKER_CAPABILITIES`."""
    errors: list[str] = []
    if not isinstance(settings_payload, dict):
        return ["Execution settings must be an object."]

    broker = str(settings_payload.get("broker") or "PAPER_SIMULATOR").upper()
    if broker not in BROKER_CAPABILITIES["brokers"]:
        errors.append(f"broker: '{broker}' is not in broker_capabilities {BROKER_CAPABILITIES['brokers']}.")
        return errors

    exchange = str(settings_payload.get("exchange") or "NFO").upper()
    allowed_exchanges = BROKER_CAPABILITIES["exchanges"].get(broker, [])
    if exchange not in allowed_exchanges:
        errors.append(f"exchange: '{exchange}' is not supported by {broker}. Allowed: {allowed_exchanges}.")

    symbol = str(settings_payload.get("symbol") or "NIFTY 50").strip()
    if symbol not in BROKER_CAPABILITIES["symbols"]:
        errors.append(f"symbol: '{symbol}' is not in broker_capabilities.")

    instrument = str(settings_payload.get("instrument") or "INDEX_OPTIONS").upper()
    if instrument not in BROKER_CAPABILITIES["instruments"]:
        errors.append(f"instrument: '{instrument}' is not in broker_capabilities {BROKER_CAPABILITIES['instruments']}.")

    expiry = settings_payload.get("expiry")
    if instrument in ("INDEX_OPTIONS", "INDEX_FUTURES"):
        if expiry not in BROKER_CAPABILITIES["expiries"]:
            errors.append(f"expiry: '{expiry}' is not in broker_capabilities {BROKER_CAPABILITIES['expiries']}.")

    if instrument == "INDEX_OPTIONS":
        strike = settings_payload.get("strike")
        if strike not in BROKER_CAPABILITIES["strikes"] and not isinstance(strike, (int, float)):
            errors.append(f"strike: '{strike}' is not in broker_capabilities {BROKER_CAPABILITIES['strikes']}.")
        opt_type = str(settings_payload.get("optionType") or "").upper()
        if opt_type not in ("CE", "PE"):
            errors.append("optionType: must be 'CE' or 'PE' for INDEX_OPTIONS.")

    product = str(settings_payload.get("product") or "PAPER").upper()
    allowed_products = BROKER_CAPABILITIES["products"].get(broker, [])
    if product not in allowed_products:
        errors.append(f"product: '{product}' is not supported by {broker}. Allowed: {allowed_products}.")

    order_type = str(settings_payload.get("orderType") or "MARKET").upper()
    if order_type not in BROKER_CAPABILITIES["orderTypes"]:
        errors.append(f"orderType: '{order_type}' is not in broker_capabilities {BROKER_CAPABILITIES['orderTypes']}.")

    if order_type in ("LIMIT", "SL_LIMIT"):
        limit_p = settings_payload.get("limitPrice")
        if limit_p is None or float(limit_p) <= 0:
            errors.append(f"limitPrice: positive limitPrice is required for orderType '{order_type}'.")

    if order_type in ("SL_LIMIT", "SL_MARKET"):
        trig_p = settings_payload.get("triggerPrice")
        if trig_p is None or float(trig_p) <= 0:
            errors.append(f"triggerPrice: positive triggerPrice is required for orderType '{order_type}'.")

    validity = str(settings_payload.get("validity") or "DAY").upper()
    if validity not in BROKER_CAPABILITIES["validities"]:
        errors.append(f"validity: '{validity}' is not in broker_capabilities {BROKER_CAPABILITIES['validities']}.")

    amo = settings_payload.get("amo", False)
    if not isinstance(amo, bool) or amo not in BROKER_CAPABILITIES["amoAllowed"]:
        errors.append("amo: must be a boolean.")

    mode = str(settings_payload.get("mode") or "PAPER").upper()
    if mode not in BROKER_CAPABILITIES["modes"]:
        errors.append(f"mode: '{mode}' is not in broker_capabilities {BROKER_CAPABILITIES['modes']}.")

    qty = int(settings_payload.get("quantity") or 0)
    if qty < 1:
        errors.append("quantity: must be at least 1 lot.")

    lot_size = int(settings_payload.get("lotSize") or 0)
    if lot_size < 1:
        errors.append("lotSize: must be at least 1.")

    return errors


def evaluate_go_live_gates(
    strategy: dict,
    backtest_runs: Sequence[dict],
    paper_worker_runs: Sequence[dict],
    live_approvals: Sequence[dict],
    kill_switch_state: dict,
    iifl_status: dict,
    eval_time: Optional[Any] = None,
    min_paper_minutes: int = 0,
    min_paper_trades: int = 0,
    live_execution_enabled: bool = False,
    live_adapter_connected: bool = False,
) -> dict[str, Any]:
    """Check all live-trading safety gates for `strategy` on its exact current `versionHash`."""
    strat_id = strategy.get("id", "")
    strat_name = (strategy.get("name") or "").strip()
    version_hash = compute_strategy_version_hash(strategy)
    eval_dt_ist = parse_ist_datetime(eval_time) if eval_time is not None else datetime.now(IST)

    # 1. Strategy validation gate
    val_errors = validate_strategy(strategy)
    strategy_valid = len(val_errors) == 0

    # 2. Risk limits gate
    risk = strategy.get("risk") or {}
    exec_cfg = strategy.get("execution") or {}
    risk_limits_ok = (
        float(risk.get("maxDailyLoss", 0)) > 0
        and float(risk.get("maxCapital", 0)) > 0
        and int(risk.get("maxTradesPerDay", 0)) >= 1
        and int(risk.get("maxQuantity", 0)) >= 1
        and 1 <= int(exec_cfg.get("quantity", 1)) <= int(risk.get("maxQuantity", 0))
    )

    # 3. Successful backtest on this exact strategy versionHash
    matching_bts = [
        b for b in backtest_runs
        if b.get("strategyId") == strat_id
        and (b.get("versionHash") in (None, version_hash))
        and int((b.get("metrics") or {}).get("totalTrades", 0)) >= 1
    ]
    # Enforce exact versionHash match if stored on backtest run, or require versionHash match
    exact_bts = [
        b for b in matching_bts
        if b.get("versionHash") == version_hash
    ]
    backtest_ok = len(exact_bts) > 0

    # 4. Minimum paper-trading period / trades on this exact strategy versionHash
    exact_pw = [
        r for r in paper_worker_runs
        if r.get("strategyId") == strat_id and r.get("versionHash") == version_hash
    ]
    paper_ok = False
    paper_detail = "No paper-trading run recorded on this exact strategy version."
    for r in exact_pw:
        closed_count = len(r.get("closedTrades") or [])
        trades_today = int(r.get("tradesToday", 0))
        elapsed_min = float(r.get("paperElapsedMinutes", 0.0))
        if r.get("createdAt") and r.get("updatedAt"):
            try:
                t0 = parse_ist_datetime(r["createdAt"])
                t1 = parse_ist_datetime(r["updatedAt"])
                elapsed_min = max(elapsed_min, (t1 - t0).total_seconds() / 60.0)
            except Exception:
                pass
        if (closed_count >= min_paper_trades or trades_today >= min_paper_trades) and elapsed_min >= min_paper_minutes:
            paper_ok = True
            paper_detail = f"Verified paper run ({closed_count or trades_today} trade(s), {round(elapsed_min, 1)} min) on version {version_hash[:8]}."
            break

    # 5. Explicit typed approval on this exact strategy versionHash
    approval_rec = next(
        (a for a in live_approvals if a.get("strategyId") == strat_id),
        None,
    )
    approval_valid_for_version = bool(
        approval_rec
        and approval_rec.get("approved") is True
        and approval_rec.get("versionHash") == version_hash
    )
    version_changed_reset = bool(
        approval_rec
        and approval_rec.get("approved") is True
        and approval_rec.get("versionHash") != version_hash
    )

    # 6. Kill switch check
    kill_switch_active = bool(kill_switch_state.get("active", False))

    # 7. Market hours check
    session_cfg = strategy.get("session") or {}
    market_check = check_session_and_holiday(session_cfg, eval_dt_ist)
    market_hours_ok = bool(market_check["allowed"])

    # 8. Broker session check
    broker_configured = bool(iifl_status.get("configured", False))
    broker_session_active = bool(iifl_status.get("sessionPresent", False))

    expected_phrase = f"{REQUIRED_CONFIRMATION_PREFIX}{strat_name}"

    gates = {
        "strategyValid": {
            "passed": strategy_valid,
            "detail": "Strategy passes schema & semantic validation" if strategy_valid else "; ".join(val_errors),
        },
        "riskLimitsSet": {
            "passed": risk_limits_ok,
            "detail": (
                f"maxDailyLoss=₹{risk.get('maxDailyLoss')}, maxCapital=₹{risk.get('maxCapital')}, "
                f"maxTrades={risk.get('maxTradesPerDay')}, maxQty={risk.get('maxQuantity')}"
            ),
        },
        "successfulBacktestOnVersion": {
            "passed": backtest_ok,
            "detail": (
                f"Verified backtest on version {version_hash[:8]}"
                if backtest_ok
                else f"Run a backtest with >=1 trade on current strategy version ({version_hash[:8]})."
            ),
        },
        "minimumPaperTradingOnVersion": {
            "passed": paper_ok,
            "detail": paper_detail,
        },
        "explicitTypedConfirmationOnVersion": {
            "passed": approval_valid_for_version,
            "versionResetDetected": version_changed_reset,
            "expectedPhrase": expected_phrase,
            "detail": (
                f"Approved at {approval_rec.get('approvedAtIst')} for version {version_hash[:8]}"
                if approval_valid_for_version
                else (
                    "Strategy version changed since last approval — live approval was automatically reset!"
                    if version_changed_reset
                    else f"Type '{expected_phrase}' to approve this exact version."
                )
            ),
        },
        "killSwitchDisarmed": {
            "passed": not kill_switch_active,
            "detail": "Kill switch is disarmed" if not kill_switch_active else f"KILL SWITCH ACTIVE: {kill_switch_state.get('reason')}",
        },
        "marketHoursOpen": {
            "passed": market_hours_ok,
            "detail": "Within IST market session hours" if market_hours_ok else market_check["reason"],
        },
        "brokerSessionActive": {
            "passed": broker_configured and broker_session_active,
            "configured": broker_configured,
            "sessionActive": broker_session_active,
            "detail": (
                "IIFL Configured / Session Active"
                if (broker_configured and broker_session_active)
                else "IIFL session not active (required only when placing LIVE broker orders)."
            ),
        },
        "liveExecutionConnected": {
            "passed": bool(live_execution_enabled and live_adapter_connected),
            "detail": (
                "Live broker order adapter connected and enabled"
                if (live_execution_enabled and live_adapter_connected)
                else "Live execution not connected"
            ),
        },
    }

    session_label = (
        "IIFL Session Active"
        if (broker_configured and broker_session_active)
        else ("IIFL Configured (Session Disconnected)" if broker_configured else "IIFL Not Configured")
    )
    broker_status_label = (
        f"{session_label} · Live Adapter Active"
        if (live_execution_enabled and live_adapter_connected)
        else f"{session_label} · Live Execution Not Connected"
    )

    ready_for_approval = (
        strategy_valid
        and risk_limits_ok
        and backtest_ok
        and paper_ok
        and not kill_switch_active
    )
    live_trading_unlocked = (
        ready_for_approval
        and approval_valid_for_version
        and not kill_switch_active
        and bool(live_execution_enabled and live_adapter_connected)
    )

    return {
        "strategyId": strat_id,
        "strategyName": strat_name,
        "versionHash": version_hash,
        "readyForApproval": ready_for_approval,
        "liveTradingUnlocked": live_trading_unlocked,
        "liveExecutionEnabled": bool(live_execution_enabled and live_adapter_connected),
        "brokerStatusLabel": broker_status_label,
        "versionChangedReset": version_changed_reset,
        "expectedConfirmationPhrase": expected_phrase,
        "gates": gates,
    }


def approve_strategy_for_live(
    strategy: dict,
    confirmation_text: str,
    gate_status: dict,
    actor: str = "local",
    eval_time: Optional[Any] = None,
) -> dict[str, Any]:
    """Verify prerequisites and explicit typed confirmation to issue a version-pinned live approval."""
    eval_dt_ist = parse_ist_datetime(eval_time) if eval_time is not None else datetime.now(IST)
    strat_name = (strategy.get("name") or "").strip()
    expected = f"{REQUIRED_CONFIRMATION_PREFIX}{strat_name}"
    typed = str(confirmation_text or "").strip()

    if typed != expected and typed != FALLBACK_CONFIRMATION_PHRASE:
        raise ValueError(
            f"Typed confirmation mismatch. Please type exactly: '{expected}'"
        )

    gates = gate_status.get("gates") or {}
    failed_prereqs = []
    for key in ("strategyValid", "riskLimitsSet", "successfulBacktestOnVersion", "minimumPaperTradingOnVersion", "killSwitchDisarmed"):
        g = gates.get(key) or {}
        if not g.get("passed"):
            failed_prereqs.append(f"{key}: {g.get('detail')}")

    if failed_prereqs:
        raise ValueError("Cannot approve strategy for live trading until all gates pass: " + "; ".join(failed_prereqs))

    return {
        "strategyId": strategy.get("id"),
        "strategyName": strat_name,
        "versionHash": gate_status["versionHash"],
        "approved": True,
        "confirmationPhrase": typed,
        "actor": actor,
        "approvedAtIst": eval_dt_ist.isoformat(),
    }


def place_broker_order(
    strategy: dict,
    execution_settings: dict,
    side: str,
    idempotency_key: str,
    existing_orders: Sequence[dict],
    gate_status: dict,
    kill_switch_state: dict,
    iifl_status: dict,
    reference_price: float = 24000.0,
    eval_time: Optional[Any] = None,
    live_execution_enabled: bool = False,
    live_broker_adapter: Optional[Any] = None,
    reference_quote: Optional[dict] = None,
    max_quote_age_seconds: float = 15.0,
    now_ts: Optional[float] = None,
) -> tuple[dict[str, Any], bool]:
    """Place or simulate an order with full gate enforcement, duplicate-order protection, and rejection tracking.

    Returns `(order_record, is_duplicate)`.
    """
    eval_dt_ist = parse_ist_datetime(eval_time) if eval_time is not None else datetime.now(IST)
    eval_iso = eval_dt_ist.isoformat()
    idem_key = str(idempotency_key or "").strip()
    if not idem_key:
        raise ValueError("idempotencyKey is required for duplicate-order protection.")

    # 1. Duplicate-order protection (idempotency key per signal)
    for ord_rec in existing_orders:
        if ord_rec.get("idempotencyKey") == idem_key and ord_rec.get("strategyId") == strategy.get("id"):
            dup_view = copy.deepcopy(ord_rec)
            dup_view["duplicateBlocked"] = True
            return dup_view, True

    # 2. Validate execution settings against BROKER_CAPABILITIES
    cap_errors = validate_execution_settings(execution_settings)
    side_u = str(side or "BUY").upper()
    if side_u not in ("BUY", "SELL"):
        cap_errors.append("side: must be BUY or SELL.")
    if cap_errors:
        rejected = _build_order_record(
            strategy=strategy,
            execution_settings=execution_settings,
            side=side_u,
            idempotency_key=idem_key,
            status="REJECTED",
            rejection_reason="; ".join(cap_errors),
            fill_price=None,
            eval_iso=eval_iso,
        )
        return rejected, False

    # 3. Check global kill switch
    if kill_switch_state.get("active"):
        rejected = _build_order_record(
            strategy=strategy,
            execution_settings=execution_settings,
            side=side_u,
            idempotency_key=idem_key,
            status="REJECTED",
            rejection_reason=f"Kill switch is active: {kill_switch_state.get('reason')}",
            fill_price=None,
            eval_iso=eval_iso,
        )
        return rejected, False

    # 4. Check risk quantity cap
    risk_cfg = strategy.get("risk") or {}
    max_qty = int(risk_cfg.get("maxQuantity", 1))
    req_qty = int(execution_settings.get("quantity", 1))
    if req_qty > max_qty:
        rejected = _build_order_record(
            strategy=strategy,
            execution_settings=execution_settings,
            side=side_u,
            idempotency_key=idem_key,
            status="REJECTED",
            rejection_reason=f"Order quantity ({req_qty}) exceeds strategy risk.maxQuantity ({max_qty}).",
            fill_price=None,
            eval_iso=eval_iso,
        )
        return rejected, False

    # 5. Check Market Hours (unless AMO is enabled)
    amo = bool(execution_settings.get("amo", False))
    gates = gate_status.get("gates") or {}
    market_gate = gates.get("marketHoursOpen") or {}
    if not market_gate.get("passed") and not amo:
        rejected = _build_order_record(
            strategy=strategy,
            execution_settings=execution_settings,
            side=side_u,
            idempotency_key=idem_key,
            status="REJECTED",
            rejection_reason=f"Market closed ({market_gate.get('detail')}) and AMO is false.",
            fill_price=None,
            eval_iso=eval_iso,
        )
        return rejected, False

    mode = str(execution_settings.get("mode") or "PAPER").upper()
    if mode == "LIVE":
        # Phase 1 safety gate: block LIVE orders unless a verified broker adapter is explicitly enabled
        if not live_execution_enabled or live_broker_adapter is None:
            rejected = _build_order_record(
                strategy=strategy,
                execution_settings=execution_settings,
                side=side_u,
                idempotency_key=idem_key,
                status="REJECTED",
                rejection_reason="Live execution not connected",
                fill_price=None,
                eval_iso=eval_iso,
            )
            return rejected, False

        # Enforce all Live Trading Gates!
        if not gate_status.get("liveTradingUnlocked"):
            failed_reasons = [
                f"{k} ({v.get('detail')})"
                for k, v in gates.items()
                if k != "marketHoursOpen" and k != "brokerSessionActive" and not v.get("passed")
            ]
            rejected = _build_order_record(
                strategy=strategy,
                execution_settings=execution_settings,
                side=side_u,
                idempotency_key=idem_key,
                status="REJECTED",
                rejection_reason="Live trading gates not satisfied: " + (", ".join(failed_reasons) or "unapproved strategy version"),
                fill_price=None,
                eval_iso=eval_iso,
            )
            return rejected, False

        if not (iifl_status.get("configured") and iifl_status.get("sessionPresent")):
            rejected = _build_order_record(
                strategy=strategy,
                execution_settings=execution_settings,
                side=side_u,
                idempotency_key=idem_key,
                status="REJECTED",
                rejection_reason="IIFL broker session is not active. Connect IIFL session before placing LIVE orders.",
                fill_price=None,
                eval_iso=eval_iso,
            )
            return rejected, False

        slippage_err = check_price_band_slippage(
            execution_settings,
            reference_price,
            risk_cfg=risk_cfg,
            reference_quote=reference_quote,
            max_quote_age_seconds=max_quote_age_seconds,
            now_ts=now_ts,
        )
        if slippage_err:
            rejected = _build_order_record(
                strategy=strategy,
                execution_settings=execution_settings,
                side=side_u,
                idempotency_key=idem_key,
                status="REJECTED",
                rejection_reason=slippage_err,
                fill_price=None,
                eval_iso=eval_iso,
            )
            return rejected, False

        # Never mark LIVE orders FILLED without a real broker confirmation from the adapter
        adapter_res = live_broker_adapter(
            strategy=strategy,
            execution_settings=execution_settings,
            side=side_u,
            idempotency_key=idem_key,
            reference_price=reference_price,
            eval_iso=eval_iso,
        )
        order_rec = _build_order_record(
            strategy=strategy,
            execution_settings=execution_settings,
            side=side_u,
            idempotency_key=idem_key,
            status=adapter_res.get("status", "OPEN"),
            rejection_reason=adapter_res.get("rejectionReason"),
            fill_price=adapter_res.get("fillPrice"),
            eval_iso=eval_iso,
        )
        for extra_key in (
            "brokerOrderId",
            "exchangeOrderId",
            "orderTag",
            "filledQuantity",
            "pendingQuantity",
            "cancelledQuantity",
            "averageTradedPrice",
            "itemResults",
            "reconciledFromOrderBook",
            "timedOut",
        ):
            if extra_key in adapter_res and adapter_res[extra_key] is not None:
                order_rec[extra_key] = adapter_res[extra_key]
        return order_rec, False

    # PAPER mode only: MARKET orders fill immediately at reference_price (unless AMO);
    # LIMIT / SL_LIMIT / SL_MARKET or AMO orders start in OPEN status so they can be tracked or cancelled.
    order_type = str(execution_settings.get("orderType") or "MARKET").upper()
    if amo or order_type in ("LIMIT", "SL_LIMIT", "SL_MARKET"):
        ord_status = "OPEN"
        fill_p = None
    else:
        ord_status = "FILLED"
        fill_p = round(float(reference_price), 2)

    order_rec = _build_order_record(
        strategy=strategy,
        execution_settings=execution_settings,
        side=side_u,
        idempotency_key=idem_key,
        status=ord_status,
        rejection_reason=None,
        fill_price=fill_p,
        eval_iso=eval_iso,
    )
    return order_rec, False


def _build_order_record(
    strategy: dict,
    execution_settings: dict,
    side: str,
    idempotency_key: str,
    status: str,
    rejection_reason: Optional[str],
    fill_price: Optional[float],
    eval_iso: str,
) -> dict[str, Any]:
    sym = str(execution_settings.get("symbol") or "NIFTY 50")
    inst = str(execution_settings.get("instrument") or "INDEX_OPTIONS")
    strike = execution_settings.get("strike")
    opt_type = execution_settings.get("optionType")
    expiry = execution_settings.get("expiry")
    if inst == "INDEX_OPTIONS":
        contract = f"{sym} {expiry} {strike} {opt_type}"
    elif inst == "INDEX_FUTURES":
        contract = f"{sym} {expiry} FUT"
    else:
        contract = f"{sym} ({inst})"

    qty = int(execution_settings.get("quantity", 1))
    raw_lot = execution_settings.get("lotSize")
    lot_size = int(raw_lot) if raw_lot is not None else 0
    return {
        "id": f"ord-{uuid4().hex[:10]}",
        "strategyId": strategy.get("id"),
        "strategyName": strategy.get("name"),
        "versionHash": compute_strategy_version_hash(strategy),
        "idempotencyKey": idempotency_key,
        "orderTag": make_order_tag(str(strategy.get("id") or ""), idempotency_key),
        "duplicateBlocked": False,
        "broker": str(execution_settings.get("broker") or "PAPER_SIMULATOR").upper(),
        "exchange": str(execution_settings.get("exchange") or "NFO").upper(),
        "symbol": sym,
        "instrument": inst,
        "expiry": expiry,
        "strike": strike,
        "optionType": opt_type,
        "contract": contract,
        "side": side,
        "quantity": qty,
        "lotSize": lot_size,
        "totalUnits": qty * lot_size,
        "product": str(execution_settings.get("product") or "PAPER").upper(),
        "orderType": str(execution_settings.get("orderType") or "MARKET").upper(),
        "validity": str(execution_settings.get("validity") or "DAY").upper(),
        "amo": bool(execution_settings.get("amo", False)),
        "mode": str(execution_settings.get("mode") or "PAPER").upper(),
        "limitPrice": execution_settings.get("limitPrice"),
        "triggerPrice": execution_settings.get("triggerPrice"),
        "fillPrice": fill_price,
        "status": status,  # OPEN | FILLED | REJECTED | CANCELLED
        "rejectionReason": rejection_reason,
        "createdAtIst": eval_iso,
        "updatedAtIst": eval_iso,
    }


def compute_broker_positions_and_pnl(
    orders: Sequence[dict],
    ltp_by_symbol: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """Compute net open positions, realized P&L, and unrealized P&L across executed broker orders.

    Includes FILLED, PARTIALLY_FILLED, and CANCELLED orders whenever filledQuantity > 0.
    """
    ltp_map = ltp_by_symbol or {}
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}

    commodity_segments = {"MCXCOMM", "NSECOMM", "BSECOMM", "NCDEXCOMM"}

    # Orders are stored newest-first; process oldest-first so average-cost / realized P&L are chronological
    for o in reversed(list(orders or [])):
        st = str(o.get("status") or "").upper()
        if st not in ("FILLED", "PARTIALLY_FILLED", "CANCELLED"):
            continue
        exchange = str(o.get("exchange") or o.get("exchangeSegment") or "").strip().upper()
        raw_filled = _safe_int(o.get("filledQuantity"), 0)
        if raw_filled > 0:
            if exchange in commodity_segments:
                lot_size = _safe_int(o.get("lotSize"), 0)
                if lot_size <= 0:
                    raise ValueError(
                        f"Commodity order {o.get('id') or o.get('symbol') or ''} ({exchange}) requires positive lotSize."
                    )
                filled_qty = raw_filled * lot_size
            else:
                filled_qty = raw_filled
        elif st == "FILLED":
            total_u = _safe_int(o.get("totalUnits"), 0)
            if total_u > 0:
                if exchange in commodity_segments and _safe_int(o.get("lotSize"), 0) > 1 and total_u == _safe_int(o.get("quantity"), 0):
                    filled_qty = total_u * _safe_int(o.get("lotSize"), 0)
                else:
                    filled_qty = total_u
            else:
                qty = _safe_int(o.get("quantity"), 0)
                if exchange in commodity_segments:
                    lot_size = _safe_int(o.get("lotSize"), 0)
                    if lot_size <= 0:
                        raise ValueError(
                            f"Commodity order {o.get('id') or o.get('symbol') or ''} ({exchange}) requires positive lotSize."
                        )
                    filled_qty = qty * lot_size
                else:
                    filled_qty = qty
        else:
            filled_qty = 0

        if filled_qty <= 0:
            continue

        avg_p = _safe_pos_float(o.get("averageTradedPrice")) or _safe_pos_float(o.get("fillPrice"))
        if avg_p is None:
            continue

        strat_id = str(o.get("strategyId") or "")
        sym = str(o.get("symbol") or "")
        contract = str(o.get("contract") or sym)
        key = (strat_id, sym, contract)
        if key not in buckets:
            buckets[key] = {
                "strategyId": strat_id,
                "symbol": sym,
                "contract": contract,
                "netUnits": 0,
                "avgEntryPrice": 0.0,
                "realizedPnl": 0.0,
                "unrealizedPnl": 0.0,
                "filledOrderCount": 0,
                "filledUnitsTotal": 0,
            }
        b = buckets[key]
        b["filledOrderCount"] += 1
        b["filledUnitsTotal"] += filled_qty
        side = str(o.get("side") or "BUY").upper()
        signed_qty = filled_qty if side == "BUY" else -filled_qty

        net = b["netUnits"]
        if net == 0 or (net > 0 and signed_qty > 0) or (net < 0 and signed_qty < 0):
            new_abs = abs(net) + filled_qty
            b["avgEntryPrice"] = ((abs(net) * b["avgEntryPrice"]) + (filled_qty * avg_p)) / float(new_abs)
            b["netUnits"] = net + signed_qty
        else:
            closing_units = min(abs(net), filled_qty)
            if net > 0:
                b["realizedPnl"] += (avg_p - b["avgEntryPrice"]) * closing_units
            else:
                b["realizedPnl"] += (b["avgEntryPrice"] - avg_p) * closing_units
            rem = filled_qty - closing_units
            if rem > 0:
                b["netUnits"] = rem if signed_qty > 0 else -rem
                b["avgEntryPrice"] = avg_p
            else:
                b["netUnits"] = net + signed_qty
                if b["netUnits"] == 0:
                    b["avgEntryPrice"] = 0.0

    positions: list[dict[str, Any]] = []
    total_realized = 0.0
    total_unrealized = 0.0
    for b in buckets.values():
        ltp = ltp_map.get(b["contract"]) or ltp_map.get(b["symbol"])
        if b["netUnits"] != 0 and ltp is not None and float(ltp) > 0:
            fltp = float(ltp)
            if b["netUnits"] > 0:
                b["unrealizedPnl"] = round((fltp - b["avgEntryPrice"]) * b["netUnits"], 2)
            else:
                b["unrealizedPnl"] = round((b["avgEntryPrice"] - fltp) * abs(b["netUnits"]), 2)
        b["avgEntryPrice"] = round(b["avgEntryPrice"], 2)
        b["realizedPnl"] = round(b["realizedPnl"], 2)
        total_realized += b["realizedPnl"]
        total_unrealized += b["unrealizedPnl"]
        positions.append(b)

    return {
        "positions": positions,
        "totalRealizedPnl": round(total_realized, 2),
        "totalUnrealizedPnl": round(total_unrealized, 2),
    }
