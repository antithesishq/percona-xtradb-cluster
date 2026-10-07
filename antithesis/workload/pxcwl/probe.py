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

import os
import traceback
import time

from . import config, db, events, journal, leases, oracles


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
            "probe_failed_since": None,
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


def _probe_write(host: str, node: str) -> tuple[str, str | None]:
    """Try to actually commit something. The health surface never checks this.

    Returns (outcome, reason). The outcome is PROBE_OK, PROBE_FAILED or
    PROBE_NO_SCHEMA. The last one is the important one: if the workload schema
    is not there (the first_ command's node was unreachable when it ran, which
    fault injection makes routine), then a failed write is a harness condition
    rather than evidence about the node, and no assertion may be based on it.

    The reason is why a non-OK outcome happened, carried into the assertion's
    details. Triage of run de51f0b9-63-0 could not tell a node that refused a
    write from a node the workload could not open a socket to, because the
    only thing recorded was that no write had landed.
    """
    conn = db.connect_with_retry(host, config.SCHEMA, attempts=1)
    if conn is None:
        # Could we reach the server at all, just not the schema?
        bare = db.connect_with_retry(host, attempts=1)
        if bare is None:
            return PROBE_FAILED, f"connect: {db.LAST_ERROR.get(host, 'unreachable')}"
        try:
            with bare.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'wl_probe'",
                    (config.SCHEMA,),
                )
                exists = int(cur.fetchone()[0]) > 0
            if exists:
                return PROBE_FAILED, "schema connect failed but wl_probe exists"
            return PROBE_NO_SCHEMA, "wl_probe absent"
        except Exception as exc:  # noqa: BLE001
            return PROBE_FAILED, f"{type(exc).__name__}: {str(exc)[:120]}"
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
                return PROBE_NO_SCHEMA, "wl_probe row for this node not seeded"
        return PROBE_OK, None
    except Exception as exc:  # noqa: BLE001
        return PROBE_FAILED, f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        db.close_quietly(conn)


def _merge_progress(
    jr,
    node: str,
    *,
    now: float,
    committed: int,
    advanced: bool,
    green: bool,
    fc_paused: int,
    probe: str,
    wedged: bool,
) -> dict:
    """Fold one observation into the shared row; return the merged state.

    Antithesis runs several instances of an anytime_ command concurrently --
    one branch of run de51f0b9-63-0 started 50 probe processes and finished 15
    -- and every one of them writes this row. The previous read-modify-write
    let a process whose probe had just failed write back the
    last_probe_commit_at it had read seconds earlier, erasing a success another
    process had recorded in between. That, not a node that could not commit, is
    what produced 355 counterexamples with last_probe_commit_age_s = null.

    So every field folds with an operator two concurrent writers can apply in
    any order: MAX for the monotone clocks, COALESCE for marks that latch on
    first observation, and an explicit NULL only from the process that actually
    observed the latching condition end. The read-back happens inside the same
    transaction, so the assertions below judge the merged view rather than one
    process's stale snapshot.
    """
    probe_commit = now if probe == PROBE_OK else 0.0
    with jr.conn:
        jr.conn.execute(
            "UPDATE progress SET "
            "  last_committed = MAX(last_committed, ?), "
            "  last_advance_at = MAX(last_advance_at, ?), "
            "  advertised_fc_paused = CASE WHEN NOT ? THEN NULL "
            "                              WHEN advertised_since IS NULL THEN ? "
            "                              ELSE advertised_fc_paused END, "
            "  advertised_since = CASE WHEN ? THEN COALESCE(advertised_since, ?) END, "
            "  last_probe_commit_at = CASE "
            "      WHEN ? > COALESCE(last_probe_commit_at, 0) THEN ? "
            "      ELSE last_probe_commit_at END, "
            "  probe_failed_since = CASE WHEN ? THEN COALESCE(probe_failed_since, ?) END, "
            "  wedge_since = CASE WHEN ? THEN COALESCE(wedge_since, ?) END "
            "WHERE node = ?",
            (
                committed,
                now if advanced else 0.0,
                green, fc_paused,
                green, now,
                probe_commit, probe_commit,
                probe == PROBE_FAILED, now,
                wedged, now,
                node,
            ),
        )
        row = jr.conn.execute(
            "SELECT * FROM progress WHERE node = ?", (node,)
        ).fetchone()
    return dict(row)


def _break_green(jr, node: str) -> None:
    """End the node's green run without any other observation.

    For a sample whose status could not be read. Same NULL-from-the-observer
    rule as _merge_progress: every other field is left alone, because an
    unreadable node says nothing about commits or probe outcomes.
    """
    with jr.conn:
        jr.conn.execute(
            "UPDATE progress SET advertised_since = NULL, advertised_fc_paused = NULL "
            "WHERE node = ?",
            (node,),
        )


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


def _disk_sample() -> None:
    """Log one sample of disk use as the SDK event `pxc_disk`.

    Why: the VM's disk is half of `custom.vm_memory_gb`, shared by every
    container, and run a03e2f17272bb3ed7aa59e50a0f30d50-63-2 filled it with no
    sign in the log until mysqld reported errno 28. This shows how close a
    history came, and it is the baseline for a deliberate disk-fill fault.

    - `binlog_bytes`: each node's binlog total from SHOW BINARY LOGS, the
      writer that filled the disk. None when the node is unreachable.
    - `workload_fs`: statvfs of this container's root. Whether that is the
      same pool the nodes write to is not confirmed.

    Data only, no assertion: what counts as "too full" is for the planned
    disk-fill fault to define.
    TODO: sample the nodes' own filesystems (needs the supervisor, so a
    pxc-node rebuild).
    """
    binlog_bytes: dict[str, int | None] = {}
    for name, host in config.NODES:
        conn = db.connect_with_retry(host, attempts=1)
        if conn is None:
            binlog_bytes[name] = None
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW BINARY LOGS")
                binlog_bytes[name] = sum(int(row[1]) for row in cur.fetchall())
        except Exception:  # noqa: BLE001 - a sample is best-effort
            binlog_bytes[name] = None
        finally:
            db.close_quietly(conn)
    st = os.statvfs("/")
    events.emit(
        "pxc_disk",
        {
            "binlog_bytes": binlog_bytes,
            "workload_fs": {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize},
        },
    )


def _split_brain(
    states: dict[str, dict[str, str] | None],
    sampled_at: dict[str, float],
    window: float,
) -> dict:
    """Find two nodes that claim disjoint Primary components at the same time.

    Two nodes that each claim Primary must agree on at least one member.
    Disjoint Primary views are split brain. Comparing member sets rather than
    counts tolerates a view change that one node has seen and the other not.

    A pair counts only when both reads returned within `window` seconds of
    each other. Two reads further apart do not show that both views existed
    at once (run 69449aa5-63-5, vtime 212.50), so they give no verdict. The
    reads come from db.cluster_status_sampled(), which reads all nodes at
    once, so a lasting split brain is still compared on every iteration.

    The Primary test is still wsrep_cluster_status alone. A node can be
    Primary while it is a Donor or Joined, and a split brain often starts a
    state transfer, so a stricter test would hide real cases.
    """
    primary_sets: dict[str, set[str]] = {}
    for name, st in states.items():
        if st is None or name not in sampled_at:
            continue
        if st.get("wsrep_cluster_status", "").lower() == "primary":
            addrs = {
                a.strip()
                for a in (st.get("wsrep_incoming_addresses") or "").split(",")
                if a.strip()
            }
            if addrs:
                primary_sets[name] = addrs

    compared = 0
    skipped_skew = []
    disjoint = None
    names = sorted(primary_sets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            skew = abs(sampled_at[a] - sampled_at[b])
            if skew > window:
                skipped_skew.append({"pair": [a, b], "skew_s": round(skew, 3)})
                continue
            compared += 1
            if not (primary_sets[a] & primary_sets[b]):
                disjoint = (a, b)

    first = min(sampled_at.values()) if sampled_at else 0.0
    return {
        "primary_views": {k: sorted(v) for k, v in primary_sets.items()},
        "disjoint_pair": disjoint,
        "compared_pairs": compared,
        "skipped_for_skew": skipped_skew,
        # Offsets from the earliest read, so the gap between reads is plain.
        "sampled_at_offset_s": {k: round(v - first, 3) for k, v in sorted(sampled_at.items())},
        "window_s": window,
    }


def _confirmed_split_brain(
    states: dict[str, dict[str, str] | None],
    sampled_at: dict[str, float],
) -> dict:
    """_split_brain, with a re-read of a disjoint pair before it counts.

    One status read is not atomic. On a leave, Galera sets
    wsrep_incoming_addresses to the new view first
    (galera/src/replicator_smm.cpp:2756, update_incoming_list) and only then
    runs the view callback that sets wsrep_cluster_status to non-Primary
    (sql/wsrep_server_service.cc:387). In run fdb9d32c-63-5 (vtime 146.11)
    node2 was read in that 20 ms gap: "Primary", members {node2}, while node3
    was the real one-member Primary. Galera never had two Primary components.

    So a disjoint pair is read again after PRIMARY_CONFIRM_DELAY_SECONDS, and
    the verdict is the re-read's. A real split brain lasts, so it shows again.
    The first read stays in the details as first_read either way.
    """
    first = _split_brain(states, sampled_at, config.PRIMARY_SAMPLE_WINDOW_SECONDS)
    if first["disjoint_pair"] is None:
        return first

    pair = set(first["disjoint_pair"])
    time.sleep(config.PRIMARY_CONFIRM_DELAY_SECONDS)
    again_states, again_at = db.cluster_status_sampled(
        [(n, h) for n, h in config.NODES if n in pair]
    )
    again = _split_brain(again_states, again_at, config.PRIMARY_SAMPLE_WINDOW_SECONDS)

    # Four outcomes of the re-read:
    #   both still Primary, still disjoint -> the verdict fails (split brain)
    #   both still Primary, now overlapping -> pass; the first read was torn
    #   a node answered and is not Primary  -> no verdict; it left, as in
    #                                          run fdb9d32c-63-5 vtime 146.11
    #   a node did not answer               -> no verdict
    #   the two reads were too far apart     -> no verdict
    # "No verdict" is not a pass. A split brain that ended inside the 0.5 s
    # delay looks the same as a torn read, so neither outcome may count as
    # evidence that there was no split brain.
    if again["disjoint_pair"] is not None:
        return {**again, "first_read": first}
    if again["compared_pairs"]:
        outcome = "overlapping"
    elif len(again_at) < len(pair):
        outcome = "unreachable"
    elif again["skipped_for_skew"]:
        # Both still Primary, but the reads were too far apart to compare.
        outcome = "skewed"
    else:
        outcome = "left_primary"
    if outcome in ("overlapping", "left_primary"):
        oracles.saw_torn_primary_read({"outcome": outcome, "first_read": first, "re_read": again})
    if outcome == "overlapping":
        return {**again, "first_read": first}
    # TODO: the re-read covers only the pair. A third node's claim is not
    # read again, which matters only if all three nodes claim Primary alone.
    return {**again, "compared_pairs": 0, "re_read_outcome": outcome, "first_read": first}


def _run() -> int:
    jr = journal.Journal("probe")
    try:
        leases.repair_expired(jr)
        _disk_sample()
        deadline = time.time() + config.PROBE_WALL_BUDGET_SECONDS
        saw_small_cluster = False
        # Reach claims already made this invocation, as "node:pattern".
        claimed: set[str] = set()
        iteration = 0

        while time.time() < deadline:
            now = time.time()
            iteration += 1
            scan_logs = iteration % config.ERROR_LOG_SCAN_EVERY == 1
            states, sampled_at = db.cluster_status_sampled()

            # ---------------------------------------------------- membership
            for st in states.values():
                if st is None:
                    continue
                try:
                    if int(st.get("wsrep_cluster_size", "3") or 3) < config.EXPECTED_CLUSTER_SIZE:
                        saw_small_cluster = True
                except ValueError:
                    pass

            verdict = _confirmed_split_brain(states, sampled_at)
            # Needs at least one pair of simultaneous Primary claims to mean
            # anything: with none, there is no pair to be disjoint and
            # evaluating the assertion would just add passing noise.
            if verdict["compared_pairs"]:
                oracles.single_primary_component(verdict["disjoint_pair"] is None, verdict)

            for name, host in config.NODES:
                st = states.get(name)
                prog = _progress_row(jr, name)

                if st is None:
                    # A sample we could not take is a gap in the green run,
                    # not a continuation of it. Run 8fd9e28b-63-5: node3's own
                    # log shows it non-Primary at least five times inside one
                    # "148 s green" window, under partitions that also cut it
                    # off from the workload.
                    _break_green(jr, name)
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
                maint_read = False
                conn = db.connect_with_retry(host, attempts=1)
                if conn is not None:
                    try:
                        maint = db.global_vars(conn, ["pxc_maint_mode"]).get("pxc_maint_mode")
                        maint_read = bool(maint)
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

                # clustercheck_green treats an unknown pxc_maint_mode as
                # DISABLED, which is right for mirroring the script's formula
                # and wrong as evidence: a maint mode we could not read is a
                # node we could not question, not a node advertising itself.
                green = maint_read and db.clustercheck_green(st, maint)

                probe, probe_reason = _probe_write(host, name)
                wrote = probe == PROBE_OK
                no_evidence = probe == PROBE_NO_SCHEMA
                if no_evidence:
                    # Neither the health claim nor the wedge watchdog can be
                    # judged without a usable probe, so drop the green window
                    # with it.
                    green = False
                elif green:
                    # The status above was read before the maint-mode read and
                    # the probe write, seconds earlier under faults. Only call
                    # the node green if it still is now that the write is
                    # done: in run 8fd9e28b-63-5 a Primary/Synced read paired
                    # with a write the node refused (1047) because it had
                    # left the primary component in between. A wedged node
                    # still answers this read green, so a real wedge is
                    # still judged.
                    green = db.clustercheck_green(db.node_status(host), maint)

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
                wedged = healthy_claim and not advanced and not wrote

                merged = _merge_progress(
                    jr,
                    name,
                    now=now,
                    committed=committed,
                    advanced=advanced,
                    green=green,
                    fc_paused=fc_paused,
                    probe=probe,
                    wedged=wedged,
                )
                advertised_since = merged["advertised_since"]
                advertised_fc_paused = merged["advertised_fc_paused"]
                last_probe_commit_at = merged["last_probe_commit_at"]
                probe_failed_since = merged["probe_failed_since"]
                wedge_since = merged["wedge_since"]

                if (
                    green
                    and advertised_since is not None
                    and now - advertised_since >= config.GREEN_WINDOW_SECONDS
                ):
                    # The trailing window, not "any time since the node went
                    # green": otherwise one probe that landed the moment the
                    # green run opened would exempt the node from the property
                    # for as long as it then stayed wedged.
                    floor = max(advertised_since, now - config.GREEN_WINDOW_SECONDS)
                    committed_in_window = (
                        last_probe_commit_at is not None
                        and last_probe_commit_at >= floor
                    )
                    # A violation needs positive evidence that writes were
                    # tried and refused for the whole window -- "no successful
                    # probe is on record" also describes a probe that never
                    # ran, a journal row another process has not filled in yet,
                    # and a workload container the network dropped. Those say
                    # nothing about the node, so they are not judged at all.
                    sustained_failure = (
                        probe_failed_since is not None
                        and now - probe_failed_since >= config.GREEN_WINDOW_SECONDS
                    )
                    if committed_in_window or sustained_failure:
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
                                "probe_failed_seconds": (
                                    round(now - probe_failed_since, 1)
                                    if probe_failed_since
                                    else None
                                ),
                                "probe_outcome": probe,
                                "probe_reason": probe_reason,
                                "local_state": local_state,
                                "pxc_maint_mode": maint,
                                # A flow-control pause is one of the real
                                # mechanisms this property attacks -- a paused
                                # Synced node keeps advertising available -- so
                                # the check stays armed. This delta is what lets
                                # triage separate a genuine wedge from a
                                # legitimately long pause, and it is the
                                # measurement that calibrates
                                # GREEN_WINDOW_SECONDS.
                                "fc_paused_ns_delta": (
                                    fc_paused - advertised_fc_paused
                                    if advertised_fc_paused is not None
                                    else None
                                ),
                                "fc_active": st.get("wsrep_flow_control_active"),
                            },
                        )

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
                            "probe_reason": probe_reason,
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
