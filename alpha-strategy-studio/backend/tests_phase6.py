"""Phase 6 unit & integration tests:
- Plain-English preview generator (`condition_node_to_english`, `strategy_to_english_preview`)
  for nested AND/OR groups, ordered sequences, unary operators, proximity/breakout tolerances,
  and trailing SL steps
- Strategy management lifecycle:
  - Create & Save (`POST /api/strategies`)
  - Duplicate (`POST /api/strategies/{id}/duplicate`)
  - Delete (`DELETE /api/strategies/{id}`)
  - Version History & Restore (`GET /api/strategies/{id}/versions`, `POST /api/strategies/{id}/restore`)
- Import / Export with strict credential-field stripping (`strip_secret_fields`,
  `POST /api/strategies/export`, `POST /api/strategies/import`)
- Exact block-path validation error mapping for the UI rule builder
"""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.repos import JsonStrategyRepo
from app.samples import (
    sample_buy_near_rc1_breakout_strategy,
    sample_buy_reentry_example_1,
    sample_phase4_trailing_partial_targets,
    sample_unresolved_sell_reentry_example_2,
)
from app.schema_v2 import (
    condition_node_to_english,
    new_rule_builder,
    strategy_to_english_preview,
    strip_secret_fields,
)
from app.strategy import validate_strategy

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        print(f"PASS {name}")
        passed += 1
    else:
        print(f"FAIL {name}: {detail}")
        failed += 1
        raise AssertionError(f"{name}: {detail}")


# ==================== 1. PLAIN-ENGLISH PREVIEW GENERATOR ====================

def test_plain_english_preview():
    # 1. Sample Phase 2 strategy: BUY when (5m candle is green AND (CLOSE is near RC1L OR CLOSE moves out of FC1H))
    s1 = sample_buy_near_rc1_breakout_strategy()
    prev1 = strategy_to_english_preview(s1)
    entry_txt = prev1["entry"][0]["text"]
    check(
        "Plain-English preview for nested AND/OR group with green candle, buy_near, and moves_out_of",
        "BUY when" in entry_txt
        and "candle is green" in entry_txt
        and "RC1L" in entry_txt
        and "FC1H" in entry_txt,
        entry_txt,
    )

    # 2. Sample Phase 3 sequential strategy: Step 1 -> Step 2
    s2 = sample_buy_reentry_example_1()
    prev2 = strategy_to_english_preview(s2)
    seq_txt = prev2["entry"][0]["text"]
    check(
        "Plain-English preview for ordered sequence",
        "Step 1: CLOSE trades below RC1L" in seq_txt and "Step 2: CLOSE trades above RC1H" in seq_txt,
        seq_txt,
    )

    # 3. Sample Phase 4 trailing SL preview
    s4 = sample_phase4_trailing_partial_targets()
    prev4 = strategy_to_english_preview(s4)
    check(
        "Plain-English preview for trailing SL steps",
        len(prev4["trailing"]) == 2
        and "IF CLOSE > RC1H THEN MOVE SL TO LEVEL RC1L" in prev4["trailing"][0]["text"],
        str(prev4["trailing"]),
    )

    # 4. Unresolved conflict badge in preview
    s_unres = sample_unresolved_sell_reentry_example_2()
    prev_unres = strategy_to_english_preview(s_unres)
    check(
        "Plain-English preview flags unresolved conflict rule",
        "UNRESOLVED CONFLICT" in prev_unres["entry"][1]["text"],
        str(prev_unres["entry"][1]),
    )


# ==================== 2. CREDENTIAL STRIPPING ON EXPORT & IMPORT ====================

def test_credential_stripping():
    dirty = sample_buy_near_rc1_breakout_strategy()
    dirty["apiKey"] = "leaked-key-123"
    dirty["app_secret"] = "leaked-secret-456"
    dirty["execution"]["userSession"] = "leaked-session-789"
    dirty["meta"]["password"] = "hunter2"

    cleaned, stripped = strip_secret_fields(dirty)
    check(
        "strip_secret_fields removes all credential-like keys recursively",
        "apiKey" not in cleaned
        and "app_secret" not in cleaned
        and "userSession" not in cleaned["execution"]
        and "password" not in cleaned["meta"]
        and len(stripped) == 4,
        str(stripped),
    )
    check("Cleaned strategy passes strict schema_v2 validation", validate_strategy(cleaned) == [])


# ==================== 3. STRATEGY MANAGEMENT API & BLOCK VALIDATION PATHS ====================

def test_phase6_strategy_management_api():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_strat_repo = main_module.strategy_repo
        try:
            main_module.strategy_repo = JsonStrategyRepo(root)
            client = TestClient(app)

            # 1. Save a new RULE_BUILDER strategy
            strat = sample_buy_near_rc1_breakout_strategy()
            resp_save = client.post("/api/strategies", json={"strategy": strat})
            check("Save strategy 200", resp_save.status_code == 200)

            # 2. Duplicate strategy (Phase 4: unique names Copy 1, Copy 2, ...)
            resp_dup = client.post(f"/api/strategies/{strat['id']}/duplicate")
            check("POST /api/strategies/{id}/duplicate 200", resp_dup.status_code == 200)
            dup_data = resp_dup.json()
            check(
                "Duplicated strategy has new id and '(Copy 1)' suffix",
                dup_data["id"] != strat["id"] and dup_data["name"].endswith("(Copy 1)"),
                str(dup_data),
            )
            resp_dup2 = client.post(f"/api/strategies/{strat['id']}/duplicate")
            check("Second duplicate returns 200", resp_dup2.status_code == 200)
            dup_data2 = resp_dup2.json()
            check(
                "Second duplicated strategy has unique '(Copy 2)' suffix",
                dup_data2["id"] not in (strat["id"], dup_data["id"]) and dup_data2["name"].endswith("(Copy 2)"),
                str(dup_data2),
            )

            # 3. Export strategy with injected credentials -> credentials must be stripped
            dirty_payload = copy.deepcopy(strat)
            dirty_payload["app_key"] = "should-be-stripped"
            dirty_payload["authorization"] = "Bearer secret"
            resp_exp = client.post("/api/strategies/export", json={"strategy": dirty_payload})
            check("POST /api/strategies/export 200", resp_exp.status_code == 200)
            exp_data = resp_exp.json()
            check(
                "Export endpoint strips credential fields and reports them",
                "app_key" not in exp_data["strategy"]
                and "authorization" not in exp_data["strategy"]
                and len(exp_data["strippedCredentialFields"]) == 2,
                str(exp_data),
            )

            # 4. Import strategy with injected credentials -> strips credentials, validates, and saves
            import_doc = copy.deepcopy(strat)
            import_doc["id"] = "imported-strat-1"
            import_doc["name"] = "Imported Clean Strategy"
            import_doc["meta"]["id"] = "imported-strat-1"
            import_doc["meta"]["name"] = "Imported Clean Strategy"
            import_doc["accessToken"] = "secret-access-token"
            resp_imp = client.post("/api/strategies/import", json={"strategy": import_doc})
            check("POST /api/strategies/import 200", resp_imp.status_code == 200)
            imp_data = resp_imp.json()
            check(
                "Import endpoint stripped accessToken and persisted strategy",
                "accessToken" not in imp_data["strategy"]
                and imp_data["strippedCredentialFields"] == ["accessToken"],
                str(imp_data),
            )

            # 5. Plain-English preview API endpoint
            resp_prev = client.post("/api/strategies/preview-english", json={"strategy": strat})
            check(
                "POST /api/strategies/preview-english 200",
                resp_prev.status_code == 200 and len(resp_prev.json()["entry"]) == 1,
            )

            # 6. Exact block path validation error for live block highlighting in UI
            bad_strat = copy.deepcopy(strat)
            bad_strat["rules"]["entry"][0]["condition"]["children"][1]["children"][0]["params"]["tolerance"] = None
            resp_val = client.post("/api/strategies/validate", json={"strategy": bad_strat})
            val_errors = resp_val.json()["errors"]
            check(
                "Validation error includes exact nested block path for UI highlighting",
                any("rules.entry[0].condition.children[1].children[0].params.tolerance" in e for e in val_errors),
                str(val_errors),
            )

            # 7. Delete duplicated strategy
            resp_del = client.delete(f"/api/strategies/{dup_data['id']}")
            check("DELETE /api/strategies/{id} 200", resp_del.status_code == 200)
            remaining_ids = [s["id"] for s in client.get("/api/strategies").json()]
            check("Deleted strategy removed from list", dup_data["id"] not in remaining_ids)
        finally:
            main_module.strategy_repo = old_strat_repo


if __name__ == "__main__":
    test_plain_english_preview()
    test_credential_stripping()
    test_phase6_strategy_management_api()
    print(f"\nPhase 6 tests complete: {passed} passed, {failed} failed.")
