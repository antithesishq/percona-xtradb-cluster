# async-monitor-leave-mismatch-unreachable

**Type:** Safety (a reached error path aborts the whole node).
**Focus:** concurrency — PXC-only `Wsrep_async_monitor` enter/leave asymmetry for killed
workers.

## What led to this property

`Wsrep_async_monitor` (PXC-only, 2024-25, built for PXC-4173) orders async-replica workers'
entry into the wsrep commit path. It is regression-dense: PXC-4664/4688 (SIGSEGV, then
"after fixing sigsegv, it is still possible to end up with a deadlock" per commit message),
PXC-4823 (scheduled seqno never entered/skipped → permanent stall).

Verified structural asymmetry in this tree:

- `sql/wsrep_mysqld.cc:2948-2972` (`thd_enter_async_monitor`): for a
  `SYSTEM_THREAD_SLAVE_WORKER`, **early-returns without entering when
  `thd->killed != THD::NOT_KILLED`** (line 2954: "If the thread is already killed, leave it
  to the caller to handle it.").
- `sql/wsrep_mysqld.cc:2974-2991` (`thd_leave_async_monitor`): **no killed check** — calls
  `wsrep_async_monitor->leave(seqno)` unconditionally.
- Callers bracket unconditionally: `sql/wsrep_trans_observer.h` `wsrep_before_prepare`
  (enter at :257, leave at :277 regardless of what happened in between) and
  `wsrep_before_commit` (:320/:338-340); also `wsrep_to_isolation_begin` path
  (wsrep_mysqld.cc:3130/3166 per SUT analysis).
- `sql/wsrep_async_monitor.cc:64-107` (`leave`): if the seqno is not in `skipped_seqnos` and
  not at the queue front → `assert(false && "Sequence number mismatch in leave()")` +
  **`unireg_abort(1)`** (line 90). The diagnostic that would explain it is a commented-out
  `std::cout` (:81-86). The abort happens while holding `m_mutex`. Exit status 1 matches the
  shipped systemd unit's `RestartPreventExitStatus` → the node stays down.

So: a worker killed between `schedule()` (sql/log_event.cc:2686) and
`thd_enter_async_monitor` skips `enter()` but still executes `leave(seqno)`. Two outcomes,
both bad:

- If its seqno happens to be at the queue front, `leave()` pops it — but the worker never
  waited for its turn, i.e. **ordering was silently bypassed** for that transaction.
- If not at the front, **`unireg_abort(1)` kills the entire node** from an ordinary
  `KILL <worker>` / `STOP REPLICA` race.

Adjacent hazards in the same subsystem (documented here, may become separate properties
after runtime confirmation):

- **Wrong sizing variable:** constructed with `rli->opt_replica_parallel_workers`
  (`sql/rpl_replica.cc:7285-7290`, verified), while entry into the wsrep pipeline is done by
  wsrep applier scheduling; `m_workers_count` bounds `skipped_seqnos` GC
  (wsrep_async_monitor.cc:96-107). A `skip(n)` pruned before a delayed `enter(n)` runs
  leaves the waiter blocked forever.
- **Stale `skipped_seqnos` across source binlog rotation:** `sequence_number` comes from
  `Gtid_log_event` (`sql/rpl_mta_submode.cc:597-604`, verified) and the source's logical
  clock restarts per binlog file; the monitor object survives rotation (deleted only at
  applier stop, rpl_replica.cc:7722-7724 per SUT analysis). A stale high seqno in
  `skipped_seqnos` makes a *future* transaction with the same number short-circuit both
  `enter()` and `leave()` (wsrep_async_monitor.cc:33, :69) — ordering bypass without any
  crash.

## Failure scenario (primary)

1. Environment includes an async MySQL source replicating into one PXC node
   (`replica_preserve_commit_order=ON`, `replica_parallel_workers > 1`,
   `wsrep_use_async_monitor=ON` — the default).
2. Workload issues `STOP REPLICA` / `KILL` on a worker while transactions are in the window
   between `schedule()` and `enter()` on an out-of-order worker.
3. Killed worker's `leave()` hits the mismatch branch → `unireg_abort(1)` → node down,
   systemd refuses restart → cluster loses a member from a routine operator action.

## Invariant / assertion plan

- **Primary (SUT-side, `Unreachable`, missing):**
  `Unreachable("async monitor leave() seqno mismatch")` at the else-branch in
  `Wsrep_async_monitor::leave` (wsrep_async_monitor.cc:80-91), placed before the
  `unireg_abort(1)`. Rationale: this is a critical failure path that must never be observed;
  `Unreachable` is exactly the semantics. Native `assert` is compiled out under NDEBUG and
  `unireg_abort` kills the process — the SDK assertion reports the state before death and
  makes the moment replayable.
- **Companion (SUT-side, `AlwaysOrUnreachable`, missing):** in `thd_leave_async_monitor`,
  assert the enter/leave pairing invariant directly:
  `"async monitor leave only after matching enter"` — i.e. if
  `thd->killed != THD::NOT_KILLED` caused enter to be skipped, leave must be skipped too.
  Cheapest correct fix/detector is a per-THD `entered` flag; asserting on it localizes the
  asymmetry independent of queue state.
- **Coverage (`Sometimes`, missing):** `Sometimes("worker killed between async-monitor
  schedule and enter")` at the early-return (wsrep_mysqld.cc:2954-2956) — proves the race
  window was exercised.
- **Ordering-bypass detector (workload-side):** replicated transactions from the async
  channel commit on the PXC node in `sequence_number` order when
  `replica_preserve_commit_order=ON`; verifiable by a workload table with a monotonic
  counter per source transaction. Catches the silent front-pop and stale-skipped bypass
  cases that don't crash.

## Config / environment dependencies

- **Requires an async replication source feeding a PXC node** — an environment-design
  decision, not just workload. Channel config: `replica_parallel_workers > 1`,
  `replica_preserve_commit_order = ON` (defaults in 8.4: workers=4, preserve order ON).
- `wsrep_use_async_monitor` is read-only, default ON.
- Workload must exercise `STOP REPLICA`, `START REPLICA`, `KILL` on worker/coordinator
  threads, and `FLUSH BINARY LOGS` on the source (rotation → sequence_number restart).
- Faults: available-by-default set suffices (thread hangs on workers widen the
  schedule→enter window). Node termination not required.

## Open questions

- Does an MTS transaction retry re-enter the monitor for a seqno that a prior `leave()`
  already popped? If so, the retrying worker's `enter()` waits forever on a seqno that will
  never reach the queue front — a stall shape distinct from PXC-4823. `(partial: retry path
  traced — Slave_worker::retry_transaction → read_and_apply_events →
  slave_worker_exec_event (rpl_rli_pdb.cc:1897-2060) never re-runs the coordinator-side
  schedule() at log_event.cc:2686; a temp-error retry after wsrep_before_prepare (which
  pops the seqno at leave, wsrep_trans_observer.h:277 — commit-order deadlock retries are
  exactly this shape) would call enter() on an already-popped seqno; unconfirmed whether
  the wsrep hooks actually re-run enter() on the retry execution path.)`

All three original questions resolved (see Investigation Log):

- `scheduled_seqnos` stays coherent across rotation — the queue uses FIFO front-equality
  with no monotonicity assumption, so the first post-rotation transaction does NOT
  mismatch; no trivial crash at rotation. The rotation hazard is confined to stale
  `skipped_seqnos` (quantified in duplicate-gtid-skip-exactly-once's log: only skips within
  the last `m_workers_count` pre-rotation seqnos survive GC, and each survivor
  deterministically bypasses ordering when the new file's stream reaches it).
- All four enter/leave brackets are lexical (same function), so the `entered` flag needs no
  cross-function plumbing: a local flag per bracket (the pattern `wsrep_before_commit`
  already uses, `async_monitor_entered`) or a symmetric killed-check in
  `thd_leave_async_monitor` suffices. "Killed inside the bracket" (entered, then killed
  during prepare) is safe today — leave pops the worker's own front entry.
- The killed-worker front-pop is masked in DATA terms — a killed worker's transaction
  errors and rolls back, so nothing out-of-order commits from that pop, and the pop itself
  is necessary bookkeeping (an unpopped head would wedge all later seqnos). The observable
  ordering bypass comes from the stale-skip path (a *genuine* transaction skipping both
  enter and leave), which IS observable: its galera seqno order vs source sequence_number
  order inverts, so the workload's monotonic-counter check can fire.

## SUT-side instrumentation suggestions (all missing)

- `Unreachable("async monitor leave() seqno mismatch")` — wsrep_async_monitor.cc:80-91.
- `AlwaysOrUnreachable("async monitor leave only after matching enter", entered_flag)` —
  thd_leave_async_monitor.
- `Sometimes("worker killed between async-monitor schedule and enter")` —
  wsrep_mysqld.cc:2954-2956.
- `Sometimes("async monitor skipped_seqnos non-empty at binlog rotation boundary")` — needs
  a hook at rotation handling; confirms the stale-seqno precondition.

### Investigation Log

#### Does scheduled_seqnos stay coherent across binlog rotation (does the first post-rotation trx mismatch)?

(2026-09-10, open-questions pass)

- Examined: full `sql/wsrep_async_monitor.cc` (schedule/enter/leave/skip; queue matching is
  `scheduled_seqnos.front() == seqno`, FIFO order of `schedule()` calls, no monotonicity
  assumption anywhere); source rotation offset reset
  (`Commit_order_trx_dependency_tracker::rotate()`, sql/rpl_trx_tracking.cc:199-204);
  replica raw consumption (rpl_mta_submode.cc:596-604, log_event.cc:2679-2688); monitor
  lifetime (rpl_replica.cc:7283-7291 create, :7719-7726 delete at applier stop only).
- Found: coordinator scheduling and worker enter/leave both follow relay-log order, so the
  queue's FIFO matching holds across a seqno restart — the first post-rotation transaction
  (seqno 1) is pushed behind the last pre-rotation seqnos and matches in order; no mismatch
  abort at rotation per se. The incoherence across rotation is confined to `skipped_seqnos`
  (value-keyed set): survivors within `m_workers_count` of the final pre-rotation seqno
  collide with equal post-rotation seqnos and silently bypass both enter() and leave() —
  ordering bypass, no crash. GC arithmetic (remove_upto = seqno - m_workers_count) can
  never remove the colliding entry in time.
- Not found: any monitor reset at rotation; any queue-side monotonic check that would turn
  rotation into a crash.
- Conclusion: resolved — "trivially hit crash at rotation" hypothesis is wrong; the cheap
  rotation-driven signal is the ordering bypass, not the unireg_abort. The Unreachable at
  the mismatch branch remains the property; rotation feeds the workload's ordering check.

#### Which callers can run thd_leave_async_monitor after an error return from before_prepare with the worker killed inside the bracket?

(2026-09-10, open-questions pass)

- Examined: all four bracket sites — `wsrep_before_prepare`
  (sql/wsrep_trans_observer.h:257/:277, unconditional enter/leave incl. before_prepare
  error returns), `wsrep_before_commit` (:314-322/:338-340, conditional with local
  `async_monitor_entered` flag), the empty-commit cleanup (:568-587, back-to-back
  enter+leave guarded by `owned_gtid.sidno == 0`), `wsrep_to_isolation_begin`
  (sql/wsrep_mysqld.cc:3130/:3166, unconditional around TOI/RSU/NBO begin); the killed
  checks in `thd_enter_async_monitor` (:2954-2956) vs `thd_leave_async_monitor` (none).
- Found: every bracket is lexical — enter and leave in the same function with no early
  return between them, so "entered then killed during prepare" always leaves correctly
  (the worker's seqno reached the front when it entered). The ONLY asymmetry is a kill
  landing between enter's `thd->killed` check (skip) and leave (unconditional). Because
  brackets are lexical, the fix/instrumentation point is fully local: either a per-bracket
  local flag (pattern already present in `wsrep_before_commit`) or a mirrored killed check
  in `thd_leave_async_monitor`. No cross-function `entered` state is needed.
- Conclusion: resolved — the planned `AlwaysOrUnreachable("leave only after matching
  enter")` can be implemented with a THD-local boolean set in `thd_enter_async_monitor`
  after the killed check and cleared in `thd_leave_async_monitor`; all sites funnel through
  those two helpers.

#### Would the mismatched-leave front-pop produce an observable out-of-order commit, or is it masked?

(2026-09-10, open-questions pass)

- Examined: the leave() pop semantics (wsrep_async_monitor.cc:75-79), the killed-worker
  transaction outcome (killed worker's trx errors/rolls back — it does not commit), the
  stale-skip bypass path (enter :32-33 / leave :67-69 early returns), and how async-channel
  transactions map to galera commit order (they are local trx on the PXC node; cluster
  order = galera seqno order assigned at replicate time).
- Found: the killed-worker front-pop is masked in data terms — the killed transaction rolls
  back, so no out-of-order COMMIT results from that specific pop; the pop is also required
  bookkeeping (an unpopped head seqno would wedge every later enter()). The observable
  bypass case is a *genuine* (non-killed) transaction short-circuiting via a stale skipped
  seqno: it commits without waiting, so its galera seqno (= cluster commit order on every
  node) inverts relative to source sequence_number order — detectable by the workload's
  monotonic-counter/commit-order check. Discovered adjacent hazard while tracing the pop:
  if MTS retries a killed worker's group, `schedule()` is not obviously re-run, so a
  retry's enter() could wait forever on an already-popped seqno — recorded as a new open
  question above.
- Conclusion: resolved — the ordering-bypass workload check CAN fire (via stale skips), the
  killed-pop itself cannot produce committed out-of-order data, and Galera does not mask
  the genuine-bypass case (cluster order faithfully exposes it).
