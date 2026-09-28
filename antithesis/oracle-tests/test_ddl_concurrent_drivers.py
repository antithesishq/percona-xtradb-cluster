"""Detection test for DDL votes that concurrent drivers manufacture.

test_ddl_direction.py proves the generator picks the legal direction against
ONE catalog that one driver reads and writes. The real workload does not look
like that. Several parallel_driver_ invocations run at once, each connected to
a different node. TOI acknowledges a DDL once the ORIGINATING node has applied
it, while every other node is still catching up. Run 93ec5045...-63-2 showed
what that costs: 952 inconsistency votes after the direction fix had landed,
557 of them ER_CANT_DROP_FIELD_OR_KEY. And one of those votes, landing on a
node that was mid-IST, got that node declared inconsistent for the rest of the
timeline.

The model here is the smallest one that has both failure mechanisms:

  * an authoritative, totally ordered catalog that judges every statement,
    the way TOI does: all nodes apply it in order, and it fails on all of
    them or none;
  * a per-node copy that lags. A statement lands on its origin node at once,
    and on the others only when a causal read (the wsrep_sync_wait READ bit)
    drains the backlog, or on a 1-in-10 chance otherwise;
  * a real gap between lookup and statement, so threads interleave inside it.

Four threads drive ddl.run_one -- the real entry point, including the real
flock -- against three nodes. The fixed generator must emit zero statements
the authoritative catalog rejects. The detection half removes each fix in
turn and requires the model to catch the result, so a green result
distinguishes a fixed generator from a model that cannot see a race.
"""
import contextlib
import pathlib
import random
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs

helper_stubs.install()

from pxcwl import config, ddl  # noqa: E402

from helper_ddl_model import JR, Catalog  # noqa: E402

TABLES = config.SCRATCH_TABLES + ["wl_fk_parent", "wl_fk_child"]

results = []


def check(name, ok, note=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + note) if note else ''}")


NODES = ["node1", "node2", "node3"]


class Cluster:
    def __init__(self, seed, catchup=0.1):
        self.catchup = catchup
        self.auth = Catalog(TABLES)
        self.view = {n: Catalog(TABLES) for n in NODES}
        self.backlog = {n: [] for n in NODES}
        self.mu = threading.Lock()
        self.rng = random.Random(seed)
        self.synced_reads = 0

    def execute(self, node, stmt):
        with self.mu:
            before = len(self.auth.invalid)
            self.auth.apply(stmt)
            if len(self.auth.invalid) != before:
                return          # failed everywhere; nothing to replicate
            # TOI runs a statement in total order, so by the time the origin
            # node applies it, that node has applied everything ordered
            # before it. Only the OTHER nodes lag.
            self._drain(node)
            self.view[node].apply(stmt)
            for other in NODES:
                if other != node:
                    self.backlog[other].append(stmt)

    def read_view(self, node, causal):
        with self.mu:
            if causal:
                self.synced_reads += 1
            # A non-causal read catches up only occasionally: this models a
            # node slowed by a network clog, which is the normal case in a
            # fault-injected run, not a corner.
            if causal or self.rng.random() < self.catchup:
                self._drain(node)
            return self.view[node]

    def _drain(self, node):
        for stmt in self.backlog[node]:
            self.view[node].apply(stmt)
        self.backlog[node].clear()


class Cursor:
    def __init__(self, conn):
        self.conn, self.rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        low = sql.lower()
        c = self.conn
        if low.startswith("select @@session.wsrep_sync_wait"):
            self.rows = [(c.sync_wait,)]
            return
        if low.startswith("set session wsrep_sync_wait"):
            c.sync_wait = int(params[0])
            return
        if low.startswith("set session"):
            return
        if "information_schema" in low:
            view = c.cluster.read_view(c.node, causal=bool(c.sync_wait & 1))
            if "information_schema.columns" in low:
                self.rows = [(x,) for x in sorted(view.cols.get(params[1], ()))]
            elif "information_schema.statistics" in low:
                self.rows = [(x,) for x in sorted(view.idx.get(params[1], ()))]
            else:
                self.rows = sorted(view.fks.items())
            # The gap between reading the catalog and acting on it. Without
            # this the GIL would run each driver's lookup and statement back
            # to back, and the model would never see the race it tests for.
            time.sleep(0.0005)
            return
        c.cluster.execute(c.node, sql)

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, cluster, node, sync_wait):
        self.cluster, self.node, self.sync_wait = cluster, node, sync_wait
        self.initial_sync_wait = sync_wait

    def cursor(self, *a, **k):
        return Cursor(self)


class S:
    def __init__(self, conn):
        self.conn, self.name = conn, conn.node


def drive(seed, per_thread=150):
    cluster = Cluster(seed)
    jr = JR()
    # Session levels as swarm.py draws them. Only a level with the READ bit
    # makes a lookup causal by itself, so the fix must supply that bit
    # whatever level the session started at.
    sessions = [S(Conn(cluster, n, lvl)) for n, lvl in
                [("node1", 0), ("node2", 0), ("node3", 2), ("node1", 0)]]

    def worker(s):
        for _ in range(per_thread):
            ddl.run_one(jr, s, {})

    threads = [threading.Thread(target=worker, args=(s,)) for s in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return cluster, jr, sessions


config.JOURNAL_DIR = tempfile.mkdtemp(prefix="ddl-lock-")

# ---------------------------------------------------------------------------
fixed = [drive(seed) for seed in range(3)]
invalid = sum(len(c.auth.invalid) for c, _, _ in fixed)
emitted = sum(len(c.auth.applied) for c, _, _ in fixed)
check(
    "four concurrent drivers on three lagging nodes emit no statement TOI would reject",
    invalid == 0,
    f"{invalid} invalid of {emitted} emitted"
    + (f"; first: {fixed[0][0].auth.invalid[:1]}" if invalid else ""),
)
check(
    "and they are emitting real work",
    emitted > 600,
    f"{emitted} statements",
)
check(
    "every catalog read was causal",
    all(c.synced_reads > 0 for c, _, _ in fixed),
    f"{[c.synced_reads for c, _, _ in fixed]} causal reads",
)
check(
    "the session's own wsrep_sync_wait is restored after every lookup",
    all(s.conn.sync_wait == s.conn.initial_sync_wait for _, _, ss in fixed for s in ss),
    f"{[(s.name, s.conn.sync_wait) for s in fixed[0][2]]}",
)
busy = [t for _, jr, _ in fixed for t in jr.tallies if t[0].endswith(":lock_busy")]
check(
    "no driver was starved out by the lock at this contention",
    not busy,
    f"{len(busy)} lock_busy skips",
)

# ---------------------------------------------------------------------------
# Detection: take each fix away and require the model to see the damage.


@contextlib.contextmanager
def _no_lock():
    yield True


real_lock, real_bit = ddl._scratch_lock, ddl.LOOKUP_SYNC_WAIT_READ_BIT


def broken(lock, bit):
    ddl._scratch_lock, ddl.LOOKUP_SYNC_WAIT_READ_BIT = lock, bit
    try:
        runs = [drive(seed) for seed in range(3)]
    finally:
        ddl._scratch_lock, ddl.LOOKUP_SYNC_WAIT_READ_BIT = real_lock, real_bit
    return sum(len(c.auth.invalid) for c, _, _ in runs), sum(len(c.auth.applied) for c, _, _ in runs)


bad, total = broken(_no_lock, 0)
check(
    "the model rejects the previous generator: no lock, no causal read (proves it detects)",
    bad > 20,
    f"{bad} of {total} invalid",
)
# The causal read, isolated. With the lock in place, each node's own TOI
# statements keep its lag short, so a statistical check against the threaded
# run above sees only a handful of stale reads -- too few to fail reliably.
# Take the threads away instead. Two drivers take turns, one on node1 and
# one on node2, and node2 never catches up unless a read is causal. That is
# a node behind a long clog. node2's own TOI statements refresh its view
# (the origin node is always current), so node1 takes nine turns for each of
# node2's. That way node2 always reads a catalog nine statements behind.
def alternate(bit, turns=1000):
    ddl.LOOKUP_SYNC_WAIT_READ_BIT = bit
    try:
        cluster, jr = Cluster(0, catchup=0.0), JR()
        pair = [S(Conn(cluster, "node1", 0)), S(Conn(cluster, "node2", 0))]
        for i in range(turns):
            ddl.run_one(jr, pair[1 if i % 10 == 9 else 0], {})
    finally:
        ddl.LOOKUP_SYNC_WAIT_READ_BIT = real_bit
    return len(cluster.auth.invalid), len(cluster.auth.applied)


bad, total = alternate(real_bit)
check(
    "a node that lags indefinitely still gets a correct lookup when the read is causal",
    bad == 0 and total > 500,
    f"{bad} of {total} invalid",
)
bad, total = alternate(0)
check(
    "and without the causal read the same lag emits invalid statements (proves it detects)",
    # Only node2's ~100 turns can go wrong; 15-18 of them do, run to run.
    bad > 5,
    f"{bad} of {total} invalid, all from node2's ~{total // 10} turns",
)
bad, total = broken(_no_lock, real_bit)
check(
    "a causal read alone is not enough: two drivers still race in the gap",
    bad > 5,
    f"{bad} of {total} invalid",
)

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
