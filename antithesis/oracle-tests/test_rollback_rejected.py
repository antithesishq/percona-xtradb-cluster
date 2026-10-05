"""Detection test: a ROLLBACK that a non-Primary node rejects must not leak a transaction.

Why: on a non-Primary node, PXC rejects ROLLBACK with ER 1047, the same as
every other data command (sql/sql_parse.cc:3790). The transaction stays open.
The old ops._rollback_quietly ignored that error, so journal.classify called
the write FAILED, and the next BEGIN on the session committed it once the
node was Primary again. Runs 7a6819d0-63-2 and a52c84b4-63-5 fired "[prod] a
cleanly failed write is absent from every Synced node" that way.

The fake connection below models only the server rules this depends on:
- a non-Primary node rejects INSERT, COMMIT, ROLLBACK and BEGIN with 1047,
  and allows SELECT 1 (a table-less SELECT);
- a rejected statement inside a transaction leaves the transaction open;
- BEGIN commits a transaction that is already open (implicit commit);
- a disconnect discards an uncommitted transaction.

Checked:
1. txn_multi_statement on a node that goes non-Primary mid-transaction:
   FAILED in the journal, the connection is closed, and no row ever commits,
   even after the node is Primary again and the session starts new work.
2. The coverage claim fires for that case, with the shape.
3. An autocommit write rejected with 1047 on a session with no open
   transaction: still FAILED, connection closed, no coverage claim.
4. A ROLLBACK that fails because the connection is gone: no coverage claim.

Detection: case 1 is replayed with the old swallow-the-error rollback, and
the test must see the "failed" rows commit. Otherwise a green result would not
tell a working fix from a fake connection that never leaks.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs
helper_stubs.install()

import pymysql
from antithesis import assertions as A
from pxcwl import db, ops, rnd

COVERAGE = "[coverage] a non-Primary node rejected ROLLBACK of an open transaction"
NOT_READY = (1047, "WSREP has not yet prepared node for application use")


class Server:
    """Committed rows, shared by every connection to the one fake node."""

    def __init__(self):
        self.committed: set[int] = set()
        self.non_primary = False


class FakeConn:
    def __init__(self, server, fail_after_inserts=None):
        self.server = server
        self.host = "10.20.20.11"
        self.server_status = 0x0002  # SERVER_STATUS_AUTOCOMMIT, no transaction
        self.pending: list[int] = []
        self.in_trans = False
        self.closed = False
        self.inserts = 0
        # Make the node non-Primary after this many successful INSERTs.
        self.fail_after_inserts = fail_after_inserts

    def _check(self, gone_errno=None):
        if self.closed:
            raise pymysql.OperationalError(gone_errno or 2006, "MySQL server has gone away")
        if self.server.non_primary:
            raise pymysql.OperationalError(*NOT_READY)

    def _status(self):
        self.server_status = 0x0002 | (0x0001 if self.in_trans else 0)

    def begin(self):
        self._check()
        if self.in_trans:  # implicit commit, as MySQL does
            self.server.committed.update(self.pending)
            self.pending = []
        self.in_trans = True
        self._status()

    def commit(self):
        self._check()
        self.server.committed.update(self.pending)
        self.pending, self.in_trans = [], False
        self._status()

    def rollback(self):
        self._check()
        self.pending, self.in_trans = [], False
        self._status()

    def close(self):
        # The server discards an uncommitted transaction on disconnect.
        self.closed = True
        self.pending, self.in_trans = [], False

    def cursor(self, *a, **k):
        conn = self

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *e): return False
            def fetchall(self): return [(1,)]

            def execute(self, sql, params=None):
                if conn.closed:
                    raise pymysql.OperationalError(2006, "MySQL server has gone away")
                if sql.strip() == "SELECT 1":
                    return 1
                if conn.server.non_primary:
                    raise pymysql.OperationalError(*NOT_READY)
                if sql.startswith("INSERT INTO `wl_witness`"):
                    if conn.fail_after_inserts is not None and conn.inserts >= conn.fail_after_inserts:
                        conn.server.non_primary = True
                        raise pymysql.OperationalError(*NOT_READY)
                    conn.inserts += 1
                    wid = params[0]
                    if conn.in_trans:
                        conn.pending.append(wid)
                    else:
                        conn.server.committed.add(wid)
                    conn._status()
                    return 1
                raise AssertionError(f"unexpected SQL: {sql}")

        return Cur()


class Journal:
    inv_id = 7

    def __init__(self):
        self.seq = 0
        self.state: dict[int, str] = {}

    def new_wid(self):
        self.seq += 1
        return (self.inv_id << 24) | self.seq

    def attempt(self, wids, **k):
        for w in wids:
            self.state[w] = "ATTEMPTED"

    def resolve(self, wids, state, **k):
        for w in wids:
            self.state[w] = state

    def tally(self, *a, **k): pass
    def attempted_witness_rows(self): return 0
    def record_incr(self, *a, **k): return 0
    def resolve_incrs(self, *a, **k): pass


class Session:
    name, host = "node1", "10.20.20.11"

    def __init__(self, conn):
        self.conn = conn


# Three statements, all witness inserts, no client-rollback arm.
rnd.sample_menu = lambda *a, **k: 3
rnd.chance = lambda p: p == 0.5

fails = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def leak_scenario():
    """Node goes non-Primary at the third insert; later it heals and the
    session starts a new transaction, on the same connection if it is still
    open (what Session.ensure() would reuse)."""
    A.FIRED.clear()
    server = Server()
    conn = FakeConn(server, fail_after_inserts=2)
    s, jr = Session(conn), Journal()
    ops.txn_multi_statement(jr, s, {"txn_size": 3, "hot_keyspace": 4})
    failed = [w for w, st in jr.state.items() if st == "FAILED"]
    server.non_primary = False
    if not conn.closed:
        conn.begin()  # the next operation's BEGIN on the reused session
        conn.commit()
    return jr, conn, server, failed


# 1 and 2. The fix.
jr, conn, server, failed = leak_scenario()
check("the interrupted transaction is journaled FAILED", len(failed) == 3)
check("the connection is closed after the rejected ROLLBACK", conn.closed)
check("no FAILED row ever commits", not (set(failed) & server.committed))
cov = [f for f in A.FIRED if f["message"] == COVERAGE]
check("the coverage claim fires once", len(cov) == 1)
check("the coverage details name the shape", cov and cov[0]["details"]["shape"] == "txn_multi_statement")

# 3. Autocommit write rejected on a clean session.
A.FIRED.clear()
server = Server()
server.non_primary = True
conn = FakeConn(server)
jr = Journal()
ops.insert_witness(jr, Session(conn), {})
check("an autocommit 1047 is still FAILED", list(jr.state.values()) == ["FAILED"])
check("an autocommit 1047 closes the connection", conn.closed)
check("an autocommit 1047 does not claim coverage",
      not [f for f in A.FIRED if f["message"] == COVERAGE])

# 4. ROLLBACK fails because the connection is gone.
A.FIRED.clear()
conn = FakeConn(Server())
conn.in_trans, conn.server_status = True, 0x0003
conn.closed = True
ops._rollback_or_close(conn, "txn_multi_statement")
check("a rollback on a dead connection does not claim coverage",
      not [f for f in A.FIRED if f["message"] == COVERAGE])

# Detection: the old rollback, which swallowed the error and kept the session.
def old_rollback(conn, shape):
    try:
        conn.rollback()
    except Exception:
        pass

fixed = ops._rollback_or_close
ops._rollback_or_close = old_rollback
try:
    jr, conn, server, failed = leak_scenario()
finally:
    ops._rollback_or_close = fixed
# The two inserts before the rejection commit; the rejected third never landed.
check("detection: the old rollback lets the FAILED rows commit",
      len(failed) == 3 and len(set(failed) & server.committed) == 2)

print()
print("FAILED: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
