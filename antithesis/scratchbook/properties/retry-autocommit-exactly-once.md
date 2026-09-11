# retry-autocommit-exactly-once — silently retried autocommit statements take effect exactly once

**Focus area:** Idempotency and Replay. **Commit:** f9ecb3ebe8ff (branch 8.4).
**Confidence:** High for the mechanism (retry loop read in full); Medium for the interesting
failure mode (an effects-doubling interleaving is hypothesized, not demonstrated).

## Claim under test

`wsrep_retry_autocommit` (default 1) makes the server transparently re-execute an autocommit
statement that lost a first-committer-wins conflict (BF abort / certification failure) instead
of returning ER_LOCK_DEADLOCK. The contract:

1. Effects of the statement appear **exactly once** cluster-wide when the client receives OK —
   never once per attempt.
2. Effects appear **zero times** when the client receives the final ER_LOCK_DEADLOCK (retries
   exhausted).
3. The retry is only taken for transactions that were *not* certified: certified-but-aborted
   transactions must go through replay (which commits the original writeset), never through
   re-execution as a *new* writeset.

Claim 3 is the idempotency core: retry re-parses and re-executes the SQL text
(`wsrep_prepare_for_autocommit_retry` resets the parser to the saved query buffer), producing a
brand-new writeset with a new trx id. If a writeset from the aborted attempt could survive on
peers, retry would double the effects on every node except in a different transaction.

## Code paths

- Retry loop: `sql/sql_parse.cc:7972-8049` (`wsrep_dispatch_sql_command`) — retry taken when
  `wsrep_after_statement(thd)` returns nonzero, `is_autocommit`, the command is retry-safe
  (`wsrep_should_retry_in_autocommit` :7928-7970 — excludes SELECT/CHECK/HANDLER OPEN and
  admin-partition ALTER because partial result sets were already sent), and
  `wsrep_retry_counter < wsrep_retry_autocommit`.
- Retry preparation: `sql_parse.cc:7887-7926` — `thd->clear_error()`, `close_thread_tables`,
  `reset_sync_wait_gtid`, counter++, parser reset. Note `assert(thd->wsrep_trx().active() ==
  false)` :7923 — the previous wsrep transaction must be fully torn down before re-execution.
- Ordering guarantee that retry is certification-failure-only:
  `wsrep-lib/src/transaction.cpp:856-958` (`after_statement`) — `s_must_abort` with
  `certified()` → `s_must_replay` → `replay()` happens *inside* `wsrep_after_statement`, before
  the retry decision. Only `s_aborted` (never-certified or cert-failed, dummied on all peers)
  falls through with an error that triggers retry.
- Error normalization before retry: `sql_parse.cc:7992-8003` — ER_QUERY_INTERRUPTED from a mid-
  execution `thd->awake()` BF abort is rewritten to ER_LOCK_DEADLOCK; `thd->killed` is reset.
- Query saved for retry: `sql_parse.cc:1811-1819` (`wsrep_copy_query` target buffer).
- Debug sync point for deterministic interleaving: `sync.wsrep_retry_autocommit`
  (`sql_parse.cc:8010-8016`).

## Failure scenarios

- **Double effects:** any path where the first attempt's writeset reaches peers (certified) but
  the local decision machinery lands in `s_aborted` instead of `s_must_replay` — e.g. a race
  between BF abort delivery and the certify() result 12-branch switch (transaction.cpp:1897-2010,
  including the in-code "Galera may return CONN_FAIL if trx BF aborted O_o" branch). Peers apply
  writeset W1; origin retries as W2; both certify (different trx, no conflict with itself) →
  every node has the row twice, or origin diverges from peers on auto-inc/generated values.
- **Lost effects with OK:** retry loop reports OK from a retry attempt whose own writeset then
  fails in a way that skips error propagation (`thd->killed` reset at :8001/:8028 is
  suspicious surface — killed state cleared while a rollback is still pending elsewhere).
- **Non-transactional side effects doubled:** statements invoking functions/UDFs with
  extra-transactional side effects execute twice by design under retry. Not a bug per se, but
  the workload must not use such statements for exactly-once checks (or should use them
  deliberately to demonstrate the documented hazard).
- **Retry of partially-sent result sets:** the SELECT/CHECK exclusion list (:7957-7961) is a
  hand-maintained denylist ("If the same symptom is found for other commands, then please add
  it") — a statement type that returns data and is missing from the list yields malformed-packet
  client errors under BF abort. Workload can probe: multi-row INSERT ... RETURNING-like paths,
  SHOW-adjacent commands under TOI conflicts.

## Suggested assertions (all missing)

- **Primary (workload, Always):** for every autocommit INSERT of a unique client-generated id
  under cross-node conflict load: id present exactly once on all nodes iff client got OK; zero
  times iff client got an error. `Always` — a per-statement safety invariant.
- **Coverage (SUT-side, missing, Sometimes):** at `sql_parse.cc:8017-8021` (retry branch taken)
  — `Sometimes(autocommit retry executed)`; without it the run may never exercise retry and the
  primary check degenerates to the plain first-committer-wins property. Workload-side proxy:
  monitor `wsrep_local_bf_aborts` delta while asserting zero ER_LOCK_DEADLOCK seen by clients
  running with retry enabled and conflict rate below the retry budget.
- **Variant knob:** run one variant with `wsrep_retry_autocommit=0` (every conflict surfaces) and
  one with a high value (e.g. 10) so the retry path dominates.

## Fault / workload requirements

- No special faults required: multi-node conflicting autocommit DML. Network
  delay/partition (default-on) stretches the executing→certifying window where BF aborts land.
- Best combined with `wsrep_applier_threads > 1` and hot-row + unique-secondary-key schema
  (same generator as bf-replay-commits-exactly-once; the two properties share a workload and
  differ in the knob and the checked outcome).

## Open questions

None — all three resolved; see Investigation Log. Consequences:

- **Claim 3 is structural** (no certified-writeset-re-executed interleaving found): in
  galera, all four post-group-ordering BF-abort sites for an F_COMMIT transaction set
  `S_MUST_REPLAY` (replicate replicator_smm.cpp:895-905; handle_local_monitor_interrupted
  :3676-3682; finish_cert :3725-3739; handle_apply_monitor_interrupted :3865-3884);
  `WSREP_CONN_FAIL` is only produced before the writeset enters group order (must_abort
  label :765-781 and gcs failures where seqnos are still ILL, :826-843), so the wsrep-lib
  CONN_FAIL branch (transaction.cpp:1959-1984) never fires for a writeset peers will
  apply; certification failures are dummied cluster-wide. The property's value
  concentrates on claims 1-2 (effects-vs-outcome accounting) as the regression oracle
  over this structural argument.
- **TOI DDL retry is idempotent-by-reach:** the retry loop triggers only when
  `client_state::after_statement` returns 1, i.e. `current_error() ==
  e_deadlock_error` (wsrep-lib client_state.cpp:288-299). For a DDL that error arises
  when the TOI certification fails — a writeset that was dummied on every node and
  executed nowhere — so re-broadcasting it as a fresh TOI preserves exactly-once. A
  locally-failed DDL whose TOI writeset peers DID execute does not set the wsrep deadlock
  error and cannot loop back through retry. (The `wsrep_write_dummy_event` GTID/binlog
  discontinuity is real but orthogonal to effects idempotency.)
- **The retry re-runs sync-wait:** the loop re-enters `dispatch_sql_command` →
  `mysql_execute_command`, whose per-statement `WSREP_SYNC_WAIT` macros re-execute
  (sql_parse.cc:4357/4373/4386/...), and `wsrep_prepare_for_autocommit_retry` explicitly
  clears the stale sync-wait GTID (`reset_sync_wait_gtid`, sql_parse.cc:7894-7895 — the
  comment states this exact intent), so the retried statement re-waits on a fresh view.

### Investigation Log

#### Is there a reachable interleaving where a certified trx ends `after_statement` in `s_aborted` while peers apply its writeset?

- Examined: the full certify status switch (`wsrep-lib/src/transaction.cpp:1804-2013`,
  `certify_commit`); `send_pending_rollback_events` failure path (:1825-1849 — occurs
  BEFORE `provider().certify()`, writeset not yet replicated); galera `galera_certify`
  (wsrep_provider.cpp:560-680); `ReplicatorSMM::replicate` (replicator_smm.cpp:744-935);
  `ReplicatorSMM::certify` (:1069-1144); `handle_local_monitor_interrupted` (:3663-3705);
  `finish_cert` (:3707-3769); `handle_apply_monitor_interrupted` (:3865-3884);
  `before_rollback` certified() gate (:709-722).
- Found: every BF abort that lands after the writeset has a global seqno, on an F_COMMIT
  transaction, sets S_MUST_REPLAY → wsrep-lib error_bf_abort/success+must_abort branches
  → s_must_replay → replay. WSREP_CONN_FAIL is only returned when the writeset never
  entered group order (pre-replication abort, gcs schedule/repl failure with seqnos still
  GCS_SEQNO_ILL — asserted at :835). Certification failure dummies the writeset on all
  nodes identically. The suspicious wsrep-lib CONN_FAIL+s_must_abort branch
  (:1959-1971) therefore only handles never-ordered writesets, where plain abort + retry
  is exactly-once-safe.
- Not found: any path returning a non-replay abort for a group-ordered committing
  writeset.
- Conclusion: resolved — claim 3 structural; retry can only re-execute statements whose
  first writeset either never reached the group or was dummied everywhere.

#### Is TOI DDL retry idempotent given `wsrep_write_dummy_event`?

- Examined: retry trigger condition (`wsrep_after_statement` wrapper
  wsrep_trans_observer.h:456-462; `client_state::after_statement`
  client_state.cpp:258-302 — returns 1 iff current_error()==e_deadlock_error);
  `wsrep_should_retry_in_autocommit` (sql_parse.cc:7928-7970); retry loop
  (:7972-8034); `transaction::after_statement` mode assertion (transaction.cpp:861).
- Found: retry requires the wsrep client error to be e_deadlock_error at statement end.
  For DDL, that error is set by TOI certification failure (enter_toi cert conflict) —
  a TOI writeset that failed certification is dummied cluster-wide and executed on no
  node, so the retried DDL is a brand-new TOI with no prior effects. A DDL that
  broadcast successfully and then failed locally surfaces its SQL error without the
  wsrep deadlock error → no retry.
- Not found: an exhaustive audit of every enter_toi error path was not performed; the
  by-mechanism argument (deadlock error ⇔ cert failure ⇔ dummied everywhere) covers the
  reachable retry trigger.
- Conclusion: resolved — TOI DDL retry re-executes only never-applied DDL; the
  wsrep_write_dummy_event GTID discontinuity is a separate (non-idempotency) concern.

#### Does the retry loop re-run sync-wait?

- Examined: `wsrep_prepare_for_autocommit_retry` (sql_parse.cc:7887-7926, incl. the
  comment at :7893-7895); `reset_sync_wait_gtid`
  (wsrep-lib include/wsrep/client_state.hpp:890-893); WSREP_SYNC_WAIT macro
  (sql_parse.h:195) and its per-statement call sites in mysql_execute_command
  (sql_parse.cc:4357, 4373, 4386, 5265, 5815, ...).
- Found: the retry loop re-invokes dispatch_sql_command → mysql_execute_command, so the
  per-statement sync-wait hooks re-run; the retry prep clears sync_wait_gtid_ so the
  re-wait is performed fresh rather than being satisfied by the pre-conflict wait.
- Conclusion: resolved — retry re-waits; no stale-read window from a skipped re-wait.
