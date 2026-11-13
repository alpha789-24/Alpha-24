import json
import os
from pathlib import Path
from threading import RLock

LOCK = RLock()
_DEFAULT_DATA = Path(__file__).resolve().parents[1] / 'data'


def data_root(data_dir=None):
    if data_dir:
        root = Path(data_dir)
    else:
        raw = os.getenv('DATA_DIR', '').strip()
        root = Path(raw) if raw else _DEFAULT_DATA
    root.mkdir(parents=True, exist_ok=True)
    return root


def read(name, default, data_dir=None):
    p = data_root(data_dir) / name
    with LOCK:
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return default


def write(name, value, data_dir=None):
    p = data_root(data_dir) / name
    with LOCK:
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')
        tmp.replace(p)
