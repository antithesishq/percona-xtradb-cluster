---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# non-primary-rejects-writes — Non-Primary / unready node never acknowledges a data-changing statement

**Merged from two independent discoveries (focus 7: distributed coordination, as
`non-primary-rejects-writes`; focus 8: lifecycle, as `unready-node-rejects-writes`) —
near-identical invariants over the same gates, approached from the partition side and the
join/startup side respectively. Cross-links: `nonready-node-error-code-contract` (which
error code — a distinct client-contract concern kept as its own property) and
`acked-commit-durable-across-restart` (the durability half of the same ack journal).**

**Type:** Safety | **Assertion:** `Always` (ack journal: every acked unique-keyed write
present exactly once cluster-wide after convergence) + `Sometimes` companions
| **Confidence:** High for the gate mechanics (read directly); Medium for the end-to-end
"no acked write from a minority/unready node" form (commit-time race window not fully
traced).

## Claim under test

A node that is not part of the Primary Component or not yet Synced (joiner during SST/IST,
JOINED but still draining, non-Primary after partition, DISCONNECTED during startup) never
acknowledges a data-changing statement — donor is the one legal exception (it keeps
`wsrep_ready=ON`, see `donor-returns-to-synced`). Clients get ER 1047
`ER_UNKNOWN_COM_ERROR` ("WSREP has not yet prepared node for application use").
Consequently, every write acknowledged to a client was accepted by a node in the (unique)
Primary Component and survives partition heal.

## Code (validated, commit f9ecb3e)

- **Readiness definition**: `wsrep_ready` set true ONLY on `s_synced`
  (`sql/wsrep_server_service.cc:349-397`, `log_state_change`) — `case s_synced:
  wsrep_ready = true` at `:379`; the `s_joined`/`s_donor` fall-through only sets
  `wsrep_cluster_status = "Primary"` and leaves `wsrep_ready` at its prior value (false
  for a joiner reaching JOINED; **true for SYNCED→DONOR**). `s_connected` (non-Primary)
  and `s_disconnected` set `wsrep_ready = false`. Reader under `LOCK_wsrep_ready`:
  `sql/wsrep_mysqld.cc:794-807`.
- **Dispatch-level gate** (`do_command`): `sql/sql_parse.cc:1678-1698` — rejects with
  ER 1047 when not applier && (`!wsrep_ready_get()` || `wsrep_reject_queries != NONE`),
  unless `CF_SKIP_WSREP_CHECK`. **COM_QUERY and COM_STMT_EXECUTE carry
  CF_SKIP_WSREP_CHECK** (`sql_parse.cc:615-634`, in-code comment "checked later in
  mysql_execute_command()") — the dispatch gate does NOT cover normal queries.
- **The real gate** (`mysql_execute_command`): `sql/sql_parse.cc:3789-3808`. Bypass list
  (each is attack surface): `thd->wsrep_applier`; `wsrep_dirty_reads` AND statement not
  `CF_CHANGES_DATA` (`:3798-3799` — **dirty reads never authorize writes**);
  `wsrep_tables_accessible_when_detached` (I_S/P_S reads); `SQLCOM_SET_OPTION`,
  `SQLCOM_SHUTDOWN`; table-less SELECT; SHOW commands. None of the bypasses admits a
  data-changing statement — that is exactly the invariant. This single gate is a narrower
  defense than documented.
- **Provider backstop**: `galera/src/replicator_smm.cpp:765` (region) — `replicate()` in
  state < S_JOINED returns CONN_FAIL, so a statement slipping past the SQL gate cannot
  certify while non-primary. GCS refuses causal reads with -EPERM in non-prim
  (`gcs/src/gcs.hpp:305-313`). But `gcs/src/gcs_core.hpp:102`: "successful return code
  does not guarantee delivery to group" — the crack the ack-journal check probes.
- **Warn-and-proceed transitions**: the wsrep-lib server_state transition matrix only
  warns in release builds (`wsrep-lib/src/server_state.cpp:1454-1500`), so an illegal
  transition (e.g. forced donor→synced skip at `:1176-1183`) can flip `wsrep_ready=ON`
  earlier than the spec allows (see `illegal-wsrep-transition-never-taken`).
- Known misleading sibling: TOI when `!wsrep_ready` returns ER 1213 deadlock
  (`sql/wsrep_mysqld.cc:3016-3025`) — see `nonready-node-error-code-contract`.
- Other entry points that must run the same bracket: event scheduler
  `sql/event_data_objects.cc:1033`, `srv_session.cc:915/:1184`, COM_CHANGE_USER
  `sql_parse.cc:2327/2383` (not all re-verified line-by-line).

## The interesting window (what Antithesis uniquely explores)

The gates are checked at statement entry. A transaction's COMMIT *in flight through
certification* when the node loses Primary status is the race: `certify_commit`
(wsrep-lib `src/transaction.cpp:1804-2010`) has a 12-branch status switch on provider
return; partition-at-certify must map to rollback, never to "committed locally but not in
group". On the join side: a window where the state machine is JOINER/JOINED but a stale
`wsrep_ready` value, a race on `LOCK_wsrep_ready`, or an unguarded entry point (event
scheduler firing at startup, srv_session, prepared-statement replay) lets a client
INSERT/UPDATE commit locally — a write neither certified against the group's current state
nor current data → lost/phantom write, later silent divergence.

## Failure scenario

1. Writers on all nodes with client-side ack journaling (unique key per write, node id,
   wall time, result), running continuously through partition and join/leave churn.
2. Antithesis partitions a node into a minority mid-load (Primary→non-Primary within
   ~evs.suspect 5s / inactive 15s), and/or a node rejoins via IST/SST.
3. Bug shapes: (a) statement acked after the provider delivered the non-prim view but
   before `wsrep_ready` flipped; (b) certification returns success on the minority side
   for a writeset the majority never ordered; (c) readiness flag lost during rapid view
   flapping; (d) alternate entry point skips the `execute_command` gate during JOINER.
4. Violation: after heal, an acked write is absent from the merged cluster (it existed
   only on the minority side and was discarded on rejoin via IST/SST).

## How to check (workload-side)

- **Primary check (`Always`)**: every client-acked committed write (unique key) is present
  exactly once on every node after cluster convergence (`wsrep_sync_wait=1` read or
  post-heal settle). Sound, race-free formulation — no timing of the non-prim window
  required. Day-one implementable, zero SUT instrumentation.
- Secondary check: while a node reports `wsrep_cluster_status != Primary` or
  `wsrep_ready = OFF`, issued INSERT/UPDATE either errors (1047, 1213, 1205, server-gone)
  or, if it succeeds, must satisfy the primary check. Never assert "must error" on a
  poll-race basis alone.
- Gate/status consistency: a connection that gets ER 1047 then `SHOW STATUS LIKE
  'wsrep_ready'` (SHOW bypasses the gate) should see OFF — mismatch = inconsistency.
- `Sometimes(got_1047_from_partitioned_node)` and `Sometimes(got_1047_while_JOINER)` —
  gate reachability under real partitions and real joins (MTR exercises only symmetric
  self-isolation).

## Instrumentation suggestions (missing)

- SUT-side `Always` at the `mysql_execute_command` gate exit: statement admitted to
  execution implies `wsrep_ready==ON` or statement in the documented bypass set; second
  callsite at commit ack (`wsrep_after_command`): acked write commit implies server state
  was synced or donor for the duration.
- SUT-side `Unreachable` in wsrep-lib `transaction.cpp` certify path: "certify returned
  success while server_state < s_joined" (confirms the provider-level backstop).

## Fault requirements

Network partitions (default-on) suffice for the non-Primary and IST-rejoin windows. CPU
throttle widens the view-callback→ready-flag window. Node termination (often disabled)
widens coverage to SST-joiner and startup-before-ready windows — partial coverage without
it, flagged. No clock faults.

## Open Questions

None — all four resolved (see Investigation Log). Key consequences folded into the
property: the secondary ("must error while non-Primary") check must keep its tolerance
window — the readiness flag legitimately lags the provider's total order, and a commit
completing in that lag is benign by construction (its writeset was ordered inside the
Primary Component, so it survives heal). The primary ack-journal `Always` remains
strict and sound. Checker rule for ER 1047: as long as the workload never sets
`wsrep_reject_queries`, every 1047 carrying the "WSREP has not yet prepared node" message
implies unready/non-primary (`pxc_maint_mode` never produces 1047).

### Investigation Log

#### Ordering of (non-prim view delivered) → (wsrep_ready=OFF) vs in-flight COMMITs — window that lets a commit complete?

- Examined: `galera/src/replicator_smm.cpp:748-880` (`replicate()`),
  `gcs/src/gcs_group.hpp:173-235` (`gcs_group_handle_act_msg`),
  `galera/src/wsrep_provider.cpp:588-660` (replicate-then-certify glue).
- Found: an ack requires `replicate()` to return a real global seqno, and
  `gcs_group_handle_act_msg` assigns one (`rcvd->id = ++group->act_id_`) **only when
  `GCS_GROUP_PRIMARY == group->state` at the writeset's position in the delivered total
  order**; a local writeset delivered while non-prim gets `-ERESTART`
  (gcs_group.hpp:214-225) → `gcs_repl` fails → `replicate()` goes to `must_abort` →
  `WSREP_CONN_FAIL` (replicator_smm.cpp:826-844, 765-781). Conf changes and writesets
  share one total order, so a writeset ordered before the non-prim conf was
  delivered/deliverable to all old-view survivors (majority included).
- Not found: any path assigning a seqno to a local action outside PRIMARY state.
- Conclusion: RESOLVED. The `wsrep_ready`-lag window exists (the flag is only a
  statement-entry gate) but is benign: a commit can complete during the lag only if its
  writeset was ordered inside the PC, which makes it durable across heal. The secondary
  check must stay tolerant; the ack-journal `Always` is strict-sound. The
  `gcs_core.hpp:102` "successful return does not guarantee delivery" caveat applies to
  raw core send, not to `gcs_repl`, which waits for ordered self-delivery. Residual bug
  surface (what the property still tests): EVS SAFE-delivery correctness under
  partitions — cross-ref `gcs-total-order-gap-free`.

#### Do event scheduler / srv_session write entry points pass the execute_command gate?

- Examined: `sql/event_data_objects.cc:1018-1041` (event bracket), `sp_instr.cc:1105`,
  `sql_prepare.cc:3610`, `sql/srv_session.cc:1141-1221`, gate at
  `sql_parse.cc:3789-3808`, dispatch gate at `sql_parse.cc:1678-1698`.
- Found: every data-changing statement funnels through `mysql_execute_command` and its
  readiness gate: event bodies and stored programs via `sp_instr.cc:1105`, prepared
  statements via `sql_prepare.cc:3610`, srv_session COM_QUERY via
  `dispatch_command → dispatch_sql_command → mysql_execute_command`
  (srv_session.cc:1208). Both alternate entry points run the `wsrep_open` +
  `wsrep_before_command` bracket (event_data_objects.cc:1033-1034,
  srv_session.cc:915/:1184). `Srv_session::execute_command` bypasses only the
  `do_command` dispatch gate — redundant for writes since COM_QUERY/COM_STMT_EXECUTE
  carry CF_SKIP_WSREP_CHECK past it anyway. The dirty-reads bypass admits only
  statements with `(sql_command_flags & CF_CHANGES_DATA) == 0` (sql_parse.cc:3798-3799).
- Not found: any write entry point skipping the gate. (The event-completion internal
  DROP EVENT bypasses the SQL gate but is caught by the TOI readiness check at
  `wsrep_mysqld.cc:3016` and is DDL, not a client-acked data write — see
  `nonready-node-error-code-contract`.)
- Conclusion: RESOLVED — no bypass to enumerate; the workload-side assertion callsite
  set is complete as formulated.

#### SR fragments straddling the partition (acked SR commit as the sharpest variant)

- Examined: wsrep-lib `src/transaction.cpp:1851-1866` (certify_commit streaming leg),
  `galera/src/wsrep_provider.cpp:588-660`.
- Found: the SR commit fragment goes through the identical `certify_commit` →
  `provider().certify()` path as a plain transaction (SR keys appended, commit flag
  set), so the same ordering-implies-majority-delivery argument covers it: an acked SR
  COMMIT's commit fragment was ordered in the PC and survives heal. The ack journal
  covers SR sessions without modification.
- Not found: (not needed for this property) the cleanup fate of *unacked* in-flight SR
  transactions on the majority side — that divergence surface is owned by
  `sr-rollback-fragment-noop` and `sr-fragment-cross-node-agreement`.
- Conclusion: RESOLVED for this property's scope. Keep the SR workload variant
  (`wsrep_trx_fragment_size>0`) so acked-SR-commit-straddling-partition is exercised.

#### Disambiguating ER 1047 causes (reject_queries / maintenance) in the checker

- Examined: all ER_UNKNOWN_COM_ERROR sites in `sql/` (`sql_parse.cc:1686, 2368, 2895,
  2916, 3805`), `pxc_maint_mode` usage (`sql_parse.cc:4482-4520`, `wsrep_mysqld.cc:170`),
  `block_write_while_in_rolling_upgrade` (`sql_parse.cc:1867-1916`).
- Found: both wsrep gates emit 1047 with the identical message "WSREP has not yet
  prepared node for application use" for `!wsrep_ready` OR `wsrep_reject_queries != NONE`
  — indistinguishable per-error. `pxc_maint_mode` has **no query-rejection path at all**
  (it only feeds clustercheck and the shutdown transition sleep) — maintenance mode never
  produces 1047. The rolling-upgrade write block emits ER_UNKNOWN_ERROR (1105), not 1047.
  Non-wsrep 1047 sites (bad COM_SET_OPTION subcommand, unknown command, GR stream) carry
  no message text.
- Conclusion: RESOLVED. Checker rule: workload never sets `wsrep_reject_queries` ⇒ any
  1047 with the WSREP message implies unready/non-primary. SHOW STATUS/VARIABLES bypass
  both gates for post-hoc confirmation.
