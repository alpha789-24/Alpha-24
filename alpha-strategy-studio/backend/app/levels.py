"""Pluggable FC/RC level providers.

MANUAL stores operator-entered values. PLACEHOLDER is an explicitly dummy,
opt-in provider for wiring tests only. FORMULA is an extension slot and refuses
to run until the owner supplies the definitions; no market formula is guessed.
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Optional, Protocol

from .schema_v2 import LEVEL_NAMES

PROVIDERS = ("MANUAL", "PLACEHOLDER", "FORMULA")
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLACEHOLDER_WARNING = (
    "PLACEHOLDER levels are dummy values, not market data. "
    "They are for wiring tests only and must not be used for trading decisions."
)


class LevelError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class FormulaNotProvided(LevelError):
    def __init__(self):
        super().__init__("formula not provided")


class LevelQuote:
    def __init__(self, symbol: str, day: str, name: str, provider: str, value: Optional[float], warning: Optional[str]):
        self.symbol, self.day, self.name = symbol, day, name
        self.provider, self.value, self.warning = provider, value, warning

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "day": self.day, "name": self.name,
                "provider": self.provider, "value": self.value, "warning": self.warning}


def normalize_level_name(name: str) -> str:
    cleaned = str(name or "").strip().upper()
    if cleaned not in LEVEL_NAMES:
        raise LevelError(f"Unknown level '{name}'. Expected one of: {', '.join(LEVEL_NAMES)}.")
    return cleaned


def normalize_day(day: str) -> str:
    cleaned = str(day or "").strip()
    if not DAY_PATTERN.fullmatch(cleaned):
        raise LevelError("Day must be YYYY-MM-DD.")
    try:
        date.fromisoformat(cleaned)
    except ValueError as exc:
        raise LevelError("Day must be a real calendar date in YYYY-MM-DD format.") from exc
    return cleaned


def normalize_symbol(symbol: str) -> str:
    cleaned = str(symbol or "").strip()
    if not cleaned or len(cleaned) > 64:
        raise LevelError("Symbol is required and must be at most 64 characters.")
    return cleaned


def sheet_id(provider: str, symbol: str, day: str) -> str:
    return f"{provider}|{symbol}|{day}"


def clean_values(values: dict) -> dict:
    if not isinstance(values, dict):
        raise LevelError("Level values must be an object of level name to number.")
    cleaned = {}
    for key, raw in values.items():
        name = normalize_level_name(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise LevelError(f"{name} must be a finite number.")
        cleaned[name] = float(raw)
    return cleaned


class LevelProvider(Protocol):
    name: str
    def lookup(self, book: "LevelBook", symbol: str, day: str, name: str) -> LevelQuote: ...


class _StoredSheetProvider:
    name = ""
    warning: Optional[str] = None

    def lookup(self, book: "LevelBook", symbol: str, day: str, name: str) -> LevelQuote:
        sheet = book.repo.get(sheet_id(self.name, symbol, day))
        values = (sheet or {}).get("values") or {}
        if name not in values:
            raise LevelError(f"No {self.name.lower()} value for {symbol} on {day} level {name}.", status_code=404)
        return LevelQuote(symbol, day, name, self.name, float(values[name]), self.warning)


class ManualProvider(_StoredSheetProvider):
    name = "MANUAL"


class PlaceholderProvider(_StoredSheetProvider):
    name = "PLACEHOLDER"
    warning = PLACEHOLDER_WARNING

    @staticmethod
    def ensure_enabled():
        from .config import settings
        if not settings.levels_placeholder_enabled:
            raise LevelError("PLACEHOLDER is off. Set LEVELS_PLACEHOLDER_ENABLED=true before using it.")


class FormulaProvider:
    name = "FORMULA"

    def lookup(self, book: "LevelBook", symbol: str, day: str, name: str) -> LevelQuote:
        raise FormulaNotProvided()


class LevelBook:
    """Persistence-backed lookup with a replaceable provider registry."""
    def __init__(self, repo, providers: Optional[dict[str, LevelProvider]] = None):
        self.repo = repo
        self.providers = providers or {
            "MANUAL": ManualProvider(),
            "PLACEHOLDER": PlaceholderProvider(),
            "FORMULA": FormulaProvider(),
        }

    def save_sheet(self, provider: str, symbol: str, day: str, values: dict) -> dict:
        provider = str(provider or "").strip().upper()
        if provider not in ("MANUAL", "PLACEHOLDER"):
            if provider == "FORMULA":
                raise FormulaNotProvided()
            raise LevelError("provider must be MANUAL, PLACEHOLDER, or FORMULA.")
        if provider == "PLACEHOLDER":
            PlaceholderProvider.ensure_enabled()
        symbol, day = normalize_symbol(symbol), normalize_day(day)
        cleaned = clean_values(values)
        missing = [name for name in LEVEL_NAMES if name not in cleaned]
        if missing:
            raise LevelError("Enter every FC/RC level. Missing: " + ", ".join(missing))
        sheet = {"id": sheet_id(provider, symbol, day), "provider": provider,
                 "symbol": symbol, "day": day, "values": cleaned}
        self.repo.save(sheet)
        return sheet

    def get_sheet(self, provider: str, symbol: str, day: str) -> Optional[dict]:
        provider = str(provider or "").strip().upper()
        if provider not in PROVIDERS:
            raise LevelError("provider must be MANUAL, PLACEHOLDER, or FORMULA.")
        if provider == "FORMULA":
            raise FormulaNotProvided()
        if provider == "PLACEHOLDER":
            PlaceholderProvider.ensure_enabled()
        return self.repo.get(sheet_id(provider, normalize_symbol(symbol), normalize_day(day)))

    def lookup(self, provider: str, symbol: str, day: str, name: str) -> LevelQuote:
        provider = str(provider or "").strip().upper()
        if provider not in self.providers:
            raise LevelError("provider must be MANUAL, PLACEHOLDER, or FORMULA.")
        symbol, day, level_name = normalize_symbol(symbol), normalize_day(day), normalize_level_name(name)
        if provider == "PLACEHOLDER":
            PlaceholderProvider.ensure_enabled()
        return self.providers[provider].lookup(self, symbol, day, level_name)
