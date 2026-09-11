# wsrep-xid-checkpoint-monotonic — InnoDB wsrep XID checkpoint seqno never regresses

**Type:** Safety | **Assertion:** Always (SUT-side) | **Confidence:** Medium

## What led to this property

The InnoDB-persisted wsrep XID (TRX_SYS page, offset `UNIV_PAGE_SIZE-3500`, magic
0x77737265, `storage/innobase/include/trx0sys.h:340-349`) is the **sole** recovery
position for a healthy node — a running node keeps grastate.dat seqno=-1 at all times
(post-transfer blanking, `galera/src/replicator_str.cpp:1423-1438, 1546-1560`) and
`--wsrep-recover` reads only the SE checkpoint (`sql/wsrep_mysqld.cc:1349-1364`). The
monotonicity check that should guard it exists but is **commented out**: verified at
`storage/innobase/trx/trx0sys.cc:404-491` — `wsrep_xid_sanity_check` (UNIV_DEBUG only) has
its `ut_ad((current_thd && wsrep_thd_is_in_nbo(current_thd)) || xid_seqno >=
trx_sys_cur_xid_seqno)` disabled, with an in-code essay enumerating four "legit" violation
cases, including the frank admission for NBO: "Reserved for NBO end, but never used. Yes,
this is a bug. TODO." and, for non-group-commit paths (SR partial commits,
`log_replica_updates=OFF` appliers): "There is nothing that enforces T1 to write the
checkpoint before T2."

A regressed checkpoint is a realized bug class: PXC-4845 (SE checkpoint lagging grastate →
joiner IST from wrong position → monitors uninitialized → cluster-wide FC lockup; commit
text: "There is no good solution for storing wsrep checkpoints in SE"), PXC-5286 (XID
on-disk format endianness bug, V2 format in 8.4.10), historic PXC-4168 assert (confirmed
real — in-tree fix commit `4b900e60266`/`b534612218a`, ancestor of f9ecb3e).

## Mechanism / code involved

- Writer: `trx_sys_update_wsrep_checkpoint` (`trx0sys.cc:529`), redo-logged mtr, called
  from `innobase_wsrep_set_checkpoint` (`ha_innodb.cc:24521`); sanity check compiled to
  nothing in release, and even in debug the ordering assert is commented out.
- `should_store_recovery_xid` (`trx0sys.cc:496-527`) is max-keeping **only during
  recovery**; normal operation stores whatever seqno the caller presents.
- Silent no-op writers: returns 0 in `srv_read_only_mode` (`ha_innodb.cc:24523`);
  undefined GTID read when `!srv_sys_tablespaces_open` (`sql/wsrep_xid.cc:133-144`).
- Ordering normally comes for free from binlog group commit under LOCK_commit — but
  `wsrep_ordered_commit` is silently disabled unless `opt_log_replica_updates &&
  opt_binlog_order_commits` (`sql/wsrep_trans_observer.h:356-373`), and SR partial commits
  bypass group commit entirely.
- PXC-4498 removed `wsrep_group_commit_queue` (−364 lines in 8.4.4, "still is the source
  of deadlocks") — what XID-ordering invariant remains for sys_header writes is an open
  question in the SUT analysis (Q26).

## Failure scenario

Under `wsrep_applier_threads>1` + SR (or `log_replica_updates=OFF`), two commits persist
XID seqnos out of order; a crash lands after the lower-seqno write. Recovery reports a
position lower than transactions already durable in InnoDB. The rejoining node requests
IST from too far back (re-applies writesets → duplicate-key apply errors → inconsistency
vote/eviction) or, PXC-4845-style, triggers gap detection → skips IST with monitors
uninitialized → cluster-wide flow-control stall. Either outcome converts a benign crash
into eviction or cluster outage.

## Invariant / how to check

SUT-side `Always` in `trx_sys_update_wsrep_checkpoint` (or
`innobase_wsrep_set_checkpoint`): within an unchanged cluster UUID, the newly persisted
seqno satisfies `carved_out(writer) || new_seqno >= current_seqno` — the predicate of the
disabled debug assert, evaluated on every checkpoint write in release builds via the
Antithesis SDK (assertions don't crash the process, so re-enabling the check this way is
safe even where the known "legit" cases fire — each firing is triageable). **Carve-out
(updated 2026-09-10 after investigation):** not just `in_nbo(current_thd)` — any write
arriving via the explicit `wsrep_set_SE_checkpoint` path (TOI end, NBO phase-one end,
view/SST handlers) runs outside `LOCK_commit` and can legitimately interleave out of order
with group-commit writes. Scope the strict `Always` to group-commit-path writes
(`trx0trx.cc:1774` with `binlog_order_commits=ON` + `log_replica_updates=ON`), and pair it
with a `Sometimes` "carved-out regression observed (NBO/TOI/explicit path)" so the real
NBO durability hole (confirmed to reach disk — see Investigation Log) stays visible.
`Always` because the predicate must hold on every evaluation of the strict class; the
optional-path variants are covered by the carve-out, not by weakening to
AlwaysOrUnreachable.

Workload-observable proxy (weaker): after every node restart, `--wsrep-recover` position
>= the highest seqno of any transaction the workload had confirmed committed on that node
before the kill. Usable without SUT instrumentation but only samples crash points, not
every write.

**FAULT NOTE:** the workload proxy requires node kill/restart (often disabled by
default — flag as fault requirement). The SUT-side assertion itself needs no restart
faults to detect ordering violations, which is a strong reason to prefer it.

## Timing / config dependencies

- Needs `wsrep_applier_threads > 1` and/or SR (`wsrep_trx_fragment_size > 0`) and/or
  `log_replica_updates=OFF` to open the non-group-commit ordering hole.
- NBO (`wsrep_OSU_method=NBO`) exercises the documented known-decrease case; run one
  variant with NBO to calibrate the carve-out.
- The known equal-value re-persists (atomic DDL double-persist, TOI INSERT..SELECT) are
  allowed by `>=`.

## SUT-side instrumentation suggestions (all missing)

- **missing**: the `Always` described above at `trx0sys.cc` checkpoint write — the primary
  form of this property; strictly better than the workload proxy because it observes every
  write, not just crash samples.
- **missing**: `Sometimes` that a checkpoint write occurred from a non-group-commit path
  (SR partial commit / applier with log_replica_updates=OFF) — coverage guard for the
  interesting writers.

## Open questions

None. (All three resolved 2026-09-10 — see Investigation Log. Net effect on the property:
the `Always` carve-out must be **wider than NBO alone**: any write from the explicit
`wsrep_set_SE_checkpoint` path — TOI end, NBO phase-one end, view/SST handlers — runs
outside `LOCK_commit` and can interleave with group-commit writes, so seqno regressions
are constructible whenever TOI/NBO/view events overlap commits. The predicate should be
"new_seqno >= current_seqno, carve-out: writer is any non-group-commit explicit-checkpoint
caller or an NBO thd" — with each carved-out firing still recorded via a companion
`Sometimes`, because the NBO regression in particular is a REAL durability defect that
reaches disk (see log). The pure group-commit class remains strict: any firing there is a
real bug.)

### Investigation Log

#### Post-PXC-4498, what enforces TRX_SYS write ordering for group-commit-path transactions?

Investigated 2026-09-10.

- Examined: PXC-4498 removal commit `c65c69d52ec` (+ 8.4 merges `00055a6d756`/
  `b5c52da11e0`); `storage/innobase/trx/trx0sys.cc:466-478`, `trx0trx.cc:1762-1789`,
  `storage/innobase/include/trx0sys.ic:77-78`, `sql/binlog.cc:8951, 9201-9207, 9412,
  9766-9767, 12750-12758`, `sql/rpl_commit_stage_manager.cc:80-124, 317-369`,
  `sql/wsrep_trans_observer.h:304, 356-372`, `sql/wsrep_xid.cc:107-148` callers.
- Found: the removal commit itself states the old queue "is not enforcing proper order of
  xid storing in sys_header ... removing". Ordering on the group-commit path is emergent:
  Galera's CommitOrder monitor is released inside the flush-queue append *under the queue
  lock* (`rpl_commit_stage_manager.cc:122-124`, enqueue+release atomic per :317-369), so
  flush-queue order == seqno order, and the commit-stage leader runs every member's
  `ha_commit_low` → `trx_sys_update_wsrep_checkpoint` (`trx0trx.cc:1772-1774`) sequentially
  under `LOCK_commit` (`binlog.cc:9202`). Gated by `opt_log_replica_updates &&
  opt_binlog_order_commits` (`wsrep_trans_observer.h:364-366`) and by the commit stage
  being entered at all (`binlog.cc:9766-9767`).
- Found (the hole is wider than documented): ALL `wsrep_set_SE_checkpoint` callers — TOI
  end (`sql/wsrep_mysqld.cc:2638`), NBO phase-one end (`:2814`), view/SST handlers
  (`sql/wsrep_server_service.cc:161,250,294,346`, `sql/wsrep_sst.cc:307-308`,
  `sql/wsrep_high_priority_service.cc:504,793`) — go through
  `innobase_wsrep_set_checkpoint` (`ha_innodb.cc:24521-24535`) with their own mtr,
  **outside LOCK_commit**, fully concurrent with a group-commit batch. Only mutual
  exclusion (TRX_SYS page X-latch, `trx0sys.ic:77-78`) exists — arbitrary order.
- Not found: any wsrep-specific ordering structure in 8.4.10; any release-build
  monotonicity check (sole assert commented out at `trx0sys.cc:481-482`).
- Conclusion: RESOLVED — with `binlog_order_commits=ON` + `log_replica_updates=ON`, the
  pure group-commit class is serialized in commit order (any Always firing there is a real
  bug). But the carve-out must widen beyond NBO: TOI-end/view/SST explicit-checkpoint
  writes can interleave with group-commit writes in either direction. With
  `binlog_order_commits=OFF` the commit stage is skipped and even the group-commit class
  loses ordering (config variant worth one run).

#### Can the NBO decrease actually reach disk?

Investigated 2026-09-10.

- Examined: `trx0sys.cc:404-560` (esp. :529-545 gate, :496-528 `should_store_recovery_xid`,
  :547-568 mlog writes), `storage/innobase/include/trx0sys.h:227-231`,
  `sql/wsrep_mysqld.cc:2687-2925, 3185-3210`, `sql/sql_parse.cc:6153-6175`,
  `sql/sql_admin.cc:2020-2065`, `trx0trx.cc:1755-1795`, `ha_innodb.cc:24521-24535`.
- Found: YES, two live sub-paths. (A) NBO phase-one end calls
  `wsrep_set_SE_checkpoint(client_state.nbo_meta().gtid())` (`wsrep_mysqld.cc:2807-2815`)
  with the phase-ONE seqno X, after other commits may have advanced the checkpoint to
  X+1+N; `innobase_wsrep_set_checkpoint` then does `innobase_flush_logs` immediately —
  the regressed value is durable at once. (B) The DDL's own InnoDB commit in phase two
  (`trans_commit_implicit` at `sql_parse.cc:6172`) carries the stale XID (seqno X)
  installed at `:2810-2811` and not reset until `wsrep_NBO_end_phase_two`
  (`:2903-2906`). The max-keeping gate `should_store_recovery_xid` applies ONLY when
  `recovery == true` (`trx0sys.cc:538`, default false per `trx0sys.h:231`; the sole
  `recovery=true` call site is `trx0trx.cc:1789`). Normal-operation writes overwrite
  unconditionally via redo-logged mlog. `wsrep_NBO_end_phase_two` contains NO
  `wsrep_set_SE_checkpoint` — the final NBO seqno is never persisted ("X+1+N+1 ...
  never persisted in SE" per the in-code comment, confirmed).
- Conclusion: RESOLVED — a regressed XID reaches disk in normal operation; NBO end is a
  live, deterministic trigger (regression of N+1 after N overlapping commits). The NBO
  carve-out therefore hides a real durability hole: a post-NBO crash recovers to X and the
  node rejoins claiming a long-passed position (stale IST / false SST decision). Worth a
  dedicated regression scenario (NBO variant + kill after DDL) and a `Sometimes`
  "checkpoint regressed under NBO carve-out" so firings are visible, not swallowed.

#### Does the PXC-5286 V2 XID format change the frequency/shape of grastate/SE divergence?

Investigated 2026-09-10.

- Examined: `git log --grep=PXC-5286` → format commits `a61fda31239` (V2 write),
  `ad3ccafc2a5`, merge `247589d70d1`; `sql/wsrep_xid.h:23-53`, `sql/wsrep_xid.cc:40-99,
  179-212`, `trx0sys.cc:396-398, 570-607`, `unittest/gunit/wsrep_xid-t.cc:62,82`.
- Found: V2 is purely a decode-correctness/interop fix — the write encoding (int8store,
  little-endian) is unchanged; only the declared version byte now matches it, and the read
  path dispatches per version (`wsrep_xid.cc:91-99`). No change to when/how often/under
  what locking checkpoints are written. Compat notes: legacy V1-on-big-endian decode stays
  bug-for-bug wrong (memcpy branch preserved); no rewrite-on-read of V1 records; DOWNGRADE
  hazard — a pre-fix binary's 8-byte `"WSREPXid"` memcmp rejects a V2 XID ('e' at byte 7)
  → `wsrep_get_SE_checkpoint` returns empty gtid → undefined position → forced full SST
  after any rollback to an earlier 8.4.x.
- Conclusion: RESOLVED — zero effect on divergence frequency/shape on little-endian
  production platforms; the workload proxy's legitimate-SST-fallback expectations need no
  adjustment. (Downgrade/mixed-version runs are out of the current single-version harness
  scope; note kept here for future upgrade-testing work.)
