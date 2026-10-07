# SESSION

Deferred issues found during triage. Fix later unless they block work.

## From run 1ef6812708256c2f81e103ba93c0d048-63-5 (2026-10-06)

- **`wl_witness` mismatch details cannot show which rows are missing.** The table-content
  oracle gives only row count and hash per node. Add the `wid` set difference to the details.
- **`[coverage] a node died in a boot whose state transfer had failed` never fires**, though
  the run has many SST-failure gu_aborts. The 2026-09-28 gate skips any boot that ends in
  gu_abort, and SST failures end in gu_abort. Check if this claim can ever pass.
- `[prod] cluster commit progress never freezes while every node reports Synced` emits only for node1 in run 17eb4031…-63-5. `healthy_claim` (`antithesis/workload/pxcwl/probe.py:438-443`) seems false for node2/node3 even when their probes succeed. Check which term fails (wsrep_ready, cluster_size, desync_count).
- `antithesis/scratchbook/properties/first-committer-wins-loser-leaves-no-trace.md:92-93,117` says CONN_FAIL is returned only before ordering. Run 17eb4031…-63-5 disproves it: a node that goes non-Primary after send gets CONN_FAIL for a writeset the rest of the cluster orders.
- The "write outcome unknown" coverage events carry no node, so triage cannot assign them to a node. Add `node` to their details.
- `snouty runs events` for the gtid property returned only passing examples in run 17eb4031…-63-5, so other counterexamples cannot be sampled without query-logs.

## From run fdb9d32c8a35933b692e455477bb0e9f-63-5 (2026-10-06)

- **InnoDB wsrep XID can lag the committed data.** In run fdb9d32c…-63-5, node2 restarted after a kill (grastate seqno -1), recovered position 1276, and IST re-applied a `wl_witness` row it already had (1062, then `sql/transaction_info.h:472`, vtime 110-120). The grastate fix does not cover this path. Candidate for T7 `grastate-se-checkpoint-agreement`.
- `snouty runs events` timed out (120 s) for the primary-component property, so it could not sample more counterexamples.
- **Terminal check cannot do the documented operator bootstrap (input_hash -1690489723058256352, vtime 682).** The injector sent `stop` (SIGTERM) to node1 and node2 at 71.34 and 71.92. node3 was killed at 74.95. node1 and node2 left from a NON_PRIM view, so Galera deleted their `gvwstate.dat` (`gcomm/src/pc.cpp:262`), and all three grastate files show `safe_to_bootstrap: 0`, seqno 600. node3 restored view 3 and waits for ever (`pc.wait_restored_prim_timeout` default `PT0S`), because pc.recovery needs the old member UUIDs (`gcomm/src/pc_proto.cpp:295-340`). Galera documents this case as operator work. `checks.bootstrap_if_no_primary` needs a node that answers SQL, and none does. A fix needs a workload-to-supervisor bootstrap channel. Decide before building it.
- **The injector logs a `stop` fault when the stop completes, not when it starts.** In run fdb9d32c…-63-5 the SIGTERMs arrived at 71.34/71.92 (`container_stop_requested`), during the unpaused window 70.91-81.47. The fault lines are at 89.07/89.15, after mysqld exited. Use `container_stop_requested` for stop timing, not the fault line.
- **Frequency of the restored-view deadlock is unknown.** The property has 143 counterexamples. The Logs Explorer gave 100 for "failing, not preceded by `inconsistent to restored view`" but 0 for "failing" alone, so neither number can be trusted. `snouty runs events` cannot filter on assertion details. The warning itself is common (more than 776 moments in a 999-event sample, all three nodes), so it is not a sufficient signature by itself. Add a text log line in the reconvergence check (`[verify] reconvergence: no node answers SQL`, added 2026-10-07, unbuilt) so that `snouty runs events` can count these cases.

## From run 69449aa58ca0c6eb951ad94a09d897a2-63-5 (12h, images @ d902550b0, 2026-10-06)

- **Stale socket lock deaths are in this run.** The images predate `86e244a08`. Do not count `MY-010119 [Server]` / `MY-010268 [Server]` deaths, or the `View callback failed` deaths that follow MY-010259.
- **gu_abort property names keep the node UUID.** "failed to close gcomm backend connection: element X not found" splits one cause into 5 properties. Normalize the UUID.
- **Crash dumps arrive late or get lost.** `tail -F` (`entrypoint.sh:907`) copies the error log with a delay. The dump lands after the counterexample moment, and it is lost if the container stops at once (signal 6 deaths had no frames). Fetch with `snouty runs logs` and no vtime to stream to the end of the branch, or flush the error log before the supervisor exits.
- **DDL lever does not log its outcome.** `_run_ddl` (`workload/pxcwl/ddl.py`) emits no statement text or result. The `wl_scratch_ephemeral` schema mismatch cannot be decided. Emit a `pxc_ddl` event with statement, node, outcome and errno.
- `compare_table_set` (`checks.py:361`) reads `information_schema` without `wsrep_sync_wait`. Add each node's `wsrep_last_committed` and in-flight TOI processlist to the details.
- The sync-wait oracle does not record the reader's `wsrep_local_state`. A read on a JOINED node looks like a read on a Synced node.
- Workload ops do not log the SR fragment size, connection id or server trx id. Triage cannot map a `wid` to a Galera trx.
- The availability oracle cannot separate a stuck node from probes that wait on the shared `wl_probe` row lock. On timeout, add the send queue, the probe processlist state and InnoDB lock waits.
- The reconvergence wait logs nothing for about 600 s. Sample `wsrep_local_state_comment` per node periodically.
- `snouty runs events` returned HTTP 500 for every property tried in this run. Parallel `snouty runs logs` downloads got HTTP 429. Pass `--` before a negative input hash. `--begin-vtime` must come before the positional arguments.
- **Terminal verification cannot recover when no node accepts SQL.** `checks.bootstrap_if_no_primary` (`workload/pxcwl/checks.py:146-148`) needs a live SQL node. When all nodes loop on `pc.wait_prim_timeout`, the reconvergence check fails without an operator bootstrap.
- **The single-primary oracle mixes lagging fields from nodes polled at different times** (`workload/pxcwl/probe.py:285-320`). Record `wsrep_local_state`, `wsrep_cluster_conf_id` and `wsrep_cluster_state_uuid` per node.
- **The availability oracle has no server-side evidence when a probe write times out.** Capture `innodb_trx`, `metadata_locks` and the processlist on a 2013 timeout.

## Found 2026-10-07

- **`oracle-tests/test_start_position.py` failed 3 cases in another session's sandbox** (the two fall-through cases and the old-behaviour detection). The test passes on the exe.dev VM with the default `/tmp` and with a Claude scratchpad as `TMPDIR`. A fake `mysqld` without exec permission gives exactly these 3 failures, so a temp directory that does not allow exec is the likely cause, but this is not proven for that sandbox. The failure details now include `recover_stdio`, so the next failure shows the cause.

## From the Percona summary of runs fdb9d32c…-63-5 and b1e4971e…-63-5 (2026-10-07)

- **The 12h run fdb9d32c…-63-5 filled the VM disk at about vtime 212.** Cause found 2026-10-07 (input_hash -428787273264483603):
  - Each node created about 52 gcache pages of 32 MB between 01:46:09 and 01:46:37 and deleted 3. That is about 1.6 GB per node, 4.8 GB for the cluster. The last page request found 8,359,936 bytes free.
  - `bulk_write` (`workload/pxcwl/ops.py:497`) runs `REPLACE` on an existing row, so the writeset is an `Update_rows` event with the full before and after image. The failed action was 16,777,592 bytes (2 × 8 MiB). That is larger than the 16 MB gcache ring, which `BULK_MAX_BYTES` (`config.py:107`) is meant to prevent.
  - Galera deletes pages only from the oldest end (`gcache_page_store.cpp:145-152`). node1 freed 48 pages at once when it went Inconsistent at 01:46:37.35, so writesets that the node had not released pinned the pages. Inferred, not proven: the receive queue (flow-control limit 173 writesets) and slow appliers (InnoDB redo-log stall warnings `MY-014084` at 01:46:15 and 01:46:28) held them.
  - Binlogs are not the cause here: below 0.5 MB per node.
  - Not explained: at vtime 168 the workload filesystem had 7.58 GB free of 8.39 GB. gcache explains about 4.8 GB. The other ~2.8 GB is not measured, and it is not proven that the workload filesystem is the same pool as the nodes' disk.
- **No property catches a 1-of-3 Primary after an identity change** (run b1e4971e…-63-5, input_hash 7386397666427349546). The single-primary oracle needs two Primaries. Add a log scan that fails when a PRIM view keeps less than half of the previous PRIM members and the rest are "partitioned", not "left".
- **Terminal verification does not restart a live node that stays Inconsistent**, so that history always fails reconvergence.
- **The gu_abort cause key is the last `[ERROR]` line, not the first fatal one.** One root cause splits into several properties (`STATE EXCHANGE`, `MY-013132`, `failed to close gcomm`).
- **A crash with two failed asserts records only the first site** (12h vtime 120.00: `transaction_info.h:472` and `client_state.cpp:438`).
- **The supervisor replays earlier boots' error-log lines at the new boot's vtime.** A timeline built only by vtime is wrong. Use the mysqld timestamps.
- **`trx0trx.cc:2534` has no backtrace in either run**, so its owner is not known. Put the error-log `#N` frames into the death details, or get them with antithesis-debug at 2h input_hash -7265113343034222745, vtime 217.09.
- **Data-oracle details carry no resolve vtime, Galera seqno or trx id**, so a `wid` cannot be mapped to its commit moment. The journal calls the op `txn_witness`, but the `pxc_op` event calls it `txn_multi_statement`.
- **`snouty runs events` returns only the first 1000 events by vtime, with no paging**, so end-of-run failures (about vtime 683) cannot be sampled. It returns HTTP 500 on run fdb9d32c…-63-5.
- **Deaths that mysqld catches show `kind:"exit"` with `status:2`**, which looks like a clean exit. Use a kind such as `caught_signal`.
- **node2 failed to apply a rollback fragment of its own transaction, then left the cluster** (run fdb9d32c…-63-5, input_hash 7856742902588311048, `01:45:30.217Z`): `Failed to apply write set … flags: 20 (rollback | pa_unsafe)`, with node2's own UUID as source. No InnoDB error comes first. Check if this belongs with the T3 SR-rollback finding (`wsrep-lib/src/transaction.cpp:374`).
