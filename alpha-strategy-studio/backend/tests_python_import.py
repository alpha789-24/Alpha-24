"""
tests_python_import.py
======================
Stand-alone test suite for Python Strategy Importer (Part A).
Runnable directly with `python tests_python_import.py`.
Supports both forward and reverse execution order to verify test isolation.
"""
from __future__ import annotations

import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from app.main import app
from app.python_analyzer import (
    analyse_source,
    decode_source,
    validate_source_bytes,
    mask_secrets,
    MAX_SOURCE_BYTES,
)

client = TestClient(app)

# ---------------------------------------------------------------------------
# Test runner and check counter
# ---------------------------------------------------------------------------

passed_checks = 0
failed_checks = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed_checks, failed_checks
    if not cond:
        failed_checks += 1
        raise AssertionError(f"FAIL: {label} ({detail})")
    passed_checks += 1
    print(f"ok {label}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_VALID_SIMPLE = """\
\"\"\"Simple EMA crossover strategy.\"\"\"
import datetime

quantity = 50
stop_loss = 100
target = 200

def on_bar(candles, context):
    ema_fast = candles['ema']
    ema_slow = candles['sma']
    if ema_fast > ema_slow:
        return 'BUY'
    if ema_fast < ema_slow:
        return 'SELL'
    return None
"""

FIXTURE_SYNTAX_ERROR = """\
def on_bar(candles
    return 'BUY'
"""

FIXTURE_FORBIDDEN_IMPORT_OS = """\
import os
import datetime

def on_bar(candles, context):
    os.system('echo hello')
    return 'BUY'
"""

FIXTURE_FORBIDDEN_IMPORT_REQUESTS = """\
import requests

def generate_signals(data):
    r = requests.get('http://example.com/prices')
    return r.json()
"""

FIXTURE_EVAL_USAGE = """\
def on_bar(candles, context):
    result = eval('1 + 1')
    return result
"""

FIXTURE_EXEC_USAGE = """\
def on_bar(candles, context):
    exec('x = 1')
    return 'BUY'
"""

FIXTURE_HIDDEN_IMPORT = """\
def on_bar(candles, context):
    mod = __import__('os')
    mod.system('whoami')
    return 'BUY'
"""

FAKE_API_KEY = 'FAKE_API_KEY_FOR_TEST_XYZ987ABC123LMNOP'
FIXTURE_SECRET_STRING = f"""\
import datetime

api_key = '{FAKE_API_KEY}'
quantity = 1

def on_bar(candles, context):
    if candles['rsi'] < 30:
        return 'BUY'
    return None
"""

FIXTURE_OVERSIZED = b'# valid python\n' + b'x = 1\n' * (200 * 1024 // 6 + 10)
FIXTURE_BINARY = bytes(range(128, 256)) * 20

FIXTURE_AMBIGUOUS = """\
import datetime

def on_bar(candles, context):
    for item in some_function():
        process(item)
    return 'BUY'
"""

FIXTURE_PARTIAL_CONVERT = """\
import math

quantity = 10
stop_loss = 50

def on_bar(candles, context):
    if candles['rsi'] < 30:
        return 'BUY'
    if unknown_indicator > some_threshold:
        return 'SELL'
    return None
"""

_MARKER_FILE = ROOT / '_PYTHON_IMPORT_EXECUTION_MARKER.txt'
FIXTURE_EXECUTION_PROBE = f"""\
import pathlib
pathlib.Path(r'{_MARKER_FILE}').write_text('EXECUTED_AT_MODULE_LEVEL')

def on_bar(candles, context):
    pathlib.Path(r'{_MARKER_FILE}').write_text('EXECUTED_IN_FUNCTION')
    return 'BUY'
"""


# ---------------------------------------------------------------------------
# Individual Test Cases
# ---------------------------------------------------------------------------

def test_valid_simple_strategy() -> None:
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_VALID_SIMPLE, 'filename': 'ema_cross.py'})
    d = r.json()
    check("python_import valid simple 200 ok", r.status_code == 200 and d.get('syntaxOk') is True)
    check("python_import valid simple is importable", d.get('importable') is True)
    check("python_import detected indicators", any(ind in d.get('indicators', []) for ind in ('EMA', 'SMA')))
    check("python_import detected on_bar entry point", any(ep['name'] == 'on_bar' for ep in d.get('entryPoints', [])))
    check("python_import rules converted", any(r2['status'] == 'converted' for r2 in d.get('rules', [])))
    check("python_import draftStrategy generated", d.get('draftStrategy') is not None)
    check("python_import draftStrategy marked PYTHON_IMPORT", d.get('draftStrategy', {}).get('meta', {}).get('sourceType') == 'PYTHON_IMPORT')


def test_syntax_error() -> None:
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_SYNTAX_ERROR, 'filename': 'bad.py'})
    d = r.json()
    check("python_import syntax error handled without 500", r.status_code == 200)
    check("python_import syntaxOk is False", d.get('syntaxOk') is False and d.get('importable') is False)
    check("python_import syntax error line reported", d.get('syntaxError', {}).get('line') is not None)


def test_forbidden_imports() -> None:
    r_os = client.post('/api/python-import/analyse', json={'source': FIXTURE_FORBIDDEN_IMPORT_OS, 'filename': 'os.py'})
    d_os = r_os.json()
    check("python_import forbidden os import blocked", d_os.get('importable') is False)
    check("python_import forbidden os construct reported", any('os' in dc.get('name', '') for dc in d_os.get('dangerousConstructs', [])))

    r_req = client.post('/api/python-import/analyse', json={'source': FIXTURE_FORBIDDEN_IMPORT_REQUESTS, 'filename': 'req.py'})
    d_req = r_req.json()
    check("python_import forbidden requests import blocked", d_req.get('importable') is False)


def test_eval_exec_constructs() -> None:
    r_ev = client.post('/api/python-import/analyse', json={'source': FIXTURE_EVAL_USAGE, 'filename': 'eval.py'})
    d_ev = r_ev.json()
    check("python_import eval blocked", d_ev.get('importable') is False and any(dc.get('name') == 'eval' for dc in d_ev.get('dangerousConstructs', [])))

    r_ex = client.post('/api/python-import/analyse', json={'source': FIXTURE_EXEC_USAGE, 'filename': 'exec.py'})
    d_ex = r_ex.json()
    check("python_import exec blocked", d_ex.get('importable') is False and any(dc.get('name') == 'exec' for dc in d_ex.get('dangerousConstructs', [])))


def test_hidden_import() -> None:
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_HIDDEN_IMPORT, 'filename': 'hidden.py'})
    d = r.json()
    check("python_import hidden __import__ blocked", d.get('importable') is False and any('__import__' in str(dc) for dc in d.get('dangerousConstructs', [])))


def test_secret_string_masked() -> None:
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_SECRET_STRING, 'filename': 'secret.py'})
    raw = r.text
    d = r.json()
    check("python_import fake api key not in raw response", FAKE_API_KEY not in raw)
    check("python_import fake api key masked in maskedSource", FAKE_API_KEY not in d.get('maskedSource', ''))
    check("python_import secrets warning reported", len(d.get('secretsFound', [])) >= 1)


def test_oversized_file() -> None:
    large = '# padding\n' * (200 * 1024 // 10 + 50)
    r = client.post('/api/python-import/analyse', json={'source': large, 'filename': 'big.py'})
    check("python_import oversized source rejected with 400", r.status_code == 400 and 'large' in r.json().get('detail', '').lower())


def test_ambiguous_logic() -> None:
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_AMBIGUOUS, 'filename': 'ambig.py'})
    d = r.json()
    check("python_import ambiguous loop flagged", any(ac.get('type') == 'loop_over_call' for ac in d.get('ambiguousConstructs', [])))


def test_partial_conversion() -> None:
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_PARTIAL_CONVERT, 'filename': 'partial.py'})
    d = r.json()
    statuses = {rule.get('status') for rule in d.get('rules', [])}
    check("python_import partial conversion reported", 'converted' in statuses and ('partially_converted' in statuses or 'could_not_be_represented' in statuses))


def test_file_upload_suite() -> None:
    r_val = client.post('/api/python-import/upload', files={'file': ('strategy.py', FIXTURE_VALID_SIMPLE.encode('utf-8'), 'text/x-python')})
    check("python_import upload valid .py 200", r_val.status_code == 200 and r_val.json().get('syntaxOk') is True)

    r_bin = client.post('/api/python-import/upload', files={'file': ('strategy.py', FIXTURE_BINARY, 'application/octet-stream')})
    check("python_import upload binary rejected 400", r_bin.status_code == 400 and 'binary' in r_bin.json().get('detail', '').lower())

    r_txt = client.post('/api/python-import/upload', files={'file': ('strategy.txt', b'x = 1', 'text/plain')})
    check("python_import upload non-.py rejected 400", r_txt.status_code == 400)

    r_big = client.post('/api/python-import/upload', files={'file': ('big.py', FIXTURE_OVERSIZED, 'text/x-python')})
    check("python_import upload oversized rejected 400", r_big.status_code == 400)

    r_null = client.post('/api/python-import/upload', files={'file': ('null.py', b'x = 1\x00evil', 'text/x-python')})
    check("python_import upload null bytes rejected 400", r_null.status_code == 400 and 'null' in r_null.json().get('detail', '').lower())


def test_code_never_executed_proof() -> None:
    if _MARKER_FILE.exists():
        _MARKER_FILE.unlink()
    r_text = client.post('/api/python-import/analyse', json={'source': FIXTURE_EXECUTION_PROBE, 'filename': 'probe.py'})
    check("python_import probe text analysed 200", r_text.status_code == 200)
    check("python_import marker file not created via text", not _MARKER_FILE.exists())

    r_up = client.post('/api/python-import/upload', files={'file': ('probe.py', FIXTURE_EXECUTION_PROBE.encode('utf-8'), 'text/x-python')})
    check("python_import probe upload analysed 200", r_up.status_code == 200)
    check("python_import marker file not created via upload", not _MARKER_FILE.exists())


def test_persistence_in_my_strategies() -> None:
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_VALID_SIMPLE, 'filename': 'ema_cross.py'})
    draft = r.json().get('draftStrategy')
    check("python_import draft ready for persistence", draft is not None)
    save_r = client.post('/api/strategies', json={'strategy': draft})
    check("python_import save to My Strategies 200", save_r.status_code == 200)
    saved = save_r.json()
    check("python_import persisted sourceType is PYTHON_IMPORT", saved.get('meta', {}).get('sourceType') == 'PYTHON_IMPORT')
    check("python_import persisted validationStatus is NEEDS_REVIEW", saved.get('meta', {}).get('validationStatus') == 'NEEDS_REVIEW')
    check("python_import persisted contentHash present", bool(saved.get('meta', {}).get('contentHash')))
    check("python_import persisted maskedSource present", bool(saved.get('meta', {}).get('maskedSource')))
    # clean up
    client.delete(f"/api/strategies/{draft['id']}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

ALL_TESTS = [
    test_valid_simple_strategy,
    test_syntax_error,
    test_forbidden_imports,
    test_eval_exec_constructs,
    test_hidden_import,
    test_secret_string_masked,
    test_oversized_file,
    test_ambiguous_logic,
    test_partial_conversion,
    test_file_upload_suite,
    test_code_never_executed_proof,
    test_persistence_in_my_strategies,
]


def run_all(reverse: bool = False) -> None:
    tests = list(reversed(ALL_TESTS)) if reverse else list(ALL_TESTS)
    direction = "REVERSE" if reverse else "FORWARD"
    print(f"\n--- Running Python Importer Test Suite ({direction} order) ---")
    for t in tests:
        t()


if __name__ == "__main__":
    passed_checks = 0
    failed_checks = 0
    run_all(reverse=False)
    fwd_count = passed_checks
    print(f"Forward order complete: {fwd_count} checks passed, {failed_checks} failed.")

    passed_checks = 0
    failed_checks = 0
    run_all(reverse=True)
    rev_count = passed_checks
    print(f"Reverse order complete: {rev_count} checks passed, {failed_checks} failed.")

    print(f"\nALL PYTHON IMPORTER TESTS PASSED ({fwd_count} checks in forward, {rev_count} in reverse)")
