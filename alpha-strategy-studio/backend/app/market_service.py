import time
import math
import requests
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, RLock, Thread

from .contract_master import ContractMasterError, contract_master_service


def _resolve_lot_size(symbol_or_name: str) -> Optional[int]:
    """Resolve contract lot size from the contract master without hardcoded fallbacks."""
    raw = str(symbol_or_name or "").strip().upper()
    if not raw:
        return None
    underlying = {
        "NIFTY 50": "NIFTY",
        "NIFTY BANK": "BANKNIFTY",
        "NIFTY FIN SERVICE": "FINNIFTY",
    }.get(raw, raw)
    summary = contract_master_service.get_lot_sizes_summary("NSEFO")
    if underlying in summary:
        return int(summary[underlying])
    try:
        rec = contract_master_service.lookup_contract(segment="NSEEQ", underlying=raw, series="EQ")
        return int(rec["lotSize"]) if rec.get("lotSize") is not None else None
    except ContractMasterError:
        return None


# Instrument metadata configuration
INSTRUMENT_CATALOG: Dict[str, Dict[str, Any]] = {
    "NIFTY 50": {
        "ticker": "^NSEI",
        "name": "NIFTY 50",
        "category": "INDEX",
        "exchange": "NSE",
        "strike_step": 50,
        "description": "NSE Flagship 50 Index"
    },
    "BANKNIFTY": {
        "ticker": "^NSEBANK",
        "name": "BANKNIFTY",
        "category": "INDEX",
        "exchange": "NSE",
        "strike_step": 100,
        "description": "Nifty Bank Sectoral Index"
    },
    "FINNIFTY": {
        "ticker": "NIFTY_FIN_SERVICE.NS",
        "name": "FINNIFTY",
        "category": "INDEX",
        "exchange": "NSE",
        "strike_step": 50,
        "description": "Nifty Financial Services Index"
    },
    "SENSEX": {
        "ticker": "^BSESN",
        "name": "SENSEX",
        "category": "INDEX",
        "exchange": "BSE",
        "strike_step": 100,
        "description": "BSE S&P 30 Bluechip Index"
    },
    "NIFTY IT": {
        "ticker": "^CNXIT",
        "name": "NIFTY IT",
        "category": "INDEX",
        "exchange": "NSE",
        "strike_step": 100,
        "description": "Nifty IT Sector Index"
    },
    "NIFTY AUTO": {
        "ticker": "^CNXAUTO",
        "name": "NIFTY AUTO",
        "category": "INDEX",
        "strike_step": 100,
        "description": "Nifty Automobiles Index"
    },
    "NIFTY FMCG": {
        "ticker": "^CNXFMCG",
        "name": "NIFTY FMCG",
        "category": "INDEX",
        "strike_step": 100,
        "description": "Nifty FMCG Index"
    },
    "RELIANCE": {
        "ticker": "RELIANCE.NS",
        "name": "RELIANCE",
        "category": "EQUITY",
        "exchange": "NSE",
        "strike_step": 20,
        "description": "Reliance Industries Ltd"
    },
    "HDFCBANK": {
        "ticker": "HDFCBANK.NS",
        "name": "HDFCBANK",
        "category": "EQUITY",
        "exchange": "NSE",
        "strike_step": 10,
        "description": "HDFC Bank Ltd"
    },
    "TCS": {
        "ticker": "TCS.NS",
        "name": "TCS",
        "category": "EQUITY",
        "exchange": "NSE",
        "strike_step": 20,
        "description": "Tata Consultancy Services Ltd"
    },
    "INFY": {
        "ticker": "INFY.NS",
        "name": "INFY",
        "category": "EQUITY",
        "exchange": "NSE",
        "strike_step": 20,
        "description": "Infosys Ltd"
    },
    "ICICIBANK": {
        "ticker": "ICICIBANK.NS",
        "name": "ICICIBANK",
        "category": "EQUITY",
        "exchange": "NSE",
        "strike_step": 10,
        "description": "ICICI Bank Ltd"
    },
    "SBIN": {
        "ticker": "SBIN.NS",
        "name": "SBIN",
        "category": "EQUITY",
        "exchange": "NSE",
        "strike_step": 5,
        "description": "State Bank of India"
    },
    "BHARTIARTL": {
        "ticker": "BHARTIARTL.NS",
        "name": "BHARTIARTL",
        "category": "EQUITY",
        "exchange": "NSE",
        "strike_step": 10,
        "description": "Bharti Airtel Ltd"
    },
    "TATAMOTORS": {
        "ticker": "TATAMOTORS.NS",
        "name": "TATAMOTORS",
        "category": "EQUITY",
        "exchange": "NSE",
        "strike_step": 10,
        "description": "Tata Motors Ltd"
    },
    "ZOMATO": {
        "ticker": "ZOMATO.NS",
        "name": "ZOMATO",
        "category": "EQUITY",
        "exchange": "NSE",
        "strike_step": 5,
        "description": "Zomato Ltd"
    }
}

class MarketDataService:
    def __init__(self, cache_ttl_seconds: float = 3.0, chart_ttl_seconds: float = 15.0):
        self.cache_ttl = cache_ttl_seconds
        self.chart_ttl = chart_ttl_seconds
        self._quote_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._chart_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._chart_timestamps: Dict[Tuple[str, str, str], float] = {}
        self._backoff_until: Dict[str, float] = {}
        self._backoff_failures: Dict[str, int] = {}
        self._ticker_locks: Dict[str, Lock] = {}
        self._tracked_symbols: set[str] = set()
        self._poller_stop = Event()
        self._poller_thread: Optional[Thread] = None
        self._lock = RLock()
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def _get_ticker_lock(self, key: str) -> Lock:
        with self._lock:
            lk = self._ticker_locks.get(key)
            if lk is None:
                lk = Lock()
                self._ticker_locks[key] = lk
            return lk

    def _record_rate_limit(self, key: str, status_code: int) -> None:
        if status_code not in (429, 403):
            return
        with self._lock:
            fails = self._backoff_failures.get(key, 0) + 1
            self._backoff_failures[key] = fails
            delay = min(120.0, 10.0 * (2 ** (fails - 1)))
            self._backoff_until[key] = time.time() + delay

    def _clear_rate_limit(self, key: str) -> None:
        with self._lock:
            self._backoff_failures.pop(key, None)
            self._backoff_until.pop(key, None)

    def _is_backed_off_unlocked(self, key: str, now: float) -> bool:
        return now < self._backoff_until.get(key, 0.0)

    def _is_backed_off(self, key: str, now: float) -> bool:
        with self._lock:
            return self._is_backed_off_unlocked(key, now)

    def register_symbols(self, symbols: List[str]) -> None:
        with self._lock:
            for s in symbols or []:
                cleaned = str(s or "").strip()
                if cleaned:
                    self._tracked_symbols.add(cleaned)

    def poll_once(self) -> int:
        with self._lock:
            syms = sorted(self._tracked_symbols)
        if not syms:
            return 0
        self.get_quotes_batch(syms)
        return len(syms)

    def start_poller(self, interval_seconds: float = 3.0) -> None:
        with self._lock:
            if self._poller_thread is not None and self._poller_thread.is_alive():
                return
            self._poller_stop.clear()

            def _run():
                while not self._poller_stop.is_set():
                    try:
                        self.poll_once()
                    except Exception:
                        pass
                    self._poller_stop.wait(interval_seconds)

            self._poller_thread = Thread(target=_run, name="market-poller", daemon=True)
            self._poller_thread.start()

    def stop_poller(self) -> None:
        with self._lock:
            self._poller_stop.set()
            t = self._poller_thread
            self._poller_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def resolve_ticker(self, symbol_or_name: str) -> str:
        s = symbol_or_name.strip().upper()
        if s in INSTRUMENT_CATALOG:
            return INSTRUMENT_CATALOG[s]["ticker"]
        # Handle variations like "NIFTY" -> "NIFTY 50"
        if s == "NIFTY":
            return "^NSEI"
        if s == "BANK NIFTY":
            return "^NSEBANK"
        # If already formatted as ticker or equity
        if s.startswith("^") or s.endswith(".NS") or s.endswith(".BO"):
            return s
        return f"{s}.NS"

    def get_metadata(self, symbol_or_name: str) -> Dict[str, Any]:
        s = symbol_or_name.strip().upper()
        if s in INSTRUMENT_CATALOG:
            return INSTRUMENT_CATALOG[s]
        for item in INSTRUMENT_CATALOG.values():
            if item["ticker"].upper() == s or item["name"].upper() == s:
                return item
        return {
            "ticker": self.resolve_ticker(symbol_or_name),
            "name": symbol_or_name,
            "category": "EQUITY",
            "exchange": "NSE",
            "strike_step": 50,
            "description": symbol_or_name
        }

    def get_quote(self, symbol_or_name: str, force_refresh: bool = False) -> Dict[str, Any]:
        ticker = self.resolve_ticker(symbol_or_name)
        self.register_symbols([symbol_or_name])
        now = time.time()

        with self._lock:
            if not force_refresh and ticker in self._quote_cache:
                cached_time = self._cache_timestamps.get(ticker, 0)
                if now - cached_time < self.cache_ttl:
                    return self._quote_cache[ticker]

        t_lock = self._get_ticker_lock(ticker)
        with t_lock:
            now = time.time()
            with self._lock:
                if not force_refresh and ticker in self._quote_cache:
                    cached_time = self._cache_timestamps.get(ticker, 0)
                    if now - cached_time < self.cache_ttl:
                        return self._quote_cache[ticker]
                if not force_refresh and self._is_backed_off_unlocked(ticker, now):
                    if ticker in self._quote_cache:
                        cached = dict(self._quote_cache[ticker])
                        cached["source"] = "CACHED FEED (BACKOFF)"
                        return cached
                    return self._offline_fallback_quote(symbol_or_name, ticker, now, "Rate limited backoff active")

            # Fetch live quote from Yahoo Finance v8 chart API
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=5m&range=1d"
            try:
                resp = requests.get(url, headers=self._headers, timeout=6)
                if resp.status_code in (429, 403):
                    self._record_rate_limit(ticker, resp.status_code)
                    resp.raise_for_status()
                resp.raise_for_status()
                self._clear_rate_limit(ticker)
                data = resp.json()
                result = data.get("chart", {}).get("result", [])
                if not result:
                    raise ValueError(f"No chart data returned for {ticker}")

                meta = result[0].get("meta", {})
                ltp = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or 0.0
                prev_close = meta.get("chartPreviousClose") or meta.get("previousClose") or ltp
                change = meta.get("fulldayChange") or (ltp - prev_close)
                change_percent = meta.get("regularMarketChangePercent") or meta.get("fulldayChangePercent")
                if change_percent is None and prev_close > 0:
                    change_percent = (change / prev_close) * 100.0

                day_high = meta.get("regularMarketDayHigh") or ltp
                day_low = meta.get("regularMarketDayLow") or ltp
                open_price = meta.get("regularMarketOpen") or ltp
                volume = meta.get("regularMarketVolume", 0)

                is_market_open = False
                current_period = meta.get("currentTradingPeriod", {}).get("regular", {})
                if current_period:
                    p_start = current_period.get("start", 0)
                    p_end = current_period.get("end", 0)
                    is_market_open = p_start <= now <= p_end

                meta_info = self.get_metadata(symbol_or_name)
                reg_market_time = meta.get("regularMarketTime")

                quote = {
                    "symbol": meta_info.get("name", symbol_or_name),
                    "ticker": ticker,
                    "displayName": meta.get("shortName") or meta_info.get("name", symbol_or_name),
                    "category": meta_info.get("category", "INDEX"),
                    "exchange": meta_info.get("exchange", "NSE"),
                    "ltp": round(float(ltp), 2),
                    "change": round(float(change), 2),
                    "changePercent": round(float(change_percent or 0.0), 2),
                    "prevClose": round(float(prev_close), 2),
                    "open": round(float(open_price), 2),
                    "high": round(float(day_high), 2),
                    "low": round(float(day_low), 2),
                    "volume": int(volume or 0),
                    "fiftyTwoWeekHigh": round(float(meta.get("fiftyTwoWeekHigh", 0) or 0), 2),
                    "fiftyTwoWeekLow": round(float(meta.get("fiftyTwoWeekLow", 0) or 0), 2),
                    "strikeStep": meta_info.get("strike_step", 50),
                    "lotSize": _resolve_lot_size(symbol_or_name),
                    "isMarketOpen": is_market_open,
                    # Note: Yahoo v8 quotes can still be delayed vs exchange ticks; real fix is IIFL market data.
                    "regularMarketTime": float(reg_market_time) if reg_market_time is not None else None,
                    "source": "LIVE YAHOO/NSE FEED",
                    "updatedAt": now
                }

                with self._lock:
                    self._quote_cache[ticker] = quote
                    self._cache_timestamps[ticker] = now

                return quote
            except Exception as e:
                with self._lock:
                    if ticker in self._quote_cache:
                        cached = dict(self._quote_cache[ticker])
                        cached["source"] = "CACHED FEED (OFFLINE)"
                        return cached

                return self._offline_fallback_quote(symbol_or_name, ticker, now, str(e))

    def _offline_fallback_quote(self, symbol_or_name: str, ticker: str, now: float, err_str: str) -> Dict[str, Any]:
        meta_info = self.get_metadata(symbol_or_name)
        demo_prices = {
            "^NSEI": 23890.0,
            "^NSEBANK": 55820.0,
            "NIFTY_FIN_SERVICE.NS": 25530.0,
            "^BSESN": 77050.0,
            "RELIANCE.NS": 1312.0,
            "TCS.NS": 2435.0,
            "HDFCBANK.NS": 735.0
        }
        if ticker in demo_prices:
            demo_ltp = demo_prices[ticker]
            return {
                "symbol": meta_info.get("name", symbol_or_name),
                "ticker": ticker,
                "displayName": meta_info.get("name", symbol_or_name),
                "category": meta_info.get("category", "INDEX"),
                "exchange": meta_info.get("exchange", "NSE"),
                "ltp": demo_ltp,
                "change": 0.0,
                "changePercent": 0.0,
                "prevClose": demo_ltp,
                "open": demo_ltp,
                "high": demo_ltp,
                "low": demo_ltp,
                "volume": 0,
                "fiftyTwoWeekHigh": demo_ltp * 1.1,
                "fiftyTwoWeekLow": demo_ltp * 0.9,
                "strikeStep": meta_info.get("strike_step", 50),
                "lotSize": _resolve_lot_size(symbol_or_name),
                "isMarketOpen": False,
                "source": "OFFLINE FALLBACK",
                "error": err_str,
                "updatedAt": now
            }

        return {
            "symbol": meta_info.get("name", symbol_or_name),
            "ticker": ticker,
            "displayName": meta_info.get("name", symbol_or_name),
            "category": "EQUITY",
            "exchange": meta_info.get("exchange", "NSE"),
            "ltp": None,
            "change": 0,
            "changePercent": 0,
            "high": None,
            "low": None,
            "volume": 0,
            "source": "NO DATA",
            "noData": True,
            "error": err_str,
            "updatedAt": now
        }

    def get_quotes_batch(self, symbols_or_names: List[str]) -> List[Dict[str, Any]]:
        if not symbols_or_names:
            return []
        self.register_symbols(symbols_or_names)
        with ThreadPoolExecutor(max_workers=max(1, min(len(symbols_or_names), 10))) as executor:
            results = list(executor.map(self.get_quote, symbols_or_names))
        return results

    def _fetch_yahoo_candles(self, ticker: str, range_period: str, interval: str):
        now = time.time()
        if self._is_backed_off(ticker, now):
            return [], {}, "Rate limited backoff active"

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={range_period}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=8)
            if resp.status_code in (429, 403):
                self._record_rate_limit(ticker, resp.status_code)
                resp.raise_for_status()
            resp.raise_for_status()
            self._clear_rate_limit(ticker)
            data = resp.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                return [], {}, "No chart data returned"

            item = result[0]
            meta = item.get("meta", {})
            timestamps = item.get("timestamp", [])
            indicators = item.get("indicators", {})
            quotes = indicators.get("quote", [{}])[0]

            opens = quotes.get("open", [])
            highs = quotes.get("high", [])
            lows = quotes.get("low", [])
            closes = quotes.get("close", [])
            volumes = quotes.get("volume", [])

            candles = []
            for i in range(len(timestamps)):
                t = timestamps[i]
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue  # skip unfilled data points
                o = opens[i] if i < len(opens) and opens[i] is not None else c
                h = highs[i] if i < len(highs) and highs[i] is not None else c
                l = lows[i] if i < len(lows) and lows[i] is not None else c
                v = volumes[i] if i < len(volumes) and volumes[i] is not None else 0

                # Convert to IST readable time
                # Offset IST is UTC + 5h30m = 19800 sec
                ist_struct = time.gmtime(t + 19800)
                if interval in ("1d", "1wk", "1mo"):
                    time_str = time.strftime("%d %b", ist_struct)
                elif range_period != "1d":
                    time_str = time.strftime("%d %b %H:%M", ist_struct)
                else:
                    time_str = time.strftime("%H:%M", ist_struct)

                date_str = time.strftime("%Y-%m-%d", ist_struct)

                candles.append({
                    "timestamp": t,
                    "time": time_str,
                    "date": date_str,
                    "open": round(float(o), 2),
                    "high": round(float(h), 2),
                    "low": round(float(l), 2),
                    "close": round(float(c), 2),
                    "volume": int(v)
                })

            return candles, meta, None
        except Exception as e:
            return [], {}, str(e)

    def get_chart(self, symbol_or_name: str, range_period: str = "1d", interval: str = "5m") -> Dict[str, Any]:
        ticker = self.resolve_ticker(symbol_or_name)
        cache_key = (ticker, range_period, interval)
        ttl = 60.0 if interval in ("1d", "1wk", "1mo") else self.chart_ttl
        now = time.time()

        with self._lock:
            if cache_key in self._chart_cache and (now - self._chart_timestamps.get(cache_key, 0.0) < ttl):
                return self._chart_cache[cache_key]

        c_lock = self._get_ticker_lock(f"chart:{ticker}:{range_period}:{interval}")
        with c_lock:
            now = time.time()
            with self._lock:
                if cache_key in self._chart_cache and (now - self._chart_timestamps.get(cache_key, 0.0) < ttl):
                    return self._chart_cache[cache_key]

            # Build attempt sequence: requested first, then fallbacks
            base_attempts = [(range_period, interval)]
            for r_fb, i_fb in [("5d", "15m"), ("1mo", "1d")]:
                if (r_fb, i_fb) not in base_attempts:
                    base_attempts.append((r_fb, i_fb))

            tickers_to_try = [ticker]
            if ticker.endswith(".NS") and not ticker.startswith("^"):
                tickers_to_try.append(ticker[:-3] + ".BO")

            last_error = None
            for t_sym in tickers_to_try:
                for r_try, i_try in base_attempts:
                    candles, meta, err = self._fetch_yahoo_candles(t_sym, r_try, i_try)
                    if err:
                        last_error = err
                    if candles:
                        is_fallback = (t_sym != ticker or r_try != range_period or i_try != interval)
                        chart_res = {
                            "symbol": symbol_or_name,
                            "ticker": t_sym,
                            "candles": candles,
                            "previousClose": meta.get("chartPreviousClose"),
                            "currency": meta.get("currency", "INR"),
                            "exchange": meta.get("exchangeName", "NSE"),
                            "rangeUsed": r_try,
                            "intervalUsed": i_try,
                            "fallback": is_fallback
                        }
                        with self._lock:
                            self._chart_cache[cache_key] = chart_res
                            self._chart_timestamps[cache_key] = time.time()
                        return chart_res

            with self._lock:
                if cache_key in self._chart_cache:
                    cached = dict(self._chart_cache[cache_key])
                    cached["fallback"] = True
                    return cached

            return {
                "symbol": symbol_or_name,
                "ticker": ticker,
                "candles": [],
                "rangeUsed": range_period,
                "intervalUsed": interval,
                "fallback": False,
                "error": last_error or "Yahoo has no candles for this instrument right now."
            }

    def calculate_strikes(self, symbol_or_name: str, spot_price: Optional[float] = None) -> Dict[str, Any]:
        """
        Calculate ATM, ITM 1/2, OTM 1/2 strikes for Calls and Puts based on current live spot.
        Also estimates realistic option premium based on intrinsic + time value.
        """
        meta = self.get_metadata(symbol_or_name)
        step = meta.get("strike_step", 50)
        lot_size = _resolve_lot_size(symbol_or_name)

        if spot_price is None or spot_price <= 0:
            quote = self.get_quote(symbol_or_name)
            spot_price = quote.get("ltp", 20000.0)

        # ATM strike is nearest multiple of step
        atm_strike = round(spot_price / step) * step

        # Base time value for an ATM weekly option (~0.7% of spot)
        atm_base_premium = max(10.0, round(spot_price * 0.0075, 1))

        def estimate_premium(strike: float, opt_type: str) -> float:
            # Intrinsic value
            if opt_type == "CE":
                intrinsic = max(0.0, spot_price - strike)
            else:
                intrinsic = max(0.0, strike - spot_price)

            # Out of the money decay
            distance = abs(strike - atm_strike) / step
            decay_factor = math.exp(-0.35 * distance)
            time_val = atm_base_premium * decay_factor
            return round(intrinsic + time_val, 1)

        strikes = {
            "spotPrice": spot_price,
            "strikeStep": step,
            "lotSize": lot_size,
            "ATM": {
                "strike": atm_strike,
                "CE": {"strike": atm_strike, "premium": estimate_premium(atm_strike, "CE")},
                "PE": {"strike": atm_strike, "premium": estimate_premium(atm_strike, "PE")}
            },
            "ITM 1": {
                "CE": {"strike": atm_strike - step, "premium": estimate_premium(atm_strike - step, "CE")},
                "PE": {"strike": atm_strike + step, "premium": estimate_premium(atm_strike + step, "PE")}
            },
            "ITM 2": {
                "CE": {"strike": atm_strike - 2 * step, "premium": estimate_premium(atm_strike - 2 * step, "CE")},
                "PE": {"strike": atm_strike + 2 * step, "premium": estimate_premium(atm_strike + 2 * step, "PE")}
            },
            "OTM 1": {
                "CE": {"strike": atm_strike + step, "premium": estimate_premium(atm_strike + step, "CE")},
                "PE": {"strike": atm_strike - step, "premium": estimate_premium(atm_strike - step, "PE")}
            },
            "OTM 2": {
                "CE": {"strike": atm_strike + 2 * step, "premium": estimate_premium(atm_strike + 2 * step, "CE")},
                "PE": {"strike": atm_strike - 2 * step, "premium": estimate_premium(atm_strike - 2 * step, "PE")}
            }
        }
        return strikes

    def get_level_candle(self, symbol_or_name: str, start_hhmm: str, minutes: int) -> Dict[str, Any]:
        """
        Build a candle of the given timeframe from 1-minute Yahoo candles.
        Returns {open, high, low, close, complete, prev_close, day_high, day_low}.
        """
        ticker = self.resolve_ticker(symbol_or_name)
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
            resp = requests.get(url, headers=self._headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                return {"complete": False, "error": "No data"}

            item = result[0]
            meta = item.get("meta", {})
            timestamps = item.get("timestamp") or []
            indicators = item.get("indicators", {})
            quotes_data = indicators.get("quote", [{}])[0]

            opens = quotes_data.get("open", [])
            highs = quotes_data.get("high", [])
            lows = quotes_data.get("low", [])
            closes = quotes_data.get("close", [])

            prev_close = meta.get("chartPreviousClose") or meta.get("previousClose") or 0

            sh, sm = int(start_hhmm.split(":")[0]), int(start_hhmm.split(":")[1])
            start_minutes = sh * 60 + sm
            end_minutes = start_minutes + minutes

            # Collect 1-min candles in the window and all candles for day extremes
            window_candles = []
            all_day_highs = []
            all_day_lows = []

            for i in range(len(timestamps)):
                t = timestamps[i]
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                o = opens[i] if i < len(opens) and opens[i] is not None else c
                h = highs[i] if i < len(highs) and highs[i] is not None else c
                l = lows[i] if i < len(lows) and lows[i] is not None else c

                ist_struct = time.gmtime(t + 19800)
                candle_m = ist_struct.tm_hour * 60 + ist_struct.tm_min

                if candle_m < end_minutes:
                    all_day_highs.append(h)
                    all_day_lows.append(l)

                if start_minutes <= candle_m < end_minutes:
                    window_candles.append({"open": o, "high": h, "low": l, "close": c})

            if not window_candles:
                return {"complete": False, "error": "No candles in window"}

            # Check if the current IST time >= start + minutes
            from datetime import datetime, timezone, timedelta
            IST_TZ = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(IST_TZ)
            now_m = now_ist.hour * 60 + now_ist.minute
            is_complete = now_m >= end_minutes and len(window_candles) >= 1

            # Aggregate the 1-min candles into one
            agg_open = window_candles[0]["open"]
            agg_high = max(c["high"] for c in window_candles)
            agg_low = min(c["low"] for c in window_candles)
            agg_close = window_candles[-1]["close"]

            day_high = max(all_day_highs) if all_day_highs else agg_high
            day_low = min(all_day_lows) if all_day_lows else agg_low

            return {
                "open": round(agg_open, 2),
                "high": round(agg_high, 2),
                "low": round(agg_low, 2),
                "close": round(agg_close, 2),
                "complete": is_complete,
                "prev_close": round(prev_close, 2),
                "day_high": round(day_high, 2),
                "day_low": round(day_low, 2)
            }
        except Exception as e:
            return {"complete": False, "error": str(e)}

    def get_day_extremes(self, symbol_or_name: str, until_hhmm: str = "15:30") -> Dict[str, Any]:
        """Return highest high and lowest low from 09:15 until until_hhmm."""
        ticker = self.resolve_ticker(symbol_or_name)
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
            resp = requests.get(url, headers=self._headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                return {"high": 0, "low": 0}

            item = result[0]
            timestamps = item.get("timestamp") or []
            indicators = item.get("indicators", {})
            quotes_data = indicators.get("quote", [{}])[0]
            highs = quotes_data.get("high", [])
            lows = quotes_data.get("low", [])

            uh, um = int(until_hhmm.split(":")[0]), int(until_hhmm.split(":")[1])
            until_m = uh * 60 + um

            day_highs = []
            day_lows = []
            for i in range(len(timestamps)):
                t = timestamps[i]
                h = highs[i] if i < len(highs) and highs[i] is not None else None
                l_val = lows[i] if i < len(lows) and lows[i] is not None else None
                if h is None or l_val is None:
                    continue
                ist_struct = time.gmtime(t + 19800)
                candle_m = ist_struct.tm_hour * 60 + ist_struct.tm_min
                if candle_m < until_m:
                    day_highs.append(h)
                    day_lows.append(l_val)

            return {
                "high": round(max(day_highs), 2) if day_highs else 0,
                "low": round(min(day_lows), 2) if day_lows else 0
            }
        except Exception:
            return {"high": 0, "low": 0}

    def search_symbols(self, query: str) -> List[Dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        try:
            resp = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": query, "quotesCount": 25, "newsCount": 0, "lang": "en-IN", "region": "IN"},
                headers=self._headers,
                timeout=5
            )
            resp.raise_for_status()
            data = resp.json()
            quotes = data.get("quotes", [])
            # Keep ONLY results where symbol ends with .NS or .BO, or starts with ^ (indices)
            filtered = [
                q for q in quotes
                if q.get("symbol", "").endswith(".NS") or q.get("symbol", "").endswith(".BO") or q.get("symbol", "").startswith("^")
            ]
            # Sort so .NS results come before .BO results
            filtered.sort(key=lambda q: (0 if q.get("symbol", "").endswith(".NS") else 1 if q.get("symbol", "").endswith(".BO") else 2))

            results = []
            for q in filtered[:8]:
                symbol = q.get("symbol", "")
                exch = q.get("exchDisp", "")
                shortname = q.get("shortname") or q.get("longname") or symbol
                results.append({
                    "symbol": symbol,
                    "name": shortname,
                    "exchange": exch,
                    "type": q.get("quoteType", "EQUITY"),
                    "cleanSymbol": symbol.replace(".NS", "").replace(".BO", "")
                })
            if results:
                return results
        except Exception:
            pass

        # Fallback to built-in stock dictionary
        results = []
        q_lower = query.lower()
        for key, val in INSTRUMENT_CATALOG.items():
            if q_lower in key.lower() or q_lower in val.get("name", "").lower() or q_lower in val.get("description", "").lower():
                results.append({
                    "symbol": val["ticker"],
                    "name": val.get("description") or val.get("name") or key,
                    "exchange": val.get("exchange", "NSE"),
                    "type": val.get("category", "EQUITY"),
                    "cleanSymbol": key
                })
        return results[:8]

# Singleton instance
market_service = MarketDataService()

