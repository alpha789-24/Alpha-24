"""
Stop-and-Reverse (Level SAR) engine — pure functions.
No network, no file I/O, no clock reads.
All times and prices are passed in by the caller.
"""
from uuid import uuid4
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

VALID_TIMEFRAMES = {"1m", "3m", "5m", "10m", "15m", "30m", "60m"}
VALID_LEVEL_SOURCES_CANDLE = {"HIGH", "LOW", "OPEN", "CLOSE", "AVG", "PREV_CLOSE", "DAY_HIGH", "DAY_LOW"}
FINAL_STATUSES = {"COMPLETED", "STOPPED_MAX_LEGS", "STOPPED_RISK_LIMIT", "STOPPED_MANUAL", "EXPIRED", "ERROR"}

# ---------- helpers ----------

def _parse_time(s):
    """Parse 'HH:MM' string to (hour, minute) tuple."""
    parts = s.strip().split(":")
    return int(parts[0]), int(parts[1])


def _time_to_minutes(hh, mm):
    return hh * 60 + mm


def _minutes_to_str(m):
    return f"{m // 60:02d}:{m % 60:02d}"


# ---------- parse_config ----------

def parse_config(strategy):
    """Return a normalized config dict with defaults filled in."""
    sar = strategy.get("sar") or {}
    cfg = {}
    cfg["strategyType"] = strategy.get("strategyType", "LEVEL_SAR")
    cfg["strategyId"] = strategy.get("id", "")
    cfg["strategyName"] = strategy.get("name", "")
    cfg["symbol"] = strategy.get("instrument", "")

    cfg["levelSource"] = sar.get("levelSource", "MANUAL")
    cfg["entryStyle"] = sar.get("entryStyle",
                                "REVERSAL" if cfg["levelSource"] == "MANUAL" else "BREAKOUT")

    manual = sar.get("manual") or {}
    cfg["manualUpper"] = float(manual.get("upper", 0))
    cfg["manualLower"] = float(manual.get("lower", 0))

    candle = sar.get("candle") or {}
    cfg["candleTimeframe"] = candle.get("timeframe", "15m")
    cfg["candleUpperSource"] = candle.get("upperSource", "HIGH")
    cfg["candleLowerSource"] = candle.get("lowerSource", "LOW")

    cfg["slPoints"] = float(sar.get("slPoints", 1))
    cfg["tgtPoints"] = float(sar.get("tgtPoints", 1.5))
    cfg["baseQuantity"] = int(sar.get("baseQuantity", 100))
    cfg["maxLegs"] = int(sar.get("maxLegs", 4))

    raw_mult = sar.get("multipliers") or []
    # Ensure list of numbers
    multipliers = []
    for m in raw_mult:
        try:
            multipliers.append(float(m))
        except (TypeError, ValueError):
            multipliers.append(2.0)
    cfg["multipliers"] = multipliers

    cfg["startTime"] = sar.get("startTime", "09:15")
    cfg["endTime"] = sar.get("endTime", "15:00")

    cfg["afterTarget"] = sar.get("afterTarget", "STOP")
    cfg["maxRestarts"] = int(sar.get("maxRestarts", 2))

    risk = strategy.get("risk") or {}
    cfg["maxDailyLoss"] = float(risk.get("maxDailyLoss", 2000))
    cfg["maxCapital"] = float(risk.get("maxCapital", 25000))

    return cfg


# ---------- validate_sar ----------

def validate_sar(strategy, reference_price=None):
    """Return (errors, warnings) lists of readable strings.

    reference_price: live underlying price used for capital checks when levels come
    from candles (instead of a fixed placeholder) and for sanity-checking manual levels.
    """
    errors = []
    warnings = []
    cfg = parse_config(strategy)

    # Name check
    if not strategy.get("name", "").strip():
        errors.append("Strategy name is required.")

    # Level validation
    if cfg["levelSource"] == "MANUAL":
        if cfg["manualUpper"] <= cfg["manualLower"]:
            errors.append("Upper level must be greater than lower level.")
        if cfg["manualUpper"] <= 0:
            errors.append("Upper level must be a positive number.")
        if cfg["manualLower"] <= 0:
            errors.append("Lower level must be a positive number.")
    elif cfg["levelSource"] == "CANDLE":
        if cfg["candleTimeframe"] not in VALID_TIMEFRAMES:
            errors.append(f"Unsupported candle timeframe: {cfg['candleTimeframe']}.")
        if cfg["candleUpperSource"] not in VALID_LEVEL_SOURCES_CANDLE:
            errors.append(f"Unsupported upper level source: {cfg['candleUpperSource']}.")
        if cfg["candleLowerSource"] not in VALID_LEVEL_SOURCES_CANDLE:
            errors.append(f"Unsupported lower level source: {cfg['candleLowerSource']}.")
    else:
        errors.append(f"Unknown levelSource: {cfg['levelSource']}.")

    if cfg["entryStyle"] not in ("REVERSAL", "BREAKOUT"):
        errors.append(f"Unknown entryStyle: {cfg['entryStyle']}.")

    # Points
    if cfg["slPoints"] <= 0:
        errors.append("Stop-loss points must be greater than zero.")
    if cfg["tgtPoints"] <= 0:
        errors.append("Target points must be greater than zero.")

    # Quantity
    if cfg["baseQuantity"] < 1:
        errors.append("Base quantity must be at least 1.")
    if cfg["baseQuantity"] != int(cfg["baseQuantity"]):
        errors.append("Base quantity must be a whole number.")

    # Max legs
    if cfg["maxLegs"] < 2 or cfg["maxLegs"] > 6:
        errors.append("Maximum legs must be between 2 and 6.")

    # Multipliers
    expected_mult_count = max(0, cfg["maxLegs"] - 1)
    if len(cfg["multipliers"]) != expected_mult_count:
        errors.append(f"Multipliers must have exactly {expected_mult_count} entries (one per reversal step), got {len(cfg['multipliers'])}.")
    for i, m in enumerate(cfg["multipliers"]):
        if m < 1:
            errors.append(f"Multiplier at position {i + 1} must be at least 1.")

    # Times
    try:
        sh, sm = _parse_time(cfg["startTime"])
        eh, em = _parse_time(cfg["endTime"])
        start_m = _time_to_minutes(sh, sm)
        end_m = _time_to_minutes(eh, em)
        trading_start = _time_to_minutes(9, 15)
        trading_end = _time_to_minutes(15, 30)

        if start_m >= end_m:
            errors.append("Start time must be before end time.")
        if start_m < trading_start or start_m > trading_end:
            errors.append(f"Start time must be between 09:15 and 15:30.")
        if end_m < trading_start or end_m > trading_end:
            errors.append(f"End time must be between 09:15 and 15:30.")
    except (ValueError, IndexError):
        errors.append("Invalid time format. Use HH:MM.")

    # Risk checks
    risk = strategy.get("risk") or {}
    if float(risk.get("maxDailyLoss", 0)) <= 0:
        errors.append("Set a positive maximum daily loss.")
    if float(risk.get("maxCapital", 0)) <= 0:
        errors.append("Set a positive capital cap.")

    # Warnings: worst-case loss vs maxDailyLoss
    if not errors and cfg["maxLegs"] >= 2 and len(cfg["multipliers"]) == cfg["maxLegs"] - 1:
        worst_loss = 0
        qty = cfg["baseQuantity"]
        for leg_no in range(1, cfg["maxLegs"] + 1):
            leg_loss = cfg["slPoints"] * qty
            cumulative = worst_loss + leg_loss
            if cumulative > cfg["maxDailyLoss"]:
                warnings.append(
                    f"Leg {leg_no} would be blocked by the daily loss cap (cumulative risk Rs {cumulative:,.0f} "
                    f"exceeds max Rs {cfg['maxDailyLoss']:,.0f})."
                )
            worst_loss = cumulative
            if leg_no < cfg["maxLegs"]:
                qty = max(1, round(qty * cfg["multipliers"][leg_no - 1]))

        # Largest leg notional
        ref = float(reference_price) if reference_price and float(reference_price) > 0 else None
        if cfg["levelSource"] == "MANUAL":
            level_price = max(cfg["manualUpper"], cfg["manualLower"])
            if ref and (level_price < ref * 0.9 or min(cfg["manualUpper"], cfg["manualLower"]) > ref * 1.1):
                warnings.append(
                    f"Manual levels ({cfg['manualLower']:g} / {cfg['manualUpper']:g}) are far from the live price "
                    f"(Rs {ref:,.2f}). Confirm they are absolute prices, not offsets or test values."
                )
        elif ref:
            level_price = ref
        else:
            level_price = None  # unknown without a live price; do not guess

        qty_check = cfg["baseQuantity"]
        max_notional = qty_check * level_price
        for i in range(cfg["maxLegs"] - 1):
            qty_check = max(1, round(qty_check * cfg["multipliers"][i]))
            notional = qty_check * level_price
            if notional > max_notional:
                max_notional = notional

        if level_price is None:
            warnings.append("Capital requirement not checked: no live price available for candle-based levels.")
        elif max_notional > cfg["maxCapital"]:
            warnings.append(
                f"Largest leg needs about Rs {max_notional:,.0f} ({qty_check} x Rs {level_price:,.0f}); "
                f"your max capital allocation is Rs {cfg['maxCapital']:,.0f}."
            )

    return errors, warnings


def risk_breakdown(cfg, entry_price=None):
    """Per-leg calculation breakdown with the exact reason a leg is blocked.

    Quantity is in underlying units (not lots). Exposure = qty x entry price (notional, not margin).
    """
    rows = []
    cumulative = 0.0
    qty = cfg["baseQuantity"]
    for leg_no in range(1, cfg["maxLegs"] + 1):
        leg_loss = cfg["slPoints"] * qty
        cumulative += leg_loss
        reasons = []
        if cumulative > cfg["maxDailyLoss"]:
            reasons.append(
                f"Cumulative risk {cumulative:,.0f} (sum of SL {cfg['slPoints']:g} pts x qty for legs 1-{leg_no}) "
                f"exceeds max daily loss {cfg['maxDailyLoss']:,.0f}"
            )
        exposure = qty * entry_price if entry_price else None
        capital_exceeded = bool(exposure is not None and exposure > cfg["maxCapital"])
        rows.append({
            "legNo": leg_no, "qty": qty, "legLoss": round(leg_loss, 2),
            "cumulativeLoss": round(cumulative, 2),
            "exposure": round(exposure, 2) if exposure is not None else None,
            "blocked": bool(reasons), "reasons": reasons,
            "capitalExceeded": capital_exceeded,
        })
        if leg_no < cfg["maxLegs"] and leg_no - 1 < len(cfg["multipliers"]):
            qty = max(1, round(qty * cfg["multipliers"][leg_no - 1]))
    return rows


# ---------- build_ladder ----------

def build_ladder(cfg, side, entry_price):
    """
    Build a list of legs [{legNo, side, entry, sl, tgt, qty, maxLoss}].
    All legs alternate sides starting from the given side.
    """
    legs = []
    qty = cfg["baseQuantity"]
    current_side = side
    current_entry = entry_price

    for leg_no in range(1, cfg["maxLegs"] + 1):
        if current_side == "SELL":
            sl = round(current_entry + cfg["slPoints"], 10)
            tgt = round(current_entry - cfg["tgtPoints"], 10)
        else:  # BUY
            sl = round(current_entry - cfg["slPoints"], 10)
            tgt = round(current_entry + cfg["tgtPoints"], 10)

        max_loss = round(cfg["slPoints"] * qty, 2)

        legs.append({
            "legNo": leg_no,
            "side": current_side,
            "entry": round(current_entry, 10),
            "sl": sl,
            "tgt": tgt,
            "qty": qty,
            "maxLoss": max_loss
        })

        # Next leg: opposite side, entry = this leg's SL, sl = this leg's entry
        next_entry = sl
        next_side = "BUY" if current_side == "SELL" else "SELL"

        # Calculate next qty
        if leg_no < cfg["maxLegs"] and leg_no - 1 < len(cfg["multipliers"]):
            qty = max(1, round(qty * cfg["multipliers"][leg_no - 1]))

        current_side = next_side
        current_entry = next_entry

    return legs


# ---------- compute_levels ----------

def compute_levels(cfg, level_candle=None, prev_close=None, day_high=None, day_low=None):
    """
    Compute upper and lower levels.
    Returns {upper, lower, upperSource, lowerSource} or None if cannot compute.
    """
    if cfg["levelSource"] == "MANUAL":
        return {
            "upper": cfg["manualUpper"],
            "lower": cfg["manualLower"],
            "upperSource": "MANUAL",
            "lowerSource": "MANUAL"
        }

    if cfg["levelSource"] == "CANDLE":
        if level_candle is None or not level_candle.get("complete"):
            return None

        candle_o = level_candle.get("open", 0)
        candle_h = level_candle.get("high", 0)
        candle_l = level_candle.get("low", 0)
        candle_c = level_candle.get("close", 0)
        candle_avg = round((candle_o + candle_h + candle_l + candle_c) / 4, 2)

        source_map = {
            "HIGH": candle_h,
            "LOW": candle_l,
            "OPEN": candle_o,
            "CLOSE": candle_c,
            "AVG": candle_avg,
            "PREV_CLOSE": prev_close if prev_close is not None else 0,
            "DAY_HIGH": day_high if day_high is not None else 0,
            "DAY_LOW": day_low if day_low is not None else 0
        }

        upper = source_map.get(cfg["candleUpperSource"], 0)
        lower = source_map.get(cfg["candleLowerSource"], 0)

        return {
            "upper": round(upper, 2),
            "lower": round(lower, 2),
            "upperSource": cfg["candleUpperSource"],
            "lowerSource": cfg["candleLowerSource"]
        }

    return None


# ---------- new_run ----------

def new_run(strategy, cfg):
    """Create a new run dict."""
    now = datetime.now(IST).isoformat()
    return {
        "id": str(uuid4()),
        "strategyId": cfg["strategyId"],
        "strategyName": cfg["strategyName"],
        "symbol": cfg["symbol"],
        "status": "WAITING_START",
        "levels": None,
        "prevPrice": None,
        "chainStarted": False,
        "activeLevelSide": None,   # which level started the chain: "UPPER" or "LOWER"
        "currentLeg": None,
        "legs": [],
        "realizedPnl": 0.0,
        "realizedLoss": 0.0,  # sum of absolute values of negative pnls
        "restarts": 0,
        "message": "",
        "createdAt": now,
        "updatedAt": now
    }


# ---------- step ----------

def _open_leg(run, cfg, leg_no, side, entry_price):
    """Open a new leg, returning an OPEN_LEG event or None if risk-blocked."""
    qty = cfg["baseQuantity"]
    for i in range(leg_no - 1):
        if i < len(cfg["multipliers"]):
            qty = max(1, round(qty * cfg["multipliers"][i]))

    potential_loss = cfg["slPoints"] * qty
    if run["realizedLoss"] + potential_loss > cfg["maxDailyLoss"]:
        run["status"] = "STOPPED_RISK_LIMIT"
        run["message"] = f"Risk limit: opening leg {leg_no} (risk Rs {potential_loss:,.0f}) would exceed daily loss cap."
        return {"type": "RISK_BLOCKED", "legNo": leg_no, "potentialLoss": potential_loss}

    if side == "SELL":
        sl = round(entry_price + cfg["slPoints"], 10)
        tgt = round(entry_price - cfg["tgtPoints"], 10)
    else:  # BUY
        sl = round(entry_price - cfg["slPoints"], 10)
        tgt = round(entry_price + cfg["tgtPoints"], 10)

    leg = {
        "legNo": leg_no,
        "side": side,
        "entry": round(entry_price, 10),
        "sl": round(sl, 10),
        "tgt": round(tgt, 10),
        "qty": qty,
        "status": "OPEN"
    }

    run["currentLeg"] = leg
    run["status"] = "IN_TRADE"
    run["message"] = f"Leg {leg_no} {side} opened at {entry_price}"

    return {"type": "OPEN_LEG", "leg": dict(leg)}


def _close_leg(run, cfg, exit_price, reason):
    """Close the current leg and return a CLOSE_LEG event."""
    leg = run["currentLeg"]
    if leg is None:
        return None

    entry = leg["entry"]
    qty = leg["qty"]
    side = leg["side"]

    if side == "BUY":
        pnl = round((exit_price - entry) * qty, 2)
    else:
        pnl = round((entry - exit_price) * qty, 2)

    leg["status"] = "CLOSED"
    leg["exitPrice"] = round(exit_price, 10)
    leg["pnl"] = pnl
    leg["exitReason"] = reason

    run["legs"].append(dict(leg))
    run["realizedPnl"] = round(run["realizedPnl"] + pnl, 2)
    if pnl < 0:
        run["realizedLoss"] = round(run["realizedLoss"] + abs(pnl), 2)

    run["currentLeg"] = None

    return {"type": "CLOSE_LEG", "leg": dict(leg), "exitPrice": round(exit_price, 10), "pnl": pnl, "reason": reason}


def step(run, cfg, cmp, now_ist, level_data=None):
    """
    Process one tick. Returns a list of events.
    Modifies run dict in place.
    now_ist: datetime object in IST.
    level_data: {open, high, low, close, complete} for candle levels, or None.
    """
    events = []
    run["updatedAt"] = now_ist.isoformat() if hasattr(now_ist, 'isoformat') else str(now_ist)

    # Check if status is final
    if run["status"] in FINAL_STATUSES:
        return events

    # Parse times
    sh, sm = _parse_time(cfg["startTime"])
    eh, em = _parse_time(cfg["endTime"])
    start_m = _time_to_minutes(sh, sm)
    end_m = _time_to_minutes(eh, em)

    if hasattr(now_ist, 'hour'):
        now_m = _time_to_minutes(now_ist.hour, now_ist.minute)
    else:
        now_m = 0

    # Before start time
    if now_m < start_m:
        run["status"] = "WAITING_START"
        run["message"] = f"Waiting for start time {cfg['startTime']}"
        return events

    # Resolve levels if not yet set
    if run["levels"] is None:
        if cfg["levelSource"] == "MANUAL":
            levels = compute_levels(cfg)
            if levels and levels["upper"] <= levels["lower"]:
                run["status"] = "ERROR"
                run["message"] = "Levels invalid: upper must be above lower"
                return events
            run["levels"] = levels
            run["status"] = "ARMED"
            run["message"] = f"Levels set: upper {levels['upper']}, lower {levels['lower']}"
        elif cfg["levelSource"] == "CANDLE":
            run["status"] = "WAITING_FOR_LEVELS"
            if level_data is not None:
                levels = compute_levels(cfg, level_candle=level_data,
                                        prev_close=level_data.get("prev_close"),
                                        day_high=level_data.get("day_high"),
                                        day_low=level_data.get("day_low"))
                if levels is not None:
                    if levels["upper"] <= levels["lower"]:
                        run["status"] = "ERROR"
                        run["message"] = "Levels invalid: upper must be above lower"
                        return events
                    run["levels"] = levels
                    run["status"] = "ARMED"
                    run["message"] = f"Levels locked: upper {levels['upper']}, lower {levels['lower']}"
                else:
                    run["message"] = "Waiting for level candle to close"
            else:
                run["message"] = "Waiting for level candle data"
            if run["status"] != "ARMED":
                run["prevPrice"] = cmp
                return events

    # At end time: close open leg or expire
    if now_m >= end_m:
        if run["currentLeg"] is not None:
            evt = _close_leg(run, cfg, cmp, "TIME_EXIT")
            if evt:
                events.append(evt)
            run["status"] = "COMPLETED"
            run["message"] = f"Time exit at {_minutes_to_str(end_m)}"
        elif run["status"] in ("ARMED", "WAITING_FOR_LEVELS", "WAITING_START"):
            run["status"] = "EXPIRED"
            run["message"] = "End time reached without a trade"
        return events

    # First tick: store prevPrice and return (never trade on first tick)
    if run["prevPrice"] is None:
        run["prevPrice"] = cmp
        if run["status"] not in ("IN_TRADE",):
            run["status"] = "ARMED"
            run["message"] = "Armed, waiting for level trigger"
        return events

    prev = run["prevPrice"]
    upper = run["levels"]["upper"]
    lower = run["levels"]["lower"]

    # Process open leg SL/TGT checks first (loop for gaps)
    max_iterations = cfg["maxLegs"] + 1  # safety
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        changed = False

        if run["currentLeg"] is not None:
            leg = run["currentLeg"]
            side = leg["side"]
            sl = leg["sl"]
            tgt = leg["tgt"]
            leg_no = leg["legNo"]

            sl_hit = False
            tgt_hit = False

            if side == "SELL":
                sl_hit = cmp >= sl
                tgt_hit = cmp <= tgt
            else:  # BUY
                sl_hit = cmp <= sl
                tgt_hit = cmp >= tgt

            if sl_hit:
                # Close at SL price
                evt = _close_leg(run, cfg, sl, "STOP_LOSS")
                if evt:
                    events.append(evt)

                # Check if max legs reached
                if leg_no >= cfg["maxLegs"]:
                    run["status"] = "STOPPED_MAX_LEGS"
                    run["message"] = f"All {cfg['maxLegs']} legs exhausted"
                    break

                # Open next leg in opposite direction
                next_side = "BUY" if side == "SELL" else "SELL"
                next_entry = sl  # entry = previous leg's SL price
                open_evt = _open_leg(run, cfg, leg_no + 1, next_side, next_entry)
                if open_evt:
                    events.append(open_evt)
                    if open_evt["type"] == "RISK_BLOCKED":
                        break
                changed = True
                continue  # re-check the new leg in same tick

            elif tgt_hit:
                # Close at TGT price
                evt = _close_leg(run, cfg, tgt, "TARGET")
                if evt:
                    events.append(evt)

                # After target
                if cfg["afterTarget"] == "RESTART" and run["restarts"] < cfg["maxRestarts"]:
                    run["restarts"] += 1
                    run["chainStarted"] = False
                    run["activeLevelSide"] = None
                    run["status"] = "ARMED"
                    run["message"] = f"Restart {run['restarts']}/{cfg['maxRestarts']}, re-armed"
                    # prevPrice continues from current tick
                else:
                    run["status"] = "COMPLETED"
                    run["message"] = "Target hit, strategy completed"
                break

        elif run["status"] == "ARMED" and not run["chainStarted"]:
            # Check for level triggers
            triggered_side = None  # "UPPER" or "LOWER"
            trade_side = None      # "BUY" or "SELL"
            trigger_price = None

            if cfg["entryStyle"] == "REVERSAL":
                # SELL when CMP rises to UPPER: prev < UPPER and cmp >= UPPER
                upper_trigger = prev < upper and cmp >= upper
                # BUY when CMP falls to LOWER: prev > LOWER and cmp <= LOWER
                lower_trigger = prev > lower and cmp <= lower

                if upper_trigger and lower_trigger:
                    # Both triggered in same tick (extreme gap) — pick the one closer to CMP
                    upper_trigger = True
                    lower_trigger = False

                if upper_trigger:
                    triggered_side = "UPPER"
                    trade_side = "SELL"
                    trigger_price = upper
                elif lower_trigger:
                    triggered_side = "LOWER"
                    trade_side = "BUY"
                    trigger_price = lower

            elif cfg["entryStyle"] == "BREAKOUT":
                # BUY when CMP goes above UPPER: prev <= UPPER and cmp > UPPER
                upper_trigger = prev <= upper and cmp > upper
                # SELL when CMP goes below LOWER: prev >= LOWER and cmp < LOWER
                lower_trigger = prev >= lower and cmp < lower

                if upper_trigger and lower_trigger:
                    upper_trigger = True
                    lower_trigger = False

                if upper_trigger:
                    triggered_side = "UPPER"
                    trade_side = "BUY"
                    trigger_price = upper
                elif lower_trigger:
                    triggered_side = "LOWER"
                    trade_side = "SELL"
                    trigger_price = lower

            if triggered_side is not None:
                run["chainStarted"] = True
                run["activeLevelSide"] = triggered_side
                open_evt = _open_leg(run, cfg, 1, trade_side, trigger_price)
                if open_evt:
                    events.append(open_evt)
                    if open_evt["type"] == "RISK_BLOCKED":
                        break
                changed = True
                continue  # re-check for SL/TGT in same tick
            break  # no trigger

        else:
            break  # nothing more to do

        if not changed:
            break

    run["prevPrice"] = cmp
    return events

