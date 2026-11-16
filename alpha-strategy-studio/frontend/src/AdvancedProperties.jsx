import React, { useMemo, useState } from 'react';
import { X, ChevronDown, ChevronRight, RotateCcw, ShieldCheck } from 'lucide-react';

// Property-grid definition. `path` is a dot-path into the strategy object so existing config is preserved on apply.
const T = { num: 'number', int: 'int', sel: 'select', time: 'time', bool: 'bool', text: 'text' };
const GROUPS = (isSar) => [
  { id: 'session', title: 'Session', props: [
    { path: isSar ? 'sar.startTime' : 'session.startTime', label: 'Session start', type: T.time, def: '09:15', min: '09:15', max: '15:30' },
    { path: isSar ? 'sar.endTime' : 'session.endTime', label: 'Session close', type: T.time, def: '15:00', min: '09:15', max: '15:30' },
    { path: 'advanced.startAfterOpenMinutes', label: 'Start after open (min)', type: T.int, def: 5, min: 0, max: 120 },
    { path: 'advanced.stopBeforeCloseMinutes', label: 'Stop before close (min)', type: T.int, def: 15, min: 0, max: 120 },
    { path: 'risk.maxTradesPerDay', label: 'Max trades per day', type: T.int, def: 3, min: 1, max: 100 }
  ]},
  { id: 'indicators', title: 'Indicators (CCI / RSI)', props: [
    { path: 'advanced.cciPeriod', label: 'CCI period', type: T.int, def: 20, min: 2, max: 200 },
    { path: 'advanced.cciUpper', label: 'CCI upper level', type: T.num, def: 100 },
    { path: 'advanced.cciLower', label: 'CCI lower level', type: T.num, def: -100 },
    { path: 'advanced.rsiPeriod', label: 'RSI period', type: T.int, def: 14, min: 2, max: 200 },
    { path: 'advanced.rsiOverbought', label: 'RSI overbought', type: T.num, def: 70, min: 1, max: 100 },
    { path: 'advanced.rsiOversold', label: 'RSI oversold', type: T.num, def: 30, min: 0, max: 99 }
  ]},
  { id: 'sl', title: 'Stop Loss', props: [
    ...(isSar ? [{ path: 'sar.slPoints', label: 'Stop-loss (points)', type: T.num, def: 1, min: 0.05 }] : [
      { path: 'advanced.slType', label: 'Stop-loss type', type: T.sel, def: 'POINTS', opts: ['POINTS', 'PERCENT'] },
      { path: 'advanced.slValue', label: 'Stop-loss value', type: T.num, def: 20, min: 0.05 }]),
    { path: 'risk.maxDailyLoss', label: 'Max daily loss (₹)', type: T.num, def: 2000, min: 1 }
  ]},
  { id: 'targets', title: 'Targets', props: [
    ...(isSar ? [{ path: 'sar.tgtPoints', label: 'Target (points)', type: T.num, def: 1.5, min: 0.05 }] : [
      { path: 'advanced.target1', label: 'Target 1 (points)', type: T.num, def: 30, min: 0 },
      { path: 'advanced.target1Lots', label: 'Target 1 lot multiplier', type: T.num, def: 1, min: 0 },
      { path: 'advanced.target2', label: 'Target 2 (points)', type: T.num, def: 60, min: 0 },
      { path: 'advanced.target2Lots', label: 'Target 2 lot multiplier', type: T.num, def: 1, min: 0 },
      { path: 'advanced.target3', label: 'Target 3 (points)', type: T.num, def: 90, min: 0 },
      { path: 'advanced.target3Lots', label: 'Target 3 lot multiplier', type: T.num, def: 1, min: 0 }])
  ]},
  { id: 'core', title: 'Core', props: [
    { path: 'instrument', label: 'Symbol', type: T.text, def: 'NIFTY 50' },
    { path: 'advanced.exchange', label: 'Exchange', type: T.sel, def: 'NFO', opts: ['NSE', 'NFO', 'BSE', 'BFO', 'MCX'] },
    { path: 'advanced.tradeDirection', label: 'Trade direction', type: T.sel, def: 'BOTH', opts: ['BOTH', 'LONG_ONLY', 'SHORT_ONLY'] },
    { path: isSar ? 'sar.baseQuantity' : 'positions.0.quantity', label: isSar ? 'Base quantity (units)' : 'Quantity (lots)', type: T.int, def: isSar ? 100 : 1, min: 1 },
    { path: 'advanced.product', label: 'Product', type: T.sel, def: 'MIS', opts: ['MIS', 'NRML', 'CNC'] },
    { path: 'advanced.orderType', label: 'Order type', type: T.sel, def: 'MARKET', opts: ['MARKET', 'LIMIT', 'SL', 'SL-M'] },
    { path: 'risk.maxCapital', label: 'Max capital (₹)', type: T.num, def: 25000, min: 1 },
    { path: 'risk.maxOpenPositions', label: 'Max open positions', type: T.int, def: 2, min: 1, max: 50 }
  ]},
  { id: 'algo', title: 'AlgoAction Module', props: [
    { path: 'advanced.orderPlacementMode', label: 'Order placement mode', type: T.sel, def: 'PAPER', opts: ['PAPER', 'SIGNAL_ONLY'], note: 'Live trading is enabled only through the go-live checklist with typed confirmation, never from this panel.' },
    { path: 'advanced.checkFrequency', label: 'Check frequency', type: T.sel, def: 'CONTINUOUS', opts: ['CONTINUOUS', 'CANDLE_CLOSE'] },
    { path: 'advanced.rollover', label: 'Rollover', type: T.sel, def: 'NONE', opts: ['NONE', 'NEXT_EXPIRY'] }
  ]}
];

const getPath = (o, p) => p.split('.').reduce((a, k) => (a == null ? a : a[k]), o);
function setPath(o, p, v) {
  const ks = p.split('.'); const root = structuredClone(o); let cur = root;
  ks.forEach((k, i) => {
    if (i === ks.length - 1) { cur[k] = v; return; }
    if (cur[k] == null || typeof cur[k] !== 'object') cur[k] = /^\d+$/.test(ks[i + 1]) ? [] : {};
    cur = cur[k];
  });
  return root;
}
const toMin = t => { const [h, m] = String(t || '').split(':').map(Number); return h * 60 + m; };

function validateAll(groups, draft) {
  const errs = {};
  groups.forEach(g => g.props.forEach(p => {
    const v = getPath(draft, p.path);
    if (v === '' || v == null) { if (p.type !== T.text) errs[p.path] = 'Required'; return; }
    if (p.type === T.num || p.type === T.int) {
      const n = Number(v);
      if (Number.isNaN(n)) errs[p.path] = 'Must be a number';
      else if (p.type === T.int && !Number.isInteger(n)) errs[p.path] = 'Whole number required';
      else if (p.min != null && n < p.min) errs[p.path] = `Minimum ${p.min}`;
      else if (p.max != null && n > p.max) errs[p.path] = `Maximum ${p.max}`;
    }
    if (p.type === T.time) {
      if (!/^\d{2}:\d{2}$/.test(v)) errs[p.path] = 'Use HH:MM';
      else if (toMin(v) < toMin(p.min) || toMin(v) > toMin(p.max)) errs[p.path] = `Between ${p.min} and ${p.max}`;
    }
  }));
  const s = getPath(draft, groups[0].props[0].path), e = getPath(draft, groups[0].props[1].path);
  if (s && e && toMin(s) >= toMin(e)) errs[groups[0].props[1].path] = 'Close must be after start';
  return errs;
}

export function AdvancedProperties({ strategy, onApply, onClose }) {
  const isSar = (strategy.strategyType || 'INDICATOR') === 'LEVEL_SAR';
  const groups = useMemo(() => GROUPS(isSar), [isSar]);
  // Start from the current strategy values; fall back to defaults only for unset keys (preserves existing config).
  const initial = useMemo(() => {
    let d = structuredClone(strategy);
    groups.forEach(g => g.props.forEach(p => { if (getPath(d, p.path) == null) d = setPath(d, p.path, p.def); }));
    // Permanently remove any legacy credential fields from draft
    if (d.advanced) {
      delete d.advanced.apiKey;
      delete d.advanced.apiSecret;
    }
    return d;
  }, [strategy, groups]);
  const [draft, setDraft] = useState(initial);
  const [open, setOpen] = useState(() => Object.fromEntries(groups.map((g, i) => [g.id, i < 2])));
  const errs = useMemo(() => validateAll(groups, draft), [groups, draft]);
  const bad = Object.keys(errs).length;
  const set = (p, v) => setDraft(d => setPath(d, p.path, v));
  const resetAll = () => {
    if (window.confirm('Reset all advanced properties to their defaults?')) {
      let d = structuredClone(strategy);
      groups.forEach(g => g.props.forEach(p => { d = setPath(d, p.path, p.def); }));
      if (d.advanced) { delete d.advanced.apiKey; delete d.advanced.apiSecret; }
      setDraft(d);
    }
  };

  const apply = () => {
    if (bad) return;
    const cleaned = structuredClone(draft);
    if (cleaned.advanced) {
      delete cleaned.advanced.apiKey;
      delete cleaned.advanced.apiSecret;
    }
    onApply(cleaned);
  };

  const control = p => {
    const v = getPath(draft, p.path);
    if (p.type === T.sel) return <select value={v ?? p.def} onChange={e => set(p, e.target.value)}>{p.opts.map(o => <option key={o}>{o}</option>)}</select>;
    if (p.type === T.bool) return <button type="button" className={`toggle ${v ? 'on' : ''}`} onClick={() => set(p, !v)} aria-pressed={!!v}><i /></button>;
    if (p.type === T.time) return <input type="time" value={v ?? ''} min={p.min} max={p.max} onChange={e => set(p, e.target.value)} />;
    if (p.type === T.text) return <input value={v ?? ''} onChange={e => set(p, e.target.value)} />;
    return <input type="number" step={p.type === T.int ? 1 : 'any'} value={v ?? ''} onChange={e => set(p, e.target.value === '' ? '' : Number(e.target.value))} />;
  };

  return (
    <div className="modal-back" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="adv-modal" role="dialog" aria-modal="true" aria-label="Advanced strategy properties">
        <div className="adv-head">
          <div><h2>Advanced Properties</h2><p>Detailed parameters for <b>{strategy.name || 'this strategy'}</b>. Changes apply only when you press Apply.</p></div>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><X size={18} /></button>
        </div>
        <div className="adv-body">
          {groups.map(g => (
            <div className="adv-group" key={g.id}>
              <button type="button" className="adv-group-head" onClick={() => setOpen(o => ({ ...o, [g.id]: !o[g.id] }))}>
                {open[g.id] ? <ChevronDown size={15} /> : <ChevronRight size={15} />}<b>{g.title}</b>
                {g.props.some(p => errs[p.path]) && <span className="adv-badge">needs attention</span>}
              </button>
              {open[g.id] && (
                <div className="adv-grid">
                  {g.props.map(p => (
                    <div className={`adv-row ${errs[p.path] ? 'err' : ''}`} key={p.path}>
                      <label>{p.label}{p.note && <small>{p.note}</small>}</label>
                      <div>{control(p)}{errs[p.path] && <em role="alert">{errs[p.path]}</em>}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
        <div className="adv-foot">
          <button className="outline" onClick={resetAll}><RotateCcw size={14} /> Reset all</button>
          <span className="adv-foot-msg">{bad ? `${bad} value${bad > 1 ? 's' : ''} need fixing before Apply` : 'All values valid'}</span>
          <button className="outline" onClick={onClose}>Cancel</button>
          <button className="primary" disabled={!!bad} onClick={apply}>Apply</button>
        </div>
      </div>
    </div>
  );
}
export default AdvancedProperties;
