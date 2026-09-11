---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# grastate-se-checkpoint-agreement — At every mysqld start, grastate.dat agrees with the InnoDB SE checkpoint

**Merged from two independent discoveries (focus 1: data integrity; focus 3: failure
recovery) — both agents converged on the same property from different starting points, a
strong confidence signal.**

**Type:** Safety | **Assertion:** `Always` — the invariant must hold at every single node
start; there is no acceptable execution where a node boots from a position that neither
durable store can vouch for. | **Confidence:** High (all load-bearing code paths read
directly at f9ecb3e by both agents)

**FAULT REQUIREMENT: node termination (kill/restart) — the disagreement is created by
crashing between the two stores' writes; graceful shutdown writes a consistent pair. This
property is untestable with network faults alone. Disk write-fault injection on the datadir
strengthens it (exercises the warnings-only write path) but is not required.**

## Property

At every mysqld startup on a previously-used datadir, the recovery position is coherent:
`grastate.dat` seqno is either `-1` (undefined → defer to the InnoDB wsrep XID via
`--wsrep_recover`) or equal to the SE-checkpoint seqno recovered from the InnoDB TRX_SYS
page, with matching UUIDs. Equivalently: a node never begins replication from a seqno that
neither durable store can vouch for.

## Why this is the weak point (code evidence, verified)

The two recovery sources are written by different subsystems with different durability, and
the arbitration between them is trivially foolable:

1. **grastate.dat is rewritten in place, non-atomically, and write failures are
   warnings-only.** `SavedState::write_file`
   (`percona-xtradb-cluster-galera/galera/src/saved_state.cpp:364-405`): `rewind(fs_)` +
   `fwrite` of a ≤256B buffer over the old content — no temp file, no rename. `fwrite`/
   `fflush`/`fsync` failures each `log_warn` and `return` (function returns void; no caller
   can observe the failure). A torn write or failed write leaves an arbitrary stale or
   half-old file that all downstream tooling trusts. The PXC-only early-out in
   `SavedState::set` (`:325-339`) skips the write when in-memory values are unchanged — a
   previously *failed* write is never retried.

2. **The SE checkpoint is trusted only under a narrow condition.**
   `ReplicatorSMM::ReplicatorSMM`
   (`percona-xtradb-cluster-galera/galera/src/replicator_smm.cpp:266-286`): the
   `--wsrep_start_position` recovered from InnoDB is used only if
   `state_id->uuid == grastate.uuid && grastate.seqno == WSREP_SEQNO_UNDEFINED`. Any stale
   non-(-1) grastate seqno silently *wins over* the SE checkpoint.

3. **The wrapper scripts institutionalize that trust.** `scripts/mysqld_safe.sh:273-279`:
   if grastate seqno != -1, `--wsrep-recover` is skipped entirely and the grastate pair is
   passed verbatim as `--wsrep_start_position` ("Skipping wsrep-recover for $uuid:$seqno
   pair"). `build-ps/rpm/mysql-systemd` and `build-ps/debian/extra/mysql-systemd` behave the
   same (grastate seqno != -1 trusted verbatim).

4. **A healthy running node keeps grastate seqno = -1 and relies wholly on the InnoDB XID**
   (`replicator_str.cpp:1423-1438` blanks the position before IST; `:1546-1560` resets to
   -1 after success; graceful close writes the final seqno via `shift_to_CLOSED` →
   `st_.set(state_uuid_, last_committed(), ...)`, `replicator_smm.cpp:315-317`).
   So the "grastate has a real seqno" state exists transiently around shutdown and around
   state transfer — exactly the windows a crash hits. The `shift_to_CLOSED` write can race
   a crashing shutdown, leaving a stale non-(-1) seqno.

5. **The XID monotonicity assert is commented out**
   (`storage/innobase/trx/trx0sys.cc:406-493`, assert at `:481-482`), with an in-code
   comment enumerating four known legitimate violations (atomic DDL double-persist, TOI
   INSERT...SELECT, NBO "Yes, this is a bug. TODO.", non-group-commit paths). So the SE
   side can itself regress in known cases (see companion property
   `wsrep-xid-checkpoint-monotonic`).

6. **NBO/TOI unsafe_ balance**: `apply_trx` marks unsafe for nbo_start
   (`replicator_smm.cpp:599-603`) and is_toi (`:616-619`), mark_safe for is_toi at
   `:660-664`; NBO's balancing decrement is in `to_isolation_end` (`:1971`). A leaked
   unsafe_ count (crash mid-NBO) = grastate permanently -1 = forced SST every restart —
   a liveness/cost cousin observable with the same probe.

## Realized bug this generalizes (validated as a real defect)

**PXC-4845** (fix ff120ce5759 + galera a8bda1ba, 2026-02-09): grastate seqno *ahead of* the
SE checkpoint → on rejoin the node ISTs from the lower SE seqno, detects the gap, skips IST
but keeps running with apply/commit monitors uninitialized (`last_left == -1`) → queued
writesets block forever → **cluster-wide flow-control stall**. The fix's own commit text:
"There is no good solution for storing wsrep checkpoints in SE" — i.e., the fix converts the
hang into a shutdown; the divergence itself is still reachable. This is a regression target,
not a closed class.

## Failure scenario (Antithesis recipe)

1. Run commit-heavy workload on a 3-node cluster.
2. Kill -9 a node at random points — especially during shutdown (the `shift_to_CLOSED`
   grastate write at `replicator_smm.cpp:315` races the InnoDB checkpoint), during NBO/TOI
   (unbalanced `mark_unsafe`/`mark_safe`), during SST/IST post-transfer blanking
   (`replicator_str.cpp:1423-1438` in-code comment: "if node gets killed during IST, it may
   recover to incorrect position"), and **while the provider is paused** (FTWRL / desync /
   donor: `pause()` stamps a real `last_committed()` seqno into grastate mid-run,
   `replicator_smm.cpp:3385-3415`; only `resume()` re-blanks it — a kill inside the pause
   window yields grastate-ahead-of-SE with no shutdown involved; found 2026-09-10).
3. Optionally inject disk write faults while grastate is being rewritten (warnings-only
   error handling means the process continues believing the write happened).
4. On each restart, before mysqld starts serving: compare `grastate.dat` against
   `mysqld --wsrep_recover` output. **The check must run in the startup wrapper before
   mysqld joins — after join, grastate is re-blanked and the evidence is gone.**

Violation consequences: node silently starts at the wrong position → requests IST from a
position it doesn't actually have (data loss on that node → divergence), or a position
ahead of its real state (PXC-4845 cluster-wide stall), or skips a needed SST entirely.

## Invariant (concrete check)

Workload/sidecar-side `Always`, evaluated at every node (re)start on a non-empty datadir,
before joining:

- Parse `grastate.dat` (uuid, seqno).
- Run `mysqld --wsrep_recover` (or capture the recovery step the harness already performs)
  → `[WSREP] Recovered position: <uuid>:<seqno>`.
- `assert_always(grastate.seqno == -1 || (grastate.uuid == se.uuid && grastate.seqno ==
  se.seqno), "startup recovery position: grastate agrees with SE checkpoint")`.

Companion check (cheap, same probe): grastate.dat parses cleanly — has all four fields
(`version/uuid/seqno/safe_to_bootstrap`, parser at `saved_state.cpp:95-135`) — a torn
in-place rewrite shows up here first; `mysql-systemd` trusts a non-(-1) seqno verbatim.

Companion `Sometimes`: at least one start observed with grastate seqno != -1 (graceful
stop) and one with -1 (crash path) — both recovery branches exercised.

## Timing / config dependencies

- Variant dependency: whether the harness reproduces the mysqld_safe/systemd
  `--wsrep-recover` dance or starts mysqld directly changes which branch of the trust
  logic runs; test both if feasible.

## Instrumentation suggestions (all missing — no SDK instrumentation exists in this SUT)

- **SUT-side `Always`** at `replicator_smm.cpp:266-286`: when both grastate seqno >= 0 and
  a non-trivial `--wsrep_start_position` was supplied, assert they agree. This is the exact
  arbitration point (the PXC-4845 precondition); today disagreement is silently resolved in
  grastate's favor.
- **SUT-side `Always`** in `SavedState::write_file` (`saved_state.cpp:388-405`): assert the
  write/flush/fsync succeeded — converts the warnings-only failure into a visible property
  violation ("grastate write failed" is currently invisible to any oracle).
- **SUT-side `Always`** (balance check): `unsafe_() == 0` whenever the node is Synced and
  idle — catches the NBO unsafe_ leak.
- **SUT-side `Sometimes`** ("node started with grastate seqno >= 0") — confirms the
  interesting arbitration branch is actually explored.

## Open Questions

None. (All four resolved 2026-09-10 — see Investigation Log. Net effect on the property:
grastate-ahead-of-SE remains fully constructible (the PXC-4845 fix changed only the
reaction, never the write ordering) and the post-fix behavior is a hard `abort()` — so
the residual failure mode for this property's "ahead" branch is a joiner crash-loop /
forced SST, not a cluster-wide stall. Two NEW kill windows discovered: (1) a crash while
the provider is **paused** (`pause()` stamps a real `last_committed()` seqno into
grastate mid-run — FTWRL, desync, donor windows — `replicator_smm.cpp:3385-3415`, reset
to -1 only in `resume()` at `:3522`); (2) the crash-during-shutdown window is wider than
one race — the fsynced grastate write happens in the signal-handler thread at
`mysqld.cc:4438` while InnoDB's final redo flush happens much later in the main thread
(`clean_up()` → `ha_pre_dd_shutdown`/`plugin_shutdown`), and even per-transaction the
WSREPXID mtr's redo flush is deferred under group commit (`trx->flush_log_later`,
`trx0trx.cc:2189-2208`). The NBO unsafe_-leak concern is bounded (one SST, self-healing),
so the companion "permanent -1" probe can be dropped in favor of a "post-NBO-crash start
took exactly one SST then healed" expectation.)

### Investigation Log

#### Is grastate-ahead-of-SE still constructible post-PXC-4845, and what is the post-fix behavior?

Investigated 2026-09-10.

- Examined: PXC commit `ff120ce5759` (MTR test + submodule bump only) and galera fix
  `a8bda1ba` (merged `c71acc56`); `galera/src/replicator_str.cpp:1678-1718` (post-fix
  catch block), `ist.cpp:557-573, 742-745`, `ist.hpp:88`, `replicator_smm.cpp:3941-3946`
  (`ReplicatorSMM::abort`), `galerautils/src/gu_abort.c`, `replicator_str.cpp:1003-1011,
  1442-1450` (sibling abort paths), `git blame` of the `mark_corrupt` line (PXC-5208,
  `350f76f32`), `replicator_smm.cpp:263-276` (recovery arbitration), `saved_state.cpp`.
- Found: post-fix, `IST started with wrong seqno` → `log_fatal "Receiving IST failed,
  node restart required"` → `st_.mark_corrupt(force_sst_after_inconsistency_)` (added by
  PXC-5208, not PXC-4845) → `ReplicatorSMM::abort()` → `gcs_.close(); gu_abort()` —
  SIGABRT with core dumps suppressed, no mysqld clean shutdown. Pre-fix this path did
  `start_closing()` and lingered with uninitialized monitors (`last_left == -1`). The fix
  is detection-and-die; the commit message explicitly declines to fix the write ordering
  ("There is no good solution for storing wsrep checkpoints in SE"). No startup pre-flight
  comparison of grastate vs SE checkpoint exists — the mismatch is discovered only
  mid-IST. Neither commit touches `shift_to_CLOSED` or `saved_state.cpp`.
- Conclusion: RESOLVED — still a live regression target; the ahead case remains
  constructible and now fail-fast-crashes (mark_corrupt zeroes the uuid so the next start
  takes one full SST and self-heals). The property's Always remains exactly right; the
  "detects → abort → SST" chain is the acceptable outcome, silent wrong-position start is
  the violation.

#### Does crash mid-NBO leak the unsafe_ counter (permanent forced SST)?

Investigated 2026-09-10.

- Examined: full `galera/src/saved_state.cpp` (esp. `mark_unsafe` :258-275, `mark_safe`
  :277-297, `set` :214-228, writer :374-404) and `saved_state.hpp:60`; all `st_.` call
  sites (`replicator_smm.cpp:598-620, 660-664, 1908-1912, 1971, 1856-1861, 2266-2289`,
  `replicator_str.cpp:1317-1321`).
- Found: `unsafe_` is `gu::Atomic<long>`, in-memory only, zero at every process start —
  never serialized (grastate has exactly four keys). `mark_unsafe` (0→1 transition)
  writes uuid=UNDEFINED (all zeros), seqno=-1, safe_to_bootstrap preserved, fsynced.
  Crash mid-NBO → next start sees uuid UNDEFINED so the `--wsrep_start_position` adoption
  test at `replicator_smm.cpp:266-276` FAILS → one full SST → post-SST `st_.set` +
  `mark_safe` restore a real uuid → subsequent restarts normal. If the process survives
  but NBO end never runs, `unsafe_` stays >=1 and every `st_.set` (incl. shift_to_CLOSED)
  is a no-op for the process lifetime — again at most one SST after the next restart.
- Conclusion: RESOLVED — no permanent forced-SST state is reachable via the unsafe_
  counter; the cost is bounded to a single SST and self-heals. The probe expectation
  changes from "grastate always -1 across many clean cycles" (won't happen) to "a
  mid-NBO/TOI kill produces uuid-UNDEFINED grastate and exactly one SST". Note the
  mark_unsafe content (zero uuid) *defeats* SE-checkpoint recovery, unlike the ordinary
  crash case (real uuid + seqno -1) where the SE checkpoint is adopted → IST.

#### Which wrapper grep family works (bracketed vs unbracketed)?

Investigated 2026-09-10 (full packaging trace under
`crash-recovery-grep-yields-true-position` — see that file's Investigation Log).

- Examined: `build-ps/percona-xtradb-cluster.spec`, both shipped unit families,
  `scripts/CMakeLists.txt`, `sql/wsrep_mysqld.cc:1364` + `wsrep_mysqld.h:290-303` emission.
- Found: the emitted line is `[WSREP] Recovered position:` (bracketed). Shipped RPM and
  Debian units both invoke `/usr/bin/mysql-systemd galera-recovery`, which greps the
  bracketed form over a fresh mktemp file — the WORKING family. The unbracketed-grep
  scripts (`mysqld_pre_systemd.in`, `mysql-helpers:get_recover_pos`) are unshipped/dead
  (WITH_SYSTEMD forced OFF; no shipped unit invokes them).
- Conclusion: RESOLVED — the harness should reproduce the `mysql-systemd galera-recovery`
  flow (or mysqld_safe, also bracketed). No in-image verification needed beyond a one-time
  sanity check; the property cannot fire "for harness reasons" if the harness copies the
  shipped script.

#### Is shift_to_CLOSED's grastate write strictly ordered after all engine commits?

Investigated 2026-09-10.

- Examined: `replicator_smm.cpp:306-317` (the write), `replicator_smm.hpp:468-476`
  (`last_committed()` = `apply_monitor_.last_left()`), `rpl_commit_stage_manager.cc:
  122-125` + `binlog.cc:8980-8984` (monitor release points), `trx0trx.cc:1761-1774,
  1873-1900, 2189-2208, 2663-2722` (WSREPXID mtr + deferred flush), `mysqld.cc:4437-4444,
  11235, 3103-3148` (shutdown ordering), `saved_state.cpp:388-404` (grastate fsync),
  `replicator_smm.cpp:3385-3415, 3522` (`pause()`/`resume()`).
- Found: NOT strictly ordered, two independent gaps. (1) Process ordering: grastate is
  fsynced by the signal-handler thread (`wsrep_shutdown_replication` at `mysqld.cc:4438` →
  provider close → shift_to_CLOSED) while InnoDB's shutdown/final redo flush runs later
  in the main thread (`clean_up()` → `ha_pre_dd_shutdown`/`plugin_shutdown`); a kill in
  that window leaves grastate durable and ahead. (2) Per-transaction: `last_committed()`
  correctly reflects apply-monitor leave (after `ha_commit_low` returns — the
  commit_order_leave concern does NOT apply, galera-bugs#555), but engine-commit return
  does not imply the WSREPXID trx-sys mtr reached durable redo: `trx->flush_log_later`
  defers the flush under group commit and flush=0/2 weakens/eliminates it — the InnoDB
  comment at `trx0trx.cc:2686-2692` says exactly this. No barrier/flush call exists
  between provider close and the grastate write. BONUS: `pause()` stamps a REAL seqno
  into grastate mid-run (FTWRL/desync/donor), reset only by `resume()` — a whole new
  crash window producing grastate-ahead with no shutdown involved.
- Not found (and inherently not static): whether an unrelated actor (log-writer thread,
  concurrent fsync) happens to have flushed the redo first — timing/config dependent,
  which is precisely the space Antithesis explores.
- Conclusion: RESOLVED — the violation channel is NOT narrowed to torn writes + NBO
  window; kill-point emphasis stays broad and gains the pause() window (kill during
  FTWRL/backup/donor pause is a first-class scenario for this property).

## Synthesis refinement (2026-09-10)

Moved from the top-10 to the phase-2 tranche (NOT deleted): near-vacuous under graceful-only restarts (both files trivially agree — v1 would validate the harness's own supervisor). The boot-time grastate/--wsrep-recover JSONL probe ships in tranche 1 as a supervisor extension; the property earns its keep once the kill channel lands.
