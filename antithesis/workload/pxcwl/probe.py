"""Continuous cluster probing. Entry point for anytime_.

Runs alongside the drivers with faults active, which is the only scheduling
slot where "the system claims health while doing nothing" can be observed at
all. State that has to span invocations -- how long a node has been
advertising itself as available, when it last committed anything -- lives in
the journal's progress table.

This command can be cancelled at any moment: an eventually_ command kills
running anytime_ commands when it starts. Being killed is normal and must never
be treated as a finding, so nothing here accumulates a verdict that depends on
reaching the end of the window.
"""

from __future__ import annotations

import traceback
import time

from . import config, db, journal, leases, oracles


def _progress_row(jr, node: str) -> dict:
    row = jr.conn.execute("SELECT * FROM progress WHERE node = ?", (node,)).fetchone()
    if row is None:
        now = time.time()
        with jr.conn:
            jr.conn.execute(
                "INSERT INTO progress (node, last_committed, last_advance_at) "
                "VALUES (?, -1, ?)",
                (node, now),
            )
        return {
            "node": node,
            "last_committed": -1,
            "last_advance_at": now,
            "advertised_since": None,
            "last_probe_commit_at": None,
            "wedge_since": None,
            "advertised_fc_paused": None,
        }
    return dict(row)


def _claim_error_log(conn, node: str, claimed: set[str]) -> None:
    """Fire the reach claims that a STILL-LIVE node's own error log backs.

    Deduplicated by CLAIM, not by pattern: two patterns feed the failed-transfer
    claim, and a node hitting both would otherwise report the same reach twice.
    One-shot per node per invocation for the same reason -- a reach claim says
    the state was entered at least once, so re-firing it every few seconds for
    as long as the line sits in the ring buffer is noise, not information.
    """
    claims = {
        "sst_failed": ("failed_transfer", oracles.saw_failed_state_transfer),
        "sst_process_error": ("failed_transfer", oracles.saw_failed_state_transfer),
        "ist_fallback": ("ist_fallback", oracles.saw_ist_fallback),
        "inconsistency": ("inconsistent", oracles.saw_inconsistency_verdict),
    }
    if all(f"{node}:{key}" in claimed for key, _ in claims.values()):
        return
    for pattern, line in db.error_log_matches(conn).items():
        key, fire = claims[pattern]
        tag = f"{node}:{key}"
        if tag in claimed:
            continue
        claimed.add(tag)
        fire({"node": node, "pattern": pattern, "log_line": line})


# Probe outcomes. "no_schema" is deliberately distinct from "failed": it means
# we could not even address the workload schema, which says nothing about
# whether the node can commit.
PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_NO_SCHEMA = "no_schema"


def _probe_write(host: str, node: str) -> str:
    """Try to actually commit something. The health surface never checks this.

    Returns PROBE_OK, PROBE_FAILED, or PROBE_NO_SCHEMA. The last one is the
    important one: if the workload schema is not there (the first_ command's
    node was unreachable when it ran, which fault injection makes routine),
    then a failed write is a harness condition rather than evidence about the
    node, and no assertion may be based on it.
    """
    conn = db.connect_with_retry(host, config.SCHEMA, attempts=1)
    if conn is None:
        # Could we reach the server at all, just not the schema?
        bare = db.connect_with_retry(host, attempts=1)
        if bare is None:
            return PROBE_FAILED
        try:
            with bare.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'wl_probe'",
                    (config.SCHEMA,),
                )
                exists = int(cur.fetchone()[0]) > 0
            return PROBE_FAILED if exists else PROBE_NO_SCHEMA
        except Exception:  # noqa: BLE001
            return PROBE_FAILED
        finally:
            db.close_quietly(bare)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `wl_probe` SET n = n + 1, at = %s WHERE node = %s",
                (int(time.time() * 1000), node),
            )
            if cur.rowcount == 0:
                # Schema present but unseeded: an UPDATE matching no row
                # commits nothing, so it proves nothing either.
                return PROBE_NO_SCHEMA
        return PROBE_OK
    except Exception:  # noqa: BLE001
        return PROBE_FAILED
    finally:
        db.close_quietly(conn)


def run() -> int:
    """Entry point. Never exits non-zero for an environment condition.

    Antithesis attaches a built-in property to each command's exit code, so a
    non-zero exit must mean a genuine bug -- not a locked journal, an
    unreachable node, or a command killed mid-flight.
    """
    try:
        return _run()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 0


def _run() -> int:
    jr = journal.Journal("probe")
    try:
        leases.repair_expired(jr)
        deadline = time.time() + config.PROBE_WALL_BUDGET_SECONDS
        saw_small_cluster = False
        # Reach claims already made this invocation, as "node:pattern".
        claimed: set[str] = set()
        iteration = 0

        while time.time() < deadline:
            now = time.time()
            iteration += 1
            scan_logs = iteration % config.ERROR_LOG_SCAN_EVERY == 1
            states = db.cluster_status()

            # ---------------------------------------------------- membership
            primary_sets: dict[str, set[str]] = {}
            for name, st in states.items():
                if st is None:
                    continue
                if st.get("wsrep_cluster_status", "").lower() == "primary":
                    addrs = {
                        a.strip()
                        for a in (st.get("wsrep_incoming_addresses") or "").split(",")
                        if a.strip()
                    }
                    if addrs:
                        primary_sets[name] = addrs
                try:
                    if int(st.get("wsrep_cluster_size", "3") or 3) < config.EXPECTED_CLUSTER_SIZE:
                        saw_small_cluster = True
                except ValueError:
                    pass

            # Two nodes each claiming Primary must at least agree on some
            # member. Disjoint primary views are split brain. Comparing
            # membership sets rather than counts is what makes this tolerant of
            # poll skew instead of flaky.
            disjoint = None
            names = sorted(primary_sets)
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    if not (primary_sets[names[i]] & primary_sets[names[j]]):
                        disjoint = (names[i], names[j])
            # Needs at least two nodes claiming Primary to mean anything: with
            # fewer, there is no pair to be disjoint and evaluating the
            # assertion would just add passing noise to the report.
            if len(names) >= 2:
                oracles.single_primary_component(
                    disjoint is None,
                    {
                        "primary_views": {k: sorted(v) for k, v in primary_sets.items()},
                        "disjoint_pair": disjoint,
                    },
                )

            for name, host in config.NODES:
                st = states.get(name)
                prog = _progress_row(jr, name)

                if st is None:
                    continue

                try:
                    committed = int(st.get("wsrep_last_committed", "-1") or -1)
                    recv_queue = int(st.get("wsrep_local_recv_queue", "0") or 0)
                    desync_count = int(st.get("wsrep_desync_count", "0") or 0)
                    fc_sent = int(st.get("wsrep_flow_control_sent", "0") or 0)
                    fc_paused = int(st.get("wsrep_flow_control_paused_ns", "0") or 0)
                except ValueError:
                    continue

                local_state = st.get("wsrep_local_state", "")
                advanced = committed > int(prog["last_committed"])

                # -------------------------------------------- recv queue bound
                # Flow control is supposed to be what keeps this bounded, so a
                # large multiple of fc_limit catches runaway rather than normal
                # backpressure. Gated on the server's own view of desync, never
                # on our ledger.
                fc_limit = int(jr.get_observed("fc_limit", str(config.FC_LIMIT_DEFAULT)) or config.FC_LIMIT_DEFAULT)
                # Both bounds: the multiple of fc_limit scales with the
                # timeline's backpressure setting, and the absolute cap keeps
                # the check firable when fc_limit was swarmed high (500 x 100
                # would allow 50,000 queued writesets, which the node would
                # never survive anyway).
                bound = min(fc_limit * config.RECV_QUEUE_SLACK, config.RECV_QUEUE_ABS_MAX)
                if local_state == "4" and desync_count == 0:
                    oracles.recv_queue_bounded(
                        recv_queue <= bound,
                        {
                            "node": name,
                            "recv_queue": recv_queue,
                            "bound": bound,
                            "fc_limit": fc_limit,
                            "absolute_cap": config.RECV_QUEUE_ABS_MAX,
                            "local_state": local_state,
                        },
                    )

                if fc_sent > 0:
                    oracles.saw_flow_control({"node": name, "flow_control_sent": fc_sent})

                if local_state in ("2", "3"):
                    oracles.saw_state_transfer(
                        {"node": name, "local_state_comment": st.get("wsrep_local_state_comment")}
                    )

                # ------------------------------------------ health truthfulness
                maint = None
                conn = db.connect_with_retry(host, attempts=1)
                if conn is not None:
                    try:
                        maint = db.global_vars(conn, ["pxc_maint_mode"]).get("pxc_maint_mode")
                    except Exception:  # noqa: BLE001
                        maint = None
                    try:
                        # A node still answering queries is exactly the case the
                        # supervisor's death-time log scan cannot reach.
                        if scan_logs:
                            _claim_error_log(conn, name, claimed)
                    except Exception:  # noqa: BLE001 - never fail a probe on this
                        pass
                    finally:
                        db.close_quietly(conn)

                green = db.clustercheck_green(st, maint)
                advertised_since = prog["advertised_since"]
                advertised_fc_paused = prog["advertised_fc_paused"]
                if green:
                    if advertised_since is None:
                        advertised_since = now
                        advertised_fc_paused = fc_paused
                else:
                    advertised_since = None
                    advertised_fc_paused = None

                probe = _probe_write(host, name)
                wrote = probe == PROBE_OK
                no_evidence = probe == PROBE_NO_SCHEMA
                last_probe_commit_at = (
                    now if wrote else prog["last_probe_commit_at"]
                )

                if no_evidence:
                    # Restart both windows: neither the health claim nor the
                    # wedge watchdog can be judged without a usable probe.
                    advertised_since = None

                if (
                    green
                    and not no_evidence
                    and advertised_since is not None
                    and now - advertised_since >= config.GREEN_WINDOW_SECONDS
                ):
                    committed_in_window = (
                        last_probe_commit_at is not None
                        and last_probe_commit_at >= advertised_since
                    )
                    oracles.green_node_can_commit(
                        committed_in_window,
                        {
                            "node": name,
                            "advertised_seconds": round(now - advertised_since, 1),
                            "window_seconds": config.GREEN_WINDOW_SECONDS,
                            "last_probe_commit_age_s": (
                                round(now - last_probe_commit_at, 1)
                                if last_probe_commit_at
                                else None
                            ),
                            "local_state": local_state,
                            "pxc_maint_mode": maint,
                            # A flow-control pause is one of the real
                            # mechanisms this property attacks -- a paused
                            # Synced node keeps advertising available -- so the
                            # check stays armed. This delta is what lets triage
                            # separate a genuine wedge from a legitimately long
                            # pause, and it is the measurement that calibrates
                            # GREEN_WINDOW_SECONDS.
                            "fc_paused_ns_delta": (
                                fc_paused - advertised_fc_paused
                                if advertised_fc_paused is not None
                                else None
                            ),
                            "fc_active": st.get("wsrep_flow_control_active"),
                        },
                    )

                # ---------------------------------------------- wedge watchdog
                # Everyone claims Synced and Primary, nobody is transferring
                # state, and yet nothing commits and our probe write fails.
                # That single condition covers a leaked ordering-monitor slot,
                # an unreleased flow-control pause, and a wedged applier.
                healthy_claim = (
                    db.is_synced(st)
                    and desync_count == 0
                    and local_state == "4"
                    and not no_evidence
                )
                wedge_since = prog["wedge_since"]
                if healthy_claim and not advanced and not wrote:
                    if wedge_since is None:
                        wedge_since = now
                else:
                    wedge_since = None

                if wedge_since is not None and now - wedge_since >= config.WEDGE_WINDOW_SECONDS:
                    oracles.commit_progress_not_frozen(
                        False,
                        {
                            "node": name,
                            "frozen_seconds": round(now - wedge_since, 1),
                            "window_seconds": config.WEDGE_WINDOW_SECONDS,
                            "last_committed": committed,
                            "recv_queue": recv_queue,
                            "flow_control_paused_ns": st.get("wsrep_flow_control_paused_ns"),
                            "probe_write_succeeded": wrote,
                        },
                    )
                elif healthy_claim and (advanced or wrote):
                    oracles.commit_progress_not_frozen(
                        True,
                        {
                            "node": name,
                            "last_committed": committed,
                            "advanced": advanced,
                            "probe_write_succeeded": wrote,
                        },
                    )

                with jr.conn:
                    jr.conn.execute(
                        "UPDATE progress SET last_committed = ?, last_advance_at = ?, "
                        "advertised_since = ?, last_probe_commit_at = ?, wedge_since = ?, "
                        "advertised_fc_paused = ? WHERE node = ?",
                        (
                            committed,
                            now if advanced else prog["last_advance_at"],
                            advertised_since,
                            last_probe_commit_at,
                            wedge_since,
                            advertised_fc_paused,
                            name,
                        ),
                    )

            if saw_small_cluster:
                states_now = db.cluster_status()
                if all(db.is_synced(s) for s in states_now.values()):
                    oracles.saw_membership_churn(
                        {
                            "note": "observed a shrunken cluster that later returned to full size",
                            "sizes": {
                                n: (s or {}).get("wsrep_cluster_size")
                                for n, s in states_now.items()
                            },
                        }
                    )
                    saw_small_cluster = False

            time.sleep(config.PROBE_INTERVAL_SECONDS)
        return 0
    finally:
        jr.finish()
