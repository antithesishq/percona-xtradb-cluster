"""Cross-invocation state: the ack journal and its three-state protocol.

Each test command invocation is a separate, bounded process, so anything that
must outlive one invocation lives here. SQLite rather than plain files because
concurrent ``parallel_driver_`` invocations write this at the same time and
need real locking and transactions.

The central problem this solves: under fault injection the workload cannot know
whether an in-flight COMMIT landed. Asserting an exact expected value would
fire legitimately every time the environment dropped a request, drowning real
bugs. So every write is recorded as ATTEMPTED before it is issued, and resolved
afterwards into exactly one of:

    ACKED    the server said it committed
    FAILED   a clean rejection, node still alive -> provably left no trace
    UNKNOWN  the connection died, or liveness failed -> outcome unknowable

Those three states give the terminal oracle a band to check instead of a point
value. See ``checks.reconcile`` for the arithmetic.
"""

from __future__ import annotations

import os
import sqlite3
import time

from . import config, db

# A write key is (inv_id << WID_SEQ_BITS) | seq. inv_id comes from SQLite's
# AUTOINCREMENT, seq from a per-invocation counter -- so keys are unique
# without uuid4() or wall-clock input, both of which would break replay
# determinism.
WID_SEQ_BITS = 24
WID_SEQ_MAX = (1 << WID_SEQ_BITS) - 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS swarm (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invocation (
  inv_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT    NOT NULL,
  pid        INTEGER NOT NULL,
  started_at REAL    NOT NULL,
  ended_at   REAL
);

-- The ack journal proper: one row per attempted unique-keyed write.
CREATE TABLE IF NOT EXISTS ack (
  wid          INTEGER PRIMARY KEY,
  target       TEXT    NOT NULL,
  inv_id       INTEGER NOT NULL,
  node         TEXT    NOT NULL,
  shape        TEXT    NOT NULL,
  state        TEXT    NOT NULL,
  errno        INTEGER,
  errmsg       TEXT,
  attempted_at REAL    NOT NULL,
  resolved_at  REAL
);
CREATE INDEX IF NOT EXISTS ack_state ON ack(state);

-- Counter accounting. Counters get bounds, never exact equality.
CREATE TABLE IF NOT EXISTS incr (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  hid    INTEGER NOT NULL,
  inv_id INTEGER NOT NULL,
  delta  INTEGER NOT NULL,
  state  TEXT    NOT NULL,
  at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS incr_state ON incr(state);

-- Global lever leases, with deadlines so a killed driver cannot leak one.
CREATE TABLE IF NOT EXISTS lease (
  lever       TEXT PRIMARY KEY,
  node        TEXT    NOT NULL,
  inv_id      INTEGER NOT NULL,
  intent      TEXT    NOT NULL,
  restore     TEXT    NOT NULL,
  acquired_at REAL    NOT NULL,
  deadline    REAL    NOT NULL
);

-- The availability gate: at most one cluster-disrupting lever at a time.
CREATE TABLE IF NOT EXISTS disruption_token (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  lever    TEXT    NOT NULL,
  node     TEXT    NOT NULL,
  inv_id   INTEGER NOT NULL,
  deadline REAL    NOT NULL
);

-- Per-node progress ledger, so windowed checks can span invocations.
CREATE TABLE IF NOT EXISTS progress (
  node                 TEXT PRIMARY KEY,
  last_committed       INTEGER NOT NULL,
  last_advance_at      REAL    NOT NULL,
  advertised_since     REAL,
  last_probe_commit_at REAL,
  probe_failed_since   REAL,
  wedge_since          REAL,
  advertised_fc_paused INTEGER
);

-- DDL episodes, so "no unresolved DDL after reconvergence" is decidable.
CREATE TABLE IF NOT EXISTS ddl (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  inv_id     INTEGER NOT NULL,
  node       TEXT    NOT NULL,
  stmt       TEXT    NOT NULL,
  started_at REAL    NOT NULL,
  ended_at   REAL,
  state      TEXT    NOT NULL,
  errno      INTEGER
);

-- shape x errno tally. Data for triage and for reach claims, not an assertion.
CREATE TABLE IF NOT EXISTS outcome (
  shape TEXT,
  errno INTEGER,
  n     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (shape, errno)
);

-- Facts observed once per timeline that later commands need (e.g. fc_limit).
CREATE TABLE IF NOT EXISTS observed (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""


class Journal:
    """Handle on the SQLite journal. One per command invocation."""

    def __init__(self, kind: str) -> None:
        os.makedirs(config.JOURNAL_DIR, exist_ok=True)
        self.conn = sqlite3.connect(config.JOURNAL_PATH, timeout=30.0)  # noqa: E501
        self.conn.row_factory = sqlite3.Row
        # WAL is exactly our shape: one writer, many readers, many processes.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        # Absorb writer contention across concurrent driver invocations rather
        # than surfacing it as "database is locked". Matches the 30s connect
        # timeout above rather than silently lowering it.
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.executescript(SCHEMA_SQL)
        self._add_missing_columns()
        self.conn.commit()

        cur = self.conn.execute(
            "INSERT INTO invocation (kind, pid, started_at) VALUES (?, ?, ?)",
            (kind, os.getpid(), time.time()),
        )
        self.inv_id = int(cur.lastrowid)
        self.conn.commit()
        self._seq = 0

    # CREATE TABLE IF NOT EXISTS silently keeps an older table shape, so a
    # journal file left over from a previous build would be missing columns
    # added since -- and every write against it would fail inside a broad
    # except and vanish. Cheap enough to just reconcile on open.
    ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("progress", "probe_failed_since", "REAL"),
    )

    def _add_missing_columns(self) -> None:
        for table, column, decl in self.ADDED_COLUMNS:
            have = {
                r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in have:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )

    # ---------------------------------------------------------------- lifecycle

    def finish(self) -> None:
        try:
            self.conn.execute(
                "UPDATE invocation SET ended_at = ? WHERE inv_id = ?",
                (time.time(), self.inv_id),
            )
            self.conn.commit()
        finally:
            self.conn.close()

    # ------------------------------------------------------------------- swarm

    def put_swarm(self, values: dict[str, object]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO swarm (k, v) VALUES (?, ?)",
                [(k, repr(v)) for k, v in values.items()],
            )

    def get_swarm(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for row in self.conn.execute("SELECT k, v FROM swarm"):
            try:
                out[row["k"]] = eval(row["v"], {"__builtins__": {}}, {})  # noqa: S307
            except Exception:  # noqa: BLE001 - fall back to the raw string
                out[row["k"]] = row["v"]
        return out

    def put_observed(self, key: str, value: object) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO observed (k, v) VALUES (?, ?)",
                (key, str(value)),
            )

    def get_observed(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT v FROM observed WHERE k = ?", (key,)
        ).fetchone()
        return row["v"] if row else default

    # --------------------------------------------------------------- write keys

    def new_wid(self) -> int:
        """Mint a unique write key.

        Deterministic under replay: derived from the SQLite invocation counter
        and a local sequence, never from wall time or a random UUID.
        """
        self._seq += 1
        if self._seq > WID_SEQ_MAX:
            raise RuntimeError("invocation exhausted its write-key space")
        return (self.inv_id << WID_SEQ_BITS) | self._seq

    # ----------------------------------------------------------- ack protocol

    def attempt(self, wids: list[int], *, target: str, node: str, shape: str) -> None:
        """Record intent BEFORE the write is issued.

        The ordering is the whole point. If this process is killed between here
        and ``resolve``, the rows read ATTEMPTED and the reconciler folds them
        into the unknown bucket -- which is correct, because the outcome truly
        is unknown. Were the journal written after the write instead, a killed
        driver would leave committed rows with no journal record at all, and the
        reconciler would call them phantom writes: a guaranteed false positive.
        """
        now = time.time()
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO ack "
                "(wid, target, inv_id, node, shape, state, attempted_at) "
                "VALUES (?, ?, ?, ?, ?, 'ATTEMPTED', ?)",
                [(w, target, self.inv_id, node, shape, now) for w in wids],
            )

    def resolve(
        self,
        wids: list[int],
        state: str,
        *,
        errno: int | None = None,
        errmsg: str | None = None,
    ) -> None:
        """Resolve every write of one transaction atomically.

        One SQLite transaction for the whole set, so a multi-row insert can
        never be left half-ACKED.
        """
        if not wids:
            return
        now = time.time()
        with self.conn:
            self.conn.executemany(
                "UPDATE ack SET state = ?, errno = ?, errmsg = ?, resolved_at = ? "
                "WHERE wid = ?",
                [(state, errno, errmsg, now, w) for w in wids],
            )

    def record_incr(self, hid: int, delta: int, state: str) -> int:
        """Record an attempted counter increment. Returns its row id.

        The caller must keep the id and resolve it: resolving "the most recent
        increment" would leave every earlier increment of a multi-statement
        transaction stuck in ATTEMPTED, which inflates the ceiling and makes
        the bound uninformative.
        """
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO incr (hid, inv_id, delta, state, at) VALUES (?, ?, ?, ?, ?)",
                (hid, self.inv_id, delta, state, time.time()),
            )
        return int(cur.lastrowid)

    def resolve_incrs(self, ids: list[int], state: str) -> None:
        """Resolve a transaction's increments atomically, all to one state."""
        if not ids:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE incr SET state = ? WHERE id = ?", [(state, i) for i in ids]
            )

    def tally(self, shape: str, errno: int | None) -> None:
        key_errno = -1 if errno is None else errno
        with self.conn:
            self.conn.execute(
                "INSERT INTO outcome (shape, errno, n) VALUES (?, ?, 1) "
                "ON CONFLICT(shape, errno) DO UPDATE SET n = n + 1",
                (shape, key_errno),
            )

    # ---------------------------------------------------------------- reporting

    def concurrent_drivers(self) -> int:
        """Other traffic invocations running right now.

        Used to substantiate the "under concurrent writes" reach claim rather
        than asserting it on faith.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM invocation "
            "WHERE kind = 'traffic' AND ended_at IS NULL AND inv_id != ?",
            (self.inv_id,),
        ).fetchone()
        return int(row["c"])

    def attempted_witness_rows(self) -> int:
        """How many witness rows this timeline has attempted, cached per call.

        Cheap: the ack table is local and indexed, and the caller only needs a
        bound, not an exact live count.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM ack WHERE target = 'witness'"
        ).fetchone()
        return int(row["c"])

    def ack_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) c FROM ack GROUP BY state")
        return {r["state"]: r["c"] for r in rows}

    def incr_bounds(self) -> tuple[int, int]:
        """(floor, ceiling) for the sum of all counter increments."""
        row = self.conn.execute(
            "SELECT "
            " COALESCE(SUM(CASE WHEN state = 'ACKED' THEN delta ELSE 0 END), 0) acked, "
            " COALESCE(SUM(CASE WHEN state IN ('ATTEMPTED','UNKNOWN') THEN delta ELSE 0 END), 0) unk "
            "FROM incr"
        ).fetchone()
        return int(row["acked"]), int(row["acked"]) + int(row["unk"])

    def outcome_tally(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT shape, errno, n FROM outcome ORDER BY n DESC LIMIT 40"
        )
        return {f"{r['shape']}/{r['errno']}": r["n"] for r in rows}

    def unresolved_ddl(self) -> list[dict[str, object]]:
        """DDL episodes that are genuinely unresolved.

        An episode left ATTEMPTED by a driver that was KILLED (which is what
        eventually_ does to every running command) is unknowable, not
        unresolved -- the same treatment the ack journal gives a killed write.
        Only an episode whose owning invocation finished normally and still
        left the episode open indicates a TOI operation that never reached a
        terminal outcome.
        """
        rows = self.conn.execute(
            "SELECT d.node, d.stmt, d.started_at FROM ddl d "
            "JOIN invocation i ON i.inv_id = d.inv_id "
            "WHERE d.state = 'ATTEMPTED' AND i.ended_at IS NOT NULL"
        )
        return [dict(r) for r in rows]

    def abandoned_ddl(self) -> int:
        """Episodes left open by a killed invocation. Reported, never asserted."""
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM ddl d "
            "JOIN invocation i ON i.inv_id = d.inv_id "
            "WHERE d.state = 'ATTEMPTED' AND i.ended_at IS NULL"
        ).fetchone()
        return int(row["c"])

    def ddl_completed(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM ddl WHERE state = 'DONE'"
        ).fetchone()
        return int(row["c"])

    # -------------------------------------------------------------------- ddl

    def ddl_start(self, node: str, stmt: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO ddl (inv_id, node, stmt, started_at, state) "
                "VALUES (?, ?, ?, ?, 'ATTEMPTED')",
                (self.inv_id, node, stmt, time.time()),
            )
        return int(cur.lastrowid)

    def ddl_end(self, ddl_id: int, state: str, errno: int | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE ddl SET ended_at = ?, state = ?, errno = ? WHERE id = ?",
                (time.time(), state, errno, ddl_id),
            )


def classify(exc: BaseException | None, conn) -> tuple[str, int | None, str | None]:
    """Decide which of the three outcome states a finished write is in.

    The liveness probe is the discriminator, and it is what licenses treating a
    deadlock/certification error as provably-no-trace: every clean rejection
    leaves the session usable, whereas a node dying mid-COMMIT does not. So a
    1213 arriving on a live connection is FAILED (galera made the writeset a
    dummy on every node), while the same transaction on a node that just died
    presents as a lost connection and lands in UNKNOWN instead. No timing logic
    is needed; the probe does the work.
    """
    if exc is None:
        return "ACKED", None, None

    errno = db.errno_of(exc)
    msg = str(exc)[:500]

    if errno in (db.CR_SERVER_GONE_ERROR, db.CR_SERVER_LOST):
        return "UNKNOWN", errno, msg
    if isinstance(exc, (OSError, TimeoutError)):
        return "UNKNOWN", errno, msg
    if errno in db.CLEAN_REJECTIONS and db.is_alive(conn):
        return "FAILED", errno, msg
    # An unrecognised server error: the connection survived, but nothing here
    # proves the transaction left no trace, and FAILED would assert exactly
    # that. UNKNOWN is the sound default; the errno tally is what surfaces the
    # distribution for triage.
    return "UNKNOWN", errno, msg
