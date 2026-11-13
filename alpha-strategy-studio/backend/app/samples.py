"""Sample strategy definitions for Phases 2 and 3 (Entry, Sequence, and Re-entry examples).

All sample strategies are represented as standard RULE_BUILDER documents (never hard-coded in the engine)
and exported to `samples/*.json`:
1. `sample_buy_near_rc1_breakout_strategy`: Phase 2 BUY near RC1L or moves out of FC1H with explicit tolerances.
2. `sample_buy_reentry_example_1`: Phase 3 Sequential BUY Re-entry Example 1
   (Step 1: price trades below RC1L -> Step 2: price trades above RC1H -> Enter BUY with re-entry SL & targets).
3. `sample_buy_reentry_example_2`: Phase 3 Sequential BUY Re-entry Example 2
   (Step 1: price trades below FC1L -> Step 2: candle is green AND price crosses above FC1H -> Enter BUY).
4. `sample_sell_reentry_example_1`: Phase 3 Sequential SELL Re-entry Example 1
   (Step 1: price trades above RC1H -> Step 2: price trades below RC1L -> Enter SELL with re-entry SL & targets).
5. `sample_unresolved_sell_reentry_example_2`: Preserves SELL Re-entry Example 2 verbatim
   (listed under SELL re-entry, but entry instruction says "enter BUY with SL RC3L"), flagged with
   `unresolvedConflict=True` and blocked from trading activation until confirmed by the user.
"""
from __future__ import annotations

import copy
from .schema_v2 import new_rule_builder


def sample_buy_near_rc1_breakout_strategy() -> dict:
    """Sample strategy demonstrating BUY NEAR RC1L and breakout out of FC1H with explicit tolerances."""
    doc = new_rule_builder()
    doc["id"] = "sample-phase2-buy-near-rc1"
    doc["name"] = "Sample Phase 2: BUY Near RC1L or Breakout FC1H"
    doc["meta"]["id"] = doc["id"]
    doc["meta"]["name"] = doc["name"]
    doc["meta"]["symbol"] = "NIFTY 50"
    doc["meta"]["description"] = (
        "Evaluates spot NIFTY 50 candles with manual FC/RC levels, RSI(14,14), and explicit proximity/breakout tolerances."
    )
    doc["indicators"] = [
        {
            "id": "rsi-14",
            "enabled": True,
            "order": 1,
            "label": "RSI 14 (SMA 14)",
            "type": "RSI",
            "timeframe": "5m",
            "source": "CLOSE",
            "period": 14,
            "smoothingPeriod": 14,
            "multiplier": None,
            "fastPeriod": None,
            "slowPeriod": None,
            "signalPeriod": None,
            "stdDev": None,
            "kPeriod": None,
            "dPeriod": None,
        }
    ]
    doc["execution"]["defaultEvaluationMode"] = "CANDLE_CLOSE"
    doc["execution"]["intrabarConflictOrder"] = "SL_FIRST"
    doc["execution"]["instrument"] = {
        "underlying": "NIFTY 50",
        "signalSource": "INDEX_SPOT",
        "instrumentType": "INDEX_OPTIONS",
        "optionType": "CE",
        "strike": "ATM",
        "expiry": "CURRENT_WEEK",
        "contractSymbol": "NIFTY 50 ATM CE",
        "lotSize": None,
    }
    doc["rules"]["entry"] = [
        {
            "id": "entry-buy-1",
            "enabled": True,
            "order": 1,
            "label": "BUY when price near RC1L or breaks out of FC1H with green candle",
            "side": "BUY",
            "condition": {
                "type": "group",
                "id": "grp-entry-1",
                "enabled": True,
                "order": 1,
                "label": "Main Entry Group",
                "logic": "AND",
                "children": [
                    {
                        "type": "leaf",
                        "id": "leaf-green",
                        "enabled": True,
                        "order": 1,
                        "label": "5m candle is green",
                        "left": None,
                        "operator": "candle_green",
                        "right": None,
                        "timeframe": "5m",
                        "candleRef": "current",
                        "evaluateOn": "CANDLE_CLOSE",
                        "params": {"tolerance": None, "toleranceUnit": None},
                    },
                    {
                        "type": "group",
                        "id": "grp-or-level",
                        "enabled": True,
                        "order": 2,
                        "label": "Near RC1L or Moves Out of FC1H",
                        "logic": "OR",
                        "children": [
                            {
                                "type": "leaf",
                                "id": "leaf-buy-near-rc1l",
                                "enabled": True,
                                "order": 1,
                                "label": "CLOSE buy_near RC1L within 5 points",
                                "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                                "operator": "buy_near",
                                "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC1L", "value": None},
                                "timeframe": "5m",
                                "candleRef": "current",
                                "evaluateOn": "CANDLE_CLOSE",
                                "params": {"tolerance": 5.0, "toleranceUnit": "points"},
                            },
                            {
                                "type": "leaf",
                                "id": "leaf-out-of-fc1h",
                                "enabled": True,
                                "order": 2,
                                "label": "CLOSE moves_out_of FC1H by 3 points",
                                "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                                "operator": "moves_out_of",
                                "right": {"kind": "level", "field": None, "indicatorId": None, "level": "FC1H", "value": None},
                                "timeframe": "5m",
                                "candleRef": "current",
                                "evaluateOn": "CANDLE_CLOSE",
                                "params": {
                                    "tolerance": 3.0,
                                    "toleranceUnit": "points",
                                    "crossingMode": "close_cross",
                                    "breakoutDirection": "ABOVE",
                                },
                            },
                        ],
                    },
                ],
            },
        }
    ]
    return doc


def sample_buy_reentry_example_1() -> dict:
    """Phase 3 Sample: BUY Re-entry Example 1 (Ordered 2-Step Sequence).

    Step 1: Price trades below RC1L
    Step 2: Price trades above RC1H (on a subsequent evaluation tick, allowSameTick=False)
    Re-entry: Enabled after Stop Loss or Target, max 2 per day, 5-minute cooldown, dedicated SL at RC1L.
    """
    doc = new_rule_builder()
    doc["id"] = "sample-phase3-buy-reentry-1"
    doc["name"] = "Sample Phase 3: BUY Re-entry Example 1 (Below RC1L then Above RC1H)"
    doc["meta"]["id"] = doc["id"]
    doc["meta"]["name"] = doc["name"]
    doc["meta"]["symbol"] = "NIFTY 50"
    doc["meta"]["description"] = (
        "Ordered 2-step sequence: Step 1 price trades below RC1L, then Step 2 price trades above RC1H. "
        "Includes re-entry after SL/Target with 5m cooldown and dedicated re-entry SL/targets."
    )
    doc["execution"]["defaultEvaluationMode"] = "CANDLE_CLOSE"
    doc["execution"]["intrabarConflictOrder"] = "SL_FIRST"
    doc["execution"]["instrument"] = {
        "underlying": "NIFTY 50",
        "signalSource": "INDEX_SPOT",
        "instrumentType": "INDEX_OPTIONS",
        "optionType": "CE",
        "strike": "ATM",
        "expiry": "CURRENT_WEEK",
        "contractSymbol": "NIFTY 50 ATM CE",
        "lotSize": None,
    }
    doc["rules"]["entry"] = [
        {
            "id": "rule-buy-seq-rc1",
            "enabled": True,
            "order": 1,
            "label": "BUY when price trades below RC1L then trades above RC1H",
            "side": "BUY",
            "condition": {
                "type": "sequence",
                "id": "seq-buy-rc1",
                "enabled": True,
                "order": 1,
                "label": "Step 1: Below RC1L -> Step 2: Above RC1H",
                "allowSameTick": False,
                "timeoutMinutes": 60,
                "steps": [
                    {
                        "type": "leaf",
                        "id": "step-1-below-rc1l",
                        "enabled": True,
                        "order": 1,
                        "label": "Price trades below RC1L",
                        "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                        "operator": "trades_below",
                        "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC1L", "value": None},
                        "timeframe": "5m",
                        "candleRef": "current",
                        "evaluateOn": "CANDLE_CLOSE",
                        "params": {"tolerance": None, "toleranceUnit": None, "tradeBasis": "CANDLE_CLOSE"},
                    },
                    {
                        "type": "leaf",
                        "id": "step-2-above-rc1h",
                        "enabled": True,
                        "order": 2,
                        "label": "Price trades above RC1H",
                        "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                        "operator": "trades_above",
                        "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC1H", "value": None},
                        "timeframe": "5m",
                        "candleRef": "current",
                        "evaluateOn": "CANDLE_CLOSE",
                        "params": {"tolerance": None, "toleranceUnit": None, "tradeBasis": "CANDLE_CLOSE"},
                    },
                ],
            },
        }
    ]
    doc["reentry"] = {
        "enabled": True,
        "maxPerDay": 2,
        "afterStopLoss": True,
        "afterTarget": True,
        "afterManualExit": False,
        "cooldownMinutes": 5,
        "directions": ["BUY"],
        "stopLoss": {
            "id": "reentry-sl-rc1l",
            "enabled": True,
            "order": 1,
            "label": "Re-entry SL at RC1L",
            "method": "level",
            "points": None,
            "percent": None,
            "indicatorId": None,
            "candlePrice": None,
            "candleCount": None,
            "bufferPercent": None,
            "level": "RC1L",
        },
        "targets": [
            {
                "id": "reentry-tgt-1",
                "enabled": True,
                "order": 1,
                "label": "Re-entry Target RC2H",
                "value": 0.0,
                "unit": "price_level",
                "quantity": 1,
                "level": "RC2H",
            }
        ],
    }
    return doc


def sample_buy_reentry_example_2() -> dict:
    """Phase 3 Sample: BUY Re-entry Example 2 (Below FC1L -> Green candle & Crosses Above FC1H)."""
    doc = sample_buy_reentry_example_1()
    doc = copy.deepcopy(doc)
    doc["id"] = "sample-phase3-buy-reentry-2"
    doc["name"] = "Sample Phase 3: BUY Re-entry Example 2 (Below FC1L then Green Cross Above FC1H)"
    doc["meta"]["id"] = doc["id"]
    doc["meta"]["name"] = doc["name"]
    doc["meta"]["description"] = (
        "Ordered 2-step sequence: Step 1 price trades below FC1L, Step 2 green candle AND price crosses above FC1H."
    )
    doc["rules"]["entry"] = [
        {
            "id": "rule-buy-seq-fc1",
            "enabled": True,
            "order": 1,
            "label": "BUY when price trades below FC1L then green candle crosses above FC1H",
            "side": "BUY",
            "condition": {
                "type": "sequence",
                "id": "seq-buy-fc1",
                "enabled": True,
                "order": 1,
                "label": "Step 1: Below FC1L -> Step 2: Green & Crosses Above FC1H",
                "allowSameTick": False,
                "timeoutMinutes": 90,
                "steps": [
                    {
                        "type": "leaf",
                        "id": "step-1-below-fc1l",
                        "enabled": True,
                        "order": 1,
                        "label": "Price trades below FC1L",
                        "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                        "operator": "trades_below",
                        "right": {"kind": "level", "field": None, "indicatorId": None, "level": "FC1L", "value": None},
                        "timeframe": "5m",
                        "candleRef": "current",
                        "evaluateOn": "CANDLE_CLOSE",
                        "params": {"tolerance": None, "toleranceUnit": None, "tradeBasis": "CANDLE_CLOSE"},
                    },
                    {
                        "type": "group",
                        "id": "step-2-group-fc1h",
                        "enabled": True,
                        "order": 2,
                        "label": "Green candle AND crosses above FC1H",
                        "logic": "AND",
                        "children": [
                            {
                                "type": "leaf",
                                "id": "step-2a-green",
                                "enabled": True,
                                "order": 1,
                                "label": "5m candle is green",
                                "left": None,
                                "operator": "candle_green",
                                "right": None,
                                "timeframe": "5m",
                                "candleRef": "current",
                                "evaluateOn": "CANDLE_CLOSE",
                                "params": {"tolerance": None, "toleranceUnit": None},
                            },
                            {
                                "type": "leaf",
                                "id": "step-2b-cross-fc1h",
                                "enabled": True,
                                "order": 2,
                                "label": "CLOSE crosses above FC1H",
                                "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                                "operator": "crosses_above",
                                "right": {"kind": "level", "field": None, "indicatorId": None, "level": "FC1H", "value": None},
                                "timeframe": "5m",
                                "candleRef": "current",
                                "evaluateOn": "CANDLE_CLOSE",
                                "params": {"tolerance": None, "toleranceUnit": None},
                            },
                        ],
                    },
                ],
            },
        }
    ]
    return doc


def sample_sell_reentry_example_1() -> dict:
    """Phase 3 Sample: SELL Re-entry Example 1 (Ordered 2-Step Sequence).

    Step 1: Price trades above RC1H
    Step 2: Price trades below RC1L (on a subsequent evaluation tick) -> Enter SELL
    Re-entry: Enabled after Stop Loss, max 2 per day, 5-minute cooldown, dedicated SL at RC1H.
    """
    doc = new_rule_builder()
    doc["id"] = "sample-phase3-sell-reentry-1"
    doc["name"] = "Sample Phase 3: SELL Re-entry Example 1 (Above RC1H then Below RC1L)"
    doc["meta"]["id"] = doc["id"]
    doc["meta"]["name"] = doc["name"]
    doc["meta"]["symbol"] = "NIFTY 50"
    doc["meta"]["description"] = (
        "Ordered 2-step SELL sequence: Step 1 price trades above RC1H, then Step 2 price trades below RC1L. "
        "Re-entry enabled after SL with 5m cooldown and SL at RC1H."
    )
    doc["execution"]["defaultEvaluationMode"] = "CANDLE_CLOSE"
    doc["execution"]["intrabarConflictOrder"] = "SL_FIRST"
    doc["execution"]["instrument"] = {
        "underlying": "NIFTY 50",
        "signalSource": "INDEX_SPOT",
        "instrumentType": "INDEX_OPTIONS",
        "optionType": "PE",
        "strike": "ATM",
        "expiry": "CURRENT_WEEK",
        "contractSymbol": "NIFTY 50 ATM PE",
        "lotSize": None,
    }
    doc["rules"]["entry"] = [
        {
            "id": "rule-sell-seq-rc1",
            "enabled": True,
            "order": 1,
            "label": "SELL when price trades above RC1H then trades below RC1L",
            "side": "SELL",
            "condition": {
                "type": "sequence",
                "id": "seq-sell-rc1",
                "enabled": True,
                "order": 1,
                "label": "Step 1: Above RC1H -> Step 2: Below RC1L",
                "allowSameTick": False,
                "timeoutMinutes": 60,
                "steps": [
                    {
                        "type": "leaf",
                        "id": "step-1-above-rc1h",
                        "enabled": True,
                        "order": 1,
                        "label": "Price trades above RC1H",
                        "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                        "operator": "trades_above",
                        "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC1H", "value": None},
                        "timeframe": "5m",
                        "candleRef": "current",
                        "evaluateOn": "CANDLE_CLOSE",
                        "params": {"tolerance": None, "toleranceUnit": None, "tradeBasis": "CANDLE_CLOSE"},
                    },
                    {
                        "type": "leaf",
                        "id": "step-2-below-rc1l",
                        "enabled": True,
                        "order": 2,
                        "label": "Price trades below RC1L",
                        "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                        "operator": "trades_below",
                        "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC1L", "value": None},
                        "timeframe": "5m",
                        "candleRef": "current",
                        "evaluateOn": "CANDLE_CLOSE",
                        "params": {"tolerance": None, "toleranceUnit": None, "tradeBasis": "CANDLE_CLOSE"},
                    },
                ],
            },
        }
    ]
    doc["reentry"] = {
        "enabled": True,
        "maxPerDay": 2,
        "afterStopLoss": True,
        "afterTarget": False,
        "afterManualExit": False,
        "cooldownMinutes": 5,
        "directions": ["SELL"],
        "stopLoss": {
            "id": "reentry-sl-rc1h",
            "enabled": True,
            "order": 1,
            "label": "Re-entry SL at RC1H",
            "method": "level",
            "points": None,
            "percent": None,
            "indicatorId": None,
            "candlePrice": None,
            "candleCount": None,
            "bufferPercent": None,
            "level": "RC1H",
        },
        "targets": [
            {
                "id": "reentry-tgt-fc1l",
                "enabled": True,
                "order": 1,
                "label": "Re-entry Target FC1L",
                "value": 0.0,
                "unit": "price_level",
                "quantity": 1,
                "level": "FC1L",
            }
        ],
    }
    return doc


def sample_unresolved_sell_reentry_example_2() -> dict:
    """Preserves SELL re-entry example 2 with its original text ('enter BUY with SL RC3L') flagged for confirmation.

    If `enabled` is set to True on the conflicting rule or re-entry block, validation rejects it
    so it can never be silently activated in trading.
    """
    doc = sample_buy_near_rc1_breakout_strategy()
    doc = copy.deepcopy(doc)
    doc["id"] = "sample-sell-reentry-example-2-unresolved"
    doc["name"] = "Sample: SELL Re-entry Example 2 (Unresolved Conflict Flagged)"
    doc["meta"]["id"] = doc["id"]
    doc["meta"]["name"] = doc["name"]
    doc["meta"]["description"] = (
        "Preserves original specification for SELL Re-entry Example 2 where section header is SELL Re-entry "
        "but entry instruction says 'enter BUY with SL RC3L'. Disabled and flagged for user confirmation."
    )
    doc["rules"]["entry"].append({
        "id": "rule-sell-reentry-ex2",
        "enabled": False,
        "order": 2,
        "label": "SELL Re-entry Example 2 (Original text: enter BUY with SL RC3L)",
        "side": "BUY",
        "unresolvedConflict": True,
        "conflictNote": (
            "UNRESOLVED SPECIFICATION CONFLICT: Original specification lists this under 'SELL Re-entry Example 2' "
            "but states 'enter BUY with SL RC3L'. Preserved verbatim and blocked from activation until confirmed."
        ),
        "condition": {
            "type": "sequence",
            "id": "seq-sell-reentry-ex2",
            "enabled": True,
            "order": 1,
            "label": "Step 1: Trades below RC3L -> Step 2: Crosses above RC3H (Original text says enter BUY with SL RC3L)",
            "allowSameTick": False,
            "timeoutMinutes": 60,
            "steps": [
                {
                    "type": "leaf",
                    "id": "leaf-sell-reentry-ex2-s1",
                    "enabled": True,
                    "order": 1,
                    "label": "Price trades below RC3L",
                    "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                    "operator": "trades_below",
                    "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC3L", "value": None},
                    "timeframe": "5m",
                    "candleRef": "current",
                    "evaluateOn": "CANDLE_CLOSE",
                    "params": {"tolerance": None, "toleranceUnit": None},
                },
                {
                    "type": "leaf",
                    "id": "leaf-sell-reentry-ex2-s2",
                    "enabled": True,
                    "order": 2,
                    "label": "Price crosses above RC3H",
                    "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                    "operator": "crosses_above",
                    "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC3H", "value": None},
                    "timeframe": "5m",
                    "candleRef": "current",
                    "evaluateOn": "CANDLE_CLOSE",
                    "params": {"tolerance": None, "toleranceUnit": None},
                },
            ],
        },
    })
    doc["reentry"]["unresolvedConflict"] = True
    doc["reentry"]["conflictNote"] = (
        "SELL re-entry example 2 specifies 'enter BUY with SL RC3L'. Flagged for confirmation; cannot be enabled until resolved."
    )
    return doc




def sample_phase4_trailing_partial_targets() -> dict:
    """Phase 4 Sample: 3 Partial Targets, Level SL, and 2 Ordered Idempotent Trailing SL Steps."""
    doc = sample_buy_near_rc1_breakout_strategy()
    doc = copy.deepcopy(doc)
    doc["id"] = "sample-phase4-trailing-partial-targets"
    doc["name"] = "Sample Phase 4: Multi-Target Partial Exits & Idempotent Trailing SL"
    doc["meta"]["id"] = doc["id"]
    doc["meta"]["name"] = doc["name"]
    doc["meta"]["description"] = (
        "Demonstrates 4-lot BUY entry with level-based SL at FC1L, 3 partial targets (1 lot + 1 lot + 2 lots = 4 lots), "
        "and 2 ordered idempotent trailing SL steps (Step 1 moves SL to RC1L; Step 2 moves SL to FC1H)."
    )
    doc["risk"]["maxQuantity"] = 10
    doc["execution"]["quantity"] = 4
    doc["execution"]["intrabarConflictOrder"] = "SL_FIRST"
    doc["stopLoss"] = {
        "id": "sl-level-fc1l",
        "enabled": True,
        "order": 1,
        "label": "Initial Stop Loss at FC1L",
        "method": "level",
        "points": None,
        "percent": None,
        "indicatorId": None,
        "candlePrice": None,
        "candleCount": None,
        "bufferPercent": None,
        "level": "FC1L",
    }
    doc["targets"] = [
        {
            "id": "tgt-1-pts",
            "enabled": True,
            "order": 1,
            "label": "Target 1 (+20 points, 1 lot)",
            "value": 20.0,
            "unit": "points",
            "quantity": 1,
            "level": None,
        },
        {
            "id": "tgt-2-pct",
            "enabled": True,
            "order": 2,
            "label": "Target 2 (+0.25%, 1 lot)",
            "value": 0.25,
            "unit": "percent",
            "quantity": 1,
            "level": None,
        },
        {
            "id": "tgt-3-final-lvl",
            "enabled": True,
            "order": 3,
            "label": "Final Target at RC3H (2 lots)",
            "value": 0.0,
            "unit": "price_level",
            "quantity": 2,
            "level": "RC3H",
        },
    ]
    doc["trailing"] = [
        {
            "id": "trail-1-to-rc1l",
            "enabled": True,
            "order": 1,
            "label": "When CLOSE > RC1H, move SL to RC1L",
            "condition": {
                "type": "leaf",
                "id": "cond-trail-1",
                "enabled": True,
                "order": 1,
                "label": "CLOSE > RC1H",
                "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                "operator": ">",
                "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC1H", "value": None},
                "timeframe": "5m",
                "candleRef": "current",
                "evaluateOn": "CANDLE_CLOSE",
                "params": {"tolerance": None, "toleranceUnit": None},
            },
            "moveSlTo": {
                "kind": "level",
                "level": "RC1L",
                "price": None,
                "points": None,
                "indicatorId": None,
            },
        },
        {
            "id": "trail-2-to-fc1h",
            "enabled": True,
            "order": 2,
            "label": "When CLOSE > RC2H, move SL to FC1H",
            "condition": {
                "type": "leaf",
                "id": "cond-trail-2",
                "enabled": True,
                "order": 1,
                "label": "CLOSE > RC2H",
                "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
                "operator": ">",
                "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC2H", "value": None},
                "timeframe": "5m",
                "candleRef": "current",
                "evaluateOn": "CANDLE_CLOSE",
                "params": {"tolerance": None, "toleranceUnit": None},
            },
            "moveSlTo": {
                "kind": "level",
                "level": "FC1H",
                "price": None,
                "points": None,
                "indicatorId": None,
            },
        },
    ]
    return doc


def all_sample_strategies() -> list[dict]:
    return [
        sample_buy_near_rc1_breakout_strategy(),
        sample_buy_reentry_example_1(),
        sample_buy_reentry_example_2(),
        sample_sell_reentry_example_1(),
        sample_phase4_trailing_partial_targets(),
        sample_unresolved_sell_reentry_example_2(),
    ]
