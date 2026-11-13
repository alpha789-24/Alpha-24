import React from 'react';
import { ArrowUpRight, ArrowDownRight, Trash2 } from 'lucide-react';

const formatPrice = (val, decimals = 2) => {
  if (val == null || isNaN(val)) return '--';
  return Number(val).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  });
};

const displaySymbol = (s) => String(s ?? '').replace(/\.(NS|BO)$/i, '').replace(/^\^/, '');

export const MarketCard = React.memo(function MarketCard({
  quote: q,
  isSelected,
  onSelect,
  isCustom,
  onRemove,
  flash
}) {
  const isPos = (q.change || 0) >= 0;
  const rangeSpan = ((q.high ?? 0) - (q.low ?? 0)) || 1;
  const markerPos = (q.ltp != null && q.low != null)
    ? Math.max(0, Math.min(100, (((q.ltp - q.low) / rangeSpan) * 100)))
    : 50;

  const symClean = displaySymbol(q.symbol || q.ticker);
  const companyName = q.displayName || q.name || symClean;

  return (
    <div
      className={`live-market-card ${isSelected ? 'selected' : ''} ${flash === 'up' ? 'flash-up' : flash === 'down' ? 'flash-down' : ''}`}
      onClick={onSelect}
    >
      {/* Card Header: Symbol + Exchange, % change chip & Remove button */}
      <div className="card-header-row">
        <div className="card-title-group">
          <div className="card-symbol-line">
            <h4 className="card-symbol">{symClean}</h4>
            <span className="card-exchange-tag">{q.exchange || 'NSE'}</span>
          </div>
          <small className="card-company-name" title={companyName}>
            {companyName}
          </small>
        </div>

        <div className="card-header-actions">
          {!q.loading && !q.noData && (
            <span className={`badge-pill ${isPos ? 'positive' : 'negative'}`}>
              {isPos ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
              {isPos ? '+' : ''}{Number(q.changePercent ?? 0).toFixed(2)}%
            </span>
          )}
          {isCustom && (
            <button
              type="button"
              className="card-remove-btn"
              onClick={(e) => {
                e.stopPropagation();
                onRemove(q.ticker || q.symbol);
              }}
              title={`Remove ${symClean} from custom watchlist`}
              aria-label={`Remove ${symClean}`}
            >
              <Trash2 size={13} />
            </button>
          )}
        </div>
      </div>

      {/* Card Body: Skeleton when loading, No data fallback, or Price + Range + Meta */}
      {q.loading ? (
        <div className="card-skeleton">
          <div className="skeleton-line skeleton-price" />
          <div className="skeleton-line skeleton-bar" />
          <div className="skeleton-line skeleton-meta" />
        </div>
      ) : q.noData ? (
        <div className="card-no-data">
          <span>No live data available for this symbol</span>
        </div>
      ) : (
        <>
          <div className="card-price-row">
            <strong className="card-price-main tabular-nums">
              {q.ltp != null ? `₹${formatPrice(q.ltp)}` : '--'}
            </strong>
            <small className={`card-price-diff tabular-nums ${isPos ? 'positive' : 'negative'}`}>
              {isPos ? '+' : ''}{q.change != null ? formatPrice(q.change) : '--'}
            </small>
          </div>

          {/* Day Range Bar */}
          <div className="range-container">
            <div className="range-bar-track">
              <div
                className="range-marker"
                style={{ left: `${markerPos}%` }}
                title={`LTP position: ${markerPos.toFixed(0)}% of day range`}
              />
            </div>
            <div className="range-labels">
              <span>L: <b className="tabular-nums">{q.low != null ? `₹${Number(q.low).toLocaleString('en-IN')}` : '--'}</b></span>
              <span>H: <b className="tabular-nums">{q.high != null ? `₹${Number(q.high).toLocaleString('en-IN')}` : '--'}</b></span>
            </div>
          </div>

          {/* Meta row: Prev Close & Volume */}
          <div className="card-meta-row">
            <span>Prev Close: <b className="tabular-nums">{q.prevClose != null ? `₹${Number(q.prevClose).toLocaleString('en-IN')}` : '--'}</b></span>
            <span>Vol: <b className="tabular-nums">{q.volume ? Number(q.volume).toLocaleString('en-IN') : '-'}</b></span>
          </div>
        </>
      )}
    </div>
  );
});

