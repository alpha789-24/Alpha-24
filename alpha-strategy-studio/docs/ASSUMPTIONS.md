# Assumptions & Exact Semantics (Phases 1, 2, 3, 4 & 5)

This document defines the exact semantics, configurable parameters, and safety rules enforced by the Alpha24 Strategy Studio backend through Phase 5.

## 1. FC/RC Level Entry & Formula Blocking

- **Supported Level Names (16 total)**: `FC1H`, `FC1L`, `FC2H`, `FC2L`, `FC3H`, `FC3L`, and `RC1H`, `RC1L`, `RC2H`, `RC2L`, `RC3H`, `RC3L`, `RC4H`, `RC4L`, `RC5H`, `RC5L`.
- **`MANUAL` Provider (Default & Active)**: Stores all 16 operator-entered levels per `symbol` and calendar `day` (`YYYY-MM-DD`). Saving a partial level sheet, boolean, `NaN`, or `Infinity` is rejected at the API and repository boundary.
- **`FORMULA` Provider (Blocked)**: Explicitly blocked (`formula not provided`) at both strategy validation and level lookup until the actual mathematical formulas are provided. No invented or guessed formulas exist in the codebase.
- **`PLACEHOLDER` Provider (Disabled by Default)**: Off unless `LEVELS_PLACEHOLDER_ENABLED=true` is explicitly set in `backend/.env` AND `levels.placeholderAck=true` in the strategy.

## 2. Condition Evaluation Modes & Intrabar Execution Settings

- **Per-Condition Evaluation Modes (`evaluateOn`)**:
  - `CANDLE_CLOSE` (default): Evaluates only on completed, closed candles whose close timestamp is $\le$ the evaluation timestamp (`evalTime` in IST).
  - `LIVE_TICK`: Evaluates against the incoming live tick (`liveTick.ltp`) or current forming bar on the primary timeframe.
  - `INTRABAR`: Allows intrabar high/low or tick inspection on the primary timeframe while still restricting higher timeframes to closed candles only.
- **Intrabar Conflict Setting (`execution.intrabarConflictOrder`)**:
  - Configurable as `SL_FIRST` (default), `TARGET_FIRST`, or `LOWER_TIMEFRAME` when a single candle touches both stop loss and target.

## 3. Exact Operator Semantics

- **Standard Comparisons (`>`, `<`, `>=`, `<=`, `==`)**: Compare resolved numeric `left` and `right` operands.
- **Stateful Crosses (`crosses_above`, `crosses_below`)**: Require two consecutive observations (`prev` and `cur`), stored per strategy ID and condition `node.id` in `evaluation_states`.
- **Configurable `trades_above` / `trades_below`**: Controlled per condition via `evaluateOn` or `params.tradeBasis` (`CANDLE_CLOSE`, `LIVE_TICK`, `INTRABAR` / `HIGH_LOW`).
- **Mandatory Proximity & Breakout Operators (Zero Hidden Defaults)**: `near`, `buy_near`, `sell_near`, `moves_out_of`, `out_of_fc1h`, `out_of_fc1l`, `out_of_range`, `breakout_above`, `breakout_below` require `params.tolerance > 0` and `params.toleranceUnit` in `{"points", "percent"}`.

## 4. Technical Indicators & Warm-Up Handling

All 10 indicators (`SMA`, `EMA`, `RSI` with independent `smoothingPeriod` average, `CCI`, `ATR`, `SUPERTREND`, `VWAP` with IST daily reset, `MACD`, `BOLLINGER`, and `STOCHASTIC`) are implemented in `backend/app/indicators.py` using pure Python and NumPy, returning `None` during their warm-up period so `ConditionEngine` never emits signals on incomplete indicator history.

## 5. Multi-Timeframe Alignment, IST Session & Holiday Enforcement

- **All timestamps are normalized to IST (`Asia/Kolkata`, `UTC+05:30`)**.
- **Session & Holiday Filtering**: Checks `session.tradingDays`, `session.holidayCalendar` (`NONE` vs `EXPLICIT` list of `YYYY-MM-DD` dates), and `session.startTime`–`session.endTime`.
- **Zero Look-Ahead on Higher Timeframes**: At evaluation time $T_{\text{eval}}$, only candles with $T_{\text{close}} \le T_{\text{eval}}$ are visible to higher-timeframe conditions and indicators.

## 6. Phase 3: Sequential Events & Re-entry Engine

- **Ordered Step Progression (`SequenceCondition`)**: Stores per-strategy, per-sequence state (`waitingStep`, `stepTimestamps`, `startedAt`, `expiresAt`). Steps never satisfy out of order, and never satisfy in the same evaluation tick unless `allowSameTick: true`. Resets automatically on: (1) IST day change, (2) trade entry, (3) explicit invalidation, and (4) `timeoutMinutes` expiry.
- **Re-entry Engine (`check_reentry_eligibility`)**: Enforces `enabled`, `maxPerDay`, `afterStopLoss`, `afterTarget`, `afterManualExit`, `cooldownMinutes`, allowed `directions`, and attaches dedicated `reentry.stopLoss` and `reentry.targets`. Preserves `SELL Re-entry Example 2` (`"enter BUY with SL RC3L"`) in `samples/sell_reentry_example_2_unresolved.json` flagged with `unresolvedConflict: true` and blocked from activation.

## 7. Phase 4: Exits, Stop Loss, Multi-Targets, Trailing SL & Intrabar Ambiguity

- **Stop Loss Types (`resolve_stop_loss_price`)**: `fixed_points`, `percentage`, `indicator`, `candle` (`CLOSE` / `HIGH_LOW` / `OPEN`, `candleCount`, `bufferPercent`), and `level` (FC/RC).
- **Targets & Partial Exits (`resolve_targets`)**: 3+ ordered targets (`points`, `percent`, `price_level`), optional disabled targets, final target always enabled, lot-sum validation, and partial position reductions.
- **Trailing Stop Loss Engine (`evaluate_trailing_steps`)**: Ordered idempotent `TrailingStep` rules (`firedTrailingStepIds`), monotonic enforcement (LONG SL never moves down; SHORT SL never moves up), and full `slHistory` audit trail.
- **Intrabar Ambiguity Resolution (`step_open_position`)**: `SL_FIRST` (default), `TARGET_FIRST`, or `LOWER_TIMEFRAME`, logged in `intrabarResolutionUsed` and `intrabarResolutionLog`.

## 8. Phase 5: Candle-by-Candle Backtest & Background Paper Trading Worker

- **Shared Engine Architecture**: `run_backtest` and `step_paper_worker_run` (`backend/app/backtest_engine.py`) call the exact same `ConditionEngine` (`condition_engine.py`) and `ExitEngine` (`exit_engine.py`) functions used in live/paper evaluation.
- **Strict No Look-Ahead Execution (`executionFillMode`)**:
  - `NEXT_CANDLE_OPEN` (default): Signals formed at candle `i` close are queued and filled at candle `i + 1`'s `open` price (provided candle `i + 1` is on the same trading day and inside session hours). At the moment of entry SL/target calculation on candle `i + 1`'s open, only candles `0 .. i` are closed and visible.
  - `SAME_CANDLE_CLOSE` (optional configurable): Fills at candle `i`'s `close` price.
- **Slippage, Brokerage & Charges**:
  - Adverse slippage (`slippagePoints + price * slippagePercent / 100`) is applied to entry fills and every partial/full exit fill (increasing `BUY` entry / `SELL` exit prices and decreasing `SELL` entry / `BUY` exit prices).
  - Order charges (`brokeragePerOrder + turnover * chargesPercent / 100`) are deducted on entry and on each partial/full exit fill.
- **Expiry & Session-End Handling (`expiryHandling`)**:
  - `SESSION_END_SQUARE_OFF` (default): Automatically squares off any open intraday position at the final bar of the trading day.
  - `SQUARE_OFF_ON_EXPIRY`: Squares off open positions at the close of `expiryDate`.
  - `ROLLOVER_NEXT_EXPIRY`: Logs a contract rollover event in `slHistory` on `expiryDate` and continues tracking the position.
  - `HOLD_ACROSS_DAYS`: Carries open positions across trading days until exited by SL, target, or exit rule (`END_OF_BACKTEST` closes any remaining position at the end of the dataset).
- **Paper Trading Worker (`paper_worker_runs`)**:
  - Supports `start`, `pause`, `resume`, `step`, and `stop` actions (`POST /api/paper/worker/*`).
  - Enforces daily `maxTradesPerDay` and `maxDailyLoss` guards (`STOPPED_RISK_LIMIT`), automatically closing any open position if combined realized + unrealized daily loss breaches `-maxDailyLoss`.
