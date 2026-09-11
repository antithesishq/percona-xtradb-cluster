---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# skip-locked-nowait-never-fatal — SELECT ... FOR UPDATE SKIP LOCKED / NOWAIT under applier concurrency never kills the node

**Provenance: evaluation gap-fill (synthesis Gap 4 — coverage #4; regression target
PXC-5099, previously zero catalog mentions).** Designed as a small property implemented as
a **rider** on the existing BF-conflict workload (`first-committer-wins-loser-leaves-no-
trace` / `bf-replay-commits-exactly-once` already generate exactly the HP-applier lock
pressure this needs).

**Type:** Safety | **Assertion:** workload `Always` (every SKIP LOCKED/NOWAIT statement
terminates with a legal outcome and returns only committed rows; the node survives) +
`Sometimes` (a SKIP LOCKED statement returned ER_LOCK_DEADLOCK — the wsrep-BF-wait-induced
DB_SKIP_LOCKED conversion path fired) + log-detector `Unreachable` (the "Unknown error
code" fatal / row0sel lock-switch assertion) | **Confidence:** High — validated realized
bug with an in-tree fix, MTR repro, and a visibly incomplete conversion surface.

## Claim under test

`SELECT ... FOR UPDATE SKIP LOCKED` and `... NOWAIT` (8.x features widely used by job-queue
workloads) are safe on PXC under concurrent high-priority (applier/replayer) lock traffic:
the statement returns a subset of committed rows, or a legal error (ER_LOCK_DEADLOCK 1213,
ER_LOCK_NOWAIT 3572, ER_LOCK_WAIT_TIMEOUT 1205) — never a node crash, and never
uncommitted data.

## The validated bug and its fix (PXC-5099, fix 0fbe08cfd7b, 2026-03-13)

Root cause confirmed from the fix commit (inherited from Codership mysql-wsrep-8.4.3-26.21):

- The wsrep patch in `rec_lock_check_conflict`
  (`storage/innobase/lock/lock0lock.cc:603-610`): a local transaction's lock request
  **always** conflicts with a lock held by a high-priority (applier) transaction
  (`!nbo && wsrep_on && !is_hp && trx_is_high_priority(lock2->trx)` → `HAS_TO_WAIT`) —
  even for requests native InnoDB considers never-waiting (gap locks, compatible modes).
- Under `SELECT_SKIP_LOCKED`, a conflicting request returns `DB_SKIP_LOCKED`
  (`lock0lock.cc:1992-1996`) at `sel_set_rec_lock` call sites where native InnoDB asserts
  the lock is always granted. Pre-fix outcome: debug assert in `row0sel.cc`, or in release
  the error leaks to `row_mysql_handle_errors`' default branch →
  `ib::fatal ... "Unknown error code 21: Skip locked records"`
  (`storage/innobase/row/row0mysql.cc:1224-1226`) → **node death from a plain SELECT**.
- The fix converts `DB_SKIP_LOCKED` → `DB_DEADLOCK` when `wsrep_on` at **three**
  `sel_set_rec_lock` switch sites (`row0sel.cc:4926-4948`, `:5067-5089`, `:5116-5138`),
  adds `DEBUG_SYNC_C("skip_locked")` at the legitimate skip path (`:5203-5206`), and a
  debug-only carve-out acknowledging the wsrep exception in `lock_reuse_for_next_key_lock`
  (`lock0lock.cc:1890-1906`).
- MTR repro (`mysql-test/suite/galera/t/mwb-1847.test`, also `mwb-1861.test`): BF-abort a
  local `SELECT ... FOR UPDATE` via an SR insert (`wsrep_trx_fragment_size=1`) so an
  HP insert-intention lock is left on the supremum record, then run
  `SELECT ... FOR UPDATE SKIP LOCKED` — it traverses the supremum gap lock and hits the
  "impossible" DB_SKIP_LOCKED.

## Residual regression surface (why this stays a standing property, not a one-shot)

The fix is call-site enumeration, and the commit's own cause statement — "wsrep patch ...
causes **any type of lock request** to potentially wait for high priority appliers" — is a
property of the lock system, not of three switch statements:

- **Unconverted site**: `row0sel.cc:4808-4812` (index-open gap-lock site):
  `case DB_SKIP_LOCKED: case DB_LOCK_NOWAIT: ut_d(ut_error); ut_o(goto next_rec);` — on
  the v1 assert image (`UNIV_DEBUG` is on whenever NDEBUG is off, `univ.i:201-203`) this
  is a crash if reachable under wsrep BF-wait; in release it silently *skips* — for
  NOWAIT that is a semantic violation (NOWAIT should error, not skip).
- **NOWAIT at the three fixed sites**: only `DB_SKIP_LOCKED` was converted;
  `DB_LOCK_NOWAIT` still executes `ut_d(ut_error)` before falling to
  `lock_wait_or_error`. Release builds survive (DB_LOCK_NOWAIT is a handled code,
  `row0mysql.cc:1160`), but **on the v1 assert image a NOWAIT query traversing a
  BF-conflicted gap lock is a candidate crash** — an active bug-hunt arm, not just a
  regression guard.
- `DB_SKIP_LOCKED` consumers elsewhere (`row0sel.cc:1002`, `:1104` spatial-index paths,
  `:5448`) were not audited by the fix.

## Failure scenario

Job-queue consumer does `SELECT * FROM q WHERE ... FOR UPDATE SKIP LOCKED` on node A while
appliers replicate competing DELETEs/INSERTs from node B. An applier's insert-intention or
next-key lock sits on a gap the reader traverses; the wsrep conflict rule fires on a
request shape the fix didn't enumerate → assertion (v1 image) or unknown-error fatal
(release) → node death from read-only-ish traffic; systemd would not restart it (exit
status excluded), so in the field this is a permanent node loss caused by a SELECT.

## Suggested implementation

Rider on the BF-conflict workload (no new topology, no faults required — applier
concurrency IS the mechanism; default faults enrich interleavings):

- Add SKIP LOCKED and NOWAIT consumers to the contended tables the BF workload already
  hammers, including the MTR shape (rows locked FOR UPDATE, then BF-aborted, leaving HP
  supremum locks) and plain hot-row contention. Include secondary-index scans and
  descending/range scans to diversify the `sel_set_rec_lock` call sites reached.
- **Workload `Always`** (message: "skip-locked/nowait statement outcome legal"): each
  such statement ends in {result set, 1213, 3572, 1205}; any other error code, lost
  connection, or node death attributable to the statement fails the property. Returned
  rows are checked against the witness protocol (rows carry writer-journal values; a
  returned value must match an acked committed write) — the "only committed rows" half.
- **`Sometimes`** (message: "wsrep BF-wait converted SKIP LOCKED to deadlock"): a SKIP
  LOCKED statement returned 1213. Native InnoDB SKIP LOCKED cannot deadlock this way, so
  1213-from-SKIP-LOCKED is a precise workload-side marker that the conversion path
  (`row0sel.cc:4937/5078/5127`) was exercised — the exploration hint the gap-fill brief
  asks for, with zero SUT instrumentation.
- **`Sometimes`** (message: "nowait under BF contention returned 3572/1205") — proves the
  NOWAIT arm generated real contention.
- **Log-detector `Unreachable`** (shared log-scan layer): "Unknown error code" fatal and
  the `row0sel.cc` assertion signature — the pre-fix crash resurfacing (also caught by
  the generic death detectors, but the dedicated message pins attribution to this
  property).

## Fault / config / phase flags

- Phase tag: **v1-assert, rider** (folded into the BF-conflict workload). No kill
  channel, no config variant; `wsrep_trx_fragment_size=1` per session reproduces the MTR
  shape (SR is un-gated — session-settable).
- The NOWAIT-on-assert-image arm is image-specific: on the release image the same path is
  a silent NOWAIT-semantics violation instead of a crash. Keep the outcome-legality
  `Always` on both images — it catches the **wrong-error** shape (an error code outside
  the legal set). The **silent-skip** shape is NOT caught by it: a silently-skipping
  NOWAIT returns a legal-looking (smaller) result set, indistinguishable from
  no-contention to the outcome check. Closing that half needs either (a) a targeted
  workload probe — NOWAIT against a row the workload's own ledger knows is currently
  locked MUST return 3572/1205, never a result set (implementable in the rider, needs
  the witness/ledger the BF workload already keeps), or (b) the SUT-side probe below.
- **SUT-side (missing, v2)**: an SDK `Unreachable` (or `AlwaysOrUnreachable` guard) at
  the release silent-skip branch (`row0sel.cc:4808-4812`) — the state is externally
  invisible by construction, so this is exactly the "rare, dangerous, hard to observe
  externally" case where SUT-side instrumentation beats workload-only checks.
- Coordination: `first-committer-wins-loser-leaves-no-trace` owns commit-outcome
  semantics of the BF conflicts themselves; this property owns the locking-read client
  contract and the InnoDB×wsrep lock-layer crash surface (bug pattern I).

## Open Questions

- Is `row0sel.cc:4808` (index-open site) reachable with `SELECT_SKIP_LOCKED`/`SELECT_NOWAIT`
  under the wsrep conflict rule? If yes, the v1 assert image should crash there under this
  rider — pre-registering the expectation turns a mystery crash into a one-line triage.
- Do spatial-index `DB_SKIP_LOCKED` paths (`row0sel.cc:1002/:1104`) interact with wsrep
  BF-waits at all (spatial predicate locks vs the HP conflict rule)? Determines whether a
  GIS leg is worth adding to the rider.
