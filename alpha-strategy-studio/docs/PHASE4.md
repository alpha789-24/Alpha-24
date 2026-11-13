# Phase 4 — Exits, Stop Loss, Multi-Targets & Trailing SL Engine

Phase 4 implements the shared exit, stop-loss, multi-target partial exit, idempotent trailing stop-loss, and intrabar conflict resolution engine in `backend/app/exit_engine.py`, together with API endpoints and comprehensive unit/integration tests.

---

## 1. Modified & Added Files in Phase 4

| File | Description |
|---|---|
| `backend/app/exit_engine.py` | **New**: Implements all 5 stop-loss methods (`fixed_points`, `percentage`, `indicator`, `candle` with `CLOSE`/`HIGH_LOW`/`OPEN` + `bufferPercent`, `level`), multi-target resolution & partial exits (`resolve_targets`), idempotent monotonic trailing SL (`evaluate_trailing_steps` with full `slHistory`), and intrabar conflict handling (`SL_FIRST`, `TARGET_FIRST`, `LOWER_TIMEFRAME` in `step_open_position`). |
| `backend/app/samples.py` | **Updated**: Added `sample_phase4_trailing_partial_targets()` demonstrating a 4-lot strategy with level SL (`FC1L`), 3 partial targets (`points`, `percent`, and `price_level` summing to 4 lots), and 2 ordered trailing SL steps. |
| `samples/phase4_trailing_partial_targets.json` | **New**: Exported JSON sample strategy for Phase 4 multi-target partial exits and trailing SL. |
| `backend/app/main.py` | **Updated**: Added `POST /api/engine/exit/resolve` and `POST /api/engine/exit/step`. |
| `backend/tests_phase4.py` | **New**: 25 unit and API integration checks verifying all 5 stop-loss types, 3-target partial exits, quantity sum validation, trailing SL idempotency, LONG/SHORT monotonic guards, `slHistory` audit trail, and `SL_FIRST` / `TARGET_FIRST` / `LOWER_TIMEFRAME` intrabar resolution. |
| `docs/ASSUMPTIONS.md` & `README.md` | **Updated**: Documented Phase 4 semantics, API endpoints, and test commands. |

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
python tests_sar.py
python test_phase_b.py

# Or run all suites via pytest:
python -m pytest -q tests_phase1.py tests_phase2.py tests_phase3.py tests_phase4.py tests_sar.py
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

---

## 5. API Summary (Phase 4 Endpoints)

Full interactive OpenAPI documentation is available at `GET /docs`.

- `POST /api/engine/exit/resolve`: Given a `strategy`, `side` (`BUY`/`SELL`), `entryPrice`, `entryTime` (IST), and `candlesByTimeframe`, resolves the initial stop loss (`fixed_points`, `percentage`, `indicator`, `candle`, or `level`) and all ordered targets (`points`, `percent`, `price_level`), returning a complete managed `position` object.
- `POST /api/engine/exit/step`: Advances an open `position` across a `candle` (and optional `lowerTimeframeCandles` for `LOWER_TIMEFRAME` intrabar resolution), executes partial target exits or SL exits, evaluates ordered idempotent `TrailingStep` rules at bar close, enforces LONG/SHORT monotonic SL guards, and returns updated `position`, `exitsThisStep`, `trailingModifications`, and `intrabarResolutionUsed`.

---

## 6. How to Connect IIFL

1. Copy `backend/.env.example` to `backend/.env` and set `IIFL_APP_KEY` and `IIFL_APP_SECRET`.
2. Open the **IIFL Connection** tab in the UI or call `GET /api/iifl/login-url`.
3. After logging in, the session token is held in backend process memory only (`GET /api/iifl/status`). Live orders remain disabled (`liveOrdersEnabled: false`) until Phase 7 safety checks are completed.

---

## 7. Walkthrough: Testing Multi-Targets, Trailing SL & Intrabar Resolution

1. **Save Manual FC/RC Levels**:
   `PUT /api/levels` for `"symbol": "NIFTY 50"`, `"day": "2026-01-05"`, `"provider": "MANUAL"` with `FC1L = 23950`, `RC1L = 23980`, `RC1H = 24020`, `FC1H = 24050`, `RC2H = 24080`, `RC3H = 24120`.
2. **Open a Managed Position (`POST /api/engine/exit/resolve`)**:
   Pass `samples/phase4_trailing_partial_targets.json` with `entryPrice: 24000.0` (`quantity: 4`, `lotSize: 25`).
   - Initial SL resolves to `FC1L = 23950.0`.
   - Targets resolve to: Target 1 `@ 24020.0` (1 lot), Target 2 `@ 24060.0` (1 lot), Final Target 3 `@ 24120.0` (2 lots).
3. **Step Across a Bullish Candle (`POST /api/engine/exit/step`)**:
   Pass a 5m candle with `high: 24035`, `close: 24030`:
   - Target 1 (`24020.0`) fills 1 lot (`pnl: +500.0`), leaving `remainingQuantity: 3`.
   - Trailing Step 1 (`CLOSE > RC1H`) fires once and moves `currentStopLoss` from `23950.0` up to `RC1L = 23980.0`, recording the change in `position.slHistory`.
