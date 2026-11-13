# Alpha24 Strategy Studio (Version 2.0 — Phase 6: Dark Trading Terminal & Visual Rule Builder)

A dark-theme visual strategy studio and paper trading terminal for Indian markets (NSE / BSE). It integrates a React + Vite + Recharts frontend, a FastAPI backend, real-time live market streaming feeds, interactive intraday charts, dynamic option strike calculators, a deterministic Phase 2–6 visual rule builder, plain-English rule previewer, sequence/re-entry/trailing-SL engine, candle-by-candle backtester, paper-trading worker, and IIFL session integration.

---

## What's Included Through Phase 6

- **Phase 1 — Strict Schema v2, Persistence & Pluggable FC/RC Levels**
- **Phase 2 — Backend Condition Evaluation & 10 Technical Indicators** (`CCI`, `RSI`, `EMA`, `SMA`, `VWAP`, `SUPERTREND`, `ATR`, `MACD`, `BOLLINGER`, `STOCHASTIC`)
- **Phase 3 — Stateful Sequential Events & Re-entry Engine** (`samples/*.json`)
- **Phase 4 — 5 Stop Loss Types, 3+ Partial Targets, Idempotent Monotonic Trailing SL & Intrabar Ambiguity Resolution**
- **Phase 5 — Shared Candle-by-Candle Backtest Simulator & Background Paper Trading Worker**
- **Phase 6 — Dark Trading Terminal UI & Visual Rule Builder**:
  - Dark trading terminal (`#070b14` / `#0f172a`, green = `BUY`, red = `SELL`, amber = warnings).
  - Collapsible Strategy Properties panels (Core/Execution, IST Session & Holidays, Indicators & Manual 16-Level FC/RC Sheet Editor, Visual Entry/Exit Rules, Stop Loss / Multi-Targets / Trailing SL, Re-entry & Risk Caps, and Backtest/Paper Worker).
  - Drag-and-drop & keyboard-operable rule blocks with visual `AND`/`OR`/`THEN STEP` connectors, nesting, duplicate/enable/disable/reorder, and exact-block validation error banners.
  - Plain-English preview on every rule (`BUY when (5m candle is green AND ...)`).
  - Overview Dashboard strategy cards, Version History modal with one-click restore, and credential-stripping JSON Import/Export.

---

## How to Run (Windows)

### 1. Start the Backend API

```powershell
cd alpha-strategy-studio\backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env -ErrorAction SilentlyContinue
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

*(Or double-click `backend\run.bat`)*

### 2. Start the Frontend

```powershell
cd alpha-strategy-studio\frontend
npm install
npm run dev
```

*(Or double-click `frontend\run.bat`)*

- Web App: http://127.0.0.1:5173

---

## Running Tests

```powershell
cd alpha-strategy-studio\backend
python tests_phase1.py
python tests_phase2.py
python tests_phase3.py
python tests_phase4.py
python tests_phase5.py
python tests_phase6.py
python tests_sar.py
python test_phase_b.py
```

See `docs/PHASE1.md` through `docs/PHASE6.md` and `docs/ASSUMPTIONS.md` for full details.
