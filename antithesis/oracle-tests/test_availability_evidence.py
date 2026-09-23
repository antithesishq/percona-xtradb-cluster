"""Detection test for the green-node availability oracle.

Two defects are under test, both found in run de51f0b91c31dea7c3e3dd22faa53e88-63-0
where this property reported 352 counterexamples and every single one of them
carried last_probe_commit_age_s = null:

  1. Antithesis runs several instances of an anytime_ command at once. The old
     whole-row read-modify-write let a probe process whose write had just
     failed store back the last_probe_commit_at it had read seconds earlier,
     erasing a success another process had recorded in between.
  2. The claim treated "no successful probe is on record" as "the node refused
     writes", so missing data fired a safety property.

These are detection tests: each one also checks that the REAL condition -- a
green node that genuinely refuses writes for the whole window -- is still
caught.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs

helper_stubs.install()

JOURNAL_DIR = tempfile.mkdtemp(prefix="pxcjournal-")
os.environ["PXC_JOURNAL_DIR"] = JOURNAL_DIR
os.environ["PXC_GREEN_WINDOW"] = "10"
os.environ["PXC_PROBE_INTERVAL"] = "1"
os.environ["PXC_PROBE_BUDGET"] = "40"
os.environ["PXC_WEDGE_WINDOW"] = "10000"   # keep the wedge oracle out of the way
os.environ["PXC_ERROR_LOG_SCAN_EVERY"] = "100000"

from antithesis import assertions as A  # noqa: E402
from pxcwl import config, db, journal, leases, oracles, probe  # noqa: E402

PROP = "a node advertising availability for a sustained window has committed a write in that window"

results = []


def check(name, ok, note=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + note) if note else ''}")


# ---------------------------------------------------------------------------
# 1. The clobber itself: two probe processes, one journal, one shared row.
# ---------------------------------------------------------------------------
class JR:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(journal.SCHEMA_SQL)
        self.conn.commit()
        self.inv_id = 1


def fresh_pair():
    path = os.path.join(tempfile.mkdtemp(prefix="pxcmerge-"), "j.sqlite3")
    a, b = JR(path), JR(path)
    with a.conn:
        a.conn.execute(
            "INSERT INTO progress (node, last_committed, last_advance_at) VALUES (?, 0, 0)",
            ("node1",),
        )
    return a, b


def merge(jr, t, *, probe_outcome, green=True, committed=100, advanced=False, wedged=False):
    return probe._merge_progress(
        jr, "node1", now=t, committed=committed, advanced=advanced, green=green,
        fc_paused=0, probe=probe_outcome, wedged=wedged,
    )


a, b = fresh_pair()
# Process A probes successfully at t=100. Process B read the row at t=95, when
# there was nothing in it, and its own probe failed; it writes at t=101.
merge(a, 100.0, probe_outcome=probe.PROBE_OK)
row = merge(b, 101.0, probe_outcome=probe.PROBE_FAILED)
check(
    "a failed probe in a second process does not erase a recorded success",
    row["last_probe_commit_at"] == 100.0,
    f"last_probe_commit_at={row['last_probe_commit_at']}",
)

a, b = fresh_pair()
merge(a, 100.0, probe_outcome=probe.PROBE_FAILED)
row = merge(b, 101.0, probe_outcome=probe.PROBE_FAILED)
check(
    "probe_failed_since latches at the first failure, not the latest",
    row["probe_failed_since"] == 100.0,
    f"probe_failed_since={row['probe_failed_since']}",
)
row = merge(b, 102.0, probe_outcome=probe.PROBE_OK)
check(
    "a successful probe clears the failure run",
    row["probe_failed_since"] is None and row["last_probe_commit_at"] == 102.0,
)
row = merge(b, 103.0, probe_outcome=probe.PROBE_NO_SCHEMA)
check(
    "an unusable probe is not a failure run",
    row["probe_failed_since"] is None,
)

a, b = fresh_pair()
merge(a, 100.0, probe_outcome=probe.PROBE_FAILED, green=True)
row = merge(b, 101.0, probe_outcome=probe.PROBE_FAILED, green=True)
check(
    "advertised_since keeps the earliest green observation across processes",
    row["advertised_since"] == 100.0,
    f"advertised_since={row['advertised_since']}",
)
row = merge(b, 102.0, probe_outcome=probe.PROBE_FAILED, green=False)
check(
    "one non-green observation clears the green window for everybody",
    row["advertised_since"] is None,
)

a, b = fresh_pair()
merge(a, 100.0, probe_outcome=probe.PROBE_OK, committed=500, advanced=True)
row = merge(b, 101.0, probe_outcome=probe.PROBE_OK, committed=400, advanced=False)
check(
    "last_committed never moves backwards under a stale writer",
    row["last_committed"] == 500,
    f"last_committed={row['last_committed']}",
)

print()

# ---------------------------------------------------------------------------
# 2. The gate: drive the real probe loop with a fake clock.
# ---------------------------------------------------------------------------
STATUS = {
    "wsrep_last_committed": "1000",
    "wsrep_local_recv_queue": "0",
    "wsrep_desync_count": "0",
    "wsrep_flow_control_sent": "0",
    "wsrep_flow_control_paused_ns": "0",
    "wsrep_local_state": "4",
    "wsrep_local_state_comment": "Synced",
    "wsrep_cluster_status": "Primary",
    "wsrep_ready": "ON",
    "wsrep_cluster_size": "3",
    "wsrep_incoming_addresses": "10.0.0.1:3306",
}


class Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def time(self):
        return self.t

    def sleep(self, d):
        self.t += d


class FakeConn:
    def cursor(self, *a, **k):
        raise AssertionError("unexpected cursor use")


def drive(outcomes, *, maint="DISABLED", ticks=25, preseed=None):
    """Run the real loop for `ticks` iterations. `outcomes` is a per-tick list.

    Returns the assertion records the availability property produced.
    """
    A.FIRED.clear()
    for f in pathlib.Path(JOURNAL_DIR).glob("*"):
        f.unlink()
    clock = Clock()
    probe.time = clock
    journal.time = clock
    leases.repair_expired = lambda jr: None
    config.NODES = [("node1", "10.0.0.1")]
    config.EXPECTED_CLUSTER_SIZE = 3
    config.PROBE_WALL_BUDGET_SECONDS = float(ticks)
    db.cluster_status = lambda: {"node1": dict(STATUS)}
    db.connect_with_retry = lambda *a, **k: FakeConn()
    db.close_quietly = lambda c: None
    db.global_vars = lambda conn, names: ({} if maint is None else {"pxc_maint_mode": maint})
    db.error_log_matches = lambda conn: {}

    seq = {"i": 0}

    def fake_probe(host, node):
        i = min(seq["i"], len(outcomes) - 1)
        seq["i"] += 1
        return outcomes[i], ("stub" if outcomes[i] != probe.PROBE_OK else None)

    probe._probe_write = fake_probe

    if preseed is not None:
        jr = journal.Journal("seed")
        with jr.conn:
            jr.conn.execute(
                "INSERT OR REPLACE INTO progress "
                "(node, last_committed, last_advance_at, advertised_since) "
                "VALUES ('node1', 0, 0, ?)",
                (clock.t - preseed,),
            )
        jr.finish()

    probe._run()
    return [r for r in A.FIRED if r["message"] == PROP]


W = config.GREEN_WINDOW_SECONDS  # 10

fired = drive([probe.PROBE_OK])
check(
    "a green node whose probe commits reports the property as satisfied",
    fired and all(r["cond"] for r in fired),
    f"{len(fired)} evaluations, {sum(1 for r in fired if not r['cond'])} failing",
)

fired = drive([probe.PROBE_FAILED])
bad = [r for r in fired if not r["cond"]]
check(
    "a green node refusing writes for the whole window is still caught",
    bool(bad) and all(not r["cond"] for r in fired),
    f"{len(bad)} counterexamples, first at "
    f"{bad[0]['details']['probe_failed_seconds'] if bad else '-'}s of failure",
)
check(
    "the counterexample says why the probe failed",
    bool(bad) and bad[0]["details"].get("probe_reason") == "stub"
    and bad[0]["details"].get("probe_outcome") == probe.PROBE_FAILED,
    str(bad[0]["details"]) if bad else "",
)
check(
    "no counterexample fires before the failure run covers the window",
    all(r["details"]["probe_failed_seconds"] >= W for r in bad),
)

# The regression. Green for ages, but the probe has only just started failing:
# under the old code the very first evaluation fired with a null commit age.
fired = drive([probe.PROBE_FAILED], ticks=int(W) - 2, preseed=500.0)
check(
    "a long-green node with no probe evidence yet is not a counterexample",
    not any(not r["cond"] for r in fired),
    f"{len(fired)} evaluations, {sum(1 for r in fired if not r['cond'])} failing",
)
check(
    "and none of them is the null-commit-age shape at all",
    not any(r["details"]["last_probe_commit_age_s"] is None for r in fired),
)

# One early success must not exempt a node that then wedges for good.
fired = drive([probe.PROBE_OK] + [probe.PROBE_FAILED] * 40, ticks=30)
check(
    "one probe at the start of the green run does not exempt a later wedge",
    any(not r["cond"] for r in fired),
    f"{sum(1 for r in fired if not r['cond'])} counterexamples",
)

fired = drive([probe.PROBE_FAILED], maint=None)
check(
    "a node whose pxc_maint_mode could not be read is never judged green",
    not fired,
    f"{len(fired)} evaluations",
)

fired = drive([probe.PROBE_NO_SCHEMA])
check(
    "a probe that cannot address the schema is never judged",
    not fired,
    f"{len(fired)} evaluations",
)

# The wedge watchdog shares the merged row, so it has to survive the rewrite.
FROZEN = "cluster commit progress never freezes while every node reports Synced"
os.environ["PXC_WEDGE_WINDOW"] = "10"
config.WEDGE_WINDOW_SECONDS = 10.0
drive([probe.PROBE_OK])
frozen = [r for r in A.FIRED if r["message"] == FROZEN]
check(
    "the wedge watchdog still passes a committing node",
    bool(frozen) and all(r["cond"] for r in frozen),
    f"{len(frozen)} evaluations",
)
drive([probe.PROBE_FAILED], ticks=30)
frozen = [r for r in A.FIRED if r["message"] == FROZEN]
check(
    "the wedge watchdog still catches a Synced node that never commits",
    any(not r["cond"] for r in frozen),
    f"{sum(1 for r in frozen if not r['cond'])} counterexamples",
)

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
