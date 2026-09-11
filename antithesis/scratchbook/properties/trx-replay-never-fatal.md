# trx-replay-never-fatal

**Boundary note (synthesis):** this property and `bf-replay-commits-exactly-once` share
the replay machinery and the fatal funnel at `wsrep_high_priority_service.cc:1054-1057`.
The catalog boundary: **this property owns replay termination** — replay ends only in
{success, cert-failure}, `Unreachable` at the fatal funnel; `bf-replay-commits-exactly-once`
owns the **effects contract** — a replayed transaction's effects appear exactly once on
every node (workload `Always`). Implement the Unreachable once, here.

**Type:** Safety (reachability of a node-fatal error path) + correctness of replay outcome.
**Focus:** concurrency — BF-abort races in the unlocked-provider-call windows force replay;
replay must terminate in exactly {success, cert-fail}, and the replayer must not be
BF-aborted again.

## What led to this property

A committing local transaction can be BF-aborted in the windows where the client mutex is
released around provider calls — verified in this tree:

- `wsrep-lib/src/transaction.cpp:1804-1830` (`certify_commit`): sets `s_certifying`, then
  `lock.unlock()` at :1823 before `send_pending_rollback_events` and `provider().certify()`.
  The 12-branch status switch afterwards includes `error_bf_abort → must_replay` and the
  in-code comment "Galera may return CONN_FAIL if trx BF aborted O_o".
- `percona-xtradb-cluster-galera/galera/src/replicator_smm.cpp:1360-1366`
  (`commit_order_enter_local`): `trx.unlock()` before `commit_monitor_.enter(co)`.
- `replicator_smm.cpp:3663-3691` (`handle_local_monitor_interrupted`, verified): a
  BF-aborted trx that carries `F_COMMIT` is set to `S_MUST_REPLAY` and the function returns
  **without cancelling the local monitor** — "it needs to be grabbed again in replay stage."

Replay is therefore the designated recovery mechanism for every one of these races, and its
contract is strict: the wsrep API kill routine must never abort a transaction that is ahead
in total order (`wsrep_api.h:1135-1145`), and an ordered transaction "can always finish
committing" (three numbered guarantees, wsrep-lib transaction.cpp:598-608). The MTR tests
`galera_UK_conflict.test`, `galera_transaction_replay.test`, `pxc_trx_replay_bf_abort.test`
exercise narrow debug-sync'd slices of this; none combine replay with membership churn or
multi-applier storms.

The enforcement gap, verified in this tree:

- `sql/wsrep_high_priority_service.cc:1046-1058` (`Wsrep_replayer_service` completion): if
  replay status is neither `success` nor `error_certification_failed` →
  `assert(0)` + **`unireg_abort(1)`** (:1057). Any third outcome — including the replayer
  itself being BF-aborted again, a monitor error during replay, or a provider error —
  kills the entire node.
- If replay never runs at all (node leaving the group, `SST_CANCELED` racing the replay —
  PXC adds writeset-dropping after SST cancel, replicator_smm.cpp:2241-2246), the local
  monitor slot reserved by `handle_local_monitor_interrupted` is never re-grabbed — see the
  companion property `local-monitor-freed-after-bf-abort`.
- Replay also throttles the node globally while in flight: every commit polls the global
  `wsrep_replaying` counter at 10ms (`sql/wsrep_client_service.cc:312-328`).

Correctness side: on `success`, the replayer restores the original THD's result
(`my_ok(...)` with shadowed affected_rows, :1050); on cert-fail it overrides to
ER_LOCK_DEADLOCK. A replay that *succeeds* must be exactly-once: the client sees one OK and
the data reflects one execution on every node.

## Failure scenario

1. Local trx T reaches certify/commit; in an unlocked window an applier BF-aborts it;
   T → `s_must_replay`; replayer thread re-executes T from cached events.
2. During replay, a second BF-abort arrives (another conflicting applier writeset, or TOI
   MDL conflict — the `galera_UK_conflict` shape with three concurrent writesets), or the
   node's provider state changes (partition → non-PRIM, SST cancel).
3. Replay returns something other than success/cert-fail → `unireg_abort(1)`: node suicide
   from an ordinary conflict storm. Or replay "succeeds" twice / applies alongside the
   original fragment → data divergence or duplicate effects (client saw one OK).

## Invariant / assertion plan

- **Primary (SUT-side, `Unreachable`, missing):**
  `Unreachable("transaction replay returned fatal status")` immediately before
  `unireg_abort(1)` at wsrep_high_priority_service.cc:1054-1057. This is a critical
  failure path that must never be observed; native assert is NDEBUG-compiled-out and
  `unireg_abort` destroys the evidence.
- **Coverage (SUT-side, `Sometimes`, missing):**
  `Sometimes("transaction replay completed successfully")` and
  `Sometimes("transaction replay ended in certification failure")` at the two legitimate
  outcome branches (:1047, :1051). Replay under real concurrency is exactly the rare
  semantic state Antithesis should be steered toward; these double as replay checkpoints.
- **Exactly-once (workload-side, `Always`):** the workload tracks every client-acknowledged
  write (OK received) with a unique token; after quiesce, each token appears exactly once on
  every node. Message: `"acked write applied exactly once on every node"`. This catches
  silent replay duplication/loss that no SUT-side probe sees. (Shared oracle with the
  divergence property, distinct message and check.)

## Config / timing dependencies

- High-conflict workload: hot rows, unique-key conflicts across nodes (INSERT ... ON
  DUPLICATE / UK churn — the galera_UK_conflict shape), plus TOI DDL on the same tables.
- `wsrep_applier_threads > 1`; `wsrep_retry_autocommit=0` for the client-visible variant
  (retry loop otherwise masks outcomes); also test with default 1.
- Membership churn during conflict storms (partition/heal — default-available) to race
  replay against non-PRIM transitions and SST cancellation.
- No disabled fault types required.

## Open questions

None — all three resolved; see Investigation Log.

Key resolutions folded into the property:

- **Reachable third statuses (each a node-suicide trigger, all verified):**
  `error_provider_failed` (galera `WSREP_NODE_FAIL`: any `gu::Exception` during the
  replay apply — "failure to replay own trx is certainly a sign of inconsistency" →
  `on_inconsistency()`, replicator_smm.cpp:1218-1237); `error_connection_failed`
  (galera `WSREP_CONN_FAIL`: any `std::exception` escaping `replay_trx`, caught in
  `galera_replay_trx` wsrep_provider.cpp:312-322 — includes the `gu_throw_fatal`
  invalid-replay-state path :1241-1243 and provider-closing exceptions from monitor
  waits); `error_fatal` (non-standard exception, :323-327). So the fatal funnel is
  reachable BY DESIGN when the node is locally inconsistent or the provider closes
  mid-replay — the `Unreachable` will report designed inconsistency-suicides as well as
  genuine replay bugs; keep the two legitimate-outcome `Sometimes` probes plus a
  `Sometimes` on the NODE_FAIL path to disambiguate.
- **Replay protection is victim-side and layered**, not merely the killer-side
  `wsrep_api.h` contract: (1) wsrep-lib `transaction::bf_abort` permits BF abort only in
  {executing, preparing, prepared, certifying, committing} — `s_must_replay`/`s_replaying`
  refuse, under the client-state mutex (transaction.cpp:1017-1082); (2) galera
  `abort_trx` returns `WSREP_NOT_ALLOWED` for `S_MUST_ABORT/S_ABORTING/S_MUST_REPLAY`
  (replicator_smm.cpp:963-970); (3) replay monitor enters submit a NULL trx pointer so
  monitor interrupts cannot target the replayer (replicator_smm.cpp:1170-1171); (4)
  `is_bf_immutable_` guards ordered-commit. An unlocked `wsrep_thd_is_BF`
  misclassification cannot force an abort through, because the state transition is
  re-checked under the transaction mutex. Residual channel: an MDL conflict against the
  replayer THD (classified BF) lands in the BF-BF `unireg_abort` funnel — node suicide,
  owned by `no-mdl-bf-bf-abort`, not a second BF abort of the transaction.
- **XA**: ordered XA replay goes through the same `replay()` → `Wsrep_replayer_service`
  path — the existing funnel probe covers it, no twin needed. UNORDERED XA
  (`is_xa() && !ordered()`, transaction.cpp:908-911) goes to `xa_replay_commit` →
  `client_service_.commit_by_xid()`, which in PXC is a stub: `assert(0); return
  error_not_implemented` (sql/wsrep_client_service.cc:330-334) — release builds return a
  commit error to the client and park the trx back in `s_prepared`
  (transaction.cpp:1349-1356); it never reaches the funnel. XA BF-abort recovery in PXC
  8.4 is thus partially unimplemented — exclude XA from the day-one replay workload (or
  give it its own property).

## SUT-side instrumentation suggestions (all missing)

- `Unreachable("transaction replay returned fatal status")` —
  wsrep_high_priority_service.cc:1054-1057.
- `Sometimes("transaction replay completed successfully")` /
  `Sometimes("transaction replay ended in certification failure")` — :1047/:1051.
- `Sometimes("replay apply raised inconsistency (NODE_FAIL)")` —
  replicator_smm.cpp:1233-1237 catch block (disambiguates designed inconsistency-suicide
  from unexpected third statuses at the funnel).
- `Sometimes("BF abort landed in unlocked certify window")` — instrument
  transaction.cpp:1823→1886 window (state observed `s_must_abort` on relock).
- `Sometimes("BF abort landed in unlocked commit-order-enter window")` —
  replicator_smm.cpp:1362-1375 EINTR path.

Note (investigated): wsrep-lib's own third-status handler — the `default:` branch of
`transaction::replay` calling `client_service_.emergency_shutdown()`
(transaction.cpp:2153-2155) — is dead code in PXC: the `Wsrep_replayer_service` dtor
runs inside `Wsrep_client_service::replay()` scope (wsrep_client_service.cc:293-299) and
`unireg_abort(1)`s before `transaction::replay` sees the status; PXC's
`emergency_shutdown()` would only `throw wsrep::not_implemented_error()`
(wsrep_client_service.h:47). The dtor is the single funnel to instrument.

### Investigation Log

#### What statuses can `provider().replay()` return besides success/cert-fail?

- Examined: `galera_replay_trx` (wsrep_provider.cpp:297-334); `ReplicatorSMM::replay_trx`
  (replicator_smm.cpp:1147-1250); status mapping (wsrep-lib
  wsrep_provider_v26.cpp:70-96); consumption in `transaction::replay`
  (transaction.cpp:2110-2163) and `Wsrep_replayer_service` dtor
  (wsrep_high_priority_service.cc:1027-1059).
- Found: exits are WSREP_OK → success; WSREP_TRX_FAIL → error_certification_failed
  (cert failure when replay re-certifies from S_REPLICATING, :1176-1183);
  WSREP_NODE_FAIL → error_provider_failed (gu::Exception during the replay apply →
  `on_inconsistency()`, :1233-1237); WSREP_CONN_FAIL → error_connection_failed (any
  std::exception escaping replay_trx, incl. `gu_throw_fatal` invalid-state :1241-1243
  and provider-closing exceptions); WSREP_FATAL → error_fatal (non-standard exception).
- Conclusion: resolved — three third statuses are reachable by design (inconsistency,
  provider closing, foreign exception); each lands in the dtor funnel → unireg_abort(1).
  The Unreachable is not merely a regression guard on an impossible state; it will fire
  on real inconsistency events. Added a NODE_FAIL `Sometimes` to the plan.

#### Is a replaying transaction protected from further BF aborts, and by which side?

- Examined: `transaction::bf_abort`/`total_order_bf_abort` (transaction.cpp:1017-1155);
  state-transition matrix (:1392-1407, `mr` row permits only `re`);
  `ReplicatorSMM::abort_trx` (replicator_smm.cpp:936-1066); replay NULL-trx monitor
  submission (:1170-1171); server funnel `wsrep_bf_abort` (wsrep_thd.cc:362-385) and
  `wsrep_abort_thd` (:331-359).
- Found: protection is victim-side, enforced under the client-state mutex at four
  layers (state gate, provider NOT_ALLOWED, NULL-trx monitors, is_bf_immutable_). All
  server-side killers (InnoDB kill, MDL abort, KILL statement) funnel through
  `wsrep_bf_abort` → the same gate.
- Not found: any killer path that mutates transaction state without passing the gate.
- Conclusion: resolved — a second BF abort of a replaying transaction is structurally
  refused; the surviving hazard is the MDL BF-BF node-suicide funnel against the
  replayer THD (cross-ref `no-mdl-bf-bf-abort`).

#### Does XA replay share the fatal-status funnel?

- Examined: `after_statement` XA dispatch (transaction.cpp:906-916); `xa_replay` /
  `xa_replay_commit` (:1319-1361); PXC `Wsrep_client_service::commit_by_xid` and
  `replay_unordered` (wsrep_client_service.cc:306-334).
- Found: ordered XA uses the normal `replay()` path → same funnel (covered). Unordered
  XA uses `xa_replay_commit` → `commit_by_xid()` which is `assert(0);
  error_not_implemented` in PXC — release builds return a commit error and reset the trx
  to s_prepared; no funnel, no abort. `replay_unordered` is likewise a stub.
- Conclusion: resolved — no twin probe needed; XA BF-abort recovery is partially
  unimplemented in PXC 8.4 (finding recorded; XA excluded from day-one replay workload).
