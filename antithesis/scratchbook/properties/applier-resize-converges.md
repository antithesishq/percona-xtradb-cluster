---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# applier-resize-converges — Runtime applier-thread resize converges to the setpoint without stalling replication

**Focus area:** Lifecycle transitions — runtime reconfiguration under load
(`wsrep_applier_threads` / `wsrep_slave_threads` resize).

## Claim under test

`SET GLOBAL wsrep_applier_threads = N` issued while the node is applying replication load
eventually results in exactly N applier threads (observable:
`wsrep_thread_count == N + 1`, appliers + rollbacker), with replication progressing
throughout. Repeated/concurrent resizes never lose count — never converging to the wrong
number, never dropping to zero appliers (replication stall → flow control → cluster
stall), and never wedging the resize machinery.

## Code paths (verified at commit f9ecb3e)

- **Setpoint computation is unlocked**: `wsrep_slave_threads_update`
  (`sql/wsrep_var.cc:673-690`) computes
  `wsrep_slave_count_change = wsrep_slave_threads - wsrep_running_threads + 1` and, for
  growth, immediately spawns threads and zeroes the counter — all **without taking
  `LOCK_wsrep_slave_threads`** (it runs under LOCK_global_system_variables only).
- **Shrink consumption is locked**: `Wsrep_applier_service::check_exit_status`
  (`sql/wsrep_high_priority_service.cc:956-965`) — each applier, after each writeset,
  checks `wsrep_slave_count_change < 0` under `LOCK_wsrep_slave_threads` and exits while
  incrementing it. Two different synchronization regimes on the same plain `int` →
  classic lost-update: a resize racing applier exits (or a second resize racing the first)
  can leave `wsrep_slave_count_change` wrong — too many exits (down to zero appliers) or a
  stale negative count silently killing threads spawned by a later grow.
- **Observable**: status var `wsrep_thread_count` = `wsrep_running_threads`
  (`sql/mysqld.cc:13557`, SHOW_LONG_NOFLUSH), maintained in `sql/wsrep_thd.cc:106-114`.
- Applier creation: `wsrep_create_appliers` (`sql/wsrep_mysqld.cc:1300`); appliers pull
  independently via `async_recv` (`galera/src/replicator_smm.cpp:471-524` — the **last
  applier is prevented from exiting** at :517-524, the SUT's own guard against
  zero-applier; this property tests that guard under the racy counter).
- Interaction hazards while resized under load: exiting appliers must not abandon a held
  apply/commit monitor slot (monitor ring `galerautils/src/monitor.hpp`, 65536 window) —
  an exit between `enter()` and `leave()` would stall the seqno pipeline cluster-wide
  (bug-pattern C shape). Also `Wsrep_async_monitor` is sized by
  `opt_replica_parallel_workers`, not `wsrep_applier_threads`
  (`sql/rpl_replica.cc:7285-7290`, per sut-analysis §5.4) — resizing appliers above the
  async-replica worker count while an async replication channel runs is a known stall
  recipe (only relevant if the workload configures async replication into the cluster).

## Failure scenario

Workload cycles `SET GLOBAL wsrep_applier_threads` among {1, 4, 16} on each node every few
seconds under sustained multi-node write load (so appliers are busy and exit checks are
frequent). A race between the unlocked update and a concurrent applier-exit consume, or
two concurrent SETs from different sessions, corrupts `wsrep_slave_count_change`:
- convergence to the wrong thread count (silent — capacity misconfiguration), or
- all-but-guard appliers exit → recv queue grows → flow control pauses the whole cluster
  (external symptom: every node's commits stall), or
- a later grow's new threads immediately consume a stale negative count and exit →
  `SET` appears successful but nothing changed.
MTR never tests this under load (~35 galera_var_* tests are quiescent, sut-analysis §10.4);
default `wsrep_applier_threads=1` in the field means the resize path is nearly untraveled.

## Suggested implementation

- **Workload-side (primary)**: after each resize settles (bounded wait, no pending
  resizes), assert `Always`: "wsrep_thread_count == wsrep_slave_threads + 1 within T of
  the last SET, and wsrep_last_committed advanced during the interval". At final
  quiescence, the same equality must hold on every node.
- **Sometimes markers**:
  - "applier pool shrunk under load and replication progressed" (workload-side — proves
    the shrink path ran while writesets flowed);
  - "applier pool grown under load" (workload-side);
  - "last-applier exit guard triggered" (**missing SUT instrumentation** at
    `replicator_smm.cpp:517-524` — reaching it means the pool hit the floor, a danger
    state worth a replay anchor).
- **SUT-side Always (missing)**: in `check_exit_status`/`wsrep_slave_threads_update`,
  assert `wsrep_slave_count_change` is consistent with
  `wsrep_slave_threads - wsrep_running_threads + 1` at quiesce; cheap and directly targets
  the lost-update.

## Assertion type

Liveness (convergence) with a safety edge — implemented as workload-side `Always` on the
settled equality + progress condition. Not `Sometimes`: the equality must hold after every
settled resize, not merely once.

## Fault requirements

None required — pure workload-driven reconfiguration under load; this property works even
in the most restricted fault environment. Network faults (default-on) enrich by making
applier queues deep during resizes. No node termination or clock faults needed.

## Confidence

High on the locking asymmetry (both sides read directly). Medium on exploitability — the
window is small, but Antithesis timing exploration plus high SET frequency is the right
tool, and the blast radius (cluster-wide FC stall) is large.

## Open questions

None — all three resolved (see Investigation Log). Resolutions folded into the design:

- Exit points are only between writesets, after both monitors are released — the thread
  count is the right observable; no monitor-strand qualifier needed.
- Two concurrent `SET GLOBAL wsrep_applier_threads` are serialized by the global-sysvar
  write lock; the realistic race is SET-vs-applier-exit. The workload can still issue SETs
  from two sessions (they queue), but the interesting interleaving is SET during apply load.
- The settled equality is `wsrep_thread_count == wsrep_applier_threads + 1` (N appliers +
  1 rollbacker; no post-rollbacker thread exists in this tree). One caveat for the check:
  NBO applier workers (`wsrep_OSU_method=NBO` DDL) and in-flight NBO replayer THDs are
  added to the same counter while running (`sql/wsrep_high_priority_service.cc:769-781`
  calls `thd_manager->add_thd` with `wsrep_applier=true`) — the workload should not run
  NBO DDL concurrently with the settled-equality check, or should tolerate transient
  excess.

### Investigation Log

#### Does an applier exiting via check_exit_status always complete its monitor leave() first?

- Examined: `sql/wsrep_high_priority_service.cc` (`must_exit_` set at :353/:529 in
  `after_apply`/TOI apply tails, consumed via `must_exit()` accessor),
  `wsrep-lib/include/wsrep/high_priority_service.hpp:231,:245`,
  `wsrep-lib/src/wsrep_provider_v26.cpp:497-538` (`apply_cb`),
  `percona-xtradb-cluster-galera/galera/src/trx_handle.cpp:376-405` (`ts.apply`),
  `replicator_smm.cpp:590-673` (`apply_trx`), `gcs_action_source.cpp:49-64,:158-185`,
  `replicator_smm.cpp:470-535` (`async_recv`).
- Found: `apply_cb` sets `*exit_loop` only after `high_priority_service->apply()` returns
  success. In `apply_trx`, `ts.apply(...)` (which includes commit-order callbacks — PXC
  comment at :610-614: "with 4.x commit callback are part of apply action") completes,
  then `cert_.set_trx_committed`, then `apply_monitor_.leave(ao)` — all before
  `ts.set_exit_loop(exit_loop)` at :672. `async_recv` reads `exit_loop` only after
  `as_->process()` returns (:513). On ApplyException, `exit_loop` stays false and the
  monitor is still left.
- Not found: any path where a thread exits the recv loop while holding an apply/commit
  monitor slot.
- Conclusion: resolved — exit is strictly between writesets, after monitor release. The
  thread-count observable stands; no monitor-strand rewording needed.

#### Are two concurrent SETs serialized (race is only SET-vs-exit)?

- Examined: `sql/set_var.cc:343-372` (`sys_var::update`), `sql/sys_vars.cc:8295-8302`
  (`Sys_wsrep_applier_threads`, plain `Sys_var_ulong`, GLOBAL,
  `ON_UPDATE(wsrep_slave_threads_update)`).
- Found: for GLOBAL scope, `global_update` + `on_update` run under
  `AutoWLock(&PLock_global_system_variables)` + the var's guard — two concurrent SETs are
  fully serialized; each SET's read-compute-write of `wsrep_slave_count_change` is atomic
  w.r.t. other SETs.
- Found (race preserved): `wsrep_slave_threads_update` (`sql/wsrep_var.cc:673-690`) never
  takes `LOCK_wsrep_slave_threads`, while `check_exit_status`
  (`wsrep_high_priority_service.cc:956-965`) mutates `wsrep_slave_count_change` under it —
  the SET-vs-exit lost-update stands.
- Conclusion: resolved — concurrent SETs serialize; the property's race target is
  SET-vs-applier-exit. Two-session SET scheduling is fine but not the mechanism.

#### wsrep_thread_count offset: +1 or +2?

- Examined: `sql/mysqld.cc:1691,:13557` (counter + status var),
  `sql/mysqld_thd_manager.cc:263-306` (increment/decrement on add_thd/remove_thd keyed on
  `thd->wsrep_applier`), `sql/mysqld.cc:12427-12530` (`start_wsrep_THD`,
  `new THD(false, true)` → `wsrep_applier=true` for every wsrep service thread),
  `sql/wsrep_thd.cc:122,:308` (creation sites), `sql/wsrep_mysqld.cc:1299-1300` +
  `sql/mysqld.cc:11060` (startup: 1 rollbacker + 1 applier pre-SE, then N-1 appliers),
  repo-wide grep for `key_THREAD_wsrep_post_rollbacker`.
- Found: wsrep service threads are exactly N appliers + 1 rollbacker. The PSI key
  `key_THREAD_wsrep_post_rollbacker` (mysqld.cc:16007) is defined but never used to spawn
  a thread anywhere in the tree — the drain-loop comment "rollback + post-rollback thread"
  (mysqld.cc:12628) and its `count > 2` threshold are stale legacy. Replayer THDs
  (`Wsrep_replayer_service`) shadow-save/restore `wsrep_applier` (:123/:201) and don't
  touch the counter; the NBO worker path (:769-781) DOES add_thd a new
  `wsrep_applier=true` THD while an NBO DDL runs.
- Conclusion: resolved — settled equality is `wsrep_thread_count == wsrep_applier_threads
  + 1`, with the NBO-DDL caveat noted above. Partial tag removed.

## Synthesis refinement (2026-09-10)

REFRAMED as a rider: a deterministic resize-loop test covers most of the value; only the settled-count convergence check is kept, as a rider on workloads that resize appliers anyway.
