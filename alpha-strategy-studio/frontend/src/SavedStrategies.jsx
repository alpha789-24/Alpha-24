import React, { useMemo, useState } from 'react';
import { Search, Pencil, Copy, Trash2, ShieldCheck, Play, FolderOpen, Plus, FileCode, CheckCircle2, AlertTriangle, ShieldAlert } from 'lucide-react';

const isPythonImport = s => (s.sourceType === 'PYTHON_IMPORT' || s.meta?.sourceType === 'PYTHON_IMPORT');

const typeLabel = s => {
  if (isPythonImport(s)) return 'Python Import';
  const t = s.strategyType || 'INDICATOR';
  return { LEVEL_SAR: 'Level SAR', RULE_BUILDER: 'Rule Builder', INDICATOR: 'Indicator' }[t] || 'Indicator';
};

const fmtDate = s => {
  const d = s ? new Date(s) : null;
  return d && !isNaN(d) ? d.toLocaleString('en-IN', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
};
const when = s => s.updatedAt || s.updated_at || s.savedAt || s.createdAt || s.created_at;

export default function SavedStrategies({ strategies, currentId, onLoad, onDuplicate, onDelete, onValidate, onPaper, onNew }) {
  const [q, setQ] = useState('');
  const [type, setType] = useState('ALL');
  const [confirmDel, setConfirmDel] = useState(null);

  const rows = useMemo(() => {
    const t = q.trim().toLowerCase();
    return (strategies || [])
      .filter(s => {
        const py = isPythonImport(s);
        const matchesType = type === 'ALL'
          || (type === 'PYTHON_IMPORT' && py)
          || (!py && (s.strategyType || 'INDICATOR') === type);
        const textMatch = !t || `${s.name} ${s.instrument || (s.execution?.instrument?.underlying || '')} ${s.description || (s.meta?.description || '')}`.toLowerCase().includes(t);
        return matchesType && textMatch;
      })
      .sort((a, b) => String(when(b) || '').localeCompare(String(when(a) || '')));
  }, [strategies, q, type]);

  return (
    <section className="content">
      <div className="welcome-row builder-title">
        <div>
          <div className="eyebrow">SAVED CONFIGURATIONS</div>
          <h1>My Strategies</h1>
          <p className="sub">Find, reopen and reuse every strategy you have saved. Loading opens a copy in the builder — your saved version is only replaced when you press Save.</p>
        </div>
        <div className="builder-actions"><button className="primary" onClick={onNew}><Plus size={16} /> New strategy</button></div>
      </div>

      <div className="panel section-card">
        <div className="saved-toolbar">
          <div className="saved-search"><Search size={14} /><input placeholder="Search by name, instrument or description…" value={q} onChange={e => setQ(e.target.value)} /></div>
          <select value={type} onChange={e => setType(e.target.value)}>
            <option value="ALL">All types</option>
            <option value="RULE_BUILDER">Rule Builder</option>
            <option value="PYTHON_IMPORT">Python Import</option>
            <option value="LEVEL_SAR">Level SAR</option>
            <option value="INDICATOR">Indicator</option>
          </select>
          <span className="muted">{rows.length} of {(strategies || []).length}</span>
        </div>

        {rows.length === 0 ? (
          <div className="saved-empty"><FolderOpen size={28} /><b>No saved strategies{q || type !== 'ALL' ? ' match your filters' : ' yet'}</b><span>Build or import a strategy and press <i>Save strategy</i> — it will appear here and survive refreshes and restarts.</span></div>
        ) : (
          <div className="table-wrap">
            <table className="saved-table">
              <thead><tr><th>Strategy</th><th>Type / Source</th><th>Instrument</th><th>Last saved</th><th>Status / Compatibility</th><th>Actions</th></tr></thead>
              <tbody>
                {rows.map(s => {
                  const py = isPythonImport(s);
                  const inst = s.instrument || s.execution?.instrument?.underlying || (s.meta?.symbol) || '—';
                  const tf = s.timeframe || s.execution?.defaultEvaluationMode || (s.indicators?.[0]?.timeframe) || null;
                  return (
                    <tr key={s.id} className={s.id === currentId ? 'is-current' : ''}>
                      <td>
                        <b>{s.name}</b>
                        {s.id === currentId && <span className="tag live-tag">open in builder</span>}
                        <small>{s.description || s.meta?.description || '—'}</small>
                      </td>
                      <td>
                        <span className={`tag ${py ? 'tag-purple' : 'tag-neutral'}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                          {py && <FileCode size={12} />}
                          {typeLabel(s)}
                        </span>
                      </td>
                      <td>{inst}{tf ? <small>{tf}</small> : null}</td>
                      <td>{fmtDate(when(s))}</td>
                      <td>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                          <span className="tag draft">{s.status || s.meta?.status || 'DRAFT'}</span>
                          {py && (
                            <small className="muted" style={{ fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                              <CheckCircle2 size={11} className="positive" /> AST Analyzed (Execution Blocked)
                            </small>
                          )}
                        </div>
                      </td>
                      <td>
                        <div className="row-actions">
                          <button title="Load / edit" onClick={() => onLoad(s)}><Pencil size={14} /> Edit</button>
                          <button title="Duplicate" onClick={() => onDuplicate(s)}><Copy size={14} /></button>
                          <button title="Validate" onClick={() => onValidate(s)}><ShieldCheck size={14} /></button>
                          <button title="Start paper trading (simulated, no real orders)" onClick={() => onPaper(s)}><Play size={14} /> Paper</button>
                          {confirmDel === s.id
                            ? <button className="danger" onClick={() => { onDelete(s); setConfirmDel(null); }}>Confirm delete</button>
                            : <button title="Delete" onClick={() => setConfirmDel(s.id)}><Trash2 size={14} /></button>}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="hint" style={{ marginTop: 12 }}>Live trading is never started from this list. It requires the separate go-live checklist and typed confirmation in the Strategy Builder.</div>
      </div>
    </section>
  );
}
