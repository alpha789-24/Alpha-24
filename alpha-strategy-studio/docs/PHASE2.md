# Phase 2 — Condition Evaluation Engine (Backend)

Phase 2 implements the shared, deterministic backend condition evaluation engine and pure-Python/NumPy indicator library for `RULE_BUILDER` strategies, incorporating all user decisions for Phases 2–7. Live order placement remains disabled (`liveOrdersEnabled: false`).

---

## 1. Modified & Added Files

| File | Description |
|---|---|
| `backend/app/indicators.py` | **New**: Pure-Python/NumPy implementations of `CCI`, `RSI` (with independent `smoothingPeriod` average), `EMA`, `SMA`, `VWAP` (with IST daily reset), `SUPERTREND`, `ATR`, `MACD`, `BOLLINGER`, and `STOCHASTIC`, with strict warm-up handling (`None` returned until sufficient bars exist). |
| `backend/app/condition_engine.py` | **New**: Deterministic condition evaluation engine supporting all price fields (`OPEN`, `HIGH`, `LOW`, `CLOSE`, `LTP`, `VOLUME`), indicators, manual FC/RC levels, constants, stateful `crosses_above`/`crosses_below`, configurable `trades_above`/`trades_below`, unary candle tests, mandatory-tolerance proximity/breakout operators, IST session & holiday filtering, and multi-timeframe no-look-ahead alignment. |
| `backend/app/samples.py` | **New**: Sample Phase 2 strategies (`sample_buy_near_rc1_breakout_strategy` and `sample_unresolved_sell_reentry_example_2` with explicit conflict flagging). |
| `samples/phase2_buy_near_rc1_breakout.json` | **New**: Sample JSON strategy demonstrating `buy_near` `RC1L` and `moves_out_of` `FC1H` with mandatory tolerances and spot-signal / option-execution separation. |
| `samples/sell_reentry_example_2_unresolved.json` | **New**: Preserved SELL re-entry example 2 ("enter BUY with SL RC3L") flagged with `unresolvedConflict: true` and blocked from activation. |
| `backend/app/schema_v2.py` | **Updated**: Added `EVALUATION_MODES` (`CANDLE_CLOSE`, `LIVE_TICK`, `INTRABAR`), proximity/breakout operator validation, indicator output selection (`operand.output`), `InstrumentSelection` (`signalSource` vs `instrumentType`), and unresolved-conflict blocking. |
| `backend/app/repos.py` | **Updated**: Added `JsonEvaluationStateRepo` and `MongoEvaluationStateRepo` (`evaluation_states` collection + `eval_state_strategy_id` unique index) for persisting stateful crossing history across evaluations. |
| `backend/app/main.py` | **Updated**: Added Phase 2 endpoints (`POST /api/indicators/compute`, `POST /api/engine/evaluate`, `GET /api/engine/state/{strategy_id}`, `DELETE /api/engine/state/{strategy_id}`, `GET /api/strategies/samples`). |
| `backend/tests_phase2.py` | **New**: 48 unit and API integration assertions verifying all 10 indicators against reference values, warm-up handling, multi-timeframe no-look-ahead, stateful crosses, session/holiday checks, and mandatory tolerance/conflict validation. |
| `backend/tests_phase1.py` | **Updated**: Kept all Phase 1 tests passing under both direct `python` execution and `pytest`. |
| `backend/requirements.txt` | **Updated**: Added `numpy>=2.0.0`. |
| `.env.example` & `backend/.env.example` | **New**: Placeholder-only environment configuration templates. |
| `docs/ASSUMPTIONS.md` | **Updated**: Documented exact operator, indicator, warm-up, multi-timeframe, and conflict semantics. |

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

- Health Check: http://127.0.0.1:8000/api/health
- OpenAPI / Swagger UI: http://127.0.0.1:8000/docs

### Frontend UI (Windows PowerShell)

```powershell
cd alpha-strategy-studio\frontend
npm install
npm run dev
```

- Web Terminal: http://127.0.0.1:5173

---

## 3. How to Run Tests

From `alpha-strategy-studio\backend`:

```powershell
# Using pytest:
.\.venv\Scripts\python.exe -m pytest -q tests_phase1.py tests_phase2.py tests_sar.py

# Or running test suites directly with Python:
.\.venv\Scripts\python.exe tests_phase1.py
.\.venv\Scripts\python.exe tests_phase2.py
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
| `evaluation_states` | `eval_state_strategy_id` | `strategyId` (ASC) | Yes | Per-strategy condition evaluation state (`crosses_*` history) |

---

## 5. API Summary (Phase 2 Endpoints)

Full interactive OpenAPI documentation is served at `GET /docs` and `GET /openapi.json`.

- `POST /api/indicators/compute`: Compute any supported indicator (`CCI`, `RSI`, `EMA`, `SMA`, `VWAP`, `SUPERTREND`, `ATR`, `MACD`, `BOLLINGER`, `STOCHASTIC`) over a candle array and inspect warm-up `null` values and output series.
- `POST /api/engine/evaluate`: Evaluate a `RULE_BUILDER` strategy at a specific `evalTime` (IST) against `candlesByTimeframe` (e.g. `{"5m": [...], "15m": [...]}`), optional `liveTick`, and optional `executionQuote`. Returns `sessionAllowed`, per-rule and per-leaf diagnostic trees, emitted `signals`, and updated `state`. Set `persistState: true` to persist crossing state in `evaluation_states`.
- `GET /api/engine/state/{strategy_id}`: Inspect persisted crossing state for a strategy.
- `DELETE /api/engine/state/{strategy_id}`: Reset stored crossing state for a strategy.
- `GET /api/strategies/samples`: Retrieve the Phase 2 sample strategies, including the flagged `SELL Re-entry Example 2` document.
- `PUT /api/levels`, `GET /api/levels`, `GET /api/levels/lookup`: Manage and query manual per-symbol, per-day FC/RC level sheets (`FC1H/L`–`FC3H/L`, `RC1H/L`–`RC5H/L`).

---

## 6. How to Connect IIFL

1. Copy `backend/.env.example` to `backend/.env` and set `IIFL_APP_KEY` and `IIFL_APP_SECRET`. Never place credentials in frontend code or strategy JSON files.
2. Start the backend and call `GET /api/iifl/login-url` (or use the **IIFL Connection** tab in the UI).
3. Complete the IIFL login in your browser; the redirect callback (`GET /api/iifl/callback`) or manual session exchange (`POST /api/iifl/session`) stores the session token in backend process memory only.
4. Check connection status at `GET /api/iifl/status` (`configured` and `sessionPresent` flags). Live order execution remains gated and disabled (`liveOrdersEnabled: false`) until Phase 7 safety checks are completed.

---

## 7. Walkthrough: Creating & Testing Your First Phase 2 Rule Strategy

1. **Enter Manual FC/RC Levels for the Trading Day**:
   Send `PUT /api/levels` with all 16 levels (`FC1H`–`FC3L`, `RC1H`–`RC5L`) for `"symbol": "NIFTY 50"`, `"day": "2026-01-05"`, `"provider": "MANUAL"`.
2. **Load the Phase 2 Sample Strategy**:
   Call `GET /api/strategies/samples` and take the first strategy (`sample-phase2-buy-near-rc1`), or load `samples/phase2_buy_near_rc1_breakout.json`.
3. **Validate the Strategy**:
   Send `POST /api/strategies/validate` with `{"strategy": <strategy_json>}`. Confirm `"valid": true` and `"errors": []`.
   - Try setting `"tolerance": null` on the `buy_near` or `moves_out_of` condition and re-validating: the validator immediately flags the exact path (`rules.entry[0].condition.children[1].children[0].params.tolerance`).
4. **Save the Strategy**:
   Send `POST /api/strategies` with `{"strategy": <strategy_json>}`. A version snapshot is automatically recorded in `GET /api/strategies/{id}/versions`.
5. **Evaluate Conditions Against Market Candles**:
   Send `POST /api/engine/evaluate` with your saved strategy, 5-minute spot candles in `candlesByTimeframe`, `"evalTime": "2026-01-05T09:20:00+05:30"`, and `"persistState": true`. Inspect `entryResults` and `signals` to verify each leaf condition's resolved `leftValue`, `rightValue`, and `matched` status.
