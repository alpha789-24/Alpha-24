r"""Standalone read-only IIFL Market Data verification script (Part 3a).

Reads the session token ONLY from the `IIFL_SESSION_TOKEN` environment variable.
Never reads the token from a file, never prints/logs/saves the token, and
never calls any order, modify, cancel, or conversion endpoint.

Usage (PowerShell):
  $env:IIFL_SESSION_TOKEN = "<your-session-token>"
  backend\.venv\Scripts\python.exe tools\verify_iifl_market_data.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.contract_master import ContractMasterService, IST
from app.iifl_market_data import (
    IIFLMarketDataClient,
    IIFLMarketDataError,
    format_iifl_date,
)
from app.broker_execution import BrokerSessionExpiredError


def _mask_token(tok: str) -> str:
    if len(tok) <= 8:
        return "****"
    return f"{tok[:2]}...{tok[-2:]} (len={len(tok)})"


def main() -> int:
    token = os.environ.get("IIFL_SESSION_TOKEN", "").strip()
    if not token:
        print("ERROR: IIFL_SESSION_TOKEN environment variable is not set.", file=sys.stderr)
        return 2

    print(f"Using IIFL_SESSION_TOKEN: {_mask_token(token)}")
    cm = ContractMasterService()
    print("Refreshing contract master (NSEEQ, NSEFO, INDICES) if stale...")
    cm.refresh_if_stale(("NSEEQ", "NSEFO", "INDICES"))

    client = IIFLMarketDataClient(session_token=token)

    # 1. One NSEEQ stock (RELIANCE or first EQ stock)
    eq_hits = cm.search("RELIANCE", segment="NSEEQ", limit=5)
    eq_pick = next((h for h in eq_hits if h.get("series") == "EQ"), eq_hits[0])
    print(f"\n[1] NSEEQ marketquotes for {eq_pick['symbol']} (instrumentId={eq_pick['instrumentId']}):")
    try:
        q_eq = client.get_market_quotes([{"exchange": "NSEEQ", "instrumentId": eq_pick["instrumentId"]}])
        if q_eq:
            row = q_eq[0]
            print(f"  HTTP 200 | fields={sorted(k for k in row.keys() if k != 'tickDatetimeIst')}")
            print(f"  ltp={row.get('ltp')}, close={row.get('close')}, tickTimestamp={row.get('tickTimestamp')}")
    except (IIFLMarketDataError, BrokerSessionExpiredError) as exc:
        print(f"  FAILED: {exc}")

    # 2. NIFTY 50 from INDICES across plausible exchange values
    idx_rec = cm.lookup_contract("INDICES", "NIFTY 50")
    idx_id = idx_rec["instrumentId"]
    print(f"\n[2] INDICES NIFTY 50 (instrumentId={idx_id}, master exchange={idx_rec.get('exchange')}):")
    for ex_try in ("NSEEQ", "INDICES", "NSEFO", "NSE"):
        try:
            q_idx = client.get_market_quotes([{"exchange": ex_try, "instrumentId": idx_id}])
            if q_idx and q_idx[0].get("ltp") is not None:
                row = q_idx[0]
                print(f"  exchange={ex_try} -> HTTP 200 OK | ltp={row.get('ltp')}, tickTimestamp={row.get('tickTimestamp')}")
            else:
                print(f"  exchange={ex_try} -> HTTP 200 (empty/no ltp): {q_idx}")
        except BrokerSessionExpiredError as exc:
            print(f"  exchange={ex_try} -> SESSION INVALID: {exc}")
            return 1
        except IIFLMarketDataError as exc:
            print(f"  exchange={ex_try} -> HTTP {exc.status_code} ERROR: {exc.message}")

    # 3. One active NSEFO contract from contract master
    fo_hits = cm.search("NIFTY", segment="NSEFO", limit=10)
    fo_pick = fo_hits[0]
    print(f"\n[3] NSEFO marketquotes for {fo_pick['symbol']} (instrumentId={fo_pick['instrumentId']}):")
    try:
        q_fo = client.get_market_quotes([{"exchange": "NSEFO", "instrumentId": fo_pick["instrumentId"]}])
        if q_fo:
            row = q_fo[0]
            print(f"  HTTP 200 | fields={sorted(k for k in row.keys() if k != 'tickDatetimeIst')}")
            print(f"  ltp={row.get('ltp')}, bestBid={row.get('bestBidPrice')}, bestAsk={row.get('bestAskPrice')}, tickTimestamp={row.get('tickTimestamp')}")
    except (IIFLMarketDataError, BrokerSessionExpiredError) as exc:
        print(f"  FAILED: {exc}")

    # 4. One small historicaldata request (last 3 days, 1 day interval)
    today_d = datetime.now(IST).date()
    from_d = today_d - timedelta(days=3)
    print(f"\n[4] historicaldata for {eq_pick['symbol']} ({format_iifl_date(from_d)} .. {format_iifl_date(today_d)}, 1 day):")
    try:
        candles = client.get_historical_data(
            exchange="NSEEQ",
            instrument_id=eq_pick["instrumentId"],
            interval="1 day",
            from_date=from_d,
            to_date=today_d,
        )
        print(f"  HTTP 200 | candleCount={len(candles)}")
        if candles:
            print(f"  sample[0]={candles[0]}")
    except (IIFLMarketDataError, BrokerSessionExpiredError) as exc:
        print(f"  FAILED: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

