"""Leases for global server levers, with automatic repair.

Why this exists at all: an ``eventually_`` command kills every running command
when it starts. A driver killed while holding ``gmcast.isolate=1``,
``wsrep_desync=ON``, a non-default ``pc.weight``, ``pxc_strict_mode=PERMISSIVE``
or a squeezed ``wsrep_max_ws_size`` leaves the cluster permanently impaired --
and then the convergence assertions fail for a reason that has nothing to do
with PXC. That is a guaranteed false positive, and it would waste whole runs.

So a lever is never simply set. It is leased, with a wall-clock deadline and
the value to restore, and every command repairs expired leases before doing
anything else.

The ``DISRUPTIVE`` levers additionally share a single cluster-wide token, so at
most one availability-affecting change is ever in flight. Without that, two
drivers could isolate two different nodes at once and manufacture the quorum
loss that several liveness properties are supposed to be detecting.
"""

from __future__ import annotations

import sqlite3
import time

from . import config, db

# Levers that change cluster availability. At most one at a time, cluster-wide,
# and only when all nodes are currently Synced.
DISRUPTIVE: frozenset[str] = frozenset(
    {
        "gmcast_isolate",
        "graceful_shutdown",
        "cluster_address_reset",
        "pc_weight",
        "desync_cycle",
        "strict_mode_window",
        "ws_size_squeeze",
    }
)

# How a lever's restore value is applied. Each entry maps a lever to the SQL
# templates used to put the server back, applied in order.
RESTORE_SQL: dict[str, tuple[str, ...]] = {
    "gmcast_isolate": ("SET GLOBAL wsrep_provider_options = 'gmcast.isolate={value}'",),
    "pc_weight": ("SET GLOBAL wsrep_provider_options = 'pc.weight={value}'",),
    "desync_cycle": ("SET GLOBAL wsrep_desync = {value}",),
    "maint_mode_cycle": ("SET GLOBAL pxc_maint_mode = {value}",),
    # Two statements, and the order is load-bearing in both directions.
    # sql_require_primary_key=OFF is rejected outright while pxc_strict_mode is
    # ENFORCING (sql/sys_vars.cc), so the strict mode must move first; and
    # raising it to ENFORCING force-sets the GLOBAL sql_require_primary_key
    # back to ON anyway (sql/wsrep_var.cc pxc_strict_mode_update), so the
    # second statement is a no-op on the restore path rather than a
    # correction. It is written out regardless: a restore that depends on
    # another variable's side effect is one refactor away from silently
    # leaving PK enforcement off for the rest of the run.
    "strict_mode_window": (
        "SET GLOBAL pxc_strict_mode = {value}",
        "SET GLOBAL sql_require_primary_key = {require_pk}",
    ),
    "ws_size_squeeze": ("SET GLOBAL wsrep_max_ws_size = {value}",),
    "applier_resize": ("SET GLOBAL wsrep_applier_threads = {value}",),
    "backup_lock": ("SELECT 1 /* backup lock is session-scoped; nothing to restore */",),
    "cluster_address_reset": ("SELECT 1 /* rejoin is self-healing; nothing to restore */",),
    "graceful_shutdown": ("SELECT 1 /* supervisor restarts the node; nothing to restore */",),
}


def _restore_statements(lever: str, value: str) -> list[str] | None:
    """The SQL that puts `lever` back to `value`, or None if there is none.

    `require_pk` is derived rather than stored so that the lease rows written
    by earlier code -- which carry only the pxc_strict_mode value -- keep
    restoring correctly.
    """
    templates = RESTORE_SQL.get(lever)
    if templates is None:
        return None
    require_pk = "OFF" if str(value).upper() in ("DISABLED", "PERMISSIVE") else "ON"
    return [t.format(value=value, require_pk=require_pk) for t in templates]


# Levers that are applied to EVERY node, so recovery must restore every node
# rather than only the one the lease names. Without this, a driver killed
# mid-window leaves the other two nodes changed for the rest of the run --
# which silently invalidates other levers' premises.
CLUSTER_WIDE: frozenset[str] = frozenset({"strict_mode_window"})


def _restore_on_host(host: str, statements: list[str]) -> bool:
    conn = db.connect_with_retry(host, attempts=2)
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
        return True
    except Exception:  # noqa: BLE001 - retried on the next repair pass
        return False
    finally:
        db.close_quietly(conn)


def _apply_restore(lever: str, node: str, value: str) -> bool:
    statements = _restore_statements(lever, value)
    if statements is None:
        return True

    if lever in CLUSTER_WIDE:
        # Every node, and a partial restore keeps the lease so the next repair
        # pass retries the ones that were unreachable.
        ok = True
        for _, host in config.NODES:
            if not _restore_on_host(host, statements):
                ok = False
        return ok

    host = config.NODE_HOSTS.get(node)
    if host is None:
        return True
    return _restore_on_host(host, statements)


def repair_expired(jr, *, force_all: bool = False) -> list[dict[str, object]]:
    """Restore every lever whose lease has expired, and drop the lease row.

    Must be the first action of every command. ``force_all`` ignores deadlines
    and reclaims everything -- used by the terminal verifiers, which run after
    all drivers have been killed and so know that no lease is legitimately
    still held.

    A lever whose restore could not be applied (node unreachable) keeps its
    lease row, so the next command retries it.
    """
    now = time.time()
    if force_all:
        rows = jr.conn.execute("SELECT * FROM lease").fetchall()
    else:
        rows = jr.conn.execute(
            "SELECT * FROM lease WHERE deadline < ?", (now,)
        ).fetchall()

    repaired: list[dict[str, object]] = []
    for row in rows:
        ok = _apply_restore(row["lever"], row["node"], row["restore"])
        if ok:
            with jr.conn:
                jr.conn.execute("DELETE FROM lease WHERE lever = ?", (row["lever"],))
            repaired.append(
                {"lever": row["lever"], "node": row["node"], "restore": row["restore"]}
            )

    # The disruption token is released whenever its lever no longer holds a
    # lease, so a killed holder cannot wedge the gate shut forever.
    tok = jr.conn.execute("SELECT * FROM disruption_token WHERE id = 1").fetchone()
    if tok is not None:
        still_held = jr.conn.execute(
            "SELECT 1 FROM lease WHERE lever = ?", (tok["lever"],)
        ).fetchone()
        if still_held is None or force_all or tok["deadline"] < now:
            with jr.conn:
                jr.conn.execute("DELETE FROM disruption_token WHERE id = 1")

    return repaired


def _all_synced() -> bool:
    states = db.cluster_status()
    return len(states) == config.EXPECTED_CLUSTER_SIZE and all(
        db.is_synced(s) for s in states.values()
    )


def acquire(
    jr,
    lever: str,
    node: str,
    *,
    intent: str,
    restore: str,
    hold_seconds: float,
) -> bool:
    """Try to take a lever. False means someone else holds it, or it is unsafe.

    The deadline is the hold time plus generous slack, so that a driver killed
    mid-hold has its lever restored by whichever command next runs repair,
    rather than at some unbounded time in the future.
    """
    now = time.time()
    deadline = now + hold_seconds + 60.0

    if lever in DISRUPTIVE and not _all_synced():
        # Never stack a disruption on top of an already-degraded cluster: that
        # is how a workload manufactures quorum loss and then reports it.
        return False

    # One transaction for both rows. Split across two, there is a window where
    # the token exists without its lease -- and repair_expired reads that as a
    # leaked token and frees it, after which a second disruptive lever can be
    # taken while this one is still about to be applied.
    try:
        with jr.conn:
            if lever in DISRUPTIVE:
                jr.conn.execute(
                    "INSERT INTO disruption_token (id, lever, node, inv_id, deadline) "
                    "VALUES (1, ?, ?, ?, ?)",
                    (lever, node, jr.inv_id, deadline),
                )
            jr.conn.execute(
                "INSERT INTO lease (lever, node, inv_id, intent, restore, acquired_at, deadline) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lever, node, jr.inv_id, intent, restore, now, deadline),
            )
    except sqlite3.IntegrityError:
        # Either the token or the lever is already held. The transaction rolled
        # back, so neither row was written.
        return False
    return True


def release(jr, lever: str, node: str, restore: str) -> None:
    """Restore the lever and drop OUR lease. Safe to call more than once.

    The inv_id guard is essential. A driver whose lease expired while it was
    still working (an injected I/O stall, or simply graceful_shutdown taking
    longer than its deadline) will have had its lever restored and its lease
    reclaimed by another command's repair pass. If it then deleted rows by
    lever name alone, it would delete the NEW holder's lease and token -- and
    when that holder is killed by eventually_, there would be no lease row left
    for repair to find, so its lever would stay set through the entire terminal
    verification and fail convergence for a harness reason.

    Restoring the value is still unconditional and harmless: putting back a
    default we no longer own is at worst a no-op.
    """
    _apply_restore(lever, node, restore)
    with jr.conn:
        jr.conn.execute(
            "DELETE FROM lease WHERE lever = ? AND inv_id = ?", (lever, jr.inv_id)
        )
        jr.conn.execute(
            "DELETE FROM disruption_token WHERE id = 1 AND lever = ? AND inv_id = ?",
            (lever, jr.inv_id),
        )


def release_all_held_by(jr) -> None:
    """Release every lease this invocation holds. Called on the way out."""
    rows = jr.conn.execute(
        "SELECT * FROM lease WHERE inv_id = ?", (jr.inv_id,)
    ).fetchall()
    for row in rows:
        release(jr, row["lever"], row["node"], row["restore"])


def maint_intent(jr, node: str) -> str | None:
    """The operator-intended pxc_maint_mode for a node, if we set one."""
    row = jr.conn.execute(
        "SELECT intent FROM lease WHERE lever = 'maint_mode_cycle' AND node = ?",
        (node,),
    ).fetchone()
    return row["intent"] if row else None
