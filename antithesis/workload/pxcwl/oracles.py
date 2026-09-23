"""Every Antithesis SDK assertion in this project.

One function per callsite, each with its property name as an INLINE CONSTANT
STRING LITERAL. Nothing here builds a name at run time, passes one through a
variable, or reuses one across callsites. Those are hard requirements, not
style: Antithesis statically scans this source before any run to pre-catalog
every assertion, which is what makes an unfired reach claim reportable at all.
A name built at run time is invisible to that scan, and a duplicated name
collapses two distinct callsites into one catalog entry.

Only the core assertion forms are used, because that is all there is:
``antithesis==0.3.1`` exposes ``always``, ``always_or_unreachable``,
``sometimes``, ``reachable`` and ``unreachable`` and **no rich numeric helpers**
(verified against the published SDK source, not assumed). So the two counter
bounds are written as plain ``always`` with the operands passed explicitly in
the details.

Reach claims here follow one rule without exception: a reach claim asserts the
PRECONDITION that makes a bug possible, never the violation itself. A
``sometimes`` that only fires when an ``always`` fails would report as failing
on a correct system, which is backwards. Every claim below still fires when PXC
is behaving.
"""

from __future__ import annotations

from typing import Any

from antithesis.assertions import (
    always,
    always_or_unreachable,
    reachable,
    unreachable,
)

Details = dict[str, Any]


# ==========================================================================
# Terminal oracles. Reached from eventually_ and finally_, where all fault
# injection has stopped and the cluster has been quiesced.
# ==========================================================================


def schemas_identical(ok: bool, details: Details) -> None:
    always(ok, "replicated table definitions are identical on every Synced node", details)


def table_set_identical(ok: bool, details: Details) -> None:
    """Every node agrees on which tables and indexes exist.

    Needed because the DDL generator works on scratch tables that are NOT in
    the checksum set, so without this nothing at all compares the results of
    TOI DDL across nodes -- a table present on one node and absent on another
    would score as identical.
    """
    always(ok, "every Synced node agrees on the set of tables and indexes", details)


def content_identical(ok: bool, details: Details) -> None:
    """The terminal oracle.

    PXC has no mechanism of its own to detect successful-but-divergent apply:
    inconsistency voting fires only on apply *errors*, and a silent divergence
    never triggers a vote at all. This comparison is the only detector for the
    dominant recurring bug class in this system.
    """
    always(ok, "replicated table content is identical on every Synced node", details)


def gtid_identical(ok: bool, details: Details) -> None:
    """Sibling terminal oracle on an independent plane.

    Rows and GTID sets can diverge separately, so neither check subsumes the
    other; both run in the same quiesced pass.
    """
    always(ok, "gtid_executed is identical on every Synced node", details)


def acked_writes_present(ok: bool, details: Details) -> None:
    always(ok, "every acknowledged write is present on every Synced node", details)


def failed_writes_absent(ok: bool, details: Details) -> None:
    always(ok, "a cleanly failed write is absent from every Synced node", details)


def counter_at_least_acked(ok: bool, details: Details) -> None:
    """Lower bound. Unacknowledged increments may have landed, so only the
    acknowledged count is a floor -- asserting equality here would fire every
    time the environment dropped a request."""
    always(ok, "counter total is at least the acknowledged increment count", details)


def counter_within_ceiling(ok: bool, details: Details) -> None:
    """Upper bound. Catches double-apply, which is what a broken IST-overlap
    gate or a replayed writeset looks like from outside."""
    always(ok, "counter total never exceeds acknowledged plus unresolved increments", details)


def nopk_content_identical(ok: bool, details: Details) -> None:
    """Held as its own property so that a red on this documented PXC
    limitation cannot mask a red on the real content invariant."""
    always_or_unreachable(
        ok, "primary-key-less table content is identical on every Synced node", details
    )


def cluster_reconverged(ok: bool, details: Details) -> None:
    always(
        ok, "the cluster returns to three Synced nodes after fault injection stops", details
    )


def single_lineage(ok: bool, details: Details) -> None:
    """Kept separate from reconvergence on purpose: split-brain and
    never-recovered are completely different bugs and must not share a
    verdict."""
    always(ok, "all nodes share one cluster state UUID at terminal convergence", details)


def no_unresolved_ddl(ok: bool, details: Details) -> None:
    always_or_unreachable(
        ok, "no data-definition statement is left unresolved after reconvergence", details
    )


def terminal_comparison_ran(details: Details) -> None:
    """Vacuity guard for every assertion above.

    Without it, a run in which quiesce always timed out would read as clean:
    the Always assertions would simply never have been evaluated.
    """
    reachable("terminal verification completed a quiesced three-node comparison", details)


# ==========================================================================
# Continuous checkers. Reached from anytime_, alongside the drivers and with
# faults active.
# ==========================================================================


def recv_queue_bounded(ok: bool, details: Details) -> None:
    always(
        ok, "a Synced node keeps its receive queue below the flow-control bound", details
    )


def commit_progress_not_frozen(ok: bool, details: Details) -> None:
    """One watchdog for a whole family: a leaked ordering-monitor slot, an
    unreleased flow-control pause, and a wedged applier all present the same
    way from outside -- every node claims health while nothing commits."""
    always(
        ok, "cluster commit progress never freezes while every node reports Synced", details
    )


def single_primary_component(ok: bool, details: Details) -> None:
    always(ok, "at most one primary component exists at any observation", details)


def green_node_can_commit(ok: bool, details: Details) -> None:
    """The health surface must tell the truth.

    The shipped check never consults wsrep_ready, flow-control state, or queue
    depth, so a node can advertise availability while every write on it hangs.

    Only evaluated when the probe has positive evidence either way: a write it
    landed inside the green window, or an unbroken run of refused writes
    covering it. "No successful probe is on record" is not the same claim --
    see probe._merge_progress for how it came to be one in run de51f0b9-63-0.
    """
    always(
        ok,
        "a node advertising availability for a sustained window has committed a write in that window",
        details,
    )


def saw_flow_control(details: Details) -> None:
    reachable("flow control was engaged by some node", details)


def saw_membership_churn(details: Details) -> None:
    reachable("the cluster was observed with fewer than three members and later returned to three", details)


def saw_state_transfer(details: Details) -> None:
    reachable("a state transfer was served to a joining node", details)


def saw_failed_state_transfer(details: Details) -> None:
    """The failure side of the transfer path.

    ``saw_state_transfer`` only ever fires on success, so without this the
    report cannot distinguish "SST worked every time" from "SST failed and the
    node died before anyone asked".
    """
    reachable("a state transfer failed on a node that kept serving", details)


def saw_ist_fallback(details: Details) -> None:
    """The graceful arm of a failed SST: code 11, datadir intact, retry by IST.

    Held apart from the plain failure claim because these are opposite
    outcomes. Folding them together would let the good path mask the bad one.
    """
    reachable(
        "a failed state transfer fell back to IST instead of killing the node", details
    )


def saw_inconsistency_verdict(details: Details) -> None:
    """A node the cluster (or the node itself) judged inconsistent, still up.

    Attribution for the terminal liveness oracles: when reconvergence goes red,
    this says whether an inconsistency verdict is why.
    """
    reachable("a node was declared inconsistent and was still serving", details)


# ==========================================================================
# In-flight checkers and reach claims. Reached from parallel_driver_.
# ==========================================================================


def locking_read_outcome_legal(ok: bool, details: Details) -> None:
    always(ok, "a locking read ends in a result set or a documented lock error", details)


def applier_resize_converged(ok: bool, details: Details) -> None:
    always_or_unreachable(
        ok, "applier thread count reaches the configured setpoint after a resize", details
    )


def maint_mode_honors_intent(ok: bool, details: Details) -> None:
    """One direction only, and the name says which.

    Renamed from "pxc_maint_mode matches the last operator-set value until the
    operator changes it", which promised a two-way equality the property never
    meant. SHUTDOWN and the forced-FLIP to MAINTENANCE are both legitimate
    server-side overrides, so only the revert-to-DISABLED arm is a violation.
    See levers.maint_mode_cycle for the carve-outs and why a view change is
    not one of them.
    """
    always_or_unreachable(
        ok,
        "an operator-set pxc_maint_mode=MAINTENANCE is never reverted to DISABLED",
        details,
    )


def graceful_shutdown_bounded(ok: bool, details: Details) -> None:
    always_or_unreachable(
        ok, "a graceful shutdown closes the port within the configured bound", details
    )


def sync_wait_read_sees_acked_write(ok: bool, details: Details) -> None:
    """The only continuous cross-node check in the workload.

    The write was acknowledged before this read began, and the read is gated on
    a non-zero ``wsrep_sync_wait``, so the row must be visible on the other
    node. The terminal oracle cannot substitute for this: it runs once, at the
    end, after everything has settled.
    """
    always(ok, "a sync-wait read on another node sees an acknowledged write", details)


def saw_certification_conflict(details: Details) -> None:
    reachable("a certification conflict returned ER 1213 to a client", details)


def saw_skip_locked_conflict(details: Details) -> None:
    """Native InnoDB SKIP LOCKED cannot deadlock, so a 1213 here is
    specifically the BF-wait conversion path."""
    reachable("a SKIP LOCKED statement returned ER 1213", details)


def saw_not_ready_rejection(details: Details) -> None:
    """Since this workload never sets wsrep_reject_queries, a 1047 carrying the
    WSREP message implies the node was genuinely unready or non-primary."""
    reachable("a client received ER 1047 from a node that was not ready", details)


def saw_streaming_transaction(details: Details) -> None:
    reachable("a streaming-replication transaction committed with fragments", details)


def saw_large_writeset(details: Details) -> None:
    reachable("a writeset larger than four megabytes was committed", details)


def saw_long_transaction(details: Details) -> None:
    """Deliberately NOT folded into the large-writeset claim: a hundred small
    witness inserts is a few kilobytes, so reusing that name would have made
    the claim untrue at one of its callsites."""
    reachable("a transaction of at least one hundred statements committed", details)


def saw_ddl_under_load(details: Details) -> None:
    reachable("a data-definition statement completed under concurrent replicated writes", details)


def saw_unknown_outcome(details: Details) -> None:
    """Proves the three-state protocol is genuinely exercised.

    If this never fires, every write resolved cleanly, the unknown bucket
    stayed empty, and the bounded assertions were really testing exact
    equality -- which means they were never tested under the conditions they
    exist for.
    """
    reachable("a write outcome was unknown after a connection failure", details)


# ==========================================================================
# Forbidden paths.
# ==========================================================================


def harness_invariant_broken(details: Details) -> None:
    """Reached only when the workload detects its own bookkeeping is impossible
    -- e.g. a node reports a write key this workload never minted. That is
    either a harness bug or something very strange in the SUT, and either way a
    human should look before trusting any other verdict in the run."""
    unreachable("workload observed a write key it never minted", details)
