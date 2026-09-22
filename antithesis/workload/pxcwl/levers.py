"""Administrative levers.

These are what make the operational properties reachable at all: a workload
that only issues DML never visits donor state, never changes quorum weights,
and never exercises maintenance mode. The ``antithesis`` user already holds
SYSTEM_VARIABLES_ADMIN, CONNECTION_ADMIN and BACKUP_ADMIN for exactly this.

Every lever that changes global state is taken under a lease with a restore
value (see ``leases``), because an ``eventually_`` command kills running
drivers and a leaked lever would leave the cluster permanently impaired --
turning every convergence assertion into a false positive.

Three things are deliberately NOT levers here, each because it self-inflicts a
finding rather than exploring one:

  pc.weight = 0    removes the node from quorum arithmetic entirely
  pc.bootstrap     manufactures split brain
  NBO DDL          deterministically aborts a joiner

The catalog fences all three into separate variants with a poison budget.
"""

from __future__ import annotations

import socket
import time

from . import config, db, leases, oracles, rnd

SCHEMA = config.SCHEMA


def _set_global(host: str, sql: str, params: tuple = ()) -> bool:
    conn = db.connect_with_retry(host, attempts=2)
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        db.close_quietly(conn)


def _port_open(host: str, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, config.MYSQL_PORT), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


# ==========================================================================
# Benign, per-node levers
# ==========================================================================


def applier_resize(jr, s, profile: dict) -> None:
    """Resize the applier pool at runtime and watch it converge.

    The setpoint check is wsrep_thread_count == wsrep_applier_threads + 1 (the
    extra thread is the receiver), paired with evidence that commits kept
    advancing -- a pool that reaches its size but stops applying is the
    interesting failure, not the one where the number is wrong.
    """
    target = rnd.choice([1, 2, 4, 8, 16])
    if not leases.acquire(
        jr, "applier_resize", s.name, intent=str(target), restore="4", hold_seconds=30
    ):
        return

    before = db.node_status(s.host) or {}
    committed_before = int(before.get("wsrep_last_committed", "0") or 0)

    if not _set_global(s.host, "SET GLOBAL wsrep_applier_threads = %s", (target,)):
        leases.release(jr, "applier_resize", s.name, "4")
        return

    deadline = time.time() + config.RESIZE_SETTLE_SECONDS
    converged = False
    observed: dict[str, str] = {}
    last_poll_ok = False
    setpoint_still_ours = False

    while time.time() < deadline:
        st = db.node_status(s.host)
        if st is None:
            last_poll_ok = False
            time.sleep(1.0)
            continue
        observed = st
        last_poll_ok = True

        # Confirm the setpoint is still the one we set. Another lever -- or a
        # node restart, which comes back at my.cnf's value -- can move it, and
        # then this is not our experiment any more.
        conn = db.connect_with_retry(s.host, attempts=1)
        current = None
        if conn is not None:
            try:
                current = db.global_vars(conn, ["wsrep_applier_threads"]).get(
                    "wsrep_applier_threads"
                )
            except Exception:  # noqa: BLE001
                current = None
            finally:
                db.close_quietly(conn)
        setpoint_still_ours = current is not None and int(current) == target
        if not setpoint_still_ours:
            break

        try:
            threads = int(st.get("wsrep_thread_count", "0") or 0)
        except ValueError:
            break
        # >= rather than ==: the receiver adds one, and a node acting as donor
        # legitimately carries extra threads. The claim is that the pool
        # REACHES the setpoint, not that nothing else ever adds a thread.
        if threads >= target + 1:
            converged = True
            break
        time.sleep(1.0)

    committed_after = int(observed.get("wsrep_last_committed", "0") or 0)

    # Judge only when the node was still answering on the LAST poll and the
    # setpoint was still ours. Otherwise the window was invalidated by
    # something other than resize convergence.
    if last_poll_ok and setpoint_still_ours:
        oracles.applier_resize_converged(
            converged,
            {
                "target": target,
                "thread_count": observed.get("wsrep_thread_count"),
                "last_committed_delta": committed_after - committed_before,
                "settle_budget_s": config.RESIZE_SETTLE_SECONDS,
                "node": s.name,
            },
        )

    leases.release(jr, "applier_resize", s.name, "4")


def maint_mode_cycle(jr, s, profile: dict) -> None:
    """Set maintenance mode, hold it, and check it is still what we set.

    Pre-registered as KNOWN-RED: operator intent is expected to be silently
    reverted on a view change. Keeping it armed is worthwhile anyway -- it is
    the end-to-end proof that an assertion here reaches the triage report.
    """
    intent = rnd.choice(["MAINTENANCE", "DISABLED"])
    hold = float(rnd.choice([1, 5, 15]))
    if not leases.acquire(
        jr, "maint_mode_cycle", s.name, intent=intent, restore="DISABLED", hold_seconds=hold
    ):
        return

    if not _set_global(s.host, "SET GLOBAL pxc_maint_mode = %s", (intent,)):
        leases.release(jr, "maint_mode_cycle", s.name, "DISABLED")
        return

    time.sleep(hold)

    conn = db.connect_with_retry(s.host, attempts=2)
    if conn is not None:
        try:
            actual = db.global_vars(conn, ["pxc_maint_mode"]).get("pxc_maint_mode")
            if actual is not None:
                oracles.maint_mode_honors_intent(
                    actual.upper() == intent.upper(),
                    {
                        "node": s.name,
                        "operator_intent": intent,
                        "observed": actual,
                        "held_seconds": hold,
                    },
                )
        except Exception:  # noqa: BLE001
            pass
        finally:
            db.close_quietly(conn)

    leases.release(jr, "maint_mode_cycle", s.name, "DISABLED")


def backup_lock(jr, s, profile: dict) -> None:
    """LOCK INSTANCE FOR BACKUP, briefly.

    pxc_strict_mode=ENFORCING blocks FLUSH TABLES WITH READ LOCK and
    LOCK TABLES, so the instance backup lock is the only backup-style lock
    available outside a fenced PERMISSIVE window. It is session-scoped, so
    there is nothing global to restore.
    """
    if not leases.acquire(
        jr, "backup_lock", s.name, intent="locked", restore="", hold_seconds=10
    ):
        return
    try:
        with s.conn.cursor() as cur:
            cur.execute("LOCK INSTANCE FOR BACKUP")
        time.sleep(float(rnd.choice([1, 3, 10])))
        with s.conn.cursor() as cur:
            cur.execute("UNLOCK INSTANCE")
        jr.tally("backup_lock", None)
    except Exception as e:  # noqa: BLE001
        jr.tally("backup_lock", db.errno_of(e))
    finally:
        leases.release(jr, "backup_lock", s.name, "")


# ==========================================================================
# Disruptive levers. One at a time, cluster-wide, and only from full health.
# ==========================================================================


def desync_cycle(jr, s, profile: dict) -> None:
    """Desync a node and bring it back.

    Desync is how a node enters local state 2 without an actual state transfer,
    which is the sub-state the health-check truthfulness property attacks: a
    desynced node keeps wsrep_ready=ON and keeps advertising available.
    """
    hold = float(rnd.choice([1, 5, 15]))
    if not leases.acquire(
        jr, "desync_cycle", s.name, intent="ON", restore="OFF", hold_seconds=hold
    ):
        return
    try:
        if not _set_global(s.host, "SET GLOBAL wsrep_desync = ON"):
            return
        jr.tally("desync_cycle", None)
        time.sleep(hold)
    finally:
        leases.release(jr, "desync_cycle", s.name, "OFF")


def pc_weight(jr, s, profile: dict) -> None:
    """Change quorum arithmetic so partitions stop being symmetric.

    Never 0: a zero-weight node is excluded from quorum entirely, which
    manufactures the quorum loss that several liveness properties exist to
    detect.
    """
    weight = rnd.choice([1, 2, 3])
    hold = float(rnd.choice([5, 20, 45]))
    if not leases.acquire(
        jr, "pc_weight", s.name, intent=str(weight), restore="1", hold_seconds=hold
    ):
        return
    try:
        if not _set_global(
            s.host, "SET GLOBAL wsrep_provider_options = %s", (f"pc.weight={weight}",)
        ):
            return
        jr.tally("pc_weight", None)
        time.sleep(hold)
    finally:
        leases.release(jr, "pc_weight", s.name, "1")


def gmcast_isolate(jr, s, profile: dict) -> None:
    """Logically isolate a node from the group, then reconnect it.

    This is the cheapest way to manufacture a genuine non-Primary window
    without waiting for the platform to partition the network, and it is what
    makes the not-ready rejection path reachable from run one rather than only
    when a fault happens to land correctly.
    """
    hold = float(rnd.choice([5, 15, 30]))
    if not leases.acquire(
        jr, "gmcast_isolate", s.name, intent="1", restore="0", hold_seconds=hold
    ):
        return
    try:
        if not _set_global(
            s.host, "SET GLOBAL wsrep_provider_options = %s", ("gmcast.isolate=1",)
        ):
            return
        jr.tally("gmcast_isolate", None)
        time.sleep(hold)
    finally:
        leases.release(jr, "gmcast_isolate", s.name, "0")


def cluster_address_reset(jr, s, profile: dict) -> None:
    """Make a node leave and rejoin, without killing anything.

    Always the full peer list, never a partial one: a partial list would be a
    configuration error rather than an exploration of the rejoin path. This is
    the only SQL-drivable IST/SST trigger available without the kill channel.
    """
    peers = ",".join(host for _, host in config.NODES)
    if not leases.acquire(
        jr,
        "cluster_address_reset",
        s.name,
        intent=peers,
        restore="",
        hold_seconds=60,
    ):
        return
    try:
        if _set_global(
            s.host, "SET GLOBAL wsrep_cluster_address = %s", (f"gcomm://{peers}",)
        ):
            jr.tally("cluster_address_reset", None)
        time.sleep(float(rnd.choice([5, 20, 45])))
    finally:
        leases.release(jr, "cluster_address_reset", s.name, "")


def graceful_shutdown(jr, s, profile: dict) -> None:
    """SQL SHUTDOWN, then wait for the supervisor to bring the node back.

    The only node-down mechanism available to this workload: the node-side kill
    channel lives on the node containers and there is no shared volume to reach
    it. So this covers the graceful half of the restart properties and leaves
    the ungraceful half untested, which is recorded as a known gap.

    The bound is generous because pxc_maint_transition_period is 10s by default
    and applier drain rides on top of it.
    """
    # The hold must cover the whole body -- port-close wait plus rejoin wait --
    # or the lease expires while still held and another command reclaims it,
    # which used to let this driver delete the next holder's lease on the way
    # out. leases.acquire adds its own slack on top of this.
    rejoin_budget = 180.0
    hold = config.SHUTDOWN_BOUND_SECONDS + rejoin_budget + 30.0
    if not leases.acquire(
        jr, "graceful_shutdown", s.name, intent="shutdown", restore="", hold_seconds=hold
    ):
        return
    try:
        started = time.time()
        conn = db.connect_with_retry(s.host, attempts=2)
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SHUTDOWN")
        except Exception:  # noqa: BLE001 - the connection dropping IS the shutdown
            pass
        finally:
            db.close_quietly(conn)

        deadline = started + config.SHUTDOWN_BOUND_SECONDS
        closed_at = None
        while time.time() < deadline:
            if not _port_open(s.host):
                closed_at = time.time()
                break
            time.sleep(1.0)

        oracles.graceful_shutdown_bounded(
            closed_at is not None,
            {
                "node": s.name,
                "bound_seconds": config.SHUTDOWN_BOUND_SECONDS,
                "elapsed_seconds": round((closed_at or time.time()) - started, 3),
                "maint_transition_period_default": 10,
            },
        )
        jr.tally("graceful_shutdown", None)

        # Give the supervisor room to restart and rejoin before another lever
        # is allowed to disrupt anything.
        rejoin_deadline = time.time() + rejoin_budget
        while time.time() < rejoin_deadline:
            if db.is_synced(db.node_status(s.host)):
                break
            time.sleep(3.0)
    finally:
        leases.release(jr, "graceful_shutdown", s.name, "")


def ws_size_squeeze(jr, s, profile: dict) -> None:
    """Probe the writeset size limit by lowering it, not by sending gigabytes.

    Shrinking wsrep_max_ws_size and then issuing a moderately large transaction
    exercises the rejection path precisely and cheaply, without the disk and
    gcache cost of an genuinely enormous writeset.
    """
    if not leases.acquire(
        jr,
        "ws_size_squeeze",
        s.name,
        intent="1048576",
        restore="2147483647",
        hold_seconds=20,
    ):
        return
    try:
        if not _set_global(s.host, "SET GLOBAL wsrep_max_ws_size = 1048576"):
            return
        wid = jr.new_wid()
        jr.attempt([wid], target="bulk", node=s.name, shape="ws_size_squeeze")
        exc = None
        try:
            with s.conn.cursor() as cur:
                cur.execute(
                    "REPLACE INTO `wl_bulk` (bid, gen, body) VALUES (%s, %s, %s)",
                    (rnd.randint(0, config.BULK_RING_SIZE - 1), wid, b"\x5a" * (4 * 1024 * 1024)),
                )
        except Exception as e:  # noqa: BLE001 - a rejection here is the expected path
            exc = e
        from . import journal as _journal

        state, errno, msg = _journal.classify(exc, s.conn)
        jr.resolve([wid], state, errno=errno, errmsg=msg)
        jr.tally("ws_size_squeeze", errno)
    finally:
        leases.release(jr, "ws_size_squeeze", s.name, "2147483647")


def strict_mode_window(jr, s, profile: dict) -> None:
    """A fenced window where PK-less DML is legal.

    pxc_strict_mode=ENFORCING blocks DML on primary-key-less tables, but
    PK-less tables are a genuine divergence lever -- certification falls back
    to hashing the whole row. So the window is opened on all three nodes,
    used briefly, and always restored. The resulting rows are compared under
    their own property name so a red there cannot mask the real invariant.
    """
    if not bool(profile.get("pkless_enabled", False)):
        return
    if not leases.acquire(
        jr,
        "strict_mode_window",
        s.name,
        intent="PERMISSIVE",
        restore="ENFORCING",
        hold_seconds=20,
    ):
        return
    try:
        for _, host in config.NODES:
            _set_global(host, "SET GLOBAL pxc_strict_mode = PERMISSIVE")
        try:
            with s.conn.cursor() as cur:
                for _ in range(int(rnd.choice([1, 5, 20]))):
                    cur.execute(
                        "INSERT INTO `wl_nopk` (a, b) VALUES (%s, %s)",
                        (rnd.randint(0, 100), f"inv{jr.inv_id}"),
                    )
            jr.tally("strict_mode_window", None)
        except Exception as e:  # noqa: BLE001
            jr.tally("strict_mode_window", db.errno_of(e))
    finally:
        # leases.release restores every node for this lever (it is in
        # leases.CLUSTER_WIDE), so a driver killed before reaching this point
        # still gets all three nodes put back by the next repair pass.
        leases.release(jr, "strict_mode_window", s.name, "ENFORCING")


LEVER_OPS: dict[str, object] = {
    "applier_resize": applier_resize,
    "maint_mode_cycle": maint_mode_cycle,
    "backup_lock": backup_lock,
    "desync_cycle": desync_cycle,
    "pc_weight": pc_weight,
    "gmcast_isolate": gmcast_isolate,
    "cluster_address_reset": cluster_address_reset,
    "graceful_shutdown": graceful_shutdown,
    "ws_size_squeeze": ws_size_squeeze,
    "strict_mode_window": strict_mode_window,
}


def run_one(jr, s, profile: dict) -> None:
    """Pull one lever, chosen by the timeline's lever weights."""
    from . import rnd as _rnd

    weights = dict(profile.get("lever_weight", {}))
    name = _rnd.weighted_choice({k: float(v) for k, v in weights.items()})
    if name is None:
        return
    op = LEVER_OPS.get(name)
    if op is None:
        return
    try:
        op(jr, s, profile)
    except Exception as e:  # noqa: BLE001
        jr.tally(f"lever:{name}:uncaught", db.errno_of(e))
