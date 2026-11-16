"""
test_iifl_status.py
===================
Backend tests for GET /api/iifl/status.

Requirements:
  1. Response contains the four required boolean fields.
  2. Response NEVER contains any secret-looking value
     (fake key, secret or token strings are absent from the JSON).
  3. All returned values are JSON-safe primitives (bool, str, None) —
     never raw credential material.
"""
import sys
import pathlib
import pytest

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi.testclient import TestClient
from app.main import app
import app.main as main_mod

client = TestClient(app)

FAKE_APP_KEY = "FAKE_TEST_APP_KEY_DO_NOT_USE"
FAKE_APP_SECRET = "FAKE_TEST_APP_SECRET_DO_NOT_USE"
FAKE_SESSION_TOKEN = "FAKE_SESSION_TOKEN_DO_NOT_USE_XYZ987"


def _patch_settings(monkeypatch, *, app_key="", app_secret=""):
    monkeypatch.setattr(main_mod.settings, "app_key", app_key, raising=False)
    monkeypatch.setattr(main_mod.settings, "app_secret", app_secret, raising=False)


def _patch_state(monkeypatch, *, token=None, expired=False, profile_verified=False):
    monkeypatch.setattr(app.state, "iifl_token", token, raising=False)
    monkeypatch.setattr(app.state, "iifl_session_expired", expired, raising=False)
    monkeypatch.setattr(app.state, "iifl_profile_verified", profile_verified, raising=False)


def test_status_returns_four_booleans_when_nothing_configured(monkeypatch):
    """All four status booleans present and False when nothing is set."""
    _patch_settings(monkeypatch)
    _patch_state(monkeypatch)

    r = client.get("/api/iifl/status")
    assert r.status_code == 200
    data = r.json()

    for field in ("credentialsConfigured", "authenticationCompleted",
                  "profileVerified", "sessionActive", "sessionExpired"):
        assert field in data, f"Missing field: {field}"
        assert isinstance(data[field], bool), f"{field} must be bool, got {type(data[field])}"

    assert data["credentialsConfigured"] is False
    assert data["authenticationCompleted"] is False
    assert data["profileVerified"] is False
    assert data["sessionActive"] is False
    assert data["sessionExpired"] is False


def test_status_credentials_configured_true(monkeypatch):
    """credentialsConfigured is True when both app_key and app_secret are set."""
    _patch_settings(monkeypatch, app_key="some_key", app_secret="some_secret")
    _patch_state(monkeypatch)

    r = client.get("/api/iifl/status")
    assert r.status_code == 200
    assert r.json()["credentialsConfigured"] is True


def test_status_session_active_true(monkeypatch):
    """sessionActive and authenticationCompleted True when token present and not expired."""
    _patch_settings(monkeypatch, app_key="k", app_secret="s")
    _patch_state(monkeypatch, token="live_token_value", expired=False)

    r = client.get("/api/iifl/status")
    assert r.status_code == 200
    data = r.json()
    assert data["sessionActive"] is True
    assert data["authenticationCompleted"] is True
    assert data["sessionExpired"] is False


def test_status_expired_session(monkeypatch):
    """sessionExpired True -> sessionActive and authenticationCompleted both False."""
    _patch_settings(monkeypatch, app_key="k", app_secret="s")
    _patch_state(monkeypatch, token=None, expired=True)

    r = client.get("/api/iifl/status")
    assert r.status_code == 200
    data = r.json()
    assert data["sessionExpired"] is True
    assert data["sessionActive"] is False
    assert data["authenticationCompleted"] is False


def test_status_profile_verified(monkeypatch):
    """profileVerified True when profile was fetched and session is live."""
    _patch_settings(monkeypatch, app_key="k", app_secret="s")
    _patch_state(monkeypatch, token="live_token", expired=False, profile_verified=True)

    r = client.get("/api/iifl/status")
    assert r.status_code == 200
    assert r.json()["profileVerified"] is True


def test_status_no_secrets_in_response(monkeypatch):
    """
    SECURITY: The response body must NEVER contain the actual app key,
    app secret or session token — even as a substring of a string value.
    """
    _patch_settings(monkeypatch, app_key=FAKE_APP_KEY, app_secret=FAKE_APP_SECRET)
    _patch_state(monkeypatch, token=FAKE_SESSION_TOKEN, expired=False)

    r = client.get("/api/iifl/status")
    assert r.status_code == 200
    raw_body = r.text

    assert FAKE_APP_KEY not in raw_body, "App key must NOT appear in status response"
    assert FAKE_APP_SECRET not in raw_body, "App secret must NOT appear in status response"
    assert FAKE_SESSION_TOKEN not in raw_body, "Session token must NOT appear in status response"


def test_status_legacy_keys_present(monkeypatch):
    """Legacy 'configured' and 'sessionPresent' keys still present for backward compat."""
    _patch_settings(monkeypatch)
    _patch_state(monkeypatch)

    r = client.get("/api/iifl/status")
    assert r.status_code == 200
    data = r.json()
    assert "configured" in data
    assert "sessionPresent" in data
    assert isinstance(data["configured"], bool)
    assert isinstance(data["sessionPresent"], bool)
