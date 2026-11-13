import React, { useState, useEffect, useMemo } from 'react';
import {
  ChevronDown, ChevronRight, Plus, Trash2, Copy, ArrowUp, ArrowDown,
  AlertTriangle, CheckCircle2, Play, Pause, Square, RotateCcw, Upload,
  Download, History, Layers, Sliders, ShieldAlert, Sparkles
} from 'lucide-react';
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid
} from 'recharts';

export const LEVEL_NAMES = [
  'FC1H', 'FC1L', 'FC2H', 'FC2L', 'FC3H', 'FC3L',
  'RC1H', 'RC1L', 'RC2H', 'RC2L', 'RC3H', 'RC3L', 'RC4H', 'RC4L', 'RC5H', 'RC5L'
];

export const TIMEFRAMES = ['1m', '3m', '5m', '10m', '15m', '30m', '60m', '1h', '1d'];
export const PRICE_FIELDS = ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'LTP', 'VOLUME'];
export const OPERATORS = [
  { value: '>', label: '> (Greater than)' },
  { value: '<', label: '< (Less than)' },
  { value: '>=', label: '>= (Greater or equal)' },
  { value: '<=', label: '<= (Less or equal)' },
  { value: '==', label: '== (Equals)' },
  { value: 'crosses_above', label: 'Crosses Above' },
  { value: 'crosses_below', label: 'Crosses Below' },
  { value: 'trades_above', label: 'Trades Above' },
  { value: 'trades_below', label: 'Trades Below' },
  { value: 'candle_green', label: 'Candle is Green (Close > Open)' },
  { value: 'candle_red', label: 'Candle is Red (Close < Open)' },
  { value: 'close_above_open', label: 'Close Above Open' },
  { value: 'close_below_open', label: 'Close Below Open' },
  { value: 'near', label: 'Near Level (Tolerance required)' },
  { value: 'buy_near', label: 'BUY Near Level (Tolerance required)' },
  { value: 'sell_near', label: 'SELL Near Level (Tolerance required)' },
  { value: 'moves_out_of', label: 'Moves Out Of Level (Breakout)' },
  { value: 'out_of_fc1h', label: 'Breaks Out Of FC1H' },
  { value: 'out_of_fc1l', label: 'Breaks Out Of FC1L' },
  { value: 'out_of_range', label: 'Moves Outside Range [Lower..Upper]' },
  { value: 'breakout_above', label: 'Breakout Above Level' },
  { value: 'breakout_below', label: 'Breakout Below Level' }
];

const UNARY_OPS = new Set(['candle_green', 'candle_red', 'close_above_open', 'close_below_open']);
const TOLERANCE_OPS = new Set([
  'near', 'buy_near', 'sell_near', 'moves_out_of',
  'out_of_fc1h', 'out_of_fc1l', 'out_of_range', 'breakout_above', 'breakout_below'
]);

const SECRET_KEYS = new Set([
  'password', 'secret', 'token', 'authcode', 'apikey', 'appsecret', 'app_key',
  'app_secret', 'usersession', 'authorization', 'credential', 'credentials',
  'accesstoken', 'refreshtoken'
]);

export function stripCredentialsClient(obj) {
  if (Array.isArray(obj)) return obj.map(stripCredentialsClient);
  if (obj && typeof obj === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      const norm = String(k).replace(/_/g, '').toLowerCase();
      if (SECRET_KEYS.has(norm) || SECRET_KEYS.has(String(k).toLowerCase())) continue;
      out[k] = stripCredentialsClient(v);
    }
    return out;
  }
  return obj;
}

export function operandToEnglish(op, indicatorLabels = {}) {
  if (!op || typeof op !== 'object') return '?';
  if (op.kind === 'price') return op.field || 'CLOSE';
  if (op.kind === 'level') return op.level || 'RC1H';
  if (op.kind === 'constant') return String(op.value ?? 0);
  if (op.kind === 'indicator') {
    const base = indicatorLabels[op.indicatorId] || op.indicatorId || 'Indicator';
    return op.output && op.output !== 'value' ? `${base}.${op.output}` : base;
  }
  return '?';
}

export function conditionToEnglish(node, indicatorLabels = {}) {
  if (!node || typeof node !== 'object') return '(empty)';
  if (node.enabled === false) return '[Disabled]';
  if (node.type === 'leaf') {
    const op = node.operator || '>';
    const tf = node.timeframe || '5m';
    const ref = node.candleRef && node.candleRef !== 'current' ? ` (${node.candleRef})` : '';
    if (op === 'candle_green') return `${tf}${ref} candle is green`;
    if (op === 'candle_red') return `${tf}${ref} candle is red`;
    if (op === 'close_above_open') return `${tf}${ref} close > open`;
    if (op === 'close_below_open') return `${tf}${ref} close < open`;

    const left = operandToEnglish(node.left, indicatorLabels);
    const right = operandToEnglish(node.right, indicatorLabels);
    const tol = node.params?.tolerance;
    const unit = node.params?.toleranceUnit || 'points';
    const tolText = tol != null && tol > 0 ? `${tol} ${unit}` : '⚠️ MISSING TOLERANCE';

    if (op === 'near' || op === 'buy_near' || op === 'sell_near') {
      return `${left} ${op.replace('_', ' ')} ${right} (within ${tolText})`;
    }
    if (TOLERANCE_OPS.has(op)) {
      return `${left} ${op.replace(/_/g, ' ')} ${right} by ${tolText}`;
    }
    return `${left} ${op.replace(/_/g, ' ')} ${right} [${tf}${ref}]`;
  }
  if (node.type === 'group') {
    const logic = node.logic || 'AND';
    const active = (node.children || []).filter(c => c.enabled !== false);
    if (!active.length) return '(empty group)';
    const parts = active.map(c => conditionToEnglish(c, indicatorLabels));
    return parts.length === 1 ? parts[0] : `(${parts.join(` ${logic} `)})`;
  }
  if (node.type === 'sequence') {
    const active = (node.steps || []).filter(s => s.enabled !== false);
    if (!active.length) return '(empty sequence)';
    const parts = active.map((s, idx) => `Step ${idx + 1}: ${conditionToEnglish(s, indicatorLabels)}`);
    const tmo = node.timeoutMinutes ? ` [timeout ${node.timeoutMinutes}m]` : '';
    return `${parts.join(' ➔ THEN ➔ ')}${tmo}`;
  }
  return '(condition)';
}

export function makeDefaultLeaf(idPrefix = 'leaf') {
  return {
    type: 'leaf',
    id: `${idPrefix}-${Math.random().toString(36).slice(2, 7)}`,
    enabled: true,
    order: 1,
    label: 'Price condition',
    left: { kind: 'price', field: 'CLOSE', indicatorId: null, level: null, value: null },
    operator: '>',
    right: { kind: 'level', field: null, indicatorId: null, level: 'RC1H', value: null },
    timeframe: '5m',
    candleRef: 'current',
    evaluateOn: 'CANDLE_CLOSE',
    params: { tolerance: null, toleranceUnit: null }
  };
}

export function makeDefaultGroup(idPrefix = 'grp') {
  return {
    type: 'group',
    id: `${idPrefix}-${Math.random().toString(36).slice(2, 7)}`,
    enabled: true,
    order: 1,
    label: 'Condition Group',
    logic: 'AND',
    children: [makeDefaultLeaf()]
  };
}

export function makeDefaultSequence(idPrefix = 'seq') {
  const s1 = makeDefaultLeaf('step1');
  s1.label = 'Step 1: Price trades below RC1L';
  s1.operator = 'trades_below';
  s1.right = { kind: 'level', field: null, indicatorId: null, level: 'RC1L', value: null };
  s1.order = 1;

  const s2 = makeDefaultLeaf('step2');
  s2.label = 'Step 2: Price trades above RC1H';
  s2.operator = 'trades_above';
  s2.right = { kind: 'level', field: null, indicatorId: null, level: 'RC1H', value: null };
  s2.order = 2;

  return {
    type: 'sequence',
    id: `${idPrefix}-${Math.random().toString(36).slice(2, 7)}`,
    enabled: true,
    order: 1,
    label: 'Sequential Event (Step 1 ➔ Step 2)',
    allowSameTick: false,
    timeoutMinutes: 60,
    steps: [s1, s2]
  };
}

function OperandPicker({ label, operand, onChange, indicators }) {
  const op = operand || { kind: 'price', field: 'CLOSE', indicatorId: null, level: null, value: null };

  const setKind = (kind) => {
    if (kind === 'price') onChange({ kind: 'price', field: 'CLOSE', indicatorId: null, level: null, value: null });
    else if (kind === 'level') onChange({ kind: 'level', field: null, indicatorId: null, level: 'RC1H', value: null });
    else if (kind === 'indicator') {
      const firstId = indicators?.[0]?.id || 'ind-1';
      onChange({ kind: 'indicator', field: null, indicatorId: firstId, output: 'value', level: null, value: null });
    } else {
      onChange({ kind: 'constant', field: null, indicatorId: null, level: null, value: 100 });
    }
  };

  return (
    <div className="rb-operand-picker">
      <span className="rb-mini-label">{label}</span>
      <div className="rb-operand-row">
        <select
          aria-label={`${label} kind`}
          value={op.kind || 'price'}
          onChange={e => setKind(e.target.value)}
        >
          <option value="price">Price / Candle</option>
          <option value="level">FC/RC Level</option>
          <option value="indicator">Indicator</option>
          <option value="constant">Constant</option>
        </select>

        {op.kind === 'price' && (
          <select
            aria-label={`${label} price field`}
            value={op.field || 'CLOSE'}
            onChange={e => onChange({ ...op, field: e.target.value })}
          >
            {PRICE_FIELDS.map(f => <option key={f} value={f}>{f}</option>)}
          </select>
        )}

        {op.kind === 'level' && (
          <select
            aria-label={`${label} FC/RC level`}
            value={op.level || 'RC1H'}
            onChange={e => onChange({ ...op, level: e.target.value })}
          >
            {LEVEL_NAMES.map(lvl => <option key={lvl} value={lvl}>{lvl}</option>)}
          </select>
        )}

        {op.kind === 'indicator' && (
          <>
            <select
              aria-label={`${label} indicator id`}
              value={op.indicatorId || ''}
              onChange={e => onChange({ ...op, indicatorId: e.target.value })}
            >
              {(indicators || []).length === 0 && <option value="">(Add indicator first)</option>}
              {(indicators || []).map(ind => (
                <option key={ind.id} value={ind.id}>{ind.label} ({ind.type})</option>
              ))}
            </select>
            <select
              aria-label={`${label} indicator output`}
              value={op.output || 'value'}
              onChange={e => onChange({ ...op, output: e.target.value })}
            >
              <option value="value">Main Value</option>
              <option value="signal">Signal / Avg</option>
              <option value="histogram">MACD Histogram</option>
              <option value="upper">Upper Band</option>
              <option value="lower">Lower Band</option>
              <option value="k">Stoch %K</option>
              <option value="d">Stoch %D</option>
              <option value="direction">Supertrend Dir</option>
              <option value="plus_di">ADX +DI</option>
              <option value="minus_di">ADX -DI</option>
            </select>
          </>
        )}

        {op.kind === 'constant' && (
          <input
            type="number"
            step="any"
            aria-label={`${label} constant value`}
            value={op.value ?? 0}
            onChange={e => onChange({ ...op, value: parseFloat(e.target.value) || 0 })}
          />
        )}
      </div>
    </div>
  );
}

export function ConditionNodeEditor({
  node,
  path,
  onChange,
  indicators,
  errorsByPath,
  insideSequence = false
}) {
  const [dragIdx, setDragIdx] = useState(null);
  const directErrors = useMemo(() => {
    const matches = [];
    for (const [errPath, msgs] of Object.entries(errorsByPath || {})) {
      if (errPath === path || errPath.startsWith(path + '.')) {
        // Only show leaf-level or direct container errors on this exact block
        const remainder = errPath.slice(path.length);
        if (!remainder.includes('children[') && !remainder.includes('steps[')) {
          matches.push(...msgs);
        }
      }
    }
    return matches;
  }, [errorsByPath, path]);

  if (!node) return null;

  const handleOperatorChange = (newOp) => {
    let updated = { ...node, operator: newOp };
    if (UNARY_OPS.has(newOp)) {
      updated.left = null;
      updated.right = null;
      updated.evaluateOn = 'candle_close';
      updated.params = { tolerance: null, toleranceUnit: null };
    } else {
      if (!updated.left) updated.left = { kind: 'price', field: 'CLOSE', indicatorId: null, level: null, value: null };
      if (!updated.right) updated.right = { kind: 'level', field: null, indicatorId: null, level: 'RC1H', value: null };
      if (TOLERANCE_OPS.has(newOp)) {
        updated.params = {
          ...(updated.params || {}),
          tolerance: updated.params?.tolerance > 0 ? updated.params.tolerance : 5,
          toleranceUnit: updated.params?.toleranceUnit || 'points'
        };
      } else {
        updated.params = { tolerance: null, toleranceUnit: null };
      }
    }
    onChange(updated);
  };

  if (node.type === 'leaf') {
    const isUnary = UNARY_OPS.has(node.operator);
    const needsTol = TOLERANCE_OPS.has(node.operator);

    return (
      <div className={`rb-block rb-leaf ${!node.enabled ? 'rb-disabled' : ''} ${directErrors.length ? 'rb-has-error' : ''}`} tabIndex={0}>
        <div className="rb-block-header">
          <span className="rb-badge-type">CONDITION</span>
          <input
            className="rb-inline-label"
            value={node.label || ''}
            placeholder="Condition label"
            onChange={e => onChange({ ...node, label: e.target.value })}
          />
          <label className="rb-enable-toggle">
            <input
              type="checkbox"
              checked={node.enabled !== false}
              onChange={e => onChange({ ...node, enabled: e.target.checked })}
            />
            <span>{node.enabled !== false ? 'Enabled' : 'Disabled'}</span>
          </label>
        </div>

        <div className="rb-leaf-grid">
          {!isUnary && (
            <OperandPicker
              label="Left Operand"
              operand={node.left}
              indicators={indicators}
              onChange={left => onChange({ ...node, left })}
            />
          )}

          <div className="rb-operand-picker">
            <span className="rb-mini-label">Operator</span>
            <select value={node.operator || '>'} onChange={e => handleOperatorChange(e.target.value)}>
              {OPERATORS.map(op => <option key={op.value} value={op.value}>{op.label}</option>)}
            </select>
          </div>

          {!isUnary && (
            <OperandPicker
              label="Right Operand"
              operand={node.right}
              indicators={indicators}
              onChange={right => onChange({ ...node, right })}
            />
          )}

          <div className="rb-operand-picker">
            <span className="rb-mini-label">Timeframe & Candle</span>
            <div className="rb-operand-row">
              <select value={node.timeframe || '5m'} onChange={e => onChange({ ...node, timeframe: e.target.value })}>
                {TIMEFRAMES.map(tf => <option key={tf} value={tf}>{tf}</option>)}
              </select>
              <select value={node.candleRef || 'current'} onChange={e => onChange({ ...node, candleRef: e.target.value })}>
                <option value="current">Current candle</option>
                <option value="previous">Previous candle</option>
                <option value="2-ago">2 candles ago</option>
                <option value="3-ago">3 candles ago</option>
              </select>
            </div>
          </div>

          <div className="rb-operand-picker">
            <span className="rb-mini-label">Evaluation Mode</span>
            <select
              value={node.evaluateOn || 'CANDLE_CLOSE'}
              disabled={isUnary}
              onChange={e => onChange({ ...node, evaluateOn: e.target.value })}
            >
              <option value="CANDLE_CLOSE">CANDLE_CLOSE (Default)</option>
              <option value="LIVE_TICK">LIVE_TICK</option>
              <option value="INTRABAR">INTRABAR</option>
            </select>
          </div>

          {needsTol && (
            <div className="rb-operand-picker rb-tolerance-box">
              <span className="rb-mini-label" style={{ color: 'var(--warning-text)' }}>Mandatory Proximity / Breakout Tolerance</span>
              <div className="rb-operand-row">
                <input
                  type="number"
                  step="any"
                  min="0.01"
                  placeholder="Tolerance (> 0)"
                  value={node.params?.tolerance ?? ''}
                  onChange={e => {
                    const val = e.target.value === '' ? null : parseFloat(e.target.value);
                    onChange({
                      ...node,
                      params: { ...(node.params || {}), tolerance: val }
                    });
                  }}
                />
                <select
                  value={node.params?.toleranceUnit || ''}
                  onChange={e => onChange({
                    ...node,
                    params: { ...(node.params || {}), toleranceUnit: e.target.value || null }
                  })}
                >
                  <option value="">-- Unit --</option>
                  <option value="points">Points</option>
                  <option value="percent">Percent (%)</option>
                </select>
              </div>
            </div>
          )}
        </div>

        {directErrors.length > 0 && (
          <div className="rb-error-banner" role="alert">
            <AlertTriangle size={14} />
            <div>{directErrors.map((err, i) => <div key={i}>{err}</div>)}</div>
          </div>
        )}
      </div>
    );
  }

  // Group or Sequence container
  const isSeq = node.type === 'sequence';
  const itemsKey = isSeq ? 'steps' : 'children';
  const items = node[itemsKey] || [];

  const updateChild = (idx, newChild) => {
    const next = items.map((it, i) => (i === idx ? newChild : it));
    onChange({ ...node, [itemsKey]: next });
  };

  const renumber = (arr) => arr.map((it, i) => ({ ...it, order: i + 1 }));

  const removeChild = (idx) => {
    const next = renumber(items.filter((_, i) => i !== idx));
    onChange({ ...node, [itemsKey]: next });
  };

  const duplicateChild = (idx) => {
    const orig = structuredClone(items[idx]);
    const reId = (n) => {
      n.id = `${n.type}-${Math.random().toString(36).slice(2, 7)}`;
      if (n.children) n.children.forEach(reId);
      if (n.steps) n.steps.forEach(reId);
    };
    reId(orig);
    orig.label = `${orig.label || 'Step'} (Copy)`;
    const next = [...items];
    next.splice(idx + 1, 0, orig);
    onChange({ ...node, [itemsKey]: renumber(next) });
  };

  const moveChild = (idx, dir) => {
    const target = idx + dir;
    if (target < 0 || target >= items.length) return;
    const next = [...items];
    const [picked] = next.splice(idx, 1);
    next.splice(target, 0, picked);
    onChange({ ...node, [itemsKey]: renumber(next) });
  };

  const addChildLeaf = () => {
    const leaf = makeDefaultLeaf(isSeq ? `step${items.length + 1}` : 'cond');
    leaf.order = items.length + 1;
    onChange({ ...node, [itemsKey]: [...items, leaf] });
  };

  const addChildGroup = () => {
    const grp = makeDefaultGroup('subgrp');
    grp.order = items.length + 1;
    onChange({ ...node, [itemsKey]: [...items, grp] });
  };

  const addChildSeq = () => {
    if (insideSequence || isSeq) return;
    const seq = makeDefaultSequence('seq');
    seq.order = items.length + 1;
    onChange({ ...node, [itemsKey]: [...items, seq] });
  };

  return (
    <div className={`rb-block ${isSeq ? 'rb-sequence' : 'rb-group'} ${directErrors.length ? 'rb-has-error' : ''}`} tabIndex={0}>
      <div className="rb-block-header">
        <span className={`rb-badge-type ${isSeq ? 'seq' : 'grp'}`}>
          {isSeq ? 'ORDERED SEQUENCE' : `${node.logic || 'AND'} GROUP`}
        </span>
        <input
          className="rb-inline-label"
          value={node.label || ''}
          onChange={e => onChange({ ...node, label: e.target.value })}
        />

        {!isSeq && (
          <div className="rb-logic-pill-toggle" role="group" aria-label="Group logic">
            {['AND', 'OR'].map(lg => (
              <button
                key={lg}
                type="button"
                className={node.logic === lg ? 'active' : ''}
                onClick={() => onChange({ ...node, logic: lg })}
              >
                {lg}
              </button>
            ))}
          </div>
        )}

        {isSeq && (
          <div className="rb-seq-options">
            <label title="Allow multiple steps to complete on the exact same tick">
              <input
                type="checkbox"
                checked={Boolean(node.allowSameTick)}
                onChange={e => onChange({ ...node, allowSameTick: e.target.checked })}
              />
              <span>Same-tick allowed</span>
            </label>
            <label title="Optional timeout in minutes before sequence resets">
              <span>Timeout (min):</span>
              <input
                type="number"
                min="1"
                placeholder="None"
                style={{ width: 68 }}
                value={node.timeoutMinutes ?? ''}
                onChange={e => onChange({
                  ...node,
                  timeoutMinutes: e.target.value === '' ? null : Math.max(1, parseInt(e.target.value, 10) || 1)
                })}
              />
            </label>
          </div>
        )}
      </div>

      {directErrors.length > 0 && (
        <div className="rb-error-banner" role="alert">
          <AlertTriangle size={14} />
          <div>{directErrors.map((err, i) => <div key={i}>{err}</div>)}</div>
        </div>
      )}

      <div className="rb-children-list">
        {items.map((child, idx) => (
          <React.Fragment key={child.id || idx}>
            {idx > 0 && (
              <div className={`rb-connector ${isSeq ? 'seq' : (node.logic || 'AND').toLowerCase()}`}>
                <span>{isSeq ? `THEN STEP ${idx + 1}` : (node.logic || 'AND')}</span>
              </div>
            )}
            <div
              className="rb-child-wrapper"
              draggable
              onDragStart={() => setDragIdx(idx)}
              onDragOver={e => e.preventDefault()}
              onDrop={() => {
                if (dragIdx !== null && dragIdx !== idx) {
                  moveChild(dragIdx, idx - dragIdx);
                }
                setDragIdx(null);
              }}
            >
              <div className="rb-child-toolbar">
                <span className="rb-step-tag">
                  {isSeq ? `Step #${idx + 1}` : `Block #${idx + 1}`}
                </span>
                <div className="rb-child-actions">
                  <button
                    type="button"
                    title="Move Up"
                    aria-label="Move block up"
                    disabled={idx === 0}
                    onClick={() => moveChild(idx, -1)}
                  >
                    <ArrowUp size={13} />
                  </button>
                  <button
                    type="button"
                    title="Move Down"
                    aria-label="Move block down"
                    disabled={idx === items.length - 1}
                    onClick={() => moveChild(idx, 1)}
                  >
                    <ArrowDown size={13} />
                  </button>
                  <button
                    type="button"
                    title="Duplicate block"
                    aria-label="Duplicate block"
                    onClick={() => duplicateChild(idx)}
                  >
                    <Copy size={13} />
                  </button>
                  <button
                    type="button"
                    className="danger"
                    title="Delete block"
                    aria-label="Delete block"
                    disabled={items.length <= (isSeq ? 2 : 1)}
                    onClick={() => removeChild(idx)}
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>

              <ConditionNodeEditor
                node={child}
                path={`${path}.${itemsKey}[${idx}]`}
                onChange={updated => updateChild(idx, updated)}
                indicators={indicators}
                errorsByPath={errorsByPath}
                insideSequence={insideSequence || isSeq}
              />
            </div>
          </React.Fragment>
        ))}
      </div>

      <div className="rb-add-bar">
        <button type="button" className="rb-btn-mini" onClick={addChildLeaf}>
          <Plus size={13} /> {isSeq ? 'Add Sequence Step' : 'Add Condition'}
        </button>
        <button type="button" className="rb-btn-mini" onClick={addChildGroup}>
          <Layers size={13} /> {isSeq ? 'Add Group Step' : 'Add Nested AND/OR Group'}
        </button>
        {!isSeq && !insideSequence && (
          <button type="button" className="rb-btn-mini" onClick={addChildSeq}>
            <Sparkles size={13} /> Add Sequence Node
          </button>
        )}
      </div>
    </div>
  );
}
