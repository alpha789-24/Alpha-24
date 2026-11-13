import React from 'react';
import { Copy, Download, Trash2 } from 'lucide-react';

export function DashboardStrategyCards({
  strategies,
  paperWorkerRuns,
  trades,
  iifl,
  onSelectStrategy,
  onDuplicateStrategy,
  onDeleteStrategy,
  onExportStrategy
}) {
  return (
    <div className="rb-dashboard-cards-grid">
      {(strategies || []).map(s => {
        const isRule = s.strategyType === 'RULE_BUILDER';
        const status = (isRule ? s.meta?.status : s.status) || 'DRAFT';
        const sym = (isRule ? (s.execution?.instrument?.underlying || s.meta?.symbol) : s.instrument) || 'NIFTY 50';
        const firstEntry = isRule ? s.rules?.entry?.[0] : null;
        const direction = isRule ? (firstEntry?.side || 'BUY') : (s.positions?.[0]?.side || 'BUY');

        const slLabel = isRule
          ? `${s.stopLoss?.method || 'fixed_points'} (${s.stopLoss?.points || s.stopLoss?.percent || s.stopLoss?.level || s.stopLoss?.indicatorId || ''})`
          : (s.strategyType === 'LEVEL_SAR' ? `${s.sar?.slPoints || 1} pts` : 'Universal SL');

        const enabledTargets = isRule ? (s.targets || []).filter(t => t.enabled) : [];
        const tgtSummary = isRule
          ? `${enabledTargets.length} active (${enabledTargets.map(t => t.level || `${t.value}${t.unit === 'percent' ? '%' : 'pt'}`).join(' / ')})`
          : (s.strategyType === 'LEVEL_SAR' ? `${s.sar?.tgtPoints || 1.5} pts` : 'Leg target');

        const reentrySummary = isRule
          ? (s.reentry?.enabled ? `ON (max ${s.reentry.maxPerDay}/day, ${s.reentry.cooldownMinutes}m CD)` : 'OFF')
          : (s.repair?.enabled ? `Repair ON (${s.repair.maxAttempts}x)` : 'OFF');

        const workerRun = (paperWorkerRuns || []).find(r => r.strategyId === s.id);
        const stratTrades = (trades || []).filter(t => t.strategy === s.name || t.strategyId === s.id);
        const openPos = workerRun?.openPosition || stratTrades.find(t => t.status === 'OPEN');

        const realizedPnl = workerRun
          ? (workerRun.totalRealizedPnl || 0)
          : stratTrades.filter(t => t.status === 'CLOSED').reduce((acc, t) => acc + (t.pnl || 0), 0);
        const unrealizedPnl = workerRun
          ? (workerRun.unrealizedPnl || 0)
          : stratTrades.filter(t => t.status === 'OPEN').reduce((acc, t) => acc + (t.pnl || 0), 0);

        const effectiveStatus = workerRun?.status === 'RUNNING'
          ? 'PAPER'
          : workerRun?.status === 'PAUSED'
          ? 'PAUSED'
          : workerRun?.status === 'STOPPED_RISK_LIMIT'
          ? 'STOPPED'
          : status;

        return (
          <div key={s.id} className="rb-dash-card">
            <div className="rb-dash-card-top">
              <div>
                <span className={`rb-status-badge status-${String(effectiveStatus).toLowerCase()}`}>
                  {effectiveStatus}
                </span>
                <span className={`rb-dir-badge ${direction === 'BUY' ? 'buy' : 'sell'}`}>
                  {direction}
                </span>
                <span className="rb-type-pill">{s.strategyType || 'INDICATOR'}</span>
              </div>
              <div className="rb-card-menu-btns">
                <button type="button" title="Edit Strategy" onClick={() => onSelectStrategy(s)}>Edit</button>
                <button type="button" title="Duplicate Strategy" onClick={() => onDuplicateStrategy(s.id)}><Copy size={13} /></button>
                <button type="button" title="Export Clean JSON" onClick={() => onExportStrategy(s)}><Download size={13} /></button>
                <button type="button" className="danger" title="Delete Strategy" onClick={() => onDeleteStrategy(s.id)}><Trash2 size={13} /></button>
              </div>
            </div>

            <h4 className="rb-dash-card-title" onClick={() => onSelectStrategy(s)}>{s.name}</h4>
            <p className="rb-dash-card-sub">{(isRule ? s.meta?.description : s.description) || 'Deterministic strategy configuration'}</p>

            <div className="rb-dash-meta-grid">
              <div><small>Symbol</small><b>{sym}</b></div>
              <div><small>Stop Loss</small><b>{slLabel}</b></div>
              <div><small>Targets</small><b>{tgtSummary}</b></div>
              <div><small>Re-entry</small><b>{reentrySummary}</b></div>
              <div><small>Broker / Mode</small><b>{iifl?.sessionPresent ? 'IIFL Active' : 'IIFL Offline'} · PAPER</b></div>
              <div>
                <small>Active Position</small>
                <b className={openPos ? (openPos.side === 'BUY' ? 'positive' : 'negative') : ''}>
                  {openPos ? `${openPos.side} ${openPos.remainingQuantity || openPos.quantity} lots @ ₹${openPos.entryPrice}` : 'Flat (None)'}
                </b>
              </div>
            </div>

            {workerRun?.message && (
              <div className="rb-dash-signal-note">
                Last Event: {workerRun.message}
              </div>
            )}

            <div className="rb-dash-pnl-bar">
              <div>
                <small>Realized P&L</small>
                <strong className={realizedPnl >= 0 ? 'positive' : 'negative'}>
                  {realizedPnl >= 0 ? '+₹' : '-₹'}{Math.abs(realizedPnl).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                </strong>
              </div>
              <div>
                <small>Unrealized P&L</small>
                <strong className={unrealizedPnl >= 0 ? 'positive' : 'negative'}>
                  {unrealizedPnl >= 0 ? '+₹' : '-₹'}{Math.abs(unrealizedPnl).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                </strong>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

