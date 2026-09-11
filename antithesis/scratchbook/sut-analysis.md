---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
external_references:
  - path: https://docs.percona.com/percona-xtradb-cluster/8.4/
    why: Upstream product documentation (user-approved scope: repo + upstream docs)
  - path: https://galeracluster.com/library/documentation/
    why: Galera replication library documentation (user-approved scope: repo + upstream docs)
  - path: https://dev.mysql.com/doc/refman/8.4/en/
    why: MySQL 8.4 reference documentation (user-approved scope: repo + upstream docs)
---

# Percona XtraDB Cluster 8.4.10 — Synthesized SUT Analysis

Synthesis of a 12-agent discovery ensemble. Every guarantee below is a CLAIM to test, not a
verified fact. File:line references are against this repo at commit f9ecb3ebe8ff (branch 8.4);
`galera/...`, `gcs/...`, `gcomm/...`, `gcache/...`, `galerautils/...` paths are inside the
`percona-xtradb-cluster-galera` submodule (@13ff9ed6); `wsrep-lib/...` is the wsrep-lib
submodule (@23432d4). In-repo docs under `doc/source/` are 8.0-era and stale — the tree is
authoritative for behavior; docs.percona.com/8.4 for product claims (focus 4, 10).

## 2. Executive summary

PXC = Percona Server for MySQL 8.4.10-10 LTS (XtraDB/InnoDB) + Galera 4.27 replication
library (wsrep API 26) + XtraBackup-based SST tooling. "Synchronous multi-source
replication": every node accepts writes; a transaction is *certified* on all nodes at commit
(total-order broadcast + deterministic certification), then applied asynchronously —
"virtually synchronous", so the out-of-the-box default (`wsrep_sync_wait=0`,
sql/sys_vars.cc:8470-8472) permits stale reads despite the marketing headline "written to all
nodes simultaneously" (focus 10). Three build artifacts: `mysqld` (statically links
wsrep-lib), `libgalera_smm.so` (dlopen'd provider — a PXC fork of Galera with extra vtable
entries, ABI-incompatible with upstream), and `garbd` arbitrator (focus 1).

PXC's correctness story rests on: (a) identical total order of writesets on all nodes,
(b) bit-identical, deterministic certification on every node, (c) inconsistency *voting* that
ejects a node whose apply *errors* differ from the group (successful-but-different apply is
undetectable), (d) weighted quorum in the PC layer, and (e) recovery anchored in three small
state files (grastate.dat, gvwstate.dat, InnoDB wsrep XID) plus the gcache ring buffer.
Enforcement of nearly all internal invariants is `assert()` — compiled out under NDEBUG in
release builds (focus 11) — or `gu_abort()`, which suppresses core dumps and exits via SIGABRT,
which the shipped systemd units explicitly refuse to restart (focus 9, 11).

Highest-value Antithesis targets (consensus of the ensemble):

1. **Silent divergence via certification non-determinism and missing cert keys** — the
   dominant recurring bug class (pattern A/B, focus 6): DDL/FK statements whose applier MDL
   footprint exceeds their certification-key set; error/warning-text-dependent inconsistency
   votes; node-local hidden cert params (`cert.max_length`) with no cross-node agreement
   (focus 4, 11). Oracle must be an external cross-node data checksum — the SUT has no state
   hash and only votes on apply *errors* (focus 11 §2.4).
2. **Crash-recovery position machinery** — grastate.dat is rewritten in-place non-atomically
   with write failures ignored (focus 8, 11); a healthy running node keeps grastate seqno=-1
   and relies wholly on the InnoDB wsrep XID whose monotonicity assert is commented out
   (trx0sys.cc:481, focus 2); systemd recovery greps the error log with a pattern that does
   not match the actual log prefix (focus 12 §3); SE-checkpoint-lags-grastate produced a
   cluster-wide FC lockup (PXC-4845). Kill-anywhere + restart is directly Antithesis-shaped.
3. **State transfer (SST/IST) lifecycle** — the single most bug-dense area (169 commits in
   wsrep_sst_xtrabackup-v2.sh, focus 6): joiner "ready" emitted after 60s even if no listener
   bound, datadir wiped concurrently with transfer after the "no way back" point, a hidden
   second mysqld run on the joiner datadir every SST, orphan process trees on kill -9,
   unbounded waits on both sides, 10s hardcoded IST watchdog that aborts the node (focus 8,
   9, 12).
4. **Monitor/commit-order liveness** — one wedged applier seqno stalls the whole cluster via
   flow control (PXC-4844, MDEV-38843, PXC-4845, PXC-4173 family, focus 6 pattern C); the
   PXC-only `Wsrep_async_monitor` is young, regression-prone, sized by the wrong variable,
   and can `unireg_abort(1)` the node from ordinary replica concurrency (focus 3, 5, 12).
5. **Split-brain / bootstrap safety** — `safe_to_bootstrap` is set on *every* singleton
   primary view, so two partitions of size 1 can both persist it and both bootstrap
   (focus 2 §2.4); `pc.wait_restored_prim_timeout` default PT0S means a full-cluster restart
   can block forever before opening the port (focus 5 §2); an interrupted bootstrap shell
   script leaves `--wsrep-new-cluster` in the systemd manager environment (focus 12 §13).

Where Antithesis adds unique value over the existing 952-test MTR corpus: real/asymmetric
partitions, failure detection at real (not MTR-relaxed) timeouts, load + membership churn
simultaneously, crash-during-commit, disk faults, clock steps, `wsrep_sync_wait=0` and
`wsrep_applier_threads>1` (both production-realistic, both under-tested), and a continuous
whole-database cross-node consistency invariant that no existing test runs (focus 7).

## 3. Architecture and data flow (focus 1; corroborated 3, 4)

### 3.1 Components and boundaries

- **Boundary A — mysqld ↔ provider (dlopen + C vtable).** `wsrep_loader()`
  galera/src/wsrep_provider.cpp:1706; vtable `galera_str` :1644-1697 including PXC-only
  entries `galera_fetch_pfs_info`, `galera_rotate_gcache_key`, `galera_try_desync_and_pause`
  (:1677-1684) — **ABI not interchangeable with upstream libgalera**. Loading in
  wsrep-lib/src/wsrep_provider_v26.cpp:840-912; provider→server callbacks :867-883 (logger,
  connected, view, sst_request, encrypt, apply, sst_donate, synced, abort, pfs_instr, enc
  keys). Optional interfaces resolved by dlsym in `init_services()` :764-823 — **resolution
  failures degrade silently** ("provider does not support X").
- **Boundary B — wsrep-lib state machines.** `server_state`
  (s_disconnected→connected→joiner→initializing→initialized→joined→synced, plus
  donor/disconnecting; transitions wsrep-lib/src/server_state.cpp:745-1183) and
  `transaction` (s_executing/preparing/certifying/committing/committed/cert_failed/
  must_abort/aborting/aborted/must_replay/replaying/prepared). PXC subclasses in
  sql/wsrep_{server_state,client_service,high_priority_service,storage_service,
  server_service}.cc.
- **Boundary C — InnoDB ↔ wsrep** via include/service_wsrep.h: key extraction
  `ha_innobase::wsrep_append_keys` ha_innodb.cc:13116 (from write_row :10630, update_row
  :11474, delete_row :11565); BF-abort hton hook :5990 → `wsrep_innobase_kill_one_trx`
  :24385 → `wsrep_thd_bf_abort` :24447; one-aborter guard `wsrep_thd_set_wsrep_aborter`
  :24439; SE checkpoint `innobase_wsrep_set_checkpoint` :24521 → TRX_SYS page at
  `UNIV_PAGE_SIZE-3500`, magic 0x77737265 (storage/innobase/include/trx0sys.h:340-349).
- **Boundary D — SST shell scripts** (fork/exec + line-oriented pipe protocol, §3.6).
- **Boundary E — async replication into PXC**: `Wsrep_async_monitor`
  (sql/wsrep_async_monitor.h:27-54), created in sql/rpl_replica.cc:7285-7290 when
  `wsrep_use_async_monitor` (default ON, read-only); schedule at sql/log_event.cc:2686; skip
  at sql/rpl_gtid_execution.cc:401, log_event.cc:11237, sql_parse.cc:338; enter/leave from
  sql/wsrep_trans_observer.h:257,277,320,338,584-585. A second ordering structure layered on
  top of Galera's monitors — a deadlock-interaction source by construction (focus 1, 3, 6).

### 3.2 Local write path

- `do_command` bracket: `wsrep_before_command` sql_parse.cc:1647; readiness gate :1678
  (ER_UNKNOWN_COM_ERROR 1047); dispatch :1718; `wsrep_after_command` :1740. BF-aborted while
  idle → ER_LOCK_DEADLOCK :1656. `CF_SKIP_WSREP_CHECK` escape :1685. Other entry points:
  event scheduler sql/event_data_objects.cc:1033, srv_session.cc:915/:1184, COM_CHANGE_USER
  sql_parse.cc:2327/2383.
- Transaction hooks all in sql/wsrep_trans_observer.h (602 lines): wsrep_open :470,
  before_statement :449, after_row :143, run_commit_hook :165, before/after_prepare
  :226/:286, before_commit :305, ordered_commit :356, after_commit :380,
  before/after_rollback :401/:433, commit_empty :547.
- Certification keys: `wsrep_append_keys` → `transaction::append_key`
  (wsrep-lib/src/transaction.cpp:248) → provider append_key :256. Shard/table digest keys
  ha_innodb.cc:13261-13268. PK enforcement `wsrep_check_pk` (wsrep_trans_observer.h:95-107).
- Payload: binlog cache → `prepare_data_for_replication` (sql/wsrep_client_service.cc:75-83)
  → `wsrep_write_cache` (sql/wsrep_binlog.cc:243) chunked append_data. **Payload = raw
  row-binlog events, opaque to the provider** — certification sees only keys, never data.
- Commit placement (binlog 2PC, sql/handler.cc): :2568 run-hook check, :2573 async-monitor
  graph wait, :2580 **wsrep_before_prepare = replication + certification**, :2607
  ht->prepare, :2619 after_prepare, :2086 before_commit, :2173 after_commit. Binlog group
  commit sql/binlog.cc:8947-8986; ordered_commit via sql/rpl_commit_stage_manager.cc:123-124.
  **wsrep_ordered_commit is silently disabled unless `opt_log_replica_updates &&
  opt_binlog_order_commits`** (wsrep_trans_observer.h:364) — commit-order semantics are a
  function of two unrelated server options (focus 1, 3).
- `certify_commit` (wsrep-lib/src/transaction.cpp:1804): wait_for_replayers :1809;
  abort_or_interrupt :1813; s_certifying :1822; **unlocks the client mutex for the provider
  call :1823 (BF-abort race window)**; send_pending_rollback_events :1826 (failure → force
  abort :1843-1848); SR keys + pa_unsafe :1851-1858; provider().certify :1886; 12-branch
  status switch :1897-2010 (error_bf_abort→must_replay :1939; success-but-must_abort→replay
  :1916; literal in-code comment "Galera may return CONN_FAIL if trx BF aborted O_o"
  :1959-1961; error_fatal→emergency_shutdown :1996). Then before_commit :460 →
  commit_order_enter :525/:565; ordered_commit :587 → commit_order_leave :595; after_commit
  :626 → release :659.
- Provider `replicate()` (galera/src/replicator_smm.cpp:748): zero-level key :758-763; state
  < S_JOINED → CONN_FAIL :765 (NODE_FAIL if corrupt :780); gather :792; gcs schedule :803;
  **finalize(last_committed()) stamps last_seen_seqno :813**; unlock + replv :814-822
  (EAGAIN retry with 1ms usleep, unbounded); TrxHandleSlave with seqno_g :854; BF-in-GCS
  handling :895-913.
- `certify()` → cert_and_catch :3805 → cert :3773: verify_checksum :3787;
  enter_local_monitor_for_cert :3792 (local monitor serializes certification in seqno_l
  order); finish_cert :3707 asserts global_seqno == cert_.position()+1 :3717; append_trx
  :3720; gcache seqno_assign :3762. **`handle_local_monitor_interrupted` :3663: a committing
  trx returns BF_ABORT WITHOUT cancelling the local monitor (:3679-3682)** — replay must
  re-enter; if replay never runs (node leaving, SST_CANCELED) the local monitor may block
  permanently. Prime fault target (focus 1 open Q1).
- Certification `do_test` (galera/src/certification.cpp:416): version match :421; reject
  last_seen < initial_position_ or cert_interval > max_length_ :433-445; dependency seeding
  :449-462 (optimistic_pa); do_test_v3to6 :347 per-key conflict :394; **PXC deviation
  :499-503 — certifies regular + SR transactions against ongoing NBOs (upstream does not)**.
- Order gates (galera/src/replicator_smm.hpp): ApplyOrder::condition :694-698
  (last_left >= depends_seqno_); CommitOrder::condition :814-836 — **PXC modifies LOCAL_OOOC
  semantics (local→true, remote→strict) vs upstream `return is_local_`** (dead code with the
  forced NO_OOOC default, but a divergence trap); LocalOrder uses local_seqno :571.

### 3.3 Remote apply path

- Applier pool: `wsrep_create_appliers` (sql/wsrep_mysqld.cc:1300);
  `wsrep_replication_process` (sql/wsrep_thd.cc:42) → provider.run_applier → galera_recv →
  `async_recv` (replicator_smm.cpp:471): -ECANCELED → recv_IST + **`usleep(10000)` "hack:
  prevent fast looping" :494-496**; INCONSISTENCY_CODE → mark_corrupt_and_close +
  WSREP_FATAL :501-506; last applier can't exit :517-524; abnormal exit synthesizes a final
  view :556-563. `wsrep_slave_threads`/`wsrep_applier_threads` dynamic (sql/wsrep_var.cc:679).
- `GcsActionSource::dispatch` (galera/src/gcs_action_source.cpp:99): WRITESET→process_trx;
  EAGAIN→resend busy-loop :67; COMMIT_CUT (replicator_smm.cpp:2313); CCHANGE :2646;
  STATE_REQ (replicator_str.cpp:508); JOIN :3338; SYNC :3366; VOTE :2379. **Corrupt-node
  skip filter :168-172 drops everything except CCHANGE/VOTE/EAGAIN — deliberately incomplete
  per in-code comment :164-168** (focus 4 F-4).
- `process_trx`: **PXC-only drop-all after SST_CANCELED (replicator_smm.cpp:2241-2246)**;
  IST overlap :2250-2254; cert_and_catch :2256; both OK and FAIL(dummy) → apply_trx :2292
  (ordering); apply exception → mark_corrupt_and_close :2294-2302.
- `apply_trx` :574: apply_monitor_.enter :598; ts.apply → mysqld :624; on_inconsistency on
  ApplyException :636; set_trx_committed :649; report_last_committed :669.
- `apply_write_set` demux (wsrep-lib/src/server_state.cpp:276-464): rollback+starts → dummy
  :292; rollback with no streaming applier → dummy, **spurious rollback fragments expected**
  :301-317; starts+commits normal :333-344 (apply error → rollback + dummy :351-358); starts
  only → new streaming applier :366-377; middle fragment :379-410; commits only :411-454.
  **Missing streaming-applier context → WARNING + dummy writeset (:392-398, :433-440) —
  a silent-divergence path under membership churn** (focus 1, corroborates focus 4 SG-1.2).
- mysqld side: `Wsrep_applier_service::apply_write_set`
  (sql/wsrep_high_priority_service.cc:640) → `wsrep_apply_events`
  (sql/wsrep_applier.cc:140); TOI apply_toi :431; NBO apply_nbo_begin :706; replayer :1061.

### 3.4 gcomm/GCS stack

- Stack: GMCast (TCP mesh, segments, relay) ← EVS (gcomm/src/evs_proto.cpp, 5030 lines;
  totally-ordered multicast + failure detection) ← PC (quorum) ← GCS (fragmentation, flow
  control, state exchange, donor selection). Transport asio TCP/UDP; TLS via `ssl://`
  (gcomm/src/gmcast.cpp:155,202). The entire gcomm layer is **single-threaded under one
  global mutex** (AsioProtonet::mutex_, galerautils asio_protonet.cpp:45-55) — a slow
  handler blocks heartbeats → spurious evictions under CPU starvation/slow disk (focus 3).
- Ports: 3306 SQL; 4567 gcomm (galera_common.hpp:34-35); 4568 IST (base_port+1,
  galera/src/ist.cpp:227-251); 4444 SST (scripts); 9200 clustercheck.
- GCS message types (gcs/src/gcs_msg_type.hpp:16-30): ACTION, LAST, COMPONENT, STATE_UUID,
  STATE_MSG, JOIN, SYNC, FLOW, VOTE, CAUSAL.
- Quorum (gcomm/src/pc_proto.cpp): have_quorum :555-568 weighted; have_split_brain :578-591
  (exact-tie case); ignore_sb path :612-634; states :382-452. GCS-level:
  `group_post_state_exchange` (gcs/src/gcs_group.cpp:429-570); local act_id ahead of quorum
  → node declared INCONSISTENT :478-489.
- Timer defaults (gcomm/src/defaults.cpp): evs.suspect PT5S, inactive PT15S, check PT0.5S,
  keepalive PT1S, join_retrans PT1S, install_timeout = inactive/2 = 7.5s,
  **evs.max_install_timeouts = 3 (defaults.cpp:53) — the vendored doc says 1
  (doc/source/wsrep-provider-index.rst:249); code wins: default is 3; note the doc/code
  discrepancy** (resolved by synthesis; MTR additionally overrides it to 1 —
  mysql-test-run.pl:4349), delay_margin PT1S, delayed_keep PT30S, auto_evict 0 (disabled),
  view_forget PT24H; pc.announce PT3S, wait_prim PT30S, **pc.wait_restored_prim_timeout
  PT0S = wait FOREVER (gcomm/src/pc.cpp:104-115)**, linger PT20S, recovery true, weight 1;
  gmcast.time_wait PT5S, peer_timeout PT3S. **PXC diverges from upstream: evs.send_window 10
  (upstream 4), evs.user_send_window 4 (upstream 2)** (focus 1, 5, 11).
- Flow control: recv_q sized from `gu_avphys_bytes()/4` (gcs/src/gcs.cpp:402-418) —
  **host-memory-dependent, cgroup-blind in containers**; hysteresis stop/cont :543-653; send
  monitor gcs_sm concurrency=1 (gcs/src/gcs_sm.hpp:20-23); **gcs_sm_grab bypasses FC for
  ROLLBACK fragments** (replicator_smm.cpp:719-725, deadlock avoidance). PXC fc_limit = 100
  (upstream 16), interval scaled /sqrt(non-arbitrator members), clamped to fifo max
  (gcs.cpp:1034-1058; gcs/src/gcs_params.cpp:32-47).

### 3.5 SST/IST

- Trigger: sst_request_cb → prepare_for_sst → `wsrep_sst_prepare` (sql/wsrep_sst.cc:880):
  ist_only short-circuit :887-901; address resolution :904-943; sst_prepare_other :796
  builds the CLI :819-832; joiner thread blocks on condvar :856-862 (no timeout); request =
  `"method\0address"` :968-971.
- Pipe protocol (sst_joiner_thread :599): line 1 `ready <addr>` :636; line 2 `<uuid>:<seqno>`
  :697 via sst_scan_uuid_seqno :507 (V6 donor-side metadata NOT implemented — focus 4 F-2,
  wsrep_mysqld.cc:125-131); cancellation race documented in-code :700-705. Donor symmetric
  :1560-1622; `wsrep_sst_donate` :1690.
- Galera joiner `request_state_transfer` (galera/src/replicator_str.cpp:1117): PXC
  conditionally marks grastate unsafe only when SST actually needed :1142-1161 (PXC-4631);
  sst_state_=SST_WAIT before send :1167; **releases sst_mutex_ before send_state_request
  :1169-1180 (documented donor-death-hang fix; residual race window)**; S_JOINING :1202;
  gcache reset if history differs :1221-1227; PXC sst_seqno_ forward adjust :1269-1276;
  SST_CANCELED → -ECANCELED :1294-1303; UUID mismatch → SST_FAILED "restart required"
  :1305-1315.
- Donor `process_state_req` :508: **drains apply+commit monitors to donor_seq :528-531
  (stalls all applying on the donor)**; S_DONOR :533; IST path gcache seqno_lock, NotFound →
  full_sst :557-597; donate SST first :604-607; run_ist_senders :612-625.
- IST protocol (galera/src/ist_proto.hpp): T_HANDSHAKE/RESPONSE/CTRL/TRX/SKIP :62-67;
  **version not negotiated — mismatch throws EPROTO :345-351** (rolling upgrade =
  fail-fast). Receiver ist.cpp:108; PXC interrupted_ flag :145; recv_IST
  (replicator_str.cpp:1632); cert-index preload :673-723, :1949-1967.
- grastate fields galera/src/saved_state.cpp:101-127; restore_saved_state :246-249 used at
  replicator_str.cpp:1288. gcache default size 128M (gcache/src/gcache_params.cpp:9-14).

### 3.6 TOI / NBO / RSU

`wsrep_to_isolation_begin` (sql/wsrep_mysqld.cc:2993): TOI `wsrep_TOI_begin` :2473 →
enter_toi_local :2558; failure path :2403 → leave_toi_local :2415; end :3185. RSU :2918
(desync + local DDL — deliberate temporary schema divergence). NBO 2-phase :2659/:2826;
apply_nbo_begin sql/wsrep_high_priority_service.cc:706 (spawns dedicated NBO applier; debug
syncs :929,:944); do_test_nbo certification.cpp:778. `wsrep_to_isolation` counter invariant
maintained across 3 functions :2877-2878. Default `wsrep_OSU_method=TOI`
(sys_vars.cc:8484-8489); NBO is tech preview — **SST fails while any NBO is in flight**
(doc/source/features/nbo.rst:18; enforced at replicator_str.cpp:874-882 joiner nulls the SST
request, and gcs donor returns -EAGAIN :704-708) (focus 1, 8, 10).

### 3.7 Streaming replication (SR)

Per-row `streaming_step` (wsrep-lib/src/transaction.cpp:1475) → certify_fragment :1530:
unlocks :1546; fragment **written to stable storage before certify** :1618-1647; certify
:1667. Tables mysql.wsrep_streaming_log / wsrep_cluster / wsrep_cluster_members /
wsrep_cluster_member_history (sql/wsrep_schema.cc:40-74) via Wsrep_storage_service on a
separate THD (sql/wsrep_storage_service.cc:63-68). streaming_rollback :2036; remote
rollback_fragment (server_state.cpp:322); rollbacker removes fragments before rollback
(sql/wsrep_thd.cc:178-199). Off by default (`wsrep_trx_fragment_size=0`).

### 3.8 Startup/shutdown/runtime reconfig

- sql/mysqld.cc: `wsrep_init_server` :8871 (singleton + PFS registration
  wsrep_mysqld.cc:1037-1100 — **swallows std::exception and returns success**, focus 11
  §4.3); wsrep_init :8893; **SST-first runs before InnoDB init :8910-8913**;
  `--wsrep-recover` :10694 prints the SE checkpoint. `wsrep_init_startup`
  (wsrep_mysqld.cc:1286-1331): start_replication → rollbacker → appliers → wait
  s_initializing/s_joiner; keyring reload after SST :1327-1330. `wsrep_new_cluster` filtered
  from argv :1425. Shutdown :1367/:1396; `wsrep_wait_appliers_close` = `while(true) sleep(1)`
  unbounded (mysqld.cc:12619-12638, focus 5).
- Runtime reconfig (sql/wsrep_var.cc): `wsrep_provider` :392 — **live unload/reload of the
  .so; drops LOCK_global_system_variables :412 with an admitted concurrent-updater race in
  the comment :409-411** (but the sysvar is READ_ONLY in PXC, sys_vars.cc:8243 — the code
  path may be dead; focus 12 §11); `wsrep_provider_options` :470 (arbitrary option string
  into the running provider — includes pc.bootstrap, pc.ignore_sb, gmcast.isolate, dbug sync
  points); `wsrep_cluster_address` :550 live rejoin; `wsrep_slave_threads` :679;
  `wsrep_desync` :745. SST vars validated by char-class filters (wsrep_sst.cc:95-109) then
  interpolated into a shell CLI :819/:1591 (see CVE section). Encryption options
  string-rewritten into the provider blob by naive substring parsing
  (wsrep_mysqld.cc:1117-1163).

### 3.9 Health/status surface

`wsrep_ready` gate sql_parse.cc:1684; `wsrep_notify_cmd` shell run synchronously on every
view change (sql/wsrep_notify.cc:22-59); wsrep::reporter JSON file; mysql.wsrep_cluster_members
refreshed per view (wsrep_schema.cc:585-599); **`wsrep_node_isolation_mode_set_v1`
(galera/src/wsrep_provider.cpp:1909-1918) is an in-process self-partition fault injector**
usable by the harness (focus 1).

### 3.10 PXC-vs-upstream divergences (#ifdef PXC)

1. async_recv exit adds SST_CANCELED (replicator_smm.cpp:532-541). 2. process_trx drops
writesets post-SST-cancel :2238-2246. 3. CommitOrder LOCAL_OOOC semantics changed
(replicator_smm.hpp:827-833). 4. sst_mutex_ restructure + conditional mark_unsafe
(replicator_str.cpp:1131-1203). 5. sst_seqno_ forward adjust :1242-1277. 6. NBO cert for
regular/SR trx (certification.cpp:490-503). 7. Larger EVS send windows. 8.
Wsrep_async_monitor (new subsystem). 9. ordered_commit gating (wsrep_trans_observer.h:
356-373). 10. Extra vtable entries = ABI fork. 11. pxc_strict_mode scattered. #ifdef PXC
density in safety-critical files: replicator_str.cpp 30, replicator_smm.cpp 26, gcs.cpp 17,
gcs_group.cpp 15, ist.cpp 8, saved_state.cpp 7, certification.cpp 4 (focus 1, 4 F-5).

## 4. State management and persistence (focus 2; corroborated 8, 11, 12)

### 4.1 State inventory

| Store | Location | Writer | Durability | Recovery |
|---|---|---|---|---|
| grastate.dat (UUID:seqno, safe_to_bootstrap) | datadir | galera::SavedState saved_state.cpp:364 | fwrite+fflush+fsync, **in-place rewrite ≤256B, NO temp+rename; failures = warnings, write_file returns void** | saved_state.cpp:53-177 |
| gvwstate.dat (own UUID + last PRIM view) | base_dir | ViewState::write_file gcomm/src/view.cpp:356 | temp+fsync+atomic rename (:404); no parent-dir fsync | view.cpp:411, pc.cpp:299-316; deleted on graceful close (pc.cpp:262) |
| galera.cache ring buffer + preamble | gcache.dir (= datadir by default) | RingBuffer gcache/src/gcache_rb_store.cpp | mmap; only preamble writes + dtor msync'd (:176-178); payload never msync'd during operation | open_preamble/recover/scan :798,:1426,:1134 |
| gcache.page.NNNNNN overflow pages | gcache.dir | PageStore gcache/src/gcache_page_store.cpp | mmap | **NOT recovered at all** (count_=0 :264; no dir scan) |
| InnoDB wsrep XID | TRX_SYS header, UNIV_PAGE_SIZE-3500 (trx0sys.h:340-349) | trx_sys_update_wsrep_checkpoint trx0sys.cc:529 | redo-logged mtr, same mtr as undo serialization (trx0trx.cc:1761-1791) | trx_sys_read_wsrep_checkpoint :571; wsrep_recover() wsrep_mysqld.cc:1349 |
| mysql.wsrep_cluster{,_members} | InnoDB | Wsrep_schema::store_view wsrep_schema.cc:580 | InnoDB trx | restore_view :682 |
| SR fragments mysql.wsrep_streaming_log | InnoDB | append_fragment wsrep_schema.cc:811 | InnoDB trx | recover_sr_transactions :1122 |
| Cert index + trx map + deps | memory only | certification.cpp | none | IST cert-index preload |
| GCS group state | memory only | gcs_group.cpp | none | state exchange per view |

### 4.2 grastate/seqno mechanics

- **unsafe_ counter**: >0 forces on-disk seqno -1 (saved_state.cpp:258-275);
  scripts/mysqld_safe.sh:273-279 skips `--wsrep-recover` when seqno != -1. **PXC-only
  early-out in SavedState::set (:216-228) skips the write when values unchanged** —
  written_uuid_ can drift vs mark_safe expectations (:289), and a previously *failed* write
  is never retried (focus 2, 8 §9). Invariant claim: on-disk == (uuid_,seqno_) whenever
  unsafe_()==0.
- **NBO unbalanced mark_unsafe**: apply_trx marks unsafe twice for NBO-start
  (replicator_smm.cpp:603 nbo_start, :619 is_toi — nbo_start requires F_ISOLATION,
  trx_handle.hpp:115-120); one mark_safe :663; balancing decrement in to_isolation_end
  :1971. **Property: unsafe_ returns to 0 after every NBO completes/aborts/partitions
  mid-NBO; a leak = permanent forced SST on every restart** (focus 2 §2.2).
- mark_corrupt(remove_state_file) saved_state.cpp:301-325, unlink :345, gated by
  `repl.force_sst_after_inconsistency` (**default no** — galera/src/replicator_smm_params
  params.cpp:50; PXC-5208 behavior ships OFF). Callers replicator_smm.hpp:387-405,
  replicator_str.cpp:1609,:1714.
- **safe_to_bootstrap_ = (memb_num==1)** — replicator_smm.cpp:3245, set on EVERY primary
  conf change; enforced only at connect() :411-419. **Two singleton partitions can both
  persist safe_to_bootstrap:1 → both bootstrap after restart → split brain.** Antithesis
  recipe: partition a 2-of-N remainder into singletons, kill both, restart both (focus 2
  §2.4).
- `wsrep_recover` reads the SE checkpoint only (wsrep_mysqld.cc:1349-1364); fed via
  `--wsrep_start_position` → wsrep_var.cc:255-290 → replicator_smm.cpp:266-286: the SE
  checkpoint is trusted only when grastate UUID matches AND grastate seqno==-1. A stale
  non-(-1) grastate seqno (e.g. the shift_to_CLOSED write :315 during a crashing shutdown) →
  mysqld_safe skips recovery, node starts at grastate seqno possibly ≠ InnoDB checkpoint.
  **Property: at process start, grastate.seqno == -1 || == SE_checkpoint.seqno** (focus 2
  §2.5; PXC-4845 is the realized bug: grastate ahead of SE checkpoint → joiner IST starts
  from lower SE seqno, detects gap, skips IST but keeps running with monitors uninitialized
  (last_left == -1) → queued writesets block forever → cluster-wide FC stall; fix =
  shutdown; commit text: "There is no good solution for storing wsrep checkpoints in SE").
- **Post-transfer blanking**: replicator_str.cpp:1423-1438 writes seqno UNDEFINED +
  mark_safe *before* IST ("if node gets killed during IST, it may recover to incorrect
  position"); :1546-1560 resets to -1 again after success. **A healthy running node has
  grastate seqno -1 at all times; recovery relies entirely on the InnoDB XID** (focus 2 §4.5).

### 4.3 gcache mechanics

- Preamble carries seqno_min/max/offset only on graceful close (write_preamble(synced)
  gcache_rb_store.cpp:745-762; synced=true only from close_preamble :1053-1056 in the dtor).
  After ungraceful stop: offset=-1 → linear-probe scan() path (:1332-1361); preamble-seqno
  sanity check :1104-1116 SKIPPED; remaining guard :1118-1130 has epsilon ≈ 5.6M seqnos for
  a 128MB cache. `synced` is parsed (:833) but never used in decisions (:1024).
  **Corruption checks are weakest exactly in the crash case** (focus 2 §3.1; PXC-5209 is the
  realized bug: BH_test never checked seqno_g → corrupt galera.cache → bogus pointer free or
  ~10^12 seqno in seqno2ptr_ → OOM; tests pxc_gcache_corrupt_{size,seqno}.test byte-patch the
  cache via Perl — directly Antithesis-shaped, focus 6 §F).
- scan() warns-only on a truncated last segment (:1313-1316); mapping exception → clear map,
  full silent reset (:1279-1297, :1441-1457) → forced SST. Seqno collision → discard BOTH
  buffers (:1195-1265). Property: post-recovery advertised cached seqnos form a gapless
  suffix byte-identical to the donor's.
- **PageStore never recovered**; spilled writesets vanish from seqno2ptr; orphan
  gcache.page.* files accumulate (O_TRUNC commented out, galerautils gu_fdesc.cpp:37).
  Property: page-file count/bytes bounded across restarts (focus 2 §3.3).
- PXC-only `gcache.freeze_purge_at_seqno` (GCache.hpp:261-265): blocks discard
  (GCache_memops.cpp:55-58; gcache_rb_store.cpp:200-204,288-296) → malloc falls through to
  the page store (GCache_memops.cpp:117-119) → unbounded disk growth; no auto-unfreeze found
  (focus 2 §3.4).
- gcache/grastate/gvwstate preserved across SST (wsrep_sst_xtrabackup-v2.sh:836 cpat) —
  old-history gcache + new-history data; obvious cases handled
  (replicator_str.cpp:1221-1227, :1337-1343); the hole = same UUID + recovered-but-truncated
  gcache (focus 2 §3.5).

### 4.4 InnoDB wsrep XID

- **Monotonicity assert COMMENTED OUT** — wsrep_xid_sanity_check trx0sys.cc:406-493, assert
  at :481-482; the comment enumerates 4 known legit violations (atomic DDL double-persist,
  TOI INSERT...SELECT, NBO — "Reserved for NBO end, but never used. Yes, this is a bug.
  TODO.", non-group-commit paths SR / log_replica_updates=OFF). Property: checkpoint seqno
  never decreases; after recovery ≥ every acked commit (focus 2 §5.1).
- should_store_recovery_xid (:496-527) is recovery-only max-keeping; if the highest-seqno
  trx rolled back, the checkpoint over-reports (focus 2 §5.2).
- Silent no-ops: innobase_wsrep_set_checkpoint returns 0 in srv_read_only_mode
  (ha_innodb.cc:24523); !srv_sys_tablespaces_open → get returns undefined GTID
  (sql/wsrep_xid.cc:133-144,182-193). **`wsrep_verify_SE_checkpoint()` is an EMPTY stub**
  (wsrep_mysqld.cc:789-792; sole call site wsrep_sst.cc:310) — the post-SST
  SE-position-vs-group-position check is a no-op. Direct property: after SST, InnoDB wsrep
  XID == group GTID reported by the joiner (focus 2 §5.3, focus 4 F-1).
- The real cross-check lives in wsrep-lib: server_state::sst_received
  (server_state.cpp:833-870) throws if view.state_id != gtid, but **skips ALL sanity checks
  if the recovered view is undefined (:855, :879-887)** — an empty mysql.wsrep_cluster table
  makes the node accept any GTID (focus 2 §5.4).
- View storage and SE checkpoint are two separate transactions (log_view
  sql/wsrep_server_service.cc:245-296; store_view commit :264-276 then set_SE_checkpoint
  :294). Crash between them → wsrep_cluster ahead of trx-sys XID → sst_received throws on a
  later rejoin. The guarding assert :296 is debug-only (focus 2 §5.5). Historic assert
  PXC-4168 (commit 4deaf713649) covered exactly this equality.
- **XID on-disk format churn (PXC-5286)**: wsrep_xid_init wrote a V1 marker with
  int8store(LE) while the reader used memcpy(host order) — coincidentally correct on x86-64;
  8.4.10 adopts a versioned V2 prefix, accepts V1-V5; first-ever unit test
  unittest/gunit/wsrep_xid-t.cc (focus 6 §G).

### 4.5 SST vs IST decision state

- state_transfer_required (replicator_str.cpp:51-75); prepare_for_IST (:777-857) — UUID
  mismatch → last_applied=-1 → full SST. prepare_state_request (:860-947):
  cert_.nbo_size()!=0 → NULLs the SST request (:881-882); donor can't IST (-EAGAIN
  :640-644 or -ENODATA) → joiner abort() (:1004-1011). **NBO in flight during a join =
  deterministic joiner-abort path.** catch(...) → abort() (:936-946) (focus 2 §4.1).
- **Donor selection uses a stale gcache low-water snapshot**: the state message carries
  cached=gcache_seqno_min at state-exchange time (gcs_group.cpp:2386); group_find_ist_donor
  (:1789-1845) applies safety_gap = min(range>>7, 1MiB); **PXC zeroes the safety gap for
  IST-only requests (:1817)**. The donor purges between selection and service;
  process_state_req re-validates via gcache_.seqno_lock → NotFound → full_sst
  (replicator_str.cpp:586-597); an IST-only request → -ENODATA → joiner abort().
  **Control/data-plane race: load the donor between state exchange and IST start** (focus 2
  §4.2; realized upstream as MDEV-36621 — seqno_release freed locked buffers → "IST didn't
  contain all write sets").
- Second stale cursor: cc_lowest_trx_seqno_ (record_cc_seqnos
  replicator_smm.cpp:2550-2563), locked for cert-preload (replicator_str.cpp:673-696);
  purged → -ENOMSG → joiner "State transfer request failed unrecoverably" → abort().
  **gcache too small ⇒ joiner suicide, not fallback** (focus 2 §4.3, focus 8 §6).
- sst_received error codes (replicator_str.cpp:78-202): -ECANCELED → deferred graceful
  shutdown; -EAGAIN → restore_saved_state() then abort(); -EPIPE → abort() without restore.
  restore_saved_state restores FIRST-constructor-call values (static first_time_,
  saved_state.cpp:21,:171-176) — provider reload would make the restored state arbitrarily
  old. SST script side: SAFE_EXIT_CODE_OVERRIDE=EAGAIN until the "no way back" point
  (wsrep_sst_xtrabackup-v2.sh:2380-2382); a crash between clearing the override and the
  first rm (:2390) = safe grastate + partially deleted datadir (focus 2 §4.4, focus 8 §1).

### 4.6 SR fragment persistence

certify_fragment ordering (transaction.cpp:1530-1740): append_fragment with seqno-undefined
(:1651) → [crash point] → provider certify (:1664) → [crash point] → update meta + storage
commit (:1680-1690). **Crash between certify and commit: the local row's seqno IS NULL →
recover_sr_transactions DELETES it (wsrep_schema.cc:1212-1218) while every other node has
the certified fragment.** Property: per (node_uuid,trx_id) fragment-seqno sets identical on
all nodes after recovery, or the txn rolled back everywhere (focus 2 §6). Orphan cleanup
close_orphaned_sr_transactions (server_state.cpp:1567-1678) keys off
equal_consecutive_views (:1587); rolls back even if adopt fails ("leaving stale entries ...
removed manually" :1645-1648). A `#if 0`'d SR double-commit assert in wsrep-lib
transaction.cpp:568-582 implies a crash window orphaning wsrep_streaming_log rows (focus 12).

### 4.7 gvwstate.dat / PC recovery

Written on every primary view when pc.recovery=true (pc.cpp:22-33); **deleted on graceful
close** (pc.cpp:262). On restart the UUID incarnation is bumped (pc.cpp:301-305). PXC strict
parsing: unrecognized token → ViewParseError (view.cpp:227-230,:257-261) → treated as no
saved state (:435-439). Duplicate UUID in the GMCast handshake → unlink gvwstate +
gu_throw_fatal (gcomm/src/gmcast_proto.cpp:158-172,:300-320) — reachable by cloning a
datadir onto two hosts (focus 2 §8).

## 5. Concurrency model (focus 3; corroborated 1, 5, 11)

### 5.1 Threads

Client/session threads (client_state mutex == THD::LOCK_wsrep_thd, sql_class.cc:769-774);
applier pool (PULL model — each applier independently calls as_->process(),
replicator_smm.cpp:490; ordering comes only from the monitors); a single rollbacker (global
wsrep_rollback_queue, wsrep_thd.cc:252-306); the gcomm event loop (single thread, all
handlers + sends serialize on one AsioProtonet::mutex_); the GCS service thread
(galera_service_thd.hpp:80; its set_last_applied error path is literally "@todo figure out
what to do", galera_service_thd.cpp:69); IST receiver + SocketWatchdog; MySQL async
replication coordinator + workers with Wsrep_async_monitor.

### 5.2 Monitors

One Monitor<C> template ×3 (replicator_smm.cpp:176-185): LocalOrder (strict
last_left+1==seqno), ApplyOrder ((local && !toi) || last_left>=depends_seqno), CommitOrder —
default NO_OOOC forced (from_string throws for other values, replicator_smm.hpp:772-790;
rejected at runtime params.cpp:184-189). **65536-slot ring indexed seqno&0xFFFF
(galerautils monitor.hpp:51-52), single mutex + per-slot condvars.** Known hazards:

- **Ring overflow**: would_block when seqno-last_left >= 65536 (monitor.hpp:339-343);
  pre_enter blocks; self_cancel logs "Deadlock is very likely" and loops (:242-253); even
  the "read" APIs state() and interrupt() block (:550-560, :280-283). Target: stall one
  applier while driving writes (focus 3 §1.1).
- wake_up_next sets S_APPLYING before signalling (documented race workaround,
  monitor.hpp:471-488); interrupt() only cancels S_IDLE/S_WAITING → a BF abort against a
  running entrant is silently dropped → WSREP_NOT_ALLOWED fallback
  (replicator_smm.cpp:1026-1034) (focus 3 §1.3).
- **trx.unlock() across blocking monitor enters** — commit_order_enter_local :1362-1366,
  enter_local_monitor_for_cert :3640-3645, replicate :814-819. Each window is a legal
  BF-abort injection point with bespoke revalidation. **The densest cluster of
  timing-dependent transitions in the SUT** (focus 3 §1.4).
- **Unlocked cert-param mutation**: Certification::param_set writes optimistic_pa_/
  log_conflicts_ with NO lock (certification.cpp:1402-1422) while do_test reads them under
  mutex (:418,:456). `SET GLOBAL wsrep_provider_options='cert.optimistic_pa=...'` under load
  = data race that changes depends_seqno computation (focus 3 §1.5).

### 5.3 BF-abort machinery and InnoDB interaction

- **InnoDB deliberately IGNORES record-lock conflicts between two high-priority (applier)
  transactions** (lock0lock.cc:581-599 "supposed to be false positives" → NO_CONFLICT; also
  :2126-2149). Divergence prevention is delegated entirely to certification's depends_seqno.
  **Highest-value divergence target: cert.optimistic_pa=yes + wsrep_applier_threads>1 +
  FK/UK-heavy workload + per-node data comparison** (focus 3 §2.1; matches bug pattern A).
- Victim selection carries the in-code comment "KH: My brain hurts! Is it OK?"
  (lock0lock.cc:2238); wsrep_thd_order_before reads both seqnos unsynchronized
  (sql/service_wsrep.cc:211-227); wsrep_thd_is_BF(thd,false) reads client_state::mode_
  without lock (service_wsrep.cc:121-132; InnoDB call sites lock0lock.cc:1674,1900,2135,
  5287) — torn/stale BF classification during mode switches (focus 3 §2.2-2.3).
- **MDL BF-BF conflict → unireg_abort(1)** (wsrep_mysqld.cc:3308-3313; "unknown BF-BF"
  :3382-3386). Node suicide on concurrent TOI + applier MDL contention — the punishment for
  every missed cert key in pattern A (focus 3 §2.4, focus 6, focus 8 §4).
- TOCTOU in wsrep_abort_thd: is_aborting checked, lock released, THEN
  ha_wsrep_abort_transaction (wsrep_thd.cc:333-358). victim_thd->wsrep_aborter protected by
  TWO different mutexes (LOCK_wsrep_thd in ha_innodb.cc:24439-24463 vs LOCK_thd_data in
  service_wsrep.cc:190-202) → lost awake(KILL_QUERY) → victim parked until
  innodb_lock_wait_timeout (focus 3 §2.5-2.6).
- **Lock-order inversion candidate**: BF aborter inside the lock-sys global shared latch
  (lock0priv.h:1086-1099 run_if_waiting) → wsrep_innobase_kill_one_trx → bf_abort →
  streaming_rollback waits UNBOUNDED on the victim's condvar (transaction.cpp:2054-2055)
  while also taking the server_state mutex. Repro shape: SR (fragment_size>0) + TOI DDL +
  row conflicts. Strong smell; the full cycle was not constructed (focus 3 §2.7).
- The rollbacker queue violates the current_cond protocol (wsrep_thd.h:122-138 vs
  sql_class.h:3741-3749); shutdown deletes the queue (wsrep_thd.cc:298) → concurrent awake()
  can broadcast on freed memory (focus 3 §2.8).

### 5.4 Wsrep_async_monitor (PXC-only, default ON)

Keyed on the logical-clock sequence_number from Gtid_log_event (log_event.cc:2682-2687);
active per-applier when replica_preserve_commit_order && workers>1
(rpl_replica.cc:7285-7290). Built in 2024-25 for PXC-4173 (MTS + preserve-commit-order
deadlock); 9 commits, then regressions PXC-4664/4688 (nullptr deref SIGSEGV on duplicate
explicit gtid_next; "after fixing sigsegv, it is still possible to end up with a deadlock")
and PXC-4823 (async OPTIMIZE TABLE scheduled but never entered/skipped → permanent stall)
(focus 6 §C-D). Structural hazards:

- **enter() early-returns when thd->killed but leave() is unconditional → seqno mismatch →
  assert + unireg_abort(1)** (wsrep_mysqld.cc:2948-2956; wsrep_async_monitor.cc:89-91).
  before_prepare and to_isolation_begin call the pair unguarded (wsrep_trans_observer.h:257/
  277, wsrep_mysqld.cc:3130/3166). KILL/STOP REPLICA between schedule and enter on an
  out-of-order worker = whole-node crash; the abort happens while holding m_mutex, the
  diagnostic is a commented-out std::cout, exit status 1 matches RestartPreventExitStatus →
  systemd won't restart (focus 3 §3.1, focus 11 §1.3, focus 12 §8).
- **Stale skipped_seqnos survive source binlog rotation** (sequence_number restarts at 1,
  rpl_mta_submode.cc:601-604; the monitor is never reset, deleted only at applier stop
  rpl_replica.cc:7722-7724). GC only erases <= seqno - m_workers_count; when the new counter
  reaches stale values, enter/leave short-circuit → transactions bypass the ordering monitor
  (needs runtime confirmation) (focus 3 §3.2).
- **Sized by the wrong variable**: constructed with rli->opt_replica_parallel_workers
  (rpl_replica.cc:7289), NOT wsrep_applier_threads; m_workers_count gates skipped_seqnos
  pruning → applier_threads >> parallel_workers → skipped seqnos erased while waiters need
  them → permanent applier stall. A skip(n) pruned before enter(n) runs → waiter blocked
  forever (wsrep_async_monitor.cc:100-106,:135-141). Raw std::mutex/condvar — invisible to
  performance_schema (focus 12 §8, focus 5 §9).

### 5.5 wsrep-lib transaction state machine

- **The 13×13 transition matrix is asserted, not enforced: an illegal transition logs +
  assert(0), and the state is applied anyway in release builds** (transaction.cpp:1392-1422)
  (focus 3 §4.1). Same pattern for server_state: illegal transitions are warnings in
  release, transition applied (server_state.cpp:1454-1500); on_sync can drive
  s_donor→s_synced skipping s_joined (:1194-1200; hazard described
  replicator_str.cpp:749-768) (focus 8 §7).
- wait_for_replayers: a global int wsrep_replaying; every commit polls at 10ms while any
  replay is in progress (wsrep_client_service.cc:312-328) — one replay throttles all commits
  (focus 3 §4.4).
- Wsrep_condition_variable::wait bypasses THD::enter_cond (wsrep_condition_variable.h:33-36)
  — threads waiting in close()/streaming_rollback are invisible to KILL and PROCESSLIST
  (focus 3 §4.5).

### 5.6 Global-state races

- Wsdb keys default-trx_id transactions by pthread_self() (galera/src/wsdb.cpp:112-163,
  :248-255): pthread_t reuse collision → gu_throw_fatal :118; dtor sleeps 5s then proceeds
  with live handles (:85-94) (focus 3 §5.3).
- wsrep_slave_count_change is a plain int with inconsistent locking (wsrep_var.cc:674-691
  unlocked vs wsrep_high_priority_service.cc:956-965 locked) → lost updates on SET GLOBAL
  wsrep_applier_threads under load (focus 3 §5.4).
- **PXC try_desync_and_pause re-enters the local monitor at a different seqno**
  (replicator_smm.cpp:3419-3511); on seqno mismatch it only WARNS (:3468-3471) then
  enter(lo2) on a non-consecutive seqno blocks until the gap fills → FTWRL /
  wsrep_desync=ON hangs indefinitely (focus 3 §5.6).
- **Interim commit**: the commit monitor is released from inside the binlog flush-queue
  mutex (rpl_commit_stage_manager.cc:104-133 → wsrep_ordered_commit →
  replicator_smm.cpp:1517). Galera total commit order is surrendered before the engine
  commit; correctness = f(opt_log_replica_updates, opt_binlog_order_commits) + no
  group-commit bypass (wsrep_trans_observer.h:364-366) (focus 3 §6.1).
- Cosmetic-but-telling: broken assert `assert(locked_ = true)` (assignment) in
  TrxHandleLock::unlock (trx_handle.hpp:1156); dead no-op critical section
  binlog.cc:8952-8959 (focus 3 §6.2-6.3).

## 6. Claimed guarantees (focus 4, 5; deduplicated with 1, 2, 3, 8, 10, 11)

All CLAIMS to test. Grouped; each with source and voiding conditions.

### 6.1 Divergence prevention

- **CLAIM SG-1.1: all nodes receive writesets in the same total order.**
  doc/source/manual/certification.rst:91-103; wsrep_api.h:365-367 (view cb in total order);
  enforced by three ordered monitors (replicator_smm.hpp:1226-1228) whose internal
  consistency is assert-only (monitor.hpp:156,:240,:439,:468 — compiled out in release).
  Test: cross-node row equality after workload+faults; wsrep_last_committed /
  wsrep_local_state_uuid agreement (focus 4).
- **CLAIM SG-1.2: certification failures produce dummy writesets so seqnos stay symmetric.**
  sql/wsrep_high_priority_service.cc:569-572 ("maintain same consistency across cluster
  nodes"); replicator_smm.cpp:1466-1468 gcache seqno_skip on the negative-vote path. Attack
  surface: seqno gaps/skips × IST replay (in-code notes ist.cpp:622,:667); the
  missing-streaming-applier dummy paths (server_state.cpp:392-398,:433-440); MDEV-38843
  (apply error + rollback error → log_dummy_write_set skipped → seqno stuck) (focus 4, 1, 6).
- **CLAIM SG-1.3: last_applied ("commit cut") computation is bit-identical on all nodes.**
  gcs_group.cpp:279-284 ("crucial for consistency ... absolutely identical on all nodes");
  version-gated monotonicity guard buggy in gcs protocols 2-4 → mixed-protocol clusters a
  target (focus 2 §7). The received commit cut is trusted: a too-large value blocks the recv
  thread forever in apply_monitor_.wait and purge_trxs_upto(too-large) empties the cert
  index → divergent certification (gcs_action_source.cpp:116-123;
  replicator_smm.cpp:2313-2339) (focus 11 §5.1).
- **Documented guarantee-void levers (use as fault injectors)**: tables without PK
  (limitation.rst:57-60; `wsrep_certify_nonPK` default true = full-row-hash certification);
  non-InnoDB writes (limitation.rst:9-11; wsrep_replicate_myisam=ON fatal under ENFORCING,
  mysqld.cc:7285-7305); RSU (schema divergence by design); pxc_strict_mode
  DISABLED/PERMISSIVE on one node (features/pxc-strict-mode.rst:79-83);
  `wsrep_ignore_apply_errors != 0` demotes apply errors (wsrep-system-index.rst:556-590;
  wsrep_mysqld.cc:3425-3477); ALTER TABLE IMPORT/EXPORT; CTAS (denied under ENFORCING)
  (focus 4 SG-1.4, focus 10).

### 6.2 Certification determinism

- **CLAIM SG-2.1: certification constants are identical cluster-wide.**
  certification.cpp:37-40: "EXTREMELY important that these constants are the same on all
  nodes. Don't change them ever!!!" — CERT_PARAM_MAX_LENGTH_DEFAULT(16384),
  LENGTH_CHECK(127). But cert.max_length / cert.length_check are node-local hidden params
  settable via wsrep_provider_options (:47-52) with **NO cross-node agreement/gossip
  (nothing in gcs_state_msg.cpp)**; do_test fails certification when cert_interval >
  max_length_ (:432-445). Different values per node → different verdicts → silent
  divergence with no vote (focus 4 SG-2.1, focus 11 §2.1 — reasoned inference; confirm no
  gossip on a second pass).
- CLAIM SG-2.2: the certification rule matrix (certification.cpp:257-273, conflicts
  :216-222) is deterministic; trx_cert_version_match() :397-414 — version mismatch →
  TEST_FAILED (silent-divergence risk during rolling upgrade); index-rebuild carve-out
  :430-431 (focus 4).
- PXC deviation SG-2.3: NBO certification of regular+SR trx (certification.cpp:490-503) —
  all-PXC clusters agree; mixed PXC/Codership clusters would not (focus 4).
- `wsrep_certification_rules` STRICT (default) vs OPTIMIZED changes FK conflict verdicts;
  cluster uniformity NOT documented as required and no negotiation path found (focus 4 §8,
  open question).
- **Nothing validates cross-node lower_case_table_names / collations / charsets**
  (wsrep_check_opts.cc:36-44 validates only 7 options, all locally); cert keys are raw name
  bytes with no case folding (wsrep_mysqld.cc:1775-1776) → mismatched
  lower_case_table_names → conflicting txns certified as non-conflicting → undetected
  divergence (focus 11 §2.3).

### 6.3 Inconsistency voting (divergence detection)

- CLAIM: a node whose apply fails votes; the minority is ejected ("Leaving cluster").
  process_apply_error → vote (replicator_smm.cpp:1432-1470; ":1439 must be done IN ORDER");
  process_vote :2379-2434 ("Vote 0 (success) ... inconsistent with group. Leaving cluster."
  :2411-2413); group_recount_votes gcs_group.cpp:939-1080; gcs.vote_policy :1034-1046.
- **Fundamental limitation: only apply ERRORS are voted on. Successful-but-different apply
  (collation differences, nondeterministic functions, missed cert keys) produces no vote, no
  state hash, nothing** — cross-node convergence must be checked externally by the workload
  (focus 11 §2.4). This is the central argument for a continuous checksum oracle.
- Vote determinism fragilities: votes hash the error buffer; appliers run as root while the
  originating session may not → privilege-dependent divergence (bug pattern B: PXC-4709,
  PXC-4765, PXC-4644, PXC-4887, PXC-4284, PXC-4268, PXC-4336, PXC-4362). Protocol V7
  (PXC-5286) reduces TOI/NBO votes to " Error_code: NNNN;" but documents residual
  divergence: DML apply errors keep full locale-dependent text; multi-error order
  nondeterministic; warning 1681 subset (sql/wsrep_mysqld.cc:110-140;
  recompute_vote_based_on_error_code gcs_group.cpp:1093-1152 — regex only 4-5-digit codes;
  **no match → vote over the EMPTY string → two nodes failing differently vote identically
  and can out-vote the correct node**; hardcoded ignore list = {"1681"} :1082-1091)
  (focus 4 SG-2.4, focus 6 §B, focus 8 §5).
- **Empty-error apply failure bypasses voting entirely: the node unilaterally declares
  itself inconsistent and leaves without a group vote** (replicator_smm.cpp:626-637
  assert(0==e.data_len()); vote only if err->ptr :1941-1944; trigger e.g. apply_toi
  acquire_shared_backup_lock failure, wsrep_high_priority_service.cc:453-456). Transient
  local condition → self-eviction (focus 8 §5).
- **Potential NULL deref in the vote handler**: non-zero code + no payload →
  std::string(data, strlen(data)) with data=NULL (gcs_group.cpp:1184-1198) — reachable from
  a malformed/truncated/mixed-version vote message (focus 8 §5).
- Voting-related in-code open question: wsrep_applier.cc:83-88 "KH: ... vote uses errors AND
  warnings. Is it OK?" (focus 4).
- After eviction: `repl.force_sst_after_inconsistency` default OFF (params.cpp:50) →
  grastate preserved → **does the inconsistent node rejoin via IST into corrupt state?**
  (PXC-5208 fixed this at branch tip by unlinking grastate on shutdown, but gated by the
  same default-OFF option — pre-fix behavior ships) (focus 4 §3, focus 6).
- Certification::mark_inconsistent has assert(!inconsistent_) (certification.cpp:1425-1430)
  — double-detection trips the assert (debug) or proceeds (release).
- Self-declared hole: closing during NBO → "node left in inconsistent state, must be
  re-initialized by full SST or backup" (replicator_smm.cpp:1808-1812).

### 6.4 Quorum / split-brain

- CLAIM SG-4.1/4.2: only the weighted-quorum partition stays Primary. have_quorum
  pc_proto.cpp:555-575; have_split_brain :578-597 (exact-tie → both non-primary); quorum
  loss → mark_non_prim :602-644; conflicting-primaries tiebreak = greater view id (pc.npvo,
  :1071-1085); hard gcomm_assert(have_quorum) in handle_trans_install :1345 (gcomm_assert =
  gu_throw_fatal, LIVE in release — gcomm/exception.hpp:21-22, focus 11 §0). Manual
  pc.bootstrap on both halves → divergence "impossible to re-merge" (failover.rst:88-94).
- CLAIM SG-4.3: non-ready node refuses queries with ER 1047 (sql_parse.cc:1678-1698,
  :3795-3810; readiness = 4 conditions wsrep_mysqld.cc:794-806). Bypasses:
  wsrep_dirty_reads, CF_SKIP_WSREP_CHECK (SET/SHOW/SHUTDOWN/bare SELECT)
  (wsrep_mysqld.cc:769-772). GCS refuses causal reads in non-primary with -EPERM
  (gcs.hpp:305-313).
- Voidable at runtime: pc.ignore_quorum (pc_proto.cpp:621-627), pc.ignore_sb (:614-620) —
  both dynamic via SET GLOBAL wsrep_provider_options (focus 4 SG-4.4).
- Re-merge liveness: re-bootstrapping prim from partitioned components (:1052-1056) requires
  ALL non-evicted members of the greatest last-prim view present; blocked while any node is
  in un() state :964-972 — a flapping third node can prevent re-merge indefinitely
  (focus 5 §2).

### 6.5 Bootstrap safety

CLAIM: a node refuses to bootstrap when safe_to_bootstrap=0 (replicator_smm.cpp:412-420);
the flag is true only when sole member (:3245); persistence saved_state.cpp:124-132,
:200-235, :365-381. Voids: the two-singleton-partitions scenario (§4.2 above); operator
hand-edit after full unclean shutdown (documented procedure, crash-recovery.rst:171-214 —
**the manual step where users lose data**: bootstrapping from a behind node discards the
ahead node's transactions via forced SST, focus 10); interrupted mysqld_bootstrap script
leaves MYSQLD_OPTS=--wsrep-new-cluster in the systemd manager environment → the next
ordinary `systemctl start` bootstraps a new cluster (focus 12 §13). Test:
galera_3nodes/t/galera_safe_to_bootstrap.test.

### 6.6 Read causality (wsrep_sync_wait)

CLAIM: with sync_wait bits set, reads observe all preceding writes (wsrep_api.h:1216-1240;
wsrep-system-index.rst:1309-1366). Implementation replicator_smm.cpp:1678-1746 with in-code
caveats: the timed monitor wait is "a hack" to avoid deadlock with drain (:1707-1713); it
uses apply_monitor_, not commit_monitor_ (:1715-1721, :1748-1758) — so "applied" not
"committed"; timeout (repl.causal_read_timeout default PT30S) → WSREP_TRX_FAIL surfaced as
**ER_LOCK_WAIT_TIMEOUT with a "Synchronous wait failed" message the in-code comment admits
won't be displayed** (wsrep_mysqld.cc:1509-1548) — diagnosability defect. Skips:
dirty_reads, inside an active multi-statement transaction, replaying
(wsrep_must_sync_wait, wsrep_mysqld.cc:767-778) — test causality of the 2nd+ statement in a
transaction. seq_cb fires before gu_cond_wait on delivery (gcs.cpp:2260-2266). Uses
CLOCK_REALTIME — NTP steps produce spurious failures (focus 4 §6, 5 §8, 9 §4, 11 §6.3).
**Default wsrep_sync_wait=0: stale reads are the shipped contract — test 0 and 1 as
separate property sets** (focus 10).

### 6.7 Commit durability and ordering

- CLAIM: an ordered transaction cannot be BF-aborted and can always finish committing —
  three numbered guarantees in wsrep-lib transaction.cpp:598-608 (exception: SR fragment
  storage commits :610-614); guards are asserts (:592-593,:625,:628) + "should always
  succeed" commit_order_leave whose non-assert failure path quietly aborts the ordered txn
  locally while peers commit (focus 11 §4.6).
- CLAIM: provider total order — assert ts.global_seqno() > last_committed()
  (replicator_smm.cpp:1392); commit_order_enter contract wsrep_api.h:1080-1082.
- Broad doc claim to attack: "loose any node at any point ... without any data loss"
  (intro.rst:28-30) vs "successful return code does not guarantee delivery to group"
  (gcs/src/gcs_core.hpp:102).
- Durability composition: replication ≠ fsync. MTR always runs
  innodb_flush_log_at_trx_commit=2; property "acked commit survives cluster-wide power
  loss" requires flush=1 and is tested nowhere (focus 7).

### 6.8 First-committer-wins

CLAIM: conflicting concurrent txns → exactly one wins; the loser gets ER 1213/40001 at
COMMIT (certification.rst:139 "FIRST WRITE WINS"; limitation.rst:32-38). Emission sites
wsrep_mysqld.cc:1853,1864,1890,1908,2581,2756,3020. BF-abort safety contract: the kill
routine never aborts a txn ahead in total order (wsrep_api.h:1135-1145). Replay contract
trx_handle.cpp:190-215; wsrep_retry_autocommit default 1 (silent retry loop
sql_parse.cc:7977-8034 — autocommit only) (focus 4 §8, 10).

### 6.9 TOI / GTID

- CLAIM: TOI DDL executes at the same total-order point everywhere; MDL ignored under TOI
  (wsrep-system-index.rst:866-870,:889-893; wsrep_api.h:1268-1330). Failure mode = pattern A
  (missed cert keys → applier BF-BF → MDL BF-BF unireg_abort).
- CLAIM: cluster GTID sequence consistent — GTID incremented only on cert pass
  (certification.rst:207-215); dummy/empty GTID to keep the sequence consistent
  (wsrep_trans_observer.h:186-200). Observable: @@global.gtid_executed equality under
  wsrep_sync_wait=7 (galera_gtid.test:26-30). Voids: **wsrep_write_dummy_event is a no-op
  returning 0 (wsrep_binlog.cc:394-402), called on TOI/NBO begin-failure
  (wsrep_mysqld.cc:2408,:2443) → seqno consumed cluster-wide with nothing in the local
  binlog → GTID/binlog discontinuity for async replicas** (focus 11 §4.4); local GTID leak
  family (PXC-4313 RSU, PXC-4312/4504 DROP IF EXISTS, PXC-4238 UDF, PXC-4034 sql_log_bin=0,
  PXC-4544 RESET BINARY LOGS); 8.4 tagged GTIDs (PXC-4526); unlocked wsrep_sidno →
  rpl_gtid_owned corruption (PXC-4652, needs applier_threads>1) (focus 6 §H).

### 6.10 Flow control bounds

CLAIM: recv queue bounded ≈ gcs.fc_limit (100 in PXC); pause at limit, resume below
fc_factor*fc_limit (wsrep-provider-index.rst:526-548; gcs.cpp gcs_fc_stop_begin :541-560,
gcs_fc_cont_begin :621-644). Voids: wsrep_desync=ON / RSU disable FC (queue unbounded);
`SET GLOBAL wsrep_desync` bypasses wsrep-lib desync accounting entirely (check fn calls
provider().desync directly, wsrep_var.cc:693-743; update fn no-op) → FTWRL can resync a
user-desynced node (focus 8 §7). Observables: wsrep_local_recv_queue,
wsrep_flow_control_paused/sent/recv. Property: without desync/RSU, wsrep_local_recv_queue
bounded on all nodes; wsrep_flow_control_paused_ns stops growing after fault heals
(focus 4 §11, 5 §3).

### 6.11 View changes / membership liveness

- CLAIM: EVS completes view changes within timer bounds (handle_timers evs_proto.cpp:857-890;
  check_inactive :899-1102 → S_GATHER :1063). Doc: write block on ungraceful death "slightly
  longer than" evs.suspect_timeout = 5s (failover.rst:20-23).
- **Termination by suicide**: handle_install_timer (evs_proto.cpp:678-773): attempt==max →
  mark ALL others inactive + isolate(20s) :726-729; attempt>max → gu_throw_fatal "giving up"
  :735-739 = process abort. Reachable with ~22.5s asymmetric connectivity at defaults
  (max_install_timeouts=3 code default; doc says 1) (focus 5 §1).
- **check_inactive self-skip: if the previous check was >3× check_period ago, the entire
  check is SKIPPED (evs_proto.cpp:900-909)** → CPU starvation indefinitely delays failure
  detection. Prime target: slow CPU + node kill (focus 5 §1).
- gmcast reconnect: retry every PT1S with max_initial_reconnect_attempts INT_MAX
  (gmcast.cpp:820-829,:1039-1113); **handle_established discards a fresh inbound connection
  if retry_cnt > max_retries (:726-734)**; peer teardown at recv_tstamp+3s (:1276-1294)
  (focus 5 §1).
- **Full-cluster restart can block forever**: restored V_PRIM view (pc.recovery) +
  pc.wait_restored_prim_timeout PT0S default → "server will wait indefinitely to reach PC"
  (pc.cpp:104-115); mysqld blocks in wait_until_state untimed (server_state.cpp:1498-1518)
  and never opens the SQL port. **Highest-value liveness property: full-cluster restart
  reaches wsrep_ready=ON within a bound** (focus 5 §2; confirmed by galera_restored_pc.test;
  the parameter is undocumented).

### 6.12 State transfer liveness

- **Unbounded STR retry**: send_state_request loops usleep(1s) forever on EAGAIN/ENOTCONN
  (replicator_str.cpp:955-1050); give-up only when local_monitor_.would_block (window 65536)
  → "Slave queue grew too long ... Application must be restarted" -EDEADLK :1032-1040.
- **Unbounded SST wait** in mysqld (while(!sst_received_) :1240; my_fgets ×2 wsrep_sst.cc
  :634,:694); bounds exist only in bash (wsrep_sst_common.sh:1189-1193: donor-timeout 10s
  socat connect, joiner/sst-initial 100s metadata-only, sst-idle 120s du-delta watchdog).
- IST: **hardcoded 10s SocketWatchdog per recv_ordered (ist.cpp:359-364,:504-512) → donor
  stall >10s kills IST → mark_corrupt + abort() "node restart required"
  (replicator_str.cpp:1677-1715)** — no config knob found; aborts the process rather than
  falling back to SST. Post-IST drain untimed (:1500) (focus 5 §4).
- Donor: sst_donate_other blocks the TOI/apply thread untimed (wsrep_sst.cc:1628); return to
  SYNCED via desync_count decrement (gcs_group.cpp:1304-1309); **documented permanent-desync
  bug in-code: gcs.cpp:2726-2751 "out of order seqnos leaving desync_count permanently
  non-zero ... node will not become synced again unless temporarily removed from group"** —
  test wsrep_desync=ON + SST donation + view change interleaved (focus 5 §4).
- **JOINED→SYNCED requires recv queue <= lower_limit (gcs.cpp:689-716) — sustained load
  keeps a joiner in JOINED forever; wsrep_ready only true on s_synced**
  (wsrep_server_service.cc:356-380) (focus 5 §3).
- Joiner FC hard limit: max_throttle=0 → GU_TIME_ETERNITY "Replication paused until state
  transfer is complete" — unbounded cluster-wide stall from one slow joiner
  (gcs_fc.cpp:104-129; gcs.cpp:1557-1595) (focus 8 §8).
- PXC-2213 workaround note replicator_str.cpp:1256-1263 documents a release-build infinite
  commit_monitor wait (focus 5).

### 6.13 Desync / pause / TOI liveness

- desync/resync refcounted (server_state.cpp:1416-1447); **resume_and_resync swallows
  failure ("server may have to be restarted" :721-744) → permanently desynced node serving
  reads and falling behind silently**; wsrep_RSU_end same (wsrep_mysqld.cc:2938-2944)
  (focus 5 §5, 11 §4.5).
- pause() holds local_monitor_ + drain_monitors untimed (replicator_smm.cpp:3385-3416) —
  a paused provider = cluster-wide FC block. wsrep_RSU_commit_timeout default 5000µs;
  try_desync_and_pause polls 100ms up to wsrep_desync_pause_retry_timeout 30s
  (server_state.cpp:693-720).
- **PXC TOI does not retry**: poll_enter_toi single attempt (wsrep-lib
  client_state.cpp:582-634; call site wsrep_mysqld.cc:2558) → e_deadlock_error;
  to_isolation_begin monitor-entry failure → gu_throw_fatal (replicator_smm.cpp:1911-1915);
  **NBO end wait unbounded** (:1805-1820). Property: TOI DDL during join/FC/kill either
  completes everywhere or fails on the originator; never leaves a permanently held TO slot
  (focus 5 §6).

### 6.14 Readiness / shutdown

wsrep_ready_wait untimed (wsrep_mysqld.cc:817-825); shutdown `wsrep_wait_appliers_close`
unbounded (mysqld.cc:12619-12638); replicator close waits up to 10 min for receivers
(replicator_smm.cpp:326-339); SIGTERM first sleeps pxc_maint_transition_period — **default
10 (sql/sys_vars.cc:8638-8642, DEFAULT(10); an earlier agent's "30s" reading was wrong —
resolved by code check)** — in the signal thread (mysqld.cc:4395-4404), and the same sleep
runs in the SET path holding MDL (sql_parse.cc:4518-4523). Liveness observables for the
workload: wsrep_ready, wsrep_local_state=4, wsrep_cluster_status=Primary,
wsrep_cluster_size, wsrep_last_committed advancing, wsrep_local_recv_queue draining,
wsrep_desync_count=0, wsrep_evs_delayed empty (focus 5).

## 7. Failure and degradation modes (focus 8; corroborated 2, 5, 9, 12)

### 7.1 SST joiner failure shapes

- **wait_for_listen prints "ready" unconditionally after 60s even if the listener never
  bound** (wsrep_sst_xtrabackup-v2.sh:1242-1318; ss branch :1193-1200; also focus 11 §8.4).
  Donor then connects to nothing. The detection loop is backgrounded BEFORE socat launches
  (:2207 vs :2224) (focus 9 §1.5).
- **Datadir deleted while the transfer runs, after the EAGAIN escape is disarmed**
  (:2383-2397); the transfer runs in a background subshell with tmt=0 (no wall clock);
  monitor_sst_progress starts only AFTER the wipe (:2426). galera_sst_failure.test asserts
  error 134 (SIGABRT) — the *intended* outcome of failed SST is abort with a wiped datadir.
- cleanup_joiner keeps sst_in_progress on failure, removes JOINER_SST_DIR evidence, and
  does `kill -KILL -$$` when estatus>=128 (:960-1000). The SIGTERM trap sig_joiner_cleanup
  does not exit (:953-957) → a SIGTERM'd script continues against a half-transferred dir.
- Shell defects (bash -ue, NO pipefail): **dead diagnostics — decompress/prepare failure
  handlers unreachable** because timeit's eval failure aborts under set -e before the
  `if [ $? -ne 0 ]` (:2511-2546; the move stage IS wrapped with set +e :2553 — the
  inconsistency is the bug); interruptable_timeout returns bare 124 losing pipeline codes,
  pkill -P only signals direct children (grandchildren survive holding port 4444);
  monitor_sst_progress: du failure → empty var → `[[ "" -eq 0 ]]` true → counted as a
  stall; kills children of $pid, not $pid (:341-390); safe_exit + SAFE_EXIT_CODE_OVERRIDE
  collapses every distinct pre-2383 failure to EAGAIN (wsrep_sst_common.sh:316-319)
  (focus 8 §3).

### 7.2 SST donor failure shapes

- **Donor pinned in FTWRL + innodb_disallow_writes indefinitely**: control words read via
  unbounded my_fgets (wsrep_sst.cc:1471-1512); the lock is released only at :1531-1535 after
  the read returns.
- **sst_disallow_writes failure is only logged; the backup proceeds with InnoDB writes
  allowed → torn snapshot, no error** (wsrep_sst.cc:1134-1152; caller :1481-1487 sets
  locked=true regardless) (focus 8 §2, 11 §4.2).
- Donor retry loop 30× full re-streams; only RC[1] (transport) checked, RC[0] (xbstream)
  ignored, RC never reset, no wall clock (:1462-1481); the joiner's `socat -u TCP-LISTEN`
  accepts ONE connection → retries connect to nothing (focus 9 §1.7). Hardcoded `sleep 10`
  before the data phase every SST (socat 1.7.4.3 workaround, :2036-2079).
- The donor↔script control channel is unauthenticated line-oriented plaintext over stdout
  ("flush tables"/"continue"/"done uuid:seqno", wsrep_sst.cc:1479-1499); any subprocess
  printing a matching line injects control frames; the "SST script died" error is inside
  `#if 0` (:1503-1508) (focus 12 §9).

### 7.3 Self-destruct inventory (abort as error handling)

Galera: replicator_str.cpp:1035-1042 (-ENODATA), :1088-1093 (non-retryable STR), :946
(exception in state request), :1288, :1318-1321 (wrong UUID from SST), :1421, :1449,
:1707-1712 (**any gu::Exception in recv_IST → mark_corrupt + abort**);
replicator_smm.cpp:1503-1510 (non-ApplyException in handle_apply_error), :3826-3828 (cert
exception), plus callback-failure aborts :2544+,2767,2796,2915-2926,3054-3062,3113-3120,
3354,3375-3380; gcs.cpp:1096-1101 (FC auto-evict, default off), :1218-1284, :2469-2483
(failure to send CONT → gu_abort "Aborting to avoid cluster lock-up");
gcs_core.cpp:638-656 (send FIFO != recv order → -ENOTRECOVERABLE), :1355-1362 (usleep(1s)
then gu_abort = the only handling of ENOTRECOVERABLE); gcs_group.cpp:479-489 (act_id ahead
of quorum → INCONSISTENT), :589-596, :637-643, :693-699; write_set_ng.hpp:169-171 (bad
version byte from peer → abort); gcache_mem_store.cpp:42-44 (corrupt buffer header);
GCache_seqno.cpp:80-86 (seqno reuse); certification.cpp:1287-1289 (dup trx entry);
evs_proto.cpp:733-739 (install-timeout give-up); **pc_proto.cpp:1486-1497 (O_SAFE delivery
gap → gu_throw_fatal — THE load-bearing total-order axiom, enforced by dying)**, :653-659,
:1271-1277. Server: wsrep_mysqld.cc:3312 (MDL BF-BF), :3385, :2425-2428/:2463 (TOI/NBO
release failure); wsrep_high_priority_service.cc:1050-1058 (unexpected replay outcome);
wsrep_async_monitor.cc:85-91; wsrep_sst.cc:738-751; bare aborts on every
pthread_mutex_lock failure. **gu_abort() (galerautils gu_abort.c:29-58) suppresses core
dumps (setrlimit 0 + PR_SET_DUMPABLE 0) — override in the test image** (focus 8 §4,
focus 11 §3).

### 7.4 Crash × SST interaction

- **The fatal-signal handler takes LOCK_wsrep_sst (sql/signal_handler.cc:402-416 →
  wsrep_sst_cancel → mutex_lock wsrep_sst.cc:554). If the crashing thread holds it, the
  crash handler self-deadlocks: mysqld never dies, the node is neither up nor down**
  (focus 8 §9).
- SST cancel = SIGTERM to the process group only, never verified, no SIGKILL escalation
  (wsrep_utils.cc:922-938); combined with the non-exiting joiner trap, a cancelled SST
  keeps running.
- **Spawn divergence**: the fork path sets PR_SET_PDEATHSIG (wsrep_utils.cc:461-470); the
  posix_spawn path only sets SETPGROUP, NO PDEATHSIG (:580-650) → **mysqld SIGKILL/OOM
  leaves the whole SST tree + the hidden post-processing mysqld orphaned, holding port 4444
  and the datadir, while systemd (Restart, RestartSec=1) starts a fresh mysqld on the same
  datadir** (focus 8 §9, focus 12 §5). Signal-killed children yield bogus errno as the SST
  failure code (:882-887).

### 7.5 DONOR/desync serving states

- **SYNCED→DONOR leaves wsrep_ready=ON and wsrep_cluster_status=Primary** (fall-through in
  log_state_change wsrep_server_service.cc:348-365; open question in-code
  wsrep_sst.cc:1692-1694). Mitigation wsrep_sst_donor_rejects_queries default OFF; even
  when ON, existing connections are not closed (:1042-1045).
- Meanwhile innodb_disallow_writes (deprecated, ha_innodb.cc:25682 — PXC SST donor is its
  only caller; a plain global **any SUPER session can flip mid-SST**) makes every InnoDB
  file write wait on srv_allow_writes_event with NO timeout (os0file.cc:236) — the node
  accepts connections, serves reads, clustercheck returns 200 with AVAILABLE_WHEN_DONOR=1,
  and every write hangs forever (focus 12 §9).
- **FTWRL does not block COMMIT in PXC** (sql/handler.cc:1841-1857 skips the MDL COMMIT
  lock when WSREP(thd)) — backup tooling's commit-quiescence assumption is broken
  (focus 12 §9).

### 7.6 Error-code taxonomy (user-visible)

1. ER 1213 ER_LOCK_DEADLOCK at COMMIT — normal first-committer-wins loser; also returned
   for BF-abort-while-idle and, misleadingly, **for TOI when !wsrep_ready
   (wsrep_mysqld.cc:3016-3025) — clients see a retriable deadlock and hammer a non-primary
   node** (focus 8 §7). 2. ER 1047 ER_UNKNOWN_COM_ERROR — non-Primary / joining / and,
   indistinguishably, wsrep_reject_queries (focus 10). 3. ER 1205 ER_LOCK_WAIT_TIMEOUT —
   sync-wait failure mislabeled as an ordinary lock timeout (wsrep_mysqld.cc:1510-1550).
4. ER_UNKNOWN_ERROR "Percona-XtraDB-Cluster prohibits..." — ~17 strict-mode sites, no
   dedicated code; also the rolling-upgrade write block (sql_parse.cc:1867,
   block_write_while_in_rolling_upgrade, protocol < V4 under ENFORCING/MASTER).
5. ER_CANT_UPDATE_WITH_READLOCK when the provider is paused (focus 8 §7, focus 10).

### 7.7 Flow-control stalls

Documented in-code hazards: STOP/CONT reorder → "nodes stuck waiting for CONT"
(gcs.cpp:583-590); recv-FC refcount reset on view change is the main recovery for wedged FC
(:1196-1218); **unbounded -EAGAIN spins at replicator_smm.cpp:731-733 ("TODO: Break loop
after some timeout"), :820-822, :2075-2076 — the SR rollback-fragment send spins at 1kHz
under FC forever** (focus 8 §8, focus 11 §8.5). One replaying transaction throttles all
commits via the global wsrep_replaying poll (focus 3 §4.4).

### 7.8 Hardcoded timeouts to perturb (consolidated)

10s IST watchdog (ist.cpp:359 → abort via replicator_str.cpp:1707-1712); 10s donor socat
connect; 100s joiner initial (+20s grace); 120s SST idle (SIGKILL); 60s wait_for_listen
(prints ready anyway); 300s rsyncd; configurable-since-PXC-4756 (was hardcoded 300s) SST
post-processing; 10s pxc_maint_transition_period sleep; evs suspect 5s / inactive 15s /
install 7.5s / keepalive 1s / max_install 3 / view_forget 24h; pc announce 3s / wait_prim
30s / wait_restored_prim ∞ / linger 20s; INT_MAX wait_for_gtid
(wsrep_server_service.cc:340-345 — error only warned, checkpoint written regardless);
repl.causal_read_timeout 30s; wsrep_desync_pause_retry_timeout 30s (focus 8 §10, 5, 9).

## 8. External dependencies (focus 9; corroborated 8, 12)

### 8.1 SST tooling

Binary deps: socat/openssl/diff fatal; pv/logger degrade; mysql/mysqldump/my_print_defaults
UNCHECKED (wsrep_sst_common.sh:176-190). xtrabackup ships bundled in
pxc_extra/pxb-{8.0,8.3,8.4}, selected by the DONOR-advertised version (:1593-1618);
unknown → safe_exit 2. lock-ddl=ON forced (PXC-5244 reverted REDUCED due to PXB-3818) → the
donor holds the backup DDL lock for the whole SST; TOI DDL during SST = interaction hazard.
nproc read from /proc/cpuinfo ignores cgroup quotas. exec_sql: the donor queries its own
mysqld with NO connect/statement timeout (common.sh:1000-1026; used for keyring status) —
an FC-paused donor hangs its own SST → hangs mysqld. /tmp fallback for the SST tmpdir
(:1504-1537). Clone SST (new in 8.4.4, PXC-4469): nc side-channel failures swallowed with
`|| :`, spawns a second full mysqld on 4444, restarts twice, greps the log for "Recovered
position", falls back to donor GTID without evidence (RP_PURGED_EMERGENCY), hand-writes
grastate.dat via cat<<EOF with no fsync/rename and hardcoded safe_to_bootstrap 0
(wsrep_sst_clone.sh:1199-1224). rsync SST: rejected at startup
(mysqld.cc:7350-7364) but the script is in-tree; unbounded listener wait (rsync.sh:421-427),
donor rsyncs --inplace --delete DIRECTLY INTO the live datadir (:282-347) (focus 9 §1, 8 §11,
12 §3).

### 8.2 TLS

Galera-channel TLS is configured from mysqld ssl-* ONCE at startup
(sql/ssl_init_callback.cc:553-558 → wsrep_mysqld.cc:1219-1224); **NOT part of ALTER INSTANCE
RELOAD TLS** (separate knob socket.ssl_reload, galerautils gu_asio.cpp:570-600) → cert
rotation done the MySQL way leaves the cluster channel on old certs → expiry breaks ALL
inter-node TLS simultaneously. **No hostname/peer-identity verification on the Galera
channel** (gu_asio.cpp:416-421, verify_peer only); SST disables commonname checking too.
pxc_encrypt_cluster_traffic default ON (read-only): the bootstrap node auto-generates its
own CA (ssl_init_callback.cc:506-548) — two independently bootstrapped halves have mutually
untrusted CAs and can never merge (focus 9 §2).

### 8.3 DNS

**gcomm addresses are resolved EXACTLY ONCE at connect() (gmcast.cpp:285-309); reconnect()
reuses stored resolved IPs; INT_MAX retries of a stale IP; no re-resolution anywhere** —
a K8s pod IP change → the node retries the stale IP forever. **Highest-value external-dep
finding for containers** (focus 9 §3.1). Unresolvable name at startup → warn + skip
(:287-295); ALL fail → gu_throw_fatal unless bootstrap (:315-320) — partial DNS outage =
silently reduced peer list. Blocking getaddrinfo on the gcomm thread (no timeout,
gu_resolver.cpp:502-560) reachable at runtime via gmcast.peer_addr change → hung resolver
blocks the protonet loop → EVS timers unserviced. Reconnect logging throttled to every 30th
attempt (gmcast.cpp:1098).

### 8.4 Clocks

gcomm/EVS/PC use monotonic time exclusively (78 uses) — membership is wall-clock-insensitive.
**But all condvar deadlines above gcomm use CLOCK_REALTIME** (no pthread_condattr_setclock
anywhere, gu_threads.h:42-47): gcs_sm.hpp:243-251 (send monitor), **gcs.cpp:1537/1579 —
the SST flow-control release: a backwards clock step → "Unplanned timeout!" → the pending FC
release is LOST (timeout reset to ETERNITY) → replication stays paused** (recv loop exits
:1733), replicator_smm.cpp:1683 (sync_wait), galera_gcs.hpp:154-156 (causal busy-poll 1ms),
nbo.hpp:64, monitor.hpp:385. Forward NTP step → spurious timeouts; backward → stalls, with
the asymmetry that membership stays up while replication stalls. UUID generation busy-spins
on CLOCK_REALTIME/100ns holding a global mutex (gu_uuid.c:30-50); node-byte fallback to
time+pid PRNG if /dev/urandom fails → identical UUIDs possible in container fleets
(gu_uuid.c:55-100). Writeset timestamps are wall clock (trx_handle.hpp:250). mysqld_safe's
restart throttle uses `date +%M%S` (mysqld_safe.sh:1209-1316) (focus 9 §4, 11 §6).

### 8.5 systemd / supervision

- **Shipped units: Restart=on-abort + RestartPreventExitStatus=SIGABRT — every Galera
  gu_abort / inconsistency-voting SIGABRT is EXCLUDED from restart → an
  inconsistency-abort leaves the node permanently down** (focus 9 §5.2). unireg_abort(1)
  exit status 1 likewise not restarted (focus 12 §8).
- TimeoutStopSec default 90s; the SIGTERM handler first sleeps pxc_maint_transition_period
  (10s) → ≤80s for flush+close → SIGKILL on slow shutdown → unclean → grastate -1 → forced
  SST (focus 9 §5.3). **Docker's default stop grace is exactly 10s → default PXC containers
  are SIGKILLed at the START of shutdown → forced SST on every container stop. Directly
  affects Antithesis harness graceful-stop assumptions** (focus 12 §7).
- **Crash recovery = grepping the error log, with two incompatible patterns**:
  scripts/mysqld_pre_systemd.in greps 'WSREP: Recovered position:' while the actual render
  is '[WSREP] Recovered position:' (log_sink_trad.cc:333-336 + subsystem tag);
  wsrep_sst_clone.sh:1202 greps the bracketed form. Consequences if the mismatch holds
  (VERIFY FIRST by running mysqld --wsrep_recover): no match → falls through to a 'skipping
  position recovery' grep against an append-only log → a stale line from an earlier boot
  satisfies it → node starts without --wsrep_start_position (silent forced-SST-forever); a
  genuinely fresh log → exit 1 → node refuses to start after crash; stale
  MYSQLD_RECOVER_START persists in the systemd manager global environment and replays on
  the next start (focus 12 §3). mysql-systemd:267-331 trusts grastate seqno != -1 verbatim
  (torn file → bogus start position); wsrep-recover mktemp INSIDE the datadir; start-post
  polls the pid file up to 900s; exit 2 (SST in progress) fails the unit (focus 9 §5.4-5.5).

### 8.6 Keyring/encryption

Donor checks keyring via untimed SQL; joiner via filesystem JSON grep
(get_keyring_manifest_and_config xtrabackup-v2.sh:1048-1105); disagreement detected only
after the transfer starts (:2325-2350). The SST transition key is generated in bash from
/dev/urandom|tr|fold|head and written PLAINTEXT to sst_info (:1968-1976) (focus 9 §6).

### 8.7 Health checks / proxy integration

- scripts/clustercheck.sh returns 200 iff wsrep_cluster_status==Primary AND
  (wsrep_local_state==4 OR (==2 AND AVAILABLE_WHEN_DONOR)) AND pxc_maint_mode==DISABLED.
  **It never validates the ability to commit: Synced+Primary+DISABLED returns 200 even with
  huge applier queues, FC pause, or frozen InnoDB writes.** Neither shipped checker looks at
  wsrep_ready, wsrep_reject_queries, wsrep_desync, FC state (focus 10, 12 §10).
  pyclustercheck.py.in is Python 2 (won't start on modern distros) and has a
  string-vs-int donor-state comparison that flaps 200/503 with a warm cache (focus 12 §10).
  **Health-check accuracy is a strong Antithesis property** (focus 10).
- pxc_maint_mode: NO_MUTEX_GUARD global with two unsynchronized writers — SET GLOBAL and
  the view-change callback (wsrep_server_service.cc:196-231, wsrep_pxc_maint_mode_forced
  auto-forces MAINTENANCE↔DISABLED on mixed-major detection) → the proxy signal is mutated
  by the server itself, an operator drain can be silently reverted, and **rolling-upgrade
  black-hole: mixed-version protocol < V4 + strict_mode ENFORCING → every node forces
  MAINTENANCE → clustercheck requires DISABLED → HAProxy/ProxySQL marks ALL nodes DOWN
  simultaneously** (focus 12 §7). Any SQLCOM_SET_OPTION session sleeps the 10s transition
  period while holding MDL after open_tables_for_query → concurrent TOI DDL blocks
  cluster-wide (focus 12 §7). ProxySQL v2 native Galera support no longer reads
  pxc_maint_mode — two health regimes in the field (focus 10).
- **wsrep_notify_cmd runs synchronously, NO timeout, from the view/state-processing path**
  (wsrep_notify.cc:104-112; callers wsrep_server_service.cc:241,:403; wsrep_sst.cc:1694) —
  a hung script stalls view processing; the shipped example wsrep_notify.sh:96 pipes SQL
  back into the node mid-view-change with no connect timeout = self-deadlock. Plus a
  memory-safety bug: 64KB stack buffer with `cmd_off += snprintf(...)` — truncation →
  negative size_t + OOB pointer; the guard tests exact equality AFTER the writes
  (wsrep_notify.cc:57-96); the stringified member id (:90) and view UUID (:65) are not
  sanitized (focus 9 §8.3, focus 12 §6).

### 8.8 Filesystem/network config

Ports 3306/4444/4567/4568 (+9200 clustercheck). wsrep_sst_receive_address=AUTO picks the
first non-loopback IPv4 interface (wsrep_utils.cc:1099-1119; IPv6-only throws) — unstable
interface order can advertise an unreachable address. gcache lives in
wsrep_data_home_dir=datadir — ENOSPC kills IST donation. grastate non-atomic vs gvwstate
atomic (temp+fsync+rename but no parent-dir fsync) — the inconsistency between the two
state files is itself notable (focus 9 §7).

## 9. Bug history (focus 6; annotations 10, 12)

PXC-ticketed commits/year: 2020: 565 → 2026 (to Sep): 175 — steady, not tapering.
2,352 PXC-ticketed commits total (2013-2026) out of 376,832.

### 9.1 Hotspot table (commits touching file, all-time / since 2023)

| Rank | File / area | All | ≥2023 | What breaks here |
|---|---|---|---|---|
| 1 | scripts/wsrep_sst_xtrabackup-v2.sh | 169 | 31 | SST orchestration: timeouts, datadir wipe, grastate safety, PXB version/lock-mode coupling, socat/encryption, child reaping |
| 2 | sql/wsrep_mysqld.cc | 129 | 52 | TOI entry/exit, replication decisions, provider glue, desync/pause |
| 3 | sql/sql_parse.cc | 105 | 50 | Cert-key collection for DDL (wsrep_to_isolation_begin call sites); repeated BF-BF source |
| 4 | sql/wsrep_sst.cc | 85 | 28 | SST request construction, donor/receive-address handling, /bin/sh -c spawning |
| 5 | storage/innobase/handler/ha_innodb.cc | 71 | 35 | wsrep hooks into InnoDB commit/rollback/lock paths |
| 6 | scripts/wsrep_sst_common.sh | 64 | 18 | Shared SST helpers, post-processing timeout, config parsing |
| 7 | sql/wsrep_high_priority_service.cc | 38 | 16 | Applier commit path, commit-order monitor release, cleanup_context |
| 8 | storage/innobase/lock/lock0lock.cc | 35 | 19 | rec_lock_check_conflict BF-abort victim selection |
| 9 | sql/wsrep_var.cc / sql/wsrep_thd.cc | 30 / 29 | 6 / 3 | Runtime sysvar validation; BF-abort of local THDs |
| 10 | sql/sql_admin.cc | 29 | 18 | OPTIMIZE/REPAIR/ANALYZE under TOI/RSU and async-replica workers |
| 11 | sql/wsrep_trans_observer.h | 29 | 13 | Transaction lifecycle hooks |

Galera submodule churn ≥2024: gcs_group.cpp (22 — quorum state, inconsistency voting),
gcs.cpp (22 — JOIN/SYNC races, vote dispatch), replicator_smm.cpp (18), replicator_str.cpp
(12), gcache_rb_store.cpp (9), ist.cpp (9), evs_proto.cpp (8). wsrep-lib churn ≥2022:
transaction.cpp (30), server_state.cpp (21), wsrep_provider_v26.cpp (15), client_state.cpp
(14).

### 9.2 Recurring bug patterns (each ≥3 tickets)

- **A. Missing certification keys for DDL → applier BF-BF abort → node eviction/suicide.**
  When the applier's MDL footprint exceeds the cert-key set, two writesets share
  depends_seqno, apply in parallel, one BF-aborts the other (illegal). PXC-4512 (RENAME
  child vs parent DML), PXC-4657/4684 (no-op UPDATE / trigger insert → Table_map without
  cert key), PXC-4789 (DROP TABLE parent with foreign_key_checks=0; the fix itself
  regressed multi-table DROP — post-push fix 8a91391942d), PXC-4348 (MDL BF-BF, exec-mode
  toi). **Class not closed by construction.** Untested candidates: multi-table RENAME,
  ALTER ADD/DROP FK cascades, TRUNCATE on FK parents, DROP DATABASE, partition exchange,
  views/triggers referencing renamed tables.
- **B. Inconsistency-vote divergence on error/warning text → benign difference evicts a
  node.** PXC-4709 (AUTHENTICATION_POLICY_ADMIN), PXC-4765 (CREATE TRIGGER DEFINER),
  PXC-4644 (ER_ROW_IS_REFERENCED vs _2), PXC-4683 (warning 1681 client-side only), PXC-4887,
  PXC-4284, PXC-4268, PXC-4336, PXC-4362, PXC-4298, PXC-5286 (protocol V7 with documented
  residual divergence). Tests: pxc_inconsistency_voting*, pxc_fk_inconsistency_voting*,
  galera_toi_vote, galera_vote_rejoin_{ddl,dml}.
- **C. Commit-order/apply monitor never released → applier hang → FC → cluster stall.**
  PXC-4844 (failed TOI leaves dirty Diagnostics_area; next empty writeset skips reset →
  cleanup_context rolls back → monitor never released; DBUG knob wsrep_force_empty_apply,
  test pxc-4844.test), MDEV-38843 (apply+rollback error → dummy writeset skipped → node
  stays PRIMARY while silently locking the cluster), PXC-4845 (grastate ahead of SE
  checkpoint → monitors uninitialized; test pxc_malformed_ist.test), PXC-4399 (FLUSH TABLES
  TOI holds CommitMonitor vs INSERT), PXC-4498/4390 (wsrep_group_commit_queue REMOVED
  −364 lines in 8.4.4 — "still is the source of deadlocks"), PXC-4173 (9 commits → the
  Wsrep_async_monitor subsystem), PXC-4823 (async OPTIMIZE never enters/skips its seqno).
- **D. Wsrep_async_monitor young and regression-prone.** PXC-4664/4688: nullptr-deref
  SIGSEGV on duplicate explicit gtid_next; "after fixing sigsegv, it is still possible to
  end up with a deadlock".
- **E. SST/IST lifecycle.** PXC-4631 (grastate marked unsafe too early; EAGAIN window),
  PXC-5208 (remove grastate after inconsistency eviction — gated default-OFF), PXC-4756
  (hardcoded 300s post-processing timeout), PXC-5244 (lock_ddl REDUCED reverted; PXB-3818),
  PXC-4292, K8SPXC-1724 (operator kills SST mid-flight), PXC-4500, PXC-3388, PXC-3691;
  Galera MDEV-36621 (seqno_release freed locked buffers → incomplete IST), MDEV-31517
  (config typo → spurious full SST), MDEV-22124 (JOIN race after SST vs config change).
- **F. gcache durability.** PXC-5209: gcache.recover trusted on-disk BufferHeader; corrupt
  cache → bogus free or ~10^12 seqno → OOM. Tests pxc_gcache_corrupt_{size,seqno}.test —
  **directly Antithesis-shaped: torn gcache after ungraceful kill.**
- **G. wsrep XID checkpoint.** PXC-5286 (V1 endianness bug, V2 format), PXC-4845, PXC-4498;
  historic PXC-4168 assert checkpoint==view.state_id.
- **H. GTID on the wsrep path.** PXC-4526 (8.4 tagged GTIDs truncated under
  CHECKSUM_ALG_UNDEF → eviction; "questionable if original logic ... is correct"), PXC-4652
  (wsrep_sidno never locked → rpl_gtid_owned corruption → SIGSEGV, needs
  applier_threads>1); local-GTID leak family PXC-4313/4312/4504/4238/4034/4544.
- **I. InnoDB × wsrep lock layer.** PXC-5099: SELECT FOR UPDATE SKIP LOCKED — the wsrep
  patch makes any lock request potentially wait on an HP applier → DB_SKIP_LOCKED trips a
  release-fatal assert (row0mysql.cc:1221); single-call-site fix, underlying "wsrep makes
  lock waits appear where InnoDB doesn't expect" unchanged.
- **J. Read-only/privilege context of internal threads.** PXC-5229 (appliers spawned after
  SET GLOBAL transaction_read_only=1 inherit read-only → apply error → inconsistency),
  PXC-4849 (joiner with super_read_only aborts the event scheduler at startup).
- **K. Shell injection (2026-06, ported from MariaDB).** PXC-5240/CVE-2026-49261
  (wsrep_notify_cmd: remote joiner's node_name/incoming_address reach every node's shell —
  CVSS 10.0; test galera_wsrep_notify_cmd_injection.test uses node name `;touch PWN;#`),
  PXC-5241/CVE-2026-48165 (wsrep_sst_donor / receive_address interpolation). PXC-3921 had
  deliberately ADDED special-char support in node names in 8.4.5.

### 9.3 Ranked regression targets (last ~12 months)

| Ticket | SHA | Date | Mechanism / why fragile |
|---|---|---|---|
| PXC-5208 | 374bb9e907e + galera 350f76f3 | 2026-09-07 | Force SST after inconsistency eviction; default OFF → pre-fix behavior ships |
| PXC-4844 | ba5eb494fb9 | 2026-04-28 | Empty writeset after failed TOI → commit-order monitor never released; other stale-DA carriers unaudited |
| MDEV-38843 | wsrep-lib 5eeef40 | 2026-05-27 | Apply+rollback error → seqno stuck; node stays PRIMARY while locking cluster |
| PXC-5209 | 383df32d591 + galera 96e28073 | 2026-05-10 | Corrupt gcache → OOM/bad free during recover |
| PXC-4845 | ff120ce5759 + galera a8bda1ba | 2026-02-09 | grastate ahead of SE checkpoint → monitors uninitialized; fix only converts hang to shutdown |
| PXC-5099 | 0fbe08cfd7b | 2026-03-13 | BF wait leaks DB_SKIP_LOCKED into asserting path; single call-site fix |
| PXC-5286 | a61fda31239, b1167962bae | 2026-08 | XID v1→v2; vote protocol V7 with documented residual divergence |
| PXC-5229 | f2a34b69292 | 2026-06-02 | Appliers inherit transaction_read_only |
| PXC-4789 | cb7d5ec3955 + 8a91391942d | 2025-11 | FK-child cert keys; fix regressed multi-table DROP |
| PXC-4657/4684 | 1c6301820d1 | 2025-05-12 | No-op UPDATE/trigger → table_map without cert key |
| PXC-4664/4688 | 00f0ff421f4 | 2025-05-27 | Async-monitor regression; deadlock still reachable per commit msg |
| PXC-4652 | aad68aa1274 | 2025-05-05 | wsrep_sidno unlocked → GTID map corruption (applier_threads>1) |
| PXC-4631 | b7202d6fef1 | 2025-06-17 | grastate unsafe too early; EAGAIN(11) window |
| PXC-4526 | 8cdccbd8524 | 2025-09-09 | Tagged GTID + checksum mismatch → eviction |
| PXC-5240/5241 | 9d4e3d5f953, 7a6af91e72a | 2026-06-15 | Remote-joiner-controlled shell injection |

### 9.4 Churn / fragile-fix signals

≥3 non-merge commits since 2024: PXC-4173 (9), PXC-4676 (7), PXC-5286 (6), PXC-5106 (5,
reverted twice then re-landed), PXC-4844 (4), PXC-4800 (4), PXC-4741 (4), PXC-4593 (4),
PXC-4469 (4, clone SST), PXC-5229 (3), PXC-4631 (3), PXC-4645 (3), PXC-4789 (2 + post-push
regression fix), PXC-4255 (post-push fix).

### 9.5 Disabled tests and CI-suppressed warnings

- disabled.def maps unfixed/flaky areas: galera_toi_ddl_online ("fails randomly with
  deadlock"), galera-index-online-fk ("fk_40 triggers inconsistency voting" — reproduces
  cluster inconsistency, disabled rather than fixed), galera_fk_lock_parent_update_child
  (PXC-3431/3501), galera_var_notify_cmd ("Failing to invoke the external script"); 7-8
  disabled "Needs dynamic wsrep_provider" (sysvar is READ_ONLY, sys_vars.cc:8243) —
  **every IST-retry and gcache-rollover scenario is untested**; ~18 galera-x disabled
  PXC-4154; ~20 innodb.* disabled because **PXC requires 16KB InnoDB page size (PXC-3129) —
  documented only in disabled.def, no enforcement found** (focus 6, 12 §11,13).
- **galera_3nodes_sr GCF-810A/B/C source include files that DO NOT EXIST in the repo — the
  SR crash-consistency suite is un-runnable** (focus 12 §11).
- mysql-test/include/mtr_warnings.sql:372-455 suppresses real degraded-state signals in CI:
  "Gap in state sequence. Need state transfer.", "Quorum: No node with complete state",
  "Failed to report last committed", "Ignoring possible split-brain", "no nodes coming from
  prim view", "JOIN message from member ... in non-primary configuration", "install timer
  expired", "Query apply failed", "Ignoring error for TO isolated action:", "Replica SQL:
  Error 'Duplicate entry", IST-AsyncSender-disconnect ("entirely timing-driven").
  **These are ready-made Antithesis assertion candidates — CI cannot see them by design**
  (focus 6 §4).

## 10. Existing test strategy and gaps (focus 7; corroborated 6, 12)

### 10.1 What exists

- MTR: 952 tests across 9 suites — galera 459 (2-node), galera-x 249, galera_sr 84, wsrep 58
  (1-node), galera_nbo 36, galera_3nodes 26, galera_3nodes_sr 18, galera_3nodes_nbo 15,
  galera_encryption 7. Only combination axis = gcache encryption. 71 big_test, 32
  have_debug, 77 debug_sync in the galera suite. Scenario counts: SST 62, IST 33, BF abort
  17, TOI 21 + DDL 21, split-brain ~6, voting ~12, gcache 15, kill/restart 22, FK 28.
- wsrep-lib: 141 Boost cases, ALL against mock_provider.hpp; **WSREP_LIB_WITH_UNIT_TESTS
  default OFF — not even built in a default PXC build.**
- Galera provider: ~500 START_TEST units (certification conflict matrix 58, key_set 44,
  evs2 42, pc 31, ist 14). **gcomm check_pc/check_evs2 with PropagationMatrix (latency,
  loss, split/merge over in-memory DummyTransport) are the only real fault modeling; the
  nondeterministic variants are excluded from CTest ("must be run manually").**
- unittest/gunit: only wsrep_async_monitor-t.cc (6 timing-based) and wsrep_xid-t.cc (3).
- **The Galera chaos harness (percona-xtradb-cluster-galera/tests/: test_seesaw kill/stop
  rotation under load with a checksum oracle, test_stopcont SIGSTOP/CONT, test_pc_recovery
  kill-all) is DEAD CODE** — requires absent sqlgen/dbt2/glb binaries + ssh hosts.
- **wsrep-lib/dbsim: an unused in-process N-node simulator loading the REAL provider .so —
  the most Antithesis-ready artifact in the tree; likely bit-rotted (CMake paths mismatch);
  needs a build attempt.**
- In-repo CI: CircleCI = clang-format/tidy only; Azure = compile matrix only; **no MTR/ctest
  runs anywhere visible** — all functional testing is internal Percona Jenkins.

### 10.2 MTR environment distortions (why MTR results don't transfer)

1. All nodes on 127.0.0.1 — no latency, loss, or real partitions (gmcast.isolate used in 27
tests is self-imposed and SYMMETRIC). 2. **innodb_flush_log_at_trx_commit=2 everywhere** —
durability-vs-replication interplay untestable. 3. **Suite default wsrep_sync_wait=15 /
causal_reads=ON — the production default 0 (stale reads permitted) is never asserted.**
4. **MTR relaxes the failure detector (mysql-test-run.pl:4349): suspect PT12S, inactive
PT30S, install PT15S, peer PT10S, wait_prim PT60S, max_install_timeouts 1 — the corpus is
tuned to AVOID membership churn.** 5. Default applier threads 1 in the field
(sys_vars.cc:8295-8299) — the multi-applier path is under-exercised everywhere.

### 10.3 Fault-injection hooks inventory (usable by the Antithesis workload)

- **Galera GU_DBUG_SYNC** (release-usable via `SET GLOBAL wsrep_provider_options=
  'dbug=d,<point>'` / `'signal=<point>'`; observe wsrep_debug_sync_waiters; dispatch
  replicator_smm_params.cpp:162-173): local/apply/commit monitor {master,slave}_enter_sync
  (replicator_smm.hpp:614-862); abort_trx_end (wsrep_provider.cpp:383); sync.apply_trx.*
  (replicator_smm.cpp:591,643,645); after_send_sync / before_replicate_sync /
  after_replicate_sync (:729,:797,:818); start_of_replay_trx :1224;
  before_local_commit_monitor_enter :1363; interim-commit syncs :1543,:1545;
  to_isolation_end :1954; sst_sent :2100; process_primary_configuration :3150;
  wsrep_desync_left_local_monitor :3481,:3556; after_cert_and_catch :3814;
  before/after_certify_apply_monitor_enter :3846,:3848; before/after_send_state_request,
  after_shift_to_joining (replicator_str.cpp:1198-1215); recv_IST_after_apply_trx :1615;
  recv_IST_after_conf_change :1670; ist_sender_send_after_get_buffers (ist.cpp:958).
  EXECUTE-type (mutating): sst_received_decrease_state_seqno (replicator_str.cpp:122),
  serve_mimimal_ist (:571), ist_receiver_wait_afterreceived_wrong_starting_seqno
  (ist.cpp:537), before_async_recv_process_sync → sleep(5) (replicator_smm.cpp:486).
  **Never used by any MTR test: process_primary_configuration, after_shift_to_joining,
  recv_IST_after_apply_trx, recv_IST_after_conf_change, sst_received_decrease_state_seqno,
  after_send_sync, sync.to_isolation_end.after_commit_leave, ist_receiver_wait...— exactly
  the state-transfer/view-change race sites.**
- **wsrep-lib crash points** (debug_crash → DBUG_SUICIDE, wsrep_client_service.cc:345-348),
  all SR: crash_last_fragment_commit_{before,after}_fragment_removal
  (transaction.cpp:307,:337); crash_replicate_fragment_{before_certify,after_certify,
  success} (:1663,:1671,:1697); crash_apply_cb_{before,after}_append_frag
  (server_state.cpp:134,:138); crash_apply_cb_{before,after}_fragment_removal,
  crash_commit_cb_before_last_fragment_commit, crash_commit_cb_last_fragment_commit_success
  (:187-199). Debug builds only.
- wsrep-lib debug_sync points: on_view_wait_initialized, wsrep_after_bf_abort,
  wsrep_after_certification, wsrep_after_commit_order_leave, wsrep_before_SR_rollback,
  wsrep_before_certification, wsrep_before_commit_order_enter/leave, wsrep_before_replay,
  wsrep_commit_or_rollback_by_xid_after_certify, wsrep_streaming_rollback.
- Server DBUG/DEBUG_SYNC: wsrep_sst_donate_cb_fails, wsrep_sst_donor_skip,
  halt_before_sst_donate, stop_after_reading_xid, pause_before_wsrep_ready,
  wsrep_force_empty_apply, simulate_wsrep_slave_error_on_init,
  wsrep_signal_{applier,nbo_applier}_thread, wsrep_to_isolation_begin_before_async_monitor,
  nbo_stop_before_apply_events, simulate_wsrep_multiple_major_versions; DEBUG_SYNC
  sync.wsrep_apply_cb, sync.wsrep_replay_cb, sync.wsrep_before_mdl_wait,
  sync.wsrep_ordered_commit, sync.wsrep_retry_autocommit, sync.wsrep_after_BF_victim_lock,
  sync.after_nbo_phase_one_begin. InnoDB crash points: crash_innodb_before_commit,
  crash_innodb_after_prepare, innodb_alter_commit_crash_{before,after}_commit.
- Process-level idioms already in MTR: galera_suspend/resume.inc (SIGSTOP/CONT),
  kill_galera.inc (kill -9), kill_at_sync_point.inc, gmcast.isolate=1, pc.bootstrap /
  pc.weight / pc.ignore_sb via provider options, gcache byte-patching via Perl.
  Provider-level self-isolation: wsrep_node_isolation_mode_set_v1
  (wsrep_provider.cpp:1909-1918).

### 10.4 Never tested anywhere (Antithesis whitespace)

Real/asymmetric partitions (no iptables/tc/netem anywhere); packet loss/reorder on real
sockets; clock skew/jump (zero faketime hits); disk faults EIO/ENOSPC/slow-fsync (zero
hits); crash during commit with in-flight writeset (InnoDB crash points unused in galera
suites); correlated multi-node crash; successful-but-different apply divergence (only
error-based injection exists); clusters >4 nodes / even-size quorum edges; membership churn
under sustained load (no MTR load generator); runtime config change under load (~35
galera_var_* tests all quiescent); rolling upgrades / mixed protocol versions; memory
pressure/OOM; SST interruption beyond one deterministic interleaving.

### 10.5 Oracle gap

Dominant oracle = golden-file .result diff — catches divergence only if the test happens to
SELECT on both nodes. galera_diff.inc and assert_table_are_same_in_cluster.inc are opt-in;
**galera_end.inc does NOT run a consistency check — no automatic cluster-wide state
comparison exists at end of any test.** Native asserts fire only in debug builds, which the
visible CI never builds. The dead harness's whole-DB checksum oracle is unreachable.
**Antithesis's continuous cross-node checksum property fills the SUT's single biggest
verification hole** (focus 7 §5, focus 11 §2.4).

## 11. Unproven assumptions and wildcard findings (focus 11, 12; unique observations)

- **NDEBUG meta-issue (focus 11 §0)**: galera cmake/compiler.cmake:57-59 and top
  CMakeLists.txt:1551-1554 force -DNDEBUG in non-debug builds. ~119 asserts in
  transaction.cpp, ~60 in certification.cpp, ~40 in GCache_seqno.cpp vanish in release.
  Asymmetry: gcomm_assert = gu_throw_fatal, LIVE in release (gcomm/exception.hpp:21-22);
  plain assert is not. Test both a release image (field behavior) and an assert-enabled
  image (spec oracle).
- Assert-only "should never happen" sites that misbehave in release (focus 11 §1):
  enter_apply_monitor_for_local_not_committing default branch returns WITHOUT entering the
  apply monitor; the paired leave then corrupts monitor state (last_left_ regression) →
  node stall (replicator_smm.cpp:3893-3910). s_donor during initial sync → forced
  donor→joined→synced — an actively donating node declares SYNCED and takes traffic
  (server_state.cpp:1176-1183). EVS message from a node claiming the current view but not
  in it → silently dropped → O_SAFE delivery gap (evs_proto.cpp:2474-2480).
- **cert.max_length node-locality** (focus 11 §2.1, focus 4 SG-2.1): hidden node-local
  params gate TEST_FAILED with no cross-node agreement — silent divergence with no vote.
  Same class: initial_position_ differs by join path; node-local index purge timing
  (:1259-1282).
- **The fork seam's only guard is a 130-line AWK script** (focus 12 §1):
  check_src/rem.awk — nest counter resets on every WITH_WSREP #if; #elif unhandled;
  **unconditionally deletes any line containing WSREP_TO_ISOLATION_BEGIN/END,
  WSREP_SYNC_WAIT, WAIT_ALLOW_WRITES — structurally blind to misplacement of TOI/sync-wait
  barriers.** Whitelist keyed by basename only (check_file.sh:29). check_src/whitelist/ is
  an undocumented changelog of unguarded semantic divergences from Percona Server
  (sql_table.cc DDL binlog rewrite, log_event.* wire-format divergence, sql_admin.cc table
  nullptr after failed wsrep_toi_replication, ha_innodb.cc `#if 0` autoinc step,
  sql_class.cc "TODO: add why this needs to be skipped"). ps-diff-check.sh:75 removes an
  undefined var path — cleanup never ran.
- **Log-grep recovery mismatch** (focus 12 §3, § 8.5 above) — VERIFY FIRST: run
  `mysqld --wsrep_recover` and grep both patterns; several downstream findings collapse or
  confirm together.
- **Hidden SST mysqld** (focus 12 §4): run_post_processing_steps
  (wsrep_sst_common.sh:655-1000) starts a standalone mysqld (--wsrep_provider=none,
  --skip-networking) on the joiner datadir on EVERY SST: sql_log_bin not disabled on the
  cmdline (upgrade DDL may mint GTIDs no other node has — verify at runtime);
  wait_for_mysqld_startup kills the wrong variable ($mysql_pid vs $mysqld_pid, :566 vs
  :611 — works by scoping accident); the GRANT ALL SST user is LOCKED but not dropped — a
  script killed before ALTER USER...LOCK leaves a live GRANT ALL account that propagates
  via future SSTs; logs written INTO the datadir propagate cluster-wide;
  normalize_version/compare_versions breaks on 3-digit components (8.0.100 < 8.0.99).
- **Orphan process trees** (focus 12 §5, §7.4 above): posix_spawn path = own process group,
  no PDEATHSIG; two divergent spawn implementations compile-time selected.
- **wsrep_notify buffer bug** (focus 12 §6, §8.7 above): snprintf accumulation overflow +
  unvalidated member id / view UUID; synchronous, untimed, in the view path.
- **pxc_maint_mode composition hazards** (focus 12 §7): two unsynchronized writers; 10s
  sleep in the dispatch path (RETRACTED at property investigation: the sleep holds NO MDL
  and no LOCK_global_system_variables — it only delays the operator's own session; see
  `properties/maint-mode-honors-operator-intent.md` Investigation Log); rolling-upgrade
  all-nodes-DOWN black-hole; SIGTERM sleep == Docker's exact default stop grace.
- **X-protocol document IDs are a function of cluster membership** (focus 12 §12, emergent):
  plugin/x/src/document_id_aggregator.cc:44 derives _id from
  @@auto_increment_offset/increment; wsrep_server_service.cc:195-198 rewrites both on EVERY
  view change (wsrep_auto_increment_control default ON, offset=own_index+1); own_index is
  reassigned per view → two nodes can transiently generate colliding document IDs. Matches
  disabled galera-x tests ("blinking auto-generated ids"). Same mechanism = INSERT
  auto-increment collision surface under membership churn (focus 10 §auto_increment).
- **FTWRL/COMMIT + innodb_disallow_writes** (focus 12 §9, §7.5 above).
- Load-bearing oddities (focus 12 §13): early-plugin registration relocated before
  server-UUID creation under WSREP (mysqld.cc:8838-8855 — keyring-before-Galera for GCache
  recovery); `#endif /* !!!!!WITH_WSREP */` rw_ha_count hoisting alters 2PC handlerton
  counting (handler.cc:1806-1808); ACL notify hooks removed entirely for CREATE/ALTER USER
  under WSREP (sql_user.cc:3288-3329,:4016-4064 — documented TOI↔ACL-lock deadlock);
  `#if 0`'d SR double-commit assert (wsrep-lib transaction.cpp:568-582);
  mysqld_bootstrap.in mutates the systemd manager global environment; percona_telemetry
  writes outside the datadir on every node; version reporting partly synthetic
  (cmake/wsrep-lib.cmake:20 hardcodes WSREP_PATCH_VERSION "4.3" while GALERA_VERSION=4.27).
- Wire-trust axioms (focus 11 §5): commit cut trusted (no bound check → recv-thread
  permablock or cert-index wipe); gap-freedom fatal at PC (pc_proto.cpp:1486),
  -ENOTRECOVERABLE at GCS, but *ignored* at certification (certification.cpp:1240-1258
  "perfectly normal"; :1121-1122 an assert given up: "local ordered transactions may get
  canceled without entering certification"); IST donor-side contiguity assert commented out
  (gcs_group.cpp:1857-1885 FIXME); SST-seqno-behind-cluster documented release-build hang
  (replicator_str.cpp:1243-1276).
- Known-broken interaction documented in-code (focus 11 §9): **NBO cert index cleared at
  2nd-phase begin while MDL still held — applier blocked in the SQL layer as the workaround
  ("it is as it is", wsrep_mysqld.cc:3280-3296).** Target: NBO + conflicting DML during the
  phase transition. Dirty-hack markers: gcs_core.cpp:1338-1341; gcs_state_msg.cpp:332;
  gcs.cpp:809,:1111 (#600), :2221 (#569); replicator_smm.cpp:2323 (#782).

## 12. Build/deployment considerations for the Antithesis harness

- **Builds**: produce BOTH a release image (NDEBUG — field behavior; assert-guarded
  invariants silently violated) and a debug/assert-enabled image (-UNDEBUG or
  CMAKE_BUILD_TYPE=Debug — turns ~200+ internal invariants into crash oracles; also unlocks
  the 77 DEBUG_SYNC-dependent behaviors, wsrep-lib SR crash points, and DBUG keywords).
  GU_DBUG_SYNC provider sync points work in release via wsrep_provider_options (focus 7,
  11).
- **Core dumps**: gu_abort() suppresses cores (setrlimit 0 + PR_SET_DUMPABLE 0,
  gu_abort.c:29-58) — patch or wrap for triage value (focus 11 §3.1).
- **Process supervision**: prefer direct process supervision over systemd in containers,
  but replicate the interesting field semantics deliberately: shipped units use
  Restart=on-abort + RestartPreventExitStatus=SIGABRT (+ exit 1), so inconsistency-aborts
  and async-monitor unireg_abort(1) stay down in the field — decide per-property whether
  the harness restarts them. **Container SIGTERM grace must exceed
  pxc_maint_transition_period (default 10s) + shutdown time; Docker's default 10s
  guarantees SIGKILL** (focus 9 §5, 12 §7).
- **Recovery flow**: if not using systemd, the harness must reproduce the
  mysqld_safe/mysql-systemd recovery dance itself (--wsrep-recover →
  --wsrep_start_position) or deliberately skip it — either choice is a distinct test
  surface given the log-grep bug (focus 12 §3).
- **Constraints**: InnoDB page size must be 16KB (PXC-3129); binlog_format=ROW,
  innodb_autoinc_lock_mode=2, log_output=FILE are startup-fatal requirements
  (mysqld.cc:7306-7342); no mysqldump/rsync SST (only xtrabackup-v2 and clone allowed,
  wsrep_sst.cc:79-89); pxc_encrypt_cluster_traffic default ON (read-only) — either provide
  consistent certs cluster-wide or set OFF explicitly; garbd needs matching certs.
- **Ports**: 3306 (SQL), 4567 (gcomm), 4568 (IST = base_port+1), 4444 (SST), 9200
  (clustercheck if used).
- **Topology**: minimum 3 nodes (matches galera_3nodes suite); 2-node + garbd is a valid
  variant that exercises the arbitrator path; galera_2nodes.cnf shows the minimal config
  (node1 gcomm:// bootstrap; per-node wsrep_node_address). Recovery lever available to the
  workload: `SET GLOBAL wsrep_provider_options='pc.bootstrap=true'` (focus 1 §12, 10).
- **Config recommendations for bug yield**: `wsrep_applier_threads > 1` (default 1 hides
  PXC-4652/4657-class bugs; recommended by focus 3, 6, 10); run both `wsrep_sync_wait=0`
  (production default — stale-read properties) and `=1/7` (causality properties) as
  separate property sets; `innodb_flush_log_at_trx_commit=1` for durability properties
  (MTR always uses 2); real failure-detector defaults (do NOT copy MTR's relaxed timers);
  consider `cert.optimistic_pa=yes` as an aggressive-parallelism variant; leave
  `repl.force_sst_after_inconsistency` at default OFF in one variant (ships that way) and
  ON in another.
- **Container gotchas**: gcs recv_q sized from host memory (gu_avphys_bytes()/4,
  gcs.cpp:402-418) and SST nproc from /proc/cpuinfo — both cgroup-blind; DNS: node
  addresses must be stable IPs or the one-shot resolution bug (§8.3) dominates every
  restart scenario (which may itself be the desired test); gcache + SST tmpdir live in the
  datadir volume — size it or ENOSPC properties fire constantly (or use that
  deliberately).
- **dbsim** (wsrep-lib/dbsim) as a possible fast inner loop: in-process N-node cluster
  loading the real libgalera_smm.so; needs a build attempt (CMake bit-rot suspected)
  (focus 7).

## 13. Assumptions (consolidated)

1. Release builds define NDEBUG and PXC, consistent with fc_limit=100 and the forced
   -DNDEBUG in cmake (focus 5, 11); confirm actual build flags in the harness image
   (UUID_URAND, _POSIX_SPAWN/HAVE_POSIX_SPAWN selection, HAVE_PSI_INTERFACE).
2. In-repo doc/source is 8.0-era; every doc-sourced claim needs 8.4 revalidation against
   docs.percona.com/8.4 and the tree (focus 4, 10).
3. The 8.4 Galera library is the MariaDB-fork lineage per external docs (MDB-REVISION files
   in-tree); the in-repo galera README saying Codership is stale — confirm with Percona
   (focus 10).
4. Commit messages taken as accurate for the bug-history mining (cross-checked against
   --stat); PXC-subject matching undercounts galera-submodule MDEV/MGL fixes (focus 6).
5. gmcast.isolate symmetry inferred from test usage, not from a gmcast.cpp read (focus 7).
6. monitor_sst_progress false-positive analysis inferred from shell semantics, not
   reproduced (focus 9).
7. RB-payload-never-msync'd claim rests on MMapFactory create(sync=false)
   (gcache_rb_store.cpp:121); gu_mmap.cpp partially read (focus 2).
8. The cert.max_length no-gossip finding is reasoned inference from gcs_state_msg.cpp
   absence — needs a confirming second pass (focus 11 §2.1).
9. Percona's internal Jenkins CI composition is invisible; the duplication calculus in §10
   assumes MTR-equivalent coverage there (focus 7).
10. Today assumed 2026-09-10; branch tip dated 2026-09-08 (focus 6).

## 14. Open questions (consolidated, deduplicated, attributed)

Verification-first items (cheap checks that gate other findings):
1. (focus 12) **Run `mysqld --wsrep_recover` and grep both 'WSREP:' and '[WSREP]' recovery
   patterns** — the systemd log-grep mismatch finding collapses or confirms.
2. (focus 3, 11) Confirm build NDEBUG status of the harness images; run assert-enabled
   binaries for at least one variant.
3. (focus 9) Which systemd unit actually ships (build-ps/rpm/mysql.service assumed) — gates
   the §8.5 properties.
4. (focus 5, 12) pxc_maint_transition_period default: RESOLVED = 10s (sys_vars.cc:8642);
   remaining question — do container entrypoints/packaged my.cnf override it?
5. (focus 5) evs.max_install_timeouts: RESOLVED = 3 in code (defaults.cpp:53) vs 1 in
   vendored docs (wsrep-provider-index.rst:249) — file the doc/code discrepancy; check
   SHOW at runtime (MTR overrides to 1).

Mechanism questions:
6. (focus 1) handle_local_monitor_interrupted returns BF_ABORT without cancelling the local
   monitor — if replay never runs (node leaves, SST_CANCELED), is local_monitor_
   permanently blocked?
7. (focus 1, 2) sst_mutex_ release window vs sst_received()/view-change race; SavedState
   first_time_ static vs provider deinit/reinit.
8. (focus 1, 4) Dummy-writeset degradation (3 sites in server_state.cpp) — which membership
   interleavings produce real divergence?
9. (focus 1, 5) pc.wait_restored_prim_timeout PT0S = indefinite startup wait — intended?
   Does packaging override it? If not, highest-value liveness property.
10. (focus 1) recv_q sizing from host memory in cgroup-limited containers — mis-sizing
    consequences?
11. (focus 1, 8) usleep busy-loops (replicator_smm.cpp:496,:733,:822;
    gcs_action_source.cpp:80) — livelock under sustained faults?
12. (focus 1) IST version non-negotiation during rolling upgrade — clean SST fallback or
    hard joiner failure?
13. (focus 1) wsrep_commit_empty assertion (transaction.cpp:555-560) enumerates 5 known
    exceptions — find the 6th.
14. (focus 2) gcache.page.* cleanup anywhere else? rsync SST truly dead in 8.4? clone-SST
    double-restart (--wsrep_recover inside wsrep_sst_clone.sh:1199) needs its own pass.
15. (focus 2) The abort() sites in replicator_str.cpp (:177,:946,:1010,:1095,:1290,:1324,
    :1375,:1420,:1449) — genuinely unrecoverable, or convertible faults?
16. (focus 3) Stale skipped_seqnos across source binlog rotation — runtime experiment
    (rotate source binlog under 4+ workers with filtered/skipped events).
17. (focus 3) The 2.7 SR+TOI lock-order inversion — construct the full cycle.
18. (focus 3) TOI double-enter of the async monitor (believed not; unverified);
    co_mode_ NO_OOOC — confirm no build variant differs.
19. (focus 4) wsrep_certification_rules cluster-uniformity: negotiated or can nodes
    disagree? No negotiation path found.
20. (focus 4, 6) Which errors/warnings besides 1681 vary per node (locale, privileges,
    timing)? Feeds the vote-divergence generator.
21. (focus 4, 10) Window between divergence and vote detection: can clients read divergent
    committed data inside it? (Also: force_sst_after_inconsistency=OFF — what exactly does
    the evicted node do on next startup?)
22. (focus 5) IST 10s watchdog — per-message or whole-IST deadline; intentional
    fail-fast-to-restart or bug (it aborts the process, doesn't fall back to SST)?
23. (focus 6) Complete enumeration of statements whose applier MDL footprint exceeds their
    cert-key set (from sql_parse.cc + service_wsrep.cc wsrep_append_fk_parent_table & co) —
    the generator for pattern A.
24. (focus 6) PXC-4845 residual: is grastate/SE divergence still reachable? Can a node loop
    crash→recover→crash? Does the PXC-5286 XID V2 format change the frequency?
25. (focus 6) PXC-5244/PXB-3818: is lock-ddl=REDUCED still broken with SST in 8.4.10;
    reachable via other config (PXC-4559)?
26. (focus 6) PXC-4498 group-commit-queue removal — what XID-ordering invariant remains for
    sys_header writes? No replacement guarantee implied by the commit.
27. (focus 6, 4) Mixed-version clusters (PXC↔Codership at V7; 8.0/8.4) are divergence-prone
    — in scope for the harness?
28. (focus 7) Percona-internal CI composition (big-test? debug builds? flake policy) — ask
    the customer; changes the duplication calculus. Does test_seesaw/sqlgen live on in
    their lab?
29. (focus 7) dbsim buildability against the current provider ABI.
30. (focus 8) gcs_group.cpp:1184-1198 vote NULL-deref — reachable from a well-behaved peer
    or only via fuzzed/mixed-version VOTE messages?
31. (focus 8) Empty-string vote collision (two different failures, identical empty vote) —
    read compute_vote fully before writing the property.
32. (focus 8, 10) DONOR keeps wsrep_ready=ON — verify live; health checkers key off it.
    Does wsrep_local_state stay 4 during FC pause (health-check accuracy property)?
33. (focus 9) DNS re-resolution absence — confirm empirically (restart a peer with a new
    IP). wsrep_notify_cmd blocking scope — which thread runs log_view; does gcomm keep
    servicing EVS while blocked (decides node-eviction vs cluster-wide-stall outcome)?
34. (focus 9) ALTER INSTANCE RELOAD TLS not covering the Galera channel — doc vs behavior
    discrepancy; socket.ssl_reload effect on established connections.
35. (focus 11) Does anything besides process_apply_error cast votes? If not, there is
    provably no in-SUT mechanism to detect silent divergence — external checksums are
    mandatory, not optional.
36. (focus 11) evs.auto_evict=0 default: interaction of permanently-delayed nodes with
    delay_margin/delayed_keep — view churn forever?
37. (focus 12) innodb_page_size != 16384 on a PXC node: rejected at startup or silent
    misbehavior? No enforcement found.
38. (focus 12) Does the hidden post-SST mysqld write binlog/GTID events (depends on my.cnf
    log_bin visibility to it)? wsrep::id stream operator — can non-UUID raw names reach the
    notify shell (read wsrep-lib/src/id.cpp)?
39. (focus 6, 12) Why is wsrep_provider READ_ONLY in PXC (blocks 8 disabled tests); does
    the constraint bind an Antithesis harness that drives provider *options* (not the
    provider path) at runtime? (Options remain dynamic — only the .so path is fixed.)
