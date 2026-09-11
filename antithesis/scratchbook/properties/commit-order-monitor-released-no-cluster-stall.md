# commit-order-monitor-released-no-cluster-stall

**Type:** Liveness (checked as a quiesce-time convergence assertion).
**Focus:** concurrency — every seqno entered into the apply/commit-order monitors must be
left; one leaked slot wedges the applier pipeline and, via flow control, the whole cluster.

## What led to this property

Galera's total-order guarantees are enforced by three ring monitors (LocalOrder, ApplyOrder,
CommitOrder — `percona-xtradb-cluster-galera/galera/src/monitor.hpp`, 65536 slots, single
mutex). The monitors have no leak detection: a seqno that enters and never leaves blocks
`last_left` forever; every later transaction queues behind it; the recv queue grows; flow
control pauses **all writes on all nodes**. The node stays PRIMARY and Synced while doing
this — no eviction, no vote, no error. This is bug pattern C in the SUT analysis, with two
recent regression targets fixed in the last 18 months:

- **PXC-4844** (fix ba5eb494fb9, 2026-04-28): a *failed TOI* leaves a dirty
  Diagnostics_area; the next **empty writeset** skips the DA reset → `cleanup_context` rolls
  back → the commit-order monitor for that seqno is **never released**. The fix audited one
  carrier of a stale DA; the commit discussion notes other stale-DA carriers are unaudited.
  Repro knob in-tree: DBUG `wsrep_force_empty_apply`, test `pxc-4844.test`.
- **MDEV-38843** (wsrep-lib 5eeef40, 2026-05-27): apply error followed by a *rollback
  error* → `log_dummy_write_set` skipped → seqno stuck; "node stays PRIMARY while silently
  locking the cluster."
- Related family: PXC-4845 (monitors never initialized after skipped IST → same cluster-wide
  FC stall; fix only converts hang to shutdown), PXC-4399 (FLUSH TABLES TOI holds
  CommitMonitor vs INSERT), PXC-4823 (async OPTIMIZE never enters/skips).

The structural reason this class recurs: release of the monitors is distributed across many
error-handling paths (apply error, rollback error, TOI failure, empty writeset, dummy
writeset, replay), and several of those paths were added or modified per-bug. There is no
single "on scope exit, monitor left" discipline, and the in-code enforcement is `assert()`
(compiled out in release).

Interacting hazard verified in-tree: the **interim commit** — `wsrep_ordered_commit`
releases the commit-order monitor from *inside* the binlog flush-queue mutex
(`sql/rpl_commit_stage_manager.cc:104-133` → `replicator_smm.cpp:1517`), and is silently
disabled unless `opt_log_replica_updates && opt_binlog_order_commits`
(`sql/wsrep_trans_observer.h:356-373`). Whether the monitor is released early (flush queue)
or late (after_commit) is a function of two unrelated server options — test both settings.

Also: ring overflow. `Monitor::would_block` when `seqno - last_left >= 65536`
(monitor.hpp:339-343); `self_cancel` on a blocked ring logs "Deadlock is very likely" and
loops (monitor.hpp:242-253, verified :250). A single stuck seqno plus sustained writes
converts the stall into that pathological loop; even nominally read-only monitor APIs block.

## Failure scenario

1. Fault or error path (TOI DDL failure, apply error + rollback error, killed applier)
   causes one applier on one node to exit its writeset without releasing the apply or
   commit-order monitor for seqno S.
2. `last_left` on that node freezes at S-1. Appliers drain; recv queue grows to
   `gcs.fc_limit`; the node emits FC pause.
3. Every node stops committing. Cluster is fully wedged; all nodes report Synced/PRIMARY.
   Only operator restart of the wedged node recovers.

## Invariant / assertion plan

- **Primary (workload-side, `Always` at drain points):** after each fault-quiesce phase, all
  nodes reach `wsrep_last_committed == max(replicated seqno)` and accept a probe write,
  within the drain window; and `wsrep_local_recv_queue` returns to ~0 on every node.
  Message: `"cluster drains and commits after faults heal"`. Rationale: liveness properties
  in Antithesis are best checked as an eventually-condition evaluated at explicit
  quiesce/validation points — a pure `Sometimes` would pass runs that stalled late.
- **Progress markers (workload-side, `Sometimes`):**
  `Sometimes("TOI DDL failed and cluster subsequently committed")`,
  `Sometimes("apply error path exercised and cluster subsequently committed")` — these are
  the PXC-4844 / MDEV-38843 shapes; without them a green run may never have entered the
  dangerous error paths.
- **SUT-side sharper check (missing):** in `galera::Monitor` (monitor.hpp), on `leave()`
  compare against entered set; better: `Always("monitor slot released by same seqno that
  entered")` and a periodic `Sometimes`/gauge of `last_entered - last_left` (a growing gap
  with idle appliers is the leak signature long before FC). Also
  `Unreachable("monitor ring overflow self_cancel loop")` at monitor.hpp:242-253 — under any
  sane workload the 65536 gap should never be reached; reaching it means a leaked slot
  upstream.

## Config / timing dependencies

- Drive the known triggers: failing TOI DDL (DDL against missing objects, DDL racing DML),
  apply errors demoted or not via `wsrep_ignore_apply_errors`, `KILL` on applier-conflicting
  local transactions, empty writesets (GTID-only/empty transactions).
- Test both `log_replica_updates`/`binlog_order_commits` combinations (interim-commit on and
  off paths release the monitor at different places).
- `wsrep_applier_threads > 1`.
- Faults: network partitions + heal, node hang (both default-available). Node kill+restart
  amplifies (PXC-4845 shape) but is often disabled — **flag: the PXC-4845 variant needs
  node termination; the PXC-4844/MDEV-38843 variants do not.**

## Open questions

- What is a safe drain-window bound under Antithesis time semantics? `(partial: no
  code-side bound exists — FC pause is legitimately unbounded while any member is in
  state transfer and monitor waits are untimed; the sharper detector is the PXC-only
  `wsrep_monitor_status (L/A/C)` status var — a frozen (last_entered, last_left) window
  on one monitor while recv processing is idle localizes a leak long before any
  wall-clock bound trips; the numeric drain bound itself remains a harness-tuning choice
  at workload-implementation time — start at minutes of virtual time)`

Resolved (see Investigation Log):

- **Stale-DA carriers:** the PXC-4844 fix is consumer-side and generic — the empty-
  writeset apply path resets the THD/DA via `mysql_reset_thd_for_next_command`
  (sql/wsrep_applier.cc:150-161; the in-code comment covers "a prior failed TOI (or
  other statement)"), and non-empty writesets get the same reset from their first row
  event. No per-carrier audit is required for the assertion plan; the workload should
  still drive failed TOI/NBO plus empty writesets to regression-test the reset.
- **`wsrep_ignore_apply_errors != 0` CLOSES (not opens) the MDEV-38843 window** for the
  demoted shapes: `wsrep_must_ignore_error`/`wsrep_ignored_error_code`
  (sql/wsrep_mysqld.cc:3425-3475) swallow the error before wsrep-lib sees it, so the
  apply reports success and monitors are released on the normal commit path — at the
  price of accepting silent divergence. Keep the field default (0) in the workload so the
  rollback+dummy seam is exercised. The MDEV-38843 fix itself is verified present in the
  vendored wsrep-lib (server_state.cpp:345-359: dummy write set logged even when
  rollback fails, comment states it releases commit order).

## SUT-side instrumentation suggestions (all missing)

- `Unreachable("monitor ring overflow self_cancel loop")` — monitor.hpp:242-253.
- The per-monitor `last_entered - last_left` gauge needs NO SUT instrumentation: PXC
  already exports `wsrep_monitor_status (L/A/C)` = `[(last_entered, last_left) × 3]`
  (replicator_smm_stats.cpp:154-156, :255-277; Monitor::stats monitor.hpp:329-334) —
  poll it from the workload as the pre-FC leak signature.
- `Sometimes("empty writeset applied after failed TOI")` — the exact PXC-4844 interleaving
  (near the `wsrep_force_empty_apply` DBUG site, sql/wsrep_applier.cc:148).
- `Always("dummy writeset logged for every failed apply", ...)` at the MDEV-38843 fix site
  (wsrep-lib server_state.cpp:345-359, apply error + rollback error path) — regression
  guard on the fix, which is confirmed present in this tree.

### Investigation Log

#### What is a safe drain-window bound under Antithesis time semantics?

- Examined: FC pause machinery (no timeout found on writer spin loops,
  replicator_smm.cpp:733/820-822/2075-2077); monitor waits (untimed, monitor.hpp);
  donor/desync FC exemption (gcs.cpp:441-442); provider stats surface
  (replicator_smm_stats.cpp).
- Found: no code-side bound exists; FC pause is legitimate for the whole duration of a
  state transfer. Found instead the PXC-only `wsrep_monitor_status (L/A/C)` status var
  exposing per-monitor (last_entered, last_left) — a leak detector that does not depend
  on a wall-clock bound.
- Not found: any config/timeout that would justify a specific numeric bound.
- Conclusion: tagged `(partial)` — bound is a harness-tuning decision; gauge-based
  detection recommended as the primary signal.

#### Which other "stale Diagnostics_area carriers" exist besides failed TOI?

- Examined: the PXC-4844 fix as present in-tree (sql/wsrep_applier.cc:140-165 —
  `wsrep_apply_events` empty-buffer branch calling `mysql_reset_thd_for_next_command`);
  the `wsrep_force_empty_apply` DBUG knob (:148); Rows_log_event reset behavior
  (referenced by the fix comment).
- Found: the fix neutralizes stale DAs at the CONSUMER (empty-writeset apply resets the
  THD unconditionally; the comment explicitly generalizes to "a prior failed TOI (or
  other statement)"); non-empty writesets are reset by their first event's
  `mysql_reset_thd_for_next_command`.
- Not found: an applier writeset path that skips both resets.
- Conclusion: resolved — per-carrier enumeration unnecessary; workload keeps driving
  failed TOI/NBO + empty writesets as regression triggers for the reset itself.

#### Does `wsrep_ignore_apply_errors != 0` close or open the MDEV-38843 window?

- Examined: `wsrep_must_ignore_error` and `wsrep_ignored_error_code`
  (sql/wsrep_mysqld.cc:3425-3475); flag definitions (wsrep_mysqld.h:127-131, default 0
  wsrep_mysqld.cc:143); the apply-error path in wsrep-lib `apply_write_set`
  (server_state.cpp:276-361).
- Found: demotion happens at event-application level — an ignored error means
  `apply_err == 0` upstream, so the transaction commits on the normal path (monitors
  released by commit), never entering the rollback+dummy seam. The MDEV-38843 fix
  (always log dummy even on rollback failure) is present at server_state.cpp:345-359.
- Conclusion: resolved — nonzero settings close the window for the listed error shapes
  (DDL: ER_DB_DROP_EXISTS/ER_BAD_TABLE_ERROR/ER_CANT_DROP_FIELD_OR_KEY or all-DDL with
  0x4; DML: DELETE_ROWS + ER_KEY_NOT_FOUND with 0x2) while masking divergence; run the
  workload at the default 0.
