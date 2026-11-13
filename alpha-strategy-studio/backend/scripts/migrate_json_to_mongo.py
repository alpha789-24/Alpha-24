"""Copy the current JSON store into MongoDB once.

The script is idempotent: each row is replaced by its id.
It does not delete JSON files and it does not print credentials.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.repos import (
    JsonLevelRepo, JsonListRepo, JsonStrategyRepo, ensure_indexes,
    migration_timestamp, mongo_client, redact_mongo_uri,
)
from app.store import read


def _replace_rows(collection, rows, id_field="id"):
    for index, row in enumerate(rows):
        document = dict(row)
        document["_id"] = str(document.get(id_field) or f"row-{index}")
        document["_position"] = index
        collection.replace_one({"_id": document["_id"]}, document, upsert=True)


def main():
    if settings.store_backend == "json":
        print("STORE_BACKEND is json. Set STORE_BACKEND=mongo and MONGODB_URI before migrating.")
        return 2
    try:
        client = mongo_client(settings.mongodb_uri)
        client.admin.command("ping")
    except Exception as exc:
        print(f"MongoDB is unavailable ({redact_mongo_uri(settings.mongodb_uri)}): {type(exc).__name__}")
        return 1
    db = client[settings.mongodb_db]
    ensure_indexes(db)

    strategies = JsonStrategyRepo().list()
    migrated = 0
    for index, row in enumerate(strategies):
        if not row.get("id"):
            print(f"Skipped strategy at position {index} because it has no id.")
            continue
        document = dict(row)
        document["_id"] = document["id"]
        document["_savedAt"] = migration_timestamp(index)
        db["strategies"].replace_one({"_id": document["_id"]}, document, upsert=True)
        migrated += 1

    versions = read("strategy_versions.json", [])
    if isinstance(versions, list):
        for row in versions:
            document = dict(row)
            document["_id"] = document.get("versionId") or document.get("strategyId")
            db["strategy_versions"].replace_one({"_id": document["_id"]}, document, upsert=True)

    _replace_rows(db["paper_trades"], JsonListRepo("paper_trades.json").list())
    _replace_rows(db["sar_runs"], JsonListRepo("sar_runs.json").list())
    _replace_rows(db["level_sheets"], JsonLevelRepo().list())

    print(
        "Migrated JSON into database "
        f"{settings.mongodb_db}: {migrated} strategies, "
        f"{len(versions) if isinstance(versions, list) else 0} versions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
