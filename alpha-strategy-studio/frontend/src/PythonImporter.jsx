/**
 * PythonImporter.jsx
 * ==================
 * Python Strategy Importer — FIRST RELEASE (static analysis only).
 *
 * SECURITY NOTICE displayed to user:
 *   "Python code is analysed only and never run in this version."
 *
 * Rules enforced:
 *  - No file or source text is stored in browser localStorage / sessionStorage / cookies
 *  - Only /api/python-import/analyse and /api/python-import/upload are called
 *  - After saving to My Strategies, the existing /api/strategies POST is used
 *  - No hardcoded colours — all from CSS variables
 */
import React, { useCallback, useRef, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight,
  Code2, FileCode2, Info, Loader2, Shield, Upload, X, XCircle,
  Sliders, ArrowRight, Play
} from 'lucide-react';

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** Line-numbered textarea editor */
function PyEditor({ value, onChange }) {
  const lines = value ? value.split('\n') : [];
  const displayLines = Math.max(lines.length, 20);
  const taRef = useRef(null);
  const lnRef = useRef(null);

  function handleScroll(e) {
    if (lnRef.current) lnRef.current.scrollTop = e.target.scrollTop;
  }

  return (
    <div className="py-editor">
      <div className="py-editor__lines" aria-hidden="true" ref={lnRef}>
        {Array.from({ length: displayLines }, (_, i) => (
          <div key={i} className="py-editor__ln">{i + 1}</div>
        ))}
      </div>
      <textarea
        ref={taRef}
        className="py-editor__area"
        value={value}
        onChange={e => onChange(e.target.value)}
        onScroll={handleScroll}
        spellCheck={false}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        placeholder={'# Paste your Python strategy here…\n# Example:\n#\n# def on_bar(candles, context):\n#     if candles["rsi"] < 30:\n#         return "BUY"'}
        aria-label="Python strategy source code"
      />
    </div>
  );
}

/** Status badge */
function StatusBadge({ status }) {
  const map = {
    converted:               { cls: 'pi-badge pi-badge--ok',      text: 'Converted' },
    partially_converted:     { cls: 'pi-badge pi-badge--warn',    text: 'Partial' },
    could_not_be_represented:{ cls: 'pi-badge pi-badge--error',   text: 'Not representable' },
    allowed:                 { cls: 'pi-badge pi-badge--ok',      text: 'Allowed' },
    unsupported:             { cls: 'pi-badge pi-badge--warn',    text: 'Unsupported' },
    dangerous:               { cls: 'pi-badge pi-badge--error',   text: 'Dangerous' },
    unknown:                 { cls: 'pi-badge pi-badge--neutral',  text: 'Unknown' },
  };
  const { cls, text } = map[status] ?? { cls: 'pi-badge pi-badge--neutral', text: status };
  return <span className={cls}>{text}</span>;
}

/** Collapsible report section */
function ReportSection({ title, count, icon: Icon, children, defaultOpen = false, variant }) {
  const [open, setOpen] = useState(defaultOpen);
  const varCls = variant ? `pi-section pi-section--${variant}` : 'pi-section';
  return (
    <div className={varCls}>
      <button
        type="button"
        className="pi-section__toggle"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
      >
        {Icon && <Icon size={15} className="pi-section__icon" />}
        <span className="pi-section__title">{title}</span>
        {count != null && <span className="pi-section__count">{count}</span>}
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      {open && <div className="pi-section__body">{children}</div>}
    </div>
  );
}

/** Displays a single rule row */
function RuleRow({ rule, index }) {
  return (
    <div className="pi-rule-row">
      <span className="pi-rule-row__num">{index + 1}</span>
      <code className="pi-rule-row__source">{rule.source || '—'}</code>
      <StatusBadge status={rule.status} />
      {rule.reason && (
        <span className="pi-rule-row__reason">{rule.reason}</span>
      )}
      <span className="pi-rule-row__line">L{rule.line}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function PythonImporter({ notify, loadData, API, onDraftCreated, setPage }) {
  const [mode, setMode] = useState('paste'); // 'paste' | 'upload'
  const [source, setSource]       = useState('');
  const [filename, setFilename]   = useState('strategy.py');
  const [report, setReport]       = useState(null);
  const [loading, setLoading]     = useState(false);
  const [saving, setSaving]       = useState(false);
  const [savedStrategy, setSavedStrategy] = useState(null);
  const fileInputRef              = useRef(null);

  // User-configurable strategy parameters
  const [userConfig, setUserConfig] = useState({
    underlying: 'NIFTY 50',
    instrumentType: 'OPTION',
    expiry: 'WEEKLY',
    optionType: 'CE',
    strikeDepth: 'ATM',
    quantity: 1,
    stopLossPoints: 25,
    targetPoints: 50,
    maxDailyLoss: 2000,
    maxCapital: 25000,
  });

  // ── Analyse ──────────────────────────────────────────────────────────────

  async function analyse() {
    if (mode === 'paste' && !source.trim()) {
      notify('Paste some Python code first');
      return;
    }
    setLoading(true);
    setReport(null);
    setSavedStrategy(null);
    try {
      let r;
      if (mode === 'paste') {
        r = await fetch(`${API}/python-import/analyse`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source, filename }),
        });
      } else {
        // Upload mode
        const fileInput = fileInputRef.current;
        if (!fileInput?.files?.length) {
          notify('Select a .py file first');
          setLoading(false);
          return;
        }
        const fd = new FormData();
        fd.append('file', fileInput.files[0]);
        r = await fetch(`${API}/python-import/upload`, {
          method: 'POST',
          body: fd,
        });
      }
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Analysis failed');
      setReport(data);
    } catch (e) {
      notify(e.message);
    } finally {
      setLoading(false);
    }
  }

  // ── Save to My Strategies ────────────────────────────────────────────────

  async function saveToStrategies() {
    if (!report) return null;
    setSaving(true);

    const baseStrat = report.draftStrategy
      ? structuredClone(report.draftStrategy)
      : _buildStubStrategy(report, source, filename);

    const strategyToSave = {
      ...baseStrat,
      instrument: userConfig.underlying,
      execution: {
        ...(baseStrat.execution || {}),
        quantity: Number(userConfig.quantity) || 1,
        instrumentType: userConfig.instrumentType,
        ...(userConfig.instrumentType === 'OPTION' ? {
          optionType: userConfig.optionType,
          strikeDepth: userConfig.strikeDepth,
          expiry: userConfig.expiry,
        } : {}),
      },
      risk: {
        ...(baseStrat.risk || {}),
        maxDailyLoss: Number(userConfig.maxDailyLoss) || 2000,
        maxCapital: Number(userConfig.maxCapital) || 25000,
      },
      stopLoss: {
        ...(baseStrat.stopLoss || {}),
        points: Number(userConfig.stopLossPoints) || 25,
      },
      targets: (baseStrat.targets && baseStrat.targets.length > 0)
        ? baseStrat.targets.map((tgt, idx) => idx === baseStrat.targets.length - 1 ? { ...tgt, value: Number(userConfig.targetPoints) || 50 } : tgt)
        : [{ id: 'tgt-1', quantity: Number(userConfig.quantity) || 1, order: 1, enabled: true, label: 'Target', value: Number(userConfig.targetPoints) || 50 }],
      meta: {
        ...(baseStrat.meta || {}),
        symbol: userConfig.underlying,
        maskedSource: report.maskedSource,
        contentHash:  report.contentHash,
        analysisReport: {
          syntaxOk:              report.syntaxOk,
          importable:            report.importable,
          entryPoints:           report.entryPoints,
          indicators:            report.indicators,
          dangerousConstructsCount: report.dangerousConstructs?.length ?? 0,
          ambiguousConstructsCount: report.ambiguousConstructs?.length ?? 0,
          rulesConverted:   (report.rules || []).filter(r => r.status === 'converted').length,
          rulesPartial:     (report.rules || []).filter(r => r.status === 'partially_converted').length,
          rulesBlocked:     (report.rules || []).filter(r => r.status === 'could_not_be_represented').length,
          secretsFound:     report.secretsFound?.length ?? 0,
          summary:          report.summary,
        },
      },
    };

    try {
      const r = await fetch(`${API}/strategies`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy: strategyToSave }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'Save failed');
      notify(`Saved "${strategyToSave.name}" to My Strategies`);
      setSavedStrategy(strategyToSave);
      if (loadData) loadData();
      return strategyToSave;
    } catch (e) {
      notify(e.message);
      return null;
    } finally {
      setSaving(false);
    }
  }

  // ── Create visual draft ──────────────────────────────────────────────────

  async function createDraft() {
    if (!report?.draftStrategy) return;
    // Save first, and only proceed to Builder if save succeeded
    const saved = await saveToStrategies();
    if (!saved) return;
    if (onDraftCreated) onDraftCreated(saved);
  }

  // ── File upload helpers ──────────────────────────────────────────────────

  const onFileChange = useCallback((e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (!f.name.endsWith('.py')) {
      notify('Only .py files are accepted');
      e.target.value = '';
      return;
    }
    if (f.size > 200 * 1024) {
      notify('File too large (max 200 KB)');
      e.target.value = '';
      return;
    }
    setFilename(f.name);
    setReport(null);
    setSavedStrategy(null);
  }, [notify]);

  function removeFile() {
    if (fileInputRef.current) fileInputRef.current.value = '';
    setFilename('strategy.py');
    setReport(null);
    setSavedStrategy(null);
  }

  function discard() {
    setSource('');
    setFilename('strategy.py');
    setReport(null);
    setSavedStrategy(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  // ── Render ───────────────────────────────────────────────────────────────

  const hasFile     = mode === 'upload' && fileInputRef.current?.files?.length > 0;
  const canAnalyse  = mode === 'paste' ? source.trim().length > 0 : hasFile;
  const canSave     = report?.syntaxOk && report?.importable;
  const canDraft    = canSave && report?.draftStrategy != null;

  return (
    <section className="content">
      {/* Header */}
      <div className="welcome-row" style={{ marginBottom: 16 }}>
        <div>
          <div className="eyebrow">STRATEGY BUILDER</div>
          <h1 className="iifl-page-title">Python Import</h1>
          <p className="sub" style={{ marginTop: 4 }}>
            Paste or upload a Python strategy file for static analysis and mapping.
          </p>
        </div>
      </div>

      {/* Security notice & Execution Sandbox Status */}
      <div className="pi-runtime-callout">
        <div className="pi-runtime-callout__title">
          <Shield size={16} /> Execution Sandbox & Runtime Status
        </div>
        <div className="pi-runtime-callout__grid">
          <div className="pi-runtime-path pi-runtime-path--active">
            <div className="pi-runtime-path__header">
              <span className="pi-runtime-path__title">Path 1: Static AST Rule Translation</span>
              <span className="pi-badge pi-badge--ok">Supported</span>
            </div>
            <p className="pi-muted" style={{ margin: 0 }}>
              Parses imports, indicators, and conditions via Python AST and converts them into Strategy Studio visual rules and safe draft metadata. No code is executed.
            </p>
          </div>
          <div className="pi-runtime-path pi-runtime-path--disabled">
            <div className="pi-runtime-path__header">
              <span className="pi-runtime-path__title">Path 2: Arbitrary Python Execution</span>
              <span className="pi-badge pi-badge--error">Disabled / Unavailable</span>
            </div>
            <p className="pi-muted" style={{ margin: 0 }}>
              Direct arbitrary Python execution in live or paper trading is disabled because an isolated container runtime (Docker / gVisor) is not present on this host.
            </p>
          </div>
        </div>
      </div>

      <div className="pi-notice pi-notice--info">
        <Shield size={15} />
        <span>
          <b>Analysis only:</b> Python code is analysed statically and{' '}
          <b>never executed, imported, or run</b> in this version. No browser storage is used.
        </span>
      </div>

      {/* Mode tabs */}
      <div className="pi-tabs">
        <button
          type="button"
          className={`pi-tab ${mode === 'paste' ? 'pi-tab--active' : ''}`}
          onClick={() => { setMode('paste'); setReport(null); }}
        >
          <Code2 size={14} /> Paste code
        </button>
        <button
          type="button"
          className={`pi-tab ${mode === 'upload' ? 'pi-tab--active' : ''}`}
          onClick={() => { setMode('upload'); setReport(null); }}
        >
          <Upload size={14} /> Upload .py file
        </button>
      </div>

      {/* Editor / upload area */}
      <div className="panel pi-input-panel">
        {mode === 'paste' ? (
          <PyEditor value={source} onChange={v => { setSource(v); setReport(null); }} />
        ) : (
          <div className="pi-upload-area">
            <input
              ref={fileInputRef}
              type="file"
              accept=".py"
              id="py-file-input"
              className="pi-file-input"
              onChange={onFileChange}
            />
            {!hasFile ? (
              <label htmlFor="py-file-input" className="pi-upload-label">
                <FileCode2 size={28} />
                <span>Click to choose a <code>.py</code> file, or drag it here</span>
                <small>UTF-8, max 200 KB</small>
              </label>
            ) : (
              <div className="pi-file-info">
                <FileCode2 size={18} />
                <span className="pi-file-name">{filename}</span>
                <button type="button" className="pi-file-remove" onClick={removeFile} title="Remove file">
                  <X size={14} />
                </button>
              </div>
            )}
          </div>
        )}

        <div className="pi-toolbar">
          <button
            type="button"
            className="primary"
            onClick={analyse}
            disabled={loading || !canAnalyse}
          >
            {loading ? <Loader2 size={15} className="spin" /> : <Code2 size={15} />}
            {loading ? 'Analysing…' : 'Analyse'}
          </button>
          {(source || hasFile) && (
            <button type="button" className="outline" onClick={discard}>
              Discard
            </button>
          )}
        </div>
      </div>

      {/* ── Analysis report ── */}
      {report && (
        <div className="pi-report">
          {/* Summary banner */}
          <div className={`pi-summary ${report.importable ? 'pi-summary--ok' : 'pi-summary--error'}`}>
            {report.importable
              ? <CheckCircle2 size={16} />
              : <XCircle size={16} />
            }
            <div>
              <b>{report.importable ? 'Importable' : 'Not importable'}</b>
              <span>{report.summary}</span>
            </div>
          </div>

          {/* Syntax */}
          <ReportSection
            title="Syntax"
            icon={Code2}
            defaultOpen
            variant={report.syntaxOk ? 'ok' : 'error'}
          >
            {report.syntaxOk ? (
              <p className="pi-ok-msg"><CheckCircle2 size={13} /> No syntax errors.</p>
            ) : (
              <div className="pi-error-block">
                <b>Syntax error at line {report.syntaxError?.line}, col {report.syntaxError?.column}</b>
                <code>{report.syntaxError?.message}</code>
                {report.syntaxError?.text && (
                  <code className="pi-error-src">{report.syntaxError.text.trim()}</code>
                )}
              </div>
            )}
          </ReportSection>

          {/* Entry points */}
          <ReportSection
            title="Detected entry points"
            icon={Info}
            count={report.entryPoints?.length ?? 0}
            defaultOpen
          >
            {!report.entryPoints?.length ? (
              <p className="pi-muted">No recognised entry-point functions or classes found.</p>
            ) : (
              <div className="pi-table">
                <div className="pi-table__header">
                  <span>Type</span><span>Name</span><span>Line</span><span>Args / Methods</span>
                </div>
                {report.entryPoints.map((ep, i) => (
                  <div key={i} className="pi-table__row">
                    <span><StatusBadge status="allowed" />{ep.type}</span>
                    <code>{ep.name}</code>
                    <span>L{ep.line}</span>
                    <code className="pi-muted">
                      {ep.args?.join(', ') || ep.entry_methods?.join(', ') || '—'}
                    </code>
                  </div>
                ))}
              </div>
            )}
            {report.indicators?.length > 0 && (
              <div className="pi-indicator-chips">
                <span className="pi-label">Indicators detected:</span>
                {report.indicators.map(ind => (
                  <span key={ind} className="pi-ind-chip">{ind}</span>
                ))}
              </div>
            )}
          </ReportSection>

          {/* Dependencies */}
          <ReportSection
            title="Imports & dependencies"
            icon={Info}
            count={report.imports?.length ?? 0}
            variant={report.imports?.some(i => i.status === 'dangerous') ? 'error' : undefined}
          >
            {!report.imports?.length ? (
              <p className="pi-muted">No imports.</p>
            ) : (
              <div className="pi-table">
                <div className="pi-table__header">
                  <span>Module</span><span>Alias</span><span>Status</span><span>Line</span>
                </div>
                {report.imports.map((imp, i) => (
                  <div key={i} className="pi-table__row">
                    <code>{imp.module}</code>
                    <code className="pi-muted">{imp.alias !== imp.module ? imp.alias : '—'}</code>
                    <StatusBadge status={imp.status} />
                    <span>L{imp.line}</span>
                  </div>
                ))}
              </div>
            )}
          </ReportSection>

          {/* Dangerous constructs */}
          {report.dangerousConstructs?.length > 0 && (
            <ReportSection
              title="Unsupported / dangerous constructs"
              icon={XCircle}
              count={report.dangerousConstructs.length}
              defaultOpen
              variant="error"
            >
              <p className="pi-warn-note">
                These constructs prevent import. Remove them and re-analyse.
              </p>
              <div className="pi-construct-list">
                {report.dangerousConstructs.map((dc, i) => (
                  <div key={i} className="pi-construct-row">
                    <XCircle size={13} className="pi-construct-row__icon" />
                    <code>{dc.name || dc.type}</code>
                    <span>L{dc.line}</span>
                    <span className="pi-muted">{dc.detail}</span>
                  </div>
                ))}
              </div>
            </ReportSection>
          )}

          {/* Secrets warning */}
          {report.secretsFound?.length > 0 && (
            <ReportSection
              title="Secrets warning"
              icon={AlertTriangle}
              count={report.secretsFound.length}
              defaultOpen
              variant="warn"
            >
              <p className="pi-warn-note">
                Credential-looking strings were found and masked. Remove credentials from source code.
                They are <b>never stored unmasked</b>.
              </p>
              <div className="pi-construct-list">
                {report.secretsFound.map((sf, i) => (
                  <div key={i} className="pi-construct-row">
                    <AlertTriangle size={13} className="pi-construct-row__icon pi-warn-icon" />
                    <span>L{sf.line}</span>
                    <span className="pi-muted">{sf.reason}</span>
                  </div>
                ))}
              </div>
            </ReportSection>
          )}

          {/* Ambiguous logic */}
          {report.ambiguousConstructs?.length > 0 && (
            <ReportSection
              title="Ambiguous logic"
              icon={AlertTriangle}
              count={report.ambiguousConstructs.length}
              variant="warn"
            >
              <div className="pi-construct-list">
                {report.ambiguousConstructs.map((ac, i) => (
                  <div key={i} className="pi-construct-row">
                    <AlertTriangle size={13} className="pi-construct-row__icon pi-warn-icon" />
                    <code>{ac.type}</code>
                    <span>L{ac.line}</span>
                    <span className="pi-muted">{ac.detail}</span>
                  </div>
                ))}
              </div>
            </ReportSection>
          )}

          {/* Rule mapping */}
          <ReportSection
            title="Rule mapping"
            icon={Info}
            count={report.rules?.length ?? 0}
            defaultOpen={!!report.rules?.length}
          >
            {!report.rules?.length ? (
              <p className="pi-muted">No if-condition rules detected.</p>
            ) : (
              <>
                <p className="pi-info-note">
                  Only simple indicator comparisons are converted automatically.
                  Partial and not-representable rules need manual recreation in the Rule Builder.
                </p>
                <div className="pi-rule-table">
                  {report.rules.map((rule, i) => (
                    <RuleRow key={i} rule={rule} index={i} />
                  ))}
                </div>
              </>
            )}
          </ReportSection>

          {/* User Configuration Section for Instrument, Options, & Risk */}
          {report.importable && (
            <div className="pi-config-panel">
              <div className="pi-config-panel__title">
                <Sliders size={15} /> Configure Strategy Parameters
              </div>
              <div className="pi-config-grid">
                <div className="pi-config-field">
                  <label>Underlying</label>
                  <select
                    value={userConfig.underlying}
                    onChange={e => setUserConfig(c => ({ ...c, underlying: e.target.value }))}
                  >
                    <option value="NIFTY 50">NIFTY 50</option>
                    <option value="BANKNIFTY">BANKNIFTY</option>
                    <option value="FINNIFTY">FINNIFTY</option>
                    <option value="SENSEX">SENSEX</option>
                    <option value="CRUDEOIL">CRUDEOIL</option>
                  </select>
                </div>
                <div className="pi-config-field">
                  <label>Instrument Type</label>
                  <select
                    value={userConfig.instrumentType}
                    onChange={e => setUserConfig(c => ({ ...c, instrumentType: e.target.value }))}
                  >
                    <option value="OPTION">Options (CE/PE)</option>
                    <option value="FUTURE">Futures</option>
                    <option value="EQUITY">Equity (Cash)</option>
                  </select>
                </div>
                {userConfig.instrumentType === 'OPTION' && (
                  <>
                    <div className="pi-config-field">
                      <label>Option Type</label>
                      <select
                        value={userConfig.optionType}
                        onChange={e => setUserConfig(c => ({ ...c, optionType: e.target.value }))}
                      >
                        <option value="CE">Call (CE)</option>
                        <option value="PE">Put (PE)</option>
                      </select>
                    </div>
                    <div className="pi-config-field">
                      <label>Strike / OTM Depth</label>
                      <select
                        value={userConfig.strikeDepth}
                        onChange={e => setUserConfig(c => ({ ...c, strikeDepth: e.target.value }))}
                      >
                        <option value="ITM2">ITM 2 (Deep In-the-Money)</option>
                        <option value="ITM1">ITM 1 (In-the-Money)</option>
                        <option value="ATM">ATM (At-the-Money)</option>
                        <option value="OTM1">OTM 1 (Near Out-of-the-Money)</option>
                        <option value="OTM2">OTM 2 (Out-of-the-Money)</option>
                        <option value="OTM3">OTM 3 (Far Out-of-the-Money)</option>
                      </select>
                    </div>
                    <div className="pi-config-field">
                      <label>Expiry</label>
                      <select
                        value={userConfig.expiry}
                        onChange={e => setUserConfig(c => ({ ...c, expiry: e.target.value }))}
                      >
                        <option value="WEEKLY">Current Weekly Expiry</option>
                        <option value="MONTHLY">Monthly Expiry</option>
                      </select>
                    </div>
                  </>
                )}
                <div className="pi-config-field">
                  <label>Lot Quantity</label>
                  <input
                    type="number"
                    min="1"
                    max="100"
                    value={userConfig.quantity}
                    onChange={e => setUserConfig(c => ({ ...c, quantity: Math.max(1, parseInt(e.target.value) || 1) }))}
                  />
                </div>
                <div className="pi-config-field">
                  <label>Stop Loss (Pts)</label>
                  <input
                    type="number"
                    min="1"
                    step="0.5"
                    value={userConfig.stopLossPoints}
                    onChange={e => setUserConfig(c => ({ ...c, stopLossPoints: parseFloat(e.target.value) || 0 }))}
                  />
                </div>
                <div className="pi-config-field">
                  <label>Target (Pts)</label>
                  <input
                    type="number"
                    min="1"
                    step="0.5"
                    value={userConfig.targetPoints}
                    onChange={e => setUserConfig(c => ({ ...c, targetPoints: parseFloat(e.target.value) || 0 }))}
                  />
                </div>
                <div className="pi-config-field">
                  <label>Max Daily Loss (₹)</label>
                  <input
                    type="number"
                    min="100"
                    step="100"
                    value={userConfig.maxDailyLoss}
                    onChange={e => setUserConfig(c => ({ ...c, maxDailyLoss: parseFloat(e.target.value) || 0 }))}
                  />
                </div>
                <div className="pi-config-field">
                  <label>Max Capital (₹)</label>
                  <input
                    type="number"
                    min="1000"
                    step="1000"
                    value={userConfig.maxCapital}
                    onChange={e => setUserConfig(c => ({ ...c, maxCapital: parseFloat(e.target.value) || 0 }))}
                  />
                </div>
              </div>
            </div>
          )}

          {/* Action buttons */}
          <div className="pi-actions">
            {canDraft && (
              <button
                type="button"
                className="primary"
                onClick={createDraft}
                disabled={saving}
              >
                {saving ? <Loader2 size={14} className="spin" /> : <CheckCircle2 size={14} />}
                Create visual draft
              </button>
            )}
            {canSave && (
              <button
                type="button"
                className="outline"
                onClick={saveToStrategies}
                disabled={saving}
              >
                {saving ? <Loader2 size={14} className="spin" /> : <FileCode2 size={14} />}
                Save to My Strategies
              </button>
            )}
            <button type="button" className="outline" onClick={discard}>
              Discard
            </button>
          </div>

          {/* Post-save action banner */}
          {savedStrategy && (
            <div className="pi-post-save-banner">
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <CheckCircle2 size={18} style={{ color: 'var(--profit-text, #22c55e)' }} />
                <span>
                  Strategy <b>"{savedStrategy.name}"</b> has been saved to <b>My Strategies</b>.
                </span>
              </div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <button
                  type="button"
                  className="primary"
                  onClick={() => onDraftCreated && onDraftCreated(savedStrategy)}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', fontSize: 12 }}
                >
                  <ArrowRight size={14} /> Open in Strategy Builder
                </button>
                {setPage && (
                  <button
                    type="button"
                    className="outline"
                    onClick={() => setPage('Saved Strategies')}
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', fontSize: 12 }}
                  >
                    View in My Strategies
                  </button>
                )}
                <button
                  type="button"
                  className="outline"
                  disabled
                  title="Direct execution of arbitrary Python runtime is disabled without an isolated sandbox; convert to visual Rule Builder draft first to simulate in Paper Trading."
                  style={{ opacity: 0.6, cursor: 'not-allowed', display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', fontSize: 12 }}
                >
                  <Play size={13} /> Paper Trading (Sandbox Req.)
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Helper: build a minimal stub strategy when there is no draft
// ---------------------------------------------------------------------------

function _buildStubStrategy(report, source, filename) {
  const id = crypto.randomUUID ? crypto.randomUUID() : `import-${Date.now()}`;
  const name = filename.replace(/\.py$/i, '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  return {
    id,
    name: `${name} (Python Import)`,
    strategyType: 'RULE_BUILDER',
    meta: {
      id,
      name: `${name} (Python Import)`,
      status: 'DRAFT',
      sourceType: 'PYTHON_IMPORT',
      importVersion: 1,
      validationStatus: report.importable ? 'IMPORTABLE' : 'NOT_IMPORTABLE',
      contentHash: report.contentHash,
      maskedSource: report.maskedSource,
      analysisReport: {
        syntaxOk: report.syntaxOk,
        importable: report.importable,
        summary: report.summary,
        secretsFound: report.secretsFound?.length ?? 0,
        dangerousConstructsCount: report.dangerousConstructs?.length ?? 0,
      },
    },
    entry: { logic: 'AND', conditions: [] },
    exit: { logic: 'OR', conditions: [] },
    indicators: [],
    risk: { maxDailyLoss: 500, maxCapital: 10000, maxTradesPerDay: 3, maxOpenPositions: 1 },
    execution: { quantity: 1 },
  };
}

export default PythonImporter;
