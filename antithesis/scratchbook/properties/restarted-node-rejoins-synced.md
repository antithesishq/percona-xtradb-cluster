---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# restarted-node-rejoins-synced — A restarted node rejoins and reaches SYNCED within a bound

**Focus area:** Lifecycle transitions — rolling restart / node join lifecycle
(joiner → IST or SST → JOINED → SYNCED), kill-during-IST.

## Claim under test

After a single node is stopped (gracefully or by kill) and restarted while the rest of the
cluster keeps serving load, the node rejoins the group, completes state transfer (IST when
some donor's gcache covers its seqno range, SST otherwise), and reaches
`wsrep_local_state=4` (Synced) / `wsrep_ready=ON` within a bounded time after faults heal.
No restart sequence may leave the node permanently wedged in
JOINER/JOINED/non-Primary, permanently looping SST, or up-but-at-the-wrong-position.

## Code paths (verified at commit f9ecb3e; galera paths in percona-xtradb-cluster-galera/)

Join pipeline and its known cliffs:

- **IST vs SST decision (joiner)**: `galera/src/replicator_str.cpp:1451-1527` — IST range
  computed as `last_committed()+1 .. cc_seqno`; logs "Receiving IST: N writesets" (:1471)
  and completion marker "IST received: uuid:seqno" (:1516). `prepare_state_request`
  (:861-947): **in-flight NBO nulls the SST request** ("Node can receive IST only",
  :870-884) — donor unable to serve IST then means joiner `abort()`.
- **Donor-side IST→SST fallback**: `replicator_str.cpp:555-597` (`process_state_req`) —
  `gcache_.seqno_lock(first)` throws `NotFound` → log "IST first seqno ... not found from
  cache, falling back to SST" → `goto full_sst`. This is the **canonical distinct-outcome
  instrumentation point** for "SST fell back from IST".
- **grastate blanking around transfer**: `replicator_str.cpp:1423-1438` — before IST the
  joiner writes `seqno=UNDEFINED` + `mark_safe()` with in-code comment "if node gets killed
  during IST, it may recover to incorrect position"; `:1546-1560` resets seqno to -1 again
  after success (PXC-only). **CORRECTED (investigation):** this blanking is conditional on
  `unsafe` (i.e. a full SST was requested; `else if (unsafe)`, PXC-4631) — **IST-only joins
  keep the old grastate position through IST**, so kill-during-IST-only-join restarts at a
  stale position (see Open questions / Investigation Log for the safety consequence).
- **IST watchdog abort**: `galera/src/ist.cpp:356-365` — `SocketWatchdog` hardcoded
  `timeoutMs = 10000`; any stall >10s per receive kills IST. `recv_IST` catch block
  `replicator_str.cpp:1678-1717`: "Receiving IST failed, node restart required" →
  `st_.mark_corrupt(force_sst_after_inconsistency_)` + `abort()`. With
  `repl.force_sst_after_inconsistency` default OFF (`galera/src/replicator_smm_params.cpp:50`)
  grastate is NOT removed — the restarted node retries from whatever state remains.
  **Abort-then-restart is the designed IST failure handling; the property is that the retry
  converges rather than loops.**
- **JOINED→SYNCED gate**: `gcs/src/gcs.cpp:691-716` (`gcs_send_sync_begin`) — SYNC sent only
  when `lower_limit >= queue_len`; sustained load can hold a joiner in JOINED indefinitely
  (liveness bound must be evaluated after load/faults quiesce).
- Unbounded retries that turn transient faults into permanent wedges: STR send retry loop
  `replicator_str.cpp:955-1050` (usleep(1s) forever on EAGAIN/ENOTCONN; -EDEADLK only when
  the local monitor window (65536) fills); joiner SST condvar wait with no timeout
  `sql/wsrep_sst.cc:856-862`; SST script waits bounded only in bash
  (`scripts/wsrep_sst_common.sh:1189-1193`).
- Restart supervision context: shipped systemd units use `Restart=on-abort` +
  `RestartPreventExitStatus=SIGABRT` — every galera `gu_abort()`/`abort()` above is
  *excluded* from auto-restart in the field (sut-analysis §8.5). The harness must decide to
  restart these processes for the liveness property to be meaningful.

## Failure scenario

Realized bugs in exactly this pipeline: PXC-4845 (grastate ahead of SE checkpoint →
IST skipped with monitors uninitialized → cluster-wide stall), MDEV-36621 (donor purged
locked gcache buffers → "IST didn't contain all write sets"), PXC-4631 (grastate marked
unsafe too early), K8SPXC-1724 (SST killed mid-flight), gcache donor-selection stale
low-water snapshot (`gcs/src/gcs_group.cpp:1789-1845`, PXC zeroes the IST safety gap at
:1817). A kill timed inside IST, inside SST post-processing, or between grastate blanking
and transfer start is the Antithesis-shaped input; the observable failure is a node that
never returns to Synced, loops SST forever, or (worst) reports Synced at a divergent
position.

## Suggested implementation

- **Workload-side (primary)**: after each restart event, at final quiescence assert
  `Always`: "every node reports wsrep_local_state=4, wsrep_ready=ON,
  wsrep_cluster_size == N, and wsrep_last_committed within delta of the cluster max,
  within T of fault-heal". Track per-node restart counts to catch crash-restart loops.
- **Sometimes markers (missing SUT instrumentation; distinct lifecycle outcomes, each a
  unique assertion message)**:
  - "joiner completed IST" — at the `IST received:` marker, `replicator_str.cpp:1516`;
  - "donor fell back from IST to SST" — at the `NotFound` catch, `replicator_str.cpp:591-597`;
  - "joiner completed full SST" — at `sst_received` success;
  - "node was killed during IST and subsequently recovered" — pairs the recv_IST abort path
    (:1678-1717) with a later successful join of the same node (workload-side correlation);
  - "cert index preloaded without IST" (:1479 "Cert. index preload up to").
  These are outcome markers, not path-entry markers — they let Antithesis confirm all
  branches of the join lifecycle are exercised and give replay anchors.
- **SUT-side Always (missing)**: at join completion, `sst_seqno_ >= cc_seqno` and the
  grastate file seqno is -1 (the PXC invariant at :1546-1560).

## Assertion type

Liveness — implemented as a workload-side `Always` on the bounded convergence condition
evaluated during the final quiescent validation phase (Antithesis convention for
"eventually within a bound"). The Sometimes markers are companion assertions with their own
messages, used as exploration hints; they are not the property.

## Fault requirements

**Requires node termination (kill/restart) for full value — flag: often disabled by
default.** Without it, network partitions still produce IST rejoin cycles (node drops
non-Primary, rejoins, IST catch-up), covering the IST half; the SST half, kill-during-IST,
and wsrep-recover paths need process kill + restart (harness restart policy or
workload-driven `kill -9` inside the container). Docker stop-grace interplay is covered by
`graceful-shutdown-bounded`.

## Confidence

High — the pipeline, its abort sites, and its unbounded waits are all read directly; the
bug history (PXC-4845/4631, MDEV-36621) shows this exact property failing in the field.

## Open questions

None — all three resolved by code investigation (see Investigation Log). Two findings feed
back into the property:

- The 10s IST watchdog is **per-message**; any single >10s receive stall aborts the joiner.
  The liveness bound must tolerate ≥1 abort+restart cycle and count consecutive IST-abort
  loops as failure (as the catalog already anticipated).
- **Safety companion finding:** for IST-only joins (`unsafe == false`, the PXC-4631
  optimization) grastate is deliberately NOT blanked before IST. Sequence: graceful shutdown
  (grastate = real position P) → restart → IST-only join → kill -9 during IST apply →
  restart: mysqld_safe skips wsrep-recover for non-(-1) grastate (mysqld_safe.sh:267-279)
  AND galera prefers a non-(-1) grastate seqno over the app-recovered position
  (replicator_smm.cpp:263-290) → node restarts claiming P while InnoDB is at P+k → the next
  IST re-delivers and re-applies P+1..P+k. No server-side reconciliation for
  grastate-behind-SE-checkpoint was found. This is the liveness→safety conversion the
  question feared, realized on the IST-only path. The workload's cross-node checksum /
  expected-value oracles (ist-overlap-writesets-not-reapplied) and
  grastate-se-checkpoint-agreement are the detectors; the trigger needs node termination
  plus a graceful-restart-then-kill-mid-IST sequence.

### Investigation Log

#### Is the recv_IST 10s SocketWatchdog per-message or a whole-IST deadline?

- Examined: `galera/src/ist.cpp:355-436` (SocketWatchdog impl: expire_cnt = timeoutMs/10,
  10ms cv waits; start() sets restart_=true), `:503-513` (usage in Receiver::run).
- Found: `watchdog.start()` is called immediately before each `p.recv_ordered(*socket, ret)`
  and `watchdog.stop()` immediately after (:509-513); start() restarts the timer loop. The
  10000ms default is hardcoded at the constructor (:359). The watchdog is therefore armed
  per received action and does not cover queue-push or apply time.
- Not found: any config knob for the timeout; any whole-transfer deadline.
- Conclusion: RESOLVED — per-message. Throttling that keeps inter-message gaps <10s cannot
  fire it; a single >10s stall does, and abort-then-restart is the designed handling
  (recv_IST catch → mark_corrupt → abort, replicator_str.cpp:1678-1717). Bound/wedge
  definition updated in the bullet above.

#### With force_sst=OFF, does every abort path run after grastate blanking?

- Examined: `replicator_str.cpp:1141-1162` (unsafe = sst_req_len != 0 && !trivial;
  mark_unsafe before sending request), `:1408-1448` (PXC `else if (unsafe)` blanking branch
  + in-code comment :1145-1153: IST-only keeps the saved state "to prevent unnecessary SST
  after node restart (if IST fails before it starts applying transaction)"), `:1546-1560`
  (post-success reset to -1, "default operating state"), `replicator_smm.cpp:263-290`
  (startup trusts non-(-1) grastate; app-recovered seqno used only when grastate seqno is
  UNDEFINED), `scripts/mysqld_safe.sh:255-300` (skips wsrep-recover when grastate seqno
  != -1), `replicator_smm.cpp:590-670,1895-1985` (apply-path mark_unsafe only for TOI/NBO),
  greps for SE-checkpoint-vs-start-position reconciliation in sql/ and wsrep-lib (none).
- Found: SST-path joins are covered — `st_.mark_unsafe()` writes UNDEFINED:-1 before the
  request (:1155-1161) and the pre-IST reset writes (uuid, UNDEFINED, safe) (:1423-1438), so
  any kill recovers via wsrep-recover → SE checkpoint. IST-only joins are NOT covered: the
  blanking branch is `else if (unsafe)` (PXC-only conditionalization), so grastate keeps the
  old position P through IST apply; regular (non-TOI) IST applies never mark unsafe.
- Not found: any code comparing grastate-behind-SE-checkpoint at startup (the PXC-4845 fix
  covers only grastate *ahead*); any sql-layer skip of already-applied writesets.
- Conclusion: RESOLVED — abort paths on the SST route are safe; the IST-only route has a
  real stale-position restart window (safety companion noted above). The prior `(partial)`
  tag is superseded: blanking does NOT happen for IST-only joins, by design.

#### How often does donor purge run between donor selection and IST service under load?

- Examined: `gcs/src/gcs_group.cpp:1788-1846` (group_find_ist_donor: stale low-water
  snapshot, safety gap = range/128 capped 1M, PXC zeroes gap for IST-only),
  `replicator_str.cpp:540-600` (process_state_req re-checks via `gcache_.seqno_lock(first)`;
  NotFound → "falling back to SST" → `goto full_sst`), `certification.cpp:1367-1381`
  (set_trx_committed → safe-to-discard → service thd `release_seqno` drives continuous
  gcache purge under load; PXC adds gcache page-retention cleanup).
- Found: purge runs continuously under write load (per commit-cut reporting), so the
  selection→service window is raced at any nonzero write rate. Consequence is bounded and
  observable: donor re-validates at service time; a purged first-seqno produces the
  IST→SST fallback (:591-597, the catalog's canonical marker). Only when the joiner's
  request was IST-only (e.g. NBO in flight nulled the SST request) does the fallback become
  donor -ENODATA → joiner abort (replicator_str.cpp:1004-1011).
- Not found: an in-code frequency; it is workload-dependent.
- Conclusion: RESOLVED for catalog purposes — mechanism and consequence pinned; the exact
  rate is a run-time calibration measured via the :591-597 fallback marker. Workload
  guidance: sustained writes during joins suffice; no special aggressiveness needed.

## Synthesis refinement (2026-09-10)

Additions: (1) the PXC-4631 arm needs a targeted graceful-restart-then-kill-mid-IST sequence — now constructible via the workload->supervisor kill channel (steerable timing). (2) GU_DBUG_SYNC (galera release-usable provider sync points, sut-analysis 10.3) is an available precondition-manufacturing mechanism for the IST/donor race shapes here — availability in the PXC build UNVERIFIED; one runtime SET answers it.
