from contextlib import asynccontextmanager
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Any, List, Optional
import asyncio
import collections
import hashlib, json, requests, secrets, time, threading, traceback
from datetime import datetime, timezone, timedelta

try:
    from pydantic import ConfigDict, field_validator
    HAS_PYDANTIC_V2 = True
except ImportError:
    from pydantic import validator as _v1_validator
    ConfigDict = None
    HAS_PYDANTIC_V2 = False

    def field_validator(field_name: str):
        return _v1_validator(field_name, allow_reuse=True)

from .config import settings
from .levels import FormulaNotProvided, LevelBook, LevelError
from .repos import build_repos, clean_actor
from .schema_v2 import json_schema, new_rule_builder, StrictModel, strip_secret_fields, strategy_to_english_preview
from .strategy import new_strategy, validate_strategy, compile_strategy, strategy_warnings
from .market_service import market_service, INSTRUMENT_CATALOG, _resolve_lot_size
from .contract_master import ContractMasterError, contract_master_service, DEFAULT_SEGMENTS
from .sar_engine import (
    parse_config, validate_sar, risk_breakdown, build_ladder, compute_levels, new_run, step,
    FINAL_STATUSES, IST
)
from .indicators import evaluate_indicator_config
from .condition_engine import (
    ConditionEngine,
    EvaluationState,
    check_reentry_eligibility,
    parse_ist_datetime,
    resample_candles_to_timeframe,
)
from .exit_engine import (
    ExitEngineError,
    open_managed_position,
    step_open_position,
)
from .backtest_engine import (
    PAPER_FINAL_STATUSES,
    BacktestError,
    _finalize_closed_trade_record,
    _force_close_position,
    new_paper_worker_run,
    run_backtest,
    step_paper_worker_run,
)
from .broker_execution import (    BrokerSessionExpiredError,
    IIFLBrokerAdapter,
    approve_strategy_for_live,
    compute_broker_positions_and_pnl,
    compute_strategy_version_hash,
    evaluate_go_live_gates,
    get_broker_capabilities,
    place_broker_order,
    validate_execution_settings,
)
from .samples import all_sample_strategies
from .python_analyzer import (
    analyse_source,
    decode_source,
    validate_source_bytes,
    MAX_SOURCE_BYTES,
)

def _refresh_contract_master_startup():
    try:
        contract_master_service.refresh_if_stale()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    start_background_daemons()
    market_service.start_poller(interval_seconds=3.0)
    threading.Thread(
        target=_refresh_contract_master_startup,
        name='contract-master-startup-refresh',
        daemon=True,
    ).start()
    try:
        yield
    finally:
        market_service.stop_poller()
        stop_background_daemons()


app = FastAPI(
    title=settings.app_name,
    version='2.0.0',
    description='Visual strategy studio with live market data feeds, Phase 2/3 condition, sequence & re-entry evaluation engine, real-time paper trading, and IIFL session integration.',
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173', 'http://localhost:3000', 'http://127.0.0.1:3000'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*']
)

class StrategyPayload(BaseModel):
    strategy: dict[str, Any]

class LoginPayload(BaseModel):
    clientId: str = Field(min_length=1)
    authCode: str = Field(min_length=1)

class CloseTradePayload(BaseModel):
    tradeId: str

class RestorePayload(BaseModel):
    versionId: str = Field(min_length=1)

class LevelSheetPayload(StrictModel):
    """Strict API boundary for a complete per-symbol/per-day level sheet."""
    symbol: str = Field(min_length=1, max_length=64)
    day: str = Field(min_length=10, max_length=10)
    provider: str = 'MANUAL'
    values: dict[str, Any]

    @field_validator("symbol")
    @classmethod
    def trim_symbol(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("symbol is required")
        return value

    @field_validator("day")
    @classmethod
    def validate_calendar_day(cls, value):
        from datetime import date
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("day must be a real YYYY-MM-DD date") from exc
        if len(value) != 10:
            raise ValueError("day must use YYYY-MM-DD format")
        return value

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value):
        value = value.strip().upper()
        if value not in ("MANUAL", "PLACEHOLDER"):
            raise ValueError("only MANUAL or PLACEHOLDER sheets can be saved")
        return value

    @field_validator("values")
    @classmethod
    def validate_values(cls, value):
        import math
        from .schema_v2 import LEVEL_NAMES
        from .levels import normalize_level_name
        if not isinstance(value, dict):
            raise ValueError("values must be an object")
        normalized = {}
        for key, raw in value.items():
            try:
                name = normalize_level_name(key)
            except Exception as exc:
                raise ValueError(str(exc)) from exc
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
                raise ValueError(f"{name} must be a finite number")
            normalized[name] = float(raw)
        missing = [name for name in LEVEL_NAMES if name not in normalized]
        if missing:
            raise ValueError("missing FC/RC levels: " + ", ".join(missing))
        return normalized


class IndicatorComputePayload(BaseModel):
    indicator: dict[str, Any]
    candles: List[dict[str, Any]]


class EngineEvaluatePayload(BaseModel):
    strategy: dict[str, Any]
    candlesByTimeframe: dict[str, List[dict[str, Any]]]
    evalTime: Any
    state: Optional[dict[str, Any]] = None
    persistState: bool = False
    autoRecordEntry: bool = False
    liveTick: Optional[dict[str, Any]] = None
    executionQuote: Optional[dict[str, Any]] = None
    manualLevelsOverride: Optional[dict[str, float]] = None
    primaryTimeframe: str = '5m'


class SequenceResetPayload(BaseModel):
    strategyId: str = Field(min_length=1)
    sequenceId: Optional[str] = None
    reason: str = 'EXPLICIT_INVALIDATION'
    evalTime: Optional[Any] = None


class TradeEventPayload(BaseModel):
    strategyId: str = Field(min_length=1)
    eventType: str = Field(min_length=1)  # "ENTRY" | "REENTRY" | "EXIT"
    side: Optional[str] = 'BUY'
    exitReason: Optional[str] = None      # "STOP_LOSS" | "TARGET" | "MANUAL"
    ruleId: Optional[str] = None
    timestampIst: Any


class ReentryCheckPayload(BaseModel):
    strategy: dict[str, Any]
    evalTime: Any
    candidateSide: Optional[str] = None
    state: Optional[dict[str, Any]] = None


class ExitResolvePayload(BaseModel):
    strategy: dict[str, Any]
    side: str = 'BUY'
    entryPrice: float
    entryTime: Any
    candlesByTimeframe: dict[str, List[dict[str, Any]]] = Field(default_factory=dict)
    manualLevelsOverride: Optional[dict[str, float]] = None
    primaryTimeframe: str = '5m'
    isReentry: bool = False
    entryRuleId: Optional[str] = None
    lotSize: Optional[int] = None


class ExitStepPayload(BaseModel):
    position: dict[str, Any]
    candle: dict[str, Any]
    strategy: dict[str, Any]
    candlesByTimeframe: dict[str, List[dict[str, Any]]] = Field(default_factory=dict)
    evalTime: Any
    state: Optional[dict[str, Any]] = None
    lowerTimeframeCandles: Optional[List[dict[str, Any]]] = None
    manualLevelsOverride: Optional[dict[str, float]] = None
    primaryTimeframe: str = '5m' 


class BacktestRunPayload(BaseModel):
    strategy: dict[str, Any]
    candlesByTimeframe: dict[str, List[dict[str, Any]]]
    primaryTimeframe: str = '5m'
    lowerTimeframeCandles: Optional[List[dict[str, Any]]] = None
    manualLevelsByDay: Optional[dict[str, dict[str, float]]] = None
    executionFillMode: str = 'NEXT_CANDLE_OPEN'
    slippagePoints: float = 0.0
    slippagePercent: float = 0.0
    brokeragePerOrder: float = 0.0
    chargesPercent: float = 0.0
    lotSizeOverride: Optional[int] = None
    initialCapitalOverride: Optional[float] = None
    expiryHandling: str = 'SESSION_END_SQUARE_OFF'
    expiryDate: Optional[str] = None
    persistResult: bool = True


class PaperWorkerStartPayload(BaseModel):
    strategy: dict[str, Any]
    primaryTimeframe: str = '5m'
    slippagePoints: float = 0.0
    brokeragePerOrder: float = 0.0
    chargesPercent: float = 0.0


class PaperWorkerActionPayload(BaseModel):
    runId: str = Field(min_length=1)


class PaperWorkerStepPayload(BaseModel):
    runId: str = Field(min_length=1)
    candlesByTimeframe: dict[str, List[dict[str, Any]]]
    evalTime: Any
    manualLevelsOverride: Optional[dict[str, float]] = None


class LiveApprovePayload(BaseModel):
    strategy: dict[str, Any]
    confirmationText: str = Field(min_length=1)
    evalTime: Optional[Any] = None


class BrokerOrderPayload(BaseModel):
    strategy: dict[str, Any]
    executionSettings: dict[str, Any]
    side: str = 'BUY'
    idempotencyKey: str = Field(min_length=1)
    referencePrice: float = 24000.0
    evalTime: Optional[Any] = None


class BrokerCancelPayload(BaseModel):
    orderId: str = Field(min_length=1)


class KillSwitchPayload(BaseModel):
    reason: str = 'Emergency Kill Switch Activated' 


_repos = build_repos(settings)
strategy_repo = _repos.strategies
trade_repo = _repos.trades
run_repo = _repos.runs
level_book = LevelBook(_repos.levels)
eval_state_repo = _repos.eval_states
backtest_repo = _repos.backtests
paper_worker_repo = _repos.paper_workers
broker_order_repo = _repos.broker_orders
live_approval_repo = _repos.live_approvals
condition_engine = ConditionEngine(level_book=level_book)
PAPER_WORKER_LOCK = threading.RLock()
kill_switch_state = {'active': False, 'reason': None, 'activatedAtIst': None, 'actor': None}


def _actor(request: Request) -> str:
    return clean_actor(request.headers.get('x-actor'))

@app.get('/api/health')
def health():
    return {
        'ok': True,
        'service': settings.app_name,
        'mode': 'PAPER',
        'liveMarketData': True,
        'liveOrdersEnabled': False,
        'storeBackend': settings.store_backend,
        'version': '2.0.0'
    }

# ==================== STRATEGY ENDPOINTS ====================

@app.get('/api/strategies')
def list_strategies():
    return strategy_repo.list()

@app.post('/api/strategies')
def save_strategy(payload: StrategyPayload, request: Request):
    s = payload.strategy
    if not s.get('id'):
        raise HTTPException(400, 'Strategy id is required')
    if s.get('strategyType') == 'RULE_BUILDER':
        errors = validate_strategy(s)
        if errors:
            raise HTTPException(400, detail={'errors': errors})
    return strategy_repo.save(s, actor=_actor(request))

@app.post('/api/strategies/validate')
def validate(payload: StrategyPayload):
    errors = validate_strategy(payload.strategy)
    return {
        'valid': not errors,
        'errors': errors,
        'warnings': strategy_warnings(payload.strategy),
        'compiled': compile_strategy(payload.strategy) if not errors else None
    }

@app.get('/api/strategies/template')
def template(strategyType: Optional[str] = None):
    if strategyType in (None, '', 'INDICATOR'):
        return new_strategy()
    if strategyType == 'RULE_BUILDER':
        return new_rule_builder()
    raise HTTPException(400, 'Unknown strategyType.')

@app.get('/api/strategies/samples')
def list_sample_strategies():
    return all_sample_strategies()

@app.get('/api/strategies/schema')
def strategy_schema():
    return json_schema()

@app.get('/api/strategies/{strategy_id}/versions')
def strategy_versions(strategy_id: str):
    if strategy_repo.get(strategy_id) is None and not strategy_repo.list_versions(strategy_id):
        raise HTTPException(404, 'Strategy not found')
    return strategy_repo.list_versions(strategy_id)

@app.post('/api/strategies/{strategy_id}/restore')
def restore_strategy(strategy_id: str, payload: RestorePayload, request: Request):
    try:
        return strategy_repo.restore(strategy_id, payload.versionId, actor=_actor(request))
    except KeyError:
        raise HTTPException(404, 'Version not found')

@app.delete('/api/strategies/{strategy_id}')
def delete_strategy_endpoint(strategy_id: str):
    deleted = strategy_repo.delete(strategy_id)
    if not deleted:
        raise HTTPException(404, 'Strategy not found')
    return {'ok': True, 'deletedId': strategy_id}

@app.post('/api/strategies/{strategy_id}/duplicate')
def duplicate_strategy_endpoint(strategy_id: str, request: Request):
    existing = strategy_repo.get(strategy_id)
    if existing is None:
        raise HTTPException(404, 'Strategy not found')
    from uuid import uuid4
    import copy
    import re
    dup = copy.deepcopy(existing)
    new_id = str(uuid4())
    raw_name = str(existing.get('name') or 'Strategy').strip()
    base_name = re.sub(r'\s+\(Copy(?:\s+\d+)?\)$', '', raw_name).strip() or 'Strategy'
    existing_names = {
        str(s.get('name') or '').strip()
        for s in strategy_repo.list()
        if isinstance(s, dict)
    }
    copy_num = 1
    while f"{base_name} (Copy {copy_num})" in existing_names:
        copy_num += 1
    new_name = f"{base_name} (Copy {copy_num})"
    dup['id'] = new_id
    dup['name'] = new_name
    if isinstance(dup.get('meta'), dict):
        dup['meta']['id'] = new_id
        dup['meta']['name'] = new_name
        dup['meta']['status'] = 'DRAFT'
    else:
        dup['status'] = 'DRAFT'
    return strategy_repo.save(dup, actor=_actor(request), summary_override=f"duplicated from {strategy_id}")

@app.post('/api/strategies/export')
def export_strategy_endpoint(payload: StrategyPayload):
    cleaned, stripped_paths = strip_secret_fields(payload.strategy)
    return {
        'strategy': cleaned,
        'strippedCredentialFields': stripped_paths,
        'exportedAt': datetime.now(timezone.utc).isoformat(),
    }

@app.post('/api/strategies/import')
def import_strategy_endpoint(payload: StrategyPayload, request: Request):
    cleaned, stripped_paths = strip_secret_fields(payload.strategy)
    if not cleaned.get('id'):
        from uuid import uuid4
        new_id = str(uuid4())
        cleaned['id'] = new_id
        if isinstance(cleaned.get('meta'), dict):
            cleaned['meta']['id'] = new_id
    if cleaned.get('strategyType') == 'RULE_BUILDER':
        errors = validate_strategy(cleaned)
        if errors:
            raise HTTPException(400, detail={'errors': errors, 'strippedCredentialFields': stripped_paths})
    saved = strategy_repo.save(cleaned, actor=_actor(request), summary_override='imported strategy')
    return {
        'strategy': saved,
        'strippedCredentialFields': stripped_paths,
        'warnings': strategy_warnings(saved),
    }

@app.post('/api/strategies/preview-english')
def preview_english_endpoint(payload: StrategyPayload):
    return strategy_to_english_preview(payload.strategy)

# ==================== PHASE 2 & PHASE 3: INDICATORS, CONDITION, SEQUENCE & RE-ENTRY ENGINE ====================

@app.post('/api/indicators/compute')
def compute_indicator_endpoint(payload: IndicatorComputePayload):
    try:
        series = evaluate_indicator_config(payload.indicator, payload.candles)
        return {
            'indicatorId': payload.indicator.get('id'),
            'type': payload.indicator.get('type'),
            'count': len(payload.candles),
            'series': series,
        }
    except Exception as exc:
        raise HTTPException(400, str(exc))

@app.post('/api/engine/evaluate')
def evaluate_strategy_conditions(payload: EngineEvaluatePayload):
    strat = payload.strategy
    strat_id = strat.get('id')
    initial_state = payload.state
    if initial_state is None and payload.persistState and strat_id:
        stored = eval_state_repo.get(strat_id)
        if stored and isinstance(stored.get('state'), dict):
            initial_state = stored['state']

    state_obj = EvaluationState(initial_state)
    result = condition_engine.evaluate_strategy(
        strategy=strat,
        candles_by_tf=payload.candlesByTimeframe,
        eval_time=payload.evalTime,
        state=state_obj,
        live_tick=payload.liveTick,
        execution_quote=payload.executionQuote,
        manual_levels_override=payload.manualLevelsOverride,
        primary_timeframe=payload.primaryTimeframe,
        auto_record_entry=payload.autoRecordEntry,
    )
    if payload.persistState and strat_id and result.get('valid'):
        eval_state_repo.save(strat_id, result['state'])
    return result

@app.get('/api/engine/state/{strategy_id}')
def get_engine_state(strategy_id: str):
    stored = eval_state_repo.get(strategy_id)
    if not stored:
        return {'strategyId': strategy_id, 'state': EvaluationState().to_dict()}
    return stored

@app.delete('/api/engine/state/{strategy_id}')
def reset_engine_state(strategy_id: str):
    eval_state_repo.reset(strategy_id)
    return {'ok': True, 'strategyId': strategy_id}

@app.post('/api/engine/sequence/reset')
def reset_sequence_endpoint(payload: SequenceResetPayload):
    stored = eval_state_repo.get(payload.strategyId)
    state_obj = EvaluationState((stored or {}).get('state'))
    ts_iso = (
        parse_ist_datetime(payload.evalTime).isoformat()
        if payload.evalTime is not None
        else datetime.now(IST).isoformat()
    )
    state_obj.reset_sequence(payload.sequenceId, reason=payload.reason, timestamp_iso=ts_iso)
    saved = eval_state_repo.save(payload.strategyId, state_obj.to_dict())
    return saved

@app.post('/api/engine/trade-event')
def record_trade_event_endpoint(payload: TradeEventPayload):
    stored = eval_state_repo.get(payload.strategyId)
    state_obj = EvaluationState((stored or {}).get('state'))
    ev = payload.eventType.strip().upper()
    if ev in ('ENTRY', 'INITIAL_ENTRY'):
        state_obj.record_trade_entry(
            side=payload.side or 'BUY',
            timestamp_ist=payload.timestampIst,
            is_reentry=False,
            rule_id=payload.ruleId,
        )
    elif ev == 'REENTRY':
        state_obj.record_trade_entry(
            side=payload.side or 'BUY',
            timestamp_ist=payload.timestampIst,
            is_reentry=True,
            rule_id=payload.ruleId,
        )
    elif ev == 'EXIT':
        if not payload.exitReason:
            raise HTTPException(400, 'exitReason is required when eventType is EXIT (STOP_LOSS, TARGET, or MANUAL)')
        state_obj.record_trade_exit(
            exit_reason=payload.exitReason,
            timestamp_ist=payload.timestampIst,
            side=payload.side,
        )
    else:
        raise HTTPException(400, f"Unsupported eventType '{payload.eventType}'. Use ENTRY, REENTRY, or EXIT.")
    return eval_state_repo.save(payload.strategyId, state_obj.to_dict())

@app.post('/api/engine/reentry/check')
def check_reentry_endpoint(payload: ReentryCheckPayload):
    strat = payload.strategy
    strat_id = strat.get('id')
    raw_state = payload.state
    if raw_state is None and strat_id:
        stored = eval_state_repo.get(strat_id)
        if stored and isinstance(stored.get('state'), dict):
            raw_state = stored['state']
    state_obj = EvaluationState(raw_state)
    eval_dt_ist = parse_ist_datetime(payload.evalTime)
    reentry_cfg = strat.get('reentry') or {}
    return check_reentry_eligibility(
        reentry_cfg=reentry_cfg,
        reentry_state=state_obj.reentry_state,
        eval_dt_ist=eval_dt_ist,
        candidate_side=payload.candidateSide,
    )

@app.post('/api/engine/exit/resolve')
def resolve_exit_position_endpoint(payload: ExitResolvePayload):
    try:
        pos = open_managed_position(
            strategy=payload.strategy,
            side=payload.side,
            entry_price=payload.entryPrice,
            entry_time=payload.entryTime,
            candles_by_tf=payload.candlesByTimeframe,
            level_book=level_book,
            manual_levels_override=payload.manualLevelsOverride,
            primary_timeframe=payload.primaryTimeframe,
            is_reentry=payload.isReentry,
            entry_rule_id=payload.entryRuleId,
            lot_size=payload.lotSize,
        )
        return pos
    except ExitEngineError as exc:
        raise HTTPException(400, exc.message)

@app.post('/api/engine/exit/step')
def step_exit_position_endpoint(payload: ExitStepPayload):
    try:
        state_obj = EvaluationState(payload.state)
        pos_copy = dict(payload.position)
        result = step_open_position(
            position=pos_copy,
            candle=payload.candle,
            strategy=payload.strategy,
            condition_engine=condition_engine,
            candles_by_tf=payload.candlesByTimeframe,
            eval_time=payload.evalTime,
            eval_state=state_obj,
            lower_tf_candles=payload.lowerTimeframeCandles,
            manual_levels_override=payload.manualLevelsOverride,
            primary_timeframe=payload.primaryTimeframe,
        )
        result['state'] = state_obj.to_dict()
        return result
    except ExitEngineError as exc:
        raise HTTPException(400, exc.message)



# ==================== PHASE 5: BACKTEST SIMULATOR & PAPER TRADING WORKER ====================

@app.post('/api/backtest/run')
def run_backtest_endpoint(payload: BacktestRunPayload):
    try:
        res = run_backtest(
            strategy=payload.strategy,
            candles_by_tf=payload.candlesByTimeframe,
            level_book=level_book,
            primary_timeframe=payload.primaryTimeframe,
            lower_tf_candles=payload.lowerTimeframeCandles,
            manual_levels_by_day=payload.manualLevelsByDay,
            execution_fill_mode=payload.executionFillMode,
            slippage_points=payload.slippagePoints,
            slippage_percent=payload.slippagePercent,
            brokerage_per_order=payload.brokeragePerOrder,
            charges_percent=payload.chargesPercent,
            lot_size_override=payload.lotSizeOverride,
            initial_capital_override=payload.initialCapitalOverride,
            expiry_handling=payload.expiryHandling,
            expiry_date=payload.expiryDate,
        )
        if payload.persistResult:
            rows = backtest_repo.list()
            rows.insert(0, res)
            backtest_repo.save_all(rows[:100])
        return res
    except (BacktestError, ExitEngineError) as exc:
        raise HTTPException(400, detail={'errors': [exc.message]})

@app.get('/api/backtest/runs')
def list_backtest_runs(strategyId: Optional[str] = None):
    rows = backtest_repo.list()
    if strategyId:
        rows = [r for r in rows if r.get('strategyId') == strategyId]
    return rows[:50]

@app.post('/api/paper/worker/start')
def start_paper_worker_endpoint(payload: PaperWorkerStartPayload):
    s = payload.strategy
    if s.get('strategyType') != 'RULE_BUILDER':
        raise HTTPException(400, detail={'errors': ['Paper worker only accepts RULE_BUILDER strategies.']})
    try:
        with PAPER_WORKER_LOCK:
            runs = paper_worker_repo.list()
            for r in runs:
                if r.get('strategyId') == s.get('id') and r.get('status') not in PAPER_FINAL_STATUSES:
                    raise HTTPException(400, detail={'errors': ['A paper worker run for this strategy is already active. Stop it first.']})
            run = new_paper_worker_run(
                strategy=s,
                primary_timeframe=payload.primaryTimeframe,
                slippage_points=payload.slippagePoints,
                brokerage_per_order=payload.brokeragePerOrder,
                charges_percent=payload.chargesPercent,
            )
            runs.insert(0, run)
            paper_worker_repo.save_all(runs)
            return run
    except BacktestError as exc:
        raise HTTPException(400, detail={'errors': [exc.message]})

@app.post('/api/paper/worker/pause')
def pause_paper_worker_endpoint(payload: PaperWorkerActionPayload):
    with PAPER_WORKER_LOCK:
        runs = paper_worker_repo.list()
        matched = next((r for r in runs if r.get('id') == payload.runId), None)
        if not matched:
            raise HTTPException(404, 'Paper worker run not found')
        if matched.get('status') in PAPER_FINAL_STATUSES:
            return matched
        matched['status'] = 'PAUSED'
        matched['message'] = 'Paused by user'
        matched['updatedAt'] = datetime.now(IST).isoformat()
        paper_worker_repo.save_all(runs)
        return matched

@app.post('/api/paper/worker/resume')
def resume_paper_worker_endpoint(payload: PaperWorkerActionPayload):
    with PAPER_WORKER_LOCK:
        runs = paper_worker_repo.list()
        matched = next((r for r in runs if r.get('id') == payload.runId), None)
        if not matched:
            raise HTTPException(404, 'Paper worker run not found')
        if matched.get('status') in PAPER_FINAL_STATUSES:
            raise HTTPException(400, f"Cannot resume worker in status {matched.get('status')}")
        matched['status'] = 'RUNNING'
        matched['message'] = 'Resumed by user'
        matched['updatedAt'] = datetime.now(IST).isoformat()
        paper_worker_repo.save_all(runs)
        return matched

@app.post('/api/paper/worker/stop')
def stop_paper_worker_endpoint(payload: PaperWorkerActionPayload):
    with PAPER_WORKER_LOCK:
        runs = paper_worker_repo.list()
        matched = next((r for r in runs if r.get('id') == payload.runId), None)
        if not matched:
            raise HTTPException(404, 'Paper worker run not found')
        if matched.get('status') in PAPER_FINAL_STATUSES:
            return matched
        now_iso = datetime.now(IST).isoformat()
        open_pos = matched.get('openPosition')
        if open_pos is not None and open_pos.get('status') == 'OPEN':
            close_p = float(open_pos.get('entryPrice', 0.0))
            _force_close_position(open_pos, close_p, now_iso, 'MANUAL_WORKER_STOP')
            trade_rec = _finalize_closed_trade_record(
                open_pos,
                trade_no=len(matched.get('closedTrades') or []) + 1,
                slippage_points=float(matched.get('slippagePoints', 0.0)),
                slippage_pct=0.0,
                brokerage_per_order=float(matched.get('brokeragePerOrder', 0.0)),
                charges_pct=float(matched.get('chargesPercent', 0.0)),
            )
            matched.setdefault('closedTrades', []).append(trade_rec)
            matched['realizedPnlToday'] = round(float(matched.get('realizedPnlToday', 0.0)) + float(trade_rec['netPnl']), 2)
            matched['totalRealizedPnl'] = round(float(matched.get('totalRealizedPnl', 0.0)) + float(trade_rec['netPnl']), 2)
            matched['openPosition'] = None
            matched['unrealizedPnl'] = 0.0
        matched['status'] = 'STOPPED'
        matched['message'] = 'Stopped manually by user'
        matched['updatedAt'] = now_iso
        paper_worker_repo.save_all(runs)
        return matched

@app.post('/api/paper/worker/step')
def step_paper_worker_endpoint(payload: PaperWorkerStepPayload):
    with PAPER_WORKER_LOCK:
        runs = paper_worker_repo.list()
        matched = next((r for r in runs if r.get('id') == payload.runId), None)
        if not matched:
            raise HTTPException(404, 'Paper worker run not found')
        try:
            step_res = step_paper_worker_run(
                worker_run=matched,
                candles_by_tf=payload.candlesByTimeframe,
                eval_time=payload.evalTime,
                level_book=level_book,
                manual_levels_override=payload.manualLevelsOverride,
            )
            paper_worker_repo.save_all(runs)
            return {'run': matched, 'stepResult': step_res}
        except (BacktestError, ExitEngineError) as exc:
            raise HTTPException(400, detail={'errors': [exc.message]})

@app.get('/api/paper/worker/runs')
def list_paper_worker_runs(strategyId: Optional[str] = None):
    with PAPER_WORKER_LOCK:
        rows = paper_worker_repo.list()
        if strategyId:
            rows = [r for r in rows if r.get('strategyId') == strategyId]
        return rows[:50]



# ==================== PHASE 7: BROKER EXECUTION, ORDER LIFECYCLE, KILL SWITCH & GO-LIVE GATES ====================

# Live execution remains hard-blocked until a verified IIFL order adapter is connected and explicitly enabled.
LIVE_ADAPTER_CONNECTED = False


def _is_live_execution_active() -> bool:
    return bool(settings.live_execution_enabled and LIVE_ADAPTER_CONNECTED)


@app.get('/api/broker/capabilities')
def broker_capabilities_endpoint():
    return get_broker_capabilities(live_execution_enabled=_is_live_execution_active())

@app.post('/api/broker/live-gates')
def check_live_gates_endpoint(payload: StrategyPayload, evalTime: Optional[str] = None):
    s = payload.strategy
    iifl_st = {
        'configured': bool(settings.app_key and settings.app_secret),
        'sessionPresent': bool(getattr(app.state, 'iifl_token', None)),
    }
    return evaluate_go_live_gates(
        strategy=s,
        backtest_runs=backtest_repo.list(),
        paper_worker_runs=paper_worker_repo.list(),
        live_approvals=live_approval_repo.list(),
        kill_switch_state=kill_switch_state,
        iifl_status=iifl_st,
        eval_time=evalTime,
        live_execution_enabled=settings.live_execution_enabled,
        live_adapter_connected=LIVE_ADAPTER_CONNECTED,
    )

@app.post('/api/broker/approve-live')
def approve_live_endpoint(payload: LiveApprovePayload, request: Request):
    s = payload.strategy
    iifl_st = {
        'configured': bool(settings.app_key and settings.app_secret),
        'sessionPresent': bool(getattr(app.state, 'iifl_token', None)),
    }
    gate_status = evaluate_go_live_gates(
        strategy=s,
        backtest_runs=backtest_repo.list(),
        paper_worker_runs=paper_worker_repo.list(),
        live_approvals=live_approval_repo.list(),
        kill_switch_state=kill_switch_state,
        iifl_status=iifl_st,
        eval_time=payload.evalTime,
        live_execution_enabled=settings.live_execution_enabled,
        live_adapter_connected=LIVE_ADAPTER_CONNECTED,
    )
    try:
        approval = approve_strategy_for_live(
            strategy=s,
            confirmation_text=payload.confirmationText,
            gate_status=gate_status,
            actor=_actor(request),
            eval_time=payload.evalTime,
        )
    except ValueError as exc:
        raise HTTPException(400, detail={'errors': [str(exc)]})

    rows = [a for a in live_approval_repo.list() if a.get('strategyId') != s.get('id')]
    rows.insert(0, approval)
    live_approval_repo.save_all(rows)
    return {
        'approval': approval,
        'gateStatus': evaluate_go_live_gates(
            strategy=s,
            backtest_runs=backtest_repo.list(),
            paper_worker_runs=paper_worker_repo.list(),
            live_approvals=rows,
            kill_switch_state=kill_switch_state,
            iifl_status=iifl_st,
            eval_time=payload.evalTime,
            live_execution_enabled=settings.live_execution_enabled,
            live_adapter_connected=LIVE_ADAPTER_CONNECTED,
        ),
    }

@app.post('/api/broker/orders')
def place_broker_order_endpoint(payload: BrokerOrderPayload):
    s = payload.strategy
    iifl_st = {
        'configured': bool(settings.app_key and settings.app_secret),
        'sessionPresent': bool(getattr(app.state, 'iifl_token', None)),
    }
    gate_status = evaluate_go_live_gates(
        strategy=s,
        backtest_runs=backtest_repo.list(),
        paper_worker_runs=paper_worker_repo.list(),
        live_approvals=live_approval_repo.list(),
        kill_switch_state=kill_switch_state,
        iifl_status=iifl_st,
        eval_time=payload.evalTime,
        live_execution_enabled=settings.live_execution_enabled,
        live_adapter_connected=LIVE_ADAPTER_CONNECTED,
    )
    existing = broker_order_repo.list()
    exec_mode = str((payload.executionSettings or {}).get('mode') or 'PAPER').upper()
    ref_price = payload.referencePrice
    ref_quote = None
    if exec_mode == 'LIVE':
        exec_sym = str((payload.executionSettings or {}).get('symbol') or (s.get('execution') or {}).get('instrument', {}).get('underlying') or 'NIFTY 50')
        ref_quote = market_service.get_quote(exec_sym)
        if ref_quote.get('ltp') is not None:
            ref_price = float(ref_quote['ltp'])
    try:
        order_rec, is_dup = place_broker_order(
            strategy=s,
            execution_settings=payload.executionSettings,
            side=payload.side,
            idempotency_key=payload.idempotencyKey,
            existing_orders=existing,
            gate_status=gate_status,
            kill_switch_state=kill_switch_state,
            iifl_status=iifl_st,
            reference_price=ref_price,
            eval_time=payload.evalTime,
            live_execution_enabled=_is_live_execution_active(),
            live_broker_adapter=None,
            reference_quote=ref_quote,
        )
    except ValueError as exc:
        raise HTTPException(400, detail={'errors': [str(exc)]})

    if not is_dup:
        existing.insert(0, order_rec)
        broker_order_repo.save_all(existing[:200])
    if order_rec.get('status') == 'REJECTED' and order_rec.get('rejectionReason') == 'Live execution not connected':
        raise HTTPException(400, detail='Live execution not connected')
    return order_rec

@app.get('/api/broker/orders')
def list_broker_orders_endpoint(strategyId: Optional[str] = None):
    rows = broker_order_repo.list()
    if strategyId:
        rows = [r for r in rows if r.get('strategyId') == strategyId]
    return rows[:100]

@app.get('/api/broker/positions')
def list_broker_positions_endpoint(strategyId: Optional[str] = None):
    rows = broker_order_repo.list()
    if strategyId:
        rows = [r for r in rows if r.get('strategyId') == strategyId]
    ltp_by_sym: dict[str, float] = {}
    for r in rows:
        sym = str(r.get('symbol') or '').strip()
        if sym and sym not in ltp_by_sym:
            q = market_service.get_quote(sym)
            if q.get('ltp') is not None:
                ltp_by_sym[sym] = float(q['ltp'])
    try:
        return compute_broker_positions_and_pnl(rows, ltp_by_symbol=ltp_by_sym)
    except ValueError as exc:
        raise HTTPException(400, detail={'errors': [str(exc)]})

class BrokerSyncPayload(BaseModel):
    orderId: Optional[str] = None
    action: Optional[str] = None  # 'resolve' for manual resolution of TIMEOUT_UNCONFIRMED
    resolutionStatus: Optional[str] = None  # 'CANCELLED' | 'REJECTED'
    reason: Optional[str] = None


def _apply_sync_update(o: dict, upd: dict, now_iso: str) -> None:
    o['status'] = upd.get('status', o['status'])
    for k in ('brokerOrderId', 'exchangeOrderId', 'fillPrice', 'averageTradedPrice', 'filledQuantity', 'pendingQuantity', 'cancelledQuantity'):
        if upd.get(k) is not None:
            o[k] = upd[k]
    if upd.get('rejectionReason'):
        o['rejectionReason'] = upd['rejectionReason']
    o['updatedAtIst'] = now_iso


IIFL_HTTP_CLIENT = requests


def _get_iifl_adapter(token: Optional[str]) -> Optional[IIFLBrokerAdapter]:
    if not token:
        return None
    return IIFLBrokerAdapter(
        base_url=settings.iifl_base,
        session_token=token,
        http_client=IIFL_HTTP_CLIENT,
        contract_master=contract_master_service,
        on_session_invalid=_clear_expired_iifl_session,
    )


@app.post('/api/broker/orders/sync')
def sync_broker_orders_endpoint(payload: Optional[BrokerSyncPayload] = None):
    rows = broker_order_repo.list()
    now_iso = datetime.now(IST).isoformat()
    token = getattr(app.state, 'iifl_token', None)
    adapter = _get_iifl_adapter(token)

    # Manual resolve path for TIMEOUT_UNCONFIRMED orders
    if payload and payload.orderId and str(payload.action or '').lower() == 'resolve':
        matched = next((r for r in rows if r.get('id') == payload.orderId), None)
        if not matched:
            raise HTTPException(404, 'Order not found')
        if matched.get('status') != 'TIMEOUT_UNCONFIRMED':
            raise HTTPException(400, f"Order status is {matched.get('status')}, not TIMEOUT_UNCONFIRMED.")
        target_st = str(payload.resolutionStatus or 'CANCELLED').upper()
        if target_st not in ('CANCELLED', 'REJECTED'):
            raise HTTPException(400, "Manual resolutionStatus must be 'CANCELLED' or 'REJECTED' (never 'FILLED').")

        # Fail closed: require active IIFL token and successful GET /orders check before manual resolve
        if not adapter:
            raise HTTPException(401, 'Cannot manually resolve TIMEOUT_UNCONFIRMED without an active IIFL session token.')
        if not matched.get('orderTag'):
            raise HTTPException(400, 'Cannot verify order in GET /orders: orderTag missing.')
        try:
            book_ok, book_match = adapter.try_reconcile_by_order_tag(str(matched['orderTag']))
        except BrokerSessionExpiredError:
            app.state.iifl_token = None
            app.state.iifl_client_id = None
            app.state.iifl_session_expired = True
            raise HTTPException(401, 'Session expired, reconnect')
        except Exception as exc:
            raise HTTPException(502, f'Could not verify GET /orders before manual resolve: {type(exc).__name__}')
        if not book_ok:
            raise HTTPException(502, 'Could not verify GET /orders before manual resolve; retry sync later.')
        if book_match:
            _apply_sync_update(matched, book_match, now_iso)
            broker_order_repo.save_all(rows)
            return JSONResponse(
                status_code=409,
                content={
                    'detail': 'Matching order found in GET /orders; manual resolve refused and real status synced.',
                    'order': matched,
                },
            )

        matched['status'] = target_st
        matched['manuallyResolved'] = True
        matched['rejectionReason'] = payload.reason or f"Manually resolved from TIMEOUT_UNCONFIRMED as {target_st}"
        matched['updatedAtIst'] = now_iso
        broker_order_repo.save_all(rows)
        return {'synced': 1, 'resolved': matched, 'orders': rows[:100]}

    if not adapter:
        return {'synced': 0, 'orders': rows[:100]}
    synced = 0
    try:
        for o in rows:
            if o.get('mode') != 'LIVE':
                continue
            if o.get('status') == 'TIMEOUT_UNCONFIRMED' and o.get('orderTag'):
                upd = adapter.reconcile_by_order_tag(str(o['orderTag']))
                if upd:
                    _apply_sync_update(o, upd, now_iso)
                    synced += 1
            elif o.get('brokerOrderId') and o.get('status') in ('OPEN', 'PENDING', 'PARTIALLY_FILLED'):
                upd = adapter.sync_order_status(str(o['brokerOrderId']))
                if upd:
                    _apply_sync_update(o, upd, now_iso)
                    synced += 1
    except BrokerSessionExpiredError:
        app.state.iifl_token = None
        app.state.iifl_client_id = None
        app.state.iifl_session_expired = True
        raise HTTPException(401, 'Session expired, reconnect')
    if synced > 0:
        broker_order_repo.save_all(rows)
    return {'synced': synced, 'orders': rows[:100]}

@app.post('/api/broker/orders/cancel')
def cancel_broker_order_endpoint(payload: BrokerCancelPayload):
    rows = broker_order_repo.list()
    matched = next((r for r in rows if r.get('id') == payload.orderId), None)
    if not matched:
        raise HTTPException(404, 'Order not found')
    if matched.get('status') in ('FILLED', 'REJECTED', 'CANCELLED'):
        return matched
    token = getattr(app.state, 'iifl_token', None)
    adapter = _get_iifl_adapter(token)
    if matched.get('mode') == 'LIVE' and matched.get('brokerOrderId'):
        if not adapter:
            raise HTTPException(401, 'Active IIFL session required to cancel a LIVE broker order.')
        try:
            res = adapter.cancel_order(str(matched['brokerOrderId']))
        except BrokerSessionExpiredError:
            app.state.iifl_token = None
            app.state.iifl_client_id = None
            app.state.iifl_session_expired = True
            raise HTTPException(401, 'Session expired, reconnect')
        if not res.get('ok'):
            raise HTTPException(502, res.get('error') or 'IIFL order cancel failed')
    matched['status'] = 'CANCELLED'
    matched['updatedAtIst'] = datetime.now(IST).isoformat()
    broker_order_repo.save_all(rows)
    return matched

@app.get('/api/broker/kill-switch')
def get_kill_switch_endpoint():
    return kill_switch_state

@app.post('/api/broker/kill-switch')
def activate_kill_switch_endpoint(payload: KillSwitchPayload, request: Request):
    now_iso = datetime.now(IST).isoformat()
    kill_switch_state['active'] = True
    kill_switch_state['reason'] = payload.reason
    kill_switch_state['activatedAtIst'] = now_iso
    kill_switch_state['actor'] = _actor(request)

    # 1. Reconcile TIMEOUT_UNCONFIRMED orders first, then cancel all OPEN / PENDING / PARTIALLY_FILLED orders
    orders = broker_order_repo.list()
    token = getattr(app.state, 'iifl_token', None)
    adapter = _get_iifl_adapter(token)
    cancelled_orders = 0
    unresolved_orders: list[str] = []
    for o in orders:
        if o.get('status') == 'TIMEOUT_UNCONFIRMED':
            if adapter and o.get('orderTag'):
                try:
                    book_ok, upd = adapter.try_reconcile_by_order_tag(str(o['orderTag']))
                except Exception:
                    book_ok, upd = False, None
                if book_ok and upd:
                    _apply_sync_update(o, upd, now_iso)
                else:
                    unresolved_orders.append(str(o.get('id') or o.get('orderTag')))
                    continue
            else:
                unresolved_orders.append(str(o.get('id') or o.get('orderTag')))
                continue

        if o.get('status') in ('OPEN', 'PENDING', 'PARTIALLY_FILLED'):
            if o.get('mode') == 'LIVE' and o.get('brokerOrderId'):
                if not adapter:
                    o['brokerCancelError'] = 'No active IIFL session token during kill switch'
                    unresolved_orders.append(str(o.get('id') or o.get('brokerOrderId')))
                    continue
                try:
                    c_res = adapter.cancel_order(str(o['brokerOrderId']))
                except BrokerSessionExpiredError:
                    app.state.iifl_token = None
                    app.state.iifl_client_id = None
                    app.state.iifl_session_expired = True
                    o['brokerCancelError'] = 'IIFL session expired during kill switch cancel'
                    unresolved_orders.append(str(o.get('id') or o.get('brokerOrderId')))
                    continue
                except Exception as exc:
                    o['brokerCancelError'] = f'Broker cancel failed: {type(exc).__name__}'
                    unresolved_orders.append(str(o.get('id') or o.get('brokerOrderId')))
                    continue
                if not c_res.get('ok'):
                    o['brokerCancelError'] = c_res.get('error') or 'Broker cancel failed'
                    unresolved_orders.append(str(o.get('id') or o.get('brokerOrderId')))
                    continue
            o['status'] = 'CANCELLED'
            o['rejectionReason'] = f'Cancelled by Kill Switch: {payload.reason}'
            o['updatedAtIst'] = now_iso
            cancelled_orders += 1
    broker_order_repo.save_all(orders)

    # 2. Stop all active paper worker runs
    stopped_paper_workers = 0
    with PAPER_WORKER_LOCK:
        pw_runs = paper_worker_repo.list()
        for r in pw_runs:
            if r.get('status') not in PAPER_FINAL_STATUSES:
                open_pos = r.get('openPosition')
                if open_pos and open_pos.get('status') == 'OPEN':
                    _force_close_position(open_pos, float(open_pos.get('entryPrice', 0.0)), now_iso, 'KILL_SWITCH')
                r['openPosition'] = None
                r['status'] = 'STOPPED'
                r['message'] = f'Stopped by Kill Switch: {payload.reason}'
                r['updatedAt'] = now_iso
                stopped_paper_workers += 1
        paper_worker_repo.save_all(pw_runs)

    # 3. Stop all active SAR runs
    stopped_sar_runs = 0
    with SAR_LOCK:
        s_runs = run_repo.list()
        for r in s_runs:
            if r.get('status') not in FINAL_STATUSES:
                r['currentLeg'] = None
                r['status'] = 'STOPPED_MANUAL'
                r['message'] = f'Stopped by Kill Switch: {payload.reason}'
                r['updatedAt'] = now_iso
                stopped_sar_runs += 1
        run_repo.save_all(s_runs)

    # 4. Close all OPEN paper trades
    trades = trade_repo.list()
    closed_trades = 0
    for t in trades:
        if t.get('status') == 'OPEN':
            t['status'] = 'CLOSED'
            t['exitPrice'] = t.get('currentPrice') or t.get('entryPrice')
            t['exitAt'] = now_iso
            t['exitReason'] = 'KILL_SWITCH'
            t['note'] = f'Closed by Kill Switch: {payload.reason}'
            closed_trades += 1
    trade_repo.save_all(trades)

    # 5. Revoke all active live approvals
    live_approval_repo.save_all([])

    return {
        'killSwitch': dict(kill_switch_state),
        'cancelledOrders': cancelled_orders,
        'unresolvedOrders': unresolved_orders,
        'stoppedPaperWorkers': stopped_paper_workers,
        'stoppedSarRuns': stopped_sar_runs,
        'closedPaperTrades': closed_trades,
    }

@app.post('/api/broker/kill-switch/reset')
def reset_kill_switch_endpoint(request: Request):
    kill_switch_state['active'] = False
    kill_switch_state['reason'] = None
    kill_switch_state['activatedAtIst'] = None
    kill_switch_state['actor'] = _actor(request)
    return {'killSwitch': dict(kill_switch_state)}

# ==================== LIVE MARKET ENDPOINTS ====================

DEFAULT_WATCHLIST = [
    'NIFTY 50', 'BANKNIFTY', 'FINNIFTY', 'SENSEX',
    'NIFTY IT', 'RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK'
]

@app.get('/api/market/live')
def get_live_market(symbols: Optional[str] = None):
    if symbols:
        sym_list = [s.strip() for s in symbols.split(',') if s.strip()]
    else:
        sym_list = DEFAULT_WATCHLIST

    quotes = market_service.get_quotes_batch(sym_list)
    return {
        'source': 'YAHOO FINANCE (DELAYED ~15m)',
        'isLive': False,
        'feedType': 'DELAYED_YAHOO',
        'exchangeRealtime': False,
        'disclaimer': 'Quotes are sourced from Yahoo Finance and may be delayed versus real-time exchange ticks.',
        'updatedAt': time.time(),
        'items': quotes
    }

@app.get('/api/market/demo')
def demo_market():
    quotes = market_service.get_quotes_batch(['NIFTY 50', 'BANKNIFTY', 'FINNIFTY', 'SENSEX'])
    return {
        'source': 'LIVE MARKET FEED',
        'updatedAt': time.time(),
        'items': quotes
    }

@app.get('/api/market/quote/{symbol}')
def get_quote_detail(symbol: str):
    quote = market_service.get_quote(symbol)
    strikes = market_service.calculate_strikes(symbol, quote.get('ltp', 0.0))
    return {
        'quote': quote,
        'strikes': strikes
    }

@app.get('/api/market/chart')
def get_chart_data(symbol: str = 'NIFTY 50', range: str = '1d', interval: str = '5m'):
    return market_service.get_chart(symbol, range_period=range, interval=interval)

@app.get('/api/market/search')
def search_market_symbols(q: str):
    return market_service.search_symbols(q)


@app.get('/api/contracts/search')
def search_contract_master(q: str, segment: Optional[str] = None, limit: int = 20):
    try:
        items = contract_master_service.search(q, segment=segment, limit=limit)
        return {'available': True, 'source': 'CONTRACT_MASTER', 'items': items}
    except ContractMasterError as exc:
        raise HTTPException(exc.status_code, detail={'error': exc.message})


@app.get('/api/contracts/lot-size')
def get_contract_lot_size(
    symbol: Optional[str] = None,
    underlying: Optional[str] = None,
    segment: str = 'NSEFO',
    instrumentType: Optional[str] = None,
    expiry: Optional[str] = None,
    strike: Optional[str] = None,
    optionType: Optional[str] = None,
    instrumentId: Optional[str] = None,
):
    raw_target = (underlying or symbol or '').strip()
    mapped_underlying = {
        'NIFTY 50': 'NIFTY',
        'NIFTY BANK': 'BANKNIFTY',
        'NIFTY FIN SERVICE': 'FINNIFTY',
    }.get(raw_target.upper(), raw_target)
    try:
        if any([instrumentId, instrumentType, expiry, strike, optionType]):
            info = contract_master_service.get_lot_size(
                segment=segment,
                underlying=mapped_underlying or None,
                instrument_type=instrumentType,
                expiry=expiry,
                strike=strike,
                option_type=optionType,
                instrument_id=instrumentId,
            )
            return {
                **info,
                'underlying': mapped_underlying.upper(),
                'source': 'CONTRACT_MASTER',
            }
        seg = contract_master_service._ensure_fresh_loaded(segment)
        summary = contract_master_service.get_lot_sizes_summary(seg)
        u_key = mapped_underlying.upper()
        if u_key not in summary:
            raise ContractMasterError(f"Lot size not found in {seg} for '{u_key}'.", status_code=404)
        return {
            'segment': seg,
            'underlying': u_key,
            'lotSize': summary[u_key],
            'source': 'CONTRACT_MASTER',
        }
    except ContractMasterError as exc:
        raise HTTPException(exc.status_code, detail={'error': exc.message})

@app.websocket('/api/market/ws')
async def market_websocket(websocket: WebSocket):
    await websocket.accept()
    subscribed_symbols = list(DEFAULT_WATCHLIST)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=0.1)
                if isinstance(msg, dict) and msg.get('action') == 'subscribe':
                    new_syms = msg.get('symbols', [])
                    if new_syms and isinstance(new_syms, list):
                        subscribed_symbols = [str(s).strip() for s in new_syms if str(s).strip()]
            except asyncio.TimeoutError:
                pass

            quotes = market_service.get_quotes_batch(subscribed_symbols)
            await websocket.send_json({
                'type': 'QUOTE_TICK',
                'source': 'LIVE YAHOO/NSE FEED',
                'timestamp': time.time(),
                'items': quotes
            })
            await asyncio.sleep(2.5)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass

# ==================== PAPER TRADING WITH REAL MARKET DATA ====================

@app.get('/api/trades')
def list_trades():
    rows = trade_repo.list()
    updated_rows = []
    symbol_cache = {}

    for t in rows:
        item = dict(t)
        if item.get('status') == 'OPEN':
            sym = item.get('symbol', 'NIFTY 50')
            if sym not in symbol_cache:
                symbol_cache[sym] = market_service.get_quote(sym)
            live_quote = symbol_cache[sym]
            current_spot = live_quote.get('ltp', 0.0)

            entry_spot = item.get('entrySpotPrice', current_spot)
            entry_premium = item.get('entryPrice', 100.0)
            side = item.get('side', 'BUY')
            qty = item.get('quantity', 1)
            lot_size = item.get('lotSize') or _resolve_lot_size(sym)
            if lot_size is None:
                continue
            inst_type = item.get('instrumentType', 'OPTION')

            if inst_type == 'OPTION':
                strike_info = market_service.calculate_strikes(sym, current_spot)
                strike_level = item.get('strikeLevel', 'ATM')
                opt_type = item.get('optionType', 'CE')
                level_data = strike_info.get(strike_level, strike_info.get('ATM', {}))
                current_price = level_data.get(opt_type, {}).get('premium', entry_premium) if isinstance(level_data.get(opt_type), dict) else entry_premium
            else:
                current_price = current_spot

            item['currentPrice'] = round(current_price, 2)
            item['currentSpot'] = round(current_spot, 2)

            total_units = qty * lot_size
            if side == 'BUY':
                unrealized_pnl = (current_price - entry_premium) * total_units
            else:
                unrealized_pnl = (entry_premium - current_price) * total_units

            item['pnl'] = round(unrealized_pnl, 2)
            item['pnlPercent'] = round(((current_price - entry_premium) / entry_premium * 100.0) if entry_premium > 0 else 0.0, 2)
            if side == 'SELL':
                item['pnlPercent'] = -item['pnlPercent']

        updated_rows.append(item)

    return updated_rows

@app.post('/api/paper/run')
def run_paper(payload: StrategyPayload):
    s = payload.strategy
    errors = validate_strategy(s)
    if errors:
        raise HTTPException(400, detail={'errors': errors})
    if s.get('strategyType') == 'RULE_BUILDER':
        with PAPER_WORKER_LOCK:
            pw_runs = paper_worker_repo.list()
            active = next(
                (r for r in pw_runs if r.get('strategyId') == s.get('id') and r.get('status') not in PAPER_FINAL_STATUSES),
                None,
            )
            if active:
                return active
            primary_tf = (s.get('indicators') or [{}])[0].get('timeframe') or s.get('timeframe') or '5m'
            try:
                pw_run = new_paper_worker_run(strategy=s, primary_timeframe=primary_tf)
            except BacktestError as exc:
                raise HTTPException(400, detail={'errors': [exc.message]})
            pw_runs.insert(0, pw_run)
            paper_worker_repo.save_all(pw_runs)
            return pw_run

    instrument = s.get('instrument', 'NIFTY 50')
    pos = s.get('positions', [{}])[0] if s.get('positions') else {}

    quote = market_service.get_quote(instrument)
    live_spot = quote.get('ltp', 0.0)
    meta = market_service.get_metadata(instrument)
    lot_size = meta.get('lot_size')
    if lot_size is None:
        raise HTTPException(400, detail={'errors': [f"Lot size unavailable for {instrument}: contract master not loaded or symbol not found."]})

    side = pos.get('side', 'BUY')
    inst_type = pos.get('instrumentType', 'OPTION')
    strike_choice = pos.get('strike', 'ATM')
    opt_type = pos.get('optionType', 'CE')
    quantity = int(pos.get('quantity', 1))

    if inst_type == 'OPTION':
        strikes = market_service.calculate_strikes(instrument, live_spot)
        level_data = strikes.get(strike_choice, strikes.get('ATM', {}))
        strike_details = level_data.get(opt_type, {}) if isinstance(level_data.get(opt_type), dict) else {}
        exec_strike = strike_details.get('strike', strikes['ATM']['strike'])
        entry_price = strike_details.get('premium', 150.0)
        contract_symbol = f"{instrument} {exec_strike} {opt_type}"
    else:
        exec_strike = None
        entry_price = live_spot
        contract_symbol = f"{instrument} (EQUITY/FUT)"

    total_units = quantity * lot_size
    traded_value = round(entry_price * total_units, 2)

    rows = trade_repo.list()
    item = {
        'id': secrets.token_hex(6),
        'strategy': s.get('name', 'Strategy'),
        'symbol': instrument,
        'contract': contract_symbol,
        'instrumentType': inst_type,
        'side': side,
        'quantity': quantity,
        'lotSize': lot_size,
        'totalUnits': total_units,
        'entrySpotPrice': live_spot,
        'entryPrice': entry_price,
        'currentPrice': entry_price,
        'strikeLevel': strike_choice,
        'strikePrice': exec_strike,
        'optionType': opt_type,
        'tradedValue': traded_value,
        'pnl': 0.0,
        'pnlPercent': 0.0,
        'status': 'OPEN',
        'mode': 'PAPER',
        'source': 'LIVE MARKET SIMULATION',
        'note': f'Simulated fill at live market spot ₹{live_spot:,.2f} ({contract_symbol} @ ₹{entry_price:,.2f})',
        'createdAt': time.strftime('%Y-%m-%d %H:%M:%S')
    }
    rows.insert(0, item)
    trade_repo.save_all(rows[:200])
    return item

@app.post('/api/paper/close')
def close_paper_trade(payload: CloseTradePayload):
    rows = trade_repo.list()
    matched = None
    for item in rows:
        if item.get('id') == payload.tradeId:
            matched = item
            break

    if not matched:
        raise HTTPException(404, 'Trade not found')

    if matched.get('sarRunId'):
        raise HTTPException(400, 'This trade belongs to a Stop-and-Reverse run. Stop the run instead.')

    if matched.get('status') == 'CLOSED':
        return matched

    sym = matched.get('symbol', 'NIFTY 50')
    live_quote = market_service.get_quote(sym)
    live_spot = live_quote.get('ltp', 0.0)
    inst_type = matched.get('instrumentType', 'OPTION')

    if inst_type == 'OPTION':
        strikes = market_service.calculate_strikes(sym, live_spot)
        level_data = strikes.get(matched.get('strikeLevel', 'ATM'), strikes.get('ATM', {}))
        opt_type = matched.get('optionType', 'CE')
        exit_price = level_data.get(opt_type, {}).get('premium', matched.get('entryPrice', 100.0))
    else:
        exit_price = live_spot

    side = matched.get('side', 'BUY')
    total_units = matched.get('totalUnits')
    if total_units is None:
        lot_size = matched.get('lotSize') or _resolve_lot_size(sym)
        if lot_size is None:
            raise HTTPException(400, detail={'errors': [f"Lot size unavailable for {sym}: contract master not loaded or symbol not found."]})
        total_units = matched.get('quantity', 1) * lot_size
    entry_price = matched.get('entryPrice', 100.0)

    if side == 'BUY':
        final_pnl = (exit_price - entry_price) * total_units
    else:
        final_pnl = (entry_price - exit_price) * total_units

    matched['status'] = 'CLOSED'
    matched['exitPrice'] = round(exit_price, 2)
    matched['exitSpotPrice'] = round(live_spot, 2)
    matched['exitAt'] = time.strftime('%Y-%m-%d %H:%M:%S')
    matched['pnl'] = round(final_pnl, 2)
    matched['pnlPercent'] = round(((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0, 2)
    if side == 'SELL':
        matched['pnlPercent'] = -matched['pnlPercent']
    matched['note'] = f'Closed at live market price ₹{exit_price:,.2f}'

    trade_repo.save_all(rows)
    return matched

# ==================== IIFL INTEGRATION ====================

@app.get('/api/iifl/status')
def iifl_status():
    # NOTE: This endpoint MUST return only booleans / safe metadata.
    # App Key, App Secret, authCode and session token MUST NEVER appear in this response.
    has_creds   = bool(settings.app_key and settings.app_secret)
    has_token   = bool(getattr(app.state, 'iifl_token', None))
    expired     = bool(getattr(app.state, 'iifl_session_expired', False))
    has_profile = bool(getattr(app.state, 'iifl_profile_verified', False))
    return {
        # ── New four-boolean shape (UI v2) ──────────────────────────────────
        'credentialsConfigured':    has_creds,
        'authenticationCompleted':  has_token and not expired,
        'profileVerified':          has_profile and has_token and not expired,
        'sessionActive':            has_token and not expired,
        'sessionExpired':           expired,
        # ── Legacy keys retained for backward compatibility ─────────────────
        'configured':               has_creds,
        'sessionPresent':           has_token and not expired,
        'sessionMessage': 'Session expired, reconnect' if expired else None,
        # ── Safe metadata (not secrets) ─────────────────────────────────────
        'baseUrl': settings.iifl_base,
        'liveOrdersEnabled': False,
    }

@app.get('/api/iifl/login-url')
def iifl_login_url():
    if not settings.app_key:
        raise HTTPException(400, 'Set IIFL_APP_KEY in backend/.env first.')
    from urllib.parse import quote
    url = f'https://markets.iiflcapital.com/?v=1&appkey={quote(settings.app_key)}&redirecturl={quote(settings.redirect_url, safe="")}'
    return {'url': url, 'redirectUrl': settings.redirect_url}

def _clear_expired_iifl_session():
    app.state.iifl_token = None
    app.state.iifl_client_id = None
    app.state.iifl_session_expired = True
    app.state.iifl_profile_verified = False

def _exchange_iifl_session(client_id: str, auth_code: str):
    if not settings.app_secret:
        raise HTTPException(400, 'Set IIFL_APP_SECRET in backend/.env first.')
    checksum = hashlib.sha256((client_id + auth_code + settings.app_secret).encode('utf-8')).hexdigest()
    try:
        r = IIFL_HTTP_CLIENT.post(settings.iifl_base + '/getusersession', json={'checkSum': checksum}, timeout=20)
        if getattr(r, 'status_code', None) == 401:
            _clear_expired_iifl_session()
            raise HTTPException(401, 'Session expired, reconnect')
        r.raise_for_status()
        data = r.json()
    except HTTPException:
        raise
    except requests.RequestException as e:
        raise HTTPException(502, f'IIFL session request failed: {type(e).__name__}')
    token = data.get('userSession')
    if str(data.get('status', '')).lower() != 'ok' or not token:
        raise HTTPException(401, 'IIFL did not create a session. Check fresh authCode and app credentials.')
    app.state.iifl_token = token
    app.state.iifl_client_id = client_id
    app.state.iifl_session_expired = False
    app.state.iifl_created_at = time.time()
    return {'ok': True, 'message': 'IIFL session created. Token is held in backend memory only.'}

@app.post('/api/iifl/session')
def create_iifl_session(payload: LoginPayload):
    return _exchange_iifl_session(payload.clientId, payload.authCode)

@app.get('/api/iifl/callback')
def iifl_callback(clientId: str, authCode: str):
    result = _exchange_iifl_session(clientId, authCode)
    return {'ok': True, 'message': 'IIFL login callback received and session created. Return to Strategy Studio and verify profile.'}

@app.get('/api/iifl/profile')
def iifl_profile():
    token = getattr(app.state, 'iifl_token', None)
    if not token:
        msg = 'Session expired, reconnect' if getattr(app.state, 'iifl_session_expired', False) else 'Connect IIFL first.'
        raise HTTPException(401, msg)
    try:
        r = IIFL_HTTP_CLIENT.get(settings.iifl_base + '/profile', headers={'Authorization': f'Bearer {token}'}, timeout=20)
        if getattr(r, 'status_code', None) == 401:
            _clear_expired_iifl_session()
            raise HTTPException(401, 'Session expired, reconnect')
        raw_txt = getattr(r, 'text', '') or ''
        if any(c in raw_txt.upper() for c in ('EC044', 'EC055', 'EC075')):
            _clear_expired_iifl_session()
            raise HTTPException(401, 'Session expired, reconnect')
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            blob = ' '.join(str(data.get(k, '')) for k in ('errorCode', 'code', 'message', 'status')).upper()
            if any(c in blob for c in ('EC044', 'EC055', 'EC075')):
                _clear_expired_iifl_session()
                raise HTTPException(401, 'Session expired, reconnect')
        # Mark profile as verified in backend state (boolean only — no data stored here)
        app.state.iifl_profile_verified = True
        return data
    except HTTPException:
        raise
    except requests.RequestException as e:
        raise HTTPException(502, f'IIFL profile request failed: {type(e).__name__}')

@app.post('/api/iifl/logout')
def iifl_logout():
    token = getattr(app.state, 'iifl_token', None)
    if token:
        try:
            IIFL_HTTP_CLIENT.post(settings.iifl_base + '/profile/logout', headers={'Authorization': f'Bearer {token}'}, timeout=10)
        except requests.RequestException:
            pass
    app.state.iifl_token = None
    app.state.iifl_client_id = None
    app.state.iifl_session_expired = False
    app.state.iifl_profile_verified = False
    return {'ok': True, 'message': 'Local IIFL session cleared.'}

# ==================== PYTHON STRATEGY IMPORTER ====================
# SECURITY: Python source is NEVER executed, imported, eval'd, compile()-and-run,
# or passed to subprocess. Only ast.parse + static AST visitor traversal.

# Simple in-memory rate limiter: max 120 analyse calls per IP per minute.
_PYTHON_IMPORT_RATE: dict = collections.defaultdict(list)
_PYTHON_IMPORT_LIMIT = 120
_PYTHON_IMPORT_WINDOW = 60  # seconds


def _check_python_import_rate(ip: str) -> bool:
    """Return True if the request is within rate limit."""
    now = time.time()
    timestamps = _PYTHON_IMPORT_RATE[ip]
    # Prune old entries
    _PYTHON_IMPORT_RATE[ip] = [t for t in timestamps if now - t < _PYTHON_IMPORT_WINDOW]
    if len(_PYTHON_IMPORT_RATE[ip]) >= _PYTHON_IMPORT_LIMIT:
        return False
    _PYTHON_IMPORT_RATE[ip].append(now)
    return True


class PythonAnalysePayload(BaseModel):
    source: str = Field(min_length=1)
    filename: str = Field(default='strategy.py', max_length=120)


@app.post('/api/python-import/analyse')
async def python_import_analyse_text(payload: PythonAnalysePayload, request: Request):
    """
    Analyse pasted Python source text. Static analysis only — source is NEVER executed.
    """
    ip = request.client.host if request.client else '127.0.0.1'
    if not _check_python_import_rate(ip):
        raise HTTPException(429, 'Rate limit: max 10 analyses per minute. Please wait.')

    source_bytes = payload.source.encode('utf-8', errors='replace')
    ok, err = validate_source_bytes(source_bytes)
    if not ok:
        raise HTTPException(400, err)

    # Reject non-.py filenames
    fname = payload.filename.strip()
    if not fname.endswith('.py'):
        fname = 'strategy.py'

    # Static analysis only — NEVER executes source
    report = analyse_source(payload.source, filename=fname)
    return report


@app.post('/api/python-import/upload')
async def python_import_upload_file(request: Request, file: UploadFile = File(...)):
    """
    Analyse an uploaded .py file. Static analysis only — file is NEVER written to disk
    under its original name, never executed, never imported.
    """
    ip = request.client.host if request.client else '127.0.0.1'
    if not _check_python_import_rate(ip):
        raise HTTPException(429, 'Rate limit: max 10 analyses per minute. Please wait.')

    # Filename validation
    original_name = (file.filename or 'strategy.py').strip()
    if not original_name.endswith('.py'):
        raise HTTPException(400, 'Only .py files are accepted.')

    # Read into memory — NEVER write to a path derived from file.filename
    raw = await file.read()

    ok, err = validate_source_bytes(raw)
    if not ok:
        raise HTTPException(400, err)

    source, decode_err = decode_source(raw)
    if source is None:
        raise HTTPException(400, decode_err)

    # Static analysis only — NEVER executes source
    report = analyse_source(source, filename=original_name)
    return report


# ==================== STOP-AND-REVERSE (LEVEL_SAR) ENGINE ====================

SAR_LOCK = threading.RLock()
WORKER_STOP_EVENT = threading.Event()
_WORKER_THREADS: list[threading.Thread] = []

class SarPreviewPayload(BaseModel):
    strategy: dict[str, Any]
    referencePrice: Optional[float] = None

class SarStopPayload(BaseModel):
    runId: str

def sar_background_worker():
    while not WORKER_STOP_EVENT.is_set():
        try:
            with SAR_LOCK:
                runs = run_repo.list()
                if not any(r.get('status') not in FINAL_STATUSES for r in runs):
                    WORKER_STOP_EVENT.wait(2.0)
                    continue
                trades = trade_repo.list()
                runs_changed = False
                trades_changed = False
                now_ist = datetime.now(IST)

                for run in runs:
                    if run.get('status') in FINAL_STATUSES:
                        continue

                    sym = run.get('symbol')
                    if not sym:
                        continue

                    cfg = run.get('cfg')
                    if not cfg:
                        strat = run.get('strategy')
                        if strat:
                            cfg = parse_config(strat)
                            run['cfg'] = cfg
                            runs_changed = True
                        else:
                            continue

                    quote = market_service.get_quote(sym)

                    is_live_source = quote.get('source') == 'LIVE YAHOO/NSE FEED'
                    no_data = bool(quote.get('noData'))
                    ltp = quote.get('ltp')
                    is_market_open = bool(quote.get('isMarketOpen'))

                    if not is_market_open:
                        msg = "Waiting: market closed"
                        if run.get('message') != msg:
                            run['message'] = msg
                            runs_changed = True
                        continue

                    if not is_live_source or no_data or ltp is None or ltp <= 0:
                        msg = "Waiting: no live market data"
                        if run.get('message') != msg:
                            run['message'] = msg
                            runs_changed = True
                        continue

                    level_data = None
                    if cfg.get('levelSource') == 'CANDLE' and run.get('levels') is None:
                        tf_str = cfg.get('candleTimeframe', '15m')
                        try:
                            tf_mins = int(tf_str.replace('m', ''))
                        except Exception:
                            tf_mins = 15
                        start_time = cfg.get('startTime', '09:15')
                        level_data = market_service.get_level_candle(sym, start_time, tf_mins)
                        if quote.get('prevClose'):
                            level_data['prev_close'] = quote.get('prevClose')

                    cmp = float(ltp)
                    before_snap = json.dumps({k: v for k, v in run.items() if k != 'updatedAt'}, sort_keys=True, default=str)
                    events = step(run, cfg, cmp, now_ist, level_data=level_data)
                    run['lastPrice'] = cmp
                    after_snap = json.dumps({k: v for k, v in run.items() if k != 'updatedAt'}, sort_keys=True, default=str)
                    if events or after_snap != before_snap:
                        runs_changed = True

                    for ev in events:
                        ev_type = ev.get('type')
                        if ev_type == 'OPEN_LEG':
                            leg = ev.get('leg', {})
                            sar_lot = run.get('lotSize') or _resolve_lot_size(sym)
                            if sar_lot is None or int(sar_lot) < 1:
                                continue
                            sar_lot_int = int(sar_lot)
                            trade_item = {
                                'id': secrets.token_hex(6),
                                'strategy': run.get('strategyName', 'Level SAR'),
                                'symbol': sym,
                                'contract': f"{sym} (SAR leg {leg.get('legNo', 1)})",
                                'instrumentType': 'EQUITY',
                                'side': leg.get('side', 'BUY'),
                                'quantity': leg.get('qty', 1),
                                'lotSize': sar_lot_int,
                                'totalUnits': leg.get('qty', 1) * sar_lot_int,
                                'entryPrice': leg.get('entry'),
                                'entrySpotPrice': leg.get('entry'),
                                'currentPrice': leg.get('entry'),
                                'status': 'OPEN',
                                'mode': 'PAPER',
                                'source': 'SAR ENGINE',
                                'sarRunId': run.get('id'),
                                'legNo': leg.get('legNo'),
                                'tradedValue': round(leg.get('entry', 0) * leg.get('qty', 1), 2),
                                'createdAt': now_ist.strftime('%Y-%m-%d %H:%M:%S'),
                                'note': f"SAR Leg {leg.get('legNo')} {leg.get('side')} entry @ ₹{leg.get('entry')}"
                            }
                            trades.insert(0, trade_item)
                            trades_changed = True

                        elif ev_type == 'CLOSE_LEG':
                            leg = ev.get('leg', {})
                            leg_no = leg.get('legNo')
                            exit_p = ev.get('exitPrice')
                            pnl = ev.get('pnl')
                            reason = ev.get('reason')

                            for t in trades:
                                if t.get('sarRunId') == run.get('id') and t.get('legNo') == leg_no and t.get('status') == 'OPEN':
                                    t['status'] = 'CLOSED'
                                    t['exitPrice'] = exit_p
                                    t['exitSpotPrice'] = exit_p
                                    t['exitAt'] = now_ist.strftime('%Y-%m-%d %H:%M:%S')
                                    t['pnl'] = pnl
                                    entry_p = t.get('entryPrice', 1)
                                    t['pnlPercent'] = round(((exit_p - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0, 2)
                                    if t.get('side') == 'SELL':
                                        t['pnlPercent'] = -t['pnlPercent']
                                    t['exitReason'] = reason
                                    t['note'] = f"Closed at ₹{exit_p} ({reason})"
                                    trades_changed = True
                                    break

                if runs_changed:
                    run_repo.save_all(runs)
                if trades_changed:
                    trade_repo.save_all(trades[:200])

        except Exception as e:
            print(f"[SAR Daemon Error]: {e}\n{traceback.format_exc()}")

        WORKER_STOP_EVENT.wait(2.0)


_TF_CHART_PARAMS = {
    '1m': ('1m', '1d'),
    '3m': ('1m', '1d'),
    '5m': ('5m', '5d'),
    '10m': ('5m', '5d'),
    '15m': ('15m', '5d'),
    '30m': ('30m', '5d'),
    '60m': ('60m', '1mo'),
    '1h': ('60m', '1mo'),
    '1d': ('1d', '1mo'),
}


def _paper_run_comparable_snapshot(run: dict) -> str:
    """Serialize all restart-critical paper worker fields while ignoring wall-clock tick timestamps."""
    snap = {
        k: v for k, v in run.items()
        if k not in ('updatedAt', 'lastEvaluatedAtIst')
    }
    eval_st = snap.get('evalState')
    if isinstance(eval_st, dict) and isinstance(eval_st.get('nodeHistory'), dict):
        snap['evalState'] = {
            **eval_st,
            'nodeHistory': {
                nid: {nk: nv for nk, nv in entry.items() if nk != 'updatedAt'}
                if isinstance(entry, dict) else entry
                for nid, entry in eval_st['nodeHistory'].items()
            },
        }
    return json.dumps(snap, sort_keys=True, default=str)


def run_paper_worker_tick_once(now_ist: Optional[datetime] = None) -> int:
    """Fetch recent candles and step all RUNNING paper worker runs once. Returns stepped count."""
    if kill_switch_state.get('active'):
        return 0

    eval_dt = now_ist or datetime.now(IST)
    stepped = 0
    chart_cache: dict[tuple[str, str], list[dict]] = {}

    with PAPER_WORKER_LOCK:
        runs = paper_worker_repo.list()
        active_runs = [r for r in runs if r.get('status') == 'RUNNING']
        if not active_runs:
            return 0

        changed = False
        for run in active_runs:
            strategy = run.get('strategy') or {}
            inst_cfg = (strategy.get('execution') or {}).get('instrument') or {}
            sym = inst_cfg.get('signalSymbol') or inst_cfg.get('underlying') or (strategy.get('meta') or {}).get('symbol') or 'NIFTY 50'
            primary_tf = run.get('primaryTimeframe') or '5m'
            tfs = {primary_tf}
            for ind in strategy.get('indicators') or []:
                if isinstance(ind, dict) and ind.get('timeframe'):
                    tfs.add(str(ind['timeframe']))

            candles_by_tf: dict[str, list[dict]] = {}
            for tf in tfs:
                interval, rng = _TF_CHART_PARAMS.get(tf, ('5m', '5d'))
                cache_key = (sym, interval)
                if cache_key not in chart_cache:
                    chart = market_service.get_chart(sym, range_period=rng, interval=interval)
                    chart_cache[cache_key] = chart.get('candles') or []
                base_candles = chart_cache[cache_key]
                if tf in ('3m', '10m') and base_candles:
                    candles_by_tf[tf] = resample_candles_to_timeframe(base_candles, interval, tf, eval_dt)
                else:
                    candles_by_tf[tf] = base_candles

            if not (candles_by_tf.get(primary_tf)):
                msg = f'Waiting: no {primary_tf} candles for {sym}'
                if run.get('message') != msg:
                    run['message'] = msg
                    changed = True
                continue

            before_pw = _paper_run_comparable_snapshot(run)
            try:
                step_paper_worker_run(
                    worker_run=run,
                    candles_by_tf=candles_by_tf,
                    eval_time=eval_dt,
                    level_book=level_book,
                )
                stepped += 1
            except (BacktestError, ExitEngineError, LevelError) as exc:
                run['message'] = exc.message
            except Exception as exc:
                run['message'] = f'Paper worker step error: {exc}'
            after_pw = _paper_run_comparable_snapshot(run)
            if after_pw != before_pw:
                changed = True

        if changed:
            paper_worker_repo.save_all(runs)
    return stepped


def paper_background_worker():
    while not WORKER_STOP_EVENT.is_set():
        try:
            run_paper_worker_tick_once()
        except Exception as e:
            print(f"[Paper Worker Daemon Error]: {e}\n{traceback.format_exc()}")
        WORKER_STOP_EVENT.wait(5.0)


def start_background_daemons():
    WORKER_STOP_EVENT.clear()
    if settings.sar_worker_enabled:
        t_sar = threading.Thread(target=sar_background_worker, name='sar-worker', daemon=True)
        t_sar.start()
        _WORKER_THREADS.append(t_sar)
    if settings.paper_worker_enabled:
        t_pw = threading.Thread(target=paper_background_worker, name='paper-worker', daemon=True)
        t_pw.start()
        _WORKER_THREADS.append(t_pw)


def stop_background_daemons():
    WORKER_STOP_EVENT.set()
    for t in list(_WORKER_THREADS):
        t.join(timeout=2.0)
    _WORKER_THREADS.clear()

@app.post('/api/sar/preview')
def sar_preview(payload: SarPreviewPayload):
    s = payload.strategy
    if s.get('strategyType') == 'RULE_BUILDER':
        raise HTTPException(400, detail={'errors': ['Stop-and-Reverse preview only accepts LEVEL_SAR strategies.']})
    cfg = parse_config(s)
    ref_price = payload.referencePrice
    has_live_ref = bool(ref_price and ref_price > 0)
    errors, warnings = validate_sar(s, reference_price=ref_price if has_live_ref else None)

    ladders = {}
    if not has_live_ref:
        ref_price = 1000.0  # illustrative only (candle levels); capital is NOT checked against it

    if cfg["levelSource"] == "MANUAL":
        upper = cfg["manualUpper"]
        lower = cfg["manualLower"]
        if cfg["entryStyle"] == "REVERSAL":
            ladders["SELL"] = build_ladder(cfg, "SELL", upper)
            ladders["BUY"] = build_ladder(cfg, "BUY", lower)
        else:
            ladders["BUY"] = build_ladder(cfg, "BUY", upper)
            ladders["SELL"] = build_ladder(cfg, "SELL", lower)
    else:
        if cfg["entryStyle"] == "REVERSAL":
            ladders["SELL"] = build_ladder(cfg, "SELL", ref_price)
            ladders["BUY"] = build_ladder(cfg, "BUY", ref_price)
        else:
            ladders["BUY"] = build_ladder(cfg, "BUY", ref_price)
            ladders["SELL"] = build_ladder(cfg, "SELL", ref_price)

    worst_case_loss = 0
    blocked_legs = []
    qty = cfg["baseQuantity"]
    for leg_no in range(1, cfg["maxLegs"] + 1):
        leg_loss = cfg["slPoints"] * qty
        cum_loss = worst_case_loss + leg_loss
        if cum_loss > cfg["maxDailyLoss"]:
            blocked_legs.append(leg_no)
        worst_case_loss = cum_loss
        if leg_no < cfg["maxLegs"] and leg_no - 1 < len(cfg["multipliers"]):
            qty = max(1, round(qty * cfg["multipliers"][leg_no - 1]))

    breakdown_entry = (cfg["manualUpper"] if cfg["levelSource"] == "MANUAL" else (ref_price if has_live_ref else None))
    breakdown = risk_breakdown(cfg, breakdown_entry)
    blocked_legs = [r["legNo"] for r in breakdown if r["blocked"]] or blocked_legs

    return {
        "ladders": ladders,
        "breakdown": breakdown,
        "calcNotes": {
            "quantityUnit": "underlying units (not lots)",
            "legLoss": "stop-loss points x leg quantity",
            "exposure": "quantity x entry price (notional, not broker margin)",
            "capitalCheckedAgainstLivePrice": has_live_ref or cfg["levelSource"] == "MANUAL",
        },
        "worstCaseLoss": worst_case_loss,
        "blockedLegs": blocked_legs,
        "errors": errors,
        "warnings": warnings
    }

@app.post('/api/sar/start')
def start_sar_run(payload: StrategyPayload):
    s = payload.strategy
    if s.get('strategyType') != 'LEVEL_SAR':
        raise HTTPException(400, detail={'errors': ['Stop-and-Reverse start only accepts LEVEL_SAR strategies. No order was placed.']})
    errors = validate_strategy(s)
    if errors:
        raise HTTPException(400, detail={'errors': errors})

    sar_instrument = s.get('instrument') or ''
    master_lot = _resolve_lot_size(sar_instrument)
    strat_lot = s.get('lotSize')
    sar_warnings: list[str] = []
    if master_lot is not None:
        sar_lot_size = master_lot
        if strat_lot is not None and int(strat_lot) != int(master_lot):
            sar_warnings.append(
                f"Strategy lotSize ({strat_lot}) differs from contract master ({master_lot}) for {sar_instrument}; using contract master value {master_lot}."
            )
    else:
        sar_lot_size = strat_lot
    if sar_lot_size is None or int(sar_lot_size) < 1:
        raise HTTPException(400, detail={'errors': [f"Cannot start Stop-and-Reverse run: lot size is unknown for '{sar_instrument}' (contract master missing or stale)."]})

    strat_id = s.get('id')
    with SAR_LOCK:
        runs = run_repo.list()
        for r in runs:
            if r.get('strategyId') == strat_id and r.get('status') not in FINAL_STATUSES:
                raise HTTPException(400, detail={'errors': ['A Stop-and-Reverse run for this strategy is already active. Stop it first.']})

        cfg = parse_config(s)
        run = new_run(s, cfg)
        run['lotSize'] = int(sar_lot_size)
        run['warnings'] = sar_warnings
        run['cfg'] = cfg
        run['strategy'] = s
        runs.insert(0, run)
        run_repo.save_all(runs)
        return run

@app.get('/api/sar/runs')
def get_sar_runs():
    with SAR_LOCK:
        runs = run_repo.list()
        result = []
        for r in runs[:20]:
            item = dict(r)
            if 'lastPrice' not in item:
                item['lastPrice'] = item.get('prevPrice')
            result.append(item)
        return result

@app.post('/api/sar/stop')
def stop_sar_run(payload: SarStopPayload):
    from .sar_engine import _close_leg
    with SAR_LOCK:
        runs = run_repo.list()
        matched = None
        for r in runs:
            if r.get('id') == payload.runId:
                matched = r
                break
        if not matched:
            raise HTTPException(404, 'SAR run not found')

        if matched.get('status') in FINAL_STATUSES:
            return matched

        now_ist = datetime.now(IST)
        cfg = matched.get('cfg') or parse_config(matched.get('strategy', {}))

        if matched.get('currentLeg'):
            cmp = matched.get('lastPrice') or matched.get('prevPrice') or matched['currentLeg']['entry']
            _close_leg(matched, cfg, cmp, 'MANUAL')
            trades = trade_repo.list()
            for t in trades:
                if t.get('sarRunId') == matched.get('id') and t.get('status') == 'OPEN':
                    t['status'] = 'CLOSED'
                    t['exitPrice'] = cmp
                    t['exitSpotPrice'] = cmp
                    t['exitAt'] = now_ist.strftime('%Y-%m-%d %H:%M:%S')
                    entry_p = t.get('entryPrice', 1)
                    units = t.get('totalUnits', 1)
                    if t.get('side') == 'BUY':
                        pnl = (cmp - entry_p) * units
                    else:
                        pnl = (entry_p - cmp) * units
                    t['pnl'] = round(pnl, 2)
                    t['pnlPercent'] = round(((cmp - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0, 2)
                    if t.get('side') == 'SELL':
                        t['pnlPercent'] = -t['pnlPercent']
                    t['exitReason'] = 'MANUAL'
                    t['note'] = f'Closed manually at ₹{cmp}'
            trade_repo.save_all(trades)

        matched['status'] = 'STOPPED_MANUAL'
        matched['message'] = 'Stopped manually by user'
        matched['updatedAt'] = now_ist.isoformat()
        run_repo.save_all(runs)
        return matched

# ==================== FC/RC LEVELS ====================

@app.get('/api/levels')
def get_level_sheet(symbol: str, day: str, provider: str = 'MANUAL'):
    try:
        sheet = level_book.get_sheet(provider, symbol, day)
    except LevelError as exc:
        raise HTTPException(400, exc.message)
    if sheet is None:
        return {'symbol': symbol.strip(), 'day': day, 'provider': provider.upper(), 'values': {}}
    return sheet

@app.put('/api/levels')
def save_level_sheet(payload: LevelSheetPayload):
    try:
        return level_book.save_sheet(payload.provider, payload.symbol, payload.day, payload.values)
    except LevelError as exc:
        raise HTTPException(exc.status_code, exc.message)

@app.get('/api/levels/lookup')
def lookup_level(symbol: str, day: str, name: str, provider: str = 'MANUAL'):
    try:
        return level_book.lookup(provider, symbol, day, name).as_dict()
    except FormulaNotProvided as exc:
        raise HTTPException(400, exc.message)
    except LevelError as exc:
        raise HTTPException(exc.status_code, exc.message)
