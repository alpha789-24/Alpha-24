import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Search, ChevronDown, Star } from 'lucide-react';

const API = 'http://127.0.0.1:8000/api';

// Baseline list shown when the search backend / contract master is unavailable.
// The real source of truth is the broker contract master + market search (see search below).
const BASE_INSTRUMENTS = [
  ...['NIFTY 50', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'NIFTY NEXT 50'].map(s => ({ symbol: s, name: s, group: 'Indices', type: 'INDEX', exchange: 'NSE' })),
  ...['NIFTY IT', 'NIFTY BANK', 'NIFTY AUTO', 'NIFTY PHARMA', 'NIFTY FMCG', 'NIFTY METAL', 'NIFTY REALTY', 'NIFTY ENERGY', 'NIFTY PSU BANK', 'NIFTY FIN SERVICE', 'NIFTY MEDIA'].map(s => ({ symbol: s, name: s, group: 'Sectoral indices', type: 'INDEX', exchange: 'NSE' })),
  ...['RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 'SBIN', 'BHARTIARTL', 'ITC', 'LT', 'KOTAKBANK', 'AXISBANK', 'HINDUNILVR', 'BAJFINANCE', 'MARUTI', 'SUNPHARMA', 'TATAMOTORS', 'TATASTEEL', 'WIPRO', 'ADANIENT', 'ASIANPAINT'].map(s => ({ symbol: s, name: s, group: 'Stocks', type: 'EQUITY', exchange: 'NSE' }))
];

const FAV_KEY = 'alpha_instrument_favs';
const loadFavs = () => { try { return JSON.parse(localStorage.getItem(FAV_KEY) || '[]'); } catch { return []; } };

export function InstrumentPicker({ value, onChange, metaHint }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [remote, setRemote] = useState([]);
  const [loading, setLoading] = useState(false);
  const [favs, setFavs] = useState(loadFavs);
  const [hi, setHi] = useState(0);
  const boxRef = useRef(null);

  useEffect(() => {
    const onDoc = e => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  // Debounced dynamic search: contract master first (all F&O underlyings), then market search (any NSE/BSE symbol).
  useEffect(() => {
    const term = q.trim();
    if (term.length < 2) { setRemote([]); return; }
    let dead = false;
    setLoading(true);
    const t = setTimeout(async () => {
      const out = [];
      try {
        const r = await fetch(`${API}/contracts/search?q=${encodeURIComponent(term)}&limit=30`);
        if (r.ok) {
          const d = await r.json();
          const seen = new Set();
          (d.items || []).forEach(it => {
            const und = it.underlyingInstrumentSymbol || it.tradingSymbol;
            if (seen.has(und)) return;
            seen.add(und);
            out.push({ symbol: und, name: it.underlyingInstrumentName || und, group: 'Broker contract master', type: it.instrumentType || 'F&O', exchange: it.exchange || it.segment, lotSize: it.lotSize, tickSize: it.tickSize, expiry: it.expiry });
          });
        }
      } catch {}
      try {
        const r = await fetch(`${API}/market/search?q=${encodeURIComponent(term)}`);
        if (r.ok) {
          const d = await r.json();
          (Array.isArray(d) ? d : d.results || []).forEach(it => {
            const sym = String(it.symbol || '').replace(/\.(NS|BO)$/i, '').replace(/^\^/, '');
            if (sym && !out.some(o => o.symbol.toUpperCase() === sym.toUpperCase())) {
              out.push({ symbol: sym, name: it.name || sym, group: 'Market search', type: it.type || 'EQUITY', exchange: it.exchange || 'NSE' });
            }
          });
        }
      } catch {}
      if (!dead) { setRemote(out); setLoading(false); }
    }, 250);
    return () => { dead = true; clearTimeout(t); };
  }, [q]);

  const results = useMemo(() => {
    const term = q.trim().toLowerCase();
    const local = BASE_INSTRUMENTS.filter(i => !term || i.symbol.toLowerCase().includes(term) || i.name.toLowerCase().includes(term));
    const merged = [...local];
    remote.forEach(r => { if (!merged.some(m => m.symbol.toUpperCase() === r.symbol.toUpperCase())) merged.push(r); });
    const favSet = new Set(favs);
    merged.sort((a, b) => (favSet.has(b.symbol) ? 1 : 0) - (favSet.has(a.symbol) ? 1 : 0));
    return merged.slice(0, 60);
  }, [q, remote, favs]);

  const toggleFav = (sym, e) => {
    e.stopPropagation();
    const next = favs.includes(sym) ? favs.filter(f => f !== sym) : [...favs, sym];
    setFavs(next);
    try { localStorage.setItem(FAV_KEY, JSON.stringify(next)); } catch {}
  };

  const pick = it => { onChange(it.symbol, it); setOpen(false); setQ(''); };
  const onKey = e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setHi(h => Math.min(h + 1, results.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHi(h => Math.max(h - 1, 0)); }
    else if (e.key === 'Enter' && results[hi]) { e.preventDefault(); pick(results[hi]); }
    else if (e.key === 'Escape') setOpen(false);
  };

  let lastGroup = null;
  return (
    <div className="instr-picker" ref={boxRef}>
      <button type="button" className="instr-trigger" onClick={() => setOpen(o => !o)} aria-haspopup="listbox" aria-expanded={open}>
        <span><b>{value || 'Select instrument'}</b>{metaHint && <small>{metaHint}</small>}</span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="instr-pop" role="listbox">
          <div className="instr-search">
            <Search size={14} />
            <input autoFocus value={q} onChange={e => { setQ(e.target.value); setHi(0); }} onKeyDown={onKey} placeholder="Search symbol or company / index name…" />
          </div>
          <div className="instr-list">
            {results.map((it, i) => {
              const head = it.group !== lastGroup ? (lastGroup = it.group) : null;
              return (
                <React.Fragment key={it.group + it.symbol}>
                  {head && <div className="instr-group">{head}</div>}
                  <div className={`instr-item ${i === hi ? 'hi' : ''} ${it.symbol === value ? 'sel' : ''}`} role="option" aria-selected={it.symbol === value}
                    onMouseEnter={() => setHi(i)} onClick={() => pick(it)}>
                    <div>
                      <b>{it.symbol}</b>
                      {it.name && it.name !== it.symbol && <small>{it.name}</small>}
                    </div>
                    <div className="instr-meta">
                      <span>{it.exchange}</span><span>{it.type}</span>{it.lotSize && <span>Lot {it.lotSize}</span>}
                      <button type="button" className={`fav ${favs.includes(it.symbol) ? 'on' : ''}`} onClick={e => toggleFav(it.symbol, e)} title="Favourite"><Star size={12} /></button>
                    </div>
                  </div>
                </React.Fragment>
              );
            })}
            {!results.length && <div className="instr-empty">{loading ? 'Searching…' : 'No instruments found'}</div>}
            {loading && results.length > 0 && <div className="instr-empty">Searching more…</div>}
          </div>
          <div className="instr-foot">Searches the broker contract master and market feed. Lot size, strike step and expiries refresh on change.</div>
        </div>
      )}
    </div>
  );
}
export default InstrumentPicker;
