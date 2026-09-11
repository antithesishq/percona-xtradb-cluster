# cross-node-row-equality — Replicated table content is identical on all primary-component nodes

**Type:** Safety | **Assertion:** Always | **Confidence:** High

## What led to this property

PXC's entire correctness story rests on identical total order + deterministic certification
+ deterministic apply. The SUT has **no mechanism whatsoever to detect
successful-but-divergent apply**: inconsistency voting fires only on apply *errors*
(non-empty `ApplyException` → `handle_apply_error` → vote; empty-error exception →
unilateral `on_inconsistency()` self-eviction — both confirmed at
`percona-xtradb-cluster-galera/galera/src/replicator_smm.cpp:623-637`). There is no state
hash, no row checksum, no cross-node comparison anywhere in the SUT (sut-analysis.md §6.3,
focus 11 §2.4; open question 35). The existing MTR corpus's oracle is golden-file diffs and
opt-in `galera_diff.inc`; `galera_end.inc` runs no consistency check (§10.5). A continuous
workload-side cross-node checksum is therefore the single highest-value oracle for this SUT
— it is the *only* detector for the dominant recurring bug class.

## Mechanism / code involved

- Certification sees only keys, never data: payload is raw row-binlog events, opaque to the
  provider (`sql/wsrep_binlog.cc:243`; sut-analysis §3.2). Any key-extraction gap =
  undetectable divergence.
- Key extraction: `ha_innobase::wsrep_append_keys` `storage/innobase/handler/ha_innodb.cc:13116`
  (insert :10630, update :11474, delete :11565); FK parent keys via
  `wsrep_append_foreign_key` (`storage/innobase/row/row0ins.cc:71`).
- PK-less tables: with `wsrep_certify_nonPK=ON` (default, `sql/sys_vars.cc:8439-8441`),
  certification key = MD5 digest of the full row (`ha_innodb.cc:13255-13273`,
  `wsrep_calc_row_hash`). With it OFF, PK-less DML is refused (`wsrep_check_pk`,
  `sql/wsrep_trans_observer.h:95-107`). Docs list PK-less tables as an explicit
  guarantee-void lever (doc limitation.rst:57-60).
- InnoDB deliberately ignores record-lock conflicts between two high-priority appliers
  ("supposed to be false positives" → NO_CONFLICT, `storage/innobase/lock/lock0lock.cc:581-599`)
  — divergence prevention is delegated entirely to certification's `depends_seqno`. Any
  missed dependency + `wsrep_applier_threads>1` = appliers race on the same rows with no
  safety net.
- Node-local hidden cert params with no cross-node agreement: `cert.max_length` /
  `cert.length_check` (`galera/src/certification.cpp:37-52`, do_test failure :432-445);
  `wsrep_certification_rules` STRICT/OPTIMIZED not negotiated; no validation of cross-node
  `lower_case_table_names`/collations (cert keys are raw name bytes,
  `sql/wsrep_mysqld.cc:1775-1776`).
- Missing-streaming-applier contexts degrade to WARNING + dummy writeset
  (`wsrep-lib/src/server_state.cpp:392-398, 433-440`) — a silent-divergence path under
  membership churn.

## Failure scenario

Two writesets that logically conflict are certified as non-conflicting (missed cert key,
mismatched node-local cert param, case-folding mismatch, dummy-writeset degradation).
Both apply "successfully" on all nodes but in different effective orders on different nodes
(parallel appliers), or one node applies a fragment/writeset another skipped. All nodes
report Synced/Primary; clients read different committed data depending on which node they
hit; nothing in the SUT ever notices.

## Invariant / how to check

Workload-side `Always`: at checkpoints, quiesce the write workload, wait until
`wsrep_last_committed` is equal on all reachable primary-component nodes, then issue a
per-node `wsrep_sync_wait` read (verified sufficient as the per-node barrier: the apply
monitor is deliberately the *stronger* barrier — it is released only after the whole
commit is over on both applier and local paths, `replicator_smm.cpp:1718-1722` comment,
release sites `:657`/`:1586` — the commit monitor can be released early under group
commit, which is exactly why sync_wait does NOT use it), then compare per-table logical
checksums
(e.g. ordered `CHECKSUM TABLE` or SELECT-hash over all replicated InnoDB tables) across
nodes. Assert all equal. `Always` because equality must hold at every quiesced comparison;
a single mismatch is a consistency bug regardless of frequency.

Companion coverage assertions (`Sometimes`): the workload actually exercised (a) DDL/FK
statements concurrent with DML, (b) PK-less table DML, (c) `wsrep_applier_threads>1` apply
concurrency, (d) membership churn during SR — otherwise the Always is vacuous for the
interesting paths.

## Timing / config dependencies

- `wsrep_applier_threads > 1` is essentially required to expose the class (field default 1
  hides it; sut-analysis §12).
- Variant with `cert.optimistic_pa=yes` widens apply parallelism.
- Workload must include: FK parent/child DML + DDL (RENAME, DROP with
  `foreign_key_checks=0`, TRUNCATE on FK parents, multi-table DROP — the PXC-4512/4789
  family), PK-less tables (`wsrep_certify_nonPK` default ON), and duplicate-row PK-less
  content.
- Comparison must only include nodes in the primary component that report Synced.

## SUT-side instrumentation suggestions (all missing)

- **missing**: `Unreachable` in `galera::ReplicatorSMM::on_inconsistency()` and in
  `process_vote`'s "inconsistent with group. Leaving cluster." branch
  (`replicator_smm.cpp:2411-2413`) — any reached vote-based eviction is itself a
  divergence event worth flagging even when the workload checksum hasn't run yet.
- **missing**: `AlwaysOrUnreachable` at the dummy-writeset degradation sites
  (`server_state.cpp:392-398, 433-440`) asserting the trx is genuinely rollback-only.

## Open questions

None — all three resolved; see Investigation Log.

### Investigation Log

#### Window between divergence and (error-based) vote detection: can clients read divergent committed data before eviction?

- Examined: `replicator_smm.cpp:1433-1471` (`process_apply_error`), `:2379-2437`
  (`process_vote`), `gcs/src/gcs.cpp:2628-2680` (`gcs_vote`).
- Found: Yes. On a vote request, success nodes first ensure the writeset is committed
  (`if (last_committed() < seqno_g) drain_monitors(seqno_g)`, `:2398`) *before* casting
  vote 0 — i.e., the divergently-committed data is client-visible on success nodes for at
  least one full vote round (group consensus wait inside `gcs_vote`, which blocks while
  `vote_wait_` is pending). For successful-but-divergent applies there is no vote at all,
  so the window is unbounded.
- Conclusion: resolved. Divergent committed data IS readable before eviction (≥1 vote
  round for error divergence; forever for silent divergence). Quiesced-checkpoint
  checksums remain the right primary oracle; per-transaction read-your-writes recording is
  a nice-to-have amplifier, not a correctness requirement of this property.

#### Does anything besides `process_apply_error` cast votes?

- Examined: tree-wide grep for `gcs_vote` / `gcs_.vote` callers
  (galera/src, gcs/src, excluding tests); callers of `process_apply_error` and
  `handle_apply_error` in `replicator_smm.cpp`.
- Found: exactly two vote call sites. Nonzero votes only at `replicator_smm.cpp:1443`
  inside `process_apply_error`, reached only via `handle_apply_error` (two callers:
  writeset apply failure `:1533`, TOI action failure `:1945`). Vote 0 is cast only in
  `process_vote` (`:2401`) in response to a vote request. Implicit success votes come from
  `last_applied` reports in `gcs_group.cpp:972-988`. Empty-error apply exceptions bypass
  voting entirely (unilateral `on_inconsistency()`, `:626-637`, also `:1235` replay
  failure, `:1498`).
- Conclusion: resolved — nothing else casts nonzero votes. Silent (successful-but-
  divergent) apply can never trigger a vote; the "external checksums are mandatory"
  premise is airtight.

#### Is `wsrep_sync_wait` (apply-monitor based) sufficient as the quiesce barrier?

- Examined: `ReplicatorSMM::sync_wait` (`replicator_smm.cpp:1678-1746`),
  `last_committed_id` (`:1749-1759`), apply-monitor release sites (`:657` applier path
  after commit + `set_trx_committed`; `:1586` local path in `release_commit`).
- Found: the in-code comments state the design explicitly: the commit monitor "may be
  released before the commit has finished and the changes ... have become visible", so
  sync_wait "rel[ies] on apply_monitor ... released only after the whole transaction is
  over". Both release sites confirm the apply monitor is held across the full commit.
  The apply monitor is therefore the STRONGER barrier — the original concern was inverted.
- Not found: any path releasing the apply monitor before commit completion for committed
  transactions.
- Conclusion: resolved — per-node `wsrep_sync_wait` (or `last_committed_id`) is a sound
  commit-visibility barrier. Cross-node comparability still requires equal
  `wsrep_last_committed` under a quiesced workload (sync_wait only drains what each node
  has locally seen), which the property already requires. Checker can run sync_wait-only
  per node without false positives.

## Synthesis refinement (2026-09-10)

Two framing changes: (1) quiesced-checkpoint convention — under active faults the Always runs as opportunistic gated attempts; the guaranteed full-strength check lives in eventually_/finally_ with faults paused (ANTITHESIS_STOP_FAULTS for mid-run quiet windows). (2) Un-injected-vote rule — any inconsistency vote NOT attributable to injected sabotage is divergence evidence and fails this property (touching traffic converts divergence into evictions before a checksum sees it); prefer write-once witness tables; checksum evicted nodes before rejoin.
