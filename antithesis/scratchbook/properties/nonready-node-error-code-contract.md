# nonready-node-error-code-contract

**Focus:** Protocol contracts — client-visible error-code taxonomy.
**Confidence:** High (all code paths read directly at commit f9ecb3e).

## Claimed contract

The documented client contract (docs.percona.com/8.4; sut-analysis §7.6) is:

- **ER 1213 / ER_LOCK_DEADLOCK (SQLSTATE 40001)** — transient conflict (first-committer-wins
  loser, BF abort). Contract: *retriable on the same node*; every client library and retry
  middleware treats 40001 as "retry here".
- **ER 1047 / ER_UNKNOWN_COM_ERROR** — node not ready / non-primary. Contract: *not retriable
  here*; clients/proxies should fail over to another node.

## Where the code breaks the contract

1. **TOI (DDL) on a not-ready node returns ER_LOCK_DEADLOCK, not 1047.**
   `sql/wsrep_mysqld.cc:3016-3024` (`wsrep_to_isolation_begin`):
   ```
   if (!wsrep_ready) {
     my_error(ER_LOCK_DEADLOCK, MYF(0), "WSREP replication failed. Check "
              "your wsrep connection state and retry the query.");
   ```
   A client that issues DDL against a partitioned/joining node receives a retriable
   deadlock code and will hammer the same unavailable node indefinitely instead of failing
   over. (The DML path gets the correct 1047 via the readiness gate at
   `sql/sql_parse.cc:1678-1698`; only statements that reach TOI hit this mislabeled exit —
   the readiness state can change between the do_command gate and TOI entry, and some TOI
   entry points, e.g. the event scheduler `sql/event_data_objects.cc:1033`, bypass the gate.)

2. **Sync-wait failure returns ER 1205 / ER_LOCK_WAIT_TIMEOUT.**
   `sql/wsrep_mysqld.cc:1532-1545` (`wsrep_sync_wait`): default branch sets
   `err = ER_LOCK_WAIT_TIMEOUT` with the in-code admission
   `// HERE! NOTE: the above msg won't be displayed with ER_LOCK_WAIT_TIMEOUT`.
   A causality failure (node cannot prove it has caught up — e.g. partitioned, FC-stalled)
   is indistinguishable from an ordinary InnoDB row-lock timeout, which clients also treat
   as retry-in-place.

3. Related (context): BF-abort-while-idle also returns 1213 (`sql_parse.cc:1656`) — that one
   *is* contract-conformant (genuinely retriable in place).

## Failure scenario

Antithesis partitions node A out of the primary component while the workload runs DDL +
retry-on-1213 logic (the retry logic every production client implements). Node A returns
1213 for DDL → the workload retries on A forever → livelock that the documented contract
says cannot happen. Same shape for 1205 under a partition with `wsrep_sync_wait=1`.

## Suggested assertions (all missing — no SDK instrumentation exists)

- **Always (workload-side):** whenever a statement fails with ER 1213, the node's
  `wsrep_ready` was ON and `wsrep_cluster_status` was `Primary` at failure time. Checkable
  on the same connection: `SHOW STATUS LIKE 'wsrep_ready'` carries `CF_SKIP_WSREP_CHECK`
  (`wsrep_mysqld.cc:769-772`) so it succeeds even on a non-ready node. Message:
  "ER 1213 implies node was ready (deadlock code never masks node-unavailable)".
  *Expected to FAIL per the code reading above — that is the point: it demonstrates the
  documented contract is not enforced, with a concrete client-livelock consequence.*
- **Always (workload-side):** whenever a statement fails with ER 1205 under
  `wsrep_sync_wait!=0`, an actual InnoDB lock wait was in progress (proxy check: the same
  read with `wsrep_sync_wait=0` on the same connection succeeds instantly ⇒ the 1205 was a
  sync-wait failure in disguise). Message: "ER 1205 is a real lock timeout, not a masked
  sync-wait failure".
- **Sometimes (workload-side):** "DDL attempted while node not ready" — exploration hint to
  make Antithesis drive TOI into the not-ready window (partition + concurrent DDL).

## Assertion-type rationale

`Always`: the taxonomy must hold on every error observation; a single mislabeled code is a
contract violation. The Sometimes companion ensures the interesting window is actually
reached (it needs a partition racing a DDL dispatch).

## Fault requirements

Default-on network faults suffice (partition → non-primary → not-ready). No node
termination or clock faults needed.

## Open questions

- Does percona-toolkit / ProxySQL retry logic in the wild actually key on 1213/40001?
  (Yes per MySQL convention, but confirming sharpens the "Why It Matters".) `(needs human input)`

### Investigation Log

#### Which TOI entry points reach wsrep_to_isolation_begin with !wsrep_ready without passing the sql_parse gate?

- Examined: all `wsrep_to_isolation_begin`/`WSREP_TO_ISOLATION_BEGIN` callsites in
  `sql/` (22 files), `sql/wsrep_mysqld.cc:2995-3045` (the not-ready 1213 exit and its
  async-action comment), `sql/event_data_objects.cc:1190-1261` (event completion drop),
  `sql/events.cc:355-374`, `sql/event_db_repository.cc:180-275`,
  `sql/sql_base.cc:6340-6372`.
- Found: two classes. (a) **Fully ungated async initiator**: the event scheduler's
  drop-on-completion — `Event_job_data::execute(thd, drop=true)` constructs an internal
  DROP EVENT and calls `Events::drop_event` directly from the event worker thread
  (event_data_objects.cc:1244-1250), reaching the TOI readiness check
  (events.cc:371 → wsrep_mysqld.cc:3016) with no do_command/execute_command gate ever
  run. This gives a *deterministic* driver for the mislabeled-1213 branch: schedule
  short-lived `ON COMPLETION NOT PRESERVE` events and partition the node — no
  statement-timing race needed. (b) **Gate-passed-then-partition**: every user DDL
  callsite (sql_parse/sql_table/sql_tablespace/sp/sql_trigger/... plus ALTER/DROP EVENT
  via event_db_repository.cc:187/:250/:268) sits inside `mysql_execute_command`, so the
  1047 gate ran first and readiness must flip between gate and TOI entry — narrow;
  needs the Sometimes driver (partition injected mid-statement). Bonus:
  `wsrep_replicate_myisam` DML→TOI kickstart (sql_base.cc:6365-6371) is a third entry,
  only with that non-default option ON.
- Conclusion: RESOLVED — the Always assertion has a deterministic trigger via the
  event-scheduler leg (workload should include recurring auto-drop events); the
  Sometimes driver remains for the user-DDL window. The 1213-mislabel is reachable
  without any statement-level race.

## Synthesis refinement (2026-09-10)

KNOWN-RED pre-registration: the unready-TOI 1213 path is deterministic — this property is expected to fail from run one (deliberate bug-finder). Pre-register that arm as a known finding at first triage and carve it out so the rest of the error-code contract still guards regressions.
