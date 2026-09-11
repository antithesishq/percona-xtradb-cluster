---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# no-mdl-bf-bf-abort — Certification keys are complete: the MDL BF-BF suicide path is never reached

**Merged from two independent discoveries (focus 1: data integrity, as
`no-mdl-bf-bf-abort`; focus 2: concurrency, as `mdl-bf-bf-conflict-no-node-suicide`) —
both agents converged on the same two `unireg_abort(1)` funnels; the concurrency agent
additionally found the funnel is reachable via unlocked seqno/mode read races even with
perfect cert keys.**

**Type:** Safety (reachability-framed) | **Assertion:** `Unreachable` (SUT-side, missing)
| **Confidence:** High

## What led to this property

Bug pattern A, the dominant recurring PXC bug class: a DDL/FK statement's *applier-side
MDL footprint* exceeds its *certification-key set*. Two writesets that should conflict
share `depends_seqno`, apply in parallel, and one BF (high-priority) thread hits an MDL
lock held by another BF thread — an "impossible" state whose handler is **node suicide**.
Verified in `sql/wsrep_mysqld.cc` (`wsrep_handle_mdl_conflict`, `:3247-3393`):

- `:3298-3313` — when an MDL request from a BF thread conflicts with an MDL held by
  another BF thread (granted is TOI/NBO/applying): the ONLY escape is the DDL-vs-SR
  carve-out (granted is SR, requester is not → the SR side is BF-aborted, `:3301-3306`);
  **every other BF-BF combination → `WSREP_MDL_LOG(INFO, "MDL BF-BF conflict", ...)` +
  `unireg_abort(1)` unconditionally** (`:3308-3312`). There is NO ordering check and NO
  benign later-transaction-aborted resolution branch in this tree — a correction to
  earlier analysis, which described a `wsrep_thd_order_before` decision here.
  `wsrep_thd_order_before` is not called anywhere in the MDL path (its only callers are
  `storage/innobase/lock/lock0lock.cc:939` and `:2239`, the InnoDB record-lock layer).
- `:3377-3386` — second funnel: granted thread is non-BF with an inactive wsrep
  transaction and the unlocked re-read `wsrep_thd_is_BF(request_thd, false)` (`:3378`)
  returns false → `"MDL unknown BF-BF conflict"` + `unireg_abort(1)` (`:3385`).

Exit status 1 matches the shipped systemd units' RestartPreventExitStatus — in the field
the node stays down.

Ticket lineage (each a missed-cert-key instance): PXC-4512 (RENAME child vs parent DML),
PXC-4657/4684 (no-op UPDATE / trigger insert → Table_map without cert key), PXC-4789
(DROP TABLE parent with `foreign_key_checks=0`; the fix itself regressed multi-table DROP
— post-push fix 8a91391942d), PXC-4348; plus the disabled `galera-index-online-fk` test.
The class is **not closed by construction**: cert keys are hand-enumerated per statement
type (`sql/sql_parse.cc` TOI call sites, `ha_innobase::wsrep_append_keys`
`ha_innodb.cc:13116`, `wsrep_append_foreign_key` `row0ins.cc:71`), while the applier's MDL
footprint is computed independently by the server layer. Every new DDL shape is a chance
to miss a key.

**Second reachability channel — read races (CORRECTED after investigation):**

The earlier claim that a torn `wsrep_thd_order_before` seqno read can steer the MDL
fatal branch is **invalidated**: `wsrep_thd_order_before` is never called from
`wsrep_handle_mdl_conflict` — the first funnel (`:3308-3312`) is unconditional for
non-SR BF-BF pairs, so no seqno read exists to be torn. The unsynchronized seqno reads
in `wsrep_thd_order_before` (`sql/service_wsrep.cc:211-227`) matter instead for the
InnoDB record-lock layer (`lock0lock.cc:939/:2239`, `wsrep_kill_victim`), i.e. for
`bf-bf-lock-suppression-no-divergence`, not for this property.

Residual read-race channels that DO reach these funnels:

- Mode reads for the *requester* are taken under its `LOCK_wsrep_thd` at `:3268-3271`,
  but the second funnel's guard `wsrep_thd_is_BF(request_thd, false)` (`:3378`) is an
  UNLOCKED re-read after the granted lock was dropped. In the `granted_thd ==
  current_thd` flow (reschedule_waiter, see the `:3254-3261` comment), the requester is
  another thread whose mode can transition (replayer entering/leaving m_high_priority)
  between `:3270` and `:3378` — a mode flip there lands in the `:3385` fatal branch.
- A local transaction mid-replay runs on a `m_high_priority` client (replayer THD), so
  it classifies as "applying" in this function — replay churn can turn what looks like
  a BF-vs-local conflict into a BF-BF pair.

The primary reachability channel remains missed cert keys — and it is *harsher* than
previously described: any BF-BF MDL conflict (non-SR) is deterministic node suicide, no
ordering luck involved.

## Mechanism / code involved

- MDL conflict hook: `wsrep_handle_mdl_conflict` — BF requester vs BF holder, minus
  tolerated carve-outs (NBO-wait `:3279-3297`, DDL-vs-SR `:3301-3306`, FLUSH at
  `:3314-3319` waits instead of aborting, user-level locks `:3320-3327`, explicit/
  preemptable MDL `:3328-3340`, `wsrep_allow_mdl_conflict` `:3341-3347`, requester DROP
  TABLE vs non-BF granted `:3348-3354`). Carve-out safety (investigated): the wait
  branches preserve serialization (the writeset is delayed, never skipped) — they are
  stall risks feeding the FC-watchdog properties, not divergence risks. DDL-vs-SR aborts
  the SR side via the designed streaming-rollback machinery; the decision is
  deterministic cluster-wide because the DDL is totally ordered (TOI) and every node
  hosts the same SR applier — divergence-safe by construction (resolution correctness
  owned by `sr-rollback-fragment-noop`).
- InnoDB analogue: record-lock conflicts between two HP transactions are deliberately
  suppressed as "false positives" (`lock0lock.cc:581-599`) — the row-lock layer will *not*
  catch a missed key; the MDL layer's suicide is the only tripwire, and only for
  statements with MDL footprints. Pure-DML missed keys produce no abort at all → silent
  divergence (covered by `cross-node-row-equality` and
  `bf-bf-lock-suppression-no-divergence`; complementary detectors for the same root
  cause).
- Certification dependency computation: `certification.cpp do_test` `:416`,
  `do_test_v3to6` `:347`.

## Failure scenario

1. TOI DDL applies on node B (BF, MDL X-lock on table, e.g. multi-table RENAME touching an
   FK parent) while an applier writeset whose cert keys under-cover its MDL footprint (FK
   cascade / trigger / rename chain) applies concurrently (`wsrep_applier_threads > 1`).
2. Both are BF; MDL conflict fires; no carve-out matches (non-SR pair) — the fatal
   branch is unconditional.
3. `unireg_abort(1)` — node down from a legal workload, no fault required; systemd refuses
   restart; cluster degraded until operator action. On a 3-node cluster two such events =
   loss of quorum. Where no MDL is involved, the same root cause silently diverges data
   instead.

## Invariant / assertion plan

- **Primary (SUT-side, `Unreachable`, missing):**
  `Unreachable("MDL BF-BF conflict aborted the node")` at `wsrep_mysqld.cc:3308-3312` and
  a **distinct** message `Unreachable("MDL unknown BF-BF conflict aborted the node")` at
  `:3382-3385` (two callsites, two messages, per assertion-uniqueness rule). These are
  critical must-never-happen paths whose current handler destroys the process; an SDK
  `Unreachable` reports and makes the moment branchable before `unireg_abort` runs.
- **Coverage (SUT-side, `Sometimes`, missing):** there is NO benign BF-BF resolution
  branch in this tree (correction — the previously planned `Sometimes` at ":3305" pointed
  at the DDL-vs-SR abort, not an ordering-based resolution). Re-target coverage probes to
  the branches that prove BF-adjacent MDL contention was generated:
  `Sometimes("MDL conflict: NBO made applier wait")` (`:3293-3297`),
  `Sometimes("MDL conflict: DDL BF-aborted SR transaction")` (`:3301-3306`), and
  `Sometimes("MDL conflict: BF aborted local transaction")` at the ordinary BF-vs-local
  branch (`:3364-3371`). Workload-level companion: TOI DDL applied concurrently with FK
  child-table DML under `wsrep_applier_threads > 1` at least once.
- **Workload-side backstop:** the node dies before any in-process check can run without
  the SDK — harness-level signal is the error log line `"MDL BF-BF conflict"` + process
  exit 1; worth a log-based detector since it fires even without SDK instrumentation.
  VERIFIED VIABLE: WSREP INFO-level lines bypass `log_error_verbosity` via the PXC
  `mlv` filter patch (`sql/server_component/log_builtins_filter.cc:727-754`;
  `wsrep_min_log_verbosity` default 3), and `unireg_abort` calls
  `flush_error_log_messages()` as its first action (`sql/mysqld.cc:2847`), so the line
  reaches the error log before process death.
- **Bonus (SUT-side, missing):** `AlwaysOrUnreachable` in InnoDB's HP-vs-HP suppression
  (`lock0lock.cc:581-599`): when two HP transactions' record locks conflict, assert
  their writesets are certification-ordered (`depends_seqno` relation) — turns the
  silent-DML variant into a detectable event. (The previously proposed probe inside
  `wsrep_thd_order_before` "when called from the MDL-conflict path" is moot — no such
  call exists; the order_before probe belongs to the InnoDB lock-layer property.)

## Config / timing dependencies

- Requires `wsrep_applier_threads > 1` (field default 1 masks the entire class).
- Workload generator should draw from the untested candidates: multi-table RENAME,
  ALTER ADD/DROP FK cascades, TRUNCATE on FK parents, DROP DATABASE, partition exchange,
  views/triggers referencing renamed tables, `foreign_key_checks=0` sessions,
  trigger-driven inserts (PXC-4657 shape), no-op UPDATEs.
- **Online-FK shapes (gap-fill, from the disabled `galera-index-online-fk` repro —
  "fk_40 triggers inconsistency voting", a known in-tree cluster-inconsistency generator
  disabled rather than fixed; it sources
  `mysql-test/suite/innodb/t/innodb-index-online-fk.test`)**: online
  `ALTER TABLE child ADD CONSTRAINT ... FOREIGN KEY ... ON DELETE CASCADE/SET NULL
  ON UPDATE CASCADE, ALGORITHM=INPLACE` under `foreign_key_checks=0`; `CREATE INDEX` on
  FK parents/children under concurrent FK DML; multi-FK single ALTER (fk_5+fk_6 shape);
  FK referencing a non-unique secondary index (`restrict_fk_on_non_standard_key=OFF`);
  expected-error ALTERs (`ER_FK_NO_INDEX_PARENT/CHILD`, `ER_FK_DUP_NAME`) and DROPs of
  FK-laden tables interleaved with cascading DELETE/UPDATE on the parents. (The repro's
  `SET DEBUG='+d,...'` error-injection legs are debug-DBUG-tier only; the statement
  shapes themselves need no debug build.)
- Replay churn (see `trx-replay-never-fatal` recipe) to exercise the unlocked mode/seqno
  read races at MDL-conflict time.
- No fault injection strictly required — pure concurrency — but network latency/CPU
  throttle widen applier-interleaving windows. Node restarts NOT required (works even with
  kill faults disabled).
- Harness note: decide restart policy for exit-status-1 deaths deliberately; the property
  itself only needs the abort detected.

## Open Questions

- Complete enumeration of statements whose applier MDL footprint exceeds their cert-key
  set (from `sql_parse.cc` TOI call sites + `service_wsrep.h` key appenders) — the
  workload generator's target list; a fuller enumeration directly raises bug yield.
  `(partial: class is open by construction — cert keys are hand-enumerated per statement
  type while MDL footprints are computed independently by the server layer; the historical
  generators (multi-table RENAME, FK cascades, TRUNCATE parents, trigger inserts, no-op
  UPDATEs, foreign_key_checks=0) are listed under Config below; a closed-form list would
  require per-statement diffing of key appenders vs MDL acquisition and is a workload-
  implementation work item, not a static-analysis deliverable)`

### Investigation Log

#### Complete enumeration of statements with MDL footprint > cert-key set

- Examined: `wsrep_handle_mdl_conflict` carve-outs (which statements escape the funnel);
  ticket lineage already in this file (PXC-4512/4657/4684/4789/4348); key-appender sites
  (`ha_innodb.cc` `wsrep_append_keys`, `row0ins.cc` `wsrep_append_foreign_key`).
- Found: the class is open by construction — cert keys are hand-enumerated per statement
  type while the applier MDL footprint is computed independently by the server layer;
  each historical ticket is one instance. The DROP TABLE (`:3348-3354`) and
  `wsrep_allow_mdl_conflict` (`:3341-3347`) branches shrink the funnel's surface for
  those shapes.
- Not found: a static, closed-form list; producing one requires per-statement diffing of
  key appenders against MDL acquisition — a workload-generator implementation task.
- Conclusion: tagged `(partial: ...)` — the known-generator list in Config/timing stands
  as the day-one workload target set; keep extending it from new tickets.

#### Under which conditions does `wsrep_thd_order_before` see an unassigned seqno for a BF MDL holder?

- Examined: `sql/wsrep_mysqld.cc:3247-3393` (`wsrep_handle_mdl_conflict`, full read);
  callers of `wsrep_thd_order_before` across sql/, storage/innobase/, include/
  (grep — only `lock0lock.cc:939`, `:2239`, plus declaration).
- Found: `wsrep_thd_order_before` is NOT called from the MDL conflict path at all. The
  BF-BF branch (`:3298-3313`) aborts the node unconditionally for any non-SR BF-BF pair;
  there is no ordering decision to corrupt. The unlocked-seqno-read hazard exists only at
  the InnoDB record-lock callers (`wsrep_kill_victim` `lock0lock.cc:926-967`, and the
  HP-bypass check at `:2239`), which belong to `bf-bf-lock-suppression-no-divergence`.
- Also found: a narrower real race remains in the MDL path — the second funnel's guard
  `wsrep_thd_is_BF(request_thd, false)` (`:3378`) is an unlocked mode re-read; in the
  `granted_thd == current_thd` (reschedule_waiter) flow the requester's mode can
  transition between the locked read at `:3270` and `:3378`.
- Conclusion: question resolved by invalidation — the premise (torn order_before read
  steering the MDL fatal branch) is wrong for this tree. Property mechanism text and
  assertion plan corrected accordingly; the property remains valid (and stricter: any
  non-SR BF-BF MDL conflict is deterministic suicide).

#### Are the tolerated carve-out branches (DDL-vs-SR, FLUSH-wait, ULL) divergence-safe?

- Examined: all branches of `wsrep_handle_mdl_conflict` (`:3279-3388`);
  `wsrep_abort_thd` (`sql/wsrep_thd.cc:331-359`); `wsrep::transaction::bf_abort`
  streaming handling (`wsrep-lib/src/transaction.cpp:1084-1131`).
- Found: NBO-wait, FLUSH-wait, user-level-lock, and explicit-lock branches make the BF
  requester WAIT — the writeset is delayed, never skipped, so ordering/serialization is
  preserved: stall risk (applier blocked behind a local lock holder → FC pause), not
  divergence risk. DDL-vs-SR aborts the SR side via `wsrep_abort_thd` →
  streaming-rollback machinery; the choice of victim is deterministic cluster-wide
  because the DDL is in total order and every node hosts the same SR transaction.
- Not found: any carve-out that skips or reorders a writeset.
- Conclusion: resolved — carve-outs are liveness hazards (covered by
  `flow-control-pause-releases` / `commit-order-monitor-released-no-cluster-stall`
  watchdogs), not divergence channels. SR-rollback resolution correctness is owned by
  `sr-rollback-fragment-noop`.

#### Is the INFO log line flushed before `unireg_abort` (backstop detector viability)?

- Examined: `WSREP_MDL_LOG`/`WSREP_LOG` macros (`sql/wsrep_mysqld.h:290-316`); PXC log
  filter patch (`sql/server_component/log_builtins_filter.cc:715-771`);
  `wsrep_min_log_verbosity` (`sql/sys_vars.cc:8613-8619`, default 3);
  `unireg_abort` (`sql/mysqld.cc:2828-2860`).
- Found: WSREP log lines carry an `mlv` item; the patched filter suppresses the
  `log_error_verbosity` DROP rule whenever prio <= wsrep_min_log_verbosity (default 3 =
  INFORMATION), so WSREP INFO lines are always emitted regardless of
  `log_error_verbosity`. `unireg_abort` calls `flush_error_log_messages()` as its first
  substantive action (`mysqld.cc:2847`) before any wsrep teardown.
- Conclusion: resolved — yes. The `"MDL BF-BF conflict"` line reaches the error log
  before process exit; the log-grep + exit-1 backstop detector is viable without SDK
  instrumentation.
