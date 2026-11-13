# Phase 7: Broker Execution, Capabilities, Go-Live Gates, Idempotency & Kill Switch

## Overview
Phase 7 completes the institutional execution and safety layer in `backend/app/broker_execution.py`, `backend/app/main.py`, `frontend/src/RuleBuilderPanels.jsx`, and `frontend/src/main.jsx`.

## Key Features Implemented

### 1. Broker Capabilities Catalog (`GET /api/broker/capabilities`)
- Single source of truth (`BROKER_CAPABILITIES`) defining supported:
  - `brokers`: `["IIFL", "PAPER_SIMULATOR"]`
  - `exchanges`: `["NFO", "NSE", "BFO", "BSE", "MCX"]`
  - `symbols`: `["NIFTY 50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]`
  - `instruments`: `["INDEX_OPTIONS", "INDEX_FUTURES", "EQUITY_CASH", "STOCK_OPTIONS", "STOCK_FUTURES"]`
  - `expiries`: `["CURRENT_WEEK", "NEXT_WEEK", "CURRENT_MONTH"]`
  - `strikes`: `["ATM", "ITM_1", "ITM_2", "ITM_3", "OTM_1", "OTM_2", "OTM_3", "SPOT"]`
  - `optionTypes`: `["CE", "PE", "FUT", "EQ"]`
  - `products`: `["INTRADAY", "MIS", "NRML", "CNC", "DELIVERY"]`
  - `orderTypes`: `["MARKET", "LIMIT", "SL", "SL-M"]`
  - `validities`: `["DAY", "IOC"]`
  - `modes`: `["PAPER", "LIVE"]`
  - `lotSizes`: per-symbol lot size mapping (`NIFTY 50: 25`, `BANKNIFTY: 15`, etc.)
- `validate_execution_settings` rejects any order or execution setting not present in `BROKER_CAPABILITIES`.

### 2. Version-Bound Go-Live Safety Gates (`POST /api/broker/live-gates` & `POST /api/broker/approve-live`)
- `compute_strategy_version_hash(strategy)` computes a deterministic SHA-256 fingerprint over all executable strategy sections (`rules`, `indicators`, `levels`, `stopLoss`, `targets`, `trailing`, `reentry`, `session`, `risk`, `execution`).
- Live trading requires **all** of the following gates to pass on the exact `versionHash`:
  1. `strategyValid`: Strategy passes full schema and semantic validation.
  2. `riskLimitsSet`: Positive `maxDailyLoss`, `maxCapital`, and `maxTradesPerDay`.
  3. `successfulBacktestOnVersion`: At least one completed backtest exists with matching `versionHash`.
  4. `minimumPaperTradingOnVersion`: At least one paper trading worker run or paper trade exists with matching `versionHash`.
  5. `explicitTypedConfirmationOnVersion`: Operator explicitly typed `"ENABLE LIVE TRADING FOR <Strategy Name>"` for this exact `versionHash`.
  6. `killSwitchDisarmed`: Emergency Kill Switch is not armed.
  7. `marketHoursOpen`: Current IST timestamp is within the NSE trading session (`09:15`–`15:30` IST on a non-holiday weekday) unless `amo=True`.
  8. `brokerSessionActive`: Server-side broker session is active (UI displays only `"Configured / Session Active"` and never exposes raw credentials).
- Editing any rule, indicator, stop loss, target, trailing step, or risk limit changes `versionHash` and automatically resets `liveTradingUnlocked` to `False` (`versionChangedReset: True`).

### 3. Order Lifecycle & Duplicate-Order Idempotency (`POST /api/broker/orders`, `POST /api/broker/orders/cancel`)
- Every order carries a unique `idempotencyKey`. Resubmitting the same `(strategyId, idempotencyKey)` returns the existing order with `duplicateBlocked: True` and never submits a duplicate broker order.
- Full lifecycle tracking with timestamped `history`:
  - `OPEN`: Active pending order (`LIMIT`, `SL`, `SL-M`, or `AMO`).
  - `FILLED`: Executed order with `filledPrice`, `filledQuantity`, and `filledAtIst`.
  - `REJECTED`: Rejected order with explicit `rejectionReason`.
  - `CANCELLED`: Cancelled via `POST /api/broker/orders/cancel` or by the Emergency Kill Switch.

### 4. Working Emergency Kill Switch (`POST /api/broker/kill-switch` & `POST /api/broker/kill-switch/reset`)
- Accessible in one click from both the Top Header Bar and Section 08 of the Strategy Builder.
- Immediately:
  1. Stops all running/paused background paper trading workers (`status = "STOPPED"`).
  2. Stops all active SAR runs (`status = "STOPPED"`).
  3. Squares off open paper trades (`status = "CLOSED"`, `exitReason = "KILL_SWITCH"`).
  4. Cancels all `OPEN` broker orders (`status = "CANCELLED"`).
  5. Revokes all live strategy approvals.
  6. Blocks all new `PAPER` and `LIVE` order submissions while armed.
