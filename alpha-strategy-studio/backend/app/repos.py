"""Repository layer.

JSON files remain the default and keep the current list-shaped files.
MongoDB stores one document per row. STORE_BACKEND selects the implementation.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .store import read, write

_ACTOR = re.compile(r"[^A-Za-z0-9 ._@-]")


def clean_actor(actor: str | None) -> str:
    cleaned = _ACTOR.sub("", str(actor or "local")).strip()[:64]
    return cleaned or "local"


def diff_summary(previous, current) -> str:
    if previous is None:
        return "created"
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return "replaced"
    changed = []
    for key in sorted(set(previous) | set(current)):
        if previous.get(key) != current.get(key):
            changed.append(str(key))
    if not changed:
        return "saved with no field changes"
    shown = changed[:12]
    suffix = "" if len(changed) <= 12 else f" (+{len(changed) - 12} more)"
    return "updated: " + ", ".join(shown) + suffix


def _strip_internal(document: dict) -> dict:
    return {key: value for key, value in document.items() if not str(key).startswith("_")}


class JsonListRepo:
    def __init__(self, filename: str, data_dir=None):
        self.filename = filename
        self.data_dir = data_dir

    def list(self) -> list:
        rows = read(self.filename, [], data_dir=self.data_dir)
        return rows if isinstance(rows, list) else []

    def save_all(self, rows: list) -> None:
        write(self.filename, list(rows), data_dir=self.data_dir)


class JsonStrategyRepo:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir
        self.filename = "strategies.json"
        self.versions_filename = "strategy_versions.json"

    def list(self) -> list:
        rows = read(self.filename, [], data_dir=self.data_dir)
        return rows if isinstance(rows, list) else []

    def get(self, strategy_id: str):
        for row in self.list():
            if row.get("id") == strategy_id:
                return row
        return None

    def save(self, strategy: dict, actor: str | None = None, summary_override: str | None = None) -> dict:
        if not isinstance(strategy, dict) or not strategy.get("id"):
            raise ValueError("Strategy id is required")
        stored = copy.deepcopy(strategy)
        rows = self.list()
        previous = next((row for row in rows if row.get("id") == stored["id"]), None)
        summary = summary_override or diff_summary(previous, stored)
        rows = [row for row in rows if row.get("id") != stored["id"]]
        rows.insert(0, stored)
        write(self.filename, rows, data_dir=self.data_dir)
        self._append_version(stored, clean_actor(actor), summary)
        return stored

    def list_versions(self, strategy_id: str) -> list:
        rows = read(self.versions_filename, [], data_dir=self.data_dir)
        if not isinstance(rows, list):
            return []
        return [row for row in rows if row.get("strategyId") == strategy_id]

    def restore(self, strategy_id: str, version_id: str, actor: str | None = None) -> dict:
        match = next((row for row in self.list_versions(strategy_id) if row.get("versionId") == version_id), None)
        if match is None or not isinstance(match.get("snapshot"), dict):
            raise KeyError(version_id)
        snapshot = copy.deepcopy(match["snapshot"])
        if snapshot.get("id") != strategy_id:
            raise KeyError(version_id)
        return self.save(snapshot, actor=actor, summary_override=f"restored from {version_id}")

    def delete(self, strategy_id: str) -> bool:
        rows = self.list()
        filtered = [row for row in rows if row.get("id") != strategy_id]
        if len(filtered) == len(rows):
            return False
        write(self.filename, filtered, data_dir=self.data_dir)
        return True

    def _append_version(self, strategy: dict, actor: str, summary: str) -> dict:
        version = {
            "versionId": str(uuid4()),
            "strategyId": strategy.get("id"),
            "savedAt": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "diffSummary": summary,
            "snapshot": copy.deepcopy(strategy),
        }
        rows = read(self.versions_filename, [], data_dir=self.data_dir)
        if not isinstance(rows, list):
            rows = []
        rows.insert(0, version)
        write(self.versions_filename, rows, data_dir=self.data_dir)
        return version


class JsonLevelRepo:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir
        self.filename = "level_sheets.json"

    def list(self) -> list:
        rows = read(self.filename, [], data_dir=self.data_dir)
        return rows if isinstance(rows, list) else []

    def get(self, sheet_id: str):
        for row in self.list():
            if row.get("id") == sheet_id:
                return row
        return None

    def save(self, sheet: dict) -> dict:
        rows = [row for row in self.list() if row.get("id") != sheet.get("id")]
        rows.insert(0, copy.deepcopy(sheet))
        write(self.filename, rows, data_dir=self.data_dir)
        return sheet


class JsonEvaluationStateRepo:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir
        self.filename = "evaluation_states.json"

    def list(self) -> list:
        rows = read(self.filename, [], data_dir=self.data_dir)
        return rows if isinstance(rows, list) else []

    def get(self, strategy_id: str) -> dict | None:
        for row in self.list():
            if row.get("strategyId") == strategy_id:
                return row
        return None

    def save(self, strategy_id: str, state_dict: dict) -> dict:
        record = {
            "strategyId": strategy_id,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "state": copy.deepcopy(state_dict),
        }
        rows = [row for row in self.list() if row.get("strategyId") != strategy_id]
        rows.insert(0, record)
        write(self.filename, rows, data_dir=self.data_dir)
        return record

    def reset(self, strategy_id: str) -> None:
        rows = [row for row in self.list() if row.get("strategyId") != strategy_id]
        write(self.filename, rows, data_dir=self.data_dir)


class MongoRepos:
    def __init__(self, db):
        self.db = db
        self.strategies = MongoStrategyRepo(db)
        self.trades = MongoListRepo(db, "paper_trades")
        self.runs = MongoListRepo(db, "sar_runs")
        self.levels = MongoLevelRepo(db)
        self.eval_states = MongoEvaluationStateRepo(db)
        self.backtests = MongoListRepo(db, "backtest_runs")
        self.paper_workers = MongoListRepo(db, "paper_worker_runs")
        self.broker_orders = MongoListRepo(db, "broker_orders")
        self.live_approvals = MongoListRepo(db, "live_approvals")
        ensure_indexes(db)


class MongoListRepo:
    def __init__(self, db, collection_name: str):
        self.collection = db[collection_name]

    def list(self) -> list:
        rows = []
        for document in self.collection.find().sort("_position", 1):
            rows.append(_strip_internal(document))
        return rows

    def save_all(self, rows: list) -> None:
        self.collection.delete_many({})
        if not rows:
            return
        documents = []
        seen = set()
        for index, row in enumerate(rows):
            document = copy.deepcopy(row)
            raw_id = str(document.get("id") or f"row-{index}")
            mongo_id = raw_id if raw_id not in seen else f"{raw_id}::{index}"
            seen.add(mongo_id)
            document["_id"] = mongo_id
            document["_position"] = index
            documents.append(document)
        self.collection.insert_many(documents)


class MongoStrategyRepo:
    def __init__(self, db):
        self.collection = db["strategies"]
        self.versions = db["strategy_versions"]

    def list(self) -> list:
        rows = []
        for document in self.collection.find().sort("_savedAt", -1):
            rows.append(_strip_internal(document))
        return rows

    def get(self, strategy_id: str):
        document = self.collection.find_one({"_id": strategy_id})
        return _strip_internal(document) if document else None

    def save(self, strategy: dict, actor: str | None = None, summary_override: str | None = None) -> dict:
        if not isinstance(strategy, dict) or not strategy.get("id"):
            raise ValueError("Strategy id is required")
        stored = copy.deepcopy(strategy)
        previous_doc = self.collection.find_one({"_id": stored["id"]})
        previous = _strip_internal(previous_doc) if previous_doc else None
        summary = summary_override or diff_summary(previous, stored)
        saved_at = datetime.now(timezone.utc).isoformat()
        document = copy.deepcopy(stored)
        document["_id"] = stored["id"]
        document["_savedAt"] = saved_at
        self.collection.replace_one({"_id": stored["id"]}, document, upsert=True)
        version = {
            "_id": str(uuid4()),
            "versionId": None,
            "strategyId": stored["id"],
            "savedAt": saved_at,
            "actor": clean_actor(actor),
            "diffSummary": summary,
            "snapshot": copy.deepcopy(stored),
        }
        version["versionId"] = version["_id"]
        self.versions.insert_one(version)
        return stored

    def list_versions(self, strategy_id: str) -> list:
        cursor = self.versions.find({"strategyId": strategy_id}).sort("savedAt", -1)
        return [_strip_internal(row) for row in cursor]

    def restore(self, strategy_id: str, version_id: str, actor: str | None = None) -> dict:
        match = self.versions.find_one({"_id": version_id, "strategyId": strategy_id})
        if match is None or not isinstance(match.get("snapshot"), dict):
            raise KeyError(version_id)
        snapshot = copy.deepcopy(match["snapshot"])
        if snapshot.get("id") != strategy_id:
            raise KeyError(version_id)
        return self.save(snapshot, actor=actor, summary_override=f"restored from {version_id}")

    def delete(self, strategy_id: str) -> bool:
        res = self.collection.delete_one({"_id": strategy_id})
        return bool(res.deleted_count)


class MongoLevelRepo:
    def __init__(self, db):
        self.collection = db["level_sheets"]

    def list(self) -> list:
        return [_strip_internal(row) for row in self.collection.find()]

    def get(self, sheet_id: str):
        document = self.collection.find_one({"_id": sheet_id})
        return _strip_internal(document) if document else None

    def save(self, sheet: dict) -> dict:
        document = copy.deepcopy(sheet)
        document["_id"] = sheet["id"]
        self.collection.replace_one({"_id": sheet["id"]}, document, upsert=True)
        return sheet


class MongoEvaluationStateRepo:
    def __init__(self, db):
        self.collection = db["evaluation_states"]

    def list(self) -> list:
        return [_strip_internal(row) for row in self.collection.find()]

    def get(self, strategy_id: str) -> dict | None:
        document = self.collection.find_one({"_id": strategy_id})
        return _strip_internal(document) if document else None

    def save(self, strategy_id: str, state_dict: dict) -> dict:
        record = {
            "_id": strategy_id,
            "strategyId": strategy_id,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "state": copy.deepcopy(state_dict),
        }
        self.collection.replace_one({"_id": strategy_id}, record, upsert=True)
        return _strip_internal(record)

    def reset(self, strategy_id: str) -> None:
        self.collection.delete_one({"_id": strategy_id})


def index_specifications() -> list[dict]:
    return [
        {"collection": "strategies", "keys": [("id", 1)], "unique": True, "name": "strategy_id"},
        {"collection": "strategy_versions", "keys": [("strategyId", 1), ("savedAt", -1)], "unique": False, "name": "versions_by_strategy"},
        {"collection": "paper_trades", "keys": [("id", 1)], "unique": False, "name": "trade_id"},
        {"collection": "sar_runs", "keys": [("id", 1)], "unique": False, "name": "run_id"},
        {"collection": "sar_runs", "keys": [("strategyId", 1)], "unique": False, "name": "run_strategy"},
        {
            "collection": "level_sheets",
            "keys": [("symbol", 1), ("day", 1), ("provider", 1)],
            "unique": True,
            "name": "level_symbol_day_provider",
        },
        {
            "collection": "evaluation_states",
            "keys": [("strategyId", 1)],
            "unique": True,
            "name": "eval_state_strategy_id",
        },
        {"collection": "backtest_runs", "keys": [("id", 1)], "unique": False, "name": "backtest_run_id"},
        {"collection": "backtest_runs", "keys": [("strategyId", 1)], "unique": False, "name": "backtest_strategy_id"},
        {"collection": "paper_worker_runs", "keys": [("id", 1)], "unique": False, "name": "paper_worker_id"},
        {"collection": "paper_worker_runs", "keys": [("strategyId", 1)], "unique": False, "name": "paper_worker_strategy_id"},
        {"collection": "broker_orders", "keys": [("id", 1)], "unique": False, "name": "broker_order_id"},
        {"collection": "broker_orders", "keys": [("idempotencyKey", 1)], "unique": False, "name": "broker_order_idempotency"},
        {"collection": "live_approvals", "keys": [("strategyId", 1)], "unique": False, "name": "live_approval_strategy_id"},
    ]


def ensure_indexes(db) -> None:
    for spec in index_specifications():
        db[spec["collection"]].create_index(spec["keys"], unique=spec["unique"], name=spec["name"])


def mongo_client(uri: str):
    from pymongo import MongoClient
    return MongoClient(uri, serverSelectionTimeoutMS=3000)


def redact_mongo_uri(uri: str) -> str:
    if "@" not in uri:
        return uri
    head, tail = uri.split("@", 1)
    scheme = head.split("://", 1)[0] if "://" in head else "mongodb"
    return f"{scheme}://***@{tail}"


class RepoBundle:
    def __init__(
        self,
        strategies,
        trades,
        runs,
        levels,
        eval_states=None,
        backtests=None,
        paper_workers=None,
        broker_orders=None,
        live_approvals=None,
    ):
        self.strategies = strategies
        self.trades = trades
        self.runs = runs
        self.levels = levels
        self.eval_states = eval_states or JsonEvaluationStateRepo()
        self.backtests = backtests or JsonListRepo("backtest_runs.json")
        self.paper_workers = paper_workers or JsonListRepo("paper_worker_runs.json")
        self.broker_orders = broker_orders or JsonListRepo("broker_orders.json")
        self.live_approvals = live_approvals or JsonListRepo("live_approvals.json")


def build_repos(settings, data_dir=None) -> RepoBundle:
    backend = (settings.store_backend or "json").strip().lower()
    if backend == "json":
        return RepoBundle(
            JsonStrategyRepo(data_dir),
            JsonListRepo("paper_trades.json", data_dir),
            JsonListRepo("sar_runs.json", data_dir),
            JsonLevelRepo(data_dir),
            JsonEvaluationStateRepo(data_dir),
            JsonListRepo("backtest_runs.json", data_dir),
            JsonListRepo("paper_worker_runs.json", data_dir),
            JsonListRepo("broker_orders.json", data_dir),
            JsonListRepo("live_approvals.json", data_dir),
        )
    if backend == "mongo":
        try:
            client = mongo_client(settings.mongodb_uri)
            client.admin.command("ping")
        except Exception as exc:
            raise RuntimeError(
                f"MongoDB store is unavailable ({redact_mongo_uri(settings.mongodb_uri)}): {type(exc).__name__}"
            ) from exc
        bundle = MongoRepos(client[settings.mongodb_db])
        return RepoBundle(
            bundle.strategies,
            bundle.trades,
            bundle.runs,
            bundle.levels,
            bundle.eval_states,
            bundle.backtests,
            bundle.paper_workers,
            bundle.broker_orders,
            bundle.live_approvals,
        )
    raise RuntimeError("STORE_BACKEND must be json or mongo.")


def migration_timestamp(index_from_newest: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=index_from_newest)
    return moment.isoformat()
