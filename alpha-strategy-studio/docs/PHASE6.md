# Phase 6 — Dark Trading Terminal UI & Visual Rule Builder

Phase 6 upgrades the frontend into a high-contrast Dark Trading Terminal (`#070b14` canvas, `#0f172a` panels, `#10b981` green = BUY, `#ef4444` red = SELL, `#f59e0b` amber = warnings) and implements the full `RULE_BUILDER` visual block editor, plain-English rule previewer, collapsible Strategy Properties panels, Overview Dashboard cards, credential-stripping import/export, and version history manager.

---

## 1. Modified & Added Files in Phase 6

| File | Description |
|---|---|
| `frontend/src/RuleBuilderStudio.jsx` | **New**: Recursive `ConditionNodeEditor` supporting drag-and-drop and keyboard-operable (`Move Up`, `Move Down`, `Duplicate`, `Delete`, `Enable/Disable`) leaf, nested `AND`/`OR` group, and ordered `sequence` blocks, FC/RC level picker, indicator output picker, mandatory tolerance inputs, exact-block validation error banners, and `conditionToEnglish` plain-English preview generator. |
| `frontend/src/RuleBuilderPanels.jsx` | **New**: `CollapsibleSection` panels (Core & Instrument Signal vs Execution Separation, IST Session & Holidays, Technical Indicators & Manual 16-Level Sheet Editor, Visual Entry/Exit Rules, Stop Loss / 3+ Partial Targets / Trailing SL Steps, Re-entry & Daily Risk Caps, and Backtest Simulator + Equity Curve + Paper Worker controls), `DashboardStrategyCards`, and Version History modal. |
| `frontend/src/main.jsx` | **Updated**: Integrated `DashboardStrategyCards` on the Overview page and `RuleBuilderEditor` in Strategy Builder alongside existing `INDICATOR` and `LEVEL_SAR` workflows. |
| `frontend/src/style.css` | **Updated**: High-contrast dark trading terminal theme (`--green: #10b981`, `--red: #ef4444`, `--amber: #f59e0b`), visual `AND`/`OR`/`THEN STEP` connectors, accessible focus rings, and responsive laptop/mobile layouts. |
| `backend/app/schema_v2.py` | **Updated**: Added `strip_secret_fields`, `condition_node_to_english`, and `strategy_to_english_preview`. |
| `backend/app/repos.py` | **Updated**: Added `delete(strategy_id)` to `JsonStrategyRepo` and `MongoStrategyRepo`. |
| `backend/app/main.py` | **Updated**: Added `DELETE /api/strategies/{strategy_id}`, `POST /api/strategies/{strategy_id}/duplicate`, `POST /api/strategies/export`, `POST /api/strategies/import`, and `POST /api/strategies/preview-english`. |
| `backend/tests_phase6.py` | **New**: 17 checks verifying plain-English previews, recursive credential stripping on export/import, strategy duplicate/delete, and exact block-path validation error mapping. |
| `docs/ASSUMPTIONS.md` & `README.md` | **Updated**: Documented Phase 6 UI features, API endpoints, and test commands. |

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

### Frontend UI (Windows PowerShell)

```powershell
cd alpha-strategy-studio\frontend
npm install
npm run dev
```

*(Or double-click `frontend\run.bat`)*

- Open http://127.0.0.1:5173 in your browser.

---

## 3. How to Run Tests

From `alpha-strategy-studio\backend`:

```powershell
python tests_phase1.py
python tests_phase2.py
python tests_phase3.py
python tests_phase4.py
python tests_phase5.py
python tests_phase6.py
python tests_sar.py
python test_phase_b.py
```

---

## 4. Walkthrough: Using the Visual Rule Builder Studio

1. **Open the Dark Terminal**: Visit http://127.0.0.1:5173. On the **Overview** tab, inspect the **Strategy Operations Dashboard Cards** (showing status badge, symbol, BUY/SELL direction, SL, targets, re-entry, broker status, active position, and realized/unrealized P&L).
2. **Create or Load a Sample Rule Strategy**: Click **+ New Rule Builder Strategy** (or open **Strategy Builder** and choose a preset from the **Load Sample Preset** dropdown at the top, such as `Sample Phase 4: Multi-Target Partial Exits & Idempotent Trailing SL`).
3. **Verify the Plain-English Preview**: Every entry and exit rule displays a live **PLAIN-ENGLISH PREVIEW** bar (e.g. `BUY when (5m candle is green AND (CLOSE buy near RC1L (within 5 points) OR CLOSE moves out of FC1H by 3 points))`).
4. **Edit Blocks & Check Live Validation**: Drag-and-drop or use the Up/Down/Duplicate buttons on any condition block. If a mandatory parameter (such as `tolerance` on `buy_near`) is cleared and you click **Validate**, the error banner highlights that exact block.
5. **Run Backtest or Start Paper Worker**: Expand **Section 07 (Candle-by-Candle Backtest & Background Paper Worker)** and click **Run Backtest** to render the equity curve and trade-by-trade table (including `entryReason`, `exitReason`, `intrabarResolutionUsed`, and `slHistory`).
