"""Pure-Python / NumPy technical indicator library with strict warm-up handling.

All indicators:
- Accept chronological candle dicts ({open, high, low, close, volume, timestamp, date, ...})
  or numeric series.
- Return None for indices before the warm-up period is satisfied so the condition
  engine never emits premature signals.
- Are deterministic and side-effect free (no future candle look-ahead).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

import numpy as np

IST = timezone(timedelta(hours=5, minutes=30))


def _is_valid_num(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    try:
        f = float(val)
        return math.isfinite(f)
    except (TypeError, ValueError):
        return False


def extract_series(candles: Sequence[dict], source: str = "CLOSE") -> list[Optional[float]]:
    """Extract a numeric series from candle dicts by source name."""
    key_map = {
        "OPEN": "open",
        "HIGH": "high",
        "LOW": "low",
        "CLOSE": "close",
        "VOLUME": "volume",
        "LTP": "ltp",
    }
    field = key_map.get(source.upper(), source.lower())
    out: list[Optional[float]] = []
    for c in candles:
        val = c.get(field)
        if val is None and field == "ltp":
            val = c.get("close")
        out.append(float(val) if _is_valid_num(val) else None)
    return out


def candle_day_ist(candle: dict) -> str:
    """Resolve the IST calendar date (YYYY-MM-DD) for a candle."""
    if isinstance(candle.get("date"), str) and len(candle["date"]) == 10:
        return candle["date"]
    ts = candle.get("timestamp")
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(float(ts), tz=IST)
        return dt.strftime("%Y-%m-%d")
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            if len(ts) >= 10:
                return ts[:10]
    return "1970-01-01"


def compute_sma(values: Sequence[Optional[float]], period: int) -> list[Optional[float]]:
    """Simple Moving Average with warm-up period `period`.

    Returns None until `period` consecutive valid numbers are available.
    """
    if period < 1:
        raise ValueError("SMA period must be >= 1")
    n = len(values)
    out: list[Optional[float]] = [None] * n
    window: list[float] = []
    for i, v in enumerate(values):
        if not _is_valid_num(v):
            window.clear()
            continue
        window.append(float(v))  # type: ignore[arg-type]
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            out[i] = float(np.mean(window))
    return out


def compute_ema(values: Sequence[Optional[float]], period: int) -> list[Optional[float]]:
    """Exponential Moving Average seeded with the SMA of the first `period` valid values.

    Multiplier k = 2 / (period + 1).
    Returns None for the first (period - 1) valid samples.
    """
    if period < 1:
        raise ValueError("EMA period must be >= 1")
    n = len(values)
    out: list[Optional[float]] = [None] * n
    k = 2.0 / (period + 1.0)
    seed_buf: list[float] = []
    ema_prev: Optional[float] = None

    for i, v in enumerate(values):
        if not _is_valid_num(v):
            continue
        fv = float(v)  # type: ignore[arg-type]
        if ema_prev is None:
            seed_buf.append(fv)
            if len(seed_buf) == period:
                ema_prev = sum(seed_buf) / float(period)
                out[i] = ema_prev
        else:
            ema_prev = (fv - ema_prev) * k + ema_prev
            out[i] = ema_prev
    return out


def compute_rsi(
    values: Sequence[Optional[float]],
    period: int = 14,
    smoothing_period: int = 14,
) -> dict[str, list[Optional[float]]]:
    """Wilder's Relative Strength Index (RSI) plus its smoothed average line.

    - Warm-up for `rsi`: requires `period + 1` valid input prices (i.e. `period` price changes).
      First value at index `period` (0-indexed when all inputs are valid).
    - Warm-up for `signal` / `average`: SMA of `rsi` over `smoothing_period` bars,
      available once `smoothing_period` valid RSI values exist.
    """
    if period < 1:
        raise ValueError("RSI period must be >= 1")
    if smoothing_period < 1:
        raise ValueError("RSI smoothingPeriod must be >= 1")

    n = len(values)
    rsi_out: list[Optional[float]] = [None] * n

    valid_indices: list[int] = []
    valid_vals: list[float] = []
    for i, v in enumerate(values):
        if _is_valid_num(v):
            valid_indices.append(i)
            valid_vals.append(float(v))  # type: ignore[arg-type]

    if len(valid_vals) >= period + 1:
        gains: list[float] = []
        losses: list[float] = []
        for j in range(1, len(valid_vals)):
            delta = valid_vals[j] - valid_vals[j - 1]
            gains.append(max(0.0, delta))
            losses.append(max(0.0, -delta))

        avg_gain = sum(gains[:period]) / float(period)
        avg_loss = sum(losses[:period]) / float(period)

        def _calc_rsi(ag: float, al: float) -> float:
            if al == 0.0 and ag == 0.0:
                return 50.0
            if al == 0.0:
                return 100.0
            rs = ag / al
            return 100.0 - (100.0 / (1.0 + rs))

        rsi_out[valid_indices[period]] = _calc_rsi(avg_gain, avg_loss)

        for j in range(period, len(gains)):
            avg_gain = ((avg_gain * (period - 1)) + gains[j]) / float(period)
            avg_loss = ((avg_loss * (period - 1)) + losses[j]) / float(period)
            rsi_out[valid_indices[j + 1]] = _calc_rsi(avg_gain, avg_loss)

    signal_out = compute_sma(rsi_out, smoothing_period)
    return {
        "value": rsi_out,
        "rsi": rsi_out,
        "signal": signal_out,
        "average": signal_out,
    }


def compute_cci(candles: Sequence[dict], period: int = 20) -> list[Optional[float]]:
    """Commodity Channel Index (CCI) using Typical Price = (H + L + C) / 3.

    CCI = (TP - SMA(TP, period)) / (0.015 * MeanDeviation(TP, period))
    Returns None for the first `period - 1` candles (warm-up).
    """
    if period < 1:
        raise ValueError("CCI period must be >= 1")
    n = len(candles)
    out: list[Optional[float]] = [None] * n
    tp_window: list[float] = []

    for i, c in enumerate(candles):
        h, l, cl = c.get("high"), c.get("low"), c.get("close")
        if not (_is_valid_num(h) and _is_valid_num(l) and _is_valid_num(cl)):
            tp_window.clear()
            continue
        tp = (float(h) + float(l) + float(cl)) / 3.0
        tp_window.append(tp)
        if len(tp_window) > period:
            tp_window.pop(0)
        if len(tp_window) == period:
            sma_tp = sum(tp_window) / float(period)
            mean_dev = sum(abs(x - sma_tp) for x in tp_window) / float(period)
            if mean_dev == 0.0:
                out[i] = 0.0
            else:
                out[i] = (tp - sma_tp) / (0.015 * mean_dev)
    return out


def compute_atr(candles: Sequence[dict], period: int = 14) -> list[Optional[float]]:
    """Wilder's Average True Range (ATR).

    True Range TR_0 = High_0 - Low_0.
    For i >= 1, TR_i = max(High_i - Low_i, |High_i - Close_{i-1}|, |Low_i - Close_{i-1}|).
    First ATR at index `period - 1` is the simple average of the first `period` TR values.
    Subsequent ATR values use Wilder's smoothing: ATR_i = ((ATR_{i-1} * (period - 1)) + TR_i) / period.
    """
    if period < 1:
        raise ValueError("ATR period must be >= 1")
    n = len(candles)
    out: list[Optional[float]] = [None] * n
    tr_list: list[float] = []
    prev_close: Optional[float] = None
    atr_prev: Optional[float] = None

    for i, c in enumerate(candles):
        h, l, cl = c.get("high"), c.get("low"), c.get("close")
        if not (_is_valid_num(h) and _is_valid_num(l) and _is_valid_num(cl)):
            continue
        fh, fl, fcl = float(h), float(l), float(cl)
        if prev_close is None:
            tr = fh - fl
        else:
            tr = max(fh - fl, abs(fh - prev_close), abs(fl - prev_close))
        prev_close = fcl
        tr_list.append(tr)

        if atr_prev is None:
            if len(tr_list) == period:
                atr_prev = sum(tr_list) / float(period)
                out[i] = atr_prev
        else:
            atr_prev = ((atr_prev * (period - 1)) + tr) / float(period)
            out[i] = atr_prev
    return out


def compute_supertrend(
    candles: Sequence[dict],
    period: int = 10,
    multiplier: float = 3.0,
) -> dict[str, list[Optional[float]]]:
    """Supertrend indicator using ATR(period) and factor `multiplier`.

    Direction: +1.0 for Uptrend (Bullish, price above Supertrend), -1.0 for Downtrend (Bearish).
    Returns None during the ATR warm-up (`period - 1` bars).
    """
    if period < 1:
        raise ValueError("Supertrend period must be >= 1")
    if multiplier <= 0:
        raise ValueError("Supertrend multiplier must be > 0")

    n = len(candles)
    atr_series = compute_atr(candles, period)
    st_out: list[Optional[float]] = [None] * n
    dir_out: list[Optional[float]] = [None] * n

    final_upper: Optional[float] = None
    final_lower: Optional[float] = None
    prev_close: Optional[float] = None
    prev_st: Optional[float] = None
    prev_dir: int = 1

    for i, c in enumerate(candles):
        atr_val = atr_series[i]
        h, l, cl = c.get("high"), c.get("low"), c.get("close")
        if atr_val is None or not (_is_valid_num(h) and _is_valid_num(l) and _is_valid_num(cl)):
            continue
        fh, fl, fcl = float(h), float(l), float(cl)
        hl2 = (fh + fl) / 2.0
        basic_upper = hl2 + multiplier * atr_val
        basic_lower = hl2 - multiplier * atr_val

        if final_upper is None or final_lower is None or prev_close is None or prev_st is None:
            final_upper = basic_upper
            final_lower = basic_lower
            if fcl >= hl2:
                prev_dir = 1
                prev_st = final_lower
            else:
                prev_dir = -1
                prev_st = final_upper
        else:
            if basic_upper < final_upper or prev_close > final_upper:
                final_upper = basic_upper
            if basic_lower > final_lower or prev_close < final_lower:
                final_lower = basic_lower

            if prev_st == final_upper:
                # Was in downtrend
                if fcl > final_upper:
                    prev_dir = 1
                    prev_st = final_lower
                else:
                    prev_dir = -1
                    prev_st = final_upper
            else:
                # Was in uptrend
                if fcl < final_lower:
                    prev_dir = -1
                    prev_st = final_upper
                else:
                    prev_dir = 1
                    prev_st = final_lower

        prev_close = fcl
        st_out[i] = prev_st
        dir_out[i] = float(prev_dir)

    return {
        "value": st_out,
        "supertrend": st_out,
        "direction": dir_out,
    }


def compute_vwap(candles: Sequence[dict]) -> list[Optional[float]]:
    """Intraday Volume Weighted Average Price (VWAP), resetting each IST trading day.

    Typical price TP = (High + Low + Close) / 3.
    VWAP = sum(TP * Volume) / sum(Volume) from the start of the IST day.
    If cumulative volume is 0 on a candle, returns None until positive volume is observed.
    """
    n = len(candles)
    out: list[Optional[float]] = [None] * n
    current_day: Optional[str] = None
    cum_pv = 0.0
    cum_vol = 0.0

    for i, c in enumerate(candles):
        h, l, cl = c.get("high"), c.get("low"), c.get("close")
        vol = c.get("volume", 0)
        if not (_is_valid_num(h) and _is_valid_num(l) and _is_valid_num(cl) and _is_valid_num(vol)):
            continue
        day = candle_day_ist(c)
        if day != current_day:
            current_day = day
            cum_pv = 0.0
            cum_vol = 0.0
        fvol = float(vol)
        if fvol < 0:
            continue
        tp = (float(h) + float(l) + float(cl)) / 3.0
        cum_pv += tp * fvol
        cum_vol += fvol
        if cum_vol > 0:
            out[i] = cum_pv / cum_vol
    return out


def compute_macd(
    values: Sequence[Optional[float]],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> dict[str, list[Optional[float]]]:
    """MACD (fast EMA - slow EMA), Signal line (EMA of MACD), and Histogram (MACD - Signal).

    - `macd` / `value` is emitted only once `slow_period` valid samples exist.
    - `signal` and `histogram` are emitted only once `slow_period + signal_period - 1` valid samples exist.
    """
    if fast_period < 1 or slow_period < 1 or signal_period < 1:
        raise ValueError("MACD periods must be >= 1")
    if fast_period >= slow_period:
        raise ValueError("MACD fastPeriod must be lower than slowPeriod")

    n = len(values)
    fast_ema = compute_ema(values, fast_period)
    slow_ema = compute_ema(values, slow_period)

    macd_line: list[Optional[float]] = [None] * n
    for i in range(n):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            macd_line[i] = fast_ema[i] - slow_ema[i]  # type: ignore[operator]

    signal_line = compute_ema(macd_line, signal_period)
    hist_line: list[Optional[float]] = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            hist_line[i] = macd_line[i] - signal_line[i]  # type: ignore[operator]

    return {
        "value": macd_line,
        "macd": macd_line,
        "signal": signal_line,
        "histogram": hist_line,
    }


def compute_bollinger(
    values: Sequence[Optional[float]],
    period: int = 20,
    std_dev: float = 2.0,
) -> dict[str, list[Optional[float]]]:
    """Bollinger Bands (Middle = SMA, Upper = Middle + stdDev * sigma, Lower = Middle - stdDev * sigma).

    Uses population standard deviation (ddof=0) over the rolling `period` window,
    matching standard charting platforms.
    """
    if period < 1:
        raise ValueError("Bollinger period must be >= 1")
    if std_dev <= 0:
        raise ValueError("Bollinger stdDev must be > 0")

    n = len(values)
    middle: list[Optional[float]] = [None] * n
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    bandwidth: list[Optional[float]] = [None] * n

    window: list[float] = []
    for i, v in enumerate(values):
        if not _is_valid_num(v):
            window.clear()
            continue
        window.append(float(v))  # type: ignore[arg-type]
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            arr = np.array(window, dtype=float)
            mean = float(np.mean(arr))
            sigma = float(np.std(arr, ddof=0))
            up = mean + std_dev * sigma
            lo = mean - std_dev * sigma
            middle[i] = mean
            upper[i] = up
            lower[i] = lo
            bandwidth[i] = ((up - lo) / mean * 100.0) if mean != 0 else 0.0

    return {
        "value": middle,
        "middle": middle,
        "upper": upper,
        "lower": lower,
        "bandwidth": bandwidth,
    }


def compute_stochastic(
    candles: Sequence[dict],
    k_period: int = 14,
    d_period: int = 3,
) -> dict[str, list[Optional[float]]]:
    """Stochastic Oscillator (%K and %D).

    %K = 100 * (Close - LowestLow(k_period)) / (HighestHigh(k_period) - LowestLow(k_period))
    %D = SMA(%K, d_period)
    Warm-up: %K requires `k_period` bars; %D requires `k_period + d_period - 1` bars.
    """
    if k_period < 1 or d_period < 1:
        raise ValueError("Stochastic periods must be >= 1")

    n = len(candles)
    k_out: list[Optional[float]] = [None] * n
    highs_win: list[float] = []
    lows_win: list[float] = []

    for i, c in enumerate(candles):
        h, l, cl = c.get("high"), c.get("low"), c.get("close")
        if not (_is_valid_num(h) and _is_valid_num(l) and _is_valid_num(cl)):
            highs_win.clear()
            lows_win.clear()
            continue
        fh, fl, fcl = float(h), float(l), float(cl)
        highs_win.append(fh)
        lows_win.append(fl)
        if len(highs_win) > k_period:
            highs_win.pop(0)
            lows_win.pop(0)
        if len(highs_win) == k_period:
            hh = max(highs_win)
            ll = min(lows_win)
            if hh == ll:
                k_out[i] = 50.0
            else:
                k_out[i] = 100.0 * (fcl - ll) / (hh - ll)

    d_out = compute_sma(k_out, d_period)
    return {
        "value": k_out,
        "k": k_out,
        "d": d_out,
    }


def compute_adx(
    candles: Sequence[dict],
    period: int = 14,
) -> dict[str, list[Optional[float]]]:
    """Wilder's Average Directional Index (+DI, -DI, and ADX).

    Warm-up:
    - +DI and -DI require `period` directional-movement transitions (first valid at index `period`).
    - ADX requires `period` DX values smoothed via Wilder's moving average (first valid at index `2 * period - 1`).
    """
    if period < 1:
        raise ValueError("ADX period must be >= 1")

    n = len(candles)
    adx_out: list[Optional[float]] = [None] * n
    plus_di_out: list[Optional[float]] = [None] * n
    minus_di_out: list[Optional[float]] = [None] * n

    prev_h: Optional[float] = None
    prev_l: Optional[float] = None
    prev_c: Optional[float] = None

    tr_buf: list[float] = []
    pdm_buf: list[float] = []
    mdm_buf: list[float] = []

    smooth_tr: Optional[float] = None
    smooth_pdm: Optional[float] = None
    smooth_mdm: Optional[float] = None

    dx_buf: list[float] = []
    adx_prev: Optional[float] = None

    for i, c in enumerate(candles):
        h, l, cl = c.get("high"), c.get("low"), c.get("close")
        if not (_is_valid_num(h) and _is_valid_num(l) and _is_valid_num(cl)):
            continue
        fh, fl, fcl = float(h), float(l), float(cl)

        if prev_h is None or prev_l is None or prev_c is None:
            prev_h, prev_l, prev_c = fh, fl, fcl
            continue

        up_move = fh - prev_h
        down_move = prev_l - fl
        pdm = up_move if (up_move > down_move and up_move > 0.0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0.0) else 0.0
        tr = max(fh - fl, abs(fh - prev_c), abs(fl - prev_c))

        prev_h, prev_l, prev_c = fh, fl, fcl

        if smooth_tr is None:
            tr_buf.append(tr)
            pdm_buf.append(pdm)
            mdm_buf.append(mdm)
            if len(tr_buf) < period:
                continue
            smooth_tr = sum(tr_buf)
            smooth_pdm = sum(pdm_buf)
            smooth_mdm = sum(mdm_buf)
        else:
            smooth_tr = smooth_tr - (smooth_tr / float(period)) + tr
            smooth_pdm = smooth_pdm - (smooth_pdm / float(period)) + pdm  # type: ignore[operator]
            smooth_mdm = smooth_mdm - (smooth_mdm / float(period)) + mdm  # type: ignore[operator]

        if smooth_tr and smooth_tr > 0.0:
            pdi = 100.0 * (smooth_pdm / smooth_tr)  # type: ignore[operator]
            mdi = 100.0 * (smooth_mdm / smooth_tr)  # type: ignore[operator]
        else:
            pdi = 0.0
            mdi = 0.0

        plus_di_out[i] = pdi
        minus_di_out[i] = mdi

        di_sum = pdi + mdi
        dx = (100.0 * abs(pdi - mdi) / di_sum) if di_sum > 0.0 else 0.0

        if adx_prev is None:
            dx_buf.append(dx)
            if len(dx_buf) == period:
                adx_prev = sum(dx_buf) / float(period)
                adx_out[i] = adx_prev
        else:
            adx_prev = ((adx_prev * (period - 1)) + dx) / float(period)
            adx_out[i] = adx_prev

    return {
        "value": adx_out,
        "adx": adx_out,
        "plus_di": plus_di_out,
        "minus_di": minus_di_out,
    }


def evaluate_indicator_config(
    indicator: dict,
    candles: Sequence[dict],
) -> dict[str, list[Optional[float]]]:
    """Compute all output series for a single schema_v2 Indicator configuration."""
    ind_type = str(indicator.get("type", "")).upper()
    source = str(indicator.get("source", "CLOSE")).upper()
    series = extract_series(candles, source)

    if ind_type == "SMA":
        res = compute_sma(series, int(indicator["period"]))
        return {"value": res}
    if ind_type == "EMA":
        res = compute_ema(series, int(indicator["period"]))
        return {"value": res}
    if ind_type == "RSI":
        return compute_rsi(
            series,
            period=int(indicator["period"]),
            smoothing_period=int(indicator.get("smoothingPeriod") or indicator["period"]),
        )
    if ind_type == "CCI":
        res = compute_cci(candles, period=int(indicator["period"]))
        return {"value": res}
    if ind_type == "ATR":
        res = compute_atr(candles, period=int(indicator["period"]))
        return {"value": res}
    if ind_type == "SUPERTREND":
        return compute_supertrend(
            candles,
            period=int(indicator["period"]),
            multiplier=float(indicator["multiplier"]),
        )
    if ind_type == "VWAP":
        res = compute_vwap(candles)
        return {"value": res}
    if ind_type == "MACD":
        return compute_macd(
            series,
            fast_period=int(indicator["fastPeriod"]),
            slow_period=int(indicator["slowPeriod"]),
            signal_period=int(indicator["signalPeriod"]),
        )
    if ind_type == "BOLLINGER":
        return compute_bollinger(
            series,
            period=int(indicator["period"]),
            std_dev=float(indicator["stdDev"]),
        )
    if ind_type == "STOCHASTIC":
        return compute_stochastic(
            candles,
            k_period=int(indicator["kPeriod"]),
            d_period=int(indicator["dPeriod"]),
        )
    if ind_type == "ADX":
        return compute_adx(candles, period=int(indicator.get("period") or 14))
    raise ValueError(f"Unsupported indicator type '{ind_type}'.")
