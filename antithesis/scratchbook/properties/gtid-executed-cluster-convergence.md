# gtid-executed-cluster-convergence — Evidence

**Focus area:** Evaluation gap-fill — bug pattern H (GTID on the wsrep path) had ZERO
catalog coverage (evaluation/synthesis.md Gap 1; sut-analysis §6.9, §9.2-H).
**Confidence:** High on the assignment mechanism (group-commit GTID minting read directly);
medium on the exact divergence signature of each historical leak (fix commits enumerated,
not all replayed).

## Claim under test

Galera documentation claims the cluster GTID sequence is kept consistent — GTID
incremented only on certification pass, with dummy/empty writesets keeping the sequence
gap-free (`percona-xtradb-cluster-galera/docs/.../certification.rst:207-215`;
`sql/wsrep_trans_observer.h:186-200`). The vendor's own observable for this claim is
literal `@@global.gtid_executed` equality across nodes under `wsrep_sync_wait`
(`mysql-test/suite/galera/t/galera_gtid.test:26-34`). The property tests that observable
under faults: **at quiesced checkpoints, all Synced primary-component nodes report the
identical `@@global.gtid_executed` set, and no node holds GTIDs outside the cluster UUID**
(no errant server-uuid GTIDs), given a workload that avoids the known-legitimate local-GTID
generators (see below).

## Why GTID divergence does NOT reduce to cross-node-row-equality

`gtid_executed` is metadata about replication history, not row data. Two nodes can hold
identical rows while their GTID sets diverge (a leaked local GTID, a skipped gno). The
damage surfaces later and elsewhere: async replicas fed from different PXC nodes see
different histories (errant-GTID failover errors), point-in-time recovery mis-anchors, and
a future node-to-node comparison declares false divergence. The checksum oracle is blind to
all of it — this is its own terminal oracle for the GTID plane.

## Code (validated, commit f9ecb3e)

### The convergence mechanism

- **Shared cluster sidno**: `sql/wsrep_mysqld.cc:673-694` (`wsrep_init_sidno`) — for wsrep
  protocol ≥ 4 the sid IS the cluster state UUID byte-for-byte; registered once in
  `global_tsid_map` as `wsrep_sidno`.
- **Per-node minting at group commit**: `sql/binlog.cc:1740-1811`
  (`assign_automatic_gtids_to_flush_group`): a transaction with a defined wsrep seqno
  (TOI meta / NBO meta / trx ws_meta checked at :1795-1800) and
  `!head->wsrep_skip_wsrep_GTID` gets `ctx_sidno = wsrep_sidno` (:1803-1804); everything
  else falls to the server's own sidno (:1805-1806). **Each node independently assigns the
  next free gno in the cluster-sidno set at its own group commit.** Convergence therefore
  holds iff every node binlogs exactly the same number of cluster-sidno transactions — the
  gno values self-align only because the counts match. Any node that mints one extra or one
  fewer diverges permanently and silently.
- **PXC-4652 regression site**: `sql/binlog.cc:1749-1778` — the fix (commit `aad68aa1274`,
  2025-05-05) adds `locked_sidno_set.add_lock_for_sidno(wsrep_sidno)` when any transaction
  in the flush group is WSREP. Pre-fix, `wsrep_sidno` was mutated UNLOCKED under
  `wsrep_applier_threads > 1` → `rpl_gtid_owned` map corruption → SIGSEGV. The harness
  config (`wsrep_applier_threads=4`) is exactly the trigger population; the fix is a
  single call-site patch of the kind that historically regresses (cf. commit message: the
  rework in 674df5a8 silently dropped the old locking).

### Divergence generators (the workload's target list)

- **wsrep_write_dummy_event is a NO-OP**: `sql/wsrep_binlog.cc:393-402` returns 0 without
  writing anything (`wsrep_write_dummy_event_low` is a literal `::abort()` stub). Called
  from `wsrep_TOI_begin_failed` (`sql/wsrep_mysqld.cc:2403-2429`) and
  `wsrep_NBO_begin_failed` (`:2438-2460`) when a TOI/NBO already obtained a seqno but
  failed to start — the seqno is consumed cluster-wide with nothing entering the local
  binlog (sut-analysis §6.9, focus 11 §4.4). Whether this desynchronizes `gtid_executed`
  between cluster nodes (vs. only creating a binlog/GTID discontinuity visible to async
  replicas) is the property's top open question — see below.
- **Local-GTID leak family (all fixed; all regression targets)**: PXC-4313 (RSU, fix
  merge `2355c7b1440`), PXC-4312/PXC-4504 (`DROP TABLE IF EXISTS` of nonexistent, fixes
  `cc8ec98a835` / `494ccbf581c`), PXC-4238 (UDF execution minted errant GTID,
  `defd0d6695a`), PXC-4034 (`sql_log_bin=0`, `d5ecac15b92`), PXC-4544
  (`RESET BINARY LOGS AND GTIDS`, fix merge `7d7cce8e023`). All fix commits verified
  in-tree, ancestors of f9ecb3e. Each was an ordinary statement minting a
  **server-uuid** GTID on one node only. The class is open by construction — fixes are
  per-statement patches; the errant-GTID clause of this property is the generalized guard.
- **Deliberate server-uuid minting that still exists**: `sql/sql_table.cc:3726` and
  `:3848` set `thd->wsrep_skip_wsrep_GTID = true` for DROP TEMPORARY TABLE binlog groups
  (reset at `:3961`; also cleared per-command at `sql/wsrep_client_service.cc:116`). Temp
  tables are session-local and not replicated, so under `gtid_mode=ON` a node that runs
  them mints server-uuid GTIDs **by design**. The workload must avoid temporary tables (or
  the errant-GTID clause needs a carve-out).
- **PXC-4526 tagged GTIDs** (fix `8cdccbd8524`, 2025-09-09): a client committing with
  `SET gtid_next = 'uuid:tag:N'` (8.4 tagged GTIDs) produced a truncated
  `GTID_TAGGED_LOG_EVENT` in the replicated writeset when the event checksum was
  `CHECKSUM_ALG_UNDEF` → apply failure → inconsistency vote → eviction. The fix commit
  itself concedes "questionable if original logic ... is correct" — a tagged-GTID leg in
  the workload is a direct regression probe (it can fail as eviction, not just GTID
  divergence — coordinate with the un-injected-vote rule).
- **PXC-4665 avoidance rule** (catalog-wide open question): the workload's explicit
  `gtid_next` legs must not hold open transactions under a contested explicit `gtid_next`
  (known won't-fix applier-vs-local deadlock).

### Test/config baseline

`galera_gtid-master.opt`: `--gtid-mode=ON --log-bin --log-slave-updates
--enforce-gtid-consistency`. The MTR test only checks equality once, fault-free, on a
2-node cluster; nothing in-tree tests GTID convergence under membership churn, TOI
failure, restarts, or parallel apply.

## Failure scenario

1. 3-node cluster, `gtid_mode=ON`, steady multi-node DML + occasional DDL, TOI
   begin-failure injection (e.g. DDL that fails pre-execution after seqno grant), tagged
   and plain explicit-`gtid_next` transactions, all under default faults.
2. At each quiesced checkpoint (shared convention: opportunistic gated attempts + the
   guaranteed `eventually_`/`finally_` full-strength check), read
   `@@global.gtid_executed` on every Synced node with `wsrep_sync_wait=7` set.
3. Violations: (a) any two Synced nodes disagree
   (`GTID_SUBTRACT(a,b) <> '' OR GTID_SUBTRACT(b,a) <> ''`); (b) any node reports GTIDs
   under a sid other than the cluster UUID (errant server-uuid GTID — a leak-family
   regression); (c) a node crashes in the GTID assignment path (PXC-4652 shape — shows up
   as SIGSEGV under write load, caught by the general no-crash/supervisor machinery, but
   the anchor `Sometimes` here tells triage it was the GTID plane).

## How to check (workload-side)

- `Always` (at quiesced checkpoints): pairwise
  `GTID_SUBTRACT(node_i.gtid_executed, node_j.gtid_executed) = ''` in both directions for
  all Synced primary-component nodes.
- `Always` (same checkpoints): `GTID_SUBTRACT(gtid_executed, 'cluster_uuid:1-<max>')`
  contains nothing under any server_uuid — no errant GTIDs. Workload precondition: no
  temporary tables, no RSU, no `sql_log_bin=0`, no `RESET BINARY LOGS` (each is a
  known-legitimate or known-fixed local-GTID generator; the fenced sabotage phases that DO
  use RSU/sql_log_bin must suppress this check for their window — same poison-budget
  scoping as the checksum oracle).
- Vacuity guard `Sometimes`: gtid_executed is non-empty and grew since the last
  checkpoint (the cluster actually minted GTIDs).
- `Sometimes` legs: a TOI begin-failure consumed a seqno (drive DDL that fails after
  seqno grant; observable via error log "TOI begin failed" / the property's top open
  question); a tagged-GTID transaction committed and replicated (PXC-4526 leg); a
  checkpoint ran while `wsrep_applier_threads > 1` load was active (PXC-4652 population).

## Assertion type

- `Always` for both equality and no-errant-GTIDs — safety invariants that must hold at
  every checkpoint evaluation.
- `Sometimes` for the three exploration legs above — they mark the rare semantic states
  (failed-TOI seqno consumption, tagged GTID under load) that make the `Always`
  non-vacuous against the interesting generators.

## Instrumentation notes (missing)

- SUT-side `Sometimes` at the two `wsrep_write_dummy_event` call sites
  (`wsrep_mysqld.cc:2408`, `:2443`) — confirms the no-op hole was actually exercised;
  without it the failed-TOI leg is unobservable except by log-grep for the TOI failure.
- SUT-side `Unreachable` in `wsrep_init_sidno` protocol<4 branch — should never fire in a
  homogeneous 8.4 cluster (cheap misconfiguration tripwire; `Unreachable`, not
  `Reachable`, because the branch firing would itself be the finding). Low priority.
- v1 runs workload-only (SQL + log scan), consistent with the shared log-string layer.

## Fault / config / phase flags

- **Config decision (topology addition)**: `gtid_mode=ON` + `enforce_gtid_consistency=ON`
  + `log_replica_updates=ON` in the harness my.cnf (matches `galera_gtid-master.opt`;
  MySQL 8.4 defaults `gtid_mode=OFF`, which makes this property vacuous — every
  transaction is ANONYMOUS). `log-bin` is default-ON in 8.4. Record per run.
- No faults strictly required (the generators are workload-driven); default faults widen
  (membership churn during TOI failures, kills during group commit). Restart/kill legs
  additionally exercise gtid_executed reconstruction from binlog +
  `mysql.gtid_executed` at recovery — free coverage once the kill channel lands.
- Phase: **v1-assert** (no async source needed — deliberately scoped away from
  `duplicate-gtid-skip-exactly-once`'s async-topology gate).
- Interplay: `applier_threads=4` (already in topology) is required for the PXC-4652 leg.

## Open questions

- Does a failed TOI/NBO begin (dummy-event no-op) diverge `gtid_executed` across cluster
  nodes, or only create a binlog-vs-GTID discontinuity visible to async replicas? If the
  failed writeset is dummied on ALL nodes (nobody binlogs, nobody mints), intra-cluster
  equality survives and the hole is only externally visible — the `Always` here stays
  green and the async-replica variant (deferred topology) inherits the check. If remote
  nodes mint while the origin skips, this property catches it directly. Either answer
  should be pinned at first triage by pairing the failed-TOI `Sometimes` with the equality
  check.
- Is the set of legitimate server-uuid GTID generators fully enumerated (temp-table DROP
  groups, RSU, sql_log_bin=0)? `(partial: the three named are code-confirmed at
  sql_table.cc:3726/:3848 and via the PXC-4313/4034 fix history; a statically complete
  enumeration of wsrep_skip_wsrep_GTID setters and non-replicated-but-binlogged statements
  was not attempted — the errant-GTID Always doubles as the discovery instrument: any red
  is either a leak bug or a missing carve-out, both worth knowing)`
- Does the joiner's `gtid_executed` after a full SST (xtrabackup) exactly match the
  donor's at the transfer point (mysql.gtid_executed + binlog copy semantics), or can a
  post-SST checkpoint show a transient mismatch while the joiner drains? If transient
  mismatch is legal, the checker must gate on Synced + sync_wait (it already does) — but a
  persistent post-SST mismatch is a finding (cf. the empty `wsrep_verify_SE_checkpoint()`
  stub, `wsrep_mysqld.cc:789-792`, sut-analysis §4.4: the post-SST position cross-check is
  a no-op, so nothing in the SUT would notice).

### Investigation Log

#### Is the set of legitimate server-uuid GTID generators fully enumerated?

- Examined: `sql/sql_table.cc:3726` and `:3848` (`thd->wsrep_skip_wsrep_GTID = true` for
  DROP TEMPORARY TABLE binlog groups; reset at `:3961`), per-command clear at
  `sql/wsrep_client_service.cc:116`; the leak-family fix history (PXC-4313 RSU,
  PXC-4034 `sql_log_bin=0` — both in-tree fix commits) as evidence those two generators
  are now *fixed* rather than legitimate.
- Found: three generator classes code-confirmed — temp-table DROP groups (legitimate by
  design under `gtid_mode=ON`), RSU and `sql_log_bin=0` (formerly leaking, now fixed,
  still fenced-phase hazards).
- Not attempted: a statically complete enumeration of all `wsrep_skip_wsrep_GTID`
  setters and of non-replicated-but-binlogged statement classes across the tree — the
  space is large and the errant-GTID `Always` doubles as the discovery instrument (any
  red is either a leak bug or a missing carve-out; both are worth knowing).
- Conclusion: tagged `(partial: the three named are code-confirmed; a statically
  complete enumeration was not attempted)`.
