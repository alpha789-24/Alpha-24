# Phase 5 — Candle-by-Candle Backtest & Background Paper Trading Worker

Phase 5 implements the deterministic candle-by-candle backtest simulator and the stateful paper trading worker (`backend/app/backtest_engine.py`), using the exact same `ConditionEngine` (`condition_engine.py`) and `ExitEngine` (`exit_engine.py`) as Phases 2–4 with zero duplicate logic.

---

## 1. Modified & Added Files in Phase 5

| File | Description |
|---|---|
| `backend/app/backtest_engine.py` | **New**: Shared candle-by-candle backtest simulator (`run_backtest`, `compute_backtest_metrics`) and background paper trading worker state machine (`new_paper_worker_run`, `step_paper_worker_run`) with no-look-ahead next-candle-open fills, slippage, brokerage/charges, expiry handling, and daily `maxTradesPerDay` / `maxDailyLoss` guards. |
| `backend/app/repos.py` | **Updated**: Added `backtest_runs` (`backtest_runs.json` / MongoDB `backtest_runs`) and `paper_worker_runs` (`paper_worker_runs.json` / MongoDB `paper_worker_runs`) repositories and indexes. |
| `backend/app/main.py` | **Updated**: Added `POST /api/backtest/run`, `GET /api/backtest/runs`, `POST /api/paper/worker/start`, `POST /api/paper/worker/pause`, `POST /api/paper/worker/resume`, `POST /api/paper/worker/stop`, `POST /api/paper/worker/step`, and `GET /api/paper/worker/runs`. |
| `backend/tests_phase5.py` | **New**: 19 unit and API integration checks covering all BUY/SELL example rules, next-candle-open no-look-ahead fills, slippage and brokerage deductions, trailing SL + partial targets in backtest, sequential re-entry, `maxTradesPerDay` / `maxDailyLoss` guards, same-candle SL+target (`SL_FIRST` vs `TARGET_FIRST`), and paper worker start/pause/resume/step/stop lifecycle. |
| `docs/ASSUMPTIONS.md` & `README.md` | **Updated**: Documented Phase 5 backtest and paper worker semantics, MongoDB collections, and test commands. |

---

## 2. Windows Run Steps (Backend & Frontend)

### Backend API (Windows PowerShell)

```powershell
cd alpha-strategy-studio\backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env -ErrorAction SilentlyContinue
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

*(Or double-click `backend\run.bat`)*

- Health Check: http://127.0.0.1:8000/api/health
- OpenAPI / Swagger UI: http://127.0.0.1:8000/docs

### Frontend UI (Windows PowerShell)

```powershell
cd alpha-strategy-studio\frontend
npm install
npm run dev
```

*(Or double-click `frontend\run.bat`)*

- Web Terminal: http://127.0.0.1:5173

---

## 3. How to Run Tests

From `alpha-strategy-studio\backend`:

```powershell
# Run each test suite directly with Python:
python tests_phase1.py
python tests_phase2.py
python tests_phase3.py
python tests_phase4.py
python tests_phase5.py
python tests_sar.py
python test_phase_b.py

# Or run all suites via pytest:
python -m pytest -q tests_phase1.py tests_phase2.py tests_phase3.py tests_phase4.py tests_phase5.py tests_sar.py
```

---

## 4. MongoDB Collections & Indexes

Set `STORE_BACKEND=mongo` and `MONGODB_URI=mongodb://127.0.0.1:27017` in `backend/.env`. Run `python scripts\migrate_json_to_mongo.py` to migrate JSON files to MongoDB.

| Collection | Index Name | Keys | Unique | Purpose |
|---|---|---|---|---|
| `strategies` | `strategy_id` | `id` (ASC) | Yes | Active strategy documents |
| `strategy_versions` | `versions_by_strategy` | `strategyId` (ASC), `savedAt` (DESC) | No | Complete version snapshots & diff audit trail |
| `paper_trades` | `trade_id` | `id` (ASC) | No | Simulated paper trade records |
| `sar_runs` | `run_id`, `run_strategy` | `id` (ASC) / `strategyId` (ASC) | No | Stop-and-Reverse worker states |
| `level_sheets` | `level_symbol_day_provider` | `symbol` (ASC), `day` (ASC), `provider` (ASC) | Yes | Per-symbol, per-trading-day FC/RC sheets |
| `evaluation_states` | `eval_state_strategy_id` | `strategyId` (ASC) | Yes | Per-strategy condition, sequence, and re-entry state |
| `backtest_runs` | `backtest_run_id`, `backtest_strategy_id` | `id` (ASC) / `strategyId` (ASC) | No | Persisted backtest results, metrics, equity curves & trade tables |
| `paper_worker_runs` | `paper_worker_id`, `paper_worker_strategy_id` | `id` (ASC) / `strategyId` (ASC) | No | Background `RULE_BUILDER` paper worker runs & daily risk guard states |

---

## 5. API Summary (Phase 5 Endpoints)

Full interactive OpenAPI documentation is available at `GET /docs`.

- `POST /api/backtest/run`: Runs a candle-by-candle backtest on `strategy` over `candlesByTimeframe` with configurable `executionFillMode` (`NEXT_CANDLE_OPEN` default or `SAME_CANDLE_CLOSE`), `slippagePoints`, `slippagePercent`, `brokeragePerOrder`, `chargesPercent`, `lotSizeOverride`, `initialCapitalOverride`, and `expiryHandling`. Returns summary `metrics` (`totalTrades`, `winners`, `losers`, `winRate`, `netProfit`, `maxDrawdown`, `avgProfit`, `avgLoss`, `riskToReward`, `profitFactor`), `equityCurve`, and `trades` (each with `entryReason`, `exitReason`, `slHistory`, `partialExits`, `intrabarResolutionUsed`).
- `GET /api/backtest/runs`: Lists saved backtest runs (optionally filtered by `strategyId`).
- `POST /api/paper/worker/start`: Starts a new stateful paper trading worker run for a `RULE_BUILDER` strategy.
- `POST /api/paper/worker/pause`, `POST /api/paper/worker/resume`, `POST /api/paper/worker/stop`: Lifecycle controls for an active paper worker run.
- `POST /api/paper/worker/step`: Steps a paper worker run across closed candles at `evalTime` (IST), enforcing daily `maxTradesPerDay` and `maxDailyLoss` guards.
- `GET /api/paper/worker/runs`: Lists paper worker runs and their active positions, closed trades, and daily risk metrics.

---

## 6. How to Connect IIFL

1. Copy `backend/.env.example` to `backend/.env` and set `IIFL_APP_KEY` and `IIFL_APP_SECRET`.
2. Open the **IIFL Connection** tab in the UI or call `GET /api/iifl/login-url`.
3. After logging in, the session token is held in backend process memory only (`GET /api/iifl/status`). Live orders remain disabled (`liveOrdersEnabled: false`) until Phase 7 safety gates are completed.
