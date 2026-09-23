"""Fake `antithesis` and `pymysql` packages, built into a temp dir on demand.

There is no container runtime on the harness development machine, so the
oracles are exercised against stubs instead. Putting the stubs on sys.path
ahead of the real packages is what lets checks.py and levers.py be imported
and driven without a server -- and, more usefully, lets a divergence be
INJECTED so the tests prove the oracles DETECT rather than merely that they
do not crash.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

_FILES = {
    "antithesis/__init__.py": "",
    "antithesis/assertions.py": '''
FIRED = []


def _rec(kind, cond, message, details):
    FIRED.append({"kind": kind, "cond": bool(cond), "message": message, "details": details})


def always(condition, message, details): _rec("always", condition, message, details)
def always_or_unreachable(condition, message, details): _rec("aou", condition, message, details)
def sometimes(condition, message, details): _rec("sometimes", condition, message, details)
def reachable(message, details): _rec("reachable", True, message, details)
def unreachable(message, details): _rec("unreachable", False, message, details)
''',
    "antithesis/random.py": '''
import random as _r


def get_random(): return _r.getrandbits(60)
def random_choice(seq): return _r.choice(list(seq))
''',
    "pymysql/__init__.py": '''
class Error(Exception): pass
class OperationalError(Error): pass
from . import cursors  # noqa: E402,F401
''',
    "pymysql/cursors.py": "class SSCursor: pass\n",
}

WORKLOAD = str(pathlib.Path(__file__).resolve().parent.parent / "workload")


def install() -> str:
    """Materialise the stubs and put them, and the workload, on sys.path."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="pxcstub-"))
    for rel, body in _FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    sys.path.insert(0, WORKLOAD)
    sys.path.insert(0, str(root))
    return str(root)
