# bf-replay-commits-exactly-once — BF-aborted committing transaction replays and commits exactly once

**Boundary note (synthesis):** this property and `trx-replay-never-fatal` share the replay
machinery. The catalog boundary: **this property owns the effects contract** — exactly-once
effects on every node, single correct client OK (workload `Always` + `Sometimes` on
`wsrep_local_replays`); `trx-replay-never-fatal` owns replay termination and carries the
`Unreachable` at the fatal funnel (`wsrep_high_priority_service.cc:1054-1057`) — do not
duplicate that assertion here.

**Focus area:** Idempotency and Replay. **Commit:** f9ecb3ebe8ff (branch 8.4).
**Confidence:** High — mechanism read end-to-end in code; a dedicated regression test
(galera_UK_conflict) encodes the exact historical failure ("replayer BF-aborted a second time").

## Claim under test

When a local transaction that has already passed certification (has a global seqno; peers will
apply it) is BF-aborted by a conflicting applier before it finishes committing, the server must
*replay* the same writeset locally — not re-execute the SQL — and the replay must succeed:

1. The transaction's effects appear **exactly once** on every node (the replay applies the
   already-replicated writeset at the same global seqno; the first attempt's InnoDB changes are
   rolled back first).
2. The client receives a single OK carrying the original affected-rows/last-insert-id
   (shadowed diagnostics area), never a duplicate execution and never a spurious error.
3. A replaying transaction is never BF-aborted again (the galera_UK_conflict regression class).

## Code paths

- Decision to replay: `wsrep-lib/src/transaction.cpp:709-713` (`before_rollback`: `s_must_abort`
  + `certified()` → `s_must_replay`) and `:856-917` (`after_statement`: `s_must_abort/s_cert_failed`
  → `bf_rollback()`; if state became `s_must_replay` → `replay(lock)`). Rollback of the first
  attempt's changes happens *before* replay — this is the local-idempotency guarantee.
- Provider replay: `percona-xtradb-cluster-galera/galera/src/replicator_smm.cpp:1147-1250`
  (`replay_trx`). Note `:1170-1171`: "We set submit NULL trx pointer below to avoid interrupting
  replaying in any monitor during replay" — the no-second-BF-abort protection.
  `:1205` `commit_monitor_.wait(ts.global_seqno() - 1)` orders replay behind all preceding
  commits. `:1218-1219`: "failure to replay own trx is certainly a sign of inconsistency, not
  trying to catch anything here"; a `gu::Exception` during replay → `on_inconsistency()` →
  `WSREP_NODE_FAIL` (`:1233-1237`).
- Server-side replayer: `sql/wsrep_high_priority_service.cc:927-1059` (`Wsrep_replayer_service`).
  Dtor `:1047-1058`: success → `my_ok(orig_thd, m_da_shadow.affected_rows, ...)`;
  cert-failed → `ER_LOCK_DEADLOCK`; **any other outcome → `unireg_abort(1)`** — whole-node
  suicide with exit status 1, which the shipped systemd units do not restart.
- Monitor seam that forces replays: `replicator_smm.cpp:3663-3682`
  (`handle_local_monitor_interrupted`) — a committing trx gets BF_ABORT *without* cancelling the
  local monitor; the replay must re-enter it. (Investigated: replay always runs via wsrep-lib
  teardown paths, or the node dies in the fatal funnel — see Investigation Log and
  `local-monitor-freed-after-bf-abort`.)
- Regression test encoding the bug: `mysql-test/suite/galera/t/galera_UK_conflict.test` —
  two applier writesets hit false-positive UK GAP-lock conflicts against a local committer;
  scenario 1: replayer must not be BF-aborted a second time by the second applier; scenario 2:
  replayer must not abort a later applier already waiting in commit order. Both are
  debug-sync-timed — exactly the interleavings Antithesis explores natively under load.
- Observability: `wsrep_local_replays` status counter (`local_replays_`, replicator_smm.cpp:1201).

## Failure scenarios

- **Doubled effects:** replay path re-applies while the first attempt's changes were not fully
  rolled back (e.g. non-transactional side effects, or a bug in the bf_rollback→replay hand-off)
  → row present twice / counter incremented twice on the origin node only → silent divergence
  (no vote fires: apply succeeded on all nodes).
- **Replayer BF-aborted again:** second conflicting applier BF-aborts the replaying transaction
  (the galera_UK_conflict bug). Outcome per the dtor: replay status neither success nor
  cert-failed → `unireg_abort(1)` → node down and not restarted by shipped units.
- **Lost effects:** replay returns cert-failed locally while peers applied the writeset (verdicts
  must be identical; if not, the origin misses a transaction all peers have).
- **Wedged local monitor:** BF abort during commit-monitor wait with replay never executing
  (node leaving / SST_CANCELED) → local monitor gap → cluster-wide certification stall.

## Suggested assertions (all missing — no SDK instrumentation exists)

- **Primary (workload, Always):** "acknowledged write appears exactly once on every node" —
  workload writes rows with unique client-generated ids under heavy cross-node conflict
  (hot rows + secondary unique keys to trigger GAP-lock false positives, per galera_UK_conflict),
  records which statements got OK, and asserts each acked id occurs exactly once on all nodes
  and each errored id occurs zero times. `Always` because it must hold on every check.
- **Coverage (workload, Sometimes):** `Sometimes(wsrep_local_replays increased during the run)` —
  without observed replays the primary property tests nothing interesting.
- **SUT-side (missing, Unreachable):** at `wsrep_high_priority_service.cc:1054-1057` (the
  `assert(0); unireg_abort(1)` branch) — "replay ended with unexpected status" is an
  impossible-state guard today; an SDK `Unreachable` there converts node suicide into a
  first-class property signal.
- **SUT-side (missing, Sometimes):** at `replicator_smm.cpp:1176` (replay from S_REPLICATING,
  i.e. BF-abort landed before certification returned) — the rarest replay entry state.

## Fault / workload requirements

- No special faults required: needs sustained multi-node conflicting DML +
  `wsrep_applier_threads > 1` (default is 1 — must be raised, see sut-analysis §12).
  Network delay/partition (default-on) widens the BF-abort windows (client mutex is unlocked
  across the provider certify call, transaction.cpp:1823 per sut-analysis §3.2).
- `wsrep_retry_autocommit=0` in at least one variant, so replay errors surface to the client
  instead of being masked by the silent retry loop (see retry-autocommit-exactly-once).
- Node termination NOT required, but the systemd no-restart semantics matter for triage:
  a failed property here may present as "node exited with status 1".

## Open questions

None — all three resolved; see Investigation Log.

Key resolutions folded into the property:

- **No bypass of the replayer's BF-abort protection was found.** MDL-level aborts
  (`wsrep_abort_thd`) funnel through `wsrep_bf_abort` →
  `client_state.bf_abort/total_order_bf_abort` → the wsrep-lib state gate
  (transaction.cpp:1017-1082), which refuses BF abort in `s_must_replay`/`s_replaying`
  under the client-state mutex; galera `abort_trx` independently returns
  `WSREP_NOT_ALLOWED` for `S_MUST_REPLAY` (replicator_smm.cpp:963-970). The
  galera_UK_conflict scenario-1 protection is thus victim-side and layered — the
  Unreachable at the fatal funnel is safe to add unconditionally. The residual channel is
  different in kind: an MDL conflict where the *replayer THD* is the granted BF side
  lands in the BF-BF `unireg_abort` funnel (node suicide, owned by `no-mdl-bf-bf-abort`).
- **No silent permanent local-monitor block.** wsrep-lib makes rollback-without-replay
  structurally unreachable (state matrix `mr`→`re` only; `after_statement` and
  `client_state::close()` both drive `s_must_replay` into `replay()`); replay failure
  lands in the fatal funnel (node death, not a silent wedge). Details and the
  SST_CANCELED nuance are owned by `local-monitor-freed-after-bf-abort`.
- **XA is a separate concern:** ordered XA shares the normal replay path; unordered XA
  replay-commit hits PXC's `commit_by_xid` stub (`assert(0); error_not_implemented`,
  wsrep_client_service.cc:330-334) — client gets a commit error, trx returns to
  s_prepared. The exactly-once contract for XA rests on partially unimplemented
  machinery; exclude XA from this property's workload (day-one) or split a dedicated XA
  property later.

### Investigation Log

#### Can MDL-path BF aborts bypass the NULL-trx replay protection?

- Examined: `wsrep_abort_thd` (sql/wsrep_thd.cc:331-359) and `wsrep_bf_abort`
  (:362-385); `wsrep::transaction::bf_abort`/`total_order_bf_abort`
  (wsrep-lib/src/transaction.cpp:1017-1155) and the state-transition matrix
  (:1392-1407); `ReplicatorSMM::abort_trx` (galera replicator_smm.cpp:936-1066);
  `wsrep_innobase_kill_one_trx` (ha_innodb.cc:24385-24470).
- Found: every server-side abort path (InnoDB record-lock kill, MDL abort, KILL
  statement) converges on `client_state.bf_abort/total_order_bf_abort`, whose allowed-
  state list {executing, preparing, prepared, certifying, committing} excludes
  s_must_replay and s_replaying, checked under the client-state mutex. Galera-side
  `abort_trx` returns NOT_ALLOWED for S_MUST_REPLAY. The NULL-trx monitor submission
  (replicator_smm.cpp:1170-1171) is a third, provider-internal layer.
- Not found: any abort path that flips transaction state without the gate.
- Conclusion: resolved — no bypass; the Unreachable at the replay funnel cannot be
  triggered by a legitimate second BF abort, so any firing is a real defect (or a
  designed inconsistency-suicide, see trx-replay-never-fatal's log).

#### Is the local monitor permanently blocked if replay never runs after `handle_local_monitor_interrupted`?

- Examined: see the full trace in `local-monitor-freed-after-bf-abort.md` (Investigation
  Log) — wsrep-lib teardown paths (after_statement transaction.cpp:906-916,
  client_state::close client_state.cpp:57-81, before_rollback :736, rollbacker
  wsrep_thd.cc:212-292) and galera `release_rollback` (replicator_smm.cpp:1605-1675).
- Found: rollback-without-replay is structurally unreachable through wsrep-lib; replay
  failure → unireg_abort(1). The silent-wedge scenario reduces to node death (view
  change frees the cluster), except the SST_CANCELED writeset-drop path which bypasses
  the local monitor by design on a node that is expected to shut down.
- Conclusion: resolved — the liveness twin ("cluster certifies after BF-abort storms")
  stays as a cheap regression guard, owned by `local-monitor-freed-after-bf-abort`.

#### Does the exactly-once contract hold for XA?

- Examined: after_statement XA dispatch (transaction.cpp:906-916); xa_replay /
  xa_replay_commit (:1319-1361); PXC stubs `commit_by_xid` and `replay_unordered`
  (sql/wsrep_client_service.cc:306-334).
- Found: ordered XA replay uses the same replay()/Wsrep_replayer_service path as normal
  transactions (contract inherited). Unordered XA replay-commit is not implemented in
  PXC — error_not_implemented → client commit error, trx back to s_prepared; effects of
  the prepared XA remain pending, not doubled.
- Conclusion: resolved — exclude XA from this property's workload; the XA machinery is
  partially unimplemented and deserves separate treatment if XA enters scope.
