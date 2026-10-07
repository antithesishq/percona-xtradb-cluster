"""Terminal verification. Entry point for eventually_ and finally_.

All fault injection has stopped by the time this runs and every other command
has been killed (eventually_) or has completed on its own (finally_). That is
what makes the strong checks possible: the cluster can be quiesced, and a
comparison across nodes is meaningful.

The two commands share this one implementation so that each assertion has a
single callsite and therefore a single catalog entry, with ``mode`` in the
details recording which command reached it.
"""

from __future__ import annotations

import time

import traceback

from . import checks, config, db, journal, leases, oracles


# A node that has no cluster state reports the all-zero UUID: Galera resets
# the local state UUID when the node declares itself Inconsistent or before a
# joiner receives a state. That is "no lineage", not a second lineage. Counting
# it made single_lineage fail in run fdb9d32c...-63-5 (vtime 687), where node2
# was Inconsistent and node1/node3 shared the real cluster UUID. The node is
# still reported by cluster_reconverged (not Synced), and its raw UUID stays
# in the details as per_node_state_uuid. Any non-zero UUID that differs is
# still a fork, whether or not that node is Synced.
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def _lineage_of(state: dict | None) -> str:
    uuid = (state or {}).get("wsrep_local_state_uuid") or ""
    return "" if uuid == ZERO_UUID else uuid


def _lineages(states: dict) -> list[str]:
    return sorted({u for u in (_lineage_of(s) for s in states.values()) if u})


def run(mode: str) -> int:
    """Entry point. Never exits non-zero for an environment condition.

    Antithesis attaches a built-in property to each command's exit code, so a
    non-zero exit must mean a genuine bug -- not a locked journal or an
    unreachable node.
    """
    try:
        return _run(mode)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 0


def _all_synced(states: dict) -> bool:
    return len(states) == config.EXPECTED_CLUSTER_SIZE and all(
        db.is_synced(s) for s in states.values()
    )


def _run(mode: str) -> int:
    jr = journal.Journal("verify")
    try:
        # First and most important: put back any lever a killed driver was
        # holding. Until this finishes, the cluster may be logically isolated
        # or desynced by our own doing, and every check below would be
        # measuring the harness rather than PXC.
        repaired = leases.repair_expired(jr, force_all=True)

        started = time.time()
        deadline = started + config.VERIFY_BUDGET_SECONDS
        # First give the cluster a share of the budget to reconverge unaided.
        # Only if it has not, and no node is Primary at all, is the operator
        # recovery applied -- and the rest of the budget is then the bound on
        # reconverging from it.
        states = checks.wait_all_synced(started + config.VERIFY_BUDGET_SECONDS * 0.4)
        bootstrap = None
        if not _all_synced(states):
            bootstrap = checks.bootstrap_if_no_primary(jr, deadline)
            if bootstrap is not None and bootstrap["bootstrapped"]:
                oracles.saw_operator_bootstrap({"mode": mode, **bootstrap})
            states = checks.wait_all_synced(deadline)

        synced = {n: db.is_synced(s) for n, s in states.items()}
        all_synced = _all_synced(states)
        uuids = _lineages(states)

        base = {
            "mode": mode,
            "levers_repaired": repaired,
            "per_node_synced": synced,
            "per_node_state": {
                n: (s or {}).get("wsrep_local_state_comment") for n, s in states.items()
            },
            "cluster_sizes": {
                n: (s or {}).get("wsrep_cluster_size") for n, s in states.items()
            },
            "cluster_status": {
                n: (s or {}).get("wsrep_cluster_status") for n, s in states.items()
            },
            "unreachable_reasons": dict(db.LAST_ERROR),
            "state_uuids": uuids,
            "per_node_state_uuid": {
                n: (s or {}).get("wsrep_local_state_uuid") for n, s in states.items()
            },
            "operator_bootstrap": bootstrap,
        }

        # Does the cluster come back at all?
        oracles.cluster_reconverged(all_synced, base)

        # Plain stdout, not an SDK event: `snouty runs events` searches log
        # text but not custom events or assertion details, so this line is
        # the only way to count, across a run, the reds where no node answered
        # SQL at the final poll. bootstrap_if_no_primary cannot act on those,
        # and the one in run fdb9d32c...-63-5 (vtime 682) was Galera waiting
        # for an operator, not a PXC defect. Keep the text stable; triage
        # greps it. `states` is empty when the deadline passed before any
        # poll, which says nothing about the nodes.
        if states and not any(states.values()):
            print(
                f"[verify] reconvergence: no node answers SQL (mode={mode})",
                flush=True,
            )

        # Did it come back as ONE cluster? Deliberately a separate property:
        # split-brain and never-recovered are different bugs, and collapsing
        # them would make a triage report ambiguous about which happened.
        #
        # Gated on at least two nodes actually reporting a lineage: a single
        # reachable node cannot evidence a fork either way.
        reporting = [s for s in states.values() if _lineage_of(s)]
        if len(reporting) >= 2:
            oracles.single_lineage(len(uuids) <= 1, base)

        if not all_synced:
            # Nothing below can be evaluated soundly without all three nodes.
            # Returning 0 is correct: failing to converge is reported by the
            # assertion above, not by this command's exit code.
            return 0

        # Its own budget: sharing the already-consumed convergence deadline
        # meant that after a slow reconvergence this usually returned False,
        # and the run then carried a meaningless False in the details.
        cut_equal, cut = checks.wait_commit_cut_equal(
            time.time() + min(120.0, config.VERIFY_BUDGET_SECONDS / 4)
        )
        conns = checks.open_barrier_connections()
        if len(conns) < config.EXPECTED_CLUSTER_SIZE:
            return 0

        try:
            tables = list(config.CHECKSUM_TABLES)

            set_ok, set_details = checks.compare_table_set(conns)
            oracles.table_set_identical(set_ok, {**base, **set_details})

            schemas_ok, schema_details = checks.compare_schemas(conns, tables)
            oracles.schemas_identical(schemas_ok, {**base, **schema_details})

            if schemas_ok:
                content_ok, content_details = checks.compare_checksums(conns, tables)
                oracles.content_identical(
                    content_ok,
                    {**base, "commit_cut": cut, "commit_cut_equal": cut_equal, **content_details},
                )

            gtid_ok, gtid_details = checks.compare_gtid_executed(conns)
            oracles.gtid_identical(gtid_ok, {**base, **gtid_details})

            # The PK-less leg, held apart so a red on a documented limitation
            # cannot mask a red on the real invariant.
            nopk_ok, nopk_details = checks.compare_checksums(conns, [config.NOPK_TABLE])
            oracles.nopk_content_identical(nopk_ok, {**base, **nopk_details})

            acked_ok, failed_ok, minted_ok, ack_details = checks.reconcile(jr, conns)
            oracles.acked_writes_present(acked_ok, {**base, **ack_details})
            oracles.failed_writes_absent(failed_ok, {**base, **ack_details})
            if not minted_ok:
                oracles.harness_invariant_broken({**base, **ack_details})

            floor_ok, ceiling_ok, counter_details = checks.counter_bounds(jr, conns)
            oracles.counter_at_least_acked(floor_ok, {**base, **counter_details})
            oracles.counter_within_ceiling(ceiling_ok, {**base, **counter_details})

            unresolved = jr.unresolved_ddl()
            oracles.no_unresolved_ddl(
                not unresolved,
                {
                    **base,
                    "unresolved_ddl": unresolved[:10],
                    "ddl_completed": jr.ddl_completed(),
                    # Episodes left open because their driver was KILLED by
                    # this very command. Reported, never asserted: that is
                    # unknowable, not unresolved.
                    "ddl_abandoned_by_killed_driver": jr.abandoned_ddl(),
                },
            )

            # Vacuity guard. Gated on the workload having actually written
            # something: a timeline where first_ never landed a swarm profile
            # drives no traffic at all, and without this gate it would still
            # fire here and read as a clean, meaningful run.
            counts = jr.ack_counts()
            wrote_something = sum(counts.values()) > 0
            if wrote_something:
                oracles.terminal_comparison_ran(
                    {
                        "mode": mode,
                        "commit_cut": cut,
                        "commit_cut_equal": cut_equal,
                        "tables_compared": len(tables),
                        "ack_counts": counts,
                        "outcome_tally": jr.outcome_tally(),
                    }
                )
        finally:
            for conn in conns.values():
                db.close_quietly(conn)
        return 0
    finally:
        jr.finish()
