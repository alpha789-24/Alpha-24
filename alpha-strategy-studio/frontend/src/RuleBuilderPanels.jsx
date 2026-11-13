import React, { useState, useEffect, useMemo, useRef } from 'react';
import {
  ChevronDown, ChevronRight, Plus, Trash2, Copy, ArrowUp, ArrowDown,
  AlertTriangle, CheckCircle2, Play, Pause, Square, RotateCcw, Upload,
  Download, History, Layers, Sliders, ShieldAlert, Sparkles
} from 'lucide-react';
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid
} from 'recharts';
import {
  LEVEL_NAMES,
  TIMEFRAMES,
  PRICE_FIELDS,
  conditionToEnglish,
  makeDefaultLeaf,
  makeDefaultGroup,
  makeDefaultSequence,
  stripCredentialsClient,
  ConditionNodeEditor
} from './RuleBuilderStudio.jsx';
import { getOperationalPollIntervalMs } from './pollingConfig.js';

const API = 'http://127.0.0.1:8000/api';
const WEEKDAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
const INDICATOR_TYPES = [
  'RSI', 'EMA', 'SMA', 'SUPERTREND', 'VWAP', 'MACD', 'ATR', 'CCI', 'BOLLINGER', 'STOCHASTIC', 'ADX'
];
const INDICATOR_DEFAULTS = {
  RSI: { period: 14, smoothingPeriod: 14 },
  EMA: { period: 20 },
  SMA: { period: 20 },
  CCI: { period: 20 },
  ATR: { period: 14 },
  SUPERTREND: { period: 10, multiplier: 3.0 },
  VWAP: {},
  MACD: { fastPeriod: 12, slowPeriod: 26, signalPeriod: 9 },
  BOLLINGER: { period: 20, stdDev: 2.0 },
  STOCHASTIC: { kPeriod: 14, dPeriod: 3 },
  ADX: { period: 14 }
};

export function parseValidationErrorsByPath(errors = []) {
  const map = {};
  (errors || []).forEach(err => {
    const str = String(err || '');
    const colonIdx = str.indexOf(':');
    if (colonIdx > 0) {
      const path = str.slice(0, colonIdx).trim();
      const msg = str.slice(colonIdx + 1).trim();
      if (!map[path]) map[path] = [];
      map[path].push(msg);
    } else {
      if (!map['_root']) map['_root'] = [];
      map['_root'].push(str);
    }
  });
  return map;
}

export function CollapsibleSection({ title, subtitle, badge, defaultOpen = true, children }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rb-collapsible-panel">
      <button
        type="button"
        className="rb-collapsible-head"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <div className="rb-collapsible-title">
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          <div>
            <b>{title}</b>
            {subtitle && <small>{subtitle}</small>}
          </div>
        </div>
        {badge && <span className="rb-section-badge">{badge}</span>}
      </button>
      {open && <div className="rb-collapsible-body">{children}</div>}
    </div>
  );
}

export { DashboardStrategyCards } from './DashboardStrategyCards.jsx';

export function RuleBuilderEditor({
  strategy,
  setStrategy,
  valid,
  onValidate,
  onSave,
  notify,
  chartCandles,
  onReloadStrategies
}) {
  const [samples, setSamples] = useState([]);
  const [versions, setVersions] = useState([]);
  const [showVersions, setShowVersions] = useState(false);

  // Manual FC/RC Level Sheet state
  const [levelDay, setLevelDay] = useState(() => new Date().toISOString().slice(0, 10));
  const [levelValues, setLevelValues] = useState(() => {
    const obj = {};
    LEVEL_NAMES.forEach((n, idx) => { obj[n] = 24000 + idx * 10; });
    return obj;
  });
  const [levelSaving, setLevelSaving] = useState(false);

  // Backtest & Paper Worker state
  const [btResult, setBtResult] = useState(null);
  const [btLoading, setBtLoading] = useState(false);
  const [btSlippage, setBtSlippage] = useState(0.5);
  const [btBrokerage, setBtBrokerage] = useState(20);
  const [btFillMode, setBtFillMode] = useState('NEXT_CANDLE_OPEN');
  const [paperRuns, setPaperRuns] = useState([]);
  const fileInputRef = useRef(null);

  // Phase 7: Broker Capabilities, Go-Live Gates, Order Lifecycle & Kill Switch state
  const [brokerCaps, setBrokerCaps] = useState(null);
  const [liveGates, setLiveGates] = useState(null);
  const [killSwitchState, setKillSwitchState] = useState({ active: false });
  const [brokerOrders, setBrokerOrders] = useState([]);
  const [typedConfirmText, setTypedConfirmText] = useState('');
  const [orderSide, setOrderSide] = useState('BUY');
  const [orderMode, setOrderMode] = useState('PAPER');
  const [orderBroker, setOrderBroker] = useState('IIFL');
  const [orderExchange, setOrderExchange] = useState('NFO');
  const [orderProduct, setOrderProduct] = useState('INTRADAY');
  const [orderTypeVal, setOrderTypeVal] = useState('MARKET');
  const [orderValidity, setOrderValidity] = useState('DAY');
  const [orderAmo, setOrderAmo] = useState(false);
  const [orderLimitPrice, setOrderLimitPrice] = useState(24000);
  const [orderTriggerPrice, setOrderTriggerPrice] = useState(23950);
  const [orderIdempotencyKey, setOrderIdempotencyKey] = useState(() => `ord-${Date.now()}`);

  const errorsByPath = useMemo(() => parseValidationErrorsByPath(valid?.errors || []), [valid]);

  const indicatorLabels = useMemo(() => {
    const m = {};
    (strategy.indicators || []).forEach(ind => { m[ind.id] = ind.label || ind.id; });
    return m;
  }, [strategy.indicators]);

  useEffect(() => {
    fetch(API + '/strategies/samples')
      .then(r => (r.ok ? r.json() : []))
      .then(setSamples)
      .catch(() => {});
    loadPaperWorkerRuns();
  }, []);

  const loadPaperWorkerRuns = async () => {
    try {
      const r = await fetch(`${API}/paper/worker/runs?strategyId=${encodeURIComponent(strategy.id)}`);
      if (r.ok) setPaperRuns(await r.json());
    } catch {}
  };

  useEffect(() => {
    loadPaperWorkerRuns();
  }, [strategy.id]);

  const loadVersions = async () => {
    try {
      const r = await fetch(`${API}/strategies/${encodeURIComponent(strategy.id)}/versions`);
      if (r.ok) {
        setVersions(await r.json());
        setShowVersions(true);
      } else {
        notify('Save the strategy first to view version history.');
      }
    } catch {
      notify('Could not fetch version history.');
    }
  };

  const restoreVersion = async (versionId) => {
    try {
      const r = await fetch(`${API}/strategies/${encodeURIComponent(strategy.id)}/restore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ versionId })
      });
      if (!r.ok) throw Error('Restore failed');
      const restored = await r.json();
      setStrategy(restored);
      setShowVersions(false);
      notify(`Restored version ${versionId.slice(0, 8)}`);
      if (onReloadStrategies) onReloadStrategies();
    } catch (e) {
      notify(e.message);
    }
  };

  const loadLevelSheet = async () => {
    const sym = strategy.execution?.instrument?.underlying || strategy.meta?.symbol || 'NIFTY 50';
    try {
      const r = await fetch(`${API}/levels?symbol=${encodeURIComponent(sym)}&day=${encodeURIComponent(levelDay)}&provider=MANUAL`);
      if (r.ok) {
        const d = await r.json();
        if (d.values && Object.keys(d.values).length > 0) {
          setLevelValues(d.values);
          notify(`Loaded manual FC/RC levels for ${sym} on ${levelDay}`);
        } else {
          notify(`No saved manual sheet for ${sym} on ${levelDay}; showing template numbers.`);
        }
      }
    } catch {}
  };

  const saveLevelSheet = async () => {
    const sym = strategy.execution?.instrument?.underlying || strategy.meta?.symbol || 'NIFTY 50';
    setLevelSaving(true);
    try {
      const numericValues = {};
      LEVEL_NAMES.forEach(k => { numericValues[k] = Number(levelValues[k] ?? 0); });
      const r = await fetch(API + '/levels', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym, day: levelDay, provider: 'MANUAL', values: numericValues })
      });
      if (!r.ok) {
        const err = await r.json();
        throw Error(err.detail || 'Failed to save level sheet');
      }
      notify(`Saved all 16 manual FC/RC levels for ${sym} on ${levelDay}`);
    } catch (e) {
      notify(e.message);
    } finally {
      setLevelSaving(false);
    }
  };

  const updateMetaName = (name) => {
    setStrategy(prev => ({
      ...prev,
      name,
      meta: { ...(prev.meta || {}), id: prev.id, name }
    }));
  };

  const updateSymbol = (symbol) => {
    setStrategy(prev => ({
      ...prev,
      meta: { ...(prev.meta || {}), symbol },
      execution: {
        ...(prev.execution || {}),
        instrument: {
          ...(prev.execution?.instrument || {
            signalSource: 'INDEX_SPOT',
            instrumentType: 'INDEX_OPTIONS',
            optionType: 'CE',
            strike: 'ATM',
            expiry: 'CURRENT_WEEK',
            lotSize: null
          }),
          underlying: symbol,
          lotSize: null
        }
      }
    }));
    fetch(`${API}/contracts/lot-size?underlying=${encodeURIComponent(symbol)}&segment=NSEFO`)
      .then(res => (res.ok ? res.json() : null))
      .then(data => {
        if (!data?.lotSize) return;
        setStrategy(prev => ({
          ...prev,
          execution: {
            ...(prev.execution || {}),
            instrument: {
              ...(prev.execution?.instrument || {}),
              lotSize: Number(data.lotSize)
            }
          }
        }));
      })
      .catch(() => {});
  };

  // Indicators CRUD
  const addIndicator = (type = 'RSI') => {
    const defs = INDICATOR_DEFAULTS[type] || {};
    const id = `${type.toLowerCase()}-${(strategy.indicators?.length || 0) + 1}`;
    const newInd = {
      id,
      enabled: true,
      order: (strategy.indicators?.length || 0) + 1,
      label: `${type} (${defs.period || defs.fastPeriod || 'Intraday'})`,
      type,
      timeframe: '5m',
      source: 'CLOSE',
      period: defs.period ?? null,
      smoothingPeriod: defs.smoothingPeriod ?? null,
      multiplier: defs.multiplier ?? null,
      fastPeriod: defs.fastPeriod ?? null,
      slowPeriod: defs.slowPeriod ?? null,
      signalPeriod: defs.signalPeriod ?? null,
      stdDev: defs.stdDev ?? null,
      kPeriod: defs.kPeriod ?? null,
      dPeriod: defs.dPeriod ?? null
    };
    setStrategy(prev => ({ ...prev, indicators: [...(prev.indicators || []), newInd] }));
  };

  const removeIndicator = (idx) => {
    setStrategy(prev => ({
      ...prev,
      indicators: (prev.indicators || [])
        .filter((_, i) => i !== idx)
        .map((it, i) => ({ ...it, order: i + 1 }))
    }));
  };

  // Entry & Exit Rules CRUD
  const updateRule = (bucket, idx, newRule) => {
    setStrategy(prev => {
      const list = [...(prev.rules?.[bucket] || [])];
      list[idx] = newRule;
      return { ...prev, rules: { ...(prev.rules || {}), [bucket]: list } };
    });
  };

  const addRule = (bucket, side = 'BUY', asSequence = false) => {
    setStrategy(prev => {
      const list = [...(prev.rules?.[bucket] || [])];
      const newRule = {
        id: `${bucket}-${Math.random().toString(36).slice(2, 7)}`,
        enabled: true,
        order: list.length + 1,
        label: `${side} ${bucket === 'entry' ? 'Entry' : 'Exit'} Rule #${list.length + 1}`,
        side,
        condition: asSequence ? makeDefaultSequence() : makeDefaultGroup()
      };
      return { ...prev, rules: { ...(prev.rules || {}), [bucket]: [...list, newRule] } };
    });
  };

  const duplicateRule = (bucket, idx) => {
    setStrategy(prev => {
      const list = [...(prev.rules?.[bucket] || [])];
      const copyRule = structuredClone(list[idx]);
      copyRule.id = `${bucket}-${Math.random().toString(36).slice(2, 7)}`;
      copyRule.label = `${copyRule.label} (Copy)`;
      const reId = (n) => {
        if (!n) return;
        n.id = `${n.type}-${Math.random().toString(36).slice(2, 7)}`;
        (n.children || []).forEach(reId);
        (n.steps || []).forEach(reId);
      };
      reId(copyRule.condition);
      list.splice(idx + 1, 0, copyRule);
      return {
        ...prev,
        rules: {
          ...(prev.rules || {}),
          [bucket]: list.map((r, i) => ({ ...r, order: i + 1 }))
        }
      };
    });
  };

  const moveRule = (bucket, idx, dir) => {
    setStrategy(prev => {
      const list = [...(prev.rules?.[bucket] || [])];
      const target = idx + dir;
      if (target < 0 || target >= list.length) return prev;
      const [picked] = list.splice(idx, 1);
      list.splice(target, 0, picked);
      return {
        ...prev,
        rules: {
          ...(prev.rules || {}),
          [bucket]: list.map((r, i) => ({ ...r, order: i + 1 }))
        }
      };
    });
  };

  const removeRule = (bucket, idx) => {
    setStrategy(prev => {
      const list = (prev.rules?.[bucket] || [])
        .filter((_, i) => i !== idx)
        .map((r, i) => ({ ...r, order: i + 1 }));
      return { ...prev, rules: { ...(prev.rules || {}), [bucket]: list } };
    });
  };

  // Trailing SL CRUD
  const addTrailingStep = () => {
    setStrategy(prev => {
      const list = [...(prev.trailing || [])];
      const step = {
        id: `trail-${Math.random().toString(36).slice(2, 7)}`,
        enabled: true,
        order: list.length + 1,
        label: `Trailing Step #${list.length + 1}`,
        condition: makeDefaultLeaf('trail-cond'),
        moveSlTo: { kind: 'level', level: 'RC1L', price: null, points: null, indicatorId: null }
      };
      return { ...prev, trailing: [...list, step] };
    });
  };

  // Export & Import with credential stripping
  const handleExportCleanJson = async () => {
    try {
      const r = await fetch(API + '/strategies/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy })
      });
      const data = r.ok ? await r.json() : { strategy: stripCredentialsClient(strategy) };
      const blob = new Blob([JSON.stringify(data.strategy, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${(strategy.name || 'strategy').toLowerCase().replace(/[^a-z0-9]+/g, '-')}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
      notify('Exported clean strategy JSON (credential fields stripped)');
    } catch (e) {
      notify('Export failed: ' + e.message);
    }
  };

  const handleImportJsonFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const raw = JSON.parse(text);
      const cleaned = stripCredentialsClient(raw.strategy || raw);
      const r = await fetch(API + '/strategies/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy: cleaned })
      });
      if (!r.ok) {
        const err = await r.json();
        throw Error((err.detail?.errors || [err.detail]).join(', '));
      }
      const res = await r.json();
      setStrategy(res.strategy);
      notify(`Imported "${res.strategy.name}" (${res.strippedCredentialFields?.length || 0} secret keys stripped)`);
      if (onReloadStrategies) onReloadStrategies();
    } catch (err) {
      notify('Import error: ' + err.message);
    } finally {
      e.target.value = '';
    }
  };

  // Run Backtest
  const handleRunBacktest = async () => {
    setBtLoading(true);
    try {
      // Use live chart candles if available, or generate a deterministic intraday sample series around levelValues
      let candles = (chartCandles || []).map((c, idx) => ({
        timestamp: c.timestamp || (1767584700 + idx * 300),
        date: c.date || levelDay,
        time: c.time || '09:15',
        open: Number(c.open),
        high: Number(c.high),
        low: Number(c.low),
        close: Number(c.close),
        volume: Number(c.volume || 1000),
        closed: true
      }));
      if (candles.length < 5) {
        const rc1l = Number(levelValues.RC1L || 23980);
        const rc1h = Number(levelValues.RC1H || 24020);
        const fc1h = Number(levelValues.FC1H || 24050);
        candles = [
          { date: levelDay, time: '09:15', open: rc1l - 5, high: rc1l + 4, low: rc1l - 12, close: rc1l - 2, volume: 1500, closed: true },
          { date: levelDay, time: '09:20', open: rc1l - 2, high: rc1h + 12, low: rc1l - 4, close: rc1h + 8, volume: 2200, closed: true },
          { date: levelDay, time: '09:25', open: rc1h + 8, high: fc1h + 15, low: rc1h + 2, close: fc1h + 10, volume: 2800, closed: true },
          { date: levelDay, time: '09:30', open: fc1h + 10, high: fc1h + 35, low: fc1h + 5, close: fc1h + 30, volume: 3100, closed: true },
          { date: levelDay, time: '09:35', open: fc1h + 30, high: fc1h + 45, low: fc1h + 18, close: fc1h + 40, volume: 1900, closed: true }
        ];
      }

      const r = await fetch(API + '/backtest/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy,
          candlesByTimeframe: { '5m': candles },
          primaryTimeframe: '5m',
          manualLevelsByDay: { [candles[0]?.date || levelDay]: levelValues },
          executionFillMode: btFillMode,
          slippagePoints: Number(btSlippage),
          brokeragePerOrder: Number(btBrokerage),
          persistResult: true
        })
      });
      const d = await r.json();
      if (!r.ok) {
        throw Error((d.detail?.errors || [d.detail]).join(', '));
      }
      setBtResult(d);
      notify(`Backtest completed: ${d.metrics.totalTrades} trades, Net P&L ₹${d.metrics.netProfit}`);
    } catch (e) {
      notify('Backtest error: ' + e.message);
    } finally {
      setBtLoading(false);
    }
  };

  // Paper Worker actions
  const startWorker = async () => {
    if (strategy.execution?.instrument?.lotSize == null) {
      notify('Cannot start paper worker: lot size is unknown (contract master unavailable)');
      return;
    }
    try {
      const r = await fetch(API + '/paper/worker/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy,
          primaryTimeframe: '5m',
          slippagePoints: Number(btSlippage),
          brokeragePerOrder: Number(btBrokerage)
        })
      });
      const d = await r.json();
      if (!r.ok) throw Error((d.detail?.errors || [d.detail]).join(', '));
      notify('Paper trading worker started!');
      loadPaperWorkerRuns();
    } catch (e) {
      notify(e.message);
    }
  };

  const controlWorker = async (runId, action) => {
    try {
      const r = await fetch(`${API}/paper/worker/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ runId })
      });
      if (!r.ok) throw Error(`Failed to ${action} worker`);
      notify(`Paper worker ${action}d`);
      loadPaperWorkerRuns();
    } catch (e) {
      notify(e.message);
    }
  };

  const loadBrokerPhase7State = async () => {
    try {
      const [capRes, ksRes, ordRes, gateRes] = await Promise.all([
        fetch(`${API}/broker/capabilities`),
        fetch(`${API}/broker/kill-switch`),
        fetch(`${API}/broker/orders?strategyId=${encodeURIComponent(strategy.id)}`),
        fetch(`${API}/broker/live-gates`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ strategy })
        })
      ]);
      if (capRes.ok) setBrokerCaps(await capRes.json());
      if (ksRes.ok) setKillSwitchState(await ksRes.json());
      if (ordRes.ok) setBrokerOrders(await ordRes.json());
      if (gateRes.ok) setLiveGates(await gateRes.json());
    } catch {}
  };

  useEffect(() => {
    loadBrokerPhase7State();
    const id = setInterval(() => {
      loadBrokerPhase7State();
      loadPaperWorkerRuns();
    }, getOperationalPollIntervalMs());
    return () => clearInterval(id);
  }, [strategy]);

  const handleApproveLive = async () => {
    try {
      const r = await fetch(`${API}/broker/approve-live`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy, confirmationText: typedConfirmText })
      });
      const d = await r.json();
      if (!r.ok) {
        const msg = d.detail?.errors?.join(' | ') || d.detail || 'Live approval rejected';
        throw Error(msg);
      }
      notify(`Live trading approved for version ${d.approval?.versionHash?.slice(0, 10)}`);
      loadBrokerPhase7State();
    } catch (e) {
      notify('Live approval failed: ' + e.message);
    }
  };

  const handlePlaceBrokerOrder = async () => {
    const liveExecutionEnabled = Boolean(brokerCaps?.liveExecutionEnabled && liveGates?.liveExecutionEnabled);
    if (orderMode === 'LIVE' && !liveExecutionEnabled) {
      notify('Order REJECTED: Live execution not connected');
      return;
    }
    try {
      const inst = strategy.execution?.instrument || {};
      const r = await fetch(`${API}/broker/orders`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy,
          side: orderSide,
          idempotencyKey: orderIdempotencyKey,
          referencePrice: Number(orderLimitPrice || 24000),
          executionSettings: {
            broker: orderBroker,
            exchange: orderExchange,
            symbol: inst.underlying || strategy.meta?.symbol || 'NIFTY 50',
            instrument: inst.instrumentType || 'INDEX_OPTIONS',
            expiry: inst.expiry || 'CURRENT_WEEK',
            strike: inst.strike || 'ATM',
            optionType: inst.optionType || 'CE',
            quantity: (strategy.targets || []).filter(t => t.enabled).reduce((a, b) => a + Number(b.quantity || 0), 0) || 1,
            lotSize: inst.lotSize != null ? Number(inst.lotSize) : null,
            product: orderProduct,
            orderType: orderTypeVal,
            validity: orderValidity,
            amo: Boolean(orderAmo),
            mode: orderMode,
            limitPrice: ['LIMIT', 'SL_LIMIT'].includes(orderTypeVal) ? Number(orderLimitPrice) : null,
            triggerPrice: ['SL_LIMIT', 'SL_MARKET'].includes(orderTypeVal) ? Number(orderTriggerPrice) : null
          }
        })
      });
      const d = await r.json();
      if (!r.ok) throw Error(d.detail || 'Order submission error');
      if (d.duplicateBlocked) {
        notify(`Duplicate order blocked by idempotencyKey (${d.idempotencyKey}) — returning existing order ${d.id.slice(0, 8)}`);
      } else if (d.status === 'REJECTED') {
        notify(`Order REJECTED: ${d.rejectionReason}`);
      } else {
        notify(`Order ${d.status}: ${d.side} ${d.quantity} lot(s) ${d.symbol} (${d.mode})`);
      }
      loadBrokerPhase7State();
    } catch (e) {
      notify('Order error: ' + e.message);
    }
  };

  const handleCancelBrokerOrder = async (orderId) => {
    try {
      const r = await fetch(`${API}/broker/orders/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ orderId, reason: 'Cancelled by operator from Strategy Studio' })
      });
      const d = await r.json();
      if (!r.ok) throw Error(d.detail || 'Cancel failed');
      notify(`Order ${d.id.slice(0, 8)} cancelled`);
      loadBrokerPhase7State();
    } catch (e) {
      notify(e.message);
    }
  };

  const handleTriggerKillSwitch = async () => {
    try {
      const r = await fetch(`${API}/broker/kill-switch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'Operator pressed EMERGENCY KILL SWITCH in Strategy Builder' })
      });
      const d = await r.json();
      if (!r.ok) throw Error('Kill switch failed');
      setKillSwitchState(d.killSwitch || d);
      notify(`EMERGENCY KILL SWITCH ARMED: Stopped ${d.stoppedPaperWorkers || 0} paper workers, cancelled ${d.cancelledOrders || 0} open orders.`);
      loadPaperWorkerRuns();
      loadBrokerPhase7State();
      if (onReloadStrategies) onReloadStrategies();
    } catch (e) {
      notify(e.message);
    }
  };

  const handleResetKillSwitch = async () => {
    try {
      const r = await fetch(`${API}/broker/kill-switch/reset`, { method: 'POST' });
      const d = await r.json();
      if (r.ok) {
        setKillSwitchState(d.killSwitch || d);
        notify('Emergency Kill Switch reset. Live trading approvals remain revoked until re-confirmed.');
        loadBrokerPhase7State();
      }
    } catch (e) {
      notify(e.message);
    }
  };

  return (
    <div className="rb-studio-container">
      {/* Top Toolbar: Sample Loader, Import/Export, Version History */}
      <div className="rb-toolbar-bar">
        <div className="rb-toolbar-left">
          <span className="rb-mini-label">Load Sample Preset:</span>
          <select
            className="rb-sample-select"
            defaultValue=""
            onChange={e => {
              const found = samples.find(s => s.id === e.target.value);
              if (found) {
                setStrategy(structuredClone(found));
                notify(`Loaded sample: ${found.name}`);
              }
            }}
          >
            <option value="">-- Choose Sample Strategy (Phases 2–4) --</option>
            {samples.map(s => (
              <option key={s.id} value={s.id}>
                {s.name} {s.reentry?.unresolvedConflict ? '⚠️ [Unresolved Conflict]' : ''}
              </option>
            ))}
          </select>
        </div>

        <div className="rb-toolbar-right">
          <input
            ref={fileInputRef}
            type="file"
            accept=".json,application/json"
            style={{ display: 'none' }}
            onChange={handleImportJsonFile}
          />
          <button type="button" className="outline small" onClick={() => fileInputRef.current?.click()}>
            <Upload size={14} /> Import JSON
          </button>
          <button type="button" className="outline small" onClick={handleExportCleanJson}>
            <Download size={14} /> Export JSON (Clean)
          </button>
          <button type="button" className="outline small" onClick={loadVersions}>
            <History size={14} /> Version History
          </button>
        </div>
      </div>

      {/* Unresolved Conflict Warning Banner (e.g. SELL Re-entry Example 2) */}
      {(strategy.reentry?.unresolvedConflict || (strategy.rules?.entry || []).some(r => r.unresolvedConflict)) && (
        <div className="rb-conflict-banner" role="alert">
          <ShieldAlert size={20} />
          <div>
            <b>Unresolved Specification Conflict Flagged (Trading Activation Blocked)</b>
            <p>
              {strategy.reentry?.conflictNote ||
                'SELL Re-entry Example 2 lists "enter BUY with SL RC3L" under SELL Re-entry. The original rule text is preserved and blocked from activation until you confirm whether BUY or SELL is intended.'}
            </p>
          </div>
        </div>
      )}

      {/* Version History Modal */}
      {showVersions && (
        <div className="rb-modal-backdrop" onClick={() => setShowVersions(false)}>
          <div className="rb-modal" onClick={e => e.stopPropagation()}>
            <div className="rb-modal-head">
              <h3>Strategy Version History ({versions.length})</h3>
              <button type="button" onClick={() => setShowVersions(false)}>✕</button>
            </div>
            <div className="rb-modal-body">
              {versions.length === 0 ? (
                <p>No saved versions yet.</p>
              ) : (
                versions.map(v => (
                  <div key={v.versionId} className="rb-version-row">
                    <div>
                      <b>{v.diffSummary}</b>
                      <small>{v.savedAt} · Actor: {v.actor}</small>
                    </div>
                    <button type="button" className="outline small" onClick={() => restoreVersion(v.versionId)}>
                      <RotateCcw size={13} /> Restore
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      )}

      {/* 1. Core & Execution Instrument Separation */}
      <CollapsibleSection
        title="01 · Core Strategy & Signal vs Execution Instrument"
        subtitle="Evaluate signals on index spot candles while executing orders and P&L on options/futures contracts"
        badge={strategy.meta?.status || 'DRAFT'}
      >
        <div className="form-grid">
          <label className="field">
            <span>Strategy Name</span>
            <input value={strategy.name || ''} onChange={e => updateMetaName(e.target.value)} />
          </label>
          <label className="field">
            <span>Underlying Index / Symbol</span>
            <select
              value={strategy.execution?.instrument?.underlying || strategy.meta?.symbol || 'NIFTY 50'}
              onChange={e => updateSymbol(e.target.value)}
            >
              {['NIFTY 50', 'BANKNIFTY', 'FINNIFTY', 'SENSEX', 'RELIANCE', 'TCS', 'HDFCBANK', 'SBIN'].map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Signal Calculation Source</span>
            <select
              value={strategy.execution?.instrument?.signalSource || 'INDEX_SPOT'}
              onChange={e => setStrategy(prev => ({
                ...prev,
                execution: {
                  ...prev.execution,
                  instrument: { ...(prev.execution?.instrument || {}), signalSource: e.target.value }
                }
              }))}
            >
              <option value="INDEX_SPOT">INDEX_SPOT (Underlying Spot Candles)</option>
              <option value="TRADED_CONTRACT">TRADED_CONTRACT (Option/Future Candles)</option>
            </select>
          </label>
          <label className="field">
            <span>Execution Instrument Type</span>
            <select
              value={strategy.execution?.instrument?.instrumentType || 'INDEX_OPTIONS'}
              onChange={e => setStrategy(prev => ({
                ...prev,
                execution: {
                  ...prev.execution,
                  instrument: { ...(prev.execution?.instrument || {}), instrumentType: e.target.value }
                }
              }))}
            >
              <option value="INDEX_OPTIONS">Index Options (CE / PE)</option>
              <option value="INDEX_FUTURES">Index Futures</option>
              <option value="INDEX_SPOT">Index Spot (Paper)</option>
              <option value="EQUITY">Equity</option>
            </select>
          </label>
          {strategy.execution?.instrument?.instrumentType === 'INDEX_OPTIONS' && (
            <>
              <label className="field">
                <span>Option Type (CE / PE)</span>
                <select
                  value={strategy.execution?.instrument?.optionType || 'CE'}
                  onChange={e => setStrategy(prev => ({
                    ...prev,
                    execution: {
                      ...prev.execution,
                      instrument: { ...(prev.execution?.instrument || {}), optionType: e.target.value }
                    }
                  }))}
                >
                  <option value="CE">CALL (CE)</option>
                  <option value="PE">PUT (PE)</option>
                </select>
              </label>
              <label className="field">
                <span>Strike Selection</span>
                <select
                  value={strategy.execution?.instrument?.strike || 'ATM'}
                  onChange={e => setStrategy(prev => ({
                    ...prev,
                    execution: {
                      ...prev.execution,
                      instrument: { ...(prev.execution?.instrument || {}), strike: e.target.value }
                    }
                  }))}
                >
                  <option value="ATM">ATM</option>
                  <option value="ITM 1">ITM 1</option>
                  <option value="ITM 2">ITM 2</option>
                  <option value="OTM 1">OTM 1</option>
                  <option value="OTM 2">OTM 2</option>
                </select>
              </label>
            </>
          )}
          <label className="field">
            <span>Position Quantity (Lots)</span>
            <input
              type="number"
              min="1"
              value={strategy.execution?.quantity ?? 1}
              onChange={e => {
                const qty = Math.max(1, parseInt(e.target.value, 10) || 1);
                setStrategy(prev => ({
                  ...prev,
                  execution: { ...(prev.execution || {}), quantity: qty },
                  risk: { ...(prev.risk || {}), maxQuantity: Math.max(prev.risk?.maxQuantity || 1, qty) }
                }));
              }}
            />
          </label>
          <label className="field">
            <span>Intrabar SL/Target Conflict Resolution</span>
            <select
              value={strategy.execution?.intrabarConflictOrder || 'SL_FIRST'}
              onChange={e => setStrategy(prev => ({
                ...prev,
                execution: { ...(prev.execution || {}), intrabarConflictOrder: e.target.value }
              }))}
            >
              <option value="SL_FIRST">SL_FIRST (Conservative Default)</option>
              <option value="TARGET_FIRST">TARGET_FIRST</option>
              <option value="LOWER_TIMEFRAME">LOWER_TIMEFRAME (1m sub-candles)</option>
            </select>
          </label>
        </div>
      </CollapsibleSection>

      {/* 2. IST Session & Holiday Calendar */}
      <CollapsibleSection
        title="02 · IST Session & Exchange Holidays"
        subtitle="All evaluations run in Asia/Kolkata (IST) and respect configured trading days and holidays"
        badge={`${strategy.session?.startTime || '09:15'} – ${strategy.session?.endTime || '15:30'} IST`}
        defaultOpen={false}
      >
        <div className="form-grid">
          <label className="field">
            <span>Session Start (IST)</span>
            <input
              type="time"
              value={strategy.session?.startTime || '09:15'}
              onChange={e => setStrategy(prev => ({
                ...prev,
                session: { ...(prev.session || {}), startTime: e.target.value }
              }))}
            />
          </label>
          <label className="field">
            <span>Session End (IST)</span>
            <input
              type="time"
              value={strategy.session?.endTime || '15:30'}
              onChange={e => setStrategy(prev => ({
                ...prev,
                session: { ...(prev.session || {}), endTime: e.target.value }
              }))}
            />
          </label>
          <label className="field">
            <span>Holiday Calendar Mode</span>
            <select
              value={strategy.session?.holidayCalendar || 'NONE'}
              onChange={e => setStrategy(prev => ({
                ...prev,
                session: {
                  ...(prev.session || {}),
                  holidayCalendar: e.target.value,
                  holidays: e.target.value === 'NONE' ? [] : (prev.session?.holidays || ['2026-01-26'])
                }
              }))}
            >
              <option value="NONE">NONE (No holiday list)</option>
              <option value="EXPLICIT">EXPLICIT (YYYY-MM-DD list)</option>
            </select>
          </label>
          {strategy.session?.holidayCalendar === 'EXPLICIT' && (
            <label className="field">
              <span>Explicit Holidays (comma-separated YYYY-MM-DD)</span>
              <input
                value={(strategy.session?.holidays || []).join(', ')}
                placeholder="2026-01-26, 2026-08-15"
                onChange={e => {
                  const list = e.target.value.split(',').map(s => s.trim()).filter(Boolean);
                  setStrategy(prev => ({
                    ...prev,
                    session: { ...(prev.session || {}), holidays: list }
                  }));
                }}
              />
            </label>
          )}
        </div>
        <div style={{ marginTop: 12 }}>
          <span className="rb-mini-label">Active Trading Days:</span>
          <div className="rb-day-pills">
            {WEEKDAYS.map(d => {
              const active = (strategy.session?.tradingDays || []).includes(d);
              return (
                <button
                  key={d}
                  type="button"
                  className={`rb-day-pill ${active ? 'active' : ''}`}
                  onClick={() => {
                    const cur = strategy.session?.tradingDays || [];
                    const next = active ? cur.filter(x => x !== d) : [...cur, d];
                    setStrategy(prev => ({
                      ...prev,
                      session: { ...(prev.session || {}), tradingDays: next }
                    }));
                  }}
                >
                  {d}
                </button>
              );
            })}
          </div>
        </div>
      </CollapsibleSection>

      {/* 3. Technical Indicators & Manual FC/RC Level Sheet */}
      <CollapsibleSection
        title="03 · Technical Indicators & Manual FC/RC Level Sheet"
        subtitle="Configure multi-timeframe indicators (with warm-up protection) and daily manual FC1–FC3 / RC1–RC5 levels"
        badge={`${(strategy.indicators || []).length} Indicators · Manual Levels`}
        defaultOpen={false}
      >
        <div className="rb-subpanel">
          <div className="rb-subpanel-head">
            <b>Configured Indicators</b>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {INDICATOR_TYPES.map(t => (
                <button key={t} type="button" className="rb-btn-mini" onClick={() => addIndicator(t)}>
                  + {t}
                </button>
              ))}
            </div>
          </div>

          {(strategy.indicators || []).length === 0 ? (
            <div className="rb-empty-hint">No indicators added. Click a button above to add RSI, EMA, Supertrend, VWAP, MACD, etc.</div>
          ) : (
            <div className="rb-indicators-list">
              {(strategy.indicators || []).map((ind, idx) => (
                <div key={ind.id} className="rb-indicator-row">
                  <span className="rb-badge-type">{ind.type}</span>
                  <input
                    value={ind.label}
                    onChange={e => {
                      const list = [...strategy.indicators];
                      list[idx] = { ...ind, label: e.target.value };
                      setStrategy(prev => ({ ...prev, indicators: list }));
                    }}
                  />
                  <select
                    value={ind.timeframe}
                    onChange={e => {
                      const list = [...strategy.indicators];
                      list[idx] = { ...ind, timeframe: e.target.value };
                      setStrategy(prev => ({ ...prev, indicators: list }));
                    }}
                  >
                    {TIMEFRAMES.map(tf => <option key={tf} value={tf}>{tf}</option>)}
                  </select>
                  {ind.period !== null && ind.period !== undefined && (
                    <label className="rb-inline-param">
                      <span>Period:</span>
                      <input
                        type="number"
                        min="1"
                        value={ind.period}
                        onChange={e => {
                          const list = [...strategy.indicators];
                          list[idx] = { ...ind, period: Math.max(1, parseInt(e.target.value, 10) || 1) };
                          setStrategy(prev => ({ ...prev, indicators: list }));
                        }}
                      />
                    </label>
                  )}
                  {ind.smoothingPeriod !== null && ind.smoothingPeriod !== undefined && (
                    <label className="rb-inline-param">
                      <span>Smoothing:</span>
                      <input
                        type="number"
                        min="1"
                        value={ind.smoothingPeriod}
                        onChange={e => {
                          const list = [...strategy.indicators];
                          list[idx] = { ...ind, smoothingPeriod: Math.max(1, parseInt(e.target.value, 10) || 1) };
                          setStrategy(prev => ({ ...prev, indicators: list }));
                        }}
                      />
                    </label>
                  )}
                  {ind.multiplier !== null && ind.multiplier !== undefined && (
                    <label className="rb-inline-param">
                      <span>Mult:</span>
                      <input
                        type="number"
                        step="0.1"
                        min="0.1"
                        value={ind.multiplier}
                        onChange={e => {
                          const list = [...strategy.indicators];
                          list[idx] = { ...ind, multiplier: parseFloat(e.target.value) || 3.0 };
                          setStrategy(prev => ({ ...prev, indicators: list }));
                        }}
                      />
                    </label>
                  )}
                  <button type="button" className="remove-btn" onClick={() => removeIndicator(idx)}>
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Manual FC/RC Daily Level Sheet Editor */}
        <div className="rb-subpanel" style={{ marginTop: 14 }}>
          <div className="rb-subpanel-head">
            <div>
              <b>Manual FC/RC Daily Level Sheet (FC1H/L–FC3H/L & RC1H/L–RC5H/L)</b>
              <small style={{ display: 'block', color: 'var(--text-muted)' }}>
                FORMULA provider is explicitly blocked until mathematical definitions are supplied.
              </small>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <input type="date" value={levelDay} onChange={e => setLevelDay(e.target.value)} />
              <button type="button" className="outline small" onClick={loadLevelSheet}>Load Day</button>
              <button type="button" className="primary small" disabled={levelSaving} onClick={saveLevelSheet}>
                {levelSaving ? 'Saving...' : 'Save 16 Levels'}
              </button>
            </div>
          </div>

          <div className="rb-levels-grid">
            {LEVEL_NAMES.map(lvl => (
              <label key={lvl} className="rb-level-cell">
                <span>{lvl}</span>
                <input
                  type="number"
                  step="any"
                  value={levelValues[lvl] ?? 0}
                  onChange={e => setLevelValues(prev => ({ ...prev, [lvl]: parseFloat(e.target.value) || 0 }))}
                />
              </label>
            ))}
          </div>
        </div>
      </CollapsibleSection>

      {/* 4. Visual Entry & Exit Rule Blocks with Plain-English Preview */}
      <CollapsibleSection
        title="04 · Visual Entry & Exit Rules (Drag-and-Drop Blocks + Plain-English Preview)"
        subtitle="Green = BUY, Red = SELL, Amber = Warnings. Reorder via drag-and-drop or Up/Down buttons."
        badge={`${(strategy.rules?.entry || []).length} Entry · ${(strategy.rules?.exit || []).length} Exit`}
        defaultOpen={true}
      >
        {['entry', 'exit'].map(bucket => {
          const rules = strategy.rules?.[bucket] || [];
          return (
            <div key={bucket} className="rb-rules-bucket">
              <div className="rb-bucket-header">
                <h4>{bucket === 'entry' ? 'ENTRY RULES' : 'OPTIONAL EXIT RULES'}</h4>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button type="button" className="rb-btn-buy" onClick={() => addRule(bucket, 'BUY', false)}>
                    + BUY Rule Group
                  </button>
                  <button type="button" className="rb-btn-sell" onClick={() => addRule(bucket, 'SELL', false)}>
                    + SELL Rule Group
                  </button>
                  <button type="button" className="rb-btn-seq" onClick={() => addRule(bucket, 'BUY', true)}>
                    + Sequential Rule (Step 1 ➔ Step 2)
                  </button>
                </div>
              </div>

              {rules.map((rule, rIdx) => {
                const rulePath = `rules.${bucket}[${rIdx}]`;
                const plainEnglish = `${rule.side} when ${conditionToEnglish(rule.condition, indicatorLabels)}`;
                const ruleErrors = errorsByPath[rulePath] || [];

                return (
                  <div
                    key={rule.id || rIdx}
                    className={`rb-rule-card ${rule.side === 'BUY' ? 'side-buy' : 'side-sell'} ${rule.unresolvedConflict ? 'has-conflict' : ''}`}
                  >
                    <div className="rb-rule-card-top">
                      <select
                        className={`rb-side-pill ${rule.side === 'BUY' ? 'buy' : 'sell'}`}
                        value={rule.side}
                        onChange={e => updateRule(bucket, rIdx, { ...rule, side: e.target.value })}
                      >
                        <option value="BUY">BUY</option>
                        <option value="SELL">SELL</option>
                      </select>

                      <input
                        className="rb-rule-title-input"
                        value={rule.label || ''}
                        onChange={e => updateRule(bucket, rIdx, { ...rule, label: e.target.value })}
                      />

                      <div className="rb-child-actions">
                        <label className="rb-enable-toggle">
                          <input
                            type="checkbox"
                            checked={rule.enabled !== false}
                            onChange={e => updateRule(bucket, rIdx, { ...rule, enabled: e.target.checked })}
                          />
                          <span>{rule.enabled !== false ? 'Active' : 'Disabled'}</span>
                        </label>
                        <button type="button" disabled={rIdx === 0} onClick={() => moveRule(bucket, rIdx, -1)} title="Move Rule Up">
                          <ArrowUp size={14} />
                        </button>
                        <button type="button" disabled={rIdx === rules.length - 1} onClick={() => moveRule(bucket, rIdx, 1)} title="Move Rule Down">
                          <ArrowDown size={14} />
                        </button>
                        <button type="button" onClick={() => duplicateRule(bucket, rIdx)} title="Duplicate Rule">
                          <Copy size={14} />
                        </button>
                        <button
                          type="button"
                          className="danger"
                          disabled={bucket === 'entry' && rules.length <= 1}
                          onClick={() => removeRule(bucket, rIdx)}
                          title="Delete Rule"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>

                    {/* Plain-English Beginner Verification Preview */}
                    <div className={`rb-english-preview ${rule.side === 'BUY' ? 'buy' : 'sell'}`}>
                      <span className="rb-preview-tag">PLAIN-ENGLISH PREVIEW</span>
                      <code>{plainEnglish}</code>
                    </div>

                    {rule.unresolvedConflict && (
                      <div className="rb-error-banner amber">
                        <AlertTriangle size={15} />
                        <span>{rule.conflictNote}</span>
                      </div>
                    )}

                    {ruleErrors.length > 0 && (
                      <div className="rb-error-banner">
                        <AlertTriangle size={15} />
                        <div>{ruleErrors.map((e, i) => <div key={i}>{e}</div>)}</div>
                      </div>
                    )}

                    <ConditionNodeEditor
                      node={rule.condition}
                      path={`${rulePath}.condition`}
                      onChange={newCond => updateRule(bucket, rIdx, { ...rule, condition: newCond })}
                      indicators={strategy.indicators || []}
                      errorsByPath={errorsByPath}
                    />
                  </div>
                );
              })}
            </div>
          );
        })}
      </CollapsibleSection>

      {/* 5. Stop Loss, Multi-Targets (3+) & Idempotent Trailing SL */}
      <CollapsibleSection
        title="05 · Stop Loss, Multi-Targets (3+) & Idempotent Trailing SL"
        subtitle="All 5 SL methods, 3+ partial targets summing to position quantity, and monotonic trailing SL steps"
        badge={`SL: ${strategy.stopLoss?.method} · ${(strategy.trailing || []).length} Trailing Steps`}
        defaultOpen={true}
      >
        {/* Stop Loss */}
        <div className="rb-subpanel">
          <b>Initial Stop Loss Configuration</b>
          <div className="form-grid" style={{ marginTop: 10 }}>
            <label className="field">
              <span>Stop Loss Method</span>
              <select
                value={strategy.stopLoss?.method || 'fixed_points'}
                onChange={e => {
                  const m = e.target.value;
                  const nextSl = {
                    id: strategy.stopLoss?.id || 'sl-1',
                    enabled: true,
                    order: 1,
                    label: `Stop Loss (${m})`,
                    method: m,
                    points: m === 'fixed_points' ? 10 : null,
                    percent: m === 'percentage' ? 0.5 : null,
                    indicatorId: m === 'indicator' ? (strategy.indicators?.[0]?.id || 'ind-1') : null,
                    candlePrice: m === 'candle' ? 'HIGH_LOW' : null,
                    candleCount: m === 'candle' ? 3 : null,
                    bufferPercent: m === 'candle' ? 0.0 : null,
                    level: m === 'level' ? 'FC1L' : null
                  };
                  setStrategy(prev => ({ ...prev, stopLoss: nextSl }));
                }}
              >
                <option value="fixed_points">Fixed Points</option>
                <option value="percentage">Percentage (%)</option>
                <option value="level">FC/RC Level (e.g. FC1L / RC1L)</option>
                <option value="candle">N-Candle High/Low/Close + Buffer %</option>
                <option value="indicator">Indicator Value</option>
              </select>
            </label>

            {strategy.stopLoss?.method === 'fixed_points' && (
              <label className="field">
                <span>Stop Loss Points (&gt; 0)</span>
                <input
                  type="number"
                  step="any"
                  min="0.1"
                  value={strategy.stopLoss?.points ?? 10}
                  onChange={e => setStrategy(prev => ({
                    ...prev,
                    stopLoss: { ...prev.stopLoss, points: parseFloat(e.target.value) || 1 }
                  }))}
                />
              </label>
            )}

            {strategy.stopLoss?.method === 'percentage' && (
              <label className="field">
                <span>Stop Loss Percent (&gt; 0%)</span>
                <input
                  type="number"
                  step="any"
                  min="0.01"
                  value={strategy.stopLoss?.percent ?? 0.5}
                  onChange={e => setStrategy(prev => ({
                    ...prev,
                    stopLoss: { ...prev.stopLoss, percent: parseFloat(e.target.value) || 0.5 }
                  }))}
                />
              </label>
            )}

            {strategy.stopLoss?.method === 'level' && (
              <label className="field">
                <span>FC/RC Stop Level</span>
                <select
                  value={strategy.stopLoss?.level || 'FC1L'}
                  onChange={e => setStrategy(prev => ({
                    ...prev,
                    stopLoss: { ...prev.stopLoss, level: e.target.value }
                  }))}
                >
                  {LEVEL_NAMES.map(lvl => <option key={lvl} value={lvl}>{lvl}</option>)}
                </select>
              </label>
            )}

            {strategy.stopLoss?.method === 'candle' && (
              <>
                <label className="field">
                  <span>Candle Comparison Price</span>
                  <select
                    value={strategy.stopLoss?.candlePrice || 'HIGH_LOW'}
                    onChange={e => setStrategy(prev => ({
                      ...prev,
                      stopLoss: { ...prev.stopLoss, candlePrice: e.target.value }
                    }))}
                  >
                    <option value="HIGH_LOW">HIGH_LOW (Lowest Low / Highest High)</option>
                    <option value="CLOSE">CLOSE</option>
                    <option value="OPEN">OPEN</option>
                  </select>
                </label>
                <label className="field">
                  <span>Lookback Candles (N)</span>
                  <input
                    type="number"
                    min="1"
                    value={strategy.stopLoss?.candleCount ?? 3}
                    onChange={e => setStrategy(prev => ({
                      ...prev,
                      stopLoss: { ...prev.stopLoss, candleCount: Math.max(1, parseInt(e.target.value, 10) || 1) }
                    }))}
                  />
                </label>
                <label className="field">
                  <span>Buffer Percent (&ge; 0%)</span>
                  <input
                    type="number"
                    step="any"
                    min="0"
                    value={strategy.stopLoss?.bufferPercent ?? 0}
                    onChange={e => setStrategy(prev => ({
                      ...prev,
                      stopLoss: { ...prev.stopLoss, bufferPercent: Math.max(0, parseFloat(e.target.value) || 0) }
                    }))}
                  />
                </label>
              </>
            )}
          </div>
        </div>

        {/* Multi-Targets (3+) */}
        <div className="rb-subpanel" style={{ marginTop: 14 }}>
          <div className="rb-subpanel-head">
            <b>Partial Targets (Minimum 3; enabled lots must sum to Position Quantity = {strategy.execution?.quantity ?? 1})</b>
          </div>
          <div className="rb-targets-list">
            {(strategy.targets || []).map((tgt, idx) => {
              const isFinal = idx === (strategy.targets?.length || 3) - 1;
              return (
                <div key={tgt.id || idx} className="rb-target-row">
                  <label className="rb-enable-toggle">
                    <input
                      type="checkbox"
                      checked={isFinal ? true : Boolean(tgt.enabled)}
                      disabled={isFinal}
                      onChange={e => {
                        const list = [...strategy.targets];
                        list[idx] = { ...tgt, enabled: e.target.checked, quantity: e.target.checked ? Math.max(1, tgt.quantity) : 0 };
                        setStrategy(prev => ({ ...prev, targets: list }));
                      }}
                    />
                    <span>{isFinal ? 'Final Target (Always On)' : `Target ${idx + 1}`}</span>
                  </label>

                  <select
                    value={tgt.unit || 'points'}
                    onChange={e => {
                      const u = e.target.value;
                      const list = [...strategy.targets];
                      list[idx] = {
                        ...tgt,
                        unit: u,
                        level: u === 'price_level' ? 'RC2H' : null,
                        value: u === 'price_level' ? 0 : (tgt.value > 0 ? tgt.value : 15)
                      };
                      setStrategy(prev => ({ ...prev, targets: list }));
                    }}
                  >
                    <option value="points">Points</option>
                    <option value="percent">Percent (%)</option>
                    <option value="price_level">FC/RC Level</option>
                  </select>

                  {tgt.unit === 'price_level' ? (
                    <select
                      value={tgt.level || 'RC2H'}
                      onChange={e => {
                        const list = [...strategy.targets];
                        list[idx] = { ...tgt, level: e.target.value };
                        setStrategy(prev => ({ ...prev, targets: list }));
                      }}
                    >
                      {LEVEL_NAMES.map(l => <option key={l} value={l}>{l}</option>)}
                    </select>
                  ) : (
                    <input
                      type="number"
                      step="any"
                      min="0.1"
                      value={tgt.value ?? 15}
                      onChange={e => {
                        const list = [...strategy.targets];
                        list[idx] = { ...tgt, value: parseFloat(e.target.value) || 1 };
                        setStrategy(prev => ({ ...prev, targets: list }));
                      }}
                    />
                  )}

                  <label className="rb-inline-param">
                    <span>Lots:</span>
                    <input
                      type="number"
                      min="0"
                      value={tgt.quantity ?? 0}
                      onChange={e => {
                        const list = [...strategy.targets];
                        list[idx] = { ...tgt, quantity: Math.max(0, parseInt(e.target.value, 10) || 0) };
                        setStrategy(prev => ({ ...prev, targets: list }));
                      }}
                    />
                  </label>
                </div>
              );
            })}
          </div>
        </div>

        {/* Trailing SL Panel */}
        <div className="rb-subpanel" style={{ marginTop: 14 }}>
          <div className="rb-subpanel-head">
            <div>
              <b>Idempotent Trailing Stop Loss Steps (IF &lt;condition&gt; THEN MOVE SL TO &lt;level&gt;)</b>
              <small style={{ display: 'block', color: 'var(--text-muted)' }}>
                LONG SL never moves down; SHORT SL never moves up. Each step fires at most once per trade.
              </small>
            </div>
            <button type="button" className="rb-btn-mini" onClick={addTrailingStep}>
              + Add Trailing SL Step
            </button>
          </div>

          {(strategy.trailing || []).map((st, idx) => (
            <div key={st.id || idx} className="rb-trailing-card">
              <div className="rb-block-header">
                <span className="rb-badge-type">TRAIL STEP #{idx + 1}</span>
                <input
                  className="rb-inline-label"
                  value={st.label || ''}
                  onChange={e => {
                    const list = [...strategy.trailing];
                    list[idx] = { ...st, label: e.target.value };
                    setStrategy(prev => ({ ...prev, trailing: list }));
                  }}
                />
                <span className="rb-mini-label">THEN MOVE SL TO:</span>
                <select
                  value={st.moveSlTo?.kind || 'level'}
                  onChange={e => {
                    const k = e.target.value;
                    const list = [...strategy.trailing];
                    list[idx] = {
                      ...st,
                      moveSlTo: {
                        kind: k,
                        level: k === 'level' ? 'RC1L' : null,
                        price: k === 'price' ? 24000 : null,
                        points: k === 'points' ? 10 : null,
                        indicatorId: k === 'indicator' ? (strategy.indicators?.[0]?.id || 'ind-1') : null
                      }
                    };
                    setStrategy(prev => ({ ...prev, trailing: list }));
                  }}
                >
                  <option value="level">FC/RC Level</option>
                  <option value="points">+Points Step</option>
                  <option value="price">Exact Price</option>
                </select>
                {st.moveSlTo?.kind === 'level' && (
                  <select
                    value={st.moveSlTo?.level || 'RC1L'}
                    onChange={e => {
                      const list = [...strategy.trailing];
                      list[idx] = { ...st, moveSlTo: { ...st.moveSlTo, level: e.target.value } };
                      setStrategy(prev => ({ ...prev, trailing: list }));
                    }}
                  >
                    {LEVEL_NAMES.map(l => <option key={l} value={l}>{l}</option>)}
                  </select>
                )}
                {(st.moveSlTo?.kind === 'points' || st.moveSlTo?.kind === 'price') && (
                  <input
                    type="number"
                    step="any"
                    style={{ width: 90 }}
                    value={st.moveSlTo?.[st.moveSlTo.kind] ?? 10}
                    onChange={e => {
                      const k = st.moveSlTo.kind;
                      const list = [...strategy.trailing];
                      list[idx] = { ...st, moveSlTo: { ...st.moveSlTo, [k]: parseFloat(e.target.value) || 1 } };
                      setStrategy(prev => ({ ...prev, trailing: list }));
                    }}
                  />
                )}
                <button
                  type="button"
                  className="remove-btn"
                  onClick={() => setStrategy(prev => ({
                    ...prev,
                    trailing: prev.trailing.filter((_, i) => i !== idx).map((x, i) => ({ ...x, order: i + 1 }))
                  }))}
                >
                  <Trash2 size={14} />
                </button>
              </div>

              <ConditionNodeEditor
                node={st.condition}
                path={`trailing[${idx}].condition`}
                onChange={newCond => {
                  const list = [...strategy.trailing];
                  list[idx] = { ...st, condition: newCond };
                  setStrategy(prev => ({ ...prev, trailing: list }));
                }}
                indicators={strategy.indicators || []}
                errorsByPath={errorsByPath}
              />
            </div>
          ))}
        </div>
      </CollapsibleSection>

      {/* 6. Re-entry & Daily Risk Guards */}
      <CollapsibleSection
        title="06 · Re-entry Rules & Daily Risk Caps"
        subtitle="Configure cooldown, max re-entries/day, allowed exit triggers, and daily max loss / max trades guards"
        badge={`Re-entry: ${strategy.reentry?.enabled ? 'ON' : 'OFF'} · Max Loss: ₹${strategy.risk?.maxDailyLoss}`}
        defaultOpen={false}
      >
        <div className="form-grid">
          <label className="field">
            <span>Enable Re-entry</span>
            <select
              value={strategy.reentry?.enabled ? 'true' : 'false'}
              onChange={e => {
                const on = e.target.value === 'true';
                setStrategy(prev => ({
                  ...prev,
                  reentry: {
                    ...(prev.reentry || {}),
                    enabled: on,
                    maxPerDay: on ? Math.max(1, prev.reentry?.maxPerDay || 2) : 0,
                    afterStopLoss: on ? true : false,
                    afterTarget: prev.reentry?.afterTarget ?? false,
                    afterManualExit: prev.reentry?.afterManualExit ?? false,
                    cooldownMinutes: prev.reentry?.cooldownMinutes ?? 5,
                    directions: on ? (prev.reentry?.directions?.length ? prev.reentry.directions : ['BUY']) : [],
                    stopLoss: on
                      ? (prev.reentry?.stopLoss || {
                          id: 're-sl-1',
                          enabled: true,
                          order: 1,
                          label: 'Re-entry SL',
                          method: 'level',
                          level: 'RC1L',
                          points: null,
                          percent: null,
                          indicatorId: null,
                          candlePrice: null,
                          candleCount: null,
                          bufferPercent: null
                        })
                      : null,
                    targets: on
                      ? (prev.reentry?.targets?.length
                          ? prev.reentry.targets
                          : [{ id: 're-tgt-1', enabled: true, order: 1, label: 'Re-entry Target', value: 20, unit: 'points', quantity: 1, level: null }])
                      : []
                  }
                }));
              }}
            >
              <option value="false">Disabled</option>
              <option value="true">Enabled</option>
            </select>
          </label>
          <label className="field">
            <span>Max Re-entries Per Day</span>
            <input
              type="number"
              min="0"
              disabled={!strategy.reentry?.enabled}
              value={strategy.reentry?.maxPerDay ?? 0}
              onChange={e => setStrategy(prev => ({
                ...prev,
                reentry: { ...prev.reentry, maxPerDay: Math.max(0, parseInt(e.target.value, 10) || 0) }
              }))}
            />
          </label>
          <label className="field">
            <span>Re-entry Cooldown (Minutes)</span>
            <input
              type="number"
              min="0"
              value={strategy.reentry?.cooldownMinutes ?? 0}
              onChange={e => setStrategy(prev => ({
                ...prev,
                reentry: { ...prev.reentry, cooldownMinutes: Math.max(0, parseInt(e.target.value, 10) || 0) }
              }))}
            />
          </label>
          <label className="field">
            <span>Max Daily Loss Guard (₹)</span>
            <input
              type="number"
              min="1"
              value={strategy.risk?.maxDailyLoss ?? 2000}
              onChange={e => setStrategy(prev => ({
                ...prev,
                risk: { ...prev.risk, maxDailyLoss: Math.max(1, parseFloat(e.target.value) || 2000) }
              }))}
            />
          </label>
          <label className="field">
            <span>Max Capital Allocation (₹)</span>
            <input
              type="number"
              min="1"
              value={strategy.risk?.maxCapital ?? 25000}
              onChange={e => setStrategy(prev => ({
                ...prev,
                risk: { ...prev.risk, maxCapital: Math.max(1, parseFloat(e.target.value) || 25000) }
              }))}
            />
          </label>
          <label className="field">
            <span>Max Trades Per Day Guard</span>
            <input
              type="number"
              min="1"
              value={strategy.risk?.maxTradesPerDay ?? 3}
              onChange={e => setStrategy(prev => ({
                ...prev,
                risk: { ...prev.risk, maxTradesPerDay: Math.max(1, parseInt(e.target.value, 10) || 1) }
              }))}
            />
          </label>
        </div>
      </CollapsibleSection>

      {/* 7. Phase 5 Shared Backtest Simulator & Paper Trading Worker Panel */}
      <CollapsibleSection
        title="07 · Candle-by-Candle Backtest & Background Paper Worker"
        subtitle="Run no-look-ahead historical simulation or launch the live background paper worker with daily risk guards"
        badge={btResult ? `Last Backtest Net: ₹${btResult.metrics.netProfit}` : 'Ready'}
        defaultOpen={true}
      >
        <div className="rb-bt-controls">
          <label className="rb-inline-param">
            <span>Fill Mode:</span>
            <select value={btFillMode} onChange={e => setBtFillMode(e.target.value)}>
              <option value="NEXT_CANDLE_OPEN">NEXT_CANDLE_OPEN (No Look-Ahead)</option>
              <option value="SAME_CANDLE_CLOSE">SAME_CANDLE_CLOSE</option>
            </select>
          </label>
          <label className="rb-inline-param">
            <span>Slippage (pts):</span>
            <input type="number" step="0.1" min="0" value={btSlippage} onChange={e => setBtSlippage(e.target.value)} />
          </label>
          <label className="rb-inline-param">
            <span>Brokerage/Order (₹):</span>
            <input type="number" step="1" min="0" value={btBrokerage} onChange={e => setBtBrokerage(e.target.value)} />
          </label>

          <button type="button" className="primary small" disabled={btLoading} onClick={handleRunBacktest}>
            <Play size={14} /> {btLoading ? 'Simulating...' : 'Run Backtest'}
          </button>
          <button type="button" className="rb-btn-buy" onClick={startWorker}>
            <Sparkles size={14} /> Start Paper Worker
          </button>
        </div>

        {/* Active Paper Worker Runs */}
        {paperRuns.length > 0 && (
          <div className="rb-worker-list">
            {paperRuns.map(run => (
              <div key={run.id} className="rb-worker-card">
                <div>
                  <span className={`rb-status-badge status-${String(run.status).toLowerCase()}`}>{run.status}</span>
                  <b style={{ marginLeft: 8 }}>{run.strategyName}</b>
                  <small style={{ marginLeft: 8, color: 'var(--text-muted)' }}>
                    Trades today: {run.tradesToday}/{run.maxTradesPerDay} · Realized Today: ₹{run.realizedPnlToday} · Max Loss Cap: -₹{run.maxDailyLoss}
                  </small>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>{run.message}</div>
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  {run.status === 'RUNNING' && (
                    <button type="button" className="outline small" onClick={() => controlWorker(run.id, 'pause')}>
                      <Pause size={13} /> Pause
                    </button>
                  )}
                  {run.status === 'PAUSED' && (
                    <button type="button" className="outline small" onClick={() => controlWorker(run.id, 'resume')}>
                      <Play size={13} /> Resume
                    </button>
                  )}
                  {!['STOPPED', 'STOPPED_RISK_LIMIT'].includes(run.status) && (
                    <button type="button" className="outline small danger" onClick={() => controlWorker(run.id, 'stop')}>
                      <Square size={13} /> Stop
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Backtest Results & Equity Curve */}
        {btResult && (
          <div className="rb-bt-results">
            <div className="rb-bt-metrics-grid">
              <div><small>Total Trades</small><b>{btResult.metrics.totalTrades} ({btResult.metrics.winners}W / {btResult.metrics.losers}L)</b></div>
              <div><small>Win Rate</small><b>{btResult.metrics.winRate}%</b></div>
              <div>
                <small>Net Profit</small>
                <b className={btResult.metrics.netProfit >= 0 ? 'positive' : 'negative'}>
                  ₹{btResult.metrics.netProfit.toLocaleString('en-IN')}
                </b>
              </div>
              <div><small>Max Drawdown</small><b className="negative">₹{btResult.metrics.maxDrawdown} ({btResult.metrics.maxDrawdownPercent}%)</b></div>
              <div><small>Avg Win / Loss</small><b>₹{btResult.metrics.avgProfit} / ₹{btResult.metrics.avgLoss}</b></div>
              <div><small>Risk-to-Reward</small><b>{btResult.metrics.riskToReward} ({btResult.intrabarConflictOrder})</b></div>
            </div>

            {btResult.equityCurve?.length > 0 && (
              <div style={{ height: 180, marginTop: 12 }}>
                <ResponsiveContainer width="100%" height={180}>
                  <AreaChart data={btResult.equityCurve}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
                    <XAxis dataKey="day" tick={{ fontSize: 10, fill: 'var(--chart-axis-text)' }} />
                    <YAxis domain={['auto', 'auto']} tick={{ fontSize: 10, fill: 'var(--chart-axis-text)' }} />
                    <Tooltip
                      contentStyle={{
                        background: 'var(--chart-tooltip-bg)',
                        border: '1px solid var(--chart-tooltip-border)',
                        color: 'var(--text-primary)',
                        fontSize: 11
                      }}
                    />
                    <Area
                      type="monotone"
                      dataKey="equity"
                      stroke="var(--chart-equity-stroke)"
                      fill="var(--chart-equity-fill)"
                      strokeWidth={2}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}

            {btResult.trades?.length > 0 && (
              <div className="table-wrap" style={{ marginTop: 12 }}>
                <table>
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Side</th>
                      <th>Entry Price</th>
                      <th>Exit Price</th>
                      <th>Entry Reason</th>
                      <th>Exit Reason</th>
                      <th>Intrabar Mode</th>
                      <th>Trailing SL History</th>
                      <th>Net P&L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {btResult.trades.map(t => (
                      <tr key={t.tradeNo}>
                        <td>{t.tradeNo}</td>
                        <td className={t.side === 'BUY' ? 'positive' : 'negative'}><b>{t.side}</b> {t.isReentry ? `(Re#${t.reentryNumber})` : ''}</td>
                        <td>₹{t.entryPrice}</td>
                        <td>₹{t.exitPrice}</td>
                        <td>{t.entryReason}</td>
                        <td>{t.exitReason}</td>
                        <td><span className="rb-type-pill">{t.intrabarResolutionUsed}</span></td>
                        <td>{t.slHistory?.length ? t.slHistory.map(h => `${h.oldSl}➔${h.newSl}`).join(', ') : 'None'}</td>
                        <td className={t.netPnl >= 0 ? 'positive' : 'negative'}><b>₹{t.netPnl}</b></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </CollapsibleSection>

      {/* 8. Phase 7: Broker Execution Settings, Go-Live Safety Gates, Order Lifecycle & Kill Switch */}
      <CollapsibleSection
        title="08 · Broker Execution, Go-Live Gates & Emergency Kill Switch"
        subtitle="Capabilities-validated execution settings, version-bound Go-Live checklist, idempotent order lifecycle, and instant Kill Switch"
        badge={
          killSwitchState?.active
            ? 'KILL SWITCH ARMED'
            : liveGates?.liveTradingUnlocked
            ? `LIVE UNLOCKED (${liveGates.versionHash?.slice(0, 8)})`
            : `PAPER ONLY (${liveGates?.versionHash?.slice(0, 8) || 'v2'})`
        }
        defaultOpen={true}
      >
        {/* Kill Switch Banner */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            flexWrap: 'wrap',
            gap: 12,
            padding: '12px 14px',
            borderRadius: 8,
            marginBottom: 14,
            background: killSwitchState?.active ? 'var(--loss-bg)' : 'var(--surface-subtle)',
            border: killSwitchState?.active ? '1px solid var(--loss)' : '1px solid var(--border-light)'
          }}
        >
          <div>
            <b style={{ color: killSwitchState?.active ? 'var(--loss-text)' : 'var(--text-primary)', fontSize: 13 }}>
              {killSwitchState?.active
                ? `EMERGENCY KILL SWITCH IS ARMED (${killSwitchState.reason || 'Halted'})`
                : 'Emergency Kill Switch (Stops all running strategies, cancels open orders, blocks new orders)'}
            </b>
            <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>
              Broker Status: <strong style={{ color: 'var(--link-color)' }}>{liveGates?.brokerStatusLabel || 'Live execution not connected (Paper Only)'}</strong> (Credentials stored server-side only; never exposed in UI or JSON exports)
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              type="button"
              className="btn-solid-danger"
              style={{
                padding: '8px 14px',
                borderRadius: 6,
                fontWeight: 700,
                cursor: 'pointer'
              }}
              onClick={handleTriggerKillSwitch}
            >
              <ShieldAlert size={14} style={{ marginRight: 6, verticalAlign: 'middle' }} />
              TRIGGER KILL SWITCH
            </button>
            {killSwitchState?.active && (
              <button type="button" className="outline small" onClick={handleResetKillSwitch}>
                <RotateCcw size={13} /> Reset Kill Switch
              </button>
            )}
          </div>
        </div>

        {/* Capabilities-driven Execution Settings */}
        <div className="form-grid" style={{ marginBottom: 14 }}>
          <label className="field">
            <span>Broker (from Capabilities)</span>
            <select value={orderBroker} onChange={e => setOrderBroker(e.target.value)}>
              {(brokerCaps?.brokers || ['IIFL', 'PAPER_SIMULATOR']).map(b => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Exchange</span>
            <select value={orderExchange} onChange={e => setOrderExchange(e.target.value)}>
              {((Array.isArray(brokerCaps?.exchanges) ? brokerCaps.exchanges : brokerCaps?.exchanges?.[orderBroker]) || ['NFO', 'NSE', 'BFO']).map(ex => (
                <option key={ex} value={ex}>{ex}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Product Type</span>
            <select value={orderProduct} onChange={e => setOrderProduct(e.target.value)}>
              {((Array.isArray(brokerCaps?.products) ? brokerCaps.products : brokerCaps?.products?.[orderBroker]) || ['INTRADAY', 'MARGIN', 'DELIVERY', 'PAPER']).map(p => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Order Type</span>
            <select value={orderTypeVal} onChange={e => setOrderTypeVal(e.target.value)}>
              {(brokerCaps?.orderTypes || ['MARKET', 'LIMIT', 'SL_LIMIT', 'SL_MARKET']).map(ot => (
                <option key={ot} value={ot}>{ot}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Validity</span>
            <select value={orderValidity} onChange={e => setOrderValidity(e.target.value)}>
              {(brokerCaps?.validities || ['DAY', 'IOC']).map(v => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>After Market Order (AMO)</span>
            <select value={orderAmo ? 'true' : 'false'} onChange={e => setOrderAmo(e.target.value === 'true')}>
              <option value="false">Regular Session Only (09:15–15:30 IST)</option>
              <option value="true">AMO Allowed Outside Session</option>
            </select>
          </label>
          <label className="field">
            <span>Execution Mode</span>
            <select
              value={orderMode === 'LIVE' && !brokerCaps?.liveExecutionEnabled ? 'PAPER' : orderMode}
              onChange={e => setOrderMode(e.target.value)}
            >
              {(brokerCaps?.modes || ['PAPER', 'LIVE']).map(m => {
                const isLiveDisabled = m === 'LIVE' && !brokerCaps?.liveExecutionEnabled;
                return (
                  <option key={m} value={m} disabled={isLiveDisabled}>
                    {isLiveDisabled ? 'LIVE (Disabled — Live execution not connected)' : m}
                  </option>
                );
              })}
            </select>
          </label>
          <label className="field">
            <span>Order Side</span>
            <select value={orderSide} onChange={e => setOrderSide(e.target.value)}>
              <option value="BUY">BUY</option>
              <option value="SELL">SELL</option>
            </select>
          </label>
          <label className="field">
            <span>Price / Reference LTP (₹)</span>
            <input type="number" step="0.5" value={orderLimitPrice} onChange={e => setOrderLimitPrice(e.target.value)} />
          </label>
          {['SL_LIMIT', 'SL_MARKET'].includes(orderTypeVal) && (
            <label className="field">
              <span>Trigger Price (₹)</span>
              <input type="number" step="0.5" value={orderTriggerPrice} onChange={e => setOrderTriggerPrice(e.target.value)} />
            </label>
          )}
          <label className="field">
            <span>Idempotency Key (Duplicate Protection)</span>
            <div style={{ display: 'flex', gap: 6 }}>
              <input
                type="text"
                value={orderIdempotencyKey}
                onChange={e => setOrderIdempotencyKey(e.target.value)}
              />
              <button
                type="button"
                className="outline small"
                onClick={() => setOrderIdempotencyKey(`ord-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`)}
                title="Generate fresh idempotency key"
              >
                New Key
              </button>
            </div>
          </label>
        </div>

        {/* Go-Live Safety Gates Checklist */}
        {liveGates && (
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: 'var(--surface-subtle)',
              border: '1px solid var(--border)',
              marginBottom: 14
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <b style={{ fontSize: 12, color: 'var(--text-primary)' }}>
                Go-Live Safety Gates (Version Hash: <code>{liveGates.versionHash?.slice(0, 12)}</code>)
              </b>
              {liveGates.versionChangedReset && (
                <span className="rb-status-badge status-stopped">
                  Strategy Edited — Previous Live Approval Reset
                </span>
              )}
            </div>

            <div className="rb-bt-metrics-grid" style={{ marginBottom: 10 }}>
              {Object.entries(liveGates.gates || {}).map(([gKey, gVal]) => (
                <div key={gKey} style={{ borderColor: gVal.passed ? 'var(--profit-border)' : 'var(--loss-border)' }}>
                  <small style={{ color: gVal.passed ? 'var(--profit-text)' : 'var(--loss-text)', fontWeight: 700 }}>
                    {gVal.passed ? '✓ PASS' : '✗ BLOCKED'} · {gKey}
                  </small>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>{gVal.detail}</div>
                </div>
              ))}
            </div>

            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <input
                type="text"
                style={{ flex: 1, minWidth: 280 }}
                placeholder={`Type "${liveGates.expectedConfirmationPhrase}" to unlock LIVE mode`}
                value={typedConfirmText}
                onChange={e => setTypedConfirmText(e.target.value)}
              />
              <button
                type="button"
                className="primary small"
                onClick={handleApproveLive}
                disabled={!liveGates?.liveExecutionEnabled}
                title={!liveGates?.liveExecutionEnabled ? 'Live execution not connected' : 'Approve this strategy version for LIVE trading'}
                style={!liveGates?.liveExecutionEnabled ? { opacity: 0.55, cursor: 'not-allowed' } : undefined}
              >
                <CheckCircle2 size={14} /> {liveGates?.liveExecutionEnabled ? 'Approve This Version for LIVE' : 'LIVE Disabled (Not Connected)'}
              </button>
              <button
                type="button"
                className="rb-btn-buy"
                onClick={handlePlaceBrokerOrder}
                disabled={orderMode === 'LIVE' && !liveGates?.liveExecutionEnabled}
                title={orderMode === 'LIVE' && !liveGates?.liveExecutionEnabled ? 'Live execution not connected' : undefined}
              >
                <Play size={14} /> Submit {orderMode === 'LIVE' && !liveGates?.liveExecutionEnabled ? 'PAPER' : orderMode} {orderSide} Order
              </button>
            </div>
          </div>
        )}

        {/* Broker Order Lifecycle Table */}
        {brokerOrders.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Order ID</th>
                  <th>Mode</th>
                  <th>Side</th>
                  <th>Instrument</th>
                  <th>Type / Validity</th>
                  <th>Qty</th>
                  <th>Status / Reason</th>
                  <th>Idempotency Key</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {brokerOrders.map(o => (
                  <tr key={o.id}>
                    <td><code>{o.id.slice(0, 8)}</code></td>
                    <td><span className="rb-type-pill">{o.mode}</span></td>
                    <td className={o.side === 'BUY' ? 'positive' : 'negative'}><b>{o.side}</b></td>
                    <td>{o.symbol} · {o.instrument} {o.strike} {o.optionType}</td>
                    <td>{o.orderType} / {o.validity} {o.amo ? '(AMO)' : ''}</td>
                    <td>{o.quantity} lot(s)</td>
                    <td>
                      <span className={`rb-status-badge status-${String(o.status).toLowerCase()}`}>
                        {o.status}
                      </span>
                      {o.rejectionReason && (
                        <div style={{ fontSize: 11, color: 'var(--loss-text)', marginTop: 2 }}>{o.rejectionReason}</div>
                      )}
                    </td>
                    <td><code>{o.idempotencyKey}</code></td>
                    <td>
                      {o.status === 'OPEN' ? (
                        <button type="button" className="outline small danger" onClick={() => handleCancelBrokerOrder(o.id)}>
                          Cancel
                        </button>
                      ) : (
                        <small style={{ color: 'var(--text-muted)' }}>Final</small>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CollapsibleSection>
    </div>
  );
}
