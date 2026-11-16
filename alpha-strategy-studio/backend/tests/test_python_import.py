"""
test_python_import.py
=====================
Backend tests for the Python Strategy Importer (Part A).

SECURITY INVARIANTS TESTED
---------------------------
1. Uploaded code is NEVER executed (payload that would create a marker file
   must have no effect).
2. No secret-looking string appears unmasked in returned or stored data.
3. Limits: .py only, UTF-8 text, max 200 KB, reject binary or null bytes.
4. AST-only static inspection: syntax validity, detected entry points,
   inputs/outputs, imports/dependencies, indicators, dangerous constructs,
   ambiguous logic, rule mapping.
"""
import os
import sys
import pathlib
import pytest

# Ensure backend root is on sys.path
_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

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

_MARKER_FILE = pathlib.Path(__file__).parent / '_PYTHON_IMPORT_EXECUTION_MARKER.txt'
FIXTURE_EXECUTION_PROBE = f"""\
import pathlib
pathlib.Path(r'{_MARKER_FILE}').write_text('EXECUTED_AT_MODULE_LEVEL')

def on_bar(candles, context):
    pathlib.Path(r'{_MARKER_FILE}').write_text('EXECUTED_IN_FUNCTION')
    return 'BUY'
"""


# ---------------------------------------------------------------------------
# Test Functions
# ---------------------------------------------------------------------------

def test_valid_simple_strategy():
    """Valid strategy with EMA/SMA comparison returns importable=True and draft."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_VALID_SIMPLE, 'filename': 'ema_cross.py'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['ok'] is True
    assert d['syntaxOk'] is True
    assert d['importable'] is True
    assert 'EMA' in d['indicators'] or 'SMA' in d['indicators']
    assert any(ep['name'] == 'on_bar' for ep in d['entryPoints'])
    assert any(r2['status'] == 'converted' for r2 in d['rules'])
    assert d['draftStrategy'] is not None
    assert d['draftStrategy']['meta']['sourceType'] == 'PYTHON_IMPORT'
    assert d['draftStrategy']['meta']['validationStatus'] == 'NEEDS_REVIEW'


def test_syntax_error():
    """Syntax error returns syntaxOk=False, importable=False with line/column."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_SYNTAX_ERROR, 'filename': 'syntax_bad.py'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['ok'] is False
    assert d['syntaxOk'] is False
    assert d['importable'] is False
    assert d['syntaxError']['line'] is not None
    assert d['syntaxError']['column'] is not None
    assert d['draftStrategy'] is None


def test_forbidden_import_os():
    """Import of 'os' marks strategy as not importable with line number."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_FORBIDDEN_IMPORT_OS, 'filename': 'os_bad.py'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['syntaxOk'] is True
    assert d['importable'] is False
    assert any(dc['type'] == 'dangerous_import' and 'os' in dc['name'] for dc in d['dangerousConstructs'])


def test_forbidden_import_requests():
    """Import of 'requests' marks strategy as not importable."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_FORBIDDEN_IMPORT_REQUESTS, 'filename': 'req_bad.py'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['importable'] is False
    assert any('requests' in dc.get('name', '') for dc in d['dangerousConstructs'])


def test_eval_usage():
    """Use of eval() marks strategy as not importable."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_EVAL_USAGE, 'filename': 'eval_bad.py'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['importable'] is False
    assert any(dc['name'] == 'eval' for dc in d['dangerousConstructs'])


def test_exec_usage():
    """Use of exec() marks strategy as not importable."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_EXEC_USAGE, 'filename': 'exec_bad.py'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['importable'] is False
    assert any(dc['name'] == 'exec' for dc in d['dangerousConstructs'])


def test_hidden_import():
    """Hidden __import__() call is detected and blocks import."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_HIDDEN_IMPORT, 'filename': 'hidden_bad.py'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['importable'] is False
    names = {dc.get('name') for dc in d['dangerousConstructs']}
    assert '__import__' in names or any('__import__' in str(dc) for dc in d['dangerousConstructs'])


def test_secret_string_masked():
    """
    SECURITY: A fake API key in the source must NOT appear unmasked
    in any part of the response or draftStrategy.
    """
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_SECRET_STRING, 'filename': 'secret.py'})
    assert r.status_code == 200, r.text
    raw_body = r.text
    assert FAKE_API_KEY not in raw_body, f'Fake API key {FAKE_API_KEY} appeared unmasked in response'
    d = r.json()
    assert len(d['secretsFound']) >= 1
    assert FAKE_API_KEY not in d['maskedSource']


def test_oversized_file_text():
    """Oversized source (> 200 KB) is rejected with HTTP 400."""
    large_source = '# padding\n' * (200 * 1024 // 10 + 50)
    r = client.post('/api/python-import/analyse', json={'source': large_source, 'filename': 'big.py'})
    assert r.status_code == 400
    assert 'large' in r.json()['detail'].lower() or '200 kb' in r.json()['detail'].lower()


def test_ambiguous_logic():
    """Ambiguous logic (e.g. for loop over function call) is flagged."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_AMBIGUOUS, 'filename': 'ambig.py'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['syntaxOk'] is True
    assert any(ac['type'] == 'loop_over_call' for ac in d['ambiguousConstructs'])


def test_partial_conversion():
    """Strategy with one understood rule and one unknown rule reports partial status."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_PARTIAL_CONVERT, 'filename': 'partial.py'})
    assert r.status_code == 200, r.text
    d = r.json()
    statuses = {rule['status'] for rule in d['rules']}
    assert 'converted' in statuses
    assert 'partially_converted' in statuses or 'could_not_be_represented' in statuses


def test_upload_valid_file():
    """Valid .py file upload via multipart/form-data succeeds."""
    r = client.post('/api/python-import/upload', files={'file': ('strategy.py', FIXTURE_VALID_SIMPLE.encode('utf-8'), 'text/x-python')})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['syntaxOk'] is True
    assert d['importable'] is True


def test_upload_binary_file_rejected():
    """Binary file upload is rejected with HTTP 400."""
    r = client.post('/api/python-import/upload', files={'file': ('strategy.py', FIXTURE_BINARY, 'application/octet-stream')})
    assert r.status_code == 400
    assert 'binary' in r.json()['detail'].lower()


def test_upload_non_py_rejected():
    """File without .py extension is rejected with HTTP 400."""
    r = client.post('/api/python-import/upload', files={'file': ('strategy.txt', b'x = 1', 'text/plain')})
    assert r.status_code == 400
    assert '.py' in r.json()['detail'].lower()


def test_upload_oversized_rejected():
    """Oversized file upload is rejected with HTTP 400."""
    r = client.post('/api/python-import/upload', files={'file': ('big.py', FIXTURE_OVERSIZED, 'text/x-python')})
    assert r.status_code == 400
    assert 'large' in r.json()['detail'].lower() or '200 kb' in r.json()['detail'].lower()


def test_upload_null_bytes_rejected():
    """File with null bytes is rejected with HTTP 400."""
    r = client.post('/api/python-import/upload', files={'file': ('null.py', b'x = 1\x00evil', 'text/x-python')})
    assert r.status_code == 400
    assert 'null' in r.json()['detail'].lower()


def test_uploaded_code_never_executed():
    """
    CRITICAL SECURITY INVARIANT:
    Code submitted is parsed with ast.parse ONLY.
    A probe file that would be written on execution must NOT be created.
    """
    if _MARKER_FILE.exists():
        _MARKER_FILE.unlink()
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_EXECUTION_PROBE, 'filename': 'probe.py'})
    assert r.status_code == 200
    assert not _MARKER_FILE.exists(), "SECURITY VIOLATION: Marker file was created — code was executed!"


def test_upload_code_never_executed():
    """Same security invariant verified via file upload endpoint."""
    if _MARKER_FILE.exists():
        _MARKER_FILE.unlink()
    r = client.post('/api/python-import/upload', files={'file': ('probe.py', FIXTURE_EXECUTION_PROBE.encode('utf-8'), 'text/x-python')})
    assert r.status_code == 200
    assert not _MARKER_FILE.exists(), "SECURITY VIOLATION: Uploaded code was executed!"


def test_save_imported_draft_to_my_strategies():
    """The draft strategy produced by analysis can be saved to My Strategies."""
    r = client.post('/api/python-import/analyse', json={'source': FIXTURE_VALID_SIMPLE, 'filename': 'ema_cross.py'})
    assert r.status_code == 200
    draft = r.json()['draftStrategy']
    assert draft is not None
    assert draft['meta']['sourceType'] == 'PYTHON_IMPORT'

    # Save to My Strategies via existing endpoint
    save_resp = client.post('/api/strategies', json={'strategy': draft})
    assert save_resp.status_code == 200, save_resp.text
    saved = save_resp.json()
    assert saved['meta']['sourceType'] == 'PYTHON_IMPORT'
    assert saved['meta']['validationStatus'] == 'NEEDS_REVIEW'
    assert saved['meta']['contentHash'] is not None
    assert saved['meta']['maskedSource'] is not None

    # Clean up
    client.delete(f"/api/strategies/{draft['id']}")
