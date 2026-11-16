import React, { Suspense, lazy, useEffect, useMemo, useState, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity, ArrowDownRight, ArrowUpRight, BarChart3, BookOpen,
  BriefcaseBusiness, ChevronRight, CircleHelp, Cloud, Code2,
  Database, Download, ExternalLink, FileText, FlaskConical,
  Gauge, Layers3, LayoutDashboard, LogOut, Menu, MoreHorizontal, Pause,
  Play, Plus, RefreshCw, Search, Settings, Shield, ShieldCheck,
  SlidersHorizontal, Sun, Moon, Trash2, TrendingUp, TrendingDown, Wallet, Wifi,
  WifiOff, X, Zap, Check, LineChart as ChartIcon, FolderOpen
} from 'lucide-react';
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, ReferenceLine, CartesianGrid
} from 'recharts';
import { DashboardStrategyCards } from './DashboardStrategyCards.jsx';
import { stripCredentialsClient } from './RuleBuilderStudio.jsx';
import './style.css';
import { MarketCard } from './MarketCard.jsx';
import { bestIntervalFor } from './chartRanges.js';
import { InstrumentPicker } from './InstrumentPicker.jsx';
import { StrikeSelector } from './StrikeSelector.jsx';
import { AdvancedProperties } from './AdvancedProperties.jsx';
import SavedStrategies from './SavedStrategies.jsx';
import { getMarketPollIntervalMs, getOperationalPollIntervalMs } from './pollingConfig.js';
import { IIFLConnection } from './IIFLConnection.jsx';
import PythonImporter from './PythonImporter.jsx';

const RuleBuilderEditor = lazy(() =>
  import('./RuleBuilderPanels.jsx').then(m => ({ default: m.RuleBuilderEditor }))
);
const MarketWatchChart = lazy(() =>
  import('./MarketWatchChart.jsx').then(m => ({ default: m.MarketWatchChart }))
);
const ReportsPage = lazy(() => import('./ReportsPage.jsx'));

const API = 'http://127.0.0.1:8000/api';
const WS_URL = 'ws://127.0.0.1:8000/api/market/ws';

// Display-only helper: hides exchange suffix (.NS/.BO) and index prefix (^). Never use for API calls or storage.
const displaySymbol = (s) => String(s ?? '').replace(/\.(NS|BO)$/i, '').replace(/^\^/, '');

const initialStrategy = {
  id: crypto.randomUUID(),
  name: 'NIFTY Supertrend Intraday',
  description: 'Trend-following example strategy with live market tracking.',
  instrument: 'NIFTY 50',
  timeframe: '5m',
  strategyType: 'INDICATOR',
  sar: {
    levelSource: 'MANUAL',
    entryStyle: 'REVERSAL',
    manual: { upper: 330, lower: 325 },
    candle: { timeframe: '15m', upperSource: 'HIGH', lowerSource: 'LOW' },
    slPoints: 1,
    tgtPoints: 1.5,
    baseQuantity: 100,
    multipliers: [3, 2, 2],
    maxLegs: 4,
    startTime: '09:15',
    endTime: '15:00',
    afterTarget: 'STOP',
    maxRestarts: 2
  },
  status: 'DRAFT',
  entry: {
    logic: 'AND',
    conditions: [{ indicator: 'SUPERTREND', operator: 'CROSSES_ABOVE', value: 'PRICE', period: 10, multiplier: 3 }]
  },
  positions: [
    { side: 'BUY', underlying: 'NIFTY', instrumentType: 'OPTION', strike: 'ATM', optionType: 'CE', expiry: 'CURRENT_WEEK', quantity: 1, orderType: 'MARKET' }
  ],
  repair: { enabled: false, maxAttempts: 1, cooldownMinutes: 5 },
  overnight: { enabled: false, action: 'SQUARE_OFF' },
  universalExit: { enabled: true, conditions: [] },
  risk: { maxDailyLoss: 2000, maxCapital: 25000, maxTradesPerDay: 3, maxOpenPositions: 2 },
  advanced: { checkFrequency: 'CONTINUOUS', startAfterOpenMinutes: 5, stopBeforeCloseMinutes: 15, tranchePercent: 100, trancheIntervalSeconds: 1, reactivateAfterHours: 6, rollover: 'NONE' }
};

const navItems = [
  ['Overview', LayoutDashboard],
  ['Strategy Builder', Layers3],
  ['My Strategies', FolderOpen],
  ['Python Importer', Code2],
  ['Positions & Trades', BriefcaseBusiness],
  ['Market Watch', Activity],
  ['IIFL Connection', Wifi],
  ['Reports', BarChart3]
];

function App() {
  const [page, setPage] = useState('Overview');
  const [strategy, setStrategy] = useState(initialStrategy);
  const [strategies, setStrategies] = useState([]);
  const [trades, setTrades] = useState([]);
  const [marketQuotes, setMarketQuotes] = useState([]);
  const [priceFlash, setPriceFlash] = useState({});
  const [health, setHealth] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [lastLiveQuoteAt, setLastLiveQuoteAt] = useState(0);
  const [brokerCaps, setBrokerCaps] = useState(null);
  const [iifl, setIifl] = useState({ configured: false, sessionPresent: false });
  const [toast, setToast] = useState('');
  const [valid, setValid] = useState(null);
  const [menu, setMenu] = useState(false);
  const [bannerDismissed, setBannerDismissed] = useState(false);
  const [showKillConfirm, setShowKillConfirm] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [instrMeta, setInstrMeta] = useState(null);
  const [saveState, setSaveState] = useState(null); // {ok, msg}
  const [theme, setTheme] = useState(() => {
    try {
      const attr = document.documentElement.getAttribute('data-theme');
      if (attr === 'dark' || attr === 'light') return attr;
      const saved = localStorage.getItem('alpha_theme');
      return saved === 'dark' ? 'dark' : 'light';
    } catch {
      return 'light';
    }
  });

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    try {
      localStorage.setItem('alpha_theme', theme);
    } catch {}
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      meta.setAttribute('content', theme === 'dark' ? '#0B1020' : '#F5F7FB');
    }
  }, [theme]);

  // Live Market Watch state
  const [activeTab, setActiveTab] = useState('Indices'); // 'Indices' | 'Equities' | 'Custom'
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [searchFallback, setSearchFallback] = useState(false);
  const [customWatchlist, setCustomWatchlist] = useState(() => {
    try {
      const stored = localStorage.getItem('alpha_custom_watchlist');
      if (stored) {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed)) return parsed;
      }
      return ['RELIANCE', 'TCS', 'HDFCBANK', 'INFY'];
    } catch {
      return ['RELIANCE', 'TCS', 'HDFCBANK', 'INFY'];
    }
  });

  // Chart state
  const [chartSymbol, setChartSymbol] = useState('NIFTY 50');
  const [chartInterval, setChartInterval] = useState('5m');
  const [chartRange, setChartRange] = useState('1d');
  const [chartData, setChartData] = useState({ candles: [], previousClose: null });
  const [chartLoading, setChartLoading] = useState(false);

  // Strategy Builder live strikes state
  const [selectedStrikes, setSelectedStrikes] = useState(null);
  const [selectedSpot, setSelectedSpot] = useState(null);

  // Stop-and-Reverse (Level SAR) state
  const [sarPreview, setSarPreview] = useState(null);
  const [sarPreviewLoading, setSarPreviewLoading] = useState(false);
  const [sarRuns, setSarRuns] = useState([]);
  const [paperWorkerRuns, setPaperWorkerRuns] = useState([]);

  const wsRef = useRef(null);
  const searchBoxRef = useRef(null);
  const chartReqId = useRef(0);
  const searchReqId = useRef(0);
  const [hasSearched, setHasSearched] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  const watchlistRef = useRef(customWatchlist);
  watchlistRef.current = customWatchlist;

  const parseError = err => {
    if (!err) return 'Request failed';
    if (typeof err === 'string') return err;
    if (Array.isArray(err)) return err.map(e => e.msg || JSON.stringify(e)).join(', ');
    if (Array.isArray(err.errors)) return err.errors.join(', ');
    if (typeof err.detail === 'string') return err.detail;
    if (Array.isArray(err.detail)) return err.detail.map(e => e.msg || JSON.stringify(e)).join(', ');
    return err.message || JSON.stringify(err);
  };

  const notify = msg => {
    setToast(parseError(msg));
    setTimeout(() => setToast(''), 4000);
  };

  // Process incoming market quotes and trigger flash animations (merging quotes by symbol)
  const updateQuotes = (newQuotes) => {
    if (!newQuotes || !Array.isArray(newQuotes)) return;

    const hasLiveSource = newQuotes.some(
      (q) =>
        q &&
        q.ltp != null &&
        q.source &&
        !/OFFLINE|BACKOFF|MOCK|FALLBACK|NO DATA|CACHED/i.test(String(q.source))
    );
    if (hasLiveSource) {
      setLastLiveQuoteAt(Date.now());
    }

    setMarketQuotes(prevQuotes => {
      const prevMap = new Map();
      prevQuotes.forEach(q => {
        const k = q.symbol || q.ticker;
        if (k) prevMap.set(k, q);
        const clean = displaySymbol(k);
        if (clean) prevMap.set(clean, q);
      });

      const flashes = {};
      const updatedList = [...prevQuotes];

      newQuotes.forEach(q => {
        const key = q.symbol || q.ticker;
        if (!key) return;
        const clean = displaySymbol(key);

        const oldQ = prevMap.get(key) || (clean ? prevMap.get(clean) : null);
        if (oldQ && oldQ.ltp != null && q.ltp != null && q.ltp !== oldQ.ltp) {
          flashes[key] = q.ltp > oldQ.ltp ? 'up' : 'down';
          if (clean) flashes[clean] = flashes[key];
        }

        const idx = updatedList.findIndex(item =>
          item.symbol === key ||
          item.ticker === key ||
          (clean && (displaySymbol(item.symbol) === clean || displaySymbol(item.ticker) === clean))
        );

        if (idx >= 0) {
          updatedList[idx] = { ...updatedList[idx], ...q };
        } else {
          updatedList.push(q);
        }
      });

      if (Object.keys(flashes).length > 0) {
        setPriceFlash(prev => ({ ...prev, ...flashes }));
        setTimeout(() => {
          setPriceFlash(prev => {
            const next = { ...prev };
            Object.keys(flashes).forEach(k => delete next[k]);
            return next;
          });
        }, 300); // 300ms flash duration
      }

      return updatedList;
    });
  };

  // Load operational backend state (strategies, trades, IIFL status, paper worker runs, capabilities)
  async function loadOperationalData() {
    try {
      const [h, s, t, i, pw, caps] = await Promise.all([
        fetch(API + '/health').catch(() => ({ ok: false })),
        fetch(API + '/strategies').then(r => r.json()).catch(() => []),
        fetch(API + '/trades').then(r => r.json()).catch(() => []),
        fetch(API + '/iifl/status').then(r => r.json()).catch(() => ({ configured: false, sessionPresent: false })),
        fetch(API + '/paper/worker/runs').then(r => r.json()).catch(() => []),
        fetch(API + '/broker/capabilities').then(r => (r.ok ? r.json() : null)).catch(() => null)
      ]);

      setHealth(h.ok);
      setStrategies(s);
      setTrades(t);
      setPaperWorkerRuns(Array.isArray(pw) ? pw : []);
      if (caps) setBrokerCaps(caps);
      setIifl(i);
    } catch {
      setHealth(false);
    }
  }

  // Load REST market quotes (/api/market/live) — reduced to 45s when WS is open, 5s fallback when disconnected
  async function loadMarketQuotes() {
    try {
      const m = await fetch(
        `${API}/market/live?symbols=${encodeURIComponent(Array.from(new Set(['NIFTY 50', 'BANKNIFTY', 'FINNIFTY', 'SENSEX', 'NIFTY IT', ...(watchlistRef.current || [])])).join(','))}`
      )
        .then(r => r.json())
        .catch(() => null);
      if (m && m.items) {
        updateQuotes(m.items);
      }
    } catch {}
  }

  async function loadData() {
    await Promise.all([loadOperationalData(), loadMarketQuotes()]);
  }

  // Fetch interactive chart data
  async function loadChart(symbol, range = '1d', interval = '5m') {
    const id = ++chartReqId.current;
    setChartLoading(true);
    try {
      const res = await fetch(`${API}/market/chart?symbol=${encodeURIComponent(symbol)}&range=${range}&interval=${interval}`);
      if (id !== chartReqId.current) return;
      if (res.ok) {
        const data = await res.json();
        if (id !== chartReqId.current) return;
        if (data && data.candles && data.candles.length > 0) {
          setChartData({ ...data, forSymbol: symbol });
        } else {
          setChartData({ ...(data || {}), forSymbol: symbol, ticker: data?.ticker || symbol, candles: [], error: data?.error || 'No candles returned for this range' });
        }
      } else {
        setChartData({ forSymbol: symbol, symbol, ticker: symbol, candles: [], error: `HTTP ${res.status}` });
      }
    } catch (e) {
      console.error('Failed to load chart:', e);
      if (id === chartReqId.current) {
        setChartData({ forSymbol: symbol, symbol, ticker: symbol, candles: [], error: String(e.message || e) });
      }
    } finally {
      if (id === chartReqId.current) {
        setChartLoading(false);
      }
    }
  }

  // Fetch strike data for currently selected instrument in Strategy Builder
  async function loadStrikes(symbol) {
    try {
      const res = await fetch(`${API}/market/quote/${encodeURIComponent(symbol)}`);
      if (res.ok) {
        const data = await res.json();
        setSelectedStrikes(data.strikes);
        setSelectedSpot(data.quote?.ltp);
      }
    } catch (e) {
      console.error('Failed to load strikes:', e);
    }
  }

  // WebSocket connection for real-time live market ticks
  useEffect(() => {
    let socket;
    let reconnectTimer;

    function connectWs() {
      try {
        socket = new WebSocket(WS_URL);
        wsRef.current = socket;

        socket.onopen = () => {
          setWsConnected(true);
          // Send active custom symbols using latest watchlistRef
          socket.send(JSON.stringify({
            action: 'subscribe',
            symbols: ['NIFTY 50', 'BANKNIFTY', 'FINNIFTY', 'SENSEX', 'NIFTY IT', ...(watchlistRef.current || [])]
          }));
        };

        socket.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'QUOTE_TICK' && data.items) {
              updateQuotes(data.items);
            }
          } catch (e) {
            console.error(e);
          }
        };

        socket.onerror = () => {
          setWsConnected(false);
          if (socket) socket.close();
        };

        socket.onclose = () => {
          setWsConnected(false);
          reconnectTimer = setTimeout(connectWs, 4000);
        };
      } catch {
        setWsConnected(false);
        reconnectTimer = setTimeout(connectWs, 5000);
      }
    }

    connectWs();
    loadData();

    return () => {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (wsRef.current) wsRef.current.close();
    };
  }, []);

  // Poll operational state (trades, paper worker runs, IIFL status) every 7.5s regardless of WS state
  useEffect(() => {
    const opInterval = setInterval(loadOperationalData, getOperationalPollIntervalMs());
    return () => clearInterval(opInterval);
  }, []);

  // Poll /api/market/live every 45s when WebSocket is open, falling back to 5s when disconnected
  useEffect(() => {
    const intervalMs = getMarketPollIntervalMs(wsConnected);
    const pollInterval = setInterval(loadMarketQuotes, intervalMs);
    return () => clearInterval(pollInterval);
  }, [wsConnected]);

  // Close Market Watch search dropdown when clicking outside
  useEffect(() => {
    if (!hasSearched) return;
    const handleOutsideClick = (e) => {
      if (searchBoxRef.current && !searchBoxRef.current.contains(e.target)) {
        setHasSearched(false);
      }
    };
    document.addEventListener('mousedown', handleOutsideClick);
    return () => document.removeEventListener('mousedown', handleOutsideClick);
  }, [hasSearched]);

  // Re-subscribe when customWatchlist changes without reconnecting websocket
  useEffect(() => {
    if (wsRef.current && wsRef.current.readyState === 1) {
      wsRef.current.send(JSON.stringify({
        action: 'subscribe',
        symbols: ['NIFTY 50', 'BANKNIFTY', 'FINNIFTY', 'SENSEX', 'NIFTY IT', ...customWatchlist]
      }));
    }
  }, [customWatchlist]);

  // Load chart when chartSymbol or interval changes
  useEffect(() => {
    loadChart(chartSymbol, chartRange, chartInterval);
  }, [chartSymbol, chartRange, chartInterval]);

  // Update strikes when strategy instrument changes
  useEffect(() => {
    if (strategy.instrument) {
      loadStrikes(strategy.instrument);
    }
  }, [strategy.instrument]);

  const loadSarRuns = async () => {
    try {
      const r = await fetch(API + '/sar/runs');
      if (r.ok) {
        const data = await r.json();
        setSarRuns(data);
      }
    } catch {}
  };

  useEffect(() => {
    if (page !== 'Strategy Builder') return;
    loadSarRuns();
    const interval = setInterval(loadSarRuns, 2000);
    return () => clearInterval(interval);
  }, [page]);

  useEffect(() => {
    if ((strategy.strategyType || 'INDICATOR') !== 'LEVEL_SAR') return;
    const timer = setTimeout(async () => {
      setSarPreviewLoading(true);
      try {
        const r = await fetch(API + '/sar/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ strategy, referencePrice: selectedSpot || null })
        });
        if (r.ok) {
          const d = await r.json();
          setSarPreview(d);
        }
      } catch (err) {
        console.error('Preview error', err);
      } finally {
        setSarPreviewLoading(false);
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [strategy.strategyType, strategy.sar, strategy.risk, selectedSpot]);

  const startSarRun = async () => {
    if (strategy.lotSize == null && selectedStrikes?.lotSize == null) {
      notify('Cannot start Stop-and-Reverse run: lot size is unknown (contract master unavailable)');
      return;
    }
    let v = valid;
    if (!v?.valid) { v = await validate(); }
    if (!v?.valid) {
      notify('Please fix validation errors before running');
      return;
    }
    try {
      const r = await fetch(API + '/sar/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy })
      });
      if (!r.ok) {
        const d = await r.json();
        const msg = d.detail?.errors?.join(', ') || d.detail || 'Could not start Stop-and-Reverse run';
        throw Error(msg);
      }
      const run = await r.json();
      notify(`Stop-and-Reverse run started for ${displaySymbol(strategy.instrument)}!`);
      loadSarRuns();
      loadData();
    } catch (e) {
      notify(e.message);
    }
  };

  const stopSarRun = async (runId) => {
    try {
      const r = await fetch(API + '/sar/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ runId })
      });
      if (!r.ok) throw Error('Failed to stop run');
      const stopped = await r.json();
      notify(`SAR run stopped (${stopped.status})`);
      loadSarRuns();
      loadData();
    } catch (e) {
      notify(e.message);
    }
  };

  // Symbol Search handler
  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchResults([]);
      setSearchFallback(false);
      setHasSearched(false);
      setSearchLoading(false);
      return;
    }
    const id = ++searchReqId.current;
    setSearchLoading(true);
    setHasSearched(true);
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`${API}/contracts/search?q=${encodeURIComponent(searchQuery)}`);
        if (id !== searchReqId.current) return;
        if (res.ok) {
          const payload = await res.json();
          if (id !== searchReqId.current) return;
          const items = Array.isArray(payload?.items) ? payload.items : [];
          setSearchFallback(false);
          setSearchResults(items.map(item => ({
            symbol: item.symbol || item.cleanSymbol,
            cleanSymbol: item.cleanSymbol || item.symbol,
            name: item.name || item.cleanSymbol || item.symbol,
            exchange: item.exchange || item.segment || 'NSE',
            instrumentId: item.instrumentId,
            segment: item.segment,
            lotSize: item.lotSize,
          })));
          return;
        }
        // Fallback only when contract master is unavailable
        const fbRes = await fetch(`${API}/market/search?q=${encodeURIComponent(searchQuery)}`);
        if (id !== searchReqId.current) return;
        if (fbRes.ok) {
          const list = await fbRes.json();
          if (id !== searchReqId.current) return;
          setSearchFallback(true);
          setSearchResults(Array.isArray(list) ? list : []);
        } else {
          setSearchFallback(true);
          setSearchResults([]);
        }
      } catch {
        if (id === searchReqId.current) {
          setSearchFallback(true);
          setSearchResults([]);
        }
      } finally {
        if (id === searchReqId.current) setSearchLoading(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  const addCustomSymbol = (rawSymbol) => {
    const sym = (rawSymbol || '').trim();
    if (!sym) return;

    setCustomWatchlist(prev => {
      const exists = prev.some(x => x === sym || displaySymbol(x) === displaySymbol(sym));
      if (exists) {
        notify(`${displaySymbol(sym)} is already in your watchlist`);
        return prev;
      }
      const updated = [...prev, sym];
      watchlistRef.current = updated;
      try {
        localStorage.setItem('alpha_custom_watchlist', JSON.stringify(updated));
      } catch {}
      notify(`Added ${displaySymbol(sym)} to watchlist`);
      return updated;
    });

    // Immediately fetch quote for the newly added symbol
    try {
      fetch(`${API}/market/quote/${encodeURIComponent(sym)}`)
        .then(r => r.json())
        .then(data => {
          if (data?.quote) {
            updateQuotes([data.quote]);
          }
        })
        .catch(() => {});
    } catch {}

    setSearchQuery('');
    setSearchResults([]);
    setHasSearched(false);
  };

  const removeCustomSymbol = (sym) => {
    setCustomWatchlist(prev => {
      const updated = prev.filter(x => x !== sym && displaySymbol(x) !== displaySymbol(sym));
      watchlistRef.current = updated;
      try {
        localStorage.setItem('alpha_custom_watchlist', JSON.stringify(updated));
      } catch {}
      return updated;
    });
    notify(`Removed ${displaySymbol(sym)} from watchlist`);

    if (chartSymbol === sym || displaySymbol(chartSymbol) === displaySymbol(sym)) {
      setCustomWatchlist(current => {
        const nextSym = current.length > 0 ? current[0] : 'NIFTY 50';
        setChartSymbol(nextSym);
        return current;
      });
    }
  };

  const save = async () => {
    try {
      const r = await fetch(API + '/strategies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy })
      });
      if (!r.ok) {
        let d = null; try { d = await r.json(); } catch {}
        const reasons = d?.detail?.errors?.join('; ') || (typeof d?.detail === 'string' ? d.detail : '') || `HTTP ${r.status}`;
        setSaveState({ ok: false, msg: `Not saved: ${reasons}` });
        notify(`Save failed: ${reasons}`);
        return;
      }
      setSaveState({ ok: true, msg: `Saved "${strategy.name}" — find it under My Strategies` });
      notify(`Saved "${strategy.name}" — available under My Strategies`);
      loadData();
    } catch {
      let a = JSON.parse(localStorage.getItem('alpha_strategies') || '[]').filter(x => x.id !== strategy.id);
      a.unshift(strategy);
      localStorage.setItem('alpha_strategies', JSON.stringify(a));
      setSaveState({ ok: false, msg: 'Backend unreachable: saved only in this browser. It will not appear in My Strategies until the backend is back and you save again.' });
      notify('Saved locally only — backend unreachable');
    }
  };

  const validate = async () => {
    try {
      const r = await fetch(API + '/strategies/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy })
      });
      const v = await r.json();
      setValid(v);
      notify(v.valid ? 'Strategy validation passed' : 'Please fix validation errors');
      return v;
    } catch {
      let errors = [];
      if (!strategy.name) errors.push('Strategy name is required');
      if ((strategy.strategyType || 'INDICATOR') === 'LEVEL_SAR') {
        const sar = strategy.sar || {};
        if (sar.levelSource === 'MANUAL' && Number(sar.manual?.upper) <= Number(sar.manual?.lower)) {
          errors.push('Upper level must be greater than lower level');
        }
      } else {
        if (!strategy.entry?.conditions?.length) errors.push('Add an entry condition');
      }
      if (strategy.risk.maxDailyLoss <= 0 || strategy.risk.maxCapital <= 0) errors.push('Set valid risk limits');
      const v = { valid: !errors.length, errors };
      setValid(v);
      notify('Backend unavailable: local validation only');
      return v;
    }
  };

  const paper = async (override) => {
    const target = override && override.id ? override : strategy;
    let v = override && override.id ? await validateStrategyObj(target) : valid;
    if (!v?.valid) { v = override && override.id ? v : await validate(); }
    if (!v?.valid) {
      notify('Please fix validation errors before simulation');
      return;
    }
    try {
      if ((target.strategyType || 'INDICATOR') === 'RULE_BUILDER') {
        if (target.execution?.instrument?.lotSize == null && selectedStrikes?.lotSize == null) {
          notify('Cannot start paper worker: lot size is unknown (contract master unavailable)');
          return;
        }
        const primaryTf = target.indicators?.[0]?.timeframe || target.timeframe || '5m';
        const r = await fetch(API + '/paper/worker/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            target,
            primaryTimeframe: primaryTf,
            slippagePoints: 0,
            brokeragePerOrder: 20,
            chargesPercent: 0.05
          })
        });
        const d = await r.json();
        if (!r.ok) {
          throw Error(d.detail?.errors?.join(', ') || d.detail || 'Could not start paper worker');
        }
        notify(`Paper Worker ${d.status}: ${d.strategyName || target.name} (${d.primaryTimeframe}) — ${d.message || 'Active'}`);
        loadData();
        return;
      }

      const r = await fetch(API + '/paper/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target })
      });
      if (!r.ok) {
        const d = await r.json();
        throw Error(d.detail?.errors?.join(', ') || 'Could not simulate');
      }
      const trade = await r.json();
      if (trade.entryPrice != null) {
        notify(`Simulated Fill: ${trade.contract} @ ₹${trade.entryPrice.toLocaleString('en-IN')}`);
      } else {
        notify(`Paper Run ${trade.status || 'STARTED'}: ${trade.strategyName || target.name}`);
      }
      loadData();
    } catch (e) {
      notify(e.message);
    }
  };

  const validateStrategyObj = async (obj) => {
    try {
      const r = await fetch(API + '/strategies/validate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ strategy: obj }) });
      return await r.json();
    } catch { return { valid: false, errors: ['Backend unavailable'] }; }
  };

  // My Strategies handlers. Loading opens a draft in the builder; the saved copy is untouched until Save.
  const hasUnsavedDraft = () => {
    if (strategy.name === initialStrategy.name && !strategies.some(x => x.id === strategy.id)) return false;
    const saved = strategies.find(x => x.id === strategy.id);
    return !saved || JSON.stringify(saved) !== JSON.stringify(strategy);
  };
  const loadSavedStrategy = (s) => {
    if (s.id !== strategy.id && hasUnsavedDraft() && !window.confirm('Your current draft has unsaved changes. Replace it with the selected strategy?')) return;
    setStrategy(structuredClone(s)); setValid(null); setSaveState(null); go('Strategy Builder');
  };
  const validateSaved = async (s) => {
    const v = await validateStrategyObj(s);
    notify(v.valid ? `"${s.name}": validation passed (this does not mean it is safe or profitable)` : `"${s.name}": ${(v.errors || []).slice(0, 2).join('; ') || 'validation failed'}`);
  };
  const paperSaved = async (s) => {
    if (!window.confirm(`Start PAPER trading for "${s.name}"?\nThis simulates fills using live prices. No real orders are placed.`)) return;
    await paper(s);
  };

  const closeTrade = async (tradeId) => {
    try {
      const r = await fetch(API + '/paper/close', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tradeId })
      });
      if (!r.ok) throw Error('Failed to close position');
      const closed = await r.json();
      notify(`Position squared off! Realized P&L: ₹${closed.pnl.toLocaleString('en-IN')}`);
      loadData();
    } catch (e) {
      notify(e.message);
    }
  };

  const update = (path, value) => setStrategy(old => {
    let n = structuredClone(old);
    let parts = path.split('.');
    let o = n;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!o[parts[i]]) o[parts[i]] = {};
      o = o[parts[i]];
    }
    o[parts.at(-1)] = value;
    return n;
  });

  const addCondition = () => setStrategy(s => ({
    ...s,
    entry: {
      ...s.entry,
      conditions: [...s.entry.conditions, { indicator: 'RSI', operator: '>', value: 50, period: 14 }]
    }
  }));

  const addPosition = () => setStrategy(s => ({
    ...s,
    positions: [...s.positions, { side: 'SELL', underlying: 'NIFTY', instrumentType: 'OPTION', strike: 'OTM 1', optionType: 'PE', expiry: 'CURRENT_WEEK', quantity: 1, orderType: 'MARKET' }]
  }));

  const removeCondition = i => setStrategy(s => ({
    ...s,
    entry: { ...s.entry, conditions: s.entry.conditions.filter((_, j) => j !== i) }
  }));

  const setPosition = (i, k, v) => setStrategy(s => ({
    ...s,
    positions: s.positions.map((p, j) => j === i ? { ...p, [k]: v } : p)
  }));

  const go = p => {
    setPage(p);
    setMenu(false);
  };

  const createNewRuleStrategy = async () => {
    try {
      const r = await fetch(API + '/strategies/template?strategyType=RULE_BUILDER');
      if (r.ok) {
        const tmpl = await r.json();
        setStrategy(tmpl);
        setValid(null);
        go('Strategy Builder');
        return;
      }
    } catch {}
    setStrategy(structuredClone(initialStrategy));
    go('Strategy Builder');
  };

  const duplicateStrategyById = async (id) => {
    try {
      const r = await fetch(`${API}/strategies/${encodeURIComponent(id)}/duplicate`, { method: 'POST' });
      if (!r.ok) throw Error('Duplicate failed');
      const dup = await r.json();
      notify(`Duplicated: ${dup.name}`);
      loadData();
    } catch (e) {
      notify(e.message);
    }
  };

  const deleteStrategyById = async (id) => {
    try {
      const r = await fetch(`${API}/strategies/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (!r.ok) throw Error('Delete failed');
      notify('Strategy deleted');
      loadData();
    } catch (e) {
      notify(e.message);
    }
  };

  const exportStrategyClean = async (stratObj) => {
    const cleaned = stripCredentialsClient(stratObj);
    const blob = new Blob([JSON.stringify(cleaned, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${(stratObj.name || 'strategy').toLowerCase().replace(/[^a-z0-9]+/g, '-')}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    notify('Exported clean strategy JSON (credentials stripped)');
  };

  // Filter quotes based on active tab (with customWatchlist as absolute source of truth)
  const displayedQuotes = useMemo(() => {
    if (activeTab === 'Indices') {
      return marketQuotes.filter(q => q.category === 'INDEX');
    }
    if (activeTab === 'Equities') {
      return marketQuotes.filter(q => q.category === 'EQUITY');
    }
    // Custom Watchlist: maps over customWatchlist so cards NEVER disappear or flicker
    const quoteMap = new Map();
    marketQuotes.forEach(q => {
      if (q.symbol) quoteMap.set(q.symbol, q);
      if (q.ticker) quoteMap.set(q.ticker, q);
      const clean = displaySymbol(q.symbol || q.ticker);
      if (clean) quoteMap.set(clean, q);
    });

    return customWatchlist.map(sym => {
      const q = quoteMap.get(sym) || quoteMap.get(displaySymbol(sym));
      if (q) {
        return {
          ...q,
          symbol: q.symbol || sym,
          ticker: q.ticker || sym,
          displayName: q.displayName || q.name || displaySymbol(sym),
          exchange: q.exchange || 'NSE',
          isCustom: true
        };
      }
      return {
        symbol: sym,
        ticker: sym,
        displayName: displaySymbol(sym),
        exchange: 'NSE',
        ltp: null,
        change: null,
        changePercent: null,
        high: null,
        low: null,
        prevClose: null,
        volume: null,
        loading: true,
        isCustom: true
      };
    });
  }, [marketQuotes, activeTab, customWatchlist]);

  // Aggregate portfolio metrics for trades
  const tradeStats = useMemo(() => {
    let totalPnl = 0;
    let openCount = 0;
    let closedCount = 0;
    let capitalDeployed = 0;

    trades.forEach(t => {
      totalPnl += (t.pnl || 0);
      if (t.status === 'OPEN') {
        openCount++;
        capitalDeployed += (t.tradedValue || 0);
      } else {
        closedCount++;
      }
    });

    return { totalPnl: Math.round(totalPnl * 100) / 100, openCount, closedCount, capitalDeployed };
  }, [trades]);

  const hasFreshLiveQuotes = marketQuotes.some(
    (q) =>
      q &&
      q.ltp != null &&
      q.source &&
      !/OFFLINE|BACKOFF|MOCK|FALLBACK|NO DATA|CACHED/i.test(String(q.source))
  );
  const feedIsLive = Boolean(health && wsConnected && hasFreshLiveQuotes && lastLiveQuoteAt > 0);
  const activeExecMode =
    brokerCaps?.liveExecutionEnabled && strategy?.execution?.mode === 'LIVE' ? 'LIVE' : 'PAPER';

  return (
    <div className="app">
      {/* Normal-flow slim dismissible warning notice */}
      {!bannerDismissed && (
        <div className="system-banner" role="status" aria-live="polite">
          <div className="system-banner-content">
            <span className="system-banner-tag">NOTICE</span>
            <span>FC/RC PLACEHOLDER is a dummy test provider, disabled by default. It is not market data and must never be used for trading decisions. FORMULA is unavailable until formulas are supplied.</span>
          </div>
          <button
            type="button"
            className="system-banner-close"
            onClick={() => setBannerDismissed(true)}
            title="Dismiss notice"
            aria-label="Dismiss banner"
          >
            <X size={14} />
          </button>
        </div>
      )}
      {/* Sidebar Navigation */}
      <aside className={`sidebar ${menu ? 'open' : ''}`}>
        <div className="brand">
          <img className="brand-logo" src="/alpha24-logo.png" alt="ALPHA24 Money Basics" />
          <div className="brand-text">
            <b>STRATEGY STUDIO</b>
            <small>by ALPHA24</small>
          </div>
          <button className="mobile-close" onClick={() => setMenu(false)}><X size={18} /></button>
        </div>

        <div className="nav-label">WORKSPACE</div>
        <nav>
          {navItems.map(([n, Icon]) => (
            <button key={n} className={page === n ? 'active' : ''} onClick={() => go(n)}>
              <Icon size={18} />
              <span>{n}</span>
              {n === 'Positions & Trades' && <em>{tradeStats.openCount}</em>}
            </button>
          ))}
        </nav>

        <div className="nav-label tools-label">PREFERENCES</div>
        <nav>
          <button className={page === 'Settings' ? 'active' : ''} onClick={() => go('Settings')}><Settings size={18} />Settings</button>
          <button className={page === 'Documentation' ? 'active' : ''} onClick={() => go('Documentation')}><BookOpen size={18} />Documentation</button>
        </nav>

        <div className="sidebar-bottom">
          <div className="connection-mini">
            <span className={`dot ${feedIsLive ? 'green' : health ? 'amber' : 'gray'}`}></span>
            <div>
              <b>{feedIsLive ? 'Backend & Market Live' : health ? 'Backend Connected' : 'Backend offline'}</b>
              <small>{feedIsLive ? 'Live NSE Feed Active' : 'Offline/Delayed'}</small>
            </div>
            <button onClick={loadData} title="Refresh data"><RefreshCw size={14} /></button>
          </div>
        </div>
      </aside>

      {showAdvanced && (
        <AdvancedProperties
          strategy={strategy}
          onClose={() => setShowAdvanced(false)}
          onApply={next => { setStrategy(next); setValid(null); setShowAdvanced(false); notify('Advanced properties applied — validate and save to keep them'); }}
        />
      )}

      {/* Main Content Area */}
      <main className="main">
        {/* Real-time Streaming Ticker Marquee with hover pause & indicators */}
        <div className="live-ticker-bar">
          <div
            className={`ticker-live-badge ${iifl.configured && iifl.sessionPresent && wsConnected ? 'ticker-live-badge--live' : 'ticker-live-badge--delayed'}`}
            title={iifl.configured && iifl.sessionPresent && wsConnected ? 'IIFL Live Broker Stream Active' : 'Public market feed (Yahoo Finance delayed ~15m)'}
          >
            <span className="pulse-dot"></span>
            {iifl.configured && iifl.sessionPresent && wsConnected ? 'IIFL LIVE' : 'NSE/BSE (DELAYED ~15m)'}
          </div>
          {marketQuotes.slice(0, 12).map(q => {
            const isPos = (q.change || 0) >= 0;
            const flash = priceFlash[q.symbol] || priceFlash[q.ticker];
            return (
              <div
                key={q.symbol || q.ticker}
                className={`ticker-item ${flash === 'up' ? 'flash-up' : flash === 'down' ? 'flash-down' : ''}`}
                onClick={() => {
                  setChartSymbol(q.ticker || q.symbol);
                  go('Market Watch');
                }}
                title={`Click to view ${displaySymbol(q.symbol)} live chart`}
              >
                <b className="ticker-sym">{displaySymbol(q.symbol)}</b>
                <span className="ticker-price tabular-nums">
                  {q.ltp != null ? `₹${Number(q.ltp).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '--'}
                </span>
                <span className={`ticker-badge ${isPos ? 'positive' : 'negative'}`}>
                  {isPos ? <ArrowUpRight size={11} /> : <ArrowDownRight size={11} />}
                  {isPos ? '+' : ''}{Number(q.changePercent ?? 0).toFixed(2)}%
                </span>
              </div>
            );
          })}
        </div>

        {/* Top Header Bar with grouped 36px controls */}
        <header className="topbar">
          <div className="top-left">
            <button className="hamburger" onClick={() => setMenu(!menu)} aria-label="Toggle menu"><Menu size={18} /></button>
            <div className="crumb">Workspace <ChevronRight size={14} /> <b>{page}</b></div>
          </div>
          <div className="top-actions">
            <button
              type="button"
              className="btn-kill-switch"
              onClick={() => setShowKillConfirm(true)}
              title="Emergency Kill Switch: Stop all running paper workers and cancel open orders"
            >
              <Shield size={13} />
              KILL SWITCH
            </button>
            <div className="top-status-pill">
              <span className={feedIsLive ? 'pulse-dot' : 'dot gray'}></span> {feedIsLive ? 'LIVE MARKET FEED' : 'OFFLINE/DELAYED'}
            </div>
            <div className="top-status-pill">
              <span className={`dot ${activeExecMode === 'LIVE' ? 'green' : 'amber'}`}></span> {activeExecMode === 'LIVE' ? 'LIVE TRADING' : 'PAPER TRADING'}
            </div>
            <button
              type="button"
              className="theme-toggle-btn"
              onClick={() => setTheme(t => (t === 'dark' ? 'light' : 'dark'))}
              aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
              title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            >
              {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
            </button>
          </div>
        </header>

        {/* Kill Switch Confirmation Dialog */}
        {showKillConfirm && (
          <div className="modal-backdrop" onClick={() => setShowKillConfirm(false)}>
            <div className="modal-card" onClick={e => e.stopPropagation()}>
              <h3><Shield size={17} /> ARM EMERGENCY KILL SWITCH</h3>
              <p>
                Are you sure you want to trigger the Emergency Kill Switch?
                This will immediately stop all running paper strategy workers, cancel all open bracket and limit orders, and block new executions.
              </p>
              <div className="modal-actions">
                <button
                  type="button"
                  className="segmented-btn"
                  onClick={() => setShowKillConfirm(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn-solid-danger"
                  onClick={async () => {
                    setShowKillConfirm(false);
                    try {
                      const r = await fetch(API + '/broker/kill-switch', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ reason: 'Operator triggered Topbar Emergency Kill Switch' })
                      });
                      if (r.ok) {
                        const d = await r.json();
                        notify(`EMERGENCY KILL SWITCH ARMED: Stopped ${d.stoppedPaperWorkers || 0} paper workers & cancelled ${d.cancelledOrders || 0} open orders`);
                        loadData();
                      }
                    } catch {}
                  }}
                >
                  Confirm Kill Switch
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ==================== PAGE: OVERVIEW ==================== */}
        {page === 'Overview' && (
          <section className="content">
            <div className="welcome-row">
              <div>
                <div className="eyebrow">LIVE STRATEGY OPERATIONS</div>
                <h1>Trading Terminal Overview</h1>
                <p className="sub">Real-time market tracking, deterministic strategy modeling, and simulated paper execution.</p>
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button className="primary" onClick={createNewRuleStrategy}>
                  <Plus size={17} /> New Rule Builder Strategy
                </button>
                <button className="outline" onClick={() => { setStrategy(structuredClone(initialStrategy)); go('Strategy Builder'); }}>
                  + Legacy Indicator / SAR
                </button>
              </div>
            </div>

            <div className="metric-grid">
              <Metric
                icon={TrendingUp}
                label="Simulated Total P&L"
                value={(tradeStats.totalPnl >= 0 ? '+₹' : '-₹') + Math.abs(tradeStats.totalPnl).toLocaleString('en-IN')}
                note={`${tradeStats.openCount} open positions`}
                good={tradeStats.totalPnl >= 0}
              />
              <Metric icon={Layers3} label="Saved Strategies" value={strategies.length} note="Ready for execution" />
              <Metric icon={BriefcaseBusiness} label="Active Paper Trades" value={tradeStats.openCount} note={`Total: ${trades.length} events`} />
              <Metric icon={Wallet} label="Capital Deployed" value={'₹' + tradeStats.capitalDeployed.toLocaleString('en-IN')} note="Paper margin allocated" />
            </div>

            <div className="dashboard-grid">
              {/* Real-time Market Snapshot */}
              <div className="panel market-panel">
                <div className="panel-head">
                  <div>
                    <h3>Live Market Watchlist</h3>
                    <p>Real-time Indian Indices & Equities</p>
                  </div>
                  <span className="tag success">
                    <span className="pulse-dot" style={{ width: 5, height: 5, marginRight: 4 }}></span>
                    REAL-TIME
                  </span>
                </div>
                <div className="market-list">
                  {marketQuotes.slice(0, 5).map(m => {
                    const isPos = m.change >= 0;
                    return (
                      <div className="market-row" key={m.symbol} style={{ cursor: 'pointer' }} onClick={() => { setChartSymbol(m.ticker || m.symbol); go('Market Watch'); }}>
                        <div>
                          <b>{displaySymbol(m.symbol)}</b>
                          <small>{m.exchange} · {m.displayName}</small>
                        </div>
                        <strong>{m.ltp != null ? `₹${m.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2 })}` : '--'}</strong>
                        <span className={isPos ? 'positive' : 'negative'}>
                          {isPos ? '+' : ''}{m.changePercent ?? 0}%
                        </span>
                      </div>
                    );
                  })}
                </div>
                <div className="panel-foot">
                  <span>Click any instrument to open live interactive chart.</span>
                  <button className="text-btn" onClick={() => go('Market Watch')}>Open Market Watch <ChevronRight size={14} /></button>
                </div>
              </div>

              {/* Quick Navigation Card */}
              <div className="panel quick-panel">
                <div className="panel-head">
                  <div>
                    <h3>Quick actions</h3>
                    <p>Continue your workflow</p>
                  </div>
                  <Zap size={18} className="muted-icon" />
                </div>
                <Quick icon={Layers3} title="Strategy Builder" desc="Design entry conditions & option legs" onClick={() => go('Strategy Builder')} />
                <Quick icon={Activity} title="Interactive Live Charts" desc="5-min intraday candlestick & volume" onClick={() => go('Market Watch')} />
                <Quick icon={BriefcaseBusiness} title="Monitor Live Positions" desc="Unrealized P&L & square-off controls" onClick={() => go('Positions & Trades')} />
                <Quick icon={Wifi} title="IIFL Broker Integration" desc="Manage API userSession & credentials" onClick={() => go('IIFL Connection')} />
              </div>
            </div>

            {/* Phase 6 Dashboard Strategy Cards */}
            <div className="panel strategy-table" style={{ marginBottom: 18 }}>
              <div className="panel-head">
                <div>
                  <h3>Strategy Operations Dashboard Cards</h3>
                  <p>Live status, symbol, direction, SL, targets, re-entry, broker status, mode, active position & P&L</p>
                </div>
                <button className="primary small" onClick={createNewRuleStrategy}>
                  <Plus size={14} /> New Rule Builder
                </button>
              </div>
              <DashboardStrategyCards
                strategies={strategies.length ? strategies : [initialStrategy]}
                paperWorkerRuns={paperWorkerRuns}
                trades={trades}
                iifl={iifl}
                onSelectStrategy={s => { setStrategy(s); setValid(null); go('Strategy Builder'); }}
                onDuplicateStrategy={duplicateStrategyById}
                onDeleteStrategy={deleteStrategyById}
                onExportStrategy={exportStrategyClean}
              />
            </div>
          </section>
        )}

        {/* ==================== PAGE: STRATEGY BUILDER ==================== */}
        {page === 'Strategy Builder' && (() => {
            const isSar = (strategy.strategyType || 'INDICATOR') === 'LEVEL_SAR';
            return (
          <section className="content">
            <div className="welcome-row builder-title">
              <div>
                <div className="eyebrow">VISUAL STRATEGY DESIGNER • REAL-TIME SPOT PRICING</div>
                <h1>Strategy Builder</h1>
                <p className="sub">Construct multi-leg option and futures strategies with live underlying price context.</p>
              </div>
              <div className="builder-actions">
                <span className="tag draft">DRAFT</span>
                <button className="outline" onClick={() => setShowAdvanced(true)}><SlidersHorizontal size={16} /> Advanced Properties</button>
                <button className="outline" onClick={() => { if (window.confirm('Discard the current draft and start from the default template?')) setStrategy({ ...structuredClone(initialStrategy), id: crypto.randomUUID() }); }}>Reset</button>
                <button className="outline" onClick={validate}><ShieldCheck size={16} /> Validate</button>
                <button className="primary" onClick={save}><Download size={16} /> Save strategy</button>
              </div>
            </div>

            <div className="mode-strip" role="note">
              <span><b>Backtest</b> uses historical data — nothing is traded.</span>
              <span><b>Paper</b> simulates fills on live prices — <u>no real orders</u>.</span>
              <span><b>Live</b> places real broker orders and needs the separate go-live checklist.</span>
            </div>
            {saveState && (
              <div className={`save-banner ${saveState.ok ? 'ok' : 'bad'}`} role="status">
                <span>{saveState.msg}</span>
                {saveState.ok && (
                  <button type="button" className="outline" onClick={() => go('My Strategies')} style={{ marginLeft: 10, padding: '3px 10px', fontSize: 12, height: 26, alignSelf: 'center' }}>
                    Open My Strategies →
                  </button>
                )}
                <button onClick={() => setSaveState(null)} aria-label="Dismiss"><X size={13} /></button>
              </div>
            )}
            <div className="builder-layout">
              <div className="builder-main">
                {/* 01: Strategy Details */}
                <div className="panel section-card">
                  <SectionHead number="01" title="Strategy details" desc="Name your strategy and select the underlying market." />
                  <div className="form-grid">
                    <Field label="Strategy name">
                      <input value={strategy.name} onChange={e => update('name', e.target.value)} />
                    </Field>
                    <Field label="Strategy type">
                      <select
                        value={strategy.strategyType || 'INDICATOR'}
                        onChange={async e => {
                          const nextType = e.target.value;
                          if (nextType === 'RULE_BUILDER') {
                            await createNewRuleStrategy();
                          } else {
                            setStrategy(s => ({ ...structuredClone(initialStrategy), id: s.id, name: s.name, strategyType: nextType }));
                          }
                        }}
                      >
                        <option value="RULE_BUILDER">Rule Builder Studio (Phase 2–6 Visual Blocks)</option>
                        <option value="INDICATOR">Indicator strategy (Legacy)</option>
                        <option value="LEVEL_SAR">Level Stop-and-Reverse (SAR)</option>
                      </select>
                    </Field>
                    <Field label="Instrument / Watchlist">
                      <InstrumentPicker
                        value={strategy.instrument}
                        metaHint={instrMeta && instrMeta.symbol === strategy.instrument ? `${instrMeta.exchange || ''} ${instrMeta.type || ''}${instrMeta.lotSize ? ' • lot ' + instrMeta.lotSize : ''}`.trim() : undefined}
                        onChange={(sym, meta) => { update('instrument', sym); setInstrMeta(meta); }}
                      />
                    </Field>
                    {!isSar && (
                      <Field label="Candle timeframe">
                        <select value={strategy.timeframe} onChange={e => update('timeframe', e.target.value)}>
                          {['1m', '3m', '5m', '10m', '15m', '30m', '1h', '1d'].map(x => <option key={x} value={x}>{x}</option>)}
                        </select>
                      </Field>
                    )}
                    <Field label="Description">
                      <input value={strategy.description} onChange={e => update('description', e.target.value)} placeholder="Describe your trading logic" />
                    </Field>
                  </div>

                  {/* Live Underlying Price Ribbon */}
                  {selectedSpot && (
                    <div className="live-ribbon">
                      <div>
                        <span>Live Spot for <b>{strategy.instrument}</b>:</span>
                        <span className="spot-val">₹{selectedSpot?.toLocaleString('en-IN', { minimumFractionDigits: 2 })}</span>
                      </div>
                      <div>
                        <span>Strike Step: <b>{selectedStrikes?.strikeStep || 50} pts</b></span>
                        <span style={{ marginLeft: 14 }}>Lot Size: <b>{selectedStrikes?.lotSize ?? 'Unknown'}</b></span>
                        {selectedStrikes?.ATM && (
                          <span className="strike-preview-pill">
                            ATM Strike: {selectedStrikes.ATM.strike}
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                </div>

                {(strategy.strategyType || 'INDICATOR') === 'RULE_BUILDER' ? (
                  <Suspense fallback={<div className="panel section-card" style={{ padding: 24, color: 'var(--text-secondary)' }}>Loading Rule Builder...</div>}>
                    <RuleBuilderEditor
                      strategy={strategy}
                      setStrategy={setStrategy}
                      valid={valid}
                      onValidate={validate}
                      onSave={save}
                      notify={notify}
                      chartCandles={chartData?.candles || []}
                      onReloadStrategies={loadData}
                    />
                  </Suspense>
                ) : isSar ? (
                  <>
                    {/* 02: Levels & Entry */}
                    <div className="panel section-card">
                      <SectionHead number="02" title="Levels & entry" desc="Configure support/resistance levels and direction of initial trigger." />
                      <div className="form-grid">
                        <Field label="Level source">
                          <select
                            value={strategy.sar?.levelSource || 'MANUAL'}
                            onChange={e => {
                              const src = e.target.value;
                              const defStyle = src === 'MANUAL' ? 'REVERSAL' : 'BREAKOUT';
                              setStrategy(s => ({
                                ...s,
                                sar: { ...(s.sar || initialStrategy.sar), levelSource: src, entryStyle: defStyle }
                              }));
                            }}
                          >
                            <option value="MANUAL">Manual price levels</option>
                            <option value="CANDLE">From candle</option>
                          </select>
                        </Field>

                        <Field label="Entry style">
                          <select
                            value={strategy.sar?.entryStyle || (strategy.sar?.levelSource === 'CANDLE' ? 'BREAKOUT' : 'REVERSAL')}
                            onChange={e => update('sar.entryStyle', e.target.value)}
                          >
                            <option value="REVERSAL">Reversal: sell at upper level, buy at lower level</option>
                            <option value="BREAKOUT">Breakout: buy above upper level, sell below lower level</option>
                          </select>
                        </Field>

                        {strategy.sar?.levelSource === 'CANDLE' ? (
                          <>
                            <Field label="Candle timeframe">
                              <select
                                value={strategy.sar?.candle?.timeframe || '15m'}
                                onChange={e => update('sar.candle.timeframe', e.target.value)}
                              >
                                {['1m', '3m', '5m', '10m', '15m', '30m', '60m'].map(tf => (
                                  <option key={tf} value={tf}>{tf}</option>
                                ))}
                              </select>
                            </Field>

                            <Field label="Upper level from">
                              <select
                                value={strategy.sar?.candle?.upperSource || 'HIGH'}
                                onChange={e => update('sar.candle.upperSource', e.target.value)}
                              >
                                <option value="HIGH">Candle high</option>
                                <option value="LOW">Candle low</option>
                                <option value="OPEN">Candle open</option>
                                <option value="CLOSE">Candle close</option>
                                <option value="AVG">Candle average</option>
                                <option value="PREV_CLOSE">Previous day close</option>
                                <option value="DAY_HIGH">Day high</option>
                                <option value="DAY_LOW">Day low</option>
                              </select>
                            </Field>

                            <Field label="Lower level from">
                              <select
                                value={strategy.sar?.candle?.lowerSource || 'LOW'}
                                onChange={e => update('sar.candle.lowerSource', e.target.value)}
                              >
                                <option value="HIGH">Candle high</option>
                                <option value="LOW">Candle low</option>
                                <option value="OPEN">Candle open</option>
                                <option value="CLOSE">Candle close</option>
                                <option value="AVG">Candle average</option>
                                <option value="PREV_CLOSE">Previous day close</option>
                                <option value="DAY_HIGH">Day high</option>
                                <option value="DAY_LOW">Day low</option>
                              </select>
                            </Field>
                          </>
                        ) : (
                          <>
                            <Field label="Upper level (resistance)">
                              <input
                                type="number"
                                step="any"
                                value={strategy.sar?.manual?.upper ?? 330}
                                onChange={e => update('sar.manual.upper', +e.target.value)}
                              />
                            </Field>
                            <Field label="Lower level (support)">
                              <input
                                type="number"
                                step="any"
                                value={strategy.sar?.manual?.lower ?? 325}
                                onChange={e => update('sar.manual.lower', +e.target.value)}
                              />
                            </Field>
                          </>
                        )}
                      </div>
                      {strategy.sar?.levelSource === 'CANDLE' && (
                        <div className="hint" style={{ color: 'var(--text-secondary)', fontSize: 11, marginTop: 10 }}>
                          <CircleHelp size={14} /> Levels are locked when the first candle of this timeframe after the start time closes.
                        </div>
                      )}
                    </div>

                    {/* 03: Stop-loss, target & quantity ladder */}
                    <div className="panel section-card">
                      <SectionHead number="03" title="Stop-loss, target & quantity ladder" desc="Define points risk, reward, and reverse multipliers." />
                      <div className="form-grid">
                        <Field label="Stop-loss (points)">
                          <input
                            type="number"
                            step="any"
                            min="0.1"
                            value={strategy.sar?.slPoints ?? 1}
                            onChange={e => update('sar.slPoints', +e.target.value)}
                          />
                        </Field>
                        <Field label="Target (points)">
                          <input
                            type="number"
                            step="any"
                            min="0.1"
                            value={strategy.sar?.tgtPoints ?? 1.5}
                            onChange={e => update('sar.tgtPoints', +e.target.value)}
                          />
                        </Field>
                        <Field label="Base quantity (units)">
                          <input
                            type="number"
                            min="1"
                            step="1"
                            value={strategy.sar?.baseQuantity ?? 100}
                            onChange={e => update('sar.baseQuantity', +e.target.value)}
                          />
                        </Field>
                        <Field label="Maximum legs">
                          <select
                            value={strategy.sar?.maxLegs ?? 4}
                            onChange={e => {
                              const count = +e.target.value;
                              const currentMults = strategy.sar?.multipliers || [3, 2, 2];
                              const needed = count - 1;
                              let newMults = [...currentMults];
                              if (newMults.length < needed) {
                                while (newMults.length < needed) newMults.push(2);
                              } else {
                                newMults = newMults.slice(0, needed);
                              }
                              setStrategy(s => ({
                                ...s,
                                sar: { ...(s.sar || initialStrategy.sar), maxLegs: count, multipliers: newMults }
                              }));
                            }}
                          >
                            {[2, 3, 4, 5, 6].map(n => <option key={n} value={n}>{n} legs</option>)}
                          </select>
                        </Field>
                      </div>

                      <div style={{ marginTop: 16 }}>
                        <b style={{ fontSize: 12, color: 'var(--text-secondary)' }}>Reversal step multipliers:</b>
                        <div className="form-grid" style={{ marginTop: 8 }}>
                          {Array.from({ length: (strategy.sar?.maxLegs ?? 4) - 1 }).map((_, idx) => (
                            <Field key={idx} label={`Leg ${idx + 2} quantity multiplier`}>
                              <input
                                type="number"
                                step="any"
                                min="1"
                                value={strategy.sar?.multipliers?.[idx] ?? 2}
                                onChange={e => {
                                  const mults = [...(strategy.sar?.multipliers || [3, 2, 2])];
                                  mults[idx] = +e.target.value;
                                  update('sar.multipliers', mults);
                                }}
                              />
                            </Field>
                          ))}
                        </div>
                      </div>
                    </div>

                    {/* 04: Trading window & after target */}
                    <div className="panel section-card">
                      <SectionHead number="04" title="Trading window & after target" desc="Set allowed trading hours and post-target behavior." />
                      <div className="form-grid">
                        <Field label="Start time">
                          <input
                            type="time"
                            value={strategy.sar?.startTime || '09:15'}
                            onChange={e => update('sar.startTime', e.target.value)}
                          />
                        </Field>
                        <Field label="End time">
                          <input
                            type="time"
                            value={strategy.sar?.endTime || '15:00'}
                            onChange={e => update('sar.endTime', e.target.value)}
                          />
                        </Field>
                        <Field label="After target is hit">
                          <select
                            value={strategy.sar?.afterTarget || 'STOP'}
                            onChange={e => update('sar.afterTarget', e.target.value)}
                          >
                            <option value="STOP">Stop for the day</option>
                            <option value="RESTART">Restart with the same levels</option>
                          </select>
                        </Field>
                        {strategy.sar?.afterTarget === 'RESTART' && (
                          <Field label="Maximum restarts">
                            <input
                              type="number"
                              min="1"
                              max="10"
                              value={strategy.sar?.maxRestarts ?? 2}
                              onChange={e => update('sar.maxRestarts', +e.target.value)}
                            />
                          </Field>
                        )}
                      </div>
                    </div>

                    {/* 05: Trade ladder preview */}
                    <div className="panel section-card">
                      <SectionHead number="05" title="Trade ladder preview" desc="Live projection of sequential legs and cumulative loss exposure." />
                      
                      {strategy.sar?.levelSource === 'CANDLE' && (
                        <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12, background: 'var(--surface-subtle)', padding: '8px 12px', borderRadius: 6, border: '1px solid var(--border)' }}>
                          Example using live price <b>₹{selectedSpot != null ? selectedSpot.toLocaleString('en-IN') : '1,000'}</b>. Real levels come from the candle.
                        </div>
                      )}

                      {sarPreview?.ladders && (
                        <div className="sar-ladder-wrap">
                          {['SELL', 'BUY'].map(side => {
                            const ladder = sarPreview.ladders[side] || [];
                            return (
                              <div key={side} style={{ background: 'var(--surface-subtle)', border: '1px solid var(--border-light)', borderRadius: 8, padding: 12 }}>
                                <b style={{ fontSize: 13, color: 'var(--text-primary)', display: 'block', marginBottom: 8 }}>
                                  If {side} triggers first:
                                </b>
                                <div className="scroll-x" tabIndex={0} aria-label="Trade ladder table, scrolls horizontally on small screens">
                                  <table className="sar-table">
                                    <thead>
                                      <tr>
                                        <th>Leg</th>
                                        <th>Side</th>
                                        <th>Entry</th>
                                        <th>SL</th>
                                        <th>Target</th>
                                        <th>Qty</th>
                                        <th>Max loss</th>
                                        <th>Cumulative loss</th>
                                        <th>Exposure (notional)</th>
                                        <th>Status</th>
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {ladder.map(leg => {
                                        const isBlocked = sarPreview.blockedLegs?.includes(leg.legNo);
                                        const bd = (sarPreview.breakdown || []).find(r => r.legNo === leg.legNo);
                                        return (
                                          <tr key={leg.legNo} style={isBlocked ? { background: 'var(--loss-bg)' } : {}}>
                                            <td>
                                              <b>Leg {leg.legNo}</b>
                                            </td>
                                            <td style={{ color: leg.side === 'BUY' ? 'var(--profit-text)' : 'var(--loss-text)', fontWeight: 700 }}>{leg.side}</td>
                                            <td>₹{leg.entry}</td>
                                            <td>₹{leg.sl}</td>
                                            <td>₹{leg.tgt}</td>
                                            <td>{leg.qty}</td>
                                            <td>₹{leg.maxLoss?.toLocaleString('en-IN')}</td>
                                            <td>{bd ? `₹${bd.cumulativeLoss.toLocaleString('en-IN')}` : '—'}</td>
                                            <td>{bd?.exposure != null ? `₹${bd.exposure.toLocaleString('en-IN')}` : 'n/a (no live price)'}</td>
                                            <td className="ladder-status">
                                              {isBlocked ? <span className="status-pill blocked">⛔ BLOCKED</span> : <span className="status-pill ok">✓ Allowed</span>}
                                              {bd?.reasons?.map(r => <small key={r}>{r}</small>)}
                                              {bd?.capitalExceeded && <small className="warn">⚠ Notional exceeds max capital</small>}
                                            </td>
                                          </tr>
                                        );
                                      })}
                                    </tbody>
                                  </table>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      )}

                      {sarPreview?.calcNotes && (
                        <div className="calc-notes">
                          <b>How these numbers are calculated</b>
                          <span>Quantity is in {sarPreview.calcNotes.quantityUnit}. Leg loss = {sarPreview.calcNotes.legLoss}. Exposure = {sarPreview.calcNotes.exposure}.</span>
                          {!sarPreview.calcNotes.capitalCheckedAgainstLivePrice && <span className="warn">Capital not checked: no live price available for candle-based levels.</span>}
                        </div>
                      )}
                      <div style={{ marginTop: 14, fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
                        Worst-case loss if every leg stops out: <b style={{ color: 'var(--loss-text)' }}>₹{Number(sarPreview?.worstCaseLoss || 0).toLocaleString('en-IN')}</b>
                      </div>

                      {sarPreview?.warnings && sarPreview.warnings.length > 0 && (
                        <div style={{ background: 'var(--warning-bg)', border: '1px solid var(--warning-border)', color: 'var(--warning-text)', padding: '10px 14px', borderRadius: 6, marginTop: 12, fontSize: 12 }}>
                          <b>Warnings:</b>
                          {sarPreview.warnings.map(w => <div key={w} style={{ marginTop: 3 }}>• {w}</div>)}
                        </div>
                      )}

                      {sarPreview?.errors && sarPreview.errors.length > 0 && (
                        <div style={{ background: 'var(--loss-bg)', border: '1px solid var(--loss-border)', color: 'var(--loss-text)', padding: '10px 14px', borderRadius: 6, marginTop: 12, fontSize: 12 }}>
                          <b>Errors:</b>
                          {sarPreview.errors.map(err => <div key={err} style={{ marginTop: 3 }}>• {err}</div>)}
                        </div>
                      )}
                    </div>

                    {/* 06: Universal Risk Controls */}
                    <div className="panel section-card">
                      <SectionHead number="06" title="Universal Risk Controls" desc="Cap maximum daily loss and exposure." />
                      <div className="form-grid compact">
                        <Field label="Maximum daily loss (₹)"><input type="number" min="1" value={strategy.risk.maxDailyLoss} onChange={e => update('risk.maxDailyLoss', +e.target.value)} /></Field>
                        <Field label="Maximum capital allocation (₹)"><input type="number" min="1" value={strategy.risk.maxCapital} onChange={e => update('risk.maxCapital', +e.target.value)} /></Field>
                      </div>
                    </div>
                  </>
                ) : (
                  <>
                    {/* 02: Entry Conditions */}
                    <div className="panel section-card">
                      <SectionHead
                        number="02"
                        title="Entry conditions"
                        desc="Define when the strategy should trigger an entry."
                        right={
                          <select className="logic-select" value={strategy.entry.logic} onChange={e => update('entry.logic', e.target.value)}>
                            <option>AND</option>
                            <option>OR</option>
                          </select>
                        }
                      />
                      <div className="condition-stack">
                        {strategy.entry.conditions.map((c, i) => (
                          <div className="condition-row" key={i}>
                            <span className="condition-index">{i + 1}</span>
                            <Field label="Indicator">
                              <select value={c.indicator} onChange={e => {
                                let a = [...strategy.entry.conditions];
                                a[i] = { ...c, indicator: e.target.value };
                                update('entry.conditions', a);
                              }}>
                                {['SUPERTREND', 'RSI', 'EMA', 'SMA', 'VWAP', 'MACD', 'ATR', 'ADX', 'BOLLINGER'].map(x => <option key={x} value={x}>{x}</option>)}
                              </select>
                            </Field>
                            <Field label="Operator">
                              <select value={c.operator} onChange={e => {
                                let a = [...strategy.entry.conditions];
                                a[i] = { ...c, operator: e.target.value };
                                update('entry.conditions', a);
                              }}>
                                {['>', '<', '>=', '<=', '=', 'CROSSES_ABOVE', 'CROSSES_BELOW'].map(x => <option key={x} value={x}>{x === 'CROSSES_ABOVE' ? 'Crosses above' : x === 'CROSSES_BELOW' ? 'Crosses below' : x}</option>)}
                              </select>
                            </Field>
                            <Field label="Compare with">
                              <input value={c.value} onChange={e => {
                                let a = [...strategy.entry.conditions];
                                a[i] = { ...c, value: e.target.value };
                                update('entry.conditions', a);
                              }} />
                            </Field>
                            <Field label="Period">
                              <input type="number" min="1" value={c.period || 14} onChange={e => {
                                let a = [...strategy.entry.conditions];
                                a[i] = { ...c, period: +e.target.value };
                                update('entry.conditions', a);
                              }} />
                            </Field>
                            <button className="remove-btn" onClick={() => removeCondition(i)} title="Remove condition"><Trash2 size={16} /></button>
                          </div>
                        ))}
                      </div>
                      <button className="add-link" onClick={addCondition}><Plus size={15} /> Add condition</button>
                      <div className="hint"><CircleHelp size={15} /> Conditions are evaluated deterministically on candle close or live ticks.</div>
                    </div>

                    {/* 03: Positions Legs with Live Strike Preview */}
                    <div className="panel section-card">
                      <SectionHead
                        number="03"
                        title="Position Legs & Dynamic Strikes"
                        desc="Define option legs. Real strike prices and premiums are computed from live market spot prices."
                        right={<button className="outline small" onClick={addPosition}><Plus size={14} /> Add leg</button>}
                      />
                      <div className="position-head">
                        <span>Side</span><span>Underlying</span><span>Type</span><span>Strike Selection</span><span>Option</span><span>Expiry</span><span>Qty (lots)</span><span></span>
                      </div>
                      {strategy.positions.map((p, i) => {
                        let strikePreviewText = '';
                        if (selectedStrikes && p.instrumentType === 'OPTION') {
                          const levelData = selectedStrikes[p.strike] || selectedStrikes['ATM'];
                          const optData = levelData ? levelData[p.optionType] : null;
                          if (optData) {
                            strikePreviewText = `(${optData.strike} @ ~₹${optData.premium})`;
                          }
                        }

                        return (
                          <div className="position-row" key={i}>
                            <select className={p.side === 'BUY' ? 'buy-select' : 'sell-select'} value={p.side} onChange={e => setPosition(i, 'side', e.target.value)}>
                              <option>BUY</option><option>SELL</option>
                            </select>
                            <select value={p.underlying} onChange={e => setPosition(i, 'underlying', e.target.value)}>
                              <option>NIFTY</option><option>BANKNIFTY</option><option>FINNIFTY</option><option>SENSEX</option>
                            </select>
                            <select value={p.instrumentType} onChange={e => setPosition(i, 'instrumentType', e.target.value)}>
                              <option value="OPTION">Option</option><option value="FUTURE">Future</option><option value="EQUITY">Equity</option>
                            </select>
                            <StrikeSelector
                              leg={p}
                              atm={selectedStrikes?.ATM?.CE?.strike ?? selectedStrikes?.ATM?.strike}
                              step={selectedStrikes?.strikeStep}
                              onChange={v => setPosition(i, 'strike', v)}
                            />
                            <select value={p.optionType} onChange={e => setPosition(i, 'optionType', e.target.value)}>
                              <option>CE</option><option>PE</option><option>NA</option>
                            </select>
                            <select value={p.expiry} onChange={e => setPosition(i, 'expiry', e.target.value)}>
                              <option value="CURRENT_WEEK">Current week</option><option value="NEXT_WEEK">Next week</option><option value="MONTHLY">Monthly</option>
                            </select>
                            <input type="number" min="1" value={p.quantity} onChange={e => setPosition(i, 'quantity', +e.target.value)} />
                            <button className="remove-btn" onClick={() => setStrategy(s => ({ ...s, positions: s.positions.filter((_, j) => j !== i) }))}>
                              <Trash2 size={15} />
                            </button>
                          </div>
                        );
                      })}
                      <div className="hint"><CircleHelp size={15} /> Strikes automatically re-anchor when the live underlying index moves.</div>
                    </div>

                    {/* 04: Repair Logic */}
                    <div className="panel section-card">
                      <SectionHead number="04" title="Repair & re-entry" desc="Optional recovery logic after stop-loss hits." />
                      <div className="toggle-line">
                        <div><b>Enable repair logic</b><small>Allow controlled re-entry after an exit condition.</small></div>
                        <Toggle checked={strategy.repair.enabled} onChange={v => update('repair.enabled', v)} />
                      </div>
                      {strategy.repair.enabled && (
                        <div className="form-grid compact">
                          <Field label="Maximum attempts"><input type="number" min="1" max="5" value={strategy.repair.maxAttempts} onChange={e => update('repair.maxAttempts', +e.target.value)} /></Field>
                          <Field label="Cooldown (minutes)"><input type="number" min="1" value={strategy.repair.cooldownMinutes} onChange={e => update('repair.cooldownMinutes', +e.target.value)} /></Field>
                        </div>
                      )}
                    </div>

                    {/* 05: Overnight Protection */}
                    <div className="panel section-card">
                      <SectionHead number="05" title="Overnight protection" desc="Manage open exposures near market close (15:15 IST)." />
                      <div className="toggle-line">
                        <div><b>Enable overnight protection</b><small>Auto square-off before market close.</small></div>
                        <Toggle checked={strategy.overnight.enabled} onChange={v => update('overnight.enabled', v)} />
                      </div>
                      {strategy.overnight.enabled && (
                        <Field label="Protection action">
                          <select value={strategy.overnight.action} onChange={e => update('overnight.action', e.target.value)}>
                            <option value="SQUARE_OFF">Square off all open positions</option>
                            <option value="HEDGE">Apply configured hedge</option>
                          </select>
                        </Field>
                      )}
                    </div>

                    {/* 06: Risk Limits */}
                    <div className="panel section-card">
                      <SectionHead number="06" title="Universal Risk Controls" desc="Cap maximum daily loss and exposure." />
                      <div className="form-grid compact">
                        <Field label="Maximum daily loss (₹)"><input type="number" min="1" value={strategy.risk.maxDailyLoss} onChange={e => update('risk.maxDailyLoss', +e.target.value)} /></Field>
                        <Field label="Maximum capital allocation (₹)"><input type="number" min="1" value={strategy.risk.maxCapital} onChange={e => update('risk.maxCapital', +e.target.value)} /></Field>
                      </div>
                    </div>
                  </>
                )}

                {valid && (
                  <div className={`validation ${valid.valid ? 'ok' : 'bad'}`}>
                    <b>{valid.valid ? 'Validation passed successfully' : 'Validation needs attention'}</b>
                    {valid.errors?.map(e => <div key={e}>• {e}</div>)}
                  </div>
                )}

                <div className="builder-bottom">
                  <span><Shield size={15} /> All paper runs execute against real-time live market pricing.</span>
                  <div>
                    <button className="outline" onClick={validate}><ShieldCheck size={16} /> Validate strategy</button>
                    {isSar ? (
                      <button className="primary" onClick={startSarRun}><FlaskConical size={16} /> Start Stop-and-Reverse paper run</button>
                    ) : (
                      <button className="primary" onClick={paper}><FlaskConical size={16} /> Run paper simulation</button>
                    )}
                  </div>
                </div>
              </div>

              {/* Sidebar Summary */}
              <aside className="builder-side">
                <div className="panel side-card">
                  <div className="panel-head"><h3>Strategy summary</h3><MoreHorizontal size={17} /></div>
                  {(strategy.strategyType || 'INDICATOR') === 'RULE_BUILDER' ? (
                    <>
                      <div className="summary-item"><span>Schema</span><b>v2 RULE_BUILDER</b></div>
                      <div className="summary-item"><span>Symbol</span><b>{strategy.execution?.instrument?.underlying || strategy.meta?.symbol || 'NIFTY 50'}</b></div>
                      <div className="summary-item"><span>Signal Source</span><b>{strategy.execution?.instrument?.signalSource || 'INDEX_SPOT'}</b></div>
                      <div className="summary-item"><span>Exec Contract</span><b>{strategy.execution?.instrument?.instrumentType || 'INDEX_OPTIONS'} ({strategy.execution?.instrument?.optionType || 'CE'})</b></div>
                      <div className="summary-item"><span>Entry Rules</span><b>{(strategy.rules?.entry || []).length} rule(s)</b></div>
                      <div className="summary-item"><span>Indicators</span><b>{(strategy.indicators || []).length} configured</b></div>
                      <div className="summary-item"><span>Stop Loss</span><b>{strategy.stopLoss?.method}</b></div>
                      <div className="summary-item"><span>Targets</span><b>{(strategy.targets || []).filter(t => t.enabled).length} active</b></div>
                      <div className="summary-item"><span>Trailing SL</span><b>{(strategy.trailing || []).length} step(s)</b></div>
                      <div className="summary-item"><span>Re-entry</span><b>{strategy.reentry?.enabled ? `ON (max ${strategy.reentry.maxPerDay})` : 'OFF'}</b></div>
                      <div className="summary-item"><span>Max Daily Loss</span><b className="negative">₹{Number(strategy.risk?.maxDailyLoss || 2000).toLocaleString('en-IN')}</b></div>
                    </>
                  ) : isSar ? (
                    <>
                      <div className="summary-item"><span>Underlying</span><b>{strategy.instrument}</b></div>
                      <div className="summary-item"><span>Live Spot</span><b style={{ color: 'var(--link-color)' }}>{selectedSpot != null ? '₹' + selectedSpot.toLocaleString('en-IN', { minimumFractionDigits: 2 }) : 'Loading...'}</b></div>
                      <div className="summary-item"><span>Level source</span><b>{strategy.sar?.levelSource === 'CANDLE' ? `From candle (${strategy.sar?.candle?.timeframe || '15m'})` : `Manual (${strategy.sar?.manual?.upper ?? 330} / ${strategy.sar?.manual?.lower ?? 325})`}</b></div>
                      <div className="summary-item"><span>Entry style</span><b>{strategy.sar?.entryStyle || 'REVERSAL'}</b></div>
                      <div className="summary-item"><span>Stop-loss</span><b>{strategy.sar?.slPoints || 1} pts</b></div>
                      <div className="summary-item"><span>Target</span><b>{strategy.sar?.tgtPoints || 1.5} pts</b></div>
                      <div className="summary-item"><span>Base quantity</span><b>{strategy.sar?.baseQuantity || 100} units</b></div>
                      <div className="summary-item"><span>Max legs</span><b>{strategy.sar?.maxLegs || 4}</b></div>
                      <div className="summary-item"><span>Time window</span><b>{strategy.sar?.startTime || '09:15'} - {strategy.sar?.endTime || '15:00'}</b></div>
                      <div className="summary-item"><span>Worst-case loss</span><b className="negative">₹{sarPreview?.worstCaseLoss != null ? Number(sarPreview.worstCaseLoss).toLocaleString('en-IN') : '...'}</b></div>
                    </>
                  ) : (
                    <>
                      <div className="summary-item"><span>Underlying</span><b>{strategy.instrument}</b></div>
                      <div className="summary-item"><span>Live Spot</span><b style={{ color: 'var(--link-color)' }}>{selectedSpot != null ? '₹' + selectedSpot.toLocaleString('en-IN', { minimumFractionDigits: 2 }) : 'Loading...'}</b></div>
                      <div className="summary-item"><span>Timeframe</span><b>{strategy.timeframe}</b></div>
                      <div className="summary-item"><span>Entry rules</span><b>{(strategy.entry?.conditions || []).length} conditions · {strategy.entry?.logic || 'AND'}</b></div>
                      <div className="summary-item"><span>Position legs</span><b>{(strategy.positions || []).length} leg(s)</b></div>
                      <div className="summary-item"><span>Max capital</span><b>₹{Number(strategy.risk?.maxCapital || 0).toLocaleString('en-IN')}</b></div>
                      <div className="summary-item"><span>Max daily loss</span><b className="negative">₹{Number(strategy.risk?.maxDailyLoss || 0).toLocaleString('en-IN')}</b></div>
                    </>
                  )}
                  <div className="risk-meter">
                    <div><span>Risk controls</span><b>Configured</b></div>
                    <div className="meter"><i /></div>
                  </div>
                </div>

                <div className="panel side-card">
                  <div className="side-title"><ShieldCheck size={18} /><b>Execution Checklist</b></div>
                  {['Live spot & strikes calculated', 'Lot sizes and tick values verified', 'Daily loss cap active', 'Paper simulation mode enabled'].map(t => (
                    <div className="check-row" key={t}>
                      <span className="check-square" style={{ background: 'var(--profit-bg)', borderColor: 'var(--profit)', color: 'var(--profit-text)', display: 'grid', placeItems: 'center' }}>
                        <Check size={10} />
                      </span>
                      {t}
                    </div>
                  ))}
                </div>
              </aside>
            </div>

            {/* Stop-and-Reverse Runs Card */}
            <div className="panel" style={{ marginTop: 24, padding: 20 }}>
              <div className="panel-head" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <h3 style={{ fontSize: 16, fontWeight: 700, color: 'var(--text-primary)', margin: 0 }}>Stop-and-Reverse runs</h3>
                  <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: '4px 0 0' }}>Live status and history of Level SAR runs</p>
                </div>
                <button className="outline small" onClick={loadSarRuns}><RefreshCw size={14} /> Refresh</button>
              </div>

              {sarRuns.length === 0 ? (
                <div style={{ padding: '24px 0', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
                  No runs yet.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 14 }}>
                  {sarRuns.map(run => {
                    const isFinal = ['COMPLETED', 'STOPPED_MAX_LEGS', 'STOPPED_RISK_LIMIT', 'STOPPED_MANUAL', 'EXPIRED', 'ERROR'].includes(run.status);
                    const pnlGood = (run.realizedPnl || 0) >= 0;
                    return (
                      <div key={run.id} style={{ border: '1px solid var(--border-light)', borderRadius: 8, padding: 14, background: 'var(--surface)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, flexWrap: 'wrap', gap: 8 }}>
                          <div>
                            <b style={{ fontSize: 14, color: 'var(--text-primary)' }}>{run.strategyName}</b>
                            <span style={{ marginLeft: 8, color: 'var(--text-secondary)', fontSize: 12 }}>({displaySymbol(run.symbol)})</span>
                          </div>
                          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                            <span className={`badge-pill ${run.status === 'IN_TRADE' ? 'positive' : isFinal ? '' : 'badge-neutral'}`} style={{ fontSize: 11, textTransform: 'uppercase' }}>
                              {run.status}
                            </span>
                            {!isFinal && (
                              <button
                                className="outline small"
                                style={{ borderColor: 'var(--loss)', color: 'var(--loss-text)', padding: '3px 8px', fontSize: 11 }}
                                onClick={() => stopSarRun(run.id)}
                              >
                                Stop run
                              </button>
                            )}
                          </div>
                        </div>

                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 10, fontSize: 12, color: 'var(--text-secondary)', background: 'var(--surface-subtle)', padding: 10, borderRadius: 6, marginBottom: 8 }}>
                          <div>Levels: <b>{run.levels ? `Upper ₹${run.levels.upper} / Lower ₹${run.levels.lower}` : 'Waiting for levels'}</b></div>
                          <div>Last price: <b>{run.lastPrice != null ? `₹${run.lastPrice}` : '-'}</b></div>
                          <div>Current leg: <b>{run.currentLeg ? `Leg ${run.currentLeg.legNo} ${run.currentLeg.side} (${run.currentLeg.qty}u @ ₹${run.currentLeg.entry}) SL: ₹${run.currentLeg.sl} TGT: ₹${run.currentLeg.tgt}` : 'None'}</b></div>
                          <div>Realized P&L: <b style={{ color: pnlGood ? 'var(--profit-text)' : 'var(--loss-text)' }}>{pnlGood ? '+₹' : '-₹'}{Math.abs(run.realizedPnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</b></div>
                        </div>

                        {run.message && (
                          <div style={{ fontSize: 11, color: 'var(--text-muted)', fontStyle: 'italic', marginBottom: 6 }}>
                            Status note: {run.message}
                          </div>
                        )}

                        {run.legs && run.legs.length > 0 && (
                          <div style={{ marginTop: 8 }}>
                            <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 4 }}>Leg history:</div>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                              {run.legs.map((l, idx) => (
                                <span key={idx} style={{ fontSize: 11, padding: '2px 8px', borderRadius: 4, background: 'var(--surface-elevated)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}>
                                  Leg {l.legNo} {l.side} ({l.qty}u) → {l.exitReason} <b style={{ color: (l.pnl || 0) >= 0 ? 'var(--profit-text)' : 'var(--loss-text)' }}>{(l.pnl || 0) >= 0 ? '+' : ''}₹{l.pnl}</b>
                                </span>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </section>
        );
      })()}

        {/* ==================== PAGE: MARKET WATCH ==================== */}
        {page === 'Market Watch' && (
          <section className="content">
            <PageTitle
              eyebrow="REAL-TIME MARKET DATA & CHARTS"
              title="Live Market Watch"
              sub="Streaming quotes for Indian Indices, F&O stocks, and custom watchlists with interactive intraday charts."
            />

            {/* Watchlist Navigation Bar */}
            <div className="watchlist-nav">
              <div className="tabs-container">
                {['Indices', 'Equities', 'Custom Watchlist'].map(t => (
                  <button
                    key={t}
                    className={`tab-btn ${activeTab === (t === 'Custom Watchlist' ? 'Custom' : t) ? 'active' : ''}`}
                    onClick={() => setActiveTab(t === 'Custom Watchlist' ? 'Custom' : t)}
                  >
                    {t}
                  </button>
                ))}
              </div>

              {/* Symbol Search Bar */}
              <div className="search-box" ref={searchBoxRef}>
                <Search size={16} className="search-icon-pos" />
                <input
                  placeholder="Search NSE/BSE stock (e.g. Tata, Zomato)..."
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                  onFocus={() => {
                    if (searchQuery.trim() && searchResults.length > 0) {
                      setHasSearched(true);
                    }
                  }}
                />
                {searchQuery.trim() && hasSearched && (
                  <div className="search-dropdown">
                    {searchFallback && !searchLoading && (
                      <div style={{ padding: '6px 14px', fontSize: 11, color: 'var(--warning, #B7791F)', borderBottom: '1px solid var(--border-color)' }}>
                        Contract master unavailable — using fallback symbol search
                      </div>
                    )}
                    {searchLoading ? (
                      <div style={{ padding: '12px 14px', fontSize: 12, color: 'var(--text-secondary)' }}>
                        Searching Indian markets...
                      </div>
                    ) : searchResults.length > 0 ? (
                      searchResults.map(item => (
                        <div
                          key={item.symbol}
                          className="search-item"
                          onClick={() => addCustomSymbol(item.symbol)}
                        >
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                            <b style={{ fontSize: 13, color: 'var(--text-primary)', lineHeight: 1.2 }}>{item.cleanSymbol}</b>
                            <small style={{ fontSize: 11, color: 'var(--text-secondary)', lineHeight: 1.2 }}>{item.name} ({item.exchange})</small>
                          </div>
                          <button
                            type="button"
                            className="primary small"
                            style={{ whiteSpace: 'nowrap', flexShrink: 0, padding: '4px 9px', fontSize: 11 }}
                            onClick={(e) => {
                              e.stopPropagation();
                              addCustomSymbol(item.symbol);
                            }}
                          >
                            + Watchlist
                          </button>
                        </div>
                      ))
                    ) : (
                      <div style={{ padding: '12px 14px', fontSize: 12, color: 'var(--text-secondary)' }}>
                        No Indian stocks found
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* Interactive Live Candlestick & Area Chart */}
            <Suspense fallback={<div className="panel section-card" style={{ padding: 24, color: 'var(--text-secondary)' }}>Loading chart...</div>}>
              <MarketWatchChart
                symbol={displaySymbol(chartSymbol)}
                chartData={chartData}
                chartLoading={chartLoading}
                chartInterval={chartInterval}
                onIntervalChange={setChartInterval}
                chartRange={chartRange}
                onRangeChange={r => { setChartRange(r); setChartInterval(i => bestIntervalFor(r, i)); }}
                onRefresh={() => loadChart(chartSymbol, chartRange, chartInterval)}
                liveQuote={marketQuotes.find(q =>
                  q.symbol === chartSymbol ||
                  q.ticker === chartSymbol ||
                  displaySymbol(q.symbol) === displaySymbol(chartSymbol)
                )}
              />
            </Suspense>

            {/* Live Cards Grid */}
            <div className="market-cards-grid">
              {displayedQuotes.map(q => {
                const isSelected = (
                  chartSymbol === q.ticker ||
                  chartSymbol === q.symbol ||
                  displaySymbol(chartSymbol) === displaySymbol(q.symbol) ||
                  displaySymbol(chartSymbol) === displaySymbol(q.ticker)
                );
                return (
                  <MarketCard
                    key={q.symbol || q.ticker}
                    quote={q}
                    isSelected={isSelected}
                    onSelect={() => setChartSymbol(q.ticker || q.symbol)}
                    isCustom={activeTab === 'Custom'}
                    onRemove={removeCustomSymbol}
                    flash={priceFlash[q.symbol] || priceFlash[q.ticker]}
                  />
                );
              })}
            </div>
          </section>
        )}

        {/* ==================== PAGE: POSITIONS & TRADES ==================== */}
        {page === 'My Strategies' && (
          <SavedStrategies
            strategies={strategies}
            currentId={strategy.id}
            onLoad={loadSavedStrategy}
            onDuplicate={s => duplicateStrategyById(s.id)}
            onDelete={s => deleteStrategyById(s.id)}
            onValidate={validateSaved}
            onPaper={paperSaved}
            onNew={() => { setStrategy({ ...structuredClone(initialStrategy), id: crypto.randomUUID(), name: 'New strategy' }); setValid(null); setSaveState(null); go('Strategy Builder'); }}
          />
        )}

        {page === 'Positions & Trades' && (
          <section className="content">
            <PageTitle
              eyebrow="EXECUTION MONITOR & LIVE P&L"
              title="Positions & Trades"
              sub="Real-time profit & loss tracking against live market quotes with one-click square off."
            />

            <div className="metric-grid">
              <Metric
                icon={TrendingUp}
                label="Unrealized / Realized P&L"
                value={(tradeStats.totalPnl >= 0 ? '+₹' : '-₹') + Math.abs(tradeStats.totalPnl).toLocaleString('en-IN')}
                note="Calculated from live market ticks"
                good={tradeStats.totalPnl >= 0}
              />
              <Metric icon={BriefcaseBusiness} label="Open Positions" value={tradeStats.openCount} note="Active in market" />
              <Metric icon={FlaskConical} label="Closed Trades" value={tradeStats.closedCount} note="Historical simulations" />
              <Metric icon={Wallet} label="Margin Value" value={'₹' + tradeStats.capitalDeployed.toLocaleString('en-IN')} note="Paper capital engaged" />
            </div>

            <div className="panel strategy-table">
              <div className="panel-head">
                <div>
                  <h3>Execution Journal</h3>
                  <p>All trades reflect simulated executions at real-time market prices</p>
                </div>
                <button className="outline small" onClick={loadData}><RefreshCw size={14} /> Refresh</button>
              </div>

              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>EXEC TIME</th>
                      <th>STRATEGY</th>
                      <th>CONTRACT</th>
                      <th>SIDE</th>
                      <th>QTY</th>
                      <th>ENTRY (₹)</th>
                      <th>CURRENT (₹)</th>
                      <th>P&L (₹)</th>
                      <th>STATUS</th>
                      <th>ACTION</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.length ? trades.map(t => {
                      const isProfit = (t.pnl || 0) >= 0;
                      return (
                        <tr key={t.id}>
                          <td>{t.createdAt}</td>
                          <td><b>{t.strategy}</b></td>
                          <td>
                            <b>{t.contract || t.symbol}</b>
                            <small>{t.note}</small>
                          </td>
                          <td className={t.side === 'BUY' ? 'positive' : 'negative'}><b>{t.side}</b></td>
                          <td>{t.quantity} lots ({t.totalUnits || t.quantity} units)</td>
                          <td>{t.entryPrice != null ? `₹${t.entryPrice.toLocaleString('en-IN', { minimumFractionDigits: 2 })}` : '-'}</td>
                          <td>{t.currentPrice != null ? `₹${t.currentPrice.toLocaleString('en-IN', { minimumFractionDigits: 2 })}` : '-'}</td>
                          <td className={t.pnl != null ? (isProfit ? 'positive' : 'negative') : ''} style={{ fontWeight: 800 }}>
                            {t.pnl != null ? `${isProfit ? '+' : ''}₹${t.pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}` : '-'}
                            {t.pnlPercent != null && <small>{isProfit ? '+' : ''}{t.pnlPercent}%</small>}
                          </td>
                          <td>
                            <span className={`tag ${t.status === 'OPEN' ? 'open' : 'closed'}`}>
                              {t.status}
                            </span>
                          </td>
                          <td>
                            {t.status === 'OPEN' ? (
                              t.source === 'SAR ENGINE' ? (
                                <button className="btn-square-off" disabled title="Stop the run from Strategy Builder">
                                  Square Off
                                </button>
                              ) : (
                                <button className="btn-square-off" onClick={() => closeTrade(t.id)}>
                                  Square Off
                                </button>
                              )
                            ) : (
                              <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{t.exitPrice != null ? `Closed @ ₹${t.exitPrice}` : 'Closed'}</span>
                            )}
                          </td>
                        </tr>
                      );
                    }) : (
                      <tr>
                        <td colSpan="10" className="empty">No trades yet. Validate a strategy and click 'Run paper simulation'.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </section>
        )}

        {/* ==================== PAGE: IIFL CONNECTION ==================== */}
        {page === 'IIFL Connection' && (
          <IIFLConnection
            iifl={iifl}
            notify={notify}
            loadData={loadData}
            API={API}
          />
        )}

        {/* ==================== PAGE: PYTHON IMPORTER ==================== */}
        {page === 'Python Importer' && (
          <PythonImporter
            notify={notify}
            loadData={loadData}
            API={API}
            setPage={setPage}
            onDraftCreated={(draft) => {
              setStrategy(draft);
              setPage('Strategy Builder');
            }}
          />
        )}

        {/* ==================== PAGE: REPORTS ==================== */}
        {page === 'Reports' && (
          <Suspense fallback={<section className="content"><div className="panel section-card" style={{ padding: 24, color: 'var(--text-secondary)' }}>Loading reports...</div></section>}>
            <ReportsPage strategies={strategies} trades={trades} tradeStats={tradeStats} />
          </Suspense>
        )}

        {/* ==================== PAGE: SETTINGS ==================== */}
        {page === 'Settings' && (
          <section className="content">
            <PageTitle eyebrow="WORKSPACE PREFERENCES" title="Settings" sub="Environment configuration and market data connectors." />
            <div className="panel section-card">
              <h3>Application environment</h3>
              <div className="summary-item"><span>Frontend</span><b>React + Vite + Recharts</b></div>
              <div className="summary-item"><span>Backend</span><b>Python FastAPI 2.0</b></div>
              <div className="summary-item"><span>Market Feed</span><b className="positive">Live NSE/BSE Streaming Feed Active</b></div>
              <div className="summary-item"><span>Execution Mode</span><b className="positive">Paper Trading Engine</b></div>
              <div className="summary-item"><span>Backend Status</span><b>{health ? 'Connected' : 'Not connected'}</b></div>
            </div>
          </section>
        )}

        {/* ==================== PAGE: DOCUMENTATION ==================== */}
        {page === 'Documentation' && (
          <section className="content">
            <PageTitle eyebrow="HELP CENTER" title="Documentation" sub="User guide for live market tracking and paper simulation." />
            <div className="panel section-card">
              <h3>Quickstart Instructions</h3>
              <ol className="docs-list">
                <li><b>Live Market Data:</b> Automatically streams live quotes and 5-min candlestick charts for NIFTY 50, BANKNIFTY, FINNIFTY, SENSEX, and top Indian equities.</li>
                <li><b>Custom Watchlist:</b> Search for any stock on NSE/BSE in Market Watch and add it to your custom live watch dashboard.</li>
                <li><b>Strategy Builder:</b> Dynamic strike calculator instantly projects ATM/ITM/OTM strikes and estimated option premiums based on live spot movements.</li>
                <li><b>Paper Execution:</b> Clicking 'Run paper simulation' fills trades at real-time market prices and tracks live fluctuating P&L on the Positions page.</li>
              </ol>
            </div>
          </section>
        )}

        {/* Footer */}
        <footer className="footer">
          <span>© 2026 Alpha24 Strategy Studio • Version 2.0.0</span>
          <span><span className="dot green"></span> Live Market Feed Connected • Paper Mode</span>
        </footer>
      </main>

      {/* Floating Notification Toast */}
      {toast && (
        <div className="toast">
          <ShieldCheck size={17} />
          {toast}
          <button onClick={() => setToast('')}><X size={15} /></button>
        </div>
      )}
    </div>
  );
}

// UI Helpers
function Metric({ icon: Icon, label, value, note, good }) {
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

function Field({ label, children }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function SectionHead({ number, title, desc, right }) {
  return (
    <div className="section-head">
      <div className="section-num">{number}</div>
      <div className="section-title">
        <h3>{title}</h3>
        <p>{desc}</p>
      </div>
      {right && <div className="section-right">{right}</div>}
    </div>
  );
}

function Toggle({ checked, onChange }) {
  return (
    <button type="button" className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}>
      <i />
    </button>
  );
}

function Quick({ icon: Icon, title, desc, onClick }) {
  return (
    <button className="quick-row" onClick={onClick}>
      <span><Icon size={17} /></span>
      <div>
        <b>{title}</b>
        <small>{desc}</small>
      </div>
      <ChevronRight size={16} />
    </button>
  );
}

function PageTitle({ eyebrow, title, sub }) {
  return (
    <div className="welcome-row">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p className="sub">{sub}</p>
      </div>
    </div>
  );
}

function StrategyTable({ rows, onOpen }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>STRATEGY</th>
            <th>UNDERLYING</th>
            <th>TIMEFRAME</th>
            <th>STATUS</th>
            <th>ACTION</th>
          </tr>
        </thead>
        <tbody>
          {(rows.length ? rows : [{ id: 'sample', name: 'NIFTY Supertrend Intraday', instrument: 'NIFTY 50', timeframe: '5m', status: 'DRAFT' }]).map(s => (
            <tr key={s.id}>
              <td>
                <b>{s.name}</b>
                <small>{s.description || 'Example strategy configuration'}</small>
              </td>
              <td>{s.instrument}</td>
              <td>{s.timeframe}</td>
              <td><span className="tag draft">{s.status || 'DRAFT'}</span></td>
              <td>
                <button className="text-btn" onClick={() => onOpen(s.id === 'sample' ? initialStrategy : s)}>
                  Edit <ChevronRight size={14} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    console.error(error, info);
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--app-bg)', padding: 24 }}>
          <div style={{ background: 'var(--surface)', border: '1px solid var(--border-light)', borderRadius: 12, padding: 28, maxWidth: 520, boxShadow: 'var(--shadow-card)' }}>
            <h2 style={{ margin: '0 0 8px', color: 'var(--text-primary)' }}>Something went wrong</h2>
            <p style={{ margin: '0 0 16px', color: 'var(--text-secondary)', fontSize: 13, wordBreak: 'break-word' }}>
              {String(this.state.error?.message || this.state.error)}
            </p>
            <button className="primary" onClick={() => window.location.reload()}>Reload page</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById('root')).render(<ErrorBoundary><App /></ErrorBoundary>);