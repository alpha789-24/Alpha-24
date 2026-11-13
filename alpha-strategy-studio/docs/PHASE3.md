# Phase 3 — Sequential Events & Re-entry Engine

Phase 3 implements stateful multi-step `SequenceCondition` evaluation, persistent sequence/re-entry state across restarts, automatic resets (on IST day change, trade entry, explicit invalidation, and timeout expiry), full re-entry policy enforcement (`maxPerDay`, exit-reason filtering, `cooldownMinutes`, allowed `directions`, and dedicated `reentry.stopLoss` / `reentry.targets`), and sample BUY/SELL strategies in `samples/`.

---

## 1. Modified & Added Files in Phase 3

| File | Description |
|---|---|
| `backend/app/condition_engine.py` | **Updated**: Added `_evaluate_sequence` (strict step ordering, `allowSameTick`, `timeoutMinutes` expiry, step timestamp tracking), `check_reentry_eligibility`, and `EvaluationState` methods (`record_trade_entry`, `record_trade_exit`, `reset_sequence`, `reset_for_new_day`). |
| `backend/app/samples.py` | **Updated**: Added `sample_buy_reentry_example_1`, `sample_buy_reentry_example_2`, `sample_sell_reentry_example_1`, and `all_sample_strategies()` alongside `sample_buy_near_rc1_breakout_strategy` and `sample_unresolved_sell_reentry_example_2`. |
| `samples/buy_reentry_example_1.json` | **New**: Sequential BUY strategy (Step 1: `trades_below RC1L` $\to$ Step 2: `trades_above RC1H`) with re-entry after SL/Target, 5m cooldown, and SL at `RC1L`. |
| `samples/buy_reentry_example_2.json` | **New**: Sequential BUY strategy (Step 1: `trades_below FC1L` $\to$ Step 2: `candle_green` AND `crosses_above FC1H`). |
| `samples/sell_reentry_example_1.json` | **New**: Sequential SELL strategy (Step 1: `trades_above RC1H` $\to$ Step 2: `trades_below RC1L`) with re-entry after SL, 5m cooldown, and SL at `RC1H`. |
| `samples/sell_reentry_example_2_unresolved.json` | **Updated**: Preserved SELL re-entry example 2 ("enter BUY with SL RC3L") as a sequence rule flagged with `unresolvedConflict: true` and blocked from activation. |
| `backend/app/main.py` | **Updated**: Added `POST /api/engine/sequence/reset`, `POST /api/engine/trade-event`, and `POST /api/engine/reentry/check`, plus `autoRecordEntry` support on `POST /api/engine/evaluate`. |
| `backend/tests_phase3.py` | **New**: 41 unit and API integration checks verifying ordered sequence execution, out-of-order rejection, `allowSameTick`, persistence across restarts, all 4 reset triggers, re-entry cooldown/maxPerDay/exit-reason filtering, sample strategy files, and Phase 3 API routes. |
| `frontend/run.bat` & `frontend/start.ps1` | **New**: Windows helper scripts that automatically check for `node_modules\react` and run `npm install` before starting Vite. |
| `docs/ASSUMPTIONS.md` & `README.md` | **Updated**: Added Phase 3 sequence and re-entry semantics, API documentation, and walkthroughs. |

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

*(Or double-click `frontend\run.bat` or run `.\start.ps1` in PowerShell)*

- Web Terminal: http://127.0.0.1:5173

---

## 3. How to Run Tests

From `alpha-strategy-studio\backend`:

```powershell
# Using pytest:
.\.venv\Scripts\python.exe -m pytest -q tests_phase1.py tests_phase2.py tests_phase3.py tests_sar.py

# Or running each test suite directly with Python:
.\.venv\Scripts\python.exe tests_phase1.py
.\.venv\Scripts\python.exe tests_phase2.py
.\.venv\Scripts\python.exe tests_phase3.py
.\.venv\Scripts\python.exe tests_sar.py
.\.venv\Scripts\python.exe test_phase_b.py
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
| `evaluation_states` | `eval_state_strategy_id` | `strategyId` (ASC) | Yes | Per-strategy condition, sequence (`sequenceStates`), and re-entry (`reentryState`) state |

---

## 5. API Summary (Phase 3 Endpoints)

Full interactive OpenAPI documentation is available at `GET /docs`.

- `POST /api/engine/evaluate`: Evaluates leaf, group, and ordered `sequence` conditions, checks re-entry eligibility when `awaitingReentry` is active, attaches dedicated `reentry.stopLoss` and `reentry.targets` on re-entry signals, and persists `sequenceStates` and `reentryState` when `persistState: true`. Set `autoRecordEntry: true` to automatically record trade entry and reset sequence progress when a signal fires.
- `POST /api/engine/sequence/reset`: Explicitly invalidate and reset a sequence node (`sequenceId`, or omit to reset all sequences for `strategyId`) with an audit `reason`.
- `POST /api/engine/trade-event`: Record `ENTRY`, `REENTRY`, or `EXIT` (`exitReason: "STOP_LOSS" | "TARGET" | "MANUAL"`) to reset sequence state on entry and arm re-entry cooldown/counters on exit.
- `POST /api/engine/reentry/check`: Check whether a strategy is currently eligible for re-entry at `evalTime` (IST) and inspect remaining cooldown seconds, `reentriesToday`, and `maxPerDay`.
- `GET /api/strategies/samples`: Returns all 5 sample strategies (`phase2_buy_near_rc1_breakout`, `buy_reentry_example_1`, `buy_reentry_example_2`, `sell_reentry_example_1`, and `sell_reentry_example_2_unresolved`).

---

## 6. How to Connect IIFL

1. Copy `backend/.env.example` to `backend/.env` and set `IIFL_APP_KEY` and `IIFL_APP_SECRET`.
2. Open the **IIFL Connection** tab in the UI or call `GET /api/iifl/login-url`.
3. After logging in, the session token is held in backend memory only (`GET /api/iifl/status`). Live orders remain disabled (`liveOrdersEnabled: false`) until Phase 7 go-live gates are completed.

---

## 7. Walkthrough: Testing a Sequential Strategy & Re-entry

1. **Save Manual FC/RC Levels**:
   `PUT /api/levels` for `"symbol": "NIFTY 50"`, `"day": "2026-01-05"`, `"provider": "MANUAL"` with `RC1L = 23980` and `RC1H = 24020`.
2. **Load `samples/buy_reentry_example_1.json`**:
   This strategy defines a 2-step sequence: Step 1 `CLOSE trades_below RC1L`, Step 2 `CLOSE trades_above RC1H` (`allowSameTick: false`, `timeoutMinutes: 60`).
3. **Evaluate Step 1 (`09:20 IST`)**:
   Send `POST /api/engine/evaluate` with a 5m candle closing at `23970` and `"persistState": true`. The response shows `waitingStep: 1` and `signals: []`.
4. **Evaluate Step 2 (`09:25 IST`)**:
   Send `POST /api/engine/evaluate` with the next 5m candle closing at `24035`, `"persistState": true`, and `"autoRecordEntry": true`. The sequence completes, emits a `BUY` signal (`bucket: "ENTRY"`), and resets `waitingStep` to `0`.
5. **Simulate Stop Loss Exit & Re-entry Cooldown**:
   Send `POST /api/engine/trade-event` with `{"strategyId": "sample-phase3-buy-reentry-1", "eventType": "EXIT", "side": "BUY", "exitReason": "STOP_LOSS", "timestampIst": "2026-01-05T09:30:00+05:30"}`.
   - Calling `POST /api/engine/reentry/check` at `09:32:00+05:30` returns `"eligible": false` with `cooldownRemainingSeconds: 180`.
   - Calling `POST /api/engine/reentry/check` at `09:36:00+05:30` returns `"eligible": true` with the dedicated re-entry stop loss (`RC1L`) and target (`RC2H`).
