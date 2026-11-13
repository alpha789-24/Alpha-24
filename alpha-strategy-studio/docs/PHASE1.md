# Phase 1

Schema v2 with strict Pydantic models and semantic validation, JSON/Mongo repositories, version history, JSON-to-Mongo migration, and pluggable FC/RC level providers. Paper trading stays the default. Live orders stay off.

### FC/RC provider contract

- `MANUAL`: enter all 16 values for one symbol and calendar day (`FC1H/L` through `FC3H/L`, and `RC1H/L` through `RC5H/L`). A partial sheet, invalid date, boolean, NaN, or infinity is rejected.
- `PLACEHOLDER`: explicitly dummy stored values for wiring tests only. It is disabled unless `LEVELS_PLACEHOLDER_ENABLED=true`; the UI displays a warning that these values are not market data and must not be used for trading.
- `FORMULA`: provider interface reserved for later definitions. Lookup raises exactly `formula not provided`; no formula is guessed or synthesized.

The provider registry is in `backend/app/levels.py` (`LevelProvider`, `ManualProvider`, `PlaceholderProvider`, `FormulaProvider`). The API payload uses strict Pydantic validation before persistence.

## Run the API (Windows)

```powershell
cd alpha-strategy-studio\backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Health: http://127.0.0.1:8000/api/health  
OpenAPI: http://127.0.0.1:8000/docs

`STORE_BACKEND` defaults to `json`. Data files stay in `backend/data/`.

## Run the UI (Windows)

```powershell
cd alpha-strategy-studio\frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173. The existing screens remain intact; a persistent FC/RC dummy-provider warning is shown. The dedicated rule-builder editor is a later phase.

## Tests

```powershell
cd alpha-strategy-studio\backend
.\.venv\Scripts\python.exe -m pytest -q tests_phase1.py tests_sar.py
```

`tests_smoke.py` also calls the live market feed, so it needs a network connection.

## MongoDB

Set `STORE_BACKEND=mongo` and `MONGODB_URI` in `backend/.env`, then:

```powershell
python scripts\migrate_json_to_mongo.py
```

The script copies JSON into MongoDB and leaves the files in place. It prints the database name and row counts, not credentials.

Collections and indexes:

| Collection | Index |
|---|---|
| `strategies` | unique `id` |
| `strategy_versions` | `strategyId` + `savedAt` |
| `paper_trades` | `id` |
| `sar_runs` | `id`, `strategyId` |
| `level_sheets` | unique `symbol` + `day` + `provider` |

## IIFL

Live orders are not enabled.

1. Put `IIFL_APP_KEY` and `IIFL_APP_SECRET` in `backend/.env` only.
2. Open IIFL Connection in the UI, or `GET /api/iifl/login-url`.
3. After redirect, the API keeps the session token in process memory.
4. Restarting the API clears that token. The UI still shows session status from `GET /api/iifl/status`. A reconnect is required after a restart.
5. Do not put the auth code or session token in a strategy, export, or frontend file.

## First rule strategy

1. `GET /api/strategies/template?strategyType=RULE_BUILDER`
2. Change the name, conditions, stop, and targets. Keep `execution.mode` as `PAPER`.
3. `POST /api/strategies/validate` with `{ "strategy": { ... } }`.
4. Enter a full manual sheet:

```json
PUT /api/levels
{
  "provider": "MANUAL",
  "symbol": "NIFTY 50",
  "day": "2026-01-05",
  "values": { "FC1H": 0, "FC1L": 0 }
}
```

The example above is incomplete on purpose. The API rejects a sheet until all 16 level names are present. Read one value with `GET /api/levels/lookup?symbol=NIFTY%2050&day=2026-01-05&name=FC1H&provider=MANUAL`.

5. `POST /api/strategies` saves the document and writes a version.
6. `GET /api/strategies/{id}/versions` lists snapshots. `POST /api/strategies/{id}/restore` with `{ "versionId": "..." }` puts one back.
7. `POST /api/paper/run` and `POST /api/sar/start` refuse this strategy type. No order is created.

`GET /api/strategies/schema` returns the JSON Schema for the rule document.

## API added in this phase

- `GET /api/strategies/template?strategyType=RULE_BUILDER`
- `GET /api/strategies/schema`
- `GET /api/strategies/{id}/versions`
- `POST /api/strategies/{id}/restore`
- `GET /api/levels`
- `PUT /api/levels`
- `GET /api/levels/lookup`

`POST /api/strategies/validate` now includes `warnings`. Existing strategy, market, paper, SAR, and IIFL routes still use the same paths. Saves of `INDICATOR` and `LEVEL_SAR` are not rejected for shape beyond the previous rules. A `RULE_BUILDER` save must pass validation.
