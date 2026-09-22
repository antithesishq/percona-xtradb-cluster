"""The data-operation vocabulary.

Every operation here is fault-tolerant by construction: it records its intent
in the journal, issues SQL, classifies the outcome into the three-state
protocol, and returns. None of them raise. That is the point -- a driver
command runs under active fault injection, where a dropped connection or a
partitioned node is an expected condition rather than a bug, so bailing on the
first failure would exit non-zero for something that is not a finding.

The goal is to have some chance of producing any legal sequence of operations,
so nothing here rules out a shape on the grounds that it is unlikely to be
interesting.
"""

from __future__ import annotations


from . import config, db, journal, oracles, rnd, schema

SCHEMA = config.SCHEMA


class Session:
    """One connection plus the session posture the timeline drew for it."""

    def __init__(self, name: str, host: str, profile: dict) -> None:
        self.name = name
        self.host = host
        self.profile = profile
        self.conn = None
        self.demoted = False

    def ensure(self) -> bool:
        """Connect if needed and apply session posture. False if unreachable."""
        if self.conn is not None and db.is_alive(self.conn):
            return True
        db.close_quietly(self.conn)
        self.conn = db.connect_with_retry(self.host, SCHEMA)
        if self.conn is None:
            return False
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SET SESSION TRANSACTION ISOLATION LEVEL "
                    + str(self.profile.get("isolation", "REPEATABLE READ"))
                )
                cur.execute(
                    "SET SESSION wsrep_sync_wait = %s",
                    (int(self.profile.get("sync_wait_level", 0)),),
                )
                frag = int(self.profile.get("sr_fragment_size", 0))
                if frag > 0:
                    cur.execute(
                        "SET SESSION wsrep_trx_fragment_unit = %s",
                        (str(self.profile.get("sr_fragment_unit", "bytes")),),
                    )
                    cur.execute("SET SESSION wsrep_trx_fragment_size = %s", (frag,))
            return True
        except Exception:  # noqa: BLE001 - posture is best-effort under faults
            return self.conn is not None

    def close(self) -> None:
        db.close_quietly(self.conn)
        self.conn = None


def _rollback_quietly(conn) -> None:
    """Discard any open transaction.

    Load-bearing, not hygiene. MySQL rolls back only the failing STATEMENT on a
    lock-wait timeout or certification conflict; the transaction stays open. A
    subsequent BEGIN or any DDL on that session implicitly commits whatever had
    already landed -- so a write the journal called FAILED would actually be
    there, and the no-trace assertion would fire on a correct cluster. Rolling
    back here is what makes the FAILED classification true.
    """
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 - a dead connection needs no rollback
        pass


def _finish(jr, wids: list[int], shape: str, exc, conn) -> str:
    """Resolve a transaction's journal rows and record the reach claims."""
    if exc is not None:
        _rollback_quietly(conn)
    state, errno, msg = journal.classify(exc, conn)
    jr.resolve(wids, state, errno=errno, errmsg=msg)
    jr.tally(shape, errno)

    if state == "UNKNOWN":
        oracles.saw_unknown_outcome({"shape": shape, "errno": errno, "error": msg})
    if errno == db.ER_LOCK_DEADLOCK:
        oracles.saw_certification_conflict({"shape": shape, "error": msg})
    if errno == db.ER_UNKNOWN_COM_ERROR and "WSREP has not yet prepared" in (msg or ""):
        oracles.saw_not_ready_rejection({"shape": shape, "error": msg})
    return state


# ==========================================================================
# Class A: writes -- the ack journal's backbone
# ==========================================================================


def _witness_full(jr) -> bool:
    """Whether the witness table has hit its per-timeline cap.

    It is write-once and nothing prunes it, the containers have no volumes, and
    the terminal oracle hashes every row on three nodes -- so unbounded growth
    costs node disk and can push the checksum pass past its budget. Counted
    from the journal rather than with SELECT COUNT(*) so this costs nothing and
    works while the cluster is partitioned.
    """
    return jr.attempted_witness_rows() >= config.WITNESS_ROW_CAP


def insert_witness(jr, s: Session, profile: dict) -> None:
    if _witness_full(jr):
        return
    wid = jr.new_wid()
    jr.attempt([wid], target="witness", node=s.name, shape="insert_witness")
    exc = None
    try:
        with s.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `wl_witness` (wid, writer, node, seq, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                (wid, f"inv{jr.inv_id}", s.name, wid & 0xFFFFFF, schema.payload_for(wid)),
            )
    except Exception as e:  # noqa: BLE001
        exc = e
    _finish(jr, [wid], "insert_witness", exc, s.conn)


def insert_multirow(jr, s: Session, profile: dict) -> None:
    if _witness_full(jr):
        return
    # Menu axis: row counts at the boundaries plus a configured-limit family,
    # rather than an arbitrary range.
    n = rnd.choice([1, 2, 10, 100, 500])
    wids = [jr.new_wid() for _ in range(n)]
    jr.attempt(wids, target="witness", node=s.name, shape="insert_multirow")
    exc = None
    try:
        # Explicit transaction, not autocommit: the journal resolves all of
        # these wids to a single state, so their fate has to be all-or-nothing.
        # Otherwise a partial failure would mark committed rows FAILED and the
        # no-trace assertion would fire on a healthy cluster.
        s.conn.begin()
        with s.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO `wl_witness` (wid, writer, node, seq, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    (w, f"inv{jr.inv_id}", s.name, w & 0xFFFFFF, schema.payload_for(w))
                    for w in wids
                ],
            )
        s.conn.commit()
    except Exception as e:  # noqa: BLE001
        exc = e
    _finish(jr, wids, "insert_multirow", exc, s.conn)


def txn_multi_statement(jr, s: Session, profile: dict) -> None:
    """A real multi-statement transaction, mixing witness inserts and updates.

    The statement count is drawn from a menu whose top end crosses 100, because
    a large transaction is a materially different writeset shape.
    """
    n = rnd.sample_menu(int(profile.get("txn_size", 8)), floor=1)
    n = max(1, min(n, 200))
    keyspace = max(1, int(profile.get("hot_keyspace", 32)))
    wids: list[int] = []
    incr_ids: list[int] = []
    exc = None
    committed_statements = 0

    try:
        s.conn.begin()
        with s.conn.cursor() as cur:
            for _ in range(n):
                if rnd.chance(0.5):
                    wid = jr.new_wid()
                    wids.append(wid)
                    jr.attempt([wid], target="witness", node=s.name, shape="txn_witness")
                    cur.execute(
                        "INSERT INTO `wl_witness` (wid, writer, node, seq, payload) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            wid,
                            f"inv{jr.inv_id}",
                            s.name,
                            wid & 0xFFFFFF,
                            schema.payload_for(wid),
                        ),
                    )
                else:
                    hid = rnd.randint(0, keyspace - 1)
                    cur.execute("UPDATE `wl_hot` SET v = v + 1 WHERE hid = %s", (hid,))
                    incr_ids.append(jr.record_incr(hid, 1, "ATTEMPTED"))
                committed_statements += 1
        if rnd.chance(0.1):
            # Explicit rollback is a first-class shape, not a failure.
            s.conn.rollback()
            jr.resolve(wids, "FAILED", errno=None, errmsg="client rollback")
            jr.resolve_incrs(incr_ids, "FAILED")
            jr.tally("txn_rollback", None)
            return
        s.conn.commit()
    except Exception as e:  # noqa: BLE001
        exc = e

    state = _finish(jr, wids, "txn_multi_statement", exc, s.conn)
    jr.resolve_incrs(incr_ids, state)
    if state == "ACKED" and committed_statements >= 100:
        oracles.saw_long_transaction(
            {"shape": "txn_multi_statement", "statements": committed_statements}
        )


def insert_autoinc(jr, s: Session, profile: dict) -> None:
    """Let the server assign identity, from whichever node we are on.

    innodb_autoinc_lock_mode=2 is configured, and wsrep assigns per-node
    offset/increment, so concurrent inserts from three nodes is the shape where
    an identity collision would show up.
    """
    wid = jr.new_wid()
    jr.attempt([wid], target="autoinc", node=s.name, shape="insert_autoinc")
    exc = None
    try:
        with s.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `wl_autoinc` (node, wid) VALUES (%s, %s)", (s.name, wid)
            )
    except Exception as e:  # noqa: BLE001
        exc = e
    _finish(jr, [wid], "insert_autoinc", exc, s.conn)


# ==========================================================================
# Class B: conflict generators
# ==========================================================================


def update_hot_row(jr, s: Session, profile: dict) -> None:
    keyspace = max(1, int(profile.get("hot_keyspace", 32)))
    hid = rnd.randint(0, keyspace - 1)
    incr_id = jr.record_incr(hid, 1, "ATTEMPTED")
    exc = None
    try:
        with s.conn.cursor() as cur:
            cur.execute("UPDATE `wl_hot` SET v = v + 1 WHERE hid = %s", (hid,))
    except Exception as e:  # noqa: BLE001
        exc = e
    if exc is not None:
        _rollback_quietly(s.conn)
    state, errno, msg = journal.classify(exc, s.conn)
    jr.resolve_incrs([incr_id], state)
    jr.tally("update_hot_row", errno)
    if errno == db.ER_LOCK_DEADLOCK:
        oracles.saw_certification_conflict({"shape": "update_hot_row", "hid": hid})
    if state == "UNKNOWN":
        oracles.saw_unknown_outcome({"shape": "update_hot_row", "error": msg})


def delete_reinsert(jr, s: Session, profile: dict) -> None:
    """Exercise the delete-key extraction path, not just insert/update keys."""
    wid = jr.new_wid()
    jr.attempt([wid], target="witness", node=s.name, shape="delete_reinsert")
    exc = None
    try:
        # One transaction: an insert that lands followed by a delete that fails
        # would otherwise leave a row behind while the journal calls the write
        # failed.
        s.conn.begin()
        with s.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `wl_witness` (wid, writer, node, seq, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                (wid, f"inv{jr.inv_id}", s.name, wid & 0xFFFFFF, schema.payload_for(wid)),
            )
            cur.execute("DELETE FROM `wl_witness` WHERE wid = %s", (wid,))
            cur.execute(
                "INSERT INTO `wl_witness` (wid, writer, node, seq, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                (wid, f"inv{jr.inv_id}", s.name, wid & 0xFFFFFF, schema.payload_for(wid)),
            )
        s.conn.commit()
    except Exception as e:  # noqa: BLE001
        exc = e
    _finish(jr, [wid], "delete_reinsert", exc, s.conn)


def uk_churn(jr, s: Session, profile: dict) -> None:
    """Certification on a UNIQUE secondary index, plus duplicate-key errors."""
    wid = jr.new_wid()
    u = rnd.randint(0, 64)
    jr.attempt([wid], target="uk", node=s.name, shape="uk_churn")
    exc = None
    try:
        with s.conn.cursor() as cur:
            cur.execute("INSERT INTO `wl_uk` (id, u) VALUES (%s, %s)", (wid, u))
    except Exception as e:  # noqa: BLE001
        exc = e
    _finish(jr, [wid], "uk_churn", exc, s.conn)


def locking_read(jr, s: Session, profile: dict) -> None:
    """A locking read, sometimes followed by a write in the same transaction.

    The assertion is that this never ends fatally: a result set, or one of the
    documented lock errors. A 1213 from SKIP LOCKED is especially interesting,
    since native InnoDB SKIP LOCKED cannot deadlock -- so it marks the BF-wait
    conversion path specifically.
    """
    modifier = rnd.choice(["", "FOR UPDATE", "FOR UPDATE SKIP LOCKED", "FOR UPDATE NOWAIT", "FOR SHARE"])
    keyspace = max(1, int(profile.get("hot_keyspace", 32)))
    hid = rnd.randint(0, keyspace - 1)
    order = rnd.choice(["", "ORDER BY hid DESC"])
    sql = f"SELECT hid, v FROM `wl_hot` WHERE hid >= %s {order} LIMIT 10 {modifier}"

    exc = None
    got_rows = False
    try:
        s.conn.begin()
        with s.conn.cursor() as cur:
            cur.execute(sql, (hid,))
            rows = cur.fetchall()
            got_rows = True
            if rows and rnd.chance(0.4):
                cur.execute(
                    "UPDATE `wl_hot` SET tag = %s WHERE hid = %s",
                    (f"inv{jr.inv_id}", rows[0][0]),
                )
        s.conn.commit()
    except Exception as e:  # noqa: BLE001
        exc = e
        try:
            s.conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    errno = db.errno_of(exc) if exc is not None else None
    jr.tally(f"locking_read{'' if not modifier else ':' + modifier.split()[-1]}", errno)

    # Only judge the statement when the server answered and is still alive. A
    # connection lost to an injected fault says nothing about this property.
    alive = db.is_alive(s.conn)
    if exc is None or errno in db.LOCKING_READ_LEGAL or errno in db.CLEAN_REJECTIONS:
        legal = True
    elif not alive or errno in (db.CR_SERVER_GONE_ERROR, db.CR_SERVER_LOST):
        legal = True  # environment, not the statement
    elif errno in db.FATAL_STATEMENT_ERRNOS:
        legal = False
    else:
        # An errno this workload does not recognise means it cannot judge the
        # statement -- not that the statement was illegal. Only the explicitly
        # fatal set fails the property; everything else lands in the errno
        # tally, which is where an unexpected distribution actually shows up.
        legal = True

    oracles.locking_read_outcome_legal(
        legal,
        {
            "modifier": modifier or "none",
            "errno": errno,
            "error": str(exc)[:300] if exc else None,
            "returned_rows": got_rows,
            "connection_alive": alive,
        },
    )

    if errno == db.ER_LOCK_DEADLOCK and "SKIP LOCKED" in modifier:
        oracles.saw_skip_locked_conflict({"hid": hid, "modifier": modifier})


def fk_cascade_dml(jr, s: Session, profile: dict) -> None:
    """DML on an FK parent with cascading children.

    This is what makes wsrep_append_foreign_key run, putting parent keys in the
    writeset. A gap in FK key extraction is an undetectable-divergence path, so
    the traffic has to include it for the terminal oracle to have a chance.
    """
    pid = rnd.randint(0, config.FK_PARENT_ROWS - 1)

    # Only the child-insert branch mints a write key. The parent-update and
    # cascade-delete branches create no keyed row, and journalling a key for
    # them just padded the ack counts that triage reads.
    if not rnd.chance(0.6):
        try:
            with s.conn.cursor() as cur:
                if rnd.chance(0.5):
                    cur.execute(
                        "UPDATE `wl_fk_parent` SET pv = pv + 1 WHERE pid = %s", (pid,)
                    )
                    jr.tally("fk_parent_update", None)
                else:
                    cur.execute(
                        "DELETE FROM `wl_fk_child` WHERE pid = %s LIMIT 5", (pid,)
                    )
                    jr.tally("fk_cascade_delete", None)
        except Exception as e:  # noqa: BLE001
            _rollback_quietly(s.conn)
            jr.tally("fk_cascade_dml", db.errno_of(e))
        return

    wid = jr.new_wid()
    jr.attempt([wid], target="fkchild", node=s.name, shape="fk_cascade_dml")
    exc = None
    try:
        with s.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `wl_fk_child` (cid, pid, cv) VALUES (%s, %s, %s)",
                (wid, pid, wid & 0xFFFF),
            )
    except Exception as e:  # noqa: BLE001
        exc = e
    _finish(jr, [wid], "fk_cascade_dml", exc, s.conn)


# ==========================================================================
# Class C: bulk -- gcache page-store pressure
# ==========================================================================


def bulk_write(jr, s: Session, profile: dict) -> None:
    """Large writesets, bounded well under gcache.size.

    Capped at half the 16M gcache: a writeset larger than the gcache forces a
    full SST on every subsequent rejoin, which would distort every state
    transfer property in the run.
    """
    size = min(int(profile.get("bulk_bytes", 65536)), config.BULK_MAX_BYTES)
    bid = rnd.randint(0, config.BULK_RING_SIZE - 1)
    wid = jr.new_wid()
    jr.attempt([wid], target="bulk", node=s.name, shape="bulk_write")
    exc = None
    try:
        with s.conn.cursor() as cur:
            cur.execute(
                "REPLACE INTO `wl_bulk` (bid, gen, body) VALUES (%s, %s, %s)",
                (bid, wid, b"\xa5" * size),
            )
    except Exception as e:  # noqa: BLE001
        exc = e
    state = _finish(jr, [wid], "bulk_write", exc, s.conn)
    if state == "ACKED" and size > 4 * 1024 * 1024:
        oracles.saw_large_writeset({"shape": "bulk_write", "bytes": size})


# ==========================================================================
# Class D: streaming replication
# ==========================================================================


def sr_session(jr, s: Session, profile: dict) -> None:
    """A transaction that replicates in fragments before it commits.

    Fragment settings are session-scoped, so this needs no config variant. The
    rollback arm matters as much as the commit arm: a spurious rollback
    fragment must be a no-op cluster-wide.
    """
    frag = int(profile.get("sr_fragment_size", 0))
    if frag <= 0:
        frag = rnd.choice([1, 16, 512])
    unit = str(profile.get("sr_fragment_unit", "bytes"))

    n = rnd.choice([2, 8, 40])
    wids = [jr.new_wid() for _ in range(n)]
    jr.attempt(wids, target="witness", node=s.name, shape="sr_session")
    exc = None
    fragments_seen = 0
    rolled_back = False

    try:
        with s.conn.cursor() as cur:
            cur.execute("SET SESSION wsrep_trx_fragment_unit = %s", (unit,))
            cur.execute("SET SESSION wsrep_trx_fragment_size = %s", (frag,))
        s.conn.begin()
        with s.conn.cursor() as cur:
            for w in wids:
                cur.execute(
                    "INSERT INTO `wl_witness` (wid, writer, node, seq, payload) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (w, f"inv{jr.inv_id}", s.name, w & 0xFFFFFF, schema.payload_for(w)),
                )
            # Observing the fragment log from inside the transaction is what
            # proves fragments were really produced, not just requested.
            try:
                cur.execute("SELECT COUNT(*) FROM `mysql`.`wsrep_streaming_log`")
                fragments_seen = int(cur.fetchone()[0])
            except Exception:  # noqa: BLE001 - visibility is best-effort
                fragments_seen = 0

        if rnd.chance(0.33):
            s.conn.rollback()
            rolled_back = True
        else:
            s.conn.commit()
    except Exception as e:  # noqa: BLE001
        exc = e

    if rolled_back and exc is None:
        jr.resolve(wids, "FAILED", errmsg="streaming transaction rolled back by client")
        jr.tally("sr_rollback", None)
    else:
        state = _finish(jr, wids, "sr_session", exc, s.conn)
        if state == "ACKED" and fragments_seen > 0:
            oracles.saw_streaming_transaction(
                {"fragment_size": frag, "fragment_unit": unit, "log_rows": fragments_seen}
            )
    try:
        with s.conn.cursor() as cur:
            cur.execute("SET SESSION wsrep_trx_fragment_size = 0")
    except Exception:  # noqa: BLE001
        pass


# ==========================================================================
# Class G: reads -- causality
# ==========================================================================


def sync_wait_read(jr, s: Session, profile: dict) -> None:
    """Read an acknowledged write back from a DIFFERENT node.

    Under wsrep_sync_wait != 0 the row must be visible. Under 0 its absence is
    legal -- that is the shipped contract, not a bug -- so no assertion is made
    in that case. The strong cross-node claim lives in the terminal oracle,
    where it can be made without racing the replication stream.
    """
    row = jr.conn.execute(
        "SELECT wid FROM ack WHERE state = 'ACKED' AND target = 'witness' "
        "ORDER BY resolved_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return
    wid = int(row["wid"])
    level = rnd.choice([1, 3, 7])

    others = [n for n, _ in config.NODES if n != s.name]
    if not others:
        return
    name = rnd.choice(others)
    conn = db.connect_with_retry(config.NODE_HOSTS[name], SCHEMA, attempts=1)
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION wsrep_sync_wait = %s", (level,))
            cur.execute("SELECT wid FROM `wl_witness` WHERE wid = %s", (wid,))
            found = cur.fetchone() is not None
        jr.tally("sync_wait_read", 0 if found else -2)
        # level is never 0 here, so absence is not legal: the write was
        # acknowledged before this read started, and wsrep_sync_wait makes the
        # read wait for the node to catch up.
        oracles.sync_wait_read_sees_acked_write(
            found,
            {
                "wid": wid,
                "written_on": s.name,
                "read_on": name,
                "sync_wait_level": level,
            },
        )
    except Exception as e:  # noqa: BLE001
        # A read that errored says nothing about causality.
        jr.tally("sync_wait_read", db.errno_of(e))
    finally:
        db.close_quietly(conn)


def hot_row_read(jr, s: Session, profile: dict) -> None:
    try:
        with s.conn.cursor() as cur:
            cur.execute("SELECT SUM(v) FROM `wl_hot`")
            cur.fetchall()
        jr.tally("hot_row_read", None)
    except Exception as e:  # noqa: BLE001
        jr.tally("hot_row_read", db.errno_of(e))


# ==========================================================================
# Dispatch
# ==========================================================================

CLASS_OPS: dict[str, list] = {
    "write": [insert_witness, insert_multirow, txn_multi_statement, insert_autoinc],
    "conflict": [update_hot_row, delete_reinsert, uk_churn, locking_read, fk_cascade_dml],
    "bulk": [bulk_write],
    "sr": [sr_session],
    "read": [sync_wait_read, hot_row_read],
}


def run_one(jr, s: Session, profile: dict, cls: str) -> None:
    """Run a single operation of the given class. Never raises."""
    choices = CLASS_OPS.get(cls)
    if not choices:
        return
    op = rnd.choice(choices)
    try:
        op(jr, s, profile)
    except Exception as e:  # noqa: BLE001 - an op must never take the driver down
        jr.tally(f"{cls}:uncaught", db.errno_of(e))
