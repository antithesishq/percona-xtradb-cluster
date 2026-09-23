"""Detection test for the maint_mode_cycle view-stability gate.

The gate must do exactly two things: suppress the claim when a view change
could have overridden the operator, and leave it fully armed otherwise.
"""
import sqlite3
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs
helper_stubs.install()

from antithesis import assertions as A
from pxcwl import levers, db, leases, rnd

PROP = "an operator-set pxc_maint_mode=MAINTENANCE is never reverted to DISABLED"


class S:
    name, host = "node1", "10.0.0.1"


class FakeConn:
    def cursor(self, *a, **k): raise AssertionError("unexpected cursor use")


def run(intent, observed, conf_seq):
    """conf_seq: values wsrep_cluster_conf_id returns, in call order."""
    A.FIRED.clear()
    seq = list(conf_seq)
    calls = {"n": 0}

    def status_vars(conn, names):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        v = seq[i]
        return {} if v is None else {"wsrep_cluster_conf_id": v}

    db.connect_with_retry = lambda *a, **k: FakeConn()
    db.close_quietly = lambda c: None
    db.status_vars = status_vars
    db.global_vars = lambda conn, names: {"pxc_maint_mode": observed}
    levers._set_global = lambda *a, **k: True
    leases.acquire = lambda *a, **k: True
    leases.release = lambda *a, **k: None
    rnd.choice = lambda seq_: intent if set(seq_) == {"MAINTENANCE", "DISABLED"} else 1
    levers.time.sleep = lambda s: None

    levers.maint_mode_cycle(object(), S(), {})
    return [f for f in A.FIRED if f["message"] == PROP]


results = []

def case(name, intent, observed, conf_seq, want):
    """want: True = fires passing, False = fires failing, None = must not fire."""
    fired = run(intent, observed, conf_seq)
    if want is None:
        ok = len(fired) == 0
        got = "no assertion" if ok else f"fired cond={fired[0]['cond']}"
    else:
        ok = len(fired) == 1 and fired[0]["cond"] == want
        got = f"fired cond={fired[0]['cond']}" if fired else "no assertion"
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"        want {'no assertion' if want is None else f'cond={want}'}, got {got}")
    results.append(ok)


# THE FINDING this property exists to make: operator drain silently erased.
case("intent MAINTENANCE, reverted to DISABLED -> CAUGHT (the forced-revert hijack)",
     "MAINTENANCE", "DISABLED", ["7", "8"], False)

# Same, with no view change at all. Must still be caught.
case("intent MAINTENANCE, reverted to DISABLED, view stable -> CAUGHT",
     "MAINTENANCE", "DISABLED", ["7", "7"], False)

# Honoured.
case("intent MAINTENANCE, still MAINTENANCE -> passes",
     "MAINTENANCE", "MAINTENANCE", ["7", "7"], True)

# Carve-out 1: shutdown wins by design (mysqld.cc:4395-4404).
case("observed SHUTDOWN -> carved out, no claim",
     "MAINTENANCE", "SHUTDOWN", ["7", "7"], None)

# Carve-out 2: the forced-FLIP branch belongs to a different property.
case("intent DISABLED, forced to MAINTENANCE -> carved out, no claim",
     "DISABLED", "MAINTENANCE", ["7", "8"], None)

# The DISABLED direction makes no claim at all, even when honoured.
case("intent DISABLED, observed DISABLED -> no claim",
     "DISABLED", "DISABLED", ["7", "7"], None)

# ---------------------------------------------------------------- leases
print()
in_set = "maint_mode_cycle" in leases.DISRUPTIVE
print(f"{'PASS' if in_set else 'FAIL'}  maint_mode_cycle is in leases.DISRUPTIVE")
results.append(in_set)

# The token really is mutually exclusive with graceful_shutdown.
import importlib
importlib.reload(leases)
from pxcwl import journal
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.executescript(journal.SCHEMA_SQL)


class JR:
    def __init__(self, c, inv): self.conn, self.inv_id = c, inv


leases._all_synced = lambda: True
jr1, jr2 = JR(conn, 1), JR(conn, 2)
a = leases.acquire(jr1, "graceful_shutdown", "node1",
                   intent="x", restore="", hold_seconds=5)
b = leases.acquire(jr2, "maint_mode_cycle", "node2",
                   intent="MAINTENANCE", restore="DISABLED", hold_seconds=5)
excl = a and not b
print(f"{'PASS' if excl else 'FAIL'}  graceful_shutdown held -> maint_mode_cycle refused "
      f"(shutdown={a}, maint={b})")
results.append(excl)

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
