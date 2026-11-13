"""Schema v2 for strategyType RULE_BUILDER.

Stores and validates strategy configuration, including Phase 2 condition evaluation
parameters, indicator output selection, evaluation modes, proximity/breakout parameters,
and signal-source vs execution-instrument separation.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

HAS_PYDANTIC_V2 = hasattr(BaseModel, "model_validate")
if HAS_PYDANTIC_V2:
    from pydantic import ConfigDict
else:
    ConfigDict = None

SCHEMA_VERSION = 2
STRATEGY_TYPE = "RULE_BUILDER"

TIMEFRAMES = {"1m", "3m", "5m", "10m", "15m", "30m", "60m", "1h", "1d"}
PRICE_FIELDS = {"OPEN", "HIGH", "LOW", "CLOSE", "LTP", "VOLUME"}
LEVEL_NAMES = tuple(
    [f"FC{i}{side}" for i in (1, 2, 3) for side in ("H", "L")]
    + [f"RC{i}{side}" for i in range(1, 6) for side in ("H", "L")]
)
CANDLE_REF = re.compile(r"^(current|previous|[1-9][0-9]*-ago)$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

EVALUATION_MODES = {
    "CANDLE_CLOSE", "LIVE_TICK", "INTRABAR",
    "candle_close", "tick", "live_tick", "intrabar",
}

COMPARISON_OPERATORS = {">", "<", ">=", "<=", "=="}
CROSS_OPERATORS = {"crosses_above", "crosses_below"}
TRADE_OPERATORS = {"trades_above", "trades_below"}
UNARY_OPERATORS = {"candle_green", "candle_red", "close_above_open", "close_below_open"}
PROXIMITY_OPERATORS = {"near", "buy_near", "sell_near"}
BREAKOUT_OPERATORS = {
    "moves_out_of", "out_of_fc1h", "out_of_fc1l", "out_of_range",
    "breakout_above", "breakout_below",
}
TOLERANCE_OPERATORS = PROXIMITY_OPERATORS | BREAKOUT_OPERATORS
OPERATORS = (
    COMPARISON_OPERATORS
    | CROSS_OPERATORS
    | TRADE_OPERATORS
    | UNARY_OPERATORS
    | TOLERANCE_OPERATORS
)

CROSSING_MODES = {"close_cross", "high_low_break", "ltp_cross", "close_beyond_tolerance"}

INDICATOR_TYPES = {
    "CCI", "RSI", "EMA", "SMA", "VWAP", "SUPERTREND", "ATR", "MACD",
    "BOLLINGER", "STOCHASTIC", "ADX",
}
INDICATOR_FIELDS = (
    "period", "smoothingPeriod", "multiplier", "fastPeriod", "slowPeriod",
    "signalPeriod", "stdDev", "kPeriod", "dPeriod",
)
INDICATOR_REQUIRED = {
    "CCI": ("period",),
    "RSI": ("period", "smoothingPeriod"),
    "EMA": ("period",),
    "SMA": ("period",),
    "VWAP": (),
    "SUPERTREND": ("period", "multiplier"),
    "ATR": ("period",),
    "MACD": ("fastPeriod", "slowPeriod", "signalPeriod"),
    "BOLLINGER": ("period", "stdDev"),
    "STOCHASTIC": ("kPeriod", "dPeriod"),
    "ADX": ("period",),
}

INDICATOR_OUTPUTS = {
    "CCI": {"value"},
    "RSI": {"value", "rsi", "signal", "average"},
    "EMA": {"value"},
    "SMA": {"value"},
    "VWAP": {"value"},
    "SUPERTREND": {"value", "supertrend", "direction"},
    "ATR": {"value"},
    "MACD": {"value", "macd", "signal", "histogram"},
    "BOLLINGER": {"value", "middle", "upper", "lower", "bandwidth"},
    "STOCHASTIC": {"value", "k", "d"},
    "ADX": {"value", "adx", "plus_di", "minus_di"},
}

INITIAL_INSTRUMENTS = {
    "NIFTY", "NIFTY 50", "BANKNIFTY", "FINNIFTY", "SENSEX",
    "NIFTY IT", "NIFTY AUTO", "NIFTY FMCG",
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL", "TATAMOTORS", "ZOMATO",
}
INSTRUMENT_TYPES = {"INDEX_SPOT", "INDEX_FUTURES", "INDEX_OPTIONS", "EQUITY", "FUTURES", "OPTION"}

SECRET_KEYS = {
    "password", "secret", "token", "authcode", "apikey", "appsecret", "app_key",
    "app_secret", "usersession", "authorization", "credential", "credentials",
    "accesstoken", "refreshtoken",
}

MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


class MigrationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def register_migration(from_version: int):
    def decorator(fn: Callable[[dict], dict]):
        MIGRATIONS[from_version] = fn
        return fn
    return decorator


def normalize_eval_mode(mode: Optional[str]) -> str:
    cleaned = str(mode or "CANDLE_CLOSE").strip().upper()
    if cleaned in ("CANDLE_CLOSE",):
        return "CANDLE_CLOSE"
    if cleaned in ("TICK", "LIVE_TICK"):
        return "LIVE_TICK"
    if cleaned in ("INTRABAR",):
        return "INTRABAR"
    return cleaned


class StrictModel(BaseModel):
    if HAS_PYDANTIC_V2:
        model_config = ConfigDict(extra="forbid")
    else:
        class Config:
            extra = "forbid"

    @classmethod
    def validate_payload(cls, data: Any):
        if hasattr(cls, "model_validate"):
            return cls.model_validate(data)
        return cls.parse_obj(data)

    def dump_json_dict(self) -> dict:
        if hasattr(self, "model_dump"):
            return self.model_dump(mode="json")
        return json.loads(self.json())

    @classmethod
    def schema_dict(cls) -> dict:
        if hasattr(cls, "model_json_schema"):
            return cls.model_json_schema()
        return cls.schema()


    if not HAS_PYDANTIC_V2:
        model_validate = classmethod(lambda cls, data: cls.parse_obj(data))
        model_dump = lambda self, mode="python": json.loads(self.json()) if mode == "json" else self.dict()
        model_json_schema = classmethod(lambda cls: cls.schema())


class Operand(StrictModel):
    kind: Literal["price", "indicator", "level", "constant"]
    field: Optional[Literal["OPEN", "HIGH", "LOW", "CLOSE", "LTP", "VOLUME"]] = None
    indicatorId: Optional[str] = None
    output: Optional[str] = None
    level: Optional[str] = None
    value: Optional[float] = None


class LeafParams(StrictModel):
    tolerance: Optional[float] = None
    toleranceUnit: Optional[Literal["points", "percent"]] = None
    crossingMode: Optional[Literal["close_cross", "high_low_break", "ltp_cross", "close_beyond_tolerance"]] = None
    breakoutDirection: Optional[Literal["ABOVE", "BELOW", "OUTSIDE_RANGE"]] = None
    upperLevel: Optional[str] = None
    lowerLevel: Optional[str] = None
    tradeBasis: Optional[Literal["CANDLE_CLOSE", "LIVE_TICK", "HIGH_LOW", "candle_close", "tick", "high_low"]] = None


class LeafCondition(StrictModel):
    type: Literal["leaf"]
    id: str
    enabled: bool
    order: int
    label: str
    left: Optional[Operand] = None
    operator: str
    right: Optional[Operand] = None
    timeframe: str
    candleRef: str
    evaluateOn: Literal[
        "tick", "candle_close", "intrabar",
        "CANDLE_CLOSE", "LIVE_TICK", "INTRABAR", "live_tick",
    ]
    params: LeafParams


class GroupCondition(StrictModel):
    type: Literal["group"]
    id: str
    enabled: bool
    order: int
    label: str
    logic: Literal["AND", "OR"]
    children: list["ConditionNode"]


StepNode = Annotated[
    Union[LeafCondition, GroupCondition],
    Field(discriminator="type"),
]


class SequenceCondition(StrictModel):
    type: Literal["sequence"]
    id: str
    enabled: bool
    order: int
    label: str
    steps: list[StepNode]
    allowSameTick: bool
    timeoutMinutes: Optional[int] = None


ConditionNode = Annotated[
    Union[LeafCondition, GroupCondition, SequenceCondition],
    Field(discriminator="type"),
]
if hasattr(GroupCondition, "model_rebuild"):
    GroupCondition.model_rebuild()
    SequenceCondition.model_rebuild()
else:
    GroupCondition.update_forward_refs(ConditionNode=ConditionNode, StepNode=StepNode)
    SequenceCondition.update_forward_refs(ConditionNode=ConditionNode, StepNode=StepNode)


class Indicator(StrictModel):
    id: str
    enabled: bool
    order: int
    label: str
    type: Literal[
        "CCI", "RSI", "EMA", "SMA", "VWAP", "SUPERTREND", "ATR", "MACD",
        "BOLLINGER", "STOCHASTIC", "ADX",
    ]
    timeframe: str
    source: Literal["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
    period: Optional[int] = None
    smoothingPeriod: Optional[int] = None
    multiplier: Optional[float] = None
    fastPeriod: Optional[int] = None
    slowPeriod: Optional[int] = None
    signalPeriod: Optional[int] = None
    stdDev: Optional[float] = None
    kPeriod: Optional[int] = None
    dPeriod: Optional[int] = None


class LevelsConfig(StrictModel):
    provider: Literal["MANUAL", "PLACEHOLDER", "FORMULA"]
    placeholderAck: bool


class StopLoss(StrictModel):
    id: str
    enabled: bool
    order: int
    label: str
    method: Literal["fixed_points", "percentage", "indicator", "candle", "level"]
    points: Optional[float] = None
    percent: Optional[float] = None
    indicatorId: Optional[str] = None
    candlePrice: Optional[Literal["CLOSE", "HIGH_LOW", "OPEN"]] = None
    candleCount: Optional[int] = None
    bufferPercent: Optional[float] = None
    level: Optional[str] = None


class Target(StrictModel):
    id: str
    enabled: bool
    order: int
    label: str
    value: float
    unit: Literal["points", "percent", "price_level"]
    quantity: int
    level: Optional[str] = None


class MoveStop(StrictModel):
    kind: Literal["level", "price", "points", "indicator"]
    level: Optional[str] = None
    price: Optional[float] = None
    points: Optional[float] = None
    indicatorId: Optional[str] = None


class TrailingStep(StrictModel):
    id: str
    enabled: bool
    order: int
    label: str
    condition: ConditionNode
    moveSlTo: MoveStop


class Rule(StrictModel):
    id: str
    enabled: bool
    order: int
    label: str
    side: Literal["BUY", "SELL"]
    condition: ConditionNode
    unresolvedConflict: Optional[bool] = None
    conflictNote: Optional[str] = None


class RuleSets(StrictModel):
    entry: list[Rule]
    exit: list[Rule]


class Reentry(StrictModel):
    enabled: bool
    maxPerDay: int
    afterStopLoss: bool
    afterTarget: bool
    afterManualExit: bool
    cooldownMinutes: int
    directions: list[Literal["BUY", "SELL"]]
    stopLoss: Optional[StopLoss] = None
    targets: list[Target]
    unresolvedConflict: Optional[bool] = None
    conflictNote: Optional[str] = None


class SessionConfig(StrictModel):
    timezone: Literal["Asia/Kolkata"]
    startTime: str
    endTime: str
    tradingDays: list[Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]]
    holidayCalendar: Literal["NONE", "EXPLICIT"]
    holidays: list[str]


class RiskConfig(StrictModel):
    maxDailyLoss: float
    maxCapital: float
    maxTradesPerDay: int
    maxOpenPositions: int
    maxQuantity: int


class InstrumentSelection(StrictModel):
    underlying: str = "NIFTY 50"
    signalSource: Literal["INDEX_SPOT", "TRADED_CONTRACT"] = "INDEX_SPOT"
    instrumentType: Literal["INDEX_SPOT", "INDEX_FUTURES", "INDEX_OPTIONS", "EQUITY"] = "INDEX_OPTIONS"
    optionType: Optional[Literal["CE", "PE"]] = "CE"
    strike: Optional[Union[str, float, int]] = "ATM"
    expiry: Optional[str] = "CURRENT_WEEK"
    contractSymbol: Optional[str] = None
    lotSize: Optional[int] = None


class ExecutionConfig(StrictModel):
    mode: Literal["PAPER"]
    liveRequested: Literal[False]
    quantity: int
    product: Literal["PAPER"]
    defaultEvaluationMode: Optional[Literal["CANDLE_CLOSE", "LIVE_TICK", "INTRABAR"]] = None
    intrabarConflictOrder: Optional[Literal["SL_FIRST", "TARGET_FIRST", "LOWER_TIMEFRAME"]] = None
    instrument: Optional[InstrumentSelection] = None


class Meta(StrictModel):
    id: str
    name: str
    description: str
    symbol: str
    status: Literal["DRAFT", "SAVED", "BACKTESTING", "PAPER", "ACTIVE", "PAUSED", "STOPPED", "ERROR"]


class RuleBuilderStrategy(StrictModel):
    schemaVersion: Literal[2]
    strategyType: Literal["RULE_BUILDER"]
    id: str
    name: str
    meta: Meta
    session: SessionConfig
    indicators: list[Indicator]
    levels: LevelsConfig
    rules: RuleSets
    stopLoss: StopLoss
    targets: list[Target]
    trailing: list[TrailingStep]
    reentry: Reentry
    risk: RiskConfig
    execution: ExecutionConfig


def _blank_operand() -> dict:
    return {"kind": "constant", "field": None, "indicatorId": None, "level": None, "value": 0}


def _leaf(node_id: str, operator: str = ">", left=None, right=None) -> dict:
    return {
        "type": "leaf",
        "id": node_id,
        "enabled": True,
        "order": 1,
        "label": "Close above zero",
        "left": left or {
            "kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None,
        },
        "operator": operator,
        "right": right or _blank_operand(),
        "timeframe": "5m",
        "candleRef": "current",
        "evaluateOn": "candle_close",
        "params": {"tolerance": None, "toleranceUnit": None},
    }


def _stop(node_id: str = "sl-1") -> dict:
    return {
        "id": node_id,
        "enabled": True,
        "order": 1,
        "label": "Fixed stop",
        "method": "fixed_points",
        "points": 10,
        "percent": None,
        "indicatorId": None,
        "candlePrice": None,
        "candleCount": None,
        "bufferPercent": None,
        "level": None,
    }


def _target(node_id: str = "tgt-1", quantity: int = 1, order: int = 1, enabled: bool = True, label: str = "Target") -> dict:
    return {
        "id": node_id,
        "enabled": enabled,
        "order": order,
        "label": label,
        "value": 15,
        "unit": "points",
        "quantity": quantity,
        "level": None,
    }


def new_rule_builder() -> dict:
    strategy_id = str(uuid4())
    return {
        "schemaVersion": SCHEMA_VERSION,
        "strategyType": STRATEGY_TYPE,
        "id": strategy_id,
        "name": "New Rule Strategy",
        "meta": {
            "id": strategy_id,
            "name": "New Rule Strategy",
            "description": "",
            "symbol": "NIFTY 50",
            "status": "DRAFT",
        },
        "session": {
            "timezone": "Asia/Kolkata",
            "startTime": "09:15",
            "endTime": "15:30",
            "tradingDays": ["MON", "TUE", "WED", "THU", "FRI"],
            "holidayCalendar": "NONE",
            "holidays": [],
        },
        "indicators": [],
        "levels": {"provider": "MANUAL", "placeholderAck": False},
        "rules": {
            "entry": [{
                "id": "entry-1",
                "enabled": True,
                "order": 1,
                "label": "Example entry",
                "side": "BUY",
                "condition": _leaf("cond-1"),
            }],
            "exit": [],
        },
        "stopLoss": _stop(),
        "targets": [
            _target("tgt-1", quantity=0, order=1, enabled=False, label="Optional target 1"),
            _target("tgt-2", quantity=0, order=2, enabled=False, label="Optional target 2"),
            _target("tgt-3", quantity=1, order=3, enabled=True, label="Final target"),
        ],
        "trailing": [],
        "reentry": {
            "enabled": False,
            "maxPerDay": 0,
            "afterStopLoss": False,
            "afterTarget": False,
            "afterManualExit": False,
            "cooldownMinutes": 0,
            "directions": [],
            "stopLoss": None,
            "targets": [],
        },
        "risk": {
            "maxDailyLoss": 2000,
            "maxCapital": 25000,
            "maxTradesPerDay": 3,
            "maxOpenPositions": 2,
            "maxQuantity": 1,
        },
        "execution": {
            "mode": "PAPER",
            "liveRequested": False,
            "quantity": 1,
            "product": "PAPER",
        },
    }


def migrate_strategy(document: dict) -> dict:
    """Return a copy at the current RULE_BUILDER schema.

    INDICATOR and LEVEL_SAR documents are returned unchanged.
    No migration from those types into RULE_BUILDER is registered.
    """
    if not isinstance(document, dict):
        raise MigrationError("Strategy must be an object.")
    if document.get("strategyType") != STRATEGY_TYPE:
        return copy.deepcopy(document)
    try:
        version = int(document.get("schemaVersion"))
    except (TypeError, ValueError):
        raise MigrationError("RULE_BUILDER strategies need a numeric schemaVersion.")
    current = copy.deepcopy(document)
    if version == SCHEMA_VERSION:
        return current
    if version > SCHEMA_VERSION:
        raise MigrationError(
            f"schemaVersion {version} is newer than this server supports ({SCHEMA_VERSION})."
        )
    steps = 0
    while version < SCHEMA_VERSION:
        fn = MIGRATIONS.get(version)
        if fn is None:
            raise MigrationError(
                f"No migration from schemaVersion {version} to {SCHEMA_VERSION}. "
                "INDICATOR and LEVEL_SAR documents are not converted into RULE_BUILDER."
            )
        current = fn(current)
        if not isinstance(current, dict):
            raise MigrationError("Migration must return an object.")
        try:
            new_version = int(current.get("schemaVersion"))
        except (TypeError, ValueError):
            raise MigrationError("Migration removed schemaVersion.")
        if new_version <= version:
            raise MigrationError("Migration did not advance schemaVersion.")
        version = new_version
        steps += 1
        if steps > 20:
            raise MigrationError("Migration exceeded the maximum number of steps.")
    return current


def _loc_path(loc) -> str:
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            if not parts:
                parts.append(f"[{item}]")
            else:
                parts[-1] = f"{parts[-1]}[{item}]"
        else:
            parts.append(str(item))
    return ".".join(parts)


def _format_validation_error(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors():
        path = _loc_path(err.get("loc", ()))
        msg = err.get("msg", "invalid value")
        messages.append(f"{path}: {msg}" if path else msg)
    return messages


def _scan_secrets(value, path: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key).replace("_", "").lower() in SECRET_KEYS or str(key).lower() in SECRET_KEYS:
                errors.append(f"{child_path}: credentials are not allowed in a strategy.")
            _scan_secrets(child, child_path, errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_secrets(child, f"{path}[{index}]", errors)


def _hhmm(value: str):
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def _check_id(value: str, path: str, errors: list[str], seen: dict[str, str]) -> None:
    if not ID_PATTERN.match(value or ""):
        errors.append(f"{path}: id must be 1-64 characters (letters, numbers, _ . : -).")
        return
    if value in seen:
        errors.append(f"{path}: duplicate id '{value}' (also at {seen[value]}).")
    else:
        seen[value] = path


def _check_label(value: str, path: str, errors: list[str]) -> None:
    if not str(value or "").strip():
        errors.append(f"{path}.label: label is required.")


def _check_order(value: int, path: str, errors: list[str]) -> None:
    if value < 1:
        errors.append(f"{path}.order: order must be 1 or greater.")


def _unique_orders(items, path: str, errors: list[str]) -> None:
    seen = {}
    for index, item in enumerate(items):
        order = item.order
        if order in seen:
            errors.append(f"{path}[{index}].order: duplicate order {order}.")
        else:
            seen[order] = index


def _check_operand(
    operand: Operand,
    path: str,
    errors: list[str],
    indicator_ids: set[str],
    indicator_map: Optional[dict[str, Indicator]] = None,
) -> None:
    payload = {
        "price": operand.field,
        "indicator": operand.indicatorId,
        "level": operand.level,
        "constant": operand.value,
    }
    for kind, present in (
        ("field", operand.field),
        ("indicatorId", operand.indicatorId),
        ("level", operand.level),
        ("value", operand.value),
    ):
        expected = {
            "price": "field",
            "indicator": "indicatorId",
            "level": "level",
            "constant": "value",
        }[operand.kind]
        if kind != expected and present is not None:
            errors.append(f"{path}.{kind}: must be null when kind is {operand.kind}.")
    if operand.kind != "indicator" and operand.output is not None:
        errors.append(f"{path}.output: must be null unless kind is indicator.")
    if payload[operand.kind] is None:
        errors.append(f"{path}: {operand.kind} operand is missing its value.")
    if operand.kind == "indicator":
        if operand.indicatorId and operand.indicatorId not in indicator_ids:
            errors.append(f"{path}.indicatorId: unknown indicator '{operand.indicatorId}'.")
        elif operand.indicatorId and indicator_map and operand.output is not None:
            ind = indicator_map.get(operand.indicatorId)
            if ind and operand.output.lower() not in INDICATOR_OUTPUTS.get(ind.type, {"value"}):
                errors.append(
                    f"{path}.output: invalid output '{operand.output}' for {ind.type}."
                )
    if operand.kind == "level" and operand.level and operand.level not in LEVEL_NAMES:
        errors.append(f"{path}.level: unknown level '{operand.level}'.")
    if operand.kind == "price" and operand.field and operand.field not in PRICE_FIELDS:
        errors.append(f"{path}.field: unknown price field '{operand.field}'.")


def _check_condition(
    node,
    path: str,
    errors: list[str],
    seen: dict[str, str],
    indicator_ids: set[str],
    inside_sequence: bool = False,
    indicator_map: Optional[dict[str, Indicator]] = None,
) -> None:
    _check_id(node.id, f"{path}.id", errors, seen)
    _check_label(node.label, path, errors)
    _check_order(node.order, path, errors)
    if isinstance(node, SequenceCondition):
        if inside_sequence:
            errors.append(f"{path}: a sequence step cannot contain another sequence.")
            return
        if len(node.steps) < 2:
            errors.append(f"{path}.steps: a sequence needs at least two steps.")
        if node.timeoutMinutes is not None and node.timeoutMinutes < 1:
            errors.append(f"{path}.timeoutMinutes: use null for no timeout, or a value of 1 minute or more.")
        for index, step in enumerate(node.steps):
            _check_condition(
                step,
                f"{path}.steps[{index}]",
                errors,
                seen,
                indicator_ids,
                inside_sequence=True,
                indicator_map=indicator_map,
            )
        return
    if isinstance(node, GroupCondition):
        if not node.children:
            errors.append(f"{path}.children: a group needs at least one condition.")
        for index, child in enumerate(node.children):
            _check_condition(
                child,
                f"{path}.children[{index}]",
                errors,
                seen,
                indicator_ids,
                inside_sequence=inside_sequence,
                indicator_map=indicator_map,
            )
        return
    if node.timeframe not in TIMEFRAMES:
        errors.append(f"{path}.timeframe: unsupported timeframe '{node.timeframe}'.")
    if not CANDLE_REF.match(node.candleRef or ""):
        errors.append(f"{path}.candleRef: use current, previous, or N-ago (example 2-ago).")
    if node.operator not in OPERATORS:
        errors.append(f"{path}.operator: unsupported operator '{node.operator}'.")
        return
    eval_norm = normalize_eval_mode(node.evaluateOn)
    if node.operator in UNARY_OPERATORS:
        if node.left is not None or node.right is not None:
            errors.append(f"{path}: {node.operator} uses the candle selected by timeframe and candleRef. Set left and right to null.")
        if eval_norm != "CANDLE_CLOSE":
            errors.append(f"{path}.evaluateOn: {node.operator} requires candle_close.")
        if node.params.tolerance is not None or node.params.toleranceUnit is not None:
            errors.append(f"{path}.params: {node.operator} does not take a tolerance.")
        return
    if node.left is None or node.right is None:
        errors.append(f"{path}: operator '{node.operator}' needs both a left and a right operand.")
    else:
        _check_operand(node.left, f"{path}.left", errors, indicator_ids, indicator_map=indicator_map)
        _check_operand(node.right, f"{path}.right", errors, indicator_ids, indicator_map=indicator_map)
    if node.operator in TOLERANCE_OPERATORS:
        if node.params.tolerance is None or node.params.tolerance <= 0:
            errors.append(f"{path}.params.tolerance: set a tolerance greater than 0. There is no default.")
        if node.params.toleranceUnit is None:
            errors.append(f"{path}.params.toleranceUnit: set points or percent. There is no default.")
        if node.operator in BREAKOUT_OPERATORS and node.params.crossingMode is not None:
            if node.params.crossingMode not in CROSSING_MODES:
                errors.append(f"{path}.params.crossingMode: unsupported crossingMode '{node.params.crossingMode}'.")
        if node.operator == "out_of_range":
            if not node.params.upperLevel or node.params.upperLevel not in LEVEL_NAMES:
                errors.append(f"{path}.params.upperLevel: required valid FC/RC level for out_of_range.")
            if not node.params.lowerLevel or node.params.lowerLevel not in LEVEL_NAMES:
                errors.append(f"{path}.params.lowerLevel: required valid FC/RC level for out_of_range.")
    elif node.params.tolerance is not None or node.params.toleranceUnit is not None:
        errors.append(f"{path}.params: tolerance is only used by near and moves_out_of (and proximity/breakout operators).")


def _check_stop(stop: StopLoss, path: str, errors: list[str], seen: dict[str, str], indicator_ids: set[str], require_enabled: bool) -> None:
    _check_id(stop.id, f"{path}.id", errors, seen)
    _check_label(stop.label, path, errors)
    _check_order(stop.order, path, errors)
    if require_enabled and not stop.enabled:
        errors.append(f"{path}.enabled: stop loss must be enabled.")
    required = {
        "fixed_points": ("points",),
        "percentage": ("percent",),
        "indicator": ("indicatorId",),
        "candle": ("candlePrice", "candleCount", "bufferPercent"),
        "level": ("level",),
    }[stop.method]
    fields = ("points", "percent", "indicatorId", "candlePrice", "candleCount", "bufferPercent", "level")
    for name in fields:
        present = getattr(stop, name) is not None
        if name in required and not present:
            errors.append(f"{path}.{name}: required for stop method {stop.method}.")
        if name not in required and present:
            errors.append(f"{path}.{name}: must be null for stop method {stop.method}.")
    if stop.method == "fixed_points" and stop.points is not None and stop.points <= 0:
        errors.append(f"{path}.points: must be greater than 0.")
    if stop.method == "percentage" and stop.percent is not None and stop.percent <= 0:
        errors.append(f"{path}.percent: must be greater than 0.")
    if stop.method == "candle" and stop.candleCount is not None and stop.candleCount < 1:
        errors.append(f"{path}.candleCount: must be 1 or greater.")
    if stop.method == "candle" and stop.bufferPercent is not None and stop.bufferPercent < 0:
        errors.append(f"{path}.bufferPercent: must be 0 or greater. Set 0 only when you want no buffer.")
    if stop.method == "indicator" and stop.indicatorId and stop.indicatorId not in indicator_ids:
        errors.append(f"{path}.indicatorId: unknown indicator '{stop.indicatorId}'.")
    if stop.method == "level" and stop.level and stop.level not in LEVEL_NAMES:
        errors.append(f"{path}.level: unknown level '{stop.level}'.")


def _check_target(target: Target, path: str, errors: list[str], seen: dict[str, str]) -> None:
    _check_id(target.id, f"{path}.id", errors, seen)
    _check_label(target.label, path, errors)
    _check_order(target.order, path, errors)
    if target.quantity < 0 or (target.enabled and target.quantity < 1):
        errors.append(f"{path}.quantity: an enabled target needs a quantity of at least 1.")
    if target.unit == "price_level":
        if target.level not in LEVEL_NAMES:
            errors.append(f"{path}.level: a price_level target needs an FC/RC level.")
    elif target.level is not None:
        errors.append(f"{path}.level: must be null unless unit is price_level.")
    if target.unit in ("points", "percent") and target.value <= 0:
        errors.append(f"{path}.value: must be greater than 0.")


def _check_move(move: MoveStop, path: str, errors: list[str], indicator_ids: set[str]) -> None:
    mapping = {"level": "level", "price": "price", "points": "points", "indicator": "indicatorId"}
    for kind, field_name in mapping.items():
        present = getattr(move, field_name) is not None
        if kind == move.kind and not present:
            errors.append(f"{path}.{field_name}: required when kind is {move.kind}.")
        if kind != move.kind and present:
            errors.append(f"{path}.{field_name}: must be null when kind is {move.kind}.")
    if move.kind == "level" and move.level and move.level not in LEVEL_NAMES:
        errors.append(f"{path}.level: unknown level '{move.level}'.")
    if move.kind == "points" and move.points is not None and move.points <= 0:
        errors.append(f"{path}.points: must be greater than 0.")
    if move.kind == "indicator" and move.indicatorId and move.indicatorId not in indicator_ids:
        errors.append(f"{path}.indicatorId: unknown indicator '{move.indicatorId}'.")


def _check_indicator(indicator: Indicator, path: str, errors: list[str], seen: dict[str, str]) -> None:
    _check_id(indicator.id, f"{path}.id", errors, seen)
    _check_label(indicator.label, path, errors)
    _check_order(indicator.order, path, errors)
    if indicator.timeframe not in TIMEFRAMES:
        errors.append(f"{path}.timeframe: unsupported timeframe '{indicator.timeframe}'.")
    required = INDICATOR_REQUIRED[indicator.type]
    for name in INDICATOR_FIELDS:
        present = getattr(indicator, name) is not None
        if name in required and not present:
            errors.append(f"{path}.{name}: required for {indicator.type}.")
        if name not in required and present:
            errors.append(f"{path}.{name}: not used by {indicator.type}. Remove it.")
    for name in ("period", "smoothingPeriod", "fastPeriod", "slowPeriod", "signalPeriod", "kPeriod", "dPeriod"):
        value = getattr(indicator, name)
        if value is not None and value < 1:
            errors.append(f"{path}.{name}: must be 1 or greater.")
    if indicator.multiplier is not None and indicator.multiplier <= 0:
        errors.append(f"{path}.multiplier: must be greater than 0.")
    if indicator.stdDev is not None and indicator.stdDev <= 0:
        errors.append(f"{path}.stdDev: must be greater than 0.")
    if indicator.type == "MACD" and None not in (indicator.fastPeriod, indicator.slowPeriod):
        if indicator.fastPeriod >= indicator.slowPeriod:
            errors.append(f"{path}: MACD fastPeriod must be lower than slowPeriod.")


def _semantic_errors(model: RuleBuilderStrategy) -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    if model.id != model.meta.id:
        errors.append("meta.id: must match the top-level id.")
    if model.name.strip() != model.meta.name.strip() or not model.name.strip():
        errors.append("meta.name: must match the top-level name.")
    _check_id(model.id, "id", errors, seen)

    start = _hhmm(model.session.startTime)
    end = _hhmm(model.session.endTime)
    if start is None:
        errors.append("session.startTime: use HH:MM.")
    if end is None:
        errors.append("session.endTime: use HH:MM.")
    if start is not None and end is not None and start >= end:
        errors.append("session.endTime: must be after startTime.")
    if not model.session.tradingDays:
        errors.append("session.tradingDays: choose at least one day.")
    if len(set(model.session.tradingDays)) != len(model.session.tradingDays):
        errors.append("session.tradingDays: duplicate day.")
    if model.session.holidayCalendar == "NONE" and model.session.holidays:
        errors.append("session.holidays: must be empty when holidayCalendar is NONE.")
    if model.session.holidayCalendar == "EXPLICIT":
        for index, day in enumerate(model.session.holidays):
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", day or ""):
                errors.append(f"session.holidays[{index}]: use YYYY-MM-DD.")

    _unique_orders(model.indicators, "indicators", errors)
    indicator_ids: set[str] = set()
    indicator_map: dict[str, Indicator] = {}
    for index, indicator in enumerate(model.indicators):
        _check_indicator(indicator, f"indicators[{index}]", errors, seen)
        indicator_ids.add(indicator.id)
        indicator_map[indicator.id] = indicator

    if model.levels.provider == "FORMULA":
        errors.append("levels.provider: formula not provided.")
    if model.levels.provider == "PLACEHOLDER":
        if not model.levels.placeholderAck:
            errors.append("levels.placeholderAck: set true to acknowledge that PLACEHOLDER levels are not real market data.")
        from .config import settings
        if not settings.levels_placeholder_enabled:
            errors.append("levels.provider: PLACEHOLDER is off. Set LEVELS_PLACEHOLDER_ENABLED=true before using it.")

    if not any(rule.enabled for rule in model.rules.entry):
        errors.append("rules.entry: add at least one enabled entry rule.")
    _unique_orders(model.rules.entry, "rules.entry", errors)
    _unique_orders(model.rules.exit, "rules.exit", errors)
    for bucket in ("entry", "exit"):
        for index, rule in enumerate(getattr(model.rules, bucket)):
            path = f"rules.{bucket}[{index}]"
            _check_id(rule.id, f"{path}.id", errors, seen)
            _check_label(rule.label, path, errors)
            _check_order(rule.order, path, errors)
            if rule.unresolvedConflict and rule.enabled:
                note = rule.conflictNote or "unresolved specification contradiction (e.g. SELL re-entry example 2 specifies BUY)."
                errors.append(f"{path}: cannot enable rule with unresolved conflict: {note}")
            _check_condition(
                rule.condition,
                f"{path}.condition",
                errors,
                seen,
                indicator_ids,
                indicator_map=indicator_map,
            )

    _check_stop(model.stopLoss, "stopLoss", errors, seen, indicator_ids, require_enabled=True)
    if len(model.targets) < 3:
        errors.append("targets: add at least three targets. Disable optional ones. The final target stays enabled.")
    _unique_orders(model.targets, "targets", errors)
    for index, target in enumerate(model.targets):
        _check_target(target, f"targets[{index}]", errors, seen)
    if model.targets:
        final = max(model.targets, key=lambda item: item.order)
        if not final.enabled:
            errors.append("targets: the final target (highest order) must stay enabled.")
        enabled_qty = sum(item.quantity for item in model.targets if item.enabled)
        if enabled_qty != model.execution.quantity:
            errors.append(
                f"targets: enabled quantities sum to {enabled_qty}, but execution.quantity is {model.execution.quantity}."
            )

    _unique_orders(model.trailing, "trailing", errors)
    for index, step in enumerate(model.trailing):
        path = f"trailing[{index}]"
        _check_id(step.id, f"{path}.id", errors, seen)
        _check_label(step.label, path, errors)
        _check_order(step.order, path, errors)
        _check_condition(
            step.condition,
            f"{path}.condition",
            errors,
            seen,
            indicator_ids,
            indicator_map=indicator_map,
        )
        _check_move(step.moveSlTo, f"{path}.moveSlTo", errors, indicator_ids)

    reentry = model.reentry
    if reentry.unresolvedConflict and reentry.enabled:
        note = reentry.conflictNote or "SELL re-entry example 2 specifies BUY with SL RC3L; flagged for confirmation."
        errors.append(f"reentry: cannot enable re-entry with unresolved conflict: {note}")
    if reentry.enabled:
        if reentry.maxPerDay < 1:
            errors.append("reentry.maxPerDay: must be at least 1 when re-entry is enabled.")
        if not reentry.directions:
            errors.append("reentry.directions: choose BUY, SELL, or both when re-entry is enabled.")
        if reentry.cooldownMinutes < 0:
            errors.append("reentry.cooldownMinutes: must be 0 or greater.")
        if reentry.stopLoss is None:
            errors.append("reentry.stopLoss: set a stop loss when re-entry is enabled.")
        if not reentry.targets:
            errors.append("reentry.targets: set at least one target when re-entry is enabled.")
    else:
        if reentry.maxPerDay != 0:
            errors.append("reentry.maxPerDay: must be 0 when re-entry is disabled.")
        if reentry.cooldownMinutes < 0:
            errors.append("reentry.cooldownMinutes: must be 0 or greater.")
    if len(set(reentry.directions)) != len(reentry.directions):
        errors.append("reentry.directions: duplicate direction.")
    if reentry.stopLoss is not None:
        _check_stop(reentry.stopLoss, "reentry.stopLoss", errors, seen, indicator_ids, require_enabled=reentry.enabled)
    _unique_orders(reentry.targets, "reentry.targets", errors)
    for index, target in enumerate(reentry.targets):
        _check_target(target, f"reentry.targets[{index}]", errors, seen)

    if model.risk.maxDailyLoss <= 0:
        errors.append("risk.maxDailyLoss: set a positive maximum daily loss.")
    if model.risk.maxCapital <= 0:
        errors.append("risk.maxCapital: set a positive capital cap.")
    if model.risk.maxTradesPerDay < 1:
        errors.append("risk.maxTradesPerDay: must be at least 1.")
    if model.risk.maxOpenPositions < 1:
        errors.append("risk.maxOpenPositions: must be at least 1.")
    if model.risk.maxQuantity < 1:
        errors.append("risk.maxQuantity: must be at least 1.")
    if model.execution.quantity < 1:
        errors.append("execution.quantity: must be at least 1.")
    if model.execution.quantity > model.risk.maxQuantity:
        errors.append("execution.quantity: exceeds risk.maxQuantity.")
    if model.execution.instrument is not None:
        inst = model.execution.instrument
        if inst.instrumentType == "INDEX_OPTIONS":
            if inst.optionType not in ("CE", "PE"):
                errors.append("execution.instrument.optionType: must be CE or PE for INDEX_OPTIONS.")
            if inst.strike is None or str(inst.strike).strip() == "":
                errors.append("execution.instrument.strike: required for INDEX_OPTIONS.")
        if inst.lotSize is not None and inst.lotSize < 1:
            errors.append("execution.instrument.lotSize: must be at least 1.")
    return errors


def validate_rule_builder(document: dict) -> list[str]:
    if not isinstance(document, dict):
        return ["Strategy must be an object."]
    errors: list[str] = []
    _scan_secrets(document, "", errors)
    if errors:
        return errors
    try:
        migrated = migrate_strategy(document)
    except MigrationError as exc:
        return [exc.message]
    if migrated.get("strategyType") != STRATEGY_TYPE:
        return ["strategyType: expected RULE_BUILDER."]
    try:
        model = RuleBuilderStrategy.validate_payload(migrated)
    except ValidationError as exc:
        return _format_validation_error(exc)
    return _semantic_errors(model)


def rule_builder_warnings(document: dict) -> list[str]:
    if not isinstance(document, dict):
        return []
    levels = document.get("levels") if isinstance(document.get("levels"), dict) else {}
    provider = levels.get("provider")
    warnings = []
    if provider == "PLACEHOLDER":
        warnings.append(
            "PLACEHOLDER level provider is a dummy. It does not calculate FC/RC levels and must not be used for trading decisions."
        )
    if provider == "FORMULA":
        warnings.append("FORMULA level provider has no formulas yet.")
    reentry = document.get("reentry") if isinstance(document.get("reentry"), dict) else {}
    if reentry.get("unresolvedConflict"):
        warnings.append(
            reentry.get("conflictNote")
            or "SELL re-entry example 2 says 'enter BUY with SL RC3L' under SELL re-entry. Preserved and flagged for confirmation; blocked from activation."
        )
    rules = document.get("rules") if isinstance(document.get("rules"), dict) else {}
    for bucket in ("entry", "exit"):
        for r in rules.get(bucket) or []:
            if isinstance(r, dict) and r.get("unresolvedConflict"):
                warnings.append(
                    r.get("conflictNote")
                    or f"Rule '{r.get('label', r.get('id'))}' has an unresolved specification conflict and cannot be enabled until confirmed."
                )
    return warnings


def compile_rule_builder(document: dict) -> dict:
    errors = validate_rule_builder(document)
    if errors:
        raise ValueError(errors)
    migrated = migrate_strategy(document)
    model = RuleBuilderStrategy.validate_payload(migrated)
    compiled = model.dump_json_dict()
    compiled["compiledAt"] = datetime.now(timezone.utc).isoformat()
    compiled["liveOrdersEnabled"] = False
    compiled["executable"] = False
    return compiled


def json_schema() -> dict:
    return RuleBuilderStrategy.schema_dict()


if not HAS_PYDANTIC_V2:
    setattr(BaseModel, "model_validate", classmethod(lambda cls, data: cls.parse_obj(data)))
    setattr(BaseModel, "model_dump", lambda self, mode="python": json.loads(self.json()) if mode == "json" else self.dict())
    setattr(BaseModel, "model_json_schema", classmethod(lambda cls: cls.schema()))


def strip_secret_fields(value: Any) -> tuple[Any, list[str]]:
    """Recursively strip any credential-like fields from a strategy dict before export/import."""
    stripped: list[str] = []

    def _walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            cleaned = {}
            for k, v in node.items():
                child_path = f"{path}.{k}" if path else str(k)
                k_norm = str(k).replace("_", "").lower()
                if k_norm in SECRET_KEYS or str(k).lower() in SECRET_KEYS:
                    stripped.append(child_path)
                    continue
                cleaned[k] = _walk(v, child_path)
            return cleaned
        if isinstance(node, list):
            return [_walk(item, f"{path}[{idx}]") for idx, item in enumerate(node)]
        return copy.deepcopy(node)

    return _walk(value, ""), stripped


def _operand_to_english(op: Optional[dict], indicator_labels: dict[str, str]) -> str:
    if not isinstance(op, dict):
        return "?"
    kind = op.get("kind")
    if kind == "price":
        return str(op.get("field") or "CLOSE")
    if kind == "level":
        return str(op.get("level") or "LEVEL")
    if kind == "constant":
        return str(op.get("value") if op.get("value") is not None else 0)
    if kind == "indicator":
        ind_id = str(op.get("indicatorId") or "INDICATOR")
        label = indicator_labels.get(ind_id, ind_id)
        out = op.get("output")
        return f"{label}.{out}" if out and out != "value" else label
    return str(kind or "?")


def condition_node_to_english(node: Optional[dict], indicator_labels: Optional[dict[str, str]] = None) -> str:
    """Generate a beginner-friendly plain-English description of any ConditionNode tree."""
    if not isinstance(node, dict):
        return "(empty condition)"
    ind_map = indicator_labels or {}
    if not node.get("enabled", True):
        return "[DISABLED]"

    ntype = node.get("type")
    if ntype == "leaf":
        op = str(node.get("operator") or ">")
        tf = node.get("timeframe") or "5m"
        ref = node.get("candleRef") or "current"
        ref_prefix = f"{tf} " if ref == "current" else f"{tf} ({ref}) "
        if op == "candle_green":
            return f"{ref_prefix}candle is green"
        if op == "candle_red":
            return f"{ref_prefix}candle is red"
        if op == "close_above_open":
            return f"{ref_prefix}close is above open"
        if op == "close_below_open":
            return f"{ref_prefix}close is below open"

        left_s = _operand_to_english(node.get("left"), ind_map)
        right_s = _operand_to_english(node.get("right"), ind_map)
        params = node.get("params") or {}
        tol = params.get("tolerance")
        unit = params.get("toleranceUnit") or "points"
        tol_str = f" (within {tol} {unit})" if tol is not None else " (missing tolerance!)"

        op_phrases = {
            "crosses_above": "crosses above",
            "crosses_below": "crosses below",
            "trades_above": "trades above",
            "trades_below": "trades below",
            "near": f"is near {right_s}{tol_str}",
            "buy_near": f"is near (BUY zone) {right_s}{tol_str}",
            "sell_near": f"is near (SELL zone) {right_s}{tol_str}",
            "moves_out_of": f"moves out of {right_s} by {tol if tol is not None else '?'} {unit}",
            "out_of_fc1h": f"breaks above FC1H by {tol if tol is not None else '?'} {unit}",
            "out_of_fc1l": f"breaks below FC1L by {tol if tol is not None else '?'} {unit}",
            "out_of_range": f"moves outside range [{params.get('lowerLevel') or 'FC1L'}..{params.get('upperLevel') or 'FC1H'}] by {tol if tol is not None else '?'} {unit}",
            "breakout_above": f"breaks above {right_s} by {tol if tol is not None else '?'} {unit}",
            "breakout_below": f"breaks below {right_s} by {tol if tol is not None else '?'} {unit}",
        }
        if op in ("near", "buy_near", "sell_near", "moves_out_of", "out_of_fc1h", "out_of_fc1l", "out_of_range", "breakout_above", "breakout_below"):
            return f"{left_s} {op_phrases[op]}"
        verb = op_phrases.get(op, op)
        return f"{left_s} {verb} {right_s}"

    if ntype == "group":
        logic = str(node.get("logic") or "AND").upper()
        children = [c for c in (node.get("children") or []) if isinstance(c, dict) and c.get("enabled", True)]
        if not children:
            return "(no enabled conditions in group)"
        parts = [condition_node_to_english(c, ind_map) for c in sorted(children, key=lambda x: x.get("order", 1))]
        if len(parts) == 1:
            return parts[0]
        return "(" + f" {logic} ".join(parts) + ")"

    if ntype == "sequence":
        steps = [s for s in (node.get("steps") or []) if isinstance(s, dict) and s.get("enabled", True)]
        if not steps:
            return "(empty sequence)"
        ordered = sorted(steps, key=lambda x: x.get("order", 1))
        step_strs = [f"Step {idx+1}: {condition_node_to_english(st, ind_map)}" for idx, st in enumerate(ordered)]
        timeout = node.get("timeoutMinutes")
        t_str = f" [within {timeout}m]" if timeout else ""
        return " -> THEN -> ".join(step_strs) + t_str

    return "(condition)"


def strategy_to_english_preview(strategy: dict) -> dict[str, Any]:
    """Return plain-English summaries for every entry rule, exit rule, and trailing SL step."""
    if not isinstance(strategy, dict):
        return {"entry": [], "exit": [], "trailing": []}
    ind_labels = {
        str(ind.get("id")): str(ind.get("label") or ind.get("id"))
        for ind in (strategy.get("indicators") or [])
        if isinstance(ind, dict) and ind.get("id")
    }
    rules = strategy.get("rules") or {}
    entry_previews = []
    for r in sorted(rules.get("entry") or [], key=lambda x: x.get("order", 1)):
        side = r.get("side", "BUY")
        cond_txt = condition_node_to_english(r.get("condition"), ind_labels)
        conflict = " [UNRESOLVED CONFLICT — BLOCKED]" if r.get("unresolvedConflict") else ""
        entry_previews.append({
            "ruleId": r.get("id"),
            "label": r.get("label"),
            "side": side,
            "enabled": bool(r.get("enabled", True)),
            "unresolvedConflict": bool(r.get("unresolvedConflict", False)),
            "text": f"{side} when {cond_txt}{conflict}",
        })

    exit_previews = []
    for r in sorted(rules.get("exit") or [], key=lambda x: x.get("order", 1)):
        side = r.get("side", "SELL")
        cond_txt = condition_node_to_english(r.get("condition"), ind_labels)
        exit_previews.append({
            "ruleId": r.get("id"),
            "label": r.get("label"),
            "side": side,
            "enabled": bool(r.get("enabled", True)),
            "text": f"EXIT ({side}) when {cond_txt}",
        })

    trailing_previews = []
    for st in sorted(strategy.get("trailing") or [], key=lambda x: x.get("order", 1)):
        cond_txt = condition_node_to_english(st.get("condition"), ind_labels)
        mv = st.get("moveSlTo") or {}
        kind = mv.get("kind", "level")
        target_val = mv.get("level") or mv.get("price") or mv.get("points") or mv.get("indicatorId")
        trailing_previews.append({
            "stepId": st.get("id"),
            "label": st.get("label"),
            "enabled": bool(st.get("enabled", True)),
            "text": f"IF {cond_txt} THEN MOVE SL TO {kind.upper()} {target_val}",
        })

    return {
        "entry": entry_previews,
        "exit": exit_previews,
        "trailing": trailing_previews,
    }
