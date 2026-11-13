import React from 'react';
import { Layers3, FlaskConical, TrendingUp, Shield, Download } from 'lucide-react';

function ReportMetric({ icon: Icon, label, value, note, good }) {
  return (
    <div className="metric-card">
      <div className="metric-top">
        <span>{label}</span>
        <Icon size={18} />
      </div>
      <strong>{value}</strong>
      <small className={good ? 'positive' : ''}>{note || 'Workspace metric'}</small>
    </div>
  );
}

export default function ReportsPage({ strategies, trades, tradeStats }) {
  return (
    <section className="content">
      <div className="welcome-row">
        <div>
          <div className="eyebrow">ANALYTICS & AUDIT</div>
          <h1>Reports & Export</h1>
          <p className="sub">Performance metrics and paper activity journal.</p>
        </div>
      </div>

      <div className="metric-grid">
        <ReportMetric icon={Layers3} label="Strategies Saved" value={strategies.length} />
        <ReportMetric icon={FlaskConical} label="Simulated Executions" value={trades.length} />
        <ReportMetric
          icon={TrendingUp}
          label="Simulated Net P&L"
          value={(tradeStats.totalPnl >= 0 ? '+₹' : '-₹') + Math.abs(tradeStats.totalPnl).toLocaleString('en-IN')}
          good={tradeStats.totalPnl >= 0}
        />
        <ReportMetric icon={Shield} label="Live Orders" value="0" note="Protected in Paper Mode" />
      </div>

      <div className="panel section-card">
        <h3>Export trade journal</h3>
        <p className="sub">Download your paper trading events as a CSV file for backtesting analysis.</p>
        <button
          className="outline"
          onClick={() => {
            const csv =
              'id,strategy,contract,symbol,side,quantity,entryPrice,currentPrice,pnl,status,createdAt\n' +
              trades
                .map(t =>
                  [t.id, t.strategy, t.contract, t.symbol, t.side, t.quantity, t.entryPrice, t.currentPrice, t.pnl, t.status, t.createdAt]
                    .map(x => '"' + String(x ?? '').replaceAll('"', '""') + '"')
                    .join(',')
                )
                .join('\n');
            const a = document.createElement('a');
            a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
            a.download = 'alpha24-paper-trades.csv';
            a.click();
            URL.revokeObjectURL(a.href);
          }}
        >
          <Download size={16} /> Export CSV Journal
        </button>
      </div>
    </section>
  );
}

