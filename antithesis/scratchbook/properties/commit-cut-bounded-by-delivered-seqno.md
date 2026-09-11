# commit-cut-bounded-by-delivered-seqno

**Focus:** Protocol contracts — GCS commit-cut trust; ordering assumptions between the
group layer and certification.
**Confidence:** High that the value is trusted with no release-build validation (code read);
medium on whether a same-version cluster can produce a violating value under faults.

## Claimed contract

The commit cut ("last_applied") broadcast as GCS_ACT_COMMIT_CUT is the group-wide minimum
applied seqno. `gcs/src/gcs_group.cpp:279-284` calls its computation "crucial for
consistency ... absolutely identical on all nodes". By construction it can never exceed any
node's highest *delivered* writeset seqno, and when a node is SYNCED it cannot exceed that
node's `last_committed()` (in-code comment at `replicator_smm.cpp:2328-2329`).

## Where the contract is trusted, not enforced (verified at commit f9ecb3e)

- `GcsActionSource::dispatch` (`galera/src/gcs_action_source.cpp:116-123`): unserializes the
  8-byte seqno; the only check is `assert(seqno >= 0)` — **compiled out under NDEBUG**
  (release builds force -DNDEBUG; sut-analysis §11 NDEBUG meta-issue).
- `ReplicatorSMM::process_commit_cut` (`galera/src/replicator_smm.cpp:2313-2339`):
  - If not SYNCED: `apply_monitor_.wait(seq)` — the **untimed** overload
    (`galera/src/monitor.hpp:365-373`: `while (last_left_ < seqno) lock.wait(...)` with no
    deadline). A seq greater than anything that will ever be delivered blocks the **single
    GCS recv thread forever** while holding the local monitor slot → no further actions are
    dispatched → node wedged but still a member → cluster-wide flow-control stall.
  - `assert(seq <= last_committed())` (:2332) — debug-only.
  - `cert_.purge_trxs_upto(seq, true)` (:2334): a too-large seq **empties the certification
    index** while every other node keeps its entries → subsequent certification verdicts
    diverge silently (no vote fires — votes only cover apply *errors*).
- Related wire-trust asymmetry (same axiom family, sut-analysis §11 focus 11 §5): the
  version-gated last_applied monotonicity guard is buggy in gcs protocols 2-4, so
  mixed-protocol clusters are the known producer of non-monotonic values.

## Failure scenario

Any defect or fault interleaving that makes one node's contribution to the commit-cut
computation wrong (membership churn racing JOIN/SYNC/LAST messages; a joiner reporting a
stale/forward-adjusted `sst_seqno_`, cf. PXC-only forward adjust replicator_str.cpp:1242-1277)
produces a commit cut ahead of a peer's delivered seqno. On that peer the outcome is either
a permanent recv-thread block (liveness) or a silent cert-index purge (safety/divergence) —
both invisible to inconsistency voting.

## Suggested assertions (all missing — SUT-side SDK instrumentation)

- **Always (SUT-side, at process_commit_cut entry):** `seq >= 0 && seq <= <highest
  delivered writeset seqno on this node>` (available as `apply_monitor_.last_left()` +
  in-flight window; when SYNCED, `seq <= last_committed()`). Message: "received commit cut
  within locally delivered range". This is the release-build replacement for the two
  compiled-out asserts.
- **Always (SUT-side, cross-message):** commit cut is monotonically non-decreasing per
  group incarnation. Message: "commit cut monotonic".
- **Sometimes (SUT-side):** "commit cut processed while node not SYNCED" — the branch that
  takes the untimed wait (:2326-2331) is the dangerous one; make Antithesis reach it under
  membership churn.
- **Workload-side liveness companion:** `wsrep_last_committed` advances on every node while
  the workload commits (a wedged recv thread freezes it) — cheap external oracle.

## Assertion-type rationale

`Always` for the bound and monotonicity: they are wire-format invariants that must hold on
every received message — the exact "documented guarantee not enforced" shape (enforced only
by debug asserts that release builds compile out). `Sometimes` for the not-SYNCED branch
because it is a rare, high-value state, not an invariant.

## Fault requirements

Network faults + membership churn (default-on) exercise the computation. Node
termination/restart would strengthen it considerably (joiners are where seqno accounting is
forward-adjusted) — **flag: stronger with node termination enabled**. Mixed gcs protocol
versions (rolling upgrade) are the known-buggy producer but are likely out of scope for a
single-version harness.

## Open questions

- Can a same-version, same-config cluster produce a violating commit cut at all?
  `(partial: the computation is a min over counted nodes' self-reports with a monotonic
  guard, so violation requires a node to over-report its own applied seqno; the one known
  over-report vector is version-gated off in 8.4.10; not every last-applied report site was
  exhaustively audited — the assertion stays as a zero-cost tripwire)`

Resolved (see Investigation Log): the `sst_seqno_` forward adjust cannot fire in a
same-version harness (5.7-donor workaround, gated `str_proto_ver < 3`; 8.4.10 negotiates
str proto 3), so that trigger drops out of this property's scenario list.

### Investigation Log

#### Can a same-version cluster produce a violating commit cut?

- Examined: `gcs/src/gcs_group.cpp:242-340` (`group_count_stateless`,
  `group_count_last_applied`, `group_redo_last_applied`), `:106`/`:150` (group last_applied
  init from act_id_), `gcs/src/gcs_act_proto.hpp:34` (homogeneous cluster negotiates GCS
  protocol 6 → the buggy proto 2-4 recalculation guard is not in play; monotonic guard at
  `:329-333` active).
- Found: the group-wide commit cut is min over counted (SYNCED-ish, stateful) nodes'
  *self-reported* applied seqnos, clamped monotonically non-decreasing. By construction the
  min cannot exceed any counted node's own report; a value ahead of some node's delivered
  state therefore requires either (a) that node to be *not counted* — joiners/stateless, which
  is the by-design not-SYNCED untimed-wait branch (the node catches up from the live
  stream/IST), or (b) a counted node over-reporting its own applied seqno.
- Not found: any same-version over-report producer. The one previously suspected vector
  (`sst_seqno_` forward adjust) is version-gated off (next entry). An exhaustive audit of
  every last-applied report site (GCS_MSG_LAST producers, state-message carry-over across
  conf changes) was not completed.
- Conclusion: tagged `(partial)` — likely unproducible in a homogeneous 8.4.10 cluster; the
  SUT-side `Always` remains worthwhile as a free tripwire that converts a hypothetical
  recv-thread wedge / cert-purge divergence into a first-class property violation.

#### Can the PXC-only sst_seqno_ forward adjust report ahead of delivered state?

- Examined: `galera/src/replicator_str.cpp:1229-1291` (the adjust and its gate),
  `galera/src/replicator_smm.hpp:1041-1057` (protocol table: repl proto 10/11 → str proto 3),
  `galera/src/replicator_smm_params.cpp:32` (`MAX_PROTO_VER = 11`).
- Found: the adjust `sst_seqno_ = cc_seqno` executes only when
  `str_proto_ver < 3 && sst_seqno_ < cc_seqno && req->ist_len() == 0` — an explicit 5.7-donor
  workaround (comment cites PXC-2213 lineage). A homogeneous 8.4.10 cluster negotiates repl
  protocol 11 → str proto 3, so the branch is dead code in the harness.
- Conclusion: resolved — cannot fire without a 5.7-era donor; drop it from this property's
  trigger list (it remains a mixed-version-only concern, out of harness scope).
