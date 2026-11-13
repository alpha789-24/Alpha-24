import React, { useState, useRef, useEffect, useMemo } from 'react';
import { Activity, RefreshCw, BarChart2, TrendingUp, ArrowUpRight, ArrowDownRight } from 'lucide-react';

const formatPrice = (val, decimals = 2) => {
  if (val == null || isNaN(val)) return '--';
  return Number(val).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  });
};

export { MarketCard } from './MarketCard.jsx';

import { RANGES, INTERVALS, intervalAllowed } from './chartRanges.js';

export function MarketWatchChart({
  symbol,
  chartData,
  chartLoading,
  chartInterval,
  onIntervalChange,
  chartRange = '1d',
  onRangeChange,
  onRefresh,
  liveQuote
}) {
  const [chartMode, setChartMode] = useState('candles'); // 'candles' | 'line'
  const [hoverIndex, setHoverIndex] = useState(null);
  const [mousePos, setMousePos] = useState({ x: 0, y: 0 });
  const containerRef = useRef(null);
  const [dimensions, setDimensions] = useState({ width: 850, height: 340 });

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0) {
          setDimensions({
            width: Math.floor(width),
            height: Math.max(300, Math.floor(height || 340))
          });
        }
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const candles = useMemo(() => {
    if (!chartData?.candles || !Array.isArray(chartData.candles)) return [];
    return chartData.candles;
  }, [chartData]);

  const previousClose = chartData?.previousClose ?? null;
  const lastCandle = candles.length > 0 ? candles[candles.length - 1] : null;

  // Header display quote: hover candle if hovering, else live quote or last candle
  const activeCandle = hoverIndex !== null && candles[hoverIndex] ? candles[hoverIndex] : null;

  const currentPrice = useMemo(() => {
    if (activeCandle) return activeCandle.close;
    if (liveQuote?.ltp != null) return liveQuote.ltp;
    return lastCandle?.close ?? null;
  }, [activeCandle, liveQuote, lastCandle]);

  const changeInfo = useMemo(() => {
    const baseClose = previousClose ?? (candles.length > 0 ? candles[0].open : currentPrice);
    if (currentPrice == null || baseClose == null) return { diff: 0, pct: 0, isPos: true };
    const diff = currentPrice - baseClose;
    const pct = baseClose !== 0 ? (diff / baseClose) * 100 : 0;
    return {
      diff,
      pct,
      isPos: diff >= 0
    };
  }, [currentPrice, previousClose, candles]);

  // Layout metrics
  const margin = { top: 22, right: 80, bottom: 28, left: 12 };
  const plotWidth = Math.max(10, dimensions.width - margin.left - margin.right);
  const totalPlotHeight = Math.max(10, dimensions.height - margin.top - margin.bottom);
  const volumeHeight = Math.floor(totalPlotHeight * 0.18);
  const gap = 12;
  const priceHeight = Math.max(10, totalPlotHeight - volumeHeight - gap);
  const volumeTop = margin.top + priceHeight + gap;

  // Price Domain Calculations
  const { yMin, yMax, ticks } = useMemo(() => {
    if (candles.length === 0) {
      return { yMin: 0, yMax: 100, ticks: [20, 40, 60, 80, 100] };
    }

    let min = Infinity;
    let max = -Infinity;

    for (const c of candles) {
      if (c.low != null && c.low < min) min = c.low;
      if (c.high != null && c.high > max) max = c.high;
    }

    if (previousClose != null) {
      if (previousClose < min) min = previousClose;
      if (previousClose > max) max = previousClose;
    }

    if (min === Infinity || max === -Infinity) {
      min = 0;
      max = 100;
    }

    const span = max - min || 1;
    const pad = span * 0.04;
    const calcMin = min - pad;
    const calcMax = max + pad;

    // Generate 5 nice ticks
    const tickCount = 5;
    const tickStep = (calcMax - calcMin) / (tickCount - 1);
    const tickList = [];
    for (let i = 0; i < tickCount; i++) {
      tickList.push(calcMin + i * tickStep);
    }

    return { yMin: calcMin, yMax: calcMax, ticks: tickList };
  }, [candles, previousClose]);

  // Volume Domain Calculations
  const maxVolume = useMemo(() => {
    if (candles.length === 0) return 1;
    let maxV = 1;
    for (const c of candles) {
      if (c.volume != null && c.volume > maxV) maxV = c.volume;
    }
    return maxV;
  }, [candles]);

  const getY = (val) => {
    if (yMax === yMin) return margin.top + priceHeight / 2;
    return margin.top + (1 - (val - yMin) / (yMax - yMin)) * priceHeight;
  };

  const getPriceFromY = (y) => {
    const ratio = 1 - (y - margin.top) / priceHeight;
    return yMin + ratio * (yMax - yMin);
  };

  const getVolY = (vol) => {
    const ratio = Math.min(1, Math.max(0, (vol || 0) / maxVolume));
    return volumeTop + volumeHeight - ratio * volumeHeight;
  };

  const getVolHeight = (vol) => {
    const ratio = Math.min(1, Math.max(0, (vol || 0) / maxVolume));
    return Math.max(1, ratio * volumeHeight);
  };

  const N = candles.length;
  const stepX = N > 0 ? plotWidth / N : 1;
  const candleWidth = Math.max(2, Math.min(16, stepX * 0.72));

  // Handle crosshair mouse interactions
  const handleMouseMove = (e) => {
    if (!containerRef.current || N === 0) return;
    const rect = containerRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    if (x >= margin.left && x <= margin.left + plotWidth && y >= margin.top && y <= margin.top + totalPlotHeight) {
      const idx = Math.floor((x - margin.left) / stepX);
      const clampedIdx = Math.max(0, Math.min(N - 1, idx));
      setHoverIndex(clampedIdx);
      setMousePos({ x, y });
    } else {
      setHoverIndex(null);
    }
  };

  const handleMouseLeave = () => {
    setHoverIndex(null);
  };

  // Paths for Line / Area mode
  const { linePath, areaPath } = useMemo(() => {
    if (candles.length === 0) return { linePath: '', areaPath: '' };
    const points = candles.map((c, i) => {
      const cx = margin.left + (i + 0.5) * stepX;
      const cy = getY(c.close);
      return `${cx.toFixed(1)},${cy.toFixed(1)}`;
    });

    const lPath = `M ${points.join(' L ')}`;
    const lastX = (margin.left + (N - 0.5) * stepX).toFixed(1);
    const firstX = (margin.left + 0.5 * stepX).toFixed(1);
    const bottomY = (margin.top + priceHeight).toFixed(1);
    const aPath = `${lPath} L ${lastX},${bottomY} L ${firstX},${bottomY} Z`;

    return { linePath: lPath, areaPath: aPath };
  }, [candles, stepX, margin.left, margin.top, priceHeight, N, yMin, yMax]);

  const lastLtp = liveQuote?.ltp ?? lastCandle?.close ?? null;
  const lastLtpY = lastLtp != null ? getY(lastLtp) : null;
  const prevCloseY = previousClose != null ? getY(previousClose) : null;

  return (
    <div className="chart-panel">
      {/* Header Row: Symbol, LTP, Change Chip, HUD, Controls */}
      <div className="chart-top-bar">
        <div className="chart-title-area">
          <div className="chart-symbol-badge">
            <Activity size={18} className="chart-symbol-icon" />
            <h3 className="chart-symbol-title">{symbol}</h3>
            <span className="chart-exchange-pill">NSE</span>
          </div>

          <div className="chart-header-metrics">
            <span className="chart-header-price tabular-nums">
              ₹{formatPrice(currentPrice)}
            </span>
            <span className={`chart-header-change tabular-nums ${changeInfo.isPos ? 'positive' : 'negative'}`}>
              {changeInfo.isPos ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}
              {changeInfo.isPos ? '+' : ''}₹{formatPrice(Math.abs(changeInfo.diff))} ({changeInfo.isPos ? '+' : ''}{changeInfo.pct.toFixed(2)}%)
            </span>
          </div>

          {/* Subtitle / HUD info */}
          {activeCandle ? (
            <div className="chart-hud-strip tabular-nums">
              <span><b>T:</b> {activeCandle.time}</span>
              <span><b>O:</b> ₹{formatPrice(activeCandle.open)}</span>
              <span><b>H:</b> ₹{formatPrice(activeCandle.high)}</span>
              <span><b>L:</b> ₹{formatPrice(activeCandle.low)}</span>
              <span><b>C:</b> ₹{formatPrice(activeCandle.close)}</span>
              <span><b>Vol:</b> {Number(activeCandle.volume || 0).toLocaleString('en-IN')}</span>
            </div>
          ) : (
            <small className="chart-sub-note">
              {chartInterval} candles • Prev Close: {previousClose ? `₹${formatPrice(previousClose)}` : '--'}
            </small>
          )}
        </div>

        {/* Right side controls: Mode Toggle (Candles | Line), Interval Pills, Refresh */}
        <div className="chart-controls-cluster">
          {onRangeChange && (
            <div className="segmented-control range-pills" role="group" aria-label="History range">
              {RANGES.map(r => (
                <button key={r.v} type="button" className={`segmented-btn ${chartRange === r.v ? 'active' : ''}`} onClick={() => onRangeChange(r.v)} title={`Show ${r.t}`}>{r.l}</button>
              ))}
            </div>
          )}
          {/* Candles / Line Toggle */}
          <div className="segmented-control" role="group" aria-label="Chart Style">
            <button
              type="button"
              className={`segmented-btn ${chartMode === 'candles' ? 'active' : ''}`}
              onClick={() => setChartMode('candles')}
              title="Candlestick mode"
            >
              Candles
            </button>
            <button
              type="button"
              className={`segmented-btn ${chartMode === 'line' ? 'active' : ''}`}
              onClick={() => setChartMode('line')}
              title="Linear area mode"
            >
              Line
            </button>
          </div>

          {/* Timeframe Interval Pills */}
          <div className="segmented-control" role="group" aria-label="Chart Interval">
            {INTERVALS.map((int) => {
              const ok = intervalAllowed(chartRange, int);
              return (
                <button
                  key={int}
                  type="button"
                  disabled={!ok}
                  className={`segmented-btn ${chartInterval === int ? 'active' : ''}`}
                  onClick={() => onIntervalChange(int)}
                  title={ok ? `${int} candles` : `${int} candles are not available for this range (data-provider limit)`}
                >
                  {int}
                </button>
              );
            })}
          </div>

          <button
            type="button"
            className="chart-icon-btn"
            onClick={onRefresh}
            title="Refresh chart data"
            aria-label="Refresh chart"
          >
            <RefreshCw size={13} className={chartLoading ? 'spin-anim' : ''} />
          </button>
        </div>
      </div>

      {/* Fallback indicator message */}
      {chartData?.fallback && (
        <div className="chart-fallback-banner">
          Showing {chartData.rangeUsed} / {chartData.intervalUsed} candles (intraday fallback session).
        </div>
      )}

      {/* SVG Interactive Chart Canvas */}
      <div
        ref={containerRef}
        className="chart-canvas-container"
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
      >
        {chartLoading && (
          <div className="chart-loading-overlay">
            <span className="chart-loading-pill">Updating live feed...</span>
          </div>
        )}

        {candles.length === 0 ? (
          <div className="chart-empty-state">
            {chartLoading ? 'Loading market chart...' : `No candle data available for ${symbol}.`}
          </div>
        ) : (
          <svg
            width="100%"
            height={dimensions.height}
            viewBox={`0 0 ${dimensions.width} ${dimensions.height}`}
            className="chart-svg"
          >
            <defs>
              <linearGradient id="blueAreaGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--chart-line)" stopOpacity="0.28" />
                <stop offset="90%" stopColor="var(--chart-line)" stopOpacity="0.01" />
              </linearGradient>
            </defs>

            {/* Subtle horizontal grid lines */}
            {ticks.map((tVal, i) => {
              const y = getY(tVal);
              return (
                <g key={i}>
                  <line
                    x1={margin.left}
                    y1={y}
                    x2={margin.left + plotWidth}
                    y2={y}
                    stroke="var(--chart-grid)"
                    strokeWidth="1"
                  />
                  {/* Right-side price axis labels */}
                  <text
                    x={margin.left + plotWidth + 8}
                    y={y + 4}
                    fill="var(--chart-axis-text)"
                    fontSize="11"
                    fontFamily="'Inter', -apple-system, sans-serif"
                    className="tabular-nums"
                  >
                    ₹{formatPrice(tVal, tVal > 1000 ? 0 : 2)}
                  </text>
                </g>
              );
            })}

            {/* Previous Close Benchmark Line */}
            {previousClose != null && prevCloseY != null && prevCloseY >= margin.top && prevCloseY <= margin.top + priceHeight && (
              <g className="prev-close-line">
                <line
                  x1={margin.left}
                  y1={prevCloseY}
                  x2={margin.left + plotWidth}
                  y2={prevCloseY}
                  stroke="var(--chart-prev-close)"
                  strokeDasharray="4 4"
                  strokeWidth="1"
                  strokeOpacity="0.85"
                />
                <text
                  x={margin.left + 6}
                  y={prevCloseY - 4}
                  fill="var(--warning-text)"
                  fontSize="10"
                  fontFamily="'Inter', sans-serif"
                  fontWeight="600"
                >
                  Prev Close: ₹{formatPrice(previousClose)}
                </text>
              </g>
            )}

            {/* Volume Histogram Strip at Bottom */}
            <g className="volume-strip">
              <line
                x1={margin.left}
                y1={volumeTop - 4}
                x2={margin.left + plotWidth}
                y2={volumeTop - 4}
                stroke="var(--chart-vol-sep)"
                strokeWidth="1"
              />
              <text
                x={margin.left + 6}
                y={volumeTop + 10}
                fill="var(--chart-axis-text)"
                fontSize="9"
                fontWeight="700"
                letterSpacing="0.8px"
              >
                VOL
              </text>
              {candles.map((c, i) => {
                const cx = margin.left + (i + 0.5) * stepX;
                const leftX = cx - candleWidth / 2;
                const vH = getVolHeight(c.volume);
                const vY = getVolY(c.volume);
                const isUp = c.close >= c.open;
                const color = isUp ? 'var(--profit)' : 'var(--loss)';

                return (
                  <rect
                    key={`v-${i}`}
                    x={leftX}
                    y={vY}
                    width={candleWidth}
                    height={vH}
                    fill={color}
                    fillOpacity="0.28"
                    rx="1"
                  />
                );
              })}
            </g>

            {/* Price Chart: Candles Mode */}
            {chartMode === 'candles' && (
              <g className="candles-group">
                {candles.map((c, i) => {
                  const cx = margin.left + (i + 0.5) * stepX;
                  const leftX = cx - candleWidth / 2;
                  const isUp = c.close >= c.open;
                  const color = isUp ? 'var(--profit)' : 'var(--loss)';

                  const yHigh = getY(c.high);
                  const yLow = getY(c.low);
                  const yOpen = getY(c.open);
                  const yClose = getY(c.close);

                  const bodyTop = Math.min(yOpen, yClose);
                  const bodyHeight = Math.max(1.5, Math.abs(yClose - yOpen));

                  return (
                    <g key={`c-${i}`}>
                      {/* Wick */}
                      <line
                        x1={cx}
                        y1={yHigh}
                        x2={cx}
                        y2={yLow}
                        stroke={color}
                        strokeWidth="1.2"
                      />
                      {/* Candle Body */}
                      <rect
                        x={leftX}
                        y={bodyTop}
                        width={candleWidth}
                        height={bodyHeight}
                        fill={color}
                        rx="1"
                      />
                    </g>
                  );
                })}
              </g>
            )}

            {/* Price Chart: Line / Area Mode */}
            {chartMode === 'line' && (
              <g className="line-mode-group">
                <path d={areaPath} fill="url(#blueAreaGrad)" />
                <path
                  d={linePath}
                  fill="none"
                  stroke="var(--chart-line)"
                  strokeWidth="2"
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              </g>
            )}

            {/* Live Last-Price Tag Line and Badge */}
            {lastLtp != null && lastLtpY != null && lastLtpY >= margin.top && lastLtpY <= margin.top + priceHeight && (
              <g className="live-price-tag-group">
                <line
                  x1={margin.left}
                  y1={lastLtpY}
                  x2={margin.left + plotWidth}
                  y2={lastLtpY}
                  stroke={changeInfo.isPos ? 'var(--profit)' : 'var(--loss)'}
                  strokeDasharray="2 2"
                  strokeWidth="1"
                  strokeOpacity="0.85"
                />
                <rect
                  x={margin.left + plotWidth + 4}
                  y={lastLtpY - 9}
                  width={72}
                  height={18}
                  rx="3"
                  fill={changeInfo.isPos ? 'var(--profit)' : 'var(--loss)'}
                />
                <text
                  x={margin.left + plotWidth + 40}
                  y={lastLtpY + 4}
                  fill="var(--chart-tag-text)"
                  fontSize="10"
                  fontWeight="700"
                  fontFamily="'Inter', monospace"
                  textAnchor="middle"
                  className="tabular-nums"
                >
                  ₹{formatPrice(lastLtp, lastLtp > 1000 ? 1 : 2)}
                </text>
              </g>
            )}

            {/* Interactive Crosshair & Tooltip Overlay */}
            {hoverIndex !== null && candles[hoverIndex] && (
              <g className="crosshair-group">
                {/* Vertical Line */}
                <line
                  x1={margin.left + (hoverIndex + 0.5) * stepX}
                  y1={margin.top}
                  x2={margin.left + (hoverIndex + 0.5) * stepX}
                  y2={margin.top + totalPlotHeight}
                  stroke="var(--chart-crosshair)"
                  strokeDasharray="3 3"
                  strokeWidth="1"
                />

                {/* Horizontal Line */}
                <line
                  x1={margin.left}
                  y1={mousePos.y}
                  x2={margin.left + plotWidth}
                  y2={mousePos.y}
                  stroke="var(--chart-crosshair)"
                  strokeDasharray="3 3"
                  strokeWidth="1"
                />

                {/* Y-Axis Hover Price Pill */}
                <g>
                  <rect
                    x={margin.left + plotWidth + 4}
                    y={mousePos.y - 9}
                    width={72}
                    height={18}
                    rx="3"
                    fill="var(--chart-pill-bg)"
                    stroke="var(--chart-pill-border)"
                  />
                  <text
                    x={margin.left + plotWidth + 40}
                    y={mousePos.y + 4}
                    fill="var(--chart-pill-text)"
                    fontSize="10"
                    fontWeight="600"
                    fontFamily="'Inter', monospace"
                    textAnchor="middle"
                    className="tabular-nums"
                  >
                    ₹{formatPrice(getPriceFromY(mousePos.y), 2)}
                  </text>
                </g>

                {/* X-Axis Hover Time Pill */}
                <g>
                  <rect
                    x={margin.left + (hoverIndex + 0.5) * stepX - 32}
                    y={margin.top + totalPlotHeight + 4}
                    width={64}
                    height={18}
                    rx="3"
                    fill="var(--chart-pill-bg)"
                    stroke="var(--chart-pill-border)"
                  />
                  <text
                    x={margin.left + (hoverIndex + 0.5) * stepX}
                    y={margin.top + totalPlotHeight + 17}
                    fill="var(--chart-pill-text)"
                    fontSize="10"
                    fontWeight="600"
                    fontFamily="'Inter', monospace"
                    textAnchor="middle"
                  >
                    {candles[hoverIndex].time}
                  </text>
                </g>
              </g>
            )}
          </svg>
        )}
      </div>
    </div>
  );
}
