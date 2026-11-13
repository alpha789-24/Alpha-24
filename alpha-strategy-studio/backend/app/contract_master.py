"""IIFL Contract Master Service (Part 2).

Downloads, caches on disk (once per IST trading day), indexes in memory, and
queries IIFL instrument master files (`NSEEQ`, `NSEFO`, `INDICES`, plus other
configured segments). Never guesses or falls back to hardcoded lot sizes.
"""
from __future__ import annotations

import csv
import io
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, Sequence

import requests

IST = timezone(timedelta(hours=5, minutes=30))

CONTRACT_BASE_URL = "https://api.iiflcapital.com/v1/contractfiles"
DEFAULT_SEGMENTS: tuple[str, ...] = ("NSEEQ", "NSEFO", "INDICES")
ALLOWED_CONTRACT_SEGMENTS: set[str] = {
    "NSEEQ", "NSEFO", "INDICES", "BSEEQ", "BSEFO", "NSECURR", "BSECURR", "MCXCOMM", "NSECOMM",
}
UNSUPPORTED_CONTRACT_SEGMENTS: set[str] = {"NCDEXCOMM", "BSECOMM"}

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "contract_cache"

# CSV -> canonical JSON field name aliases across NSEEQ.csv, NSEFO.csv, INDICES.csv
CSV_COLUMN_ALIASES: dict[str, str] = {
    "Exchange": "exchange",
    "Underlying Instrument Symbol": "underlyingInstrumentSymbol",
    "Instrument ID": "instrumentId",
    "Instrument Type": "instrumentType",
    "Option Type": "optionType",
    "Strike Price": "strikePrice",
    "Underlying Instrument Name": "underlyingInstrumentName",
    "Formatted Instrument Name": "formattedInstrumentName",
    "Formatted Ins Name": "formattedInstrumentName",
    "Trading Symbol": "tradingSymbol",
    "Series": "series",
    "qtyLimit": "qtyLimit",
    "Expiry": "expiry",
    "Lot Size": "lotSize",
    "Tick Size": "tickSize",
}

_FUT_OPTION_ALIASES = {"", "XX", "FUT", "FUTURE", "FUTURES", "NA"}


class ContractMasterError(RuntimeError):
    """Raised when contract master is missing, stale, unsupported, ambiguous, or not found."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _safe_pos_int(val: Any) -> Optional[int]:
    s = str(val or "").strip()
    if not s:
        return None
    try:
        n = int(Decimal(s))
        return n if n > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _safe_float(val: Any) -> Optional[float]:
    s = str(val or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def normalize_strike(val: Any) -> str:
    """Normalize strike price so '207.5', '207.50', and 207.5 match identically."""
    s = str(val or "").strip()
    if not s:
        return ""
    try:
        d = Decimal(s).normalize()
        txt = format(d, "f")
        if "." in txt:
            txt = txt.rstrip("0").rstrip(".")
        return txt
    except (InvalidOperation, ValueError):
        return s


def normalize_expiry_date(val: Any) -> str:
    """Parse expiry string (e.g. '29-Dec-2026 23:59' or '2026-12-29') to 'YYYY-MM-DD'."""
    s = str(val or "").strip()
    if not s:
        return ""
    for fmt in (
        "%d-%b-%Y %H:%M",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def normalize_option_type(val: Any, instrument_type: str = "") -> str:
    """Normalize optionType: CE/PE stay CE/PE; XX or blank on futures becomes 'FUT'."""
    raw = str(val or "").strip().upper()
    inst = str(instrument_type or "").strip().upper()
    if raw in ("CE", "PE"):
        return raw
    if raw == "XX" or inst.startswith("FUT"):
        return "FUT"
    return "" if raw in _FUT_OPTION_ALIASES else raw


def normalize_contract_row(raw: dict[str, Any], default_segment: str = "") -> dict[str, Any]:
    """Normalize either a JSON contract object or a CSV DictReader row into canonical shape."""
    mapped: dict[str, Any] = {}
    for k, v in raw.items():
        canon = CSV_COLUMN_ALIASES.get(k, k)
        mapped[canon] = v

    seg = str(mapped.get("exchange") or default_segment or "").strip().upper()
    inst_id = str(mapped.get("instrumentId") or "").strip()
    inst_type = str(mapped.get("instrumentType") or "").strip().upper()
    underlying = str(mapped.get("underlyingInstrumentSymbol") or "").strip().upper()
    underlying_name = str(mapped.get("underlyingInstrumentName") or "").strip()
    trading_sym = str(mapped.get("tradingSymbol") or "").strip()
    formatted_name = str(mapped.get("formattedInstrumentName") or "").strip()
    series = str(mapped.get("series") or "").strip().upper()
    expiry_date = normalize_expiry_date(mapped.get("expiry"))
    strike_norm = normalize_strike(mapped.get("strikePrice"))
    opt_raw = str(mapped.get("optionType") or "").strip().upper()
    opt_norm = normalize_option_type(opt_raw, inst_type)
    lot_size = _safe_pos_int(mapped.get("lotSize"))
    tick_size = _safe_float(mapped.get("tickSize"))
    qty_limit = _safe_pos_int(mapped.get("qtyLimit"))

    return {
        "segment": default_segment.upper() if default_segment else seg,
        "exchange": seg,
        "instrumentId": inst_id,
        "underlyingInstrumentSymbol": underlying,
        "underlyingInstrumentName": underlying_name,
        "instrumentType": inst_type,
        "optionType": opt_raw,
        "normalizedOptionType": opt_norm,
        "strikePrice": strike_norm,
        "expiry": expiry_date,
        "tradingSymbol": trading_sym,
        "formattedInstrumentName": formatted_name,
        "series": series,
        "lotSize": lot_size,
        "tickSize": tick_size,
        "qtyLimit": qty_limit,
    }


@dataclass
class SegmentCacheState:
    segment: str
    downloaded_date_ist: str
    downloaded_at: float
    row_count: int


class ContractMasterService:
    """In-memory indexed, disk-cached IIFL contract master service."""

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        http_client: Any = requests,
        base_url: str = CONTRACT_BASE_URL,
        min_request_interval_sec: float = 0.5,  # 2 requests/sec per segment
        now_fn: Any = None,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.http = http_client
        self.base_url = base_url.rstrip("/")
        self.min_request_interval_sec = float(min_request_interval_sec)
        self._now_fn = now_fn or time.time
        self._lock = threading.RLock()
        self._last_download_ts: dict[str, float] = {}
        self._meta: dict[str, SegmentCacheState] = {}
        self._by_id: dict[tuple[str, str], dict[str, Any]] = {}
        self._by_spec: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = {}
        self._rows_by_segment: dict[str, list[dict[str, Any]]] = {}

    def _today_ist(self) -> str:
        return datetime.fromtimestamp(float(self._now_fn()), tz=IST).strftime("%Y-%m-%d")

    @staticmethod
    def _validate_segment(segment: str) -> str:
        seg = str(segment or "").strip().upper()
        if seg in UNSUPPORTED_CONTRACT_SEGMENTS:
            raise ContractMasterError(
                f"Segment '{seg}' has no IIFL contract master file and is not supported.",
                status_code=400,
            )
        if seg not in ALLOWED_CONTRACT_SEGMENTS:
            raise ContractMasterError(
                f"Unsupported contract master segment '{seg}'.",
                status_code=400,
            )
        return seg

    def _cache_paths(self, segment: str) -> tuple[Path, Path]:
        return (
            self.cache_dir / f"{segment}.json",
            self.cache_dir / f"{segment}.meta.json",
        )

    def _enforce_rate_limit(self, segment: str) -> None:
        last_ts = self._last_download_ts.get(segment)
        if last_ts is not None and self.min_request_interval_sec > 0:
            elapsed = float(self._now_fn()) - last_ts
            if elapsed < self.min_request_interval_sec:
                time.sleep(self.min_request_interval_sec - elapsed)
        self._last_download_ts[segment] = float(self._now_fn())

    def _index_rows(self, segment: str, raw_rows: list[dict[str, Any]], meta: SegmentCacheState) -> None:
        # Remove prior entries for this segment
        old_rows = self._rows_by_segment.get(segment, [])
        for r in old_rows:
            self._by_id.pop((segment, r["instrumentId"]), None)
            spec_key = (
                segment,
                r["underlyingInstrumentSymbol"],
                r["instrumentType"],
                r["expiry"],
                r["strikePrice"],
                r["normalizedOptionType"],
            )
            self._by_spec.pop(spec_key, None)

        normalized_list: list[dict[str, Any]] = []
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            rec = normalize_contract_row(raw, default_segment=segment)
            if not rec["instrumentId"]:
                continue
            normalized_list.append(rec)
            self._by_id[(segment, rec["instrumentId"])] = rec
            spec_key = (
                segment,
                rec["underlyingInstrumentSymbol"],
                rec["instrumentType"],
                rec["expiry"],
                rec["strikePrice"],
                rec["normalizedOptionType"],
            )
            self._by_spec.setdefault(spec_key, []).append(rec)

        self._rows_by_segment[segment] = normalized_list
        self._meta[segment] = meta

    def load_rows_into_cache(
        self,
        segment: str,
        raw_rows: list[dict[str, Any]],
        downloaded_date_ist: Optional[str] = None,
        persist: bool = True,
    ) -> int:
        """Load already-parsed JSON or CSV rows (used by downloads and offline fixture tests)."""
        seg = self._validate_segment(segment)
        date_str = downloaded_date_ist or self._today_ist()
        meta = SegmentCacheState(
            segment=seg,
            downloaded_date_ist=date_str,
            downloaded_at=float(self._now_fn()),
            row_count=len(raw_rows),
        )
        with self._lock:
            if persist:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                data_path, meta_path = self._cache_paths(seg)
                data_path.write_text(json.dumps(raw_rows), encoding="utf-8")
                meta_path.write_text(
                    json.dumps(
                        {
                            "segment": seg,
                            "downloadedDateIst": meta.downloaded_date_ist,
                            "downloadedAt": meta.downloaded_at,
                            "rowCount": meta.row_count,
                        }
                    ),
                    encoding="utf-8",
                )
            self._index_rows(seg, raw_rows, meta)
        return len(self._rows_by_segment[seg])

    def load_csv_into_cache(
        self,
        segment: str,
        csv_text: str,
        downloaded_date_ist: Optional[str] = None,
        persist: bool = True,
    ) -> int:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = [dict(row) for row in reader]
        return self.load_rows_into_cache(segment, rows, downloaded_date_ist=downloaded_date_ist, persist=persist)

    def load_from_disk(self, segment: str) -> bool:
        """Load cached segment from disk into memory if present. Returns True if loaded."""
        seg = self._validate_segment(segment)
        data_path, meta_path = self._cache_paths(seg)
        if not data_path.exists() or not meta_path.exists():
            return False
        try:
            meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
            rows_raw = json.loads(data_path.read_text(encoding="utf-8"))
            if not isinstance(rows_raw, list):
                return False
            meta = SegmentCacheState(
                segment=seg,
                downloaded_date_ist=str(meta_raw.get("downloadedDateIst") or ""),
                downloaded_at=float(meta_raw.get("downloadedAt") or 0.0),
                row_count=len(rows_raw),
            )
            with self._lock:
                self._index_rows(seg, rows_raw, meta)
            return True
        except Exception:
            return False

    def is_fresh(self, segment: str) -> bool:
        seg = self._validate_segment(segment)
        with self._lock:
            if seg not in self._meta:
                self.load_from_disk(seg)
            meta = self._meta.get(seg)
            if not meta or not self._rows_by_segment.get(seg):
                return False
            return meta.downloaded_date_ist == self._today_ist()

    def _ensure_fresh_loaded(self, segment: str) -> str:
        seg = self._validate_segment(segment)
        with self._lock:
            if seg not in self._meta:
                self.load_from_disk(seg)
            meta = self._meta.get(seg)
            if not meta or not self._rows_by_segment.get(seg):
                raise ContractMasterError(
                    f"Contract master cache for {seg} is missing; refresh required before lookup.",
                    status_code=503,
                )
            today = self._today_ist()
            if meta.downloaded_date_ist != today:
                raise ContractMasterError(
                    f"Contract master cache for {seg} is stale ({meta.downloaded_date_ist} != {today}); refresh required.",
                    status_code=503,
                )
            return seg

    def download_segment(self, segment: str, force: bool = False) -> int:
        """Download {SEGMENT}.json (falling back to .csv) obeying 2 req/sec per segment."""
        seg = self._validate_segment(segment)
        with self._lock:
            if not force and self.is_fresh(seg):
                return len(self._rows_by_segment.get(seg, []))
            self._enforce_rate_limit(seg)

        url_json = f"{self.base_url}/{seg}.json"
        resp = self.http.get(url_json, timeout=30)
        status = getattr(resp, "status_code", 0)
        if status == 200:
            payload = resp.json()
            rows = payload.get("result") if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise ContractMasterError(f"Unexpected JSON structure from {url_json}", status_code=502)
            return self.load_rows_into_cache(seg, rows, persist=True)

        # CSV fallback if JSON endpoint fails
        with self._lock:
            self._enforce_rate_limit(seg)
        url_csv = f"{self.base_url}/{seg}.csv"
        resp_csv = self.http.get(url_csv, timeout=30)
        if getattr(resp_csv, "status_code", 0) != 200:
            raise ContractMasterError(
                f"Failed to download {seg} contract file (JSON HTTP {status}, CSV HTTP {getattr(resp_csv, 'status_code', '?')}).",
                status_code=502,
            )
        return self.load_csv_into_cache(seg, resp_csv.text, persist=True)

    def refresh_if_stale(self, segments: Sequence[str] = DEFAULT_SEGMENTS) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for seg in segments:
            try:
                count = self.download_segment(seg, force=False)
                results[seg] = {"ok": True, "count": count}
            except Exception as exc:
                results[seg] = {"ok": False, "error": str(exc)}
        return results

    def get_by_instrument_id(self, segment: str, instrument_id: str) -> dict[str, Any]:
        seg = self._ensure_fresh_loaded(segment)
        iid = str(instrument_id or "").strip()
        if not iid:
            raise ContractMasterError("instrumentId is required.", status_code=400)
        with self._lock:
            rec = self._by_id.get((seg, iid))
        if not rec:
            raise ContractMasterError(
                f"Instrument '{iid}' not found in {seg} contract master.",
                status_code=404,
            )
        today = self._today_ist()
        if rec.get("expiry") and rec["expiry"] < today:
            raise ContractMasterError(
                f"Contract {rec['tradingSymbol']} ({seg}/{iid}) expired on {rec['expiry']} (before {today}).",
                status_code=400,
            )
        return dict(rec)

    def lookup_contract(
        self,
        segment: str,
        underlying: str,
        instrument_type: Optional[str] = None,
        expiry: Optional[str] = None,
        strike: Any = None,
        option_type: Optional[str] = None,
        series: Optional[str] = None,
    ) -> dict[str, Any]:
        """Exact contract lookup returning instrumentId, lotSize, tickSize, tradingSymbol."""
        seg = self._ensure_fresh_loaded(segment)
        u_sym = str(underlying or "").strip().upper()
        if not u_sym:
            raise ContractMasterError("underlying symbol is required.", status_code=400)

        exp_norm = normalize_expiry_date(expiry) if expiry else ""
        today = self._today_ist()
        if exp_norm and exp_norm < today:
            raise ContractMasterError(
                f"Expiry {exp_norm} is in the past (before {today}).",
                status_code=400,
            )
        strike_norm = normalize_strike(strike) if strike not in (None, "") else ""
        inst_norm = str(instrument_type or "").strip().upper()
        opt_norm = normalize_option_type(option_type, inst_norm) if option_type is not None else None
        series_norm = str(series or "").strip().upper()

        with self._lock:
            candidates = [
                r for r in self._rows_by_segment.get(seg, [])
                if r["underlyingInstrumentSymbol"] == u_sym
            ]

        if inst_norm:
            candidates = [r for r in candidates if r["instrumentType"] == inst_norm]
        if exp_norm:
            candidates = [r for r in candidates if r["expiry"] == exp_norm]
        if strike_norm:
            candidates = [r for r in candidates if r["strikePrice"] == strike_norm]
        if opt_norm is not None:
            candidates = [r for r in candidates if r["normalizedOptionType"] == opt_norm]
        if series_norm:
            candidates = [r for r in candidates if r["series"] == series_norm]
        elif seg == "NSEEQ" and len(candidates) > 1:
            # Prefer EQ series only if caller didn't specify series AND multiple series exist?
            # Wait: refuse ambiguous if caller didn't disambiguate, OR require series if multiple!
            pass

        if not candidates:
            raise ContractMasterError(
                f"No matching contract in {seg} for underlying={u_sym}, type={inst_norm or '*'}, "
                f"expiry={exp_norm or '*'}, strike={strike_norm or '*'}, option={opt_norm if opt_norm is not None else '*'}.",
                status_code=404,
            )
        active_candidates = [r for r in candidates if not r.get("expiry") or r["expiry"] >= today]
        if not active_candidates:
            raise ContractMasterError(
                f"Matching contract(s) in {seg} for {u_sym} are expired (before {today}).",
                status_code=400,
            )
        candidates = active_candidates
        if len(candidates) > 1:
            raise ContractMasterError(
                f"Ambiguous contract lookup in {seg} for {u_sym}: matched {len(candidates)} contracts. "
                "Specify expiry, strike, optionType, instrumentType, or series.",
                status_code=409,
            )
        return dict(candidates[0])

    def get_lot_size(
        self,
        segment: str,
        underlying: Optional[str] = None,
        instrument_id: Optional[str] = None,
        instrument_type: Optional[str] = None,
        expiry: Optional[str] = None,
        strike: Any = None,
        option_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Resolve exact contract lotSize; refuses if missing, stale, or ambiguous."""
        if instrument_id:
            rec = self.get_by_instrument_id(segment, instrument_id)
        else:
            rec = self.lookup_contract(
                segment=segment,
                underlying=underlying or "",
                instrument_type=instrument_type,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
            )
        if rec.get("lotSize") is None:
            raise ContractMasterError(
                f"Contract {rec['segment']}/{rec['instrumentId']} ({rec['tradingSymbol']}) has no lotSize.",
                status_code=400,
            )
        return {
            "segment": rec["segment"],
            "instrumentId": rec["instrumentId"],
            "tradingSymbol": rec["tradingSymbol"],
            "lotSize": int(rec["lotSize"]),
            "tickSize": rec.get("tickSize"),
        }

    def check_quantity_multiple(self, segment: str, instrument_id: str, quantity: Any) -> int:
        """Local EC954/EC990 pre-check: quantity must be a positive integer multiple of lotSize."""
        info = self.get_lot_size(segment=segment, instrument_id=instrument_id)
        lot_size = info["lotSize"]
        try:
            qty_int = int(quantity)
            if isinstance(quantity, float) and not quantity.is_integer():
                raise ValueError("non-integer")
        except (TypeError, ValueError):
            raise ContractMasterError(
                f"EC949: quantity '{quantity}' must be a positive integer.",
                status_code=400,
            )
        if qty_int <= 0:
            raise ContractMasterError(
                f"EC949: quantity ({qty_int}) must be a positive integer.",
                status_code=400,
            )
        if qty_int % lot_size != 0:
            raise ContractMasterError(
                f"EC954/EC990: quantity ({qty_int}) must be a multiple of lotSize ({lot_size}) for {info['tradingSymbol']}.",
                status_code=400,
            )
        return lot_size

    def get_lot_sizes_summary(self, segment: str = "NSEFO") -> dict[str, int]:
        """Return representative lot sizes from loaded contract master, or {} if unavailable/stale."""
        try:
            seg = self._ensure_fresh_loaded(segment)
        except ContractMasterError:
            return {}
        summary: dict[str, int] = {}
        with self._lock:
            for r in self._rows_by_segment.get(seg, []):
                u = r["underlyingInstrumentSymbol"]
                ls = r.get("lotSize")
                if u and ls and u not in summary:
                    summary[u] = int(ls)
        return summary

    def search(self, query: str, segment: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        """Fast in-memory search by tradingSymbol, underlying, or formattedInstrumentName."""
        q = str(query or "").strip().lower()
        if len(q) > 64:
            raise ContractMasterError("Search query exceeds maximum length of 64 characters.", status_code=400)
        if len(q) < 2:
            return []
        cap = max(1, min(int(limit or 20), 50))
        today = self._today_ist()
        segments = [self._validate_segment(segment)] if segment else list(DEFAULT_SEGMENTS)

        available_rows: list[dict[str, Any]] = []
        any_fresh = False
        for seg in segments:
            if self.is_fresh(seg):
                any_fresh = True
                with self._lock:
                    available_rows.extend(self._rows_by_segment.get(seg, []))
        if not any_fresh:
            raise ContractMasterError(
                "Contract master cache is missing or stale; refresh required for search.",
                status_code=503,
            )

        prefix_hits: list[dict[str, Any]] = []
        sub_hits: list[dict[str, Any]] = []
        for r in available_rows:
            if r.get("expiry") and r["expiry"] < today:
                continue
            sym_l = r["tradingSymbol"].lower()
            und_l = r["underlyingInstrumentSymbol"].lower()
            name_l = r["formattedInstrumentName"].lower()
            if und_l.startswith(q) or sym_l.startswith(q):
                prefix_hits.append(r)
            elif q in sym_l or q in und_l or q in name_l:
                sub_hits.append(r)

        combined = (prefix_hits + sub_hits)[:cap]
        return [
            {
                "segment": r["segment"],
                "exchange": r["exchange"],
                "instrumentId": r["instrumentId"],
                "symbol": r["tradingSymbol"],
                "cleanSymbol": r["underlyingInstrumentSymbol"] or r["tradingSymbol"],
                "name": r["formattedInstrumentName"] or r["underlyingInstrumentName"] or r["tradingSymbol"],
                "instrumentType": r["instrumentType"],
                "series": r["series"],
                "expiry": r["expiry"],
                "strikePrice": r["strikePrice"],
                "optionType": r["optionType"],
                "lotSize": r["lotSize"],
                "tickSize": r["tickSize"],
                "type": r["instrumentType"] or "EQUITY",
            }
            for r in combined
        ]


contract_master_service = ContractMasterService()

