"""
python_analyzer.py
==================
Static-only analysis and rule mapping of user-supplied Python trading strategies.

SECURITY CONTRACT
-----------------
* The uploaded source is NEVER executed, imported, eval'd, compile()-and-run,
  or passed to subprocess in any form.
* Only ast.parse() and read-only AST visitor traversal are used.
* Secrets found in string literals are masked before anything is returned or stored.
"""
from __future__ import annotations

import ast
import hashlib
import re
import uuid
from typing import Any, Optional

from .schema_v2 import new_rule_builder, _leaf, _target

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_SOURCE_BYTES: int = 200 * 1024  # 200 KB

ALLOWED_MODULES: set[str] = {
    'math', 'datetime', 'statistics', 'decimal', 'dataclasses',
    'typing', 'collections', 'functools', 'itertools', 'operator',
    'enum', 'abc', 'copy', 'numbers', 'fractions', 'cmath',
    'string', 're', 'json', 'io', '__future__', 'types',
}

# Supported libraries that cannot be statically resolved into visual builder rules
UNSUPPORTED_MODULES: set[str] = {
    'numpy', 'pandas', 'scipy', 'sklearn', 'ta', 'talib', 'finta',
    'vectorbt', 'backtrader', 'zipline', 'pyalgotrade', 'bt',
    'matplotlib', 'seaborn', 'plotly',
}

DANGEROUS_MODULES: set[str] = {
    'os', 'sys', 'subprocess', 'socket', 'requests', 'urllib',
    'ctypes', 'pickle', 'http', 'ftplib', 'smtplib', 'asyncio',
    'threading', 'multiprocessing', 'importlib', 'shelve',
    'shutil', 'glob', 'tempfile', 'pty', 'signal', 'resource',
    'mmap', 'grp', 'pwd', 'xmlrpc', 'grpc', 'aiohttp', 'httpx',
    'paramiko', 'fabric',
    # Broker / trading libraries
    'iifl', 'kite', 'zerodha', 'upstox', 'angel', 'fyers', 'nse',
    'breeze', 'smartapi', 'alice', 'motilal', 'edelweiss',
}

DANGEROUS_BUILTINS: set[str] = {
    'exec', 'eval', 'compile', '__import__', 'open', 'input',
    'breakpoint', 'memoryview',
}

KNOWN_ENTRY_NAMES: set[str] = {
    'on_bar', 'on_tick', 'on_candle', 'generate_signals',
    'run', 'execute', 'next', 'handle_data', 'strategy',
    'on_data', 'calculate', 'compute', 'process', 'on_start',
    'initialize', 'on_finish', 'on_new_bar',
}

# The 10 indicators specified in the requirements
KNOWN_INDICATORS: dict[str, list[str]] = {
    'SMA':        ['sma', 'simple_moving_average', 'moving_average', 'ma'],
    'EMA':        ['ema', 'exponential_moving_average'],
    'RSI':        ['rsi', 'relative_strength_index'],
    'SUPERTREND': ['supertrend', 'super_trend'],
    'VWAP':       ['vwap', 'volume_weighted'],
    'MACD':       ['macd', 'macd_line', 'signal_line'],
    'ATR':        ['atr', 'average_true_range'],
    'BOLLINGER':  ['bbands', 'bollinger', 'upper_band', 'lower_band', 'boll', 'bb_'],
    'STOCHASTIC': ['stoch', 'stochastic', 'stoch_k', 'stoch_d'],
    'ADX':        ['adx', 'average_directional'],
}

_MASK = '***MASKED***'
_SECRET_VAR_KWS = (
    'key', 'secret', 'token', 'password', 'passwd', 'auth',
    'api', 'appkey', 'appsecret', 'authcode', 'credential',
)

_SECRET_REGEX = re.compile(
    r'(?i)(?:api[_\-]?key|app[_\-]?key|secret|token|password|auth[_\-]?code|credential)'
    r'[^=\n]{0,10}=\s*["\']([A-Za-z0-9+/=_\-]{16,})["\']'
)
_LONG_OPAQUE = re.compile(r'["\']([A-Za-z0-9+/=_\-]{24,})["\']')


# ---------------------------------------------------------------------------
# Source validation (NEVER executes content)
# ---------------------------------------------------------------------------

def validate_source_bytes(raw: bytes) -> tuple[bool, str]:
    """Return (ok: bool, error: str). Never executes or imports content."""
    if len(raw) > MAX_SOURCE_BYTES:
        return False, (
            f'File too large: {len(raw):,} bytes '
            f'(maximum allowed is {MAX_SOURCE_BYTES:,} bytes = 200 KB)'
        )
    if b'\x00' in raw:
        return False, 'File contains null bytes; must be plain UTF-8 Python source'
    sample = raw[:4096]
    if sample:
        non_text = sum(1 for b in sample if (b < 9) or (13 < b < 32) or (b > 126))
        if non_text / len(sample) > 0.30:
            return False, 'File appears to be binary, not UTF-8 Python source'
    return True, ''


def decode_source(raw: bytes) -> tuple[Optional[str], str]:
    """Return (source: str|None, error: str)."""
    try:
        return raw.decode('utf-8'), ''
    except UnicodeDecodeError as e:
        return None, f'File is not valid UTF-8: {e}'


# ---------------------------------------------------------------------------
# Secret detection and masking
# ---------------------------------------------------------------------------

def _find_secrets_ast(tree: ast.AST) -> list[dict[str, Any]]:
    """
    Walk AST for secret-looking strings.
    Returns list of {line, reason}. NEVER includes actual secret values.
    """
    secrets: list[dict[str, Any]] = []
    seen: set[int] = set()

    for node in ast.walk(tree):
        # Assignment to suspicious variable name
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name_low = target.id.lower()
                    if any(kw in name_low for kw in _SECRET_VAR_KWS):
                        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                            ln = node.lineno
                            if ln not in seen:
                                seen.add(ln)
                                secrets.append({
                                    'line': ln,
                                    'reason': f'Variable name "{target.id}" suggests a credential',
                                })

        # Long opaque string constant (>= 24 alphanumeric chars)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            ln = getattr(node, 'lineno', 0)
            if (len(s) >= 24
                    and re.match(r'^[A-Za-z0-9+/=_\-]{24,}$', s)
                    and ln not in seen):
                seen.add(ln)
                secrets.append({
                    'line': ln,
                    'reason': 'Long opaque string literal looks like an API key or token',
                })

    return secrets


def mask_secrets(source: str) -> tuple[str, list[dict[str, Any]]]:
    """
    Return (masked_source, secrets_meta).
    masked_source has suspicious values replaced with ***MASKED***.
    secrets_meta contains {line, reason} only — never actual values.
    """
    masked = _SECRET_REGEX.sub(
        lambda m: m.group(0).replace(m.group(1), _MASK), source
    )
    masked = _LONG_OPAQUE.sub(
        lambda m: (m.group(0).replace(m.group(1), _MASK)
                   if len(m.group(1)) >= 24 and any(c.isdigit() for c in m.group(1)) else m.group(0)),
        masked,
    )
    try:
        tree = ast.parse(source, mode='exec')
        meta = _find_secrets_ast(tree)
    except SyntaxError:
        meta = []
    return masked, meta


# ---------------------------------------------------------------------------
# AST analysis visitors (read-only — no execution)
# ---------------------------------------------------------------------------

class _AnalysisVisitor(ast.NodeVisitor):
    """Collects imports, dangerous constructs, entry points, indicators."""

    def __init__(self):
        self.imports: list[dict[str, Any]] = []
        self.dangerous_constructs: list[dict[str, Any]] = []
        self.ambiguous_constructs: list[dict[str, Any]] = []
        self.entry_points: list[dict[str, Any]] = []
        self.indicators_used: set[str] = set()

    # -- Imports -------------------------------------------------------

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self._handle_import(alias.name, alias.asname or alias.name, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ''
        for alias in node.names:
            local = alias.asname or alias.name
            self._handle_import(module, local, node.lineno, from_import=True)
        self.generic_visit(node)

    def _handle_import(self, module: str, local: str, lineno: int, from_import: bool = False):
        top = module.split('.')[0] if module else ''
        if top in DANGEROUS_MODULES:
            status = 'dangerous'
            self.dangerous_constructs.append({
                'type': 'dangerous_import',
                'name': module,
                'line': lineno,
                'detail': f"Import of '{module}' is not allowed (security / OS / network access).",
            })
        elif top in UNSUPPORTED_MODULES:
            status = 'unsupported'
        elif top in ALLOWED_MODULES or not top:
            status = 'allowed'
        else:
            status = 'unknown'
        self.imports.append({'module': module, 'alias': local, 'status': status, 'line': lineno})

    # -- Dangerous builtins, broker calls and patterns -----------------

    def visit_Call(self, node: ast.Call):
        name = self._call_name(node)
        name_lower = name.lower()

        # Dangerous built-ins
        if name in DANGEROUS_BUILTINS:
            self.dangerous_constructs.append({
                'type': 'dangerous_builtin',
                'name': name,
                'line': node.lineno,
                'detail': f"Use of '{name}()' is not allowed.",
            })

        # Broker / order / network calls
        broker_order_patterns = (
            'place_order', 'send_order', 'buy_order', 'sell_order',
            'create_order', 'modify_order', 'cancel_order',
            'order_place', 'order_cancel', 'trade_book', 'order_book',
        )
        if any(pat in name_lower for pat in broker_order_patterns):
            self.dangerous_constructs.append({
                'type': 'broker_order_call',
                'name': name,
                'line': node.lineno,
                'detail': f"Direct broker order call '{name}()' is not allowed.",
            })

        network_patterns = ('connect', 'urlopen', 'request', 'post', 'get_response')
        if any(pat == name_lower or pat in name_lower.split('.') for pat in network_patterns):
            if any(mod in name_lower for mod in ('socket', 'http', 'urllib', 'requests', 'aiohttp', 'client')):
                self.dangerous_constructs.append({
                    'type': 'network_call',
                    'name': name,
                    'line': node.lineno,
                    'detail': f"Network communication '{name}()' is not allowed.",
                })

        # Ambiguous dynamic attributes
        if name in ('getattr', 'setattr', 'delattr'):
            self.ambiguous_constructs.append({
                'type': 'dynamic_attribute',
                'name': name,
                'line': node.lineno,
                'detail': f"Dynamic attribute access '{name}()' cannot be statically resolved.",
            })

        # Ambiguous randomness
        if 'random' in name_lower:
            self.ambiguous_constructs.append({
                'type': 'randomness',
                'name': name,
                'line': node.lineno,
                'detail': f"Random function '{name}()' produces non-deterministic logic.",
            })

        self._check_indicator_name(name, node.lineno)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr in ('__import__', '__reduce__', '__reduce_ex__', '__code__', '__builtins__', '__subclasses__'):
            self.dangerous_constructs.append({
                'type': 'dangerous_dunder',
                'name': node.attr,
                'line': node.lineno,
                'detail': f"Access to '{node.attr}' is not allowed.",
            })
        self._check_indicator_name(node.attr, node.lineno)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript):
        # Look for candles['rsi'] or data['close']
        key_str = self._unparse(node.slice)
        self._check_indicator_name(key_str, node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        self._check_indicator_name(node.id, node.lineno)
        self.generic_visit(node)

    # -- Entry points --------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if node.name in KNOWN_ENTRY_NAMES:
            # Extract inputs (args + subscript keys used)
            args = [a.arg for a in node.args.args]
            used_keys = [
                self._unparse(n.slice) for n in ast.walk(node)
                if isinstance(n, ast.Subscript)
            ]
            # Extract return values (outputs)
            returns = [
                self._unparse(r.value) for r in ast.walk(node)
                if isinstance(r, ast.Return) and r.value is not None
            ]
            self.entry_points.append({
                'type': 'function',
                'name': node.name,
                'line': node.lineno,
                'args': args,
                'inputs': sorted(set(args + used_keys[:6])),
                'outputs': sorted(set(returns[:6])),
                'docstring': ast.get_docstring(node),
            })
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef):
        matched = [
            n.name for n in ast.walk(node)
            if isinstance(n, ast.FunctionDef) and n.name in KNOWN_ENTRY_NAMES
        ]
        if matched:
            self.entry_points.append({
                'type': 'class',
                'name': node.name,
                'line': node.lineno,
                'entry_methods': matched,
                'inputs': ['self', 'candles'],
                'outputs': ['signals'],
                'docstring': ast.get_docstring(node),
            })
        self.generic_visit(node)

    # -- Ambiguous constructs ------------------------------------------

    def visit_For(self, node: ast.For):
        if isinstance(node.iter, ast.Call):
            self.ambiguous_constructs.append({
                'type': 'loop_over_call',
                'line': node.lineno,
                'detail': 'For-loop over a function-call result — data shape cannot be determined statically.',
            })
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global):
        self.ambiguous_constructs.append({
            'type': 'global_state',
            'line': node.lineno,
            'detail': f"'global' introduces mutable shared state: {', '.join(node.names)}",
        })
        self.generic_visit(node)

    # -- Helpers -------------------------------------------------------

    def _call_name(self, node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            left = self._call_name(node.func.value) if isinstance(node.func.value, ast.Call) else (
                node.func.value.id if isinstance(node.func.value, ast.Name) else ''
            )
            return f"{left}.{node.func.attr}" if left else node.func.attr
        return ''

    def _check_indicator_name(self, name: str, lineno: int):
        nl = name.lower()
        for indicator, synonyms in KNOWN_INDICATORS.items():
            if any(syn in nl for syn in synonyms):
                self.indicators_used.add(indicator)
                break

    def _unparse(self, node: Optional[ast.AST]) -> str:
        if node is None:
            return ''
        try:
            return ast.unparse(node).strip()
        except Exception:
            return '?'


# ---------------------------------------------------------------------------
# Rule pattern detector & mapper
# ---------------------------------------------------------------------------

class _RuleDetector(ast.NodeVisitor):
    """Detects and maps simple condition patterns to Rule Builder leaves."""

    def __init__(self):
        self.rules: list[dict[str, Any]] = []
        self.param_assignments: dict[str, Any] = {}

    def visit_Assign(self, node: ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            var = node.targets[0].id.lower()
            if isinstance(node.value, ast.Constant):
                self.param_assignments[var] = node.value.value
        self.generic_visit(node)

    def visit_If(self, node: ast.If):
        rule = self._try_map(node.test, node.lineno)
        if rule:
            self.rules.append(rule)
        self.generic_visit(node)

    def _try_map(self, test: ast.AST, lineno: int) -> dict[str, Any]:
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
            return self._map_compare(test, lineno)
        if isinstance(test, ast.BoolOp):
            return self._map_boolop(test, lineno)
        return {
            'line': lineno,
            'status': 'could_not_be_represented',
            'ruleType': 'unknown',
            'reason': 'Condition pattern is not a simple comparison',
            'source': self._unparse(test),
        }

    def _map_compare(self, node: ast.Compare, lineno: int) -> dict[str, Any]:
        left_name  = self._extract_name(node.left)
        right_name = self._extract_name(node.comparators[0])
        op_str     = type(node.ops[0]).__name__
        is_right_const = isinstance(node.comparators[0], ast.Constant)
        left_ind  = self._match_indicator(left_name)
        right_ind = self._match_indicator(right_name)
        src = self._unparse(node)

        # 1. Indicator vs Constant (e.g. rsi < 30)
        if left_ind and is_right_const:
            val = float(node.comparators[0].value)
            operator = self._rb_op(op_str)
            return {
                'line': lineno,
                'status': 'converted',
                'ruleType': 'indicator_vs_value',
                'indicator': left_ind,
                'operator': operator,
                'value': val,
                'source': src,
                'leftKind': 'indicator',
                'leftIndicator': left_ind,
                'rightKind': 'constant',
                'rightValue': val,
            }

        # 2. Indicator vs Indicator (e.g. ema_fast > ema_slow)
        if left_ind and right_ind:
            operator = self._rb_op(op_str)
            return {
                'line': lineno,
                'status': 'converted',
                'ruleType': 'indicator_vs_indicator',
                'leftIndicator': left_ind,
                'operator': operator,
                'rightIndicator': right_ind,
                'source': src,
                'leftKind': 'indicator',
                'rightKind': 'indicator',
            }

        # 3. Price vs Indicator or Constant (e.g. close > 24000, close > vwap)
        if any(kw in left_name.lower() for kw in ('close', 'price', 'ltp', 'high', 'low')):
            price_field = 'CLOSE'
            for fld in ('OPEN', 'HIGH', 'LOW', 'CLOSE', 'LTP'):
                if fld.lower() in left_name.lower():
                    price_field = fld
                    break
            operator = self._rb_op(op_str)
            if is_right_const:
                val = float(node.comparators[0].value)
                return {
                    'line': lineno,
                    'status': 'converted',
                    'ruleType': 'price_vs_value',
                    'priceField': price_field,
                    'operator': operator,
                    'value': val,
                    'source': src,
                    'leftKind': 'price',
                    'leftField': price_field,
                    'rightKind': 'constant',
                    'rightValue': val,
                }
            if right_ind:
                return {
                    'line': lineno,
                    'status': 'converted',
                    'ruleType': 'price_vs_indicator',
                    'priceField': price_field,
                    'operator': operator,
                    'rightIndicator': right_ind,
                    'source': src,
                    'leftKind': 'price',
                    'leftField': price_field,
                    'rightKind': 'indicator',
                }

        # Partial conversion: comparison recognized, but operands not mapped to known indicators
        return {
            'line': lineno,
            'status': 'partially_converted',
            'ruleType': 'comparison',
            'reason': 'Could not resolve one or both sides to a known indicator or price field',
            'source': src,
        }

    def _map_boolop(self, node: ast.BoolOp, lineno: int) -> dict[str, Any]:
        sub = [self._try_map(v, lineno) for v in node.values]
        all_ok = all(r['status'] == 'converted' for r in sub)
        any_ok = any(r['status'] != 'could_not_be_represented' for r in sub)
        return {
            'line': lineno,
            'status': 'converted' if all_ok else ('partially_converted' if any_ok else 'could_not_be_represented'),
            'ruleType': 'bool_op',
            'operator': type(node.op).__name__,
            'subRules': sub,
            'source': self._unparse(node),
        }

    def _extract_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Subscript):
            return self._unparse(node.slice)
        if isinstance(node, ast.Call):
            return self._extract_name(node.func)
        return ''

    def _match_indicator(self, name: str) -> Optional[str]:
        nl = name.lower()
        for indicator, synonyms in KNOWN_INDICATORS.items():
            if any(syn in nl for syn in synonyms):
                return indicator
        return None

    def _rb_op(self, op_name: str) -> str:
        return {
            'Gt': '>', 'Lt': '<', 'GtE': '>=', 'LtE': '<=',
            'Eq': '==', 'NotEq': '!=',
        }.get(op_name, op_name)

    def _unparse(self, node: Optional[ast.AST]) -> str:
        if node is None:
            return ''
        try:
            return ast.unparse(node).strip()
        except Exception:
            return '?'


# ---------------------------------------------------------------------------
# Draft strategy builder
# ---------------------------------------------------------------------------

def _build_indicator_dict(ind_type: str, ind_id: str, order: int = 1) -> dict[str, Any]:
    """Build a valid Indicator dictionary complying with schema_v2."""
    return {
        'id': ind_id,
        'enabled': True,
        'order': order,
        'label': f"{ind_type} 14",
        'type': ind_type,
        'timeframe': '5m',
        'source': 'CLOSE',
        'period': 14,
        'smoothingPeriod': 14 if ind_type == 'RSI' else None,
        'multiplier': 3.0 if ind_type == 'SUPERTREND' else None,
        'fastPeriod': 12 if ind_type == 'MACD' else None,
        'slowPeriod': 26 if ind_type == 'MACD' else None,
        'signalPeriod': 9 if ind_type == 'MACD' else None,
        'stdDev': 2.0 if ind_type == 'BOLLINGER' else None,
        'kPeriod': 14 if ind_type == 'STOCHASTIC' else None,
        'dPeriod': 3 if ind_type == 'STOCHASTIC' else None,
    }


def _build_draft_strategy(
    rules: list[dict[str, Any]],
    params: dict[str, Any],
    name: str,
    content_hash: str,
    masked_source: str,
    analysis_report: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """
    Build a valid Rule Builder strategy draft from fully converted rules only.
    Uses new_rule_builder() as base so all schema fields and constraints pass.
    """
    converted = [r for r in rules if r.get('status') == 'converted']
    if not converted:
        return None

    # Start from canonical template
    draft = new_rule_builder()
    strat_id = str(uuid.uuid4())
    draft['id'] = strat_id
    draft['name'] = f"{name} (Imported Draft)"

    # Identify indicators needed
    needed_indicators: set[str] = set()
    for r in converted:
        if r.get('leftKind') == 'indicator' and r.get('leftIndicator'):
            needed_indicators.add(r['leftIndicator'])
        if r.get('rightKind') == 'indicator' and r.get('rightIndicator'):
            needed_indicators.add(r['rightIndicator'])

    # Build indicator list and id map
    ind_map: dict[str, str] = {}
    indicators_list = []
    for order_idx, ind_type in enumerate(sorted(needed_indicators), start=1):
        ind_id = f"ind-{order_idx}"
        ind_map[ind_type] = ind_id
        indicators_list.append(_build_indicator_dict(ind_type, ind_id, order=order_idx))

    draft['indicators'] = indicators_list

    # Build entry condition leaves
    first_rule = converted[0]
    operator = first_rule.get('operator', '>')

    if first_rule.get('leftKind') == 'indicator':
        left_op = {
            'kind': 'indicator',
            'field': None,
            'indicatorId': ind_map.get(first_rule['leftIndicator']),
            'output': 'value',
            'level': None,
            'value': None,
        }
    else:
        left_op = {
            'kind': 'price',
            'field': first_rule.get('leftField', 'CLOSE'),
            'indicatorId': None,
            'output': None,
            'level': None,
            'value': None,
        }

    if first_rule.get('rightKind') == 'indicator':
        right_op = {
            'kind': 'indicator',
            'field': None,
            'indicatorId': ind_map.get(first_rule['rightIndicator']),
            'output': 'value',
            'level': None,
            'value': None,
        }
    else:
        right_op = {
            'kind': 'constant',
            'field': None,
            'indicatorId': None,
            'output': None,
            'level': None,
            'value': float(first_rule.get('rightValue', 0.0)),
        }

    cond_leaf = _leaf(f"cond-{uuid.uuid4().hex[:8]}", operator=operator, left=left_op, right=right_op)
    draft['rules']['entry'] = [{
        'id': f"entry-{uuid.uuid4().hex[:8]}",
        'enabled': True,
        'order': 1,
        'label': f"Imported rule: {first_rule.get('source', 'Condition')}",
        'side': 'BUY',
        'condition': cond_leaf,
    }]

    # Quantity & Risk
    qty = int(params.get('quantity', params.get('qty', params.get('lot_size', 1))))
    draft['execution']['quantity'] = qty
    draft['risk']['maxDailyLoss'] = float(params.get('max_daily_loss', 2000))
    draft['risk']['maxCapital']   = float(params.get('max_capital', params.get('capital', 25000)))
    draft['risk']['maxTradesPerDay'] = int(params.get('max_trades', 3))
    draft['risk']['maxOpenPositions'] = 1
    draft['risk']['maxQuantity'] = max(int(draft['risk'].get('maxQuantity', 1)), qty)

    # Stop-loss and targets from detected parameters
    sl_val = float(params.get('stop_loss', params.get('sl', params.get('sl_points', 20.0))))
    draft['stopLoss']['points'] = sl_val

    tgt_val = float(params.get('target', params.get('tgt', params.get('target_points', 40.0))))
    draft['targets'] = [
        _target('tgt-1', quantity=0, order=1, enabled=False, label='Optional target 1'),
        _target('tgt-2', quantity=0, order=2, enabled=False, label='Optional target 2'),
        _target('tgt-3', quantity=qty, order=3, enabled=True, label='Final target'),
    ]
    draft['targets'][2]['value'] = tgt_val

    # Meta section with required sourceType "PYTHON_IMPORT" and required fields
    draft['meta']['id'] = strat_id
    draft['meta']['name'] = f"{name} (Imported Draft)"
    draft['meta']['status'] = 'DRAFT'
    draft['meta']['sourceType'] = 'PYTHON_IMPORT'
    draft['meta']['version'] = 1
    draft['meta']['importVersion'] = 1
    draft['meta']['validationStatus'] = 'NEEDS_REVIEW'
    draft['meta']['contentHash'] = content_hash
    draft['meta']['maskedSource'] = masked_source
    draft['meta']['analysisReport'] = analysis_report
    draft['meta']['note'] = (
        'Imported from Python source via static analysis. '
        'Only fully-understood rules are converted. Review before running paper simulation.'
    )

    # Top-level mirrors
    draft['sourceType'] = 'PYTHON_IMPORT'
    draft['version'] = 1
    draft['importVersion'] = 1
    draft['validationStatus'] = 'NEEDS_REVIEW'
    draft['contentHash'] = content_hash
    draft['maskedSource'] = masked_source
    draft['analysisReport'] = analysis_report

    return draft


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse_source(source: str, filename: str = 'strategy.py') -> dict[str, Any]:
    """
    Perform complete static analysis of Python source text.
    Returns a structured report dictionary.

    SECURITY CONTRACT:
    NEVER executes, imports, evals, or compiles-and-runs the source.
    """
    # 1. Content hash of original source (before masking)
    content_hash = hashlib.sha256(source.encode('utf-8')).hexdigest()

    # 2. Mask secrets before any storage or return
    masked_source, secrets_meta = mask_secrets(source)

    # 3. Syntax check only (no execution)
    try:
        tree = ast.parse(source, filename=filename, mode='exec')
    except SyntaxError as e:
        return {
            'ok': False,
            'importable': False,
            'syntaxOk': False,
            'syntaxError': {
                'message': str(e.msg),
                'line': e.lineno,
                'column': e.offset,
                'text': e.text,
            },
            'contentHash': content_hash,
            'maskedSource': masked_source,
            'secretsFound': secrets_meta,
            'imports': [],
            'entryPoints': [],
            'indicators': [],
            'dangerousConstructs': [],
            'ambiguousConstructs': [],
            'rules': [],
            'draftStrategy': None,
            'summary': f'Syntax error at line {e.lineno}: {e.msg}',
        }

    # 4. Walk AST — read-only visitors
    analysis = _AnalysisVisitor()
    analysis.visit(tree)

    rule_det = _RuleDetector()
    rule_det.visit(tree)

    # 5. Importability (dangerous constructs hard-block import)
    importable = not bool(analysis.dangerous_constructs)

    # 6. Basic report summary metrics
    conv  = sum(1 for r in rule_det.rules if r.get('status') == 'converted')
    part  = sum(1 for r in rule_det.rules if r.get('status') == 'partially_converted')
    block = sum(1 for r in rule_det.rules if r.get('status') == 'could_not_be_represented')

    summary_parts = [
        'Syntax OK.',
        f'Entry points: {len(analysis.entry_points)}.',
        f'Rules: {conv} converted, {part} partial, {block} not representable.',
    ]
    if analysis.dangerous_constructs:
        summary_parts.append(
            f'NOT IMPORTABLE — {len(analysis.dangerous_constructs)} dangerous construct(s) found.'
        )
    if secrets_meta:
        summary_parts.append(f'Secrets warning: {len(secrets_meta)} masked.')

    summary_str = ' '.join(summary_parts)

    minimal_report_meta = {
        'syntaxOk': True,
        'importable': importable,
        'entryPoints': [ep['name'] for ep in analysis.entry_points],
        'indicators': sorted(analysis.indicators_used),
        'dangerousConstructsCount': len(analysis.dangerous_constructs),
        'ambiguousConstructsCount': len(analysis.ambiguous_constructs),
        'rulesConverted': conv,
        'rulesPartial': part,
        'rulesBlocked': block,
        'secretsFound': len(secrets_meta),
        'summary': summary_str,
    }

    # 7. Build draft strategy (only if importable and has converted rules)
    draft = None
    if importable:
        name = filename.removesuffix('.py').replace('_', ' ').title()
        draft = _build_draft_strategy(
            rules=rule_det.rules,
            params=rule_det.param_assignments,
            name=name,
            content_hash=content_hash,
            masked_source=masked_source,
            analysis_report=minimal_report_meta,
        )

    return {
        'ok': True,
        'importable': importable,
        'syntaxOk': True,
        'syntaxError': None,
        'contentHash': content_hash,
        'maskedSource': masked_source,
        'secretsFound': secrets_meta,
        'imports': analysis.imports,
        'entryPoints': analysis.entry_points,
        'indicators': sorted(analysis.indicators_used),
        'dangerousConstructs': analysis.dangerous_constructs,
        'ambiguousConstructs': analysis.ambiguous_constructs,
        'rules': rule_det.rules,
        'draftStrategy': draft,
        'summary': summary_str,
    }
