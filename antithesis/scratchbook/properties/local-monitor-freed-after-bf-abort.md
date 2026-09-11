# local-monitor-freed-after-bf-abort

**Type:** Liveness (node-local certification pipeline never permanently blocked).
**Focus:** concurrency — `handle_local_monitor_interrupted` leaves a local-monitor slot
reserved on the MUST_REPLAY path; only a later replay fills it.

## What led to this property

Verified in `percona-xtradb-cluster-galera/galera/src/replicator_smm.cpp:3663-3691`
(`handle_local_monitor_interrupted`): when a local transaction is BF-aborted while waiting
to enter the local (certification-order) monitor:

- If it does **not** carry `F_COMMIT`: it is pushed onto `pending_cert_queue_` and treated
  as cert-failed — the seqno's monitor obligations are discharged via the pending queue.
- If it **does** carry `F_COMMIT` (a committing transaction): state → `S_MUST_REPLAY` and
  the function returns **"immediately without canceling local monitor, it needs to be
  grabbed again in replay stage"** (in-code comment, :3678-3682).

The local monitor is strict: `LocalOrder` requires `last_left + 1 == seqno`
(monitor.hpp; LocalOrder uses `local_seqno`). Every writeset — local and remote — passes
through `enter_local_monitor_for_cert` (replicator_smm.cpp:3792) in `seqno_l` order. A
`seqno_l` that is neither entered/left nor self-cancelled is a hole: **all certification on
that node stops at the hole**, which stops applying, which triggers flow control and stalls
the cluster (same blast radius as `commit-order-monitor-released-no-cluster-stall`, but a
different monitor and a different leak mechanism).

The hole is filled only if replay actually runs and re-enters the local monitor at that
seqno. Paths where replay may never run:

- Node leaves the primary component before the replayer executes (conf change; replay
  requires provider interaction).
- PXC-only `SST_CANCELED` handling: after SST cancel the node **drops all incoming
  writesets** (replicator_smm.cpp:2241-2246) and async_recv treats -ECANCELED specially
  (:494-496, :532-541) — if the replay's monitor grab is queued behind dropped seqnos, or
  the replayer errors out against a closing provider, the slot stays reserved.
- The client session is killed (`KILL <thd>`) between MUST_REPLAY and the replayer picking
  the transaction up — does rollback of a MUST_REPLAY trx cancel the reserved slot?
  (Open question below; the `self_cancel` path exists but this specific flow was flagged as
  the SUT analysis "focus 1 open Q1" precisely because no cancellation call is visible on
  it.)

This differs from the commit-order property: that one is about *applier error paths* leaking
apply/commit monitor slots; this one is about the *local client BF-abort/replay handoff*
leaking the certification-order monitor. Both end in FC stall; the triggers and the
instrumentation sites are disjoint.

## Failure scenario

1. Local committing trx T (seqno_l = L) blocks entering the local monitor behind a slow
   certification.
2. Applier BF-aborts T → `handle_local_monitor_interrupted` → MUST_REPLAY, slot L reserved.
3. Before replay runs: node drops out of PRIM (partition — default-available fault), or the
   client connection is killed, or SST cancellation fires.
4. Slot L is never entered/cancelled → local monitor wedged at L-1 → no writeset on this
   node certifies again → recv queue growth → FC pause → cluster-wide write stall while the
   node reports Synced (until the view change demotes it, if it ever does).

## Invariant / assertion plan

- **Primary (workload-side, `Always` at drain points):** after fault heal + drain, every
  node accepts and commits a local probe write, and `wsrep_cert_index_size` /
  `wsrep_local_recv_queue` are draining. Message:
  `"every node certifies new transactions after BF-abort/replay churn"`. Distinct from the
  cluster-drain message of the commit-order property so failures localize.
- **SUT-side leak detector (`AlwaysOrUnreachable`, missing):** when a MUST_REPLAY
  transaction is destroyed/rolled back *without* having replayed, assert that its reserved
  local-monitor interval was cancelled: instrument `TrxHandleMaster` teardown for state
  S_MUST_REPLAY with a check that `local_monitor_.last_left() >= seqno_l` or an explicit
  `self_cancel` was issued. Message: `"MUST_REPLAY trx released its local monitor slot"`.
  `AlwaysOrUnreachable` because the abandoned-replay path is rare; every occurrence must
  satisfy the release obligation.
- **Coverage (`Sometimes`, missing):**
  `Sometimes("committing trx BF-aborted before local monitor entry")` at
  replicator_smm.cpp:3676-3682 — this is the exact rare interleaving; and
  `Sometimes("MUST_REPLAY trx abandoned without replay")` on the abandonment path once
  located.

## Config / timing dependencies

- Hot-row conflict workload with committing transactions racing appliers
  (`wsrep_applier_threads > 1` to make local-monitor waits longer and BF aborts frequent).
- Membership churn (partition/heal, node hang) timed against conflict storms — the
  replay-abandonment triggers are view changes and SST cancellation.
- The SST_CANCELED variant needs a joiner mid-SST getting its transfer cancelled —
  achievable with network faults against the donor (default-available).
- Node termination not required, but kill-during-replay would widen coverage — **flag:
  optional dependency on node-termination fault.**

## Open questions

None — all three resolved; see Investigation Log. Net effect on the property:

- **Downgrade from "possible live bug" to regression guard.** No cancellation path exists
  in galera (`release_rollback` never touches `local_monitor_`,
  replicator_smm.cpp:1605-1675), but none is needed: wsrep-lib makes
  rollback-without-replay structurally unreachable. The `s_must_replay` state can only
  transition to `s_replaying` (transition matrix, wsrep-lib transaction.cpp:1392-1407);
  `after_statement` drives it into `replay()` (:906-916); session disconnect goes through
  `client_state::close()` → `bf_rollback()` + `after_statement()` → replay
  (client_state.cpp:57-81); `before_rollback`/`after_rollback` and the background
  rollbacker's `bf_rollback` all preserve `s_must_replay`. If replay itself fails (node
  left PRIM, provider closing), the `Wsrep_replayer_service` dtor `unireg_abort(1)`s —
  the reserved slot dies with the node (view change unblocks the cluster); no *silent*
  permanent wedge path was found. The workload `Always`-at-drain-points check stays as a
  cheap guard; the "targeted repro" escalation is off.
- **SST_CANCELED writeset-drop BYPASSES the local monitor entirely** — `process_trx`
  returns before any monitor interaction (replicator_smm.cpp:2238-2246), so every dropped
  writeset leaves an undischarged local-monitor slot. This is tolerated only because SST
  cancellation implies the node is shutting down. If any path keeps a node serving after
  SST_CANCELED, its certification is permanently wedged — worth a `Sometimes("writeset
  dropped after SST cancel")` probe plus the drain check on any node that survives an SST
  cancel.
- **Cheap external observable EXISTS (better than the cert_index_size proxy):** PXC ships
  a provider status var `wsrep_monitor_status (L/A/C)` printing `(last_entered,
  last_left)` for the Local/Apply/Commit monitors (replicator_smm_stats.cpp:154-156 and
  :255-277; Monitor::stats monitor.hpp:329-334). A frozen local-monitor pair with
  advancing last_entered elsewhere is the leak signature, pollable by the workload with
  no SUT instrumentation.

## SUT-side instrumentation suggestions (all missing)

- `Sometimes("committing trx BF-aborted before local monitor entry")` —
  replicator_smm.cpp:3676-3682.
- `Sometimes("replay re-entered local monitor after BF abort")` — the replay-side re-grab
  (start_of_replay path), pairing with the reservation site.
- `Sometimes("writeset dropped after SST cancel")` — replicator_smm.cpp:2241-2245 (the
  local-monitor-bypass path; must correlate with node shutdown).
- The previously proposed `AlwaysOrUnreachable("MUST_REPLAY trx released its local
  monitor slot")` teardown probe is superseded: the abandoned-replay teardown path does
  not exist in wsrep-lib (see Investigation Log); keep instead the workload drain check.
- The monitor-gap gauge needs NO instrumentation: poll `wsrep_monitor_status (L/A/C)`
  (PXC-only status var) from the workload.

### Investigation Log

#### When a MUST_REPLAY transaction is rolled back instead of replayed, which code path cancels the reserved LocalOrder interval?

- Examined: `handle_local_monitor_interrupted` (replicator_smm.cpp:3663-3705),
  `enter_local_monitor_for_cert` (:3627-3661), `cert`/`finish_cert` (:3707-3802),
  `release_rollback` (:1605-1675), `replay_trx` (:1147-1250); wsrep-lib
  `transaction::after_statement` (transaction.cpp:850-959), `before_rollback`
  (:676-772), `after_rollback` (:774-806), state matrix (:1392-1407);
  `client_state::close/cleanup/before_command` (client_state.cpp:42-179); background
  rollbacker (sql/wsrep_thd.cc:212-306).
- Found: no cancellation path exists in galera — `release_rollback` enters/leaves
  apply+commit monitors but never local_monitor_. However every wsrep-lib teardown route
  for `s_must_replay` funnels into `replay()`: after_statement (:906-916), disconnect via
  close() (:70-75 — bf_rollback then after_statement), before_command error path, and
  the rollbacker's bf_rollback all preserve `s_must_replay` (state matrix permits only
  mr→re). Replay re-enters the local monitor via `cert_and_catch` when ts is still
  S_REPLICATING and via finish_cert leaves it; replay failure → `Wsrep_replayer_service`
  dtor `unireg_abort(1)` (node death; slot moot).
- Not found: any reachable path that destroys a MUST_REPLAY master trx while the process
  keeps serving — i.e. the silent permanent wedge.
- Conclusion: resolved — property downgraded from possible-live-bug to regression guard;
  drain-point `Always` retained; priority rationale updated above.

#### Does the PXC writeset-drop after SST_CANCELED bypass the local monitor?

- Examined: `process_trx` (replicator_smm.cpp:2224-2310); SST_CANCELED handling
  (:2238-2246); `cancel_seqnos`/`cancel_seqno` (:2342-2369).
- Found: yes — the SST_CANCELED early return happens before any local-monitor
  enter/self_cancel; dropped writesets leave undischarged slots. `cancel_seqnos` (used
  for corrupt-state queue dismissal) does self_cancel and is a separate path.
- Conclusion: resolved — bypass confirmed; benign only under the assumption the node
  shuts down after SST cancel. Added a probe suggestion; any node observed serving after
  SST_CANCELED with frozen local monitor is a finding.

#### Can the LocalOrder gap be observed cheaply from status variables?

- Examined: provider stats table and snapshot code
  (replicator_smm_stats.cpp:150-217, :240-300); `Monitor::stats` (monitor.hpp:329-334).
- Found: PXC-only `monitor_status (L/A/C)` status variable exposes `(last_entered,
  last_left)` for all three monitors, refreshed on every SHOW STATUS; `last_committed`
  is `commit_monitor_.last_left()` in PXC builds (:248-249).
- Conclusion: resolved — the workload can detect a frozen local-monitor window directly;
  the cert_index_size proxy is unnecessary.
