# rolling-upgrade-write-gate — Evidence

**Property (catalog one-liner):** Whenever the server considers the cluster to span multiple
major versions (wsrep_protocol_version < V4, or the debug injection
`simulate_wsrep_multiple_major_versions`), a node with `pxc_strict_mode` ENFORCING or MASTER
rejects every data-changing statement, and `pxc_maint_mode` is forced to MAINTENANCE; when
the condition clears, writes resume and a *forced* MAINTENANCE reverts to DISABLED.

**Type:** Safety (with a liveness tail on the revert). **Assertion:**
`AlwaysOrUnreachable` — the gate is an optional path (active only while the multi-major
state is injected), but any execution while it is active must reject the write. This is
exactly the "optional path, but every execution must satisfy the invariant" shape.
**Confidence:** high — both code sites read directly at commit f9ecb3e; the only caveat is
that the trigger is debug-build-only in a single-version cluster.

## Mechanism

- `sql/sql_parse.cc:1867-1916` — `block_write_while_in_rolling_upgrade(THD*)`:
  - Skips: server not yet initialized, and wsrep appliers (:1876-1878) — appliers must keep
    applying remote writesets while the gate blocks local clients.
  - Applies to `CF_CHANGES_DATA` statements only (:1882).
  - Trigger (:1883-1886): `wsrep_protocol_version < WsrepVersion::V4` **OR**
    `DBUG_EVALUATE_IF("simulate_wsrep_multiple_major_versions", true, false)`.
  - Outcome by `pxc_strict_mode` (:1887-1912): DISABLED → nothing; PERMISSIVE → SQL warning
    + WSREP_WARN only; ENFORCING/MASTER (default is ENFORCING) → `block = true`,
    `my_message(ER_UNKNOWN_ERROR, "Percona-XtraDB-Cluster prohibits use of multiple major
    versions while accepting write workload ...")`. Note: no dedicated error code —
    clients cannot distinguish this from other strict-mode ER_UNKNOWN_ERROR sites.
  - Call site `sql/sql_parse.cc:3809-3811`, immediately after the wsrep readiness gate in
    `mysql_execute_command` — so it covers dispatched statements including prepared
    statements and stored-program bodies that route through this function.
- `sql/wsrep_server_service.cc:189-231` — `Wsrep_server_service::log_view`, runs on EVERY
  view under `LOCK_global_system_variables`:
  - :200 `wsrep_protocol_version = view.protocol_version()` — renegotiated per view.
  - :202-215 if `(multi_version_cluster && pxc_strict_mode > PERMISSIVE) || DBUG(...)` and
    not SHUTDOWN → `pxc_maint_mode = MAINTENANCE; wsrep_pxc_maint_mode_forced = true`.
  - :216-230 else, if `wsrep_pxc_maint_mode_forced` → reset to DISABLED.
- Interlock hazards documented in sut-analysis (focus 12 §7), both reachable via this gate:
  1. **Operator-drain hijack:** operator sets MAINTENANCE manually
     (`wsrep_pxc_maint_mode_forced` stays false); a multi-major view arrives → the forced
     branch overwrites the flag to true; when the condition clears, the next view silently
     reverts the *operator's* MAINTENANCE to DISABLED — un-draining the node.
  2. **Health-check blackhole:** clustercheck returns 200 only when
     `pxc_maint_mode == DISABLED`; the forcing runs on every node of a mixed-major cluster
     simultaneously → all nodes 503 → proxy marks the whole cluster down. Checkable in the
     single-version harness with the debug injection active on all nodes.
- Comment drift (harmless, worth noting): the :1870-1874 comment says
  "wsrep_protocol_version == 4" before the first view, but the initializer is
  `wsrep_max_protocol_version = V7` (sql/wsrep_mysqld.cc:137-139). The `is_initialized()`
  guard makes the value irrelevant pre-view.
- Protocol history context: wsrep_mysqld.cc:109-136 documents V0-V7; V4 is the
  8.0-era boundary below which "Writing on a new node can crash nodes with lower version
  8.0->5.7" (:1863-1866) — the gate exists to prevent cross-major writeset formats reaching
  old appliers.

## Failure scenario

With the debug flag set on an ENFORCING node and a view change delivered (network blip →
view churn), the gate must be active. Bugs this property can catch:

- A write slipping through a dispatch path that bypasses `mysql_execute_command`'s check
  (event scheduler `event_data_objects.cc:1033`, srv_session entry points,
  `CF_SKIP_WSREP_CHECK`-style escapes) while the cluster believes it is mixed-major — the
  exact writeset-format hazard the gate exists to stop.
- The forced/revert state machine wedging: `wsrep_pxc_maint_mode_forced` left true after the
  condition clears (permanent 503), or MAINTENANCE never forced despite the condition
  (health checks keep routing writes to a gated node that errors on every DML).
- The revert clobbering an operator-set MAINTENANCE (hazard 1 above) — assertable by the
  workload: it sets MAINTENANCE itself, injects+clears the flag, then checks the mode
  survived.

## Antithesis angle

Fault injection supplies the view churn that drives `log_view` re-evaluation while the
workload toggles `simulate_wsrep_multiple_major_versions` (via
`SET GLOBAL debug = '+d,simulate_wsrep_multiple_major_versions'` per node, debug build) and
continuously attempts DML. Interleavings of {flag set/cleared} × {view delivery} ×
{operator SET pxc_maint_mode} × {in-flight transactions at gate activation} are exactly what
Antithesis explores; a transaction prepared before activation and committed after is the
interesting boundary case.

## Instrumentation suggestions (all missing)

- Workload-side `AlwaysOrUnreachable`: while the workload knows the gate is active on node N
  (it set the debug flag and observed forced MAINTENANCE), every CF_CHANGES_DATA statement
  it issues to N fails with ER_UNKNOWN_ERROR and no data change is observed cluster-wide;
  reads on N still succeed.
- Workload-side `Sometimes`: after clearing the flag and a subsequent view change, a write
  on N succeeds and `pxc_maint_mode` reads DISABLED (revert liveness observed).
- SUT-side `Reachable` (details: strict mode, protocol version) at sql_parse.cc:1904 (block
  branch) — exploration anchor + evidence the gate actually fired rather than the statement
  failing for another reason.

## Config / fault requirements

- **Debug build required** (`DBUG_EVALUATE_IF` compiled out with DBUG_OFF/NDEBUG server
  builds) — flag for the harness build matrix; sut-analysis §12 already recommends a
  debug-image variant.
- Network faults (default-on) to force view changes; no node termination or clock faults
  needed.
- `pxc_strict_mode` per-node control (dynamic GLOBAL) to cover
  ENFORCING/MASTER/PERMISSIVE/DISABLED matrix — including the mixed case where a DISABLED
  node keeps accepting writes while ENFORCING peers block (documented intended behavior;
  the gate is node-local by design, itself a config-as-skew observation).

## Open Questions

None — both resolved (see Investigation Log). Design consequences:

- All SQL execution funnels (client dispatch, prepared statements, stored programs, event
  scheduler, srv_session/X plugin) route through `mysql_execute_command`, so the workload
  can restrict itself to SQL-level probing. Known escapes, all by design: sessions with
  `wsrep_on = OFF` skip the entire `WSREP(thd)` block (gate AND readiness check);
  statements without `CF_CHANGES_DATA`; wsrep appliers (:1876-1878).
- `view.protocol_version() < V4` IS reachable in a release build: every NON-PRIMARY view
  carries -1 (see no-spurious-multi-major-detection's evidence file for the full chain).
  This gives the maint-mode forcing/revert state machine — including the operator-drain
  hijack (hazard 1) — a release-build, network-faults-only trigger. The debug flag remains
  required only for the *write-blocking* half of the property: during non-Primary the
  wsrep-readiness gate (sql_parse.cc:3787-3808) rejects writes before the rolling-upgrade
  gate is consulted, so only the DBUG injection exercises the gate on a node that is
  otherwise Synced/Primary and accepting statements.

### Investigation Log

#### Do non-dispatch write entry points pass through the gate?

(2026-09-10, open-questions pass)

- Examined: gate call site sql/sql_parse.cc:3789-3812 (inside the `WSREP(thd)` block of
  `mysql_execute_command`, right after the readiness gate); sql/sp_instr.cc:1105
  (`exec_core` → `mysql_execute_command` — stored programs and, via sp_head, event bodies);
  plugin/x/src/sql_data_context.cc:198,:303,:566-609 (X plugin executes COM_QUERY /
  COM_STMT_* via `command_service_run_command` → srv_session command service →
  `dispatch_command` → `dispatch_sql_command` → `mysql_execute_command`);
  sql/srv_session.cc:1188 (same dispatch expectations).
- Found: every SQL-statement execution path funnels through `mysql_execute_command`, so the
  gate covers dispatched queries, prepared-statement execution, stored
  procedures/functions/triggers, event-scheduler event bodies, and srv_session/X-plugin
  clients. Bypasses: (a) `WSREP(thd)` false — i.e. a session with `wsrep_on = OFF`
  (privileged escape hatch, skips the readiness gate too); (b) commands not flagged
  `CF_CHANGES_DATA` (:1882) — the same flag set used by the rest of the wsrep gating; (c)
  applier threads, by design (:1876-1878).
- Not found: any writer-path entry point that reaches the storage layer without passing
  `mysql_execute_command` (other than internal/applier writes, which are exempt by design).
- Conclusion: resolved — SQL-level probing suffices; optionally include one
  `wsrep_on = OFF` probe to document the intended escape, not as a gate bug.

#### Is view.protocol_version() < V4 transiently reachable in a homogeneous cluster?

(2026-09-10, open-questions pass)

- Examined: gcs/src/gcs_state_msg.hpp:78-85 (`GCS_QUORUM_NON_PRIMARY` → appl_proto_ver
  -1), gcs/src/gcs_group.cpp:2295-2317 (non-prim cchange copies quorum's -1),
  galera/src/galera_info.cpp:39, galera/src/replicator_smm.cpp:2725-2760
  (`process_non_prim_conf_change` → `submit_view_info`), wsrep-lib
  src/server_state.cpp:1125-1156 (`on_view` calls `log_view` for non_primary too),
  wsrep_provider_v26.cpp:357-367, include/service_wsrep.h:31-40 (signed enum),
  sql/wsrep_server_service.cc:189-231.
- Found: yes — deterministically, on every non-Primary view, `view.protocol_version()` is
  -1 and `log_view` runs, forcing MAINTENANCE under default ENFORCING (chain and
  consequences documented in no-spurious-multi-major-detection's evidence file). During
  non-Primary the readiness gate masks the write-block half; the maint-forcing/revert and
  forced-flag hijack are the release-observable effects.
- Conclusion: resolved — the maint-mode state-machine half of this property merges with the
  release-build variant (no debug flag needed, network faults suffice); the DBUG build
  remains required only for exercising the write block on an otherwise-healthy node.
