"""Detection tests for the false positives triaged in run 5aa4afb5-63-0.

Each section replays the counterexample that went red on a correct cluster and
requires it to stay silent now -- and, beside it, injects the real failure the
property exists for and requires it to still be caught. A fix that silences a
property entirely passes the first half and fails the second.

  1. applier_resize judged a node whose provider was disconnected.
  2. A COMMIT that failed 1105/1205/1317 was journalled FAILED, although the
     writeset may already have replicated.
  3. The reconciler's red carried no journal evidence to classify it by.
  4. Terminal verify expected three Synced nodes after a total loss of the
     Primary Component, which Galera documents as needing an operator.
  5. Session.ensure stopped at the first rejected SET.
"""
import sqlite3
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs
helper_stubs.install()

import pymysql
from antithesis import assertions as A
from pxcwl import checks, config, db, journal, leases, levers, ops, rnd

results = []


def check(name, ok, got=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if got:
        print(f"        got {got}")
    results.append(ok)


def fired(prop):
    return [f for f in A.FIRED if f["message"] == prop]


class FakeJR:
    inv_id = 3

    def __init__(self):
        self.resolved, self.tallies, self.n = [], [], 0

    def tally(self, shape, errno): self.tallies.append((shape, errno))
    def new_wid(self):
        self.n += 1
        return (self.inv_id << 24) | self.n
    def attempt(self, wids, **k): pass
    def attempted_witness_rows(self): return 0
    def resolve(self, wids, state, errno=None, errmsg=None):
        self.resolved.append((list(wids), state, errno, errmsg))


# ======================================================================= 1
print("--- applier_resize")
RESIZE = "applier thread count reaches the configured setpoint after a resize"
SYNCED = {"wsrep_connected": "ON", "wsrep_local_state": "4",
          "wsrep_cluster_status": "Primary", "wsrep_local_state_comment": "Synced",
          "wsrep_last_committed": "10"}
# What node3 reported in the counterexample: provider gone, pool empty.
DISCONNECTED = {"wsrep_connected": "OFF", "wsrep_local_state": "0",
                "wsrep_cluster_status": "Disconnected",
                "wsrep_local_state_comment": "Initialized",
                "wsrep_last_committed": "10", "wsrep_thread_count": "0"}


class NoConn:
    pass


def run_resize(before, polls, target=8):
    A.FIRED.clear()
    seq = [before] + list(polls)
    calls = {"n": 0}

    def node_status(host):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return dict(seq[i]) if seq[i] is not None else None

    db.node_status = node_status
    db.connect_with_retry = lambda *a, **k: NoConn()
    db.close_quietly = lambda c: None
    db.global_vars = lambda conn, names: {"wsrep_applier_threads": str(target)}
    levers._set_global = lambda *a, **k: True
    leases.acquire = lambda *a, **k: True
    leases.release = lambda *a, **k: None
    levers.rnd.choice = lambda seq_: target
    levers.time.sleep = lambda s: None
    config.RESIZE_SETTLE_SECONDS = 0.05
    levers.applier_resize(FakeJR(), type("S", (), {"name": "node3", "host": "h3"})(), {})
    return fired(RESIZE)


f = run_resize(SYNCED, [DISCONNECTED])
check("synced at SET, then disconnected with thread_count 0 -> no verdict (the 5aa4afb5 red)",
      not f, f"{len(f)} assertion(s)")
f = run_resize(DISCONNECTED, [DISCONNECTED])
check("disconnected before the SET -> lever skipped, no verdict", not f, f"{len(f)} assertion(s)")
f = run_resize(SYNCED, [{**SYNCED, "wsrep_thread_count": "9"}])
check("synced throughout, pool reaches 8+1 -> passes", len(f) == 1 and f[0]["cond"],
      str([x["cond"] for x in f]))
f = run_resize(SYNCED, [{**SYNCED, "wsrep_thread_count": "1"}])
check("synced throughout, pool stuck at 1 -> CAUGHT", len(f) >= 1 and not f[-1]["cond"],
      str([x["cond"] for x in f]))

# ======================================================================= 2
print("\n--- commit-phase classification")
db.is_alive = lambda c: True
E = pymysql.OperationalError
for errno, at_commit, want in [
    (1105, True, "UNKNOWN"),   # wsrep default client_error -> 1105 at COMMIT
    (1317, True, "UNKNOWN"),   # e_interrupted_error
    (1205, True, "UNKNOWN"),   # e_timeout_error
    (1105, False, "FAILED"),   # statement-time 1105 is still a clean rejection
    (1205, False, "FAILED"),
    (1213, True, "FAILED"),    # certification failure: dummy writeset everywhere
    (1062, True, "FAILED"),
    (2013, True, "UNKNOWN"),
]:
    state = journal.classify(E(errno, "x"), object(), at_commit=at_commit)[0]
    check(f"errno {errno} {'at COMMIT' if at_commit else 'mid-statement'} -> {want}",
          state == want, state)


class TxnConn:
    """Explicit-transaction connection whose COMMIT fails with ``errno``."""

    def __init__(self, errno): self.errno = errno
    def begin(self): pass
    def rollback(self): pass
    def commit(self): raise E(self.errno, "commit failed")
    def cursor(self, *a, **k):
        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, *a): pass
            def executemany(s, *a): pass
        return C()


ops.rnd.choice = lambda seq_: 10
for errno, want in [(1105, "UNKNOWN"), (1213, "FAILED")]:
    jr = FakeJR()
    s = ops.Session("node1", "h1", {})
    s.conn = TxnConn(errno)
    ops.insert_multirow(jr, s, {})
    got = jr.resolved[-1][1] if jr.resolved else None
    check(f"insert_multirow COMMIT fails {errno} -> journalled {want}", got == want, got)
    msg = jr.resolved[-1][3] if jr.resolved else ""
    check(f"insert_multirow COMMIT fails {errno} -> phase recorded in errmsg",
          (msg or "").startswith("[at_commit]"), msg)

# ======================================================================= 3
print("\n--- reconcile evidence")
jconn = sqlite3.connect(":memory:")
jconn.row_factory = sqlite3.Row
jconn.executescript(journal.SCHEMA_SQL)
cols = [r[1] for r in jconn.execute("PRAGMA table_info(ack)")]
rows = [
    (50332897, "witness", 3, "node2", "sr_session", "FAILED", 1105, "[at_commit] boom"),
    (50332898, "witness", 3, "node2", "insert_witness", "ACKED", None, None),
]
for r in rows:
    vals = dict(zip(["wid", "target", "inv_id", "node", "shape", "state", "errno", "errmsg"], r))
    for c in cols:
        vals.setdefault(c, 0)
    jconn.execute(
        f"INSERT INTO ack ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
        list(vals.values()),
    )


class RJ:
    conn = jconn
    def ack_counts(self): return {}


class NodeConn:
    def __init__(self, wids): self.wids = wids
    def cursor(self, *a, **k):
        it = iter([(w,) for w in self.wids])
        class C:
            def execute(s, *a): pass
            def fetchone(s): return next(it, None)
            def close(s): pass
        return C()


acked_ok, failed_ok, minted_ok, d = checks.reconcile(
    RJ(), {"node1": NodeConn([50332897, 50332898])}
)
ev = d.get("failed_present_evidence", {}).get("50332897", {})
check("a FAILED row present on a node -> still CAUGHT", not failed_ok and acked_ok and minted_ok)
check("the red carries the journal's shape, errno and phase",
      ev.get("shape") == "sr_session" and ev.get("errno") == 1105
      and ev.get("errmsg", "").startswith("[at_commit]"), str(ev))
acked_ok, failed_ok, _, d = checks.reconcile(RJ(), {"node1": NodeConn([50332898])})
check("FAILED row absent -> passes, no evidence", failed_ok and not d["failed_present_evidence"])

# ======================================================================= 4
print("\n--- operator bootstrap on total loss of Primary")
REFUSED = "OperationalError: (2003, \"Can't connect to MySQL server on '10.20.20.11' ([Errno 111] Connection refused)\")"
TIMEOUT = "OperationalError: (2003, \"Can't connect to MySQL server on '10.20.20.11' (timed out)\")"
H1 = dict(config.NODES)["node1"]


def nonprim(lc):
    return {"wsrep_cluster_status": "non-Primary", "wsrep_local_state_comment": "Initialized",
            "wsrep_cluster_size": "3", "wsrep_last_committed": str(lc)}


def ledger(**high):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(journal.SCHEMA_SQL)
    for node, lc in high.items():
        c.execute("INSERT INTO progress (node, last_committed, last_advance_at) VALUES (?, ?, 0)",
                  (node, lc))
    return type("LJ", (), {"conn": c})()


def run_bootstrap(samples, n1_error, jr=None):
    seq = list(samples)
    calls = {"n": 0}
    sent = []

    def cluster_status():
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    class BConn:
        def cursor(self):
            class C:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def execute(s, sql, *a): sent.append(sql)
            return C()

    db.cluster_status = cluster_status
    db.LAST_ERROR.clear()
    db.LAST_ERROR[H1] = n1_error
    db.connect_with_retry = lambda host, *a, **k: BConn()
    checks.time.sleep = lambda s: None
    jr = jr or ledger(node1=690, node2=700, node3=704)
    return checks.bootstrap_if_no_primary(jr), sent


def did(r, sent):
    return bool(r) and r["bootstrapped"] and any("pc.bootstrap=YES" in q for q in sent)


# The 5aa4afb5 end state: node1 between boots and counted in the others'
# 3-member non-Primary component; node2/node3 non-Primary.
LOST = {"node1": None, "node2": nonprim(700), "node3": nonprim(704)}
r, sent = run_bootstrap([LOST, LOST, LOST], REFUSED)
check("no Primary anywhere, node1 refused -> bootstraps the most advanced node (node3)",
      did(r, sent) and r["node"] == "node3", f"{r and r['node']} {sent}")

DNS = "OperationalError: (2003, \"Can't connect to MySQL server on 'pxc-node1' ([Errno -2] Name or service not known)\")"
r, sent = run_bootstrap([LOST, LOST, LOST], DNS)
check("node1 container gone (DNS failure) -> treated as down, bootstraps", did(r, sent))

with_prim = {**LOST, "node2": {**nonprim(700), "wsrep_cluster_status": "Primary"}}
r, sent = run_bootstrap([with_prim, with_prim], REFUSED)
check("a node is still Primary -> never bootstraps (would be split brain)", r is None and not sent)

r, sent = run_bootstrap([LOST, LOST], TIMEOUT)
check("unreachable node timed out, not refused -> never bootstraps (may be a hung Primary)",
      r is None and not sent)

HEALED = {"node1": nonprim(704), "node2": {**nonprim(704), "wsrep_cluster_status": "Primary"},
          "node3": nonprim(704)}
r, sent = run_bootstrap([LOST, HEALED], REFUSED)
check("condition gone on the second sample (remerge in progress) -> left alone",
      r is None and not sent)

r, sent = run_bootstrap([LOST, LOST, with_prim], REFUSED)
check("a Primary appears between sampling and the SET -> not sent",
      bool(r) and not r["bootstrapped"] and not sent, str(r and r["error"]))

# The reviewer's scenario: node1 was a lone Primary acknowledging writes, then
# went down. The others do not count it in their component, so it may restore
# its own Primary from gvwstate.dat.
OUTSIDE = {"node1": None, "node2": {**nonprim(700), "wsrep_cluster_size": "2"},
           "node3": {**nonprim(704), "wsrep_cluster_size": "2"}}
r, sent = run_bootstrap([OUTSIDE, OUTSIDE, OUTSIDE], REFUSED)
check("down node outside the reachable nodes' component -> never bootstraps", r is None and not sent)

# Same, but node1 is in the component; the ledger saw it further ahead than
# anyone reachable, so bootstrapping node3 would SST its acked writes away.
r, sent = run_bootstrap([LOST, LOST, LOST], REFUSED, ledger(node1=900, node2=700, node3=704))
check("ledger saw a down node ahead of every reachable node -> never bootstraps",
      bool(r) and not r["bootstrapped"] and not sent, str(r and r["error"]))

# ======================================================================= 5
print("\n--- Session.ensure")


class PostureConn:
    def __init__(self): self.sent = []
    def cursor(self):
        conn = self
        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, sql, params=()):
                conn.sent.append(sql)
                if "SERIALIZABLE" in sql:
                    raise E(1105, "Percona-XtraDB-Cluster doesn't recommend using SERIALIZABLE")
        return C()


pc = PostureConn()
db.connect_with_retry = lambda *a, **k: pc
db.close_quietly = lambda c: None
s = ops.Session("node1", "h1", {"isolation": "SERIALIZABLE", "sync_wait_level": 7,
                                "sr_fragment_size": 16, "sr_fragment_unit": "rows"})
ok = s.ensure()
check("rejected SERIALIZABLE does not skip the settings after it",
      ok and len(pc.sent) == 4 and any("wsrep_sync_wait" in q for q in pc.sent)
      and any("wsrep_trx_fragment_size" in q for q in pc.sent), str(pc.sent))
check("the rejection is recorded for the driver to tally",
      s.posture_failed == {"isolation": 1105}, str(s.posture_failed))

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
