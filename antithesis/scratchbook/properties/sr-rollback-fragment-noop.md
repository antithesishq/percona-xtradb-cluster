# sr-rollback-fragment-noop — spurious SR rollback fragments are no-ops and fragments apply exactly once

**Focus area:** Idempotency and Replay. **Commit:** f9ecb3ebe8ff (branch 8.4).
**Confidence:** Medium — all three code paths confirmed; which membership/BF interleavings turn
the "expected spurious" case into real divergence is unproven (the code itself hedges: "it may be
an indication of a bug too").

## Claim under test

Streaming replication (`wsrep_trx_fragment_size > 0`) replicates a transaction as multiple
fragments, with rollback delivered as its own writeset. The provider *cannot always tell* whether
an interrupted SR transaction's pending fragment certified, so it deliberately over-sends
rollback fragments (at-least-once delivery of rollbacks). The idempotency contract:

1. A rollback fragment for a transaction with no streaming-applier context on the receiving node
   is a **no-op** (logged as a dummy writeset to keep seqno symmetry) — expected and harmless.
2. Each data fragment is applied **exactly once** per node into `mysql.wsrep_streaming_log`, and
   a rolled-back SR transaction leaves zero rows there and zero data effects on every node.
3. An SR transaction that commits produces its full effects exactly once per node, even when the
   originator was BF-aborted mid-stream and replayed.
4. After crash recovery, the per-(server_id, trx_id) fragment sets are consistent cluster-wide:
   either the transaction survives everywhere or is rolled back everywhere.

## Code paths

- Spurious rollback handling: `wsrep-lib/src/server_state.cpp:295-328` — rollback fragment with
  no applier context → debug-level message ("unnecessary rollback fragments may be delivered
  here", referencing `transaction::certify_fragment()` comments) → `log_dummy_write_set`.
  With context → `rollback_fragment` (consumes the applier).
- Missing-context degradation on *data/commit* fragments: `server_state.cpp:379-410` (middle
  fragment) and `:411-454` (commit fragment) — no context → **warning + dummy writeset** ("rapid
  group membership changes may cause streaming transaction be rolled back before commit fragment
  comes in. Although this is a valid situation ... it may be an indication of a bug too").
  If the missing context is *not* legitimate (context lost by a bug), the node dummies a commit
  every other node applies → silent divergence with no vote.
- Fragment certification write-before-certify: `wsrep-lib/src/transaction.cpp:1530-1740`
  (`certify_fragment`): fragment written to stable storage with seqno UNDEFINED (:1618-1651),
  then provider certify (:1664+), then seqno update + storage commit. Crash between certify and
  commit → local row has NULL seqno.
- Recovery deletion of the half-certified fragment: `sql/wsrep_schema.cc:1212-1218`
  (`recover_sr_transactions`): "This is possible if the server crashes between inserting the
  fragment into table and updating the fragment seqno after certification" → row deleted —
  while every peer holds the *certified* fragment for this trx. Cross-node fragment-set
  asymmetry by design; consistency then depends entirely on orphan cleanup rolling the trx back
  everywhere.
- Orphan cleanup: `server_state.cpp:1567-1678` (`close_orphaned_sr_transactions`) — keyed off
  `equal_consecutive_views`; on adopt failure proceeds anyway ("leaving stale entries ...
  removed manually" :1645-1648).
- Streaming rollback of the originator: `transaction.cpp:2036-2055` (`streaming_rollback`) —
  sends the rollback fragment; the BF-aborter can wait unbounded on the victim's condvar.
- Replay of SR: `sql/wsrep_high_priority_service.cc:1082-1086` — replayer re-applies stored
  fragments via `wsrep_schema->replay_transaction` before the final writeset; doubling seam if
  fragments were already applied by a streaming applier that wasn't torn down.
- Disabled `#if 0` double-commit assert: `wsrep-lib/src/transaction.cpp:568-582` — the project
  itself suspected a crash window orphaning `wsrep_streaming_log` rows.
- Test-suite gap: the SR crash-consistency suite galera_3nodes_sr GCF-810A/B/C sources include
  files that do not exist in the repo — un-runnable (sut-analysis §9.5). wsrep-lib SR crash
  points exist for debug builds (`crash_replicate_fragment_*`, `crash_apply_cb_*`,
  transaction.cpp:307-1697, server_state.cpp:134-199).

## Failure scenarios

- **Rollback applied twice / to the wrong incarnation:** duplicate rollback fragment arrives
  after the transaction was replayed and committed → rollback_fragment consumes a *live* applier
  context → committed effects partially undone on one node.
- **Commit fragment dummied on one node:** membership churn drops the streaming applier context
  on node A only (view-change orphan cleanup raced with the in-flight commit fragment) → A logs a
  dummy, B and C commit → silent divergence; no inconsistency vote because nothing errored.
- **Fragment double-apply on replay:** replayer re-applies stored fragments while the original
  streaming applier already applied them (context not found vs found race) → duplicate rows in
  `wsrep_streaming_log` → duplicate effects at commit.
- **Crash-window asymmetry:** NULL-seqno fragment deleted locally at recovery while certified on
  peers; if orphan cleanup does not roll the trx back on peers (adopt failure path), stale
  fragments and locks persist on peers only.

## Suggested assertions (all missing)

- **Primary (workload, Always):** after any SR transaction resolves (commit or rollback observed
  by the workload) and the cluster is quiescent/synced: (a) data effects present exactly once on
  all nodes or on none, matching the client outcome; (b) `SELECT COUNT(*) FROM
  mysql.wsrep_streaming_log` is 0 on every node when no SR transaction is in flight. `Always`.
- **Coverage (SUT-side, missing, Sometimes):** at `server_state.cpp:316` —
  `Sometimes(spurious rollback fragment dummied)`: proves the at-least-once rollback delivery
  path actually ran (it is the documented-expected case this property stresses).
- **SUT-side (missing, AlwaysOrUnreachable):** at `server_state.cpp:397/:439` (missing-context
  dummy for data/commit fragments) — assert the transaction is *known rolled back* (present in
  the rollbacked-trx bookkeeping) rather than merely unknown. The code comment admits it can't
  distinguish "valid situation" from "bug"; instrumentation here is exactly the discriminator.
  `AlwaysOrUnreachable` because non-SR workloads never reach it.
- **Coverage (workload, Sometimes):** `Sometimes(an SR transaction was BF-aborted mid-stream and
  the client saw deadlock or successful replay)` — the interesting generator condition.

## Fault / workload requirements

- Workload must enable SR: sessions with `wsrep_trx_fragment_size > 0` (off by default) running
  multi-row transactions, mixed with conflicting short transactions to force mid-stream BF aborts.
- Membership churn (network partition, default-on) concurrent with in-flight SR transactions
  drives the missing-context paths.
- The crash-window scenario (NULL-seqno fragment) needs **node termination** (often disabled) or
  debug-build crash points (`crash_replicate_fragment_after_certify`) — flag: without kill
  faults, claims 1-3 are testable, claim 4 is not.

## Open questions

None — all three resolved; see Investigation Log. Net effect: failure scenario 1
("rollback applied to a replayed live incarnation") is unreachable by construction — drop
it from the recipe; the property's weight shifts to the missing-context dummy-demotion
paths (scenario 2), the recovery-applier interplay after restart, and the adopt-failure /
s_prepared residuals of orphan cleanup.

### Investigation Log

#### Can a rollback fragment for trx T meet T's replayed live incarnation on the same node?

- Examined: `wsrep-lib/src/transaction.cpp:684-745` (rollback path state machine),
  `:2036-2060` (`streaming_rollback` — asserts `state_ != s_must_replay`),
  `sql/sql_class.h:3455-3488` (trx_id = monotonic query_id),
  `replicator_smm.cpp:719-725` (FC bypass for rollback fragments).
- Found: replay and rollback-fragment emission are mutually exclusive by the state
  machine: a BF-aborted trx that is `certified()` goes `s_must_replay` and never calls
  `streaming_rollback`; only uncertified/voluntary aborts send the rollback fragment, and
  those never replay. trx_ids are monotonic within an incarnation, so no later transaction
  can alias (server_id, T). The FC bypass affects only send admission on the origin;
  delivery position is still total-order, serialized against T's data fragments.
- Conclusion: resolved — scenario 1 is unreachable by construction; the at-least-once
  over-delivery can only hit the no-context (dummy) path, which is the documented-expected
  case the `Sometimes` probe covers.

#### Does `rollback_fragment` tear down recovery-created streaming appliers the same way as live ones?

- Examined: `sql/wsrep_schema.cc:1122-1250` (`recover_sr_transactions` registers recovery
  appliers via `server_state.start_streaming_applier(server_id, transaction_id, applier)`
  into the same `streaming_appliers_` map, created by the same
  `wsrep_create_streaming_applier` factory), rollback-fragment handling
  (`server_state.cpp:295-328` — `find_streaming_applier` lookup is map-based, blind to how
  the applier was created), `close_orphaned_sr_transactions` (same map scan).
- Found: identical keying, identical map, identical factory type, identical
  rollback/teardown code path; recovery re-apply runs with binlog/wsrep off on a storage
  THD, so it does not re-replicate.
- Conclusion: resolved — same teardown semantics; claim 4 does not have a
  post-recovery-only failure mode on the designed path.

#### Does orphan cleanup fire on every view sequence rapid flapping can produce?

- Examined: `server_state.cpp:1055-1084` (`on_primary_view` calls
  `close_orphaned_sr_transactions` on EVERY primary view), `:1567-1600` (per-applier
  rollback condition `equal_consecutive_views || not is_member(server_id)`; comment noting
  the equal-membership clause is retained for backwards compatibility and the
  rollback_event_queue mechanism now covers the non-prim sandwich for local clients).
- Found: the origin-absent check is evaluated against the *current* membership at every
  primary view, so no flap ordering can permanently skip cleanup of an orphan whose origin
  is gone (restarted origins always carry a new UUID/incarnation). The
  `equal_consecutive_views` key is only the trigger for the origin-still-present sandwich
  case, and even there it compares the current view against the *previous primary* view,
  not "consecutive" wall-clock views.
- Not found: a reachable flap sequence that starves cleanup, short of the two designed
  exemptions: `adopt_error` (stale table rows, "removed manually") and `s_prepared` (XA)
  transactions.
- Conclusion: resolved — the slow-leak variant reduces to the adopt-failure/XA residuals,
  which the row-count-returns-to-zero assertion already catches.

## Synthesis refinement (2026-09-10)

SR UN-GATED (see sr-fragment-cross-node-agreement note): session-set fragment size makes the non-crash legs (rollback-churn, dummy-demotion under membership churn) v1-viable; crash sub-case remains kill-channel-gated.
