"""Phase 1 checks: schema v2, FC/RC lookup, and the JSON repository."""
import copy
import tempfile
from pathlib import Path

from app.config import settings
from app.levels import FormulaNotProvided, LevelBook, LevelError
from app.repos import JsonLevelRepo, JsonListRepo, JsonStrategyRepo, index_specifications
from app.schema_v2 import LEVEL_NAMES, MIGRATIONS, migrate_strategy, new_rule_builder
from app.strategy import compile_strategy, new_strategy, validate_strategy
from tests_sar import base_strategy


def full_levels():
    return {name: float(1000 + index) for index, name in enumerate(LEVEL_NAMES)}


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name} {detail}".strip())
    print(f"ok {name}")


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return True
    raise AssertionError(f"Expected {exc_type.__name__} to be raised")


def test_indicator_and_sar_still_validate():
    indicator = new_strategy()
    check("indicator valid", validate_strategy(indicator) == [])
    compiled = compile_strategy(indicator)
    check("indicator schema v1", compiled["schemaVersion"] == 1)
    check("sar valid", validate_strategy(base_strategy()) == [])
    migrated = migrate_strategy(indicator)
    check("indicator migration is a copy", migrated == indicator and migrated is not indicator)


def test_rule_builder_template():
    document = new_rule_builder()
    errors = validate_strategy(document)
    check("rule builder template valid", errors == [], str(errors))
    compiled = compile_strategy(document)
    check("compiled schema v2", compiled["schemaVersion"] == 2)
    check("compiled not executable", compiled["executable"] is False and compiled["liveOrdersEnabled"] is False)
    check("three targets", len(document["targets"]) == 3)


def test_validation_paths():
    document = new_rule_builder()
    document["rules"]["entry"][0]["condition"]["operator"] = "CROSSES_ABOVE"
    errors = validate_strategy(document)
    check("legacy operator rejected", any("rules.entry[0].condition.operator" in item for item in errors), str(errors))

    document = new_rule_builder()
    document["rules"]["entry"][0]["condition"]["operator"] = "near"
    errors = validate_strategy(document)
    check("near requires tolerance", any("tolerance" in item for item in errors), str(errors))

    document = new_rule_builder()
    document["rules"]["entry"][0]["condition"]["operator"] = "moves_out_of"
    document["rules"]["entry"][0]["condition"]["params"] = {"tolerance": 4, "toleranceUnit": "points"}
    check("explicit tolerance accepted", validate_strategy(document) == [])

    document = new_rule_builder()
    document["targets"] = document["targets"][:2]
    check("two targets rejected", any("at least three" in item for item in validate_strategy(document)))

    document = new_rule_builder()
    document["targets"][2]["enabled"] = False
    check("final target must stay on", any("final target" in item for item in validate_strategy(document)))

    document = new_rule_builder()
    document["targets"][2]["quantity"] = 2
    check("quantity sum", any("execution.quantity" in item for item in validate_strategy(document)))

    document = new_rule_builder()
    document["execution"]["mode"] = "LIVE"
    check("live mode rejected", validate_strategy(document) != [])

    document = new_rule_builder()
    document["password"] = "nope"
    check("secret key rejected", any("password" in item for item in validate_strategy(document)))

    document = new_rule_builder()
    document["levels"]["provider"] = "FORMULA"
    check("formula blocked", any("formula not provided" in item for item in validate_strategy(document)))

    document = new_rule_builder()
    document["levels"] = {"provider": "PLACEHOLDER", "placeholderAck": True}
    previous = settings.levels_placeholder_enabled
    settings.levels_placeholder_enabled = False
    try:
        errors = validate_strategy(document)
    finally:
        settings.levels_placeholder_enabled = previous
    check("placeholder off", any("PLACEHOLDER is off" in item for item in errors), str(errors))


def _step(node_id, label):
    return {
        "type": "leaf",
        "id": node_id,
        "enabled": True,
        "order": 1,
        "label": label,
        "left": {"kind": "price", "field": "CLOSE", "indicatorId": None, "level": None, "value": None},
        "operator": ">",
        "right": {"kind": "level", "field": None, "indicatorId": None, "level": "RC1H", "value": None},
        "timeframe": "5m",
        "candleRef": "previous",
        "evaluateOn": "candle_close",
        "params": {"tolerance": None, "toleranceUnit": None},
    }


def test_sequence_is_not_a_group():
    document = new_rule_builder()
    document["rules"]["entry"][0]["condition"] = {
        "type": "sequence",
        "id": "seq-1",
        "enabled": True,
        "order": 1,
        "label": "Two-step entry",
        "allowSameTick": False,
        "timeoutMinutes": None,
        "steps": [_step("step-1", "First step"), _step("step-2", "Second step")],
    }
    document["rules"]["entry"][0]["condition"]["steps"][1]["order"] = 2
    check("sequence accepted", validate_strategy(document) == [], str(validate_strategy(document)))

    nested = copy.deepcopy(document)
    nested["rules"]["entry"][0]["condition"]["steps"][1] = {
        "type": "sequence",
        "id": "seq-2",
        "enabled": True,
        "order": 2,
        "label": "Nested",
        "allowSameTick": False,
        "timeoutMinutes": None,
        "steps": [_step("step-3", "Inner"), _step("step-4", "Inner 2")],
    }
    errors = validate_strategy(nested)
    check("nested sequence rejected", errors != [] and any("seq" in item.lower() or "sequence" in item.lower() or "discriminator" in item.lower() or "type" in item.lower() for item in errors), str(errors))


def test_migration_hook():
    future = new_rule_builder()
    future["schemaVersion"] = 9
    try:
        migrate_strategy(future)
        raised = False
    except Exception as exc:
        raised = "newer" in str(exc)
    check("future version refused", raised)

    legacy = new_rule_builder()
    legacy["schemaVersion"] = 1

    def bump(document):
        moved = copy.deepcopy(document)
        moved["schemaVersion"] = 2
        return moved

    MIGRATIONS[1] = bump
    try:
        moved = migrate_strategy(legacy)
        check("registered migration runs", moved["schemaVersion"] == 2)
    finally:
        MIGRATIONS.pop(1, None)
    try:
        migrate_strategy(legacy)
        missing = False
    except Exception as exc:
        missing = "No migration" in str(exc)
    check("missing migration refused", missing)


def test_levels_and_repo():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = LevelBook(JsonLevelRepo(root))
        try:
            book.lookup("FORMULA", "NIFTY 50", "2026-01-05", "FC1H")
            formula_raised = False
        except FormulaNotProvided as exc:
            formula_raised = str(exc) == "formula not provided"
        check("formula lookup raises", formula_raised)

        previous = settings.levels_placeholder_enabled
        settings.levels_placeholder_enabled = False
        try:
            blocked = False
            try:
                book.lookup("PLACEHOLDER", "NIFTY 50", "2026-01-05", "FC1H")
            except LevelError:
                blocked = True
            check("placeholder lookup blocked", blocked)
        finally:
            settings.levels_placeholder_enabled = previous

        try:
            book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", {"FC1H": 10})
            partial = False
        except LevelError:
            partial = True
        check("partial manual sheet rejected", partial)

        sheet = book.save_sheet("MANUAL", "NIFTY 50", "2026-01-05", full_levels())
        quote = book.lookup("MANUAL", "NIFTY 50", "2026-01-05", "rc1h")
        check("manual lookup", quote.value == sheet["values"]["RC1H"] and quote.warning is None)

        settings.levels_placeholder_enabled = True
        try:
            book.save_sheet("PLACEHOLDER", "NIFTY 50", "2026-01-05", full_levels())
            warning = book.lookup("PLACEHOLDER", "NIFTY 50", "2026-01-05", "FC1H").warning
            check("placeholder warning", warning and "not market data" in warning)
        finally:
            settings.levels_placeholder_enabled = previous

        repo = JsonStrategyRepo(root)
        saved = repo.save({"id": "s-1", "name": "One", "strategyType": "INDICATOR"}, actor="tester")
        saved["name"] = "Two"
        repo.save(saved, actor="tester")
        versions = repo.list_versions("s-1")
        check("two versions", len(versions) == 2)
        check("diff mentions name", "name" in versions[0]["diffSummary"])
        restored = repo.restore("s-1", versions[1]["versionId"], actor="tester")
        check("restore previous name", restored["name"] == "One")

        trades = JsonListRepo("paper_trades.json", root)
        trades.save_all([{"id": "t-1", "status": "OPEN", "pnl": 1.5, "mode": "PAPER"}])
        loaded = trades.list()
        check("trade file round trip", loaded == [{"id": "t-1", "status": "OPEN", "pnl": 1.5, "mode": "PAPER"}])

    names = {item["name"] for item in index_specifications()}
    check("mongo indexes named", {"strategy_id", "versions_by_strategy", "trade_id", "run_id", "level_symbol_day_provider"} <= names)


def test_pydantic_schema_and_level_payload_validation():
    from pydantic import ValidationError
    from app.main import LevelSheetPayload
    from app.schema_v2 import RuleBuilderStrategy

    valid = new_rule_builder()
    assert RuleBuilderStrategy.model_validate(valid).schemaVersion == 2
    invalid = copy.deepcopy(valid)
    invalid["unexpected"] = True
    _assert_raises(ValidationError, RuleBuilderStrategy.model_validate, invalid)

    values = full_levels()
    payload = LevelSheetPayload(symbol=" NIFTY 50 ", day="2026-01-05", provider="manual", values=values)
    assert payload.symbol == "NIFTY 50"
    assert payload.provider == "MANUAL"
    assert len(payload.values) == len(LEVEL_NAMES)

    _assert_raises(ValidationError, LevelSheetPayload, symbol="NIFTY 50", day="2026-02-30", provider="MANUAL", values=values)
    _assert_raises(ValidationError, LevelSheetPayload, symbol="NIFTY 50", day="2026-01-05", provider="MANUAL", values={"FC1H": True})
    _assert_raises(ValidationError, LevelSheetPayload, symbol="NIFTY 50", day="2026-01-05", provider="MANUAL", values={"FC1H": 1})
    print("ok pydantic_schema_and_level_payload_validation")


def test_level_provider_contract_and_date_validation():
    from app.levels import FormulaProvider, ManualProvider, PlaceholderProvider, normalize_day

    assert isinstance(LevelBook(JsonLevelRepo()).providers["MANUAL"], ManualProvider)
    assert isinstance(LevelBook(JsonLevelRepo()).providers["PLACEHOLDER"], PlaceholderProvider)
    assert isinstance(LevelBook(JsonLevelRepo()).providers["FORMULA"], FormulaProvider)
    _assert_raises(LevelError, normalize_day, "2026-02-30")
    print("ok level_provider_contract_and_date_validation")


if __name__ == "__main__":
    test_indicator_and_sar_still_validate()
    test_rule_builder_template()
    test_validation_paths()
    test_sequence_is_not_a_group()
    test_migration_hook()
    test_levels_and_repo()
    test_pydantic_schema_and_level_payload_validation()
    test_level_provider_contract_and_date_validation()
    print("Phase 1 tests passed.")
