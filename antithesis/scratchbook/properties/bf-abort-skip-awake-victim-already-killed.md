# bf-abort-skip-awake-victim-already-killed

**Type:** Safety (a lost-wakeup detector; the user-visible symptom is a liveness stall).
**Focus:** concurrency — `THD::wsrep_aborter` is guarded by two different mutexes.

## What led to this property

The one-aborter deduplication around BF aborts uses the field `victim_thd->wsrep_aborter`,
but the two places that read/write it hold **different mutexes**. Verified in this tree:

- `storage/innobase/handler/ha_innodb.cc:24385-24470` (`wsrep_innobase_kill_one_trx`): calls
  `wsrep_thd_set_wsrep_aborter(bf_thd, thd)` while holding the victim's **`LOCK_wsrep_thd`**
  (via `wsrep_thd_LOCK`). `wsrep_thd_set_wsrep_aborter` itself
  (`sql/service_wsrep.cc:298-313`) takes no lock — it relies on the caller's mutex. If
  another aborter is already recorded it returns true and the kill is skipped
  ("innodb kill transaction skipped due to wsrep_aborter set", ha_innodb.cc:24439-24443).
- `sql/service_wsrep.cc:189-202` (`wsrep_thd_bf_abort` awake path): reads and writes
  `victim_thd->wsrep_aborter` under **`LOCK_thd_data`**, and — critically — **skips
  `victim_thd->awake(THD::KILL_QUERY)`** when a different aborter is already recorded:
  `"victim is killed already by %u, skipping awake"` (service_wsrep.cc:194).

Because the field is written under `LOCK_wsrep_thd` in one path and read under
`LOCK_thd_data` in the other, the read at service_wsrep.cc:192 can observe an aborter that
was recorded but whose own `awake()` never executed (its `wsrep_thd_bf_abort` failed and the
failure path reset flags — ha_innodb.cc:24459-24466 — racing with the second aborter's
check), or observe a torn/stale value. Result: **every aborter believes someone else
delivered the KILL, and nobody did.**

Note also the failure-path reset itself: ha_innodb.cc:24459-24466 re-acquires
`LOCK_wsrep_thd` to clear `was_chosen_as_*` and `wsrep_aborter` — again a different mutex
than the awake path's `LOCK_thd_data`. The TOCTOU in `wsrep_abort_thd`
(`sql/wsrep_thd.cc:333-358`: `is_aborting` checked, lock released, then
`ha_wsrep_abort_transaction`) widens the interleaving space.

## Failure scenario

1. Applier A1 (high-priority) conflicts with local trx V; A1 enters
   `wsrep_innobase_kill_one_trx`, records itself as aborter, calls `wsrep_thd_bf_abort`,
   which **fails** (e.g. transaction state changed concurrently); failure path starts to
   reset `wsrep_aborter` under `LOCK_wsrep_thd`.
2. Concurrently applier A2 (or a TOI MDL abort via `wsrep_abort_thd`) targets the same V.
   Under `LOCK_thd_data` it sees `wsrep_aborter == A1` still set → logs "skipping awake" and
   returns *without* `awake(KILL_QUERY)`.
3. V is parked in a killable wait (row lock wait, condvar). No KILL is ever delivered. V
   holds locks the appliers need → applier stalls → apply/commit monitors stall → flow
   control pauses the whole cluster until `innodb_lock_wait_timeout` (default 50s) or
   forever if V's wait is not an InnoDB lock wait.

## Invariant / assertion plan

- **Primary (SUT-side, `AlwaysOrUnreachable`, missing):** at the skip branch
  (service_wsrep.cc:192-198), assert
  `victim_thd->killed != THD::NOT_KILLED`:
  `"skip-awake only when victim already has a kill signal"`.
  Rationale: the branch's justification is "someone already killed the victim"; if we are
  skipping the wakeup while `killed == NOT_KILLED`, the recorded aborter's KILL was lost and
  this dedup is suppressing the only remaining wakeup. `AlwaysOrUnreachable` (not `Always`)
  because the branch is rare and workload-dependent — a run that never takes it is fine, but
  any execution must satisfy the check. This check is only possible SUT-side: the state is
  a transient two-mutex interleaving invisible to any workload query.
- **Coverage companion (SUT-side, `Sometimes`, missing):**
  `Sometimes("concurrent BF aborters raced on one victim")` at the same branch — confirms
  Antithesis actually produced the multi-aborter interleaving.
- **Workload-side backstop:** the generic cluster-progress drain check (see
  `commit-order-monitor-released-no-cluster-stall`) catches the stall symptom; this property
  exists to localize the cause.

## Config / timing dependencies

- Needs concurrent conflict pressure: hot-row local updates + applier traffic + TOI DDL on
  the same tables (TOI MDL aborts arrive via `wsrep_abort_thd`, the second aborter flavor).
- `wsrep_applier_threads > 1` increases simultaneous aborters.
- No special faults required; CPU throttling / thread hangs (available by default) widen
  the window between "record aborter" and "deliver awake".

## Open questions

None — all three resolved; see Investigation Log. Consequences folded in:

- **signal=false flows exist and DO leave `wsrep_aborter` set with no awake, by design.**
  `THD::notify_shared_lock` calls `wsrep_abort_thd(this, in_use, false)`
  (sql/sql_class.cc:2033-2038); through `wsrep_abort_transaction_func` →
  `wsrep_innobase_kill_one_trx`, the aborter is recorded (ha_innodb.cc:24439) but the
  signal branch of `wsrep_thd_bf_abort` is skipped — the victim's wakeup is the THR_LOCK
  abort, not a KILL. Therefore the primary assertion must NOT be a bare
  `victim->killed != NOT_KILLED` at the skip branch: it needs to either record whether
  the aborter's flow carried `signal=true`, or condition on the victim being parked in a
  killable wait. The evidence remains that a second, signal=true aborter seeing that
  stale record skips its awake — the lost-KILL shape is real; the check just needs the
  signal-flow qualifier.
- **A third reader exists**: `kill_one_thread` (sql/sql_parse.cc:7855-7868) reads
  `wsrep_aborter` under LOCK_thd_data (THD_ptr holds it, mysqld_thd_manager.h:107-138)
  and SUPPRESSES a user `KILL` when it is set — so a stale never-cleared aborter also
  blocks the operator's manual KILL of the wedged session. Worse, the owner-thread
  RESETS happen under NO mutex at all: wsrep_trans_observer.h:441 (cleared after
  commit/rollback) and sql_parse.cc:7891 (autocommit-retry prep). The field is a plain
  `my_thread_id` (sql_class.h:3339), not atomic.
- **The MariaDB single-mutex rework was NOT ported**: the two-mutex split is live in
  8.4.10 (writer/reset under LOCK_wsrep_thd at ha_innodb.cc:24439/24459-24463;
  reader/writer under LOCK_thd_data at service_wsrep.cc:189-202) plus the unlocked
  owner resets above. Regression-gap target confirmed.

**New finding (candidate hang, reachability unproven):** `THD::notify_shared_lock` holds
`in_use->LOCK_thd_data` (sql_class.cc:2023) across `wsrep_abort_thd(..., false)`, whose
call chain re-acquires the same victim's `LOCK_thd_data` in
`wsrep_abort_transaction_func` (ha_innodb.cc:24493-24497) — a self-deadlock on a
non-recursive mutex if the path is reached with a THR_LOCK-holding victim (MyISAM/LOCK
TABLES shapes). Worth a workload probe: BF DDL against sessions holding table-level
locks; symptom would be a hung applier holding a victim's LOCK_thd_data.

## SUT-side instrumentation suggestions (all missing)

- `AlwaysOrUnreachable("skip-awake only when victim already has a kill signal or a
  signal-less abort flow", ...)` at service_wsrep.cc:192-198 — MUST be qualified per the
  investigation: a bare `victim_thd->killed != THD::NOT_KILLED` false-positives against
  the designed signal=false flow (notify_shared_lock); condition additionally on the
  recorded aborter's flow having carried signal=true, or on the victim being in a
  killable wait.
- `Sometimes("concurrent BF aborters raced on one victim")` at the same branch.
- `Sometimes("wsrep_thd_bf_abort failed, victim survives")` at ha_innodb.cc:24459-24466 —
  the reset path is the racing writer; knowing it fired in the same run as the skip branch
  is the interesting interleaving.
- `Sometimes("user KILL suppressed by recorded wsrep aborter")` — sql_parse.cc:7860-7864
  (the third-reader suppression found during investigation).

### Investigation Log

#### Can `wsrep_thd_bf_abort` return true without `signal`, leaving `wsrep_aborter` set but no awake by design?

- Examined: all `wsrep_thd_bf_abort` callers (trx0trx.cc:3848 signal=true;
  ha_innodb.cc:24447 caller-propagated; ha_innodb.cc:24516 caller-propagated); all
  `wsrep_abort_thd`/`ha_wsrep_abort_transaction` callers (wsrep_mysqld.cc MDL branches —
  all signal=true; sql_class.cc:2037 — signal=FALSE); `wsrep_thd_bf_abort` body
  (service_wsrep.cc:164-205); `wsrep_innobase_kill_one_trx` (ha_innodb.cc:24385-24470).
- Found: `THD::notify_shared_lock` (sql_class.cc:2018-2046) is a real signal=false flow.
  In it, `wsrep_innobase_kill_one_trx` records the aborter (:24439, under LOCK_wsrep_thd)
  and `wsrep_thd_bf_abort(…, false)` skips the whole awake/aborter branch — aborter set,
  no KILL, victim woken by THR_LOCK abort instead. The aborter is cleared only by the
  victim itself at trx end (wsrep_trans_observer.h:441) or retry prep (sql_parse.cc:7891).
- Also found (new): notify_shared_lock holds in_use->LOCK_thd_data (:2023) across the
  abort chain which re-locks the same mutex at ha_innodb.cc:24495 — candidate
  self-deadlock; reachability (BF thread + THR_LOCK victim) not demonstrated.
- Conclusion: resolved — yes; assertion design updated to qualify on signal flow.

#### Is `THD::wsrep_aborter` ever read while holding neither mutex? (third reader)

- Examined: every `wsrep_aborter` reference in sql/ and storage/innobase (grep);
  THD_ptr semantics (mysqld_thd_manager.h:107-160, mysqld_thd_manager.cc:402-432).
- Found: third reader `kill_one_thread` (sql_parse.cc:7855-7868) — under LOCK_thd_data
  (THD_ptr acquires it), it suppresses user KILLs when an aborter is recorded. Owner-side
  WRITES with no mutex: wsrep_trans_observer.h:441, sql_parse.cc:7891,
  sql_class.cc:1271/:1422. `wsrep_thd_set_wsrep_aborter` itself takes no lock and relies
  on callers (service_wsrep.cc:298-313).
- Conclusion: resolved — reads are always under one of the two mutexes, but resets are
  unlocked and the two locked sites use different mutexes; the field is a plain
  my_thread_id. The race surface is confirmed and slightly wider than the property
  originally stated (user-KILL suppression added as a symptom channel).

#### Was the MariaDB single-mutex fix (MDEV-23483 family) ported to this tree?

- Examined: the locking at every wsrep_aborter site (above); searched sql/ for any
  consolidated-mutex or atomic rework of the field.
- Found: the split persists — LOCK_wsrep_thd on the InnoDB kill/reset path,
  LOCK_thd_data on the awake/kill paths, unlocked owner resets. No port found.
- Conclusion: resolved — not ported; the property stands as a regression-gap target with
  the MariaDB rework as external prior art.
