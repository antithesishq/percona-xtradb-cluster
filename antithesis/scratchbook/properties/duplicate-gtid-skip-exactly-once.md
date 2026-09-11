# duplicate-gtid-skip-exactly-once — an already-executed GTID delivered again is skipped as a clean no-op

**Focus area:** Idempotency and Replay. **Commit:** f9ecb3ebe8ff (branch 8.4).
**Confidence:** Medium — the skip machinery and the regression history (PXC-4664/4688, PXC-4823)
are confirmed; the residual-deadlock claim comes from Percona's own commit message ("after fixing
sigsegv, it is still possible to end up with a deadlock") and has not been independently
reproduced here.

## Claim under test

When a transaction with an explicit `gtid_next` that is already in `gtid_executed` reaches a PXC
node — via an async replication channel into the cluster (multi-source, failover re-pointing, or
a duplicate emitted upstream), or via a client setting `gtid_next` explicitly — MySQL's GTID
auto-skip must make it a **no-op executed zero additional times**, and PXC's
`Wsrep_async_monitor` bookkeeping must absorb the skip without crashing or wedging the applier
pipeline:

1. Data effects of a duplicate GTID appear exactly once cluster-wide (the skip produces no
   writeset, no effects, no local-GTID leak).
2. The skipped transaction's monitor seqno is registered via `skip()` so later transactions'
   `enter()`/`leave()` never wait on it — the replica channel keeps advancing (no permanent
   stall) and the node never asserts/aborts.

This is the PXC-4664/4688 regression family (nullptr deref SIGSEGV on duplicate explicit
gtid_next through the async monitor) plus PXC-4823 (a scheduled seqno that is never
entered/skipped → permanent applier stall; fixed in-tree — merge commits `6c5b5c63547`
(8.4) / `43dffa284e9`, ancestors of f9ecb3e).

## Code paths

- Skip notification: `sql/rpl_gtid_execution.cc:388-405` (`gtid_skip_transaction` tail) —
  "Despite the transaction was skipped, it needs to be updated in the Wsrep_async_monitor";
  guarded `dynamic_cast<Slave_worker*>` + null checks (`assert(sw != nullptr)` — the PXC-4664
  fix shape) → `wsrep_async_monitor->skip(seqno)`. Sibling skip sites:
  `sql/log_event.cc:11237`, `sql/sql_parse.cc:338` (per sut-analysis §3.1 boundary E).
- Monitor: `sql/wsrep_async_monitor.cc` — `enter()` returns early for skipped seqnos (:32-33,
  :67-68); waiters drain leading skipped seqnos from `scheduled_seqnos` (:37-56); `skip()`
  (:112-121+) idempotent per seqno; GC prunes `skipped_seqnos <= seqno - m_workers_count`
  (:94-106). Structural hazards (sut-analysis §5.4): sized by
  `opt_replica_parallel_workers`, not `wsrep_applier_threads` (rpl_replica.cc:7285-7290); a
  `skip(n)` pruned before a late `enter(n)` → permanent wait; **stale `skipped_seqnos` survive
  source binlog rotation** (sequence_number restarts at 1, monitor never reset —
  rpl_mta_submode.cc:601-604 vs rpl_replica.cc:7722-7724) → later genuine seqnos short-circuit
  the ordering monitor.
- Mismatch abort: `sql/wsrep_mysqld.cc:2948-2956` + `wsrep_async_monitor.cc:85-91` — `enter()`
  early-returns when `thd->killed` but `leave()` is unconditional → seqno mismatch →
  `unireg_abort(1)` (exit status 1: shipped systemd units do not restart).
- GTID dedup substrate: `gtid_pre_statement_checks` / `is_already_logged_transaction` →
  `gtid_skip_transaction` (rpl_gtid_execution.cc:333+); debug-build check that the GTID really is
  in `executed_gtids` (:407-414) — release builds skip this verification.
- Related leak family (effects of the *first* execution mis-recorded → dedup keyed on a wrong
  set): local GTID event leaks PXC-4313 (RSU), PXC-4312/4504 (DROP IF EXISTS), PXC-4238 (UDF),
  PXC-4034 (sql_log_bin=0), PXC-4544 (RESET BINARY LOGS); unlocked `wsrep_sidno` →
  `rpl_gtid_owned` corruption PXC-4652 (needs `wsrep_applier_threads > 1`).
  All seven are confirmed real defects with in-tree fix commits at f9ecb3e (verified via
  `git log --grep`): PXC-4313 `2355c7b1440`, PXC-4312 `cc8ec98a835`, PXC-4504
  `494ccbf581c`, PXC-4238 `421d0d2b5f4`, PXC-4034 `7933daa988f`, PXC-4544 `7d7cce8e023`,
  PXC-4652 `0470e0ede5c`. They are motivational context (the pattern-H family), not this
  property's load-bearing claims.

## Failure scenarios

- **Crash on duplicate:** a new interleaving reaches the monitor with a THD shape the PXC-4664
  null-checks don't cover (e.g. duplicate GTID on the *first* event after worker start, or during
  `STOP REPLICA`) → SIGSEGV or `unireg_abort(1)` → node down, not restarted.
- **Permanent stall:** skip pruned by GC before a slow out-of-order worker calls `enter()` on a
  neighboring seqno (workers > monitor window), or a skipped seqno recorded but its waiters
  never re-checked (PXC-4823 shape) → replica channel frozen; if the channel feeds the cluster,
  writes stop arriving with zero errors anywhere.
- **Double execution:** binlog-rotation seqno reuse hits a stale `skipped_seqnos` entry → a
  *genuine* transaction's `enter()` short-circuits → commit-order bypass → it races a dependent
  transaction → applied out of order or duplicated relative to peers (each PXC node applies the
  async stream independently; ordering divergence here is per-node data divergence).
- **GTID recorded but effects absent (or vice versa):** duplicate detection consults
  `gtid_executed` while wsrep-side effects come from the writeset — a leak-family bug desyncs
  the two, making the "duplicate" classification wrong in either direction.

## Suggested assertions (all missing)

- **Primary (workload, Always):** the workload drives an async replication channel into one PXC
  node and periodically re-points/restarts it (and/or injects explicit
  `SET gtid_next='<already-executed>'; BEGIN; ...; COMMIT` no-op transactions): every marker row
  shipped over the channel appears exactly once on every PXC node; `gtid_executed` on all PXC
  nodes converges. `Always`.
- **Liveness (workload, Sometimes):** `Sometimes(replica channel advanced past a skipped
  transaction)` — check `Executed_Gtid_Set` progress after each injected duplicate; guards
  against the silent-stall failure mode (a pure Always check passes vacuously while frozen).
- **SUT-side (missing, Always):** in `Wsrep_async_monitor::leave()` — assert the seqno being
  left was entered (converts the current mismatch-`unireg_abort(1)` into a property signal);
  in `enter()` — assert never blocking on a seqno older than the GC horizon (the
  pruned-skip stall, currently invisible: raw std::mutex/condvar, no performance_schema).
- **SUT-side (missing, Unreachable):** the `unireg_abort(1)` branch at
  `wsrep_mysqld.cc:2948-2956`.

## Fault / workload requirements

- **Topology requirement (flag):** needs an async replication source feeding a PXC node with
  `replica_parallel_workers > 1` and `replica_preserve_commit_order = ON` (the monitor is only
  active then) — a harness without an async channel cannot exercise this property. The
  explicit-`gtid_next` client variant needs no extra topology but covers only part of the surface.
- No injected faults strictly required; network faults (default-on) between source and replica
  produce the retry/reconnect duplicates this property is about. `STOP/START REPLICA` and source
  binlog rotation (`FLUSH BINARY LOGS`) under load are workload-driven levers for the
  seqno-reset hazard. Node termination (often disabled) would add the recovery-duplicate case.

## Open questions

None — all three resolved (see Investigation Log). Design consequences:

- The residual deadlock is **PXC-4665**, a *known, won't-fix* deadlock between a wsrep
  applier and a local connection when two nodes use the same explicit `gtid_next` and one
  holds its transaction open. The exact interleaving is documented (commented out) in
  `mysql-test/suite/galera/t/galera_gtid.test:36-57`. It needs NO async channel — plain SQL
  on two nodes. The workload can target it directly, but a liveness failure there is an
  expected known issue, not a new finding: either flag it as a known-issue property outcome
  or avoid holding an open transaction under a contested explicit gtid_next.
- Binlog rotation DOES restart sequence_numbers against retained monitor state (static
  chain complete; runtime demo left to the workload). The stale-skip double-order hazard is
  real but narrow: only skips within the last `m_workers_count` seqnos before rotation
  survive GC, and each survivor S deterministically causes an ordering bypass if the new
  binlog file's stream reaches seqno S (GC arithmetic can never remove S in time — at
  enter(S), remove_upto = S-1-workers < S).
- `wsrep_use_async_monitor = OFF` is a supported startup config (READ_ONLY, CMD_LINE,
  default ON — sql/sys_vars.cc:8666-8677). With OFF the monitor is never constructed
  (rpl_replica.cc:7284-7291) and every enter/leave/skip/schedule site no-ops via null
  checks; the GTID auto-skip contract itself is monitor-independent. OFF simply reverts to
  Commit_order_manager-only ordering — the pre-PXC-4173 configuration whose wsrep-vs-relay
  commit-order deadlock the monitor was built to fix. An OFF variant would test that
  deadlock regression (a different property), not this one; not required here.

### Investigation Log

#### What is the residual post-fix deadlock interleaving (PXC-4664/4688 commit note)?

(2026-09-10, open-questions pass)

- Examined: commit 1bf0ec6dd844 full message ("After fixing sigsegv, it is still possible
  to end up with a deadlock (PXC-4665). It is an old issue."); grep for PXC-4665 across
  sql/ and mysql-test/; `mysql-test/suite/galera/t/galera_gtid.test:36-57`.
- Found: the deadlock is tracked as PXC-4665 and predates the async monitor ("an old
  issue"). The test file documents the exact interleaving, commented out because "we don't
  plan to fix PXC-4665 for now": node_1 `SET SESSION GTID_NEXT='uuid:N'; BEGIN;` (holds
  GTID ownership, transaction open) → node_2 same GTID_NEXT, `BEGIN; COMMIT;` (replicates)
  → node_1's wsrep applier applying the replicated trx deadlocks against the local
  connection owning the GTID; node_1's own COMMIT completes the deadlock. In versions with
  the PXC-4664 fix (>= 8.0.41 line) the SIGSEGV became this deadlock.
- Not found: any fix or workaround in this tree; no async channel involvement — the
  interleaving is pure client-side explicit gtid_next on two nodes.
- Conclusion: resolved — interleaving known and directly targetable; it is a KNOWN won't-fix
  deadlock, so the property must treat hitting it as a known issue (or the workload avoids
  holding open transactions under a contested explicit gtid_next).

#### Does binlog rotation reuse sequence numbers against retained monitor state?

(2026-09-10, open-questions pass)

- Examined: source-side `Commit_order_trx_dependency_tracker::rotate()`
  (sql/rpl_trx_tracking.cc:199-204 — offsets updated so per-file sequence_numbers restart);
  replica-side raw use of `gtid_log_ev->sequence_number` (sql/rpl_mta_submode.cc:596-604;
  schedule at sql/log_event.cc:2679-2688; worker-side `Slave_worker::sequence_number()`
  rpl_rli_pdb.h:882-885); monitor lifecycle (created rpl_replica.cc:7283-7291, deleted only
  at applier stop :7719-7726 — survives source rotation); full monitor implementation
  (sql/wsrep_async_monitor.cc).
- Found: the static chain is complete — sequence_numbers restart per source binlog file,
  the replica consumes them raw, and the monitor retains `skipped_seqnos` /
  `scheduled_seqnos` across rotation. Quantified the hazard: GC (`remove_upto = seqno -
  m_workers_count`) means only skips within the last `m_workers_count` pre-rotation seqnos
  survive; each survivor S collides deterministically when the post-rotation stream reaches
  S (at that moment remove_upto < S always), making a genuine trx bypass both enter() and
  leave() — a silent preserve-commit-order violation, no crash. The `scheduled_seqnos`
  queue itself is compared by FIFO front-equality (no monotonicity assumption), so the
  first post-rotation trx does NOT mismatch: no trivial crash at rotation.
- Not found: any monitor reset on rotation or on `FLUSH BINARY LOGS` (only applier stop
  deletes it). Runtime confirmation is inherently out of scope for static analysis — the
  workload's FLUSH BINARY LOGS lever exercises it.
- Conclusion: resolved (statically) — the double-execution/ordering-bypass scenario stands,
  with a precise trigger recipe: cause a skip within the last `replica_parallel_workers`
  transactions of a binlog file, rotate, and run the new file up to the same seqno.

#### Is there a supported configuration with wsrep_use_async_monitor OFF, and does the no-op contract hold?

(2026-09-10, open-questions pass)

- Examined: sql/sys_vars.cc:8666-8677 (Sys_var_bool, READ_ONLY GLOBAL, CMD_LINE(OPT_ARG),
  DEFAULT(true), help text: "only allowed to be changed through command line");
  construction guard rpl_replica.cc:7283-7291; all call sites' null checks
  (wsrep_mysqld.cc:2963/:2984, rpl_gtid_execution.cc:398, sql_parse.cc:335/:443,
  log_event.cc:2684/:11233).
- Found: OFF is a legitimate startup configuration; with it the monitor pointer stays null
  and every site no-ops. GTID auto-skip (`gtid_pre_statement_checks` →
  `gtid_skip_transaction`) is upstream-MySQL machinery independent of the monitor, so the
  duplicate-GTID no-op data contract holds; commit ordering falls back to
  Commit_order_manager only, reintroducing the PXC-4173 wsrep-vs-relay-order deadlock risk
  the monitor was created to fix (per the sysvar help text).
- Conclusion: resolved — no OFF variant needed for this property; an OFF variant would be a
  separate PXC-4173 deadlock-regression property if ever wanted.
