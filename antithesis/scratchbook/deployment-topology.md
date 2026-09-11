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

# Antithesis Deployment Topology — Percona XtraDB Cluster 8.4.10

## Summary

**Topology: 4 containers — 3 full PXC mysqld nodes + 1 workload client. No proxy, no
garbd, no async replica in v1.**

```text
                      +---------------------------+
                      | workload (client)         |
                      | Antithesis Python SDK     |
                      | test template + checkers  |
                      +------+-------+-------+----+
                             | 3306  | 3306  | 3306   (direct per-node SQL)
                             v       v       v
      +---------------+   +---------------+   +---------------+
      | pxc-node1     |<->| pxc-node2     |<->| pxc-node3     |
      | mysqld+galera |   | mysqld+galera |   | mysqld+galera |
      +---------------+   +---------------+   +---------------+
        full mesh on 4567 (gcomm), 4568 (IST), 4444 (SST)
```

Three mysqld nodes is the minimal topology that covers everything on the target list:

- **Quorum / split-brain**: weighted quorum (pc_proto.cpp have_quorum/have_split_brain)
  needs 3 voting members to have a majority-vs-minority partition at all. With 2 nodes,
  any partition or single node loss makes both/one side non-primary — no interesting
  quorum decisions, only total outage.
- **Inconsistency voting**: a vote needs a majority to out-vote the deviant node
  (gcs_group.cpp group_recount_votes). 3 data-bearing voters is the minimum where
  "minority is ejected" is a real decision.
- **SST/IST donor choice**: with 3 nodes, a joiner has a donor *and* the cluster keeps
  primary during the transfer; donor-selection logic (group_find_ist_donor, stale
  gcache low-water snapshot races) is only reachable with ≥2 potential donors online.
- **Galera replication / certification / BF aborts / flow control**: reachable with 2
  nodes, but 3 nodes adds nothing in cost that isn't already paid for the above, and
  matches the in-repo galera_3nodes suite and Percona's documented minimum
  recommendation (docs.percona.com/8.4: minimum 3 nodes to avoid split-brain).

Rejected alternative — 2 mysqld + garbd: saves one mysqld's memory but garbd is
data-less, so it removes the 3-way cross-node consistency oracle (our single most
important external check — the SUT cannot detect successful-but-different apply, see
sut-analysis §6.3), removes donor choice (only one possible donor), and adds a second
image/protocol variant to maintain. The arbitrator path is worth a later variant, not
the baseline.

Key decisions (detail below):

| Decision | v1 choice |
|---|---|
| Image source | Custom Dockerfile building this repo via `build-ps/build-binary.sh` (no runtime Dockerfile exists in-tree; official `percona/percona-xtradb-cluster` image noted as fallback) |
| Build type | Start with **assert-enabled** build (RelWithDebInfo with NDEBUG removed, or Debug); add pure-release (NDEBUG) image later — both eventually |
| Cluster TLS | `pxc_encrypt_cluster_traffic=OFF` for v1 |
| SST | `wsrep_sst_method=xtrabackup-v2` (bundled PXB in `pxc_extra/pxb-8.4` + socat in image) |
| Appliers | `wsrep_applier_threads=4` |
| gcache | `gcache.size=16M` (small, so IST/SST boundaries are reachable) |
| Failure-detector timers | **Real defaults** — do NOT copy MTR's relaxed timers |
| sync_wait | Server default `wsrep_sync_wait=0`; workload sessions set it per property |
| Node supervision | Entrypoint supervisor loop (mysqld restarts in-container after SQL `SHUTDOWN`/crash, with the `--wsrep-recover` dance) |
| Stop semantics | `stop_grace_period: 90s` in compose; `pxc_maint_transition_period` left at default 10 |
| Workload SDK | Python (Antithesis Python SDK); C/C++ SDK instrumentation of mysqld/libgalera is a later phase |

## Per-container specification

### pxc-node1, pxc-node2, pxc-node3 (role: service — the SUT)

- **Container names**: `pxc-node1`, `pxc-node2`, `pxc-node3` (replica count 3, one
  mysqld process each; separate containers so Antithesis can fault them independently
  and partition between them).
- **Image source**: **new Dockerfile** (e.g. `antithesis/pxc-node/Dockerfile`). The repo
  ships no runtime Dockerfile — `packaging/rpm-docker/` is an RPM *build* environment
  (spec + my.cnf template only), `build-ps/` builds tarballs/RPMs. Two-stage build:
  1. *Build stage*: `build-ps/build-binary.sh` against this checkout (it already
     handles cmake invocation, bundles percona-xtrabackup into `pxc_extra/pxb-8.4/`,
     and supports `Debug` vs `RelWithDebInfo`).
  2. *Runtime stage*: minimal OS base (oraclelinux/ubi9 or debian, matching Percona's
     supported platforms) + the tarball + runtime deps for SST:
     `socat` (fatal if missing — wsrep_sst_common.sh:176-190), `openssl`, `diff`,
     `pv`, `lsof`, `procps`, plus the bundled PXB (`pxc_extra/pxb-8.4` symlinked where
     wsrep_sst_xtrabackup-v2.sh expects it).
  - **Alternative (fallback / fast bootstrap): official `percona/percona-xtradb-cluster:8.4`
    Docker Hub image** (maintained by Percona in the percona-docker GitHub repo;
    referenced from docs.percona.com/8.4 "Running Percona XtraDB Cluster in a Docker
    Container"). Pros: known-good entrypoint, cluster bootstrap logic, PXB+socat
    already installed. Cons that rule it out as the primary path: (a) it ships the
    published release binaries, **not this commit** (f9ecb3e is branch tip and
    includes fixes like PXC-5208 dated after the last release — we would test the
    wrong code); (b) NDEBUG release only — ~200+ internal invariants compiled out
    (sut-analysis §11 NDEBUG meta-issue), no assert oracles; (c) no way to add
    Antithesis compile-time coverage instrumentation; (d) its entrypoint
    auto-generates config we'd immediately override. It remains useful for a quick
    smoke-test of the compose skeleton before the source build is ready.
  - **Instrumentation / build variants — THREE image tiers (clarified at evaluation
    synthesis: assert-enabled ≠ DBUG), start with assert-enabled**:
    - *v1: assert-enabled image.* Preferred flavor: RelWithDebInfo with `-DNDEBUG`
      stripped (top-level CMakeLists.txt:1551-1554 and galera cmake/compiler.cmake
      force it in non-debug builds — patch or override via CFLAGS), giving optimized
      code with live `assert()`.
      **Important: stripping NDEBUG does NOT enable DBUG** — `DBUG_OFF` remains
      defined in non-Debug builds, so DBUG keywords, DEBUG_SYNC, and the wsrep-lib SR
      crash points stay unavailable on this tier.
      Rationale for starting here: assertions are the densest free oracle in this SUT
      (the entire transaction/monitor/certification invariant surface is assert-only),
      and Antithesis's value is oracle density per CPU-hour.
    - *DBUG tier: plain `CMAKE_BUILD_TYPE=Debug` image* (build-binary.sh `--debug`) —
      the ONLY tier with DBUG keywords, DEBUG_SYNC, the DBUG multi-major knob, and the
      wsrep-lib SR crash points. Accepts the slowdown; all timing bounds must be
      re-calibrated per tier (see calibration-run note below).
    - *later: release image (NDEBUG)* — field behavior; needed for properties about
      what *ships* (illegal state transitions applied anyway, monitor corruption paths
      that only exist when asserts are compiled out, the vote pipeline un-preempted by
      asserts). Catalog properties are tagged per tier in property-catalog.md
      "Phasing and variants".
    - *later: Antithesis coverage instrumentation* — C/C++ instrumentation (libvoidstar)
      is compile-time, which is exactly why we build from source; instrument `mysqld`,
      `libgalera_smm.so`, and eventually the SST shell paths (bash not instrumentable;
      the C++ side is). Thread-pausing faults require this instrumentation.
    - *Core dumps*: `gu_abort()` calls `setrlimit(RLIMIT_CORE,0)` +
      `PR_SET_DUMPABLE,0` (galerautils gu_abort.c:29-58), so every Galera
      self-destruct dies core-less. Since we build from source, patch gu_abort.c to
      skip the suppression in the test image (a container-level `ulimit -c unlimited`
      is NOT sufficient — the suppression is in-process). Note the patch in the image
      so it's not mistaken for field behavior.
- **What it runs**: one `mysqld` under a small entrypoint supervisor loop:
  1. First boot on node1 only: initialize datadir, start with `--wsrep-new-cluster`
     (guarded by a marker file so a *restart* never re-bootstraps — re-bootstrapping
     an already-formed cluster is the split-brain bug we want to *find*, not cause by
     harness accident).
  2. All boots on nodes 2/3 and subsequent boots on node1: run the recovery dance
     (`mysqld --wsrep-recover` → parse `Recovered position` → start with
     `--wsrep_start_position=...`), i.e. reproduce what `scripts/mysqld_safe.sh` /
     mysql-systemd do. (Note: the once-suspected shipped log-grep bug is RESOLVED —
     the shipped `mysql-systemd galera-recovery` flow greps the bracketed `[WSREP]`
     form that actually prints; the broken unbracketed-grep scripts are vestigial and
     unshipped. See catalog `crash-recovery-grep-yields-true-position`. Because the
     supervisor replaces the shipped wrapper, that property and
     `cluster-identity-single-lineage` are deferred to a future **field-faithful
     supervisor variant** that runs the shipped flow instead.)
  3. Loop: when mysqld exits (SQL `SHUTDOWN`, `mysqladmin shutdown`, gu_abort,
     unireg_abort), the supervisor restarts it after a short delay. This gives the
     workload graceful stop/restart cycles via test commands **without requiring
     node-termination faults** (disabled by default — see Faults note below). It
     deliberately diverges from the shipped systemd policy
     (`RestartPreventExitStatus=SIGABRT` + exit 1 → inconsistency-aborts stay down in
     the field); revisit per-property once crash-recovery properties are active.
  4. **Workload→supervisor crash channel (added at evaluation synthesis)** — the v1
     path to *ungraceful* death without tenant termination faults, and steerable in a
     way platform faults are not (e.g. kill-mid-IST): the supervisor polls a
     node-local marker (a file dropped via a node-side test-command script, or an
     SQL-visible marker table row naming the node and an optional trigger condition)
     and on match executes `kill -9` on mysqld, then re-enters the normal
     restart/recovery loop. Companion **node-side kill test-command scripts** live on
     the pxc-node containers (`/opt/antithesis/test/v1/pxc/` singleton commands per
     node) so the composer can kill a specific node at a chosen moment.
  5. **Supervisor extensions (added at evaluation synthesis)** — the supervisor is the
     harness's only file/PID-level observer, so it also:
     - runs a **boot-time grastate/`--wsrep-recover` probe** on every boot (near-free —
       it already runs the recovery dance) and emits the parsed grastate fields +
       recovered position as **JSONL** for the workload/SDK to consume
       (`grastate-se-checkpoint-agreement`, `no-dual-bootstrap` per-boot emission +
       workload epoch ledger);
     - **(gap-fill) evaluates post-graceful-shutdown grastate against the DDL episode
       ledger**: after every graceful mysqld exit, if the workload's DDL ledger shows
       all NBO episodes terminated, grastate must carry a real UUID and seqno != -1 —
       an on-disk UNDEFINED:-1 there is the NBO `unsafe_`-counter leak observable
       (`toi-nbo-ddl-completes-or-fails-cleanly`), which
       `grastate-se-checkpoint-agreement` cannot see (seqno=-1 is a legal branch for it);
     - tails the error log as a **log-tail watchdog** for fatal markers (feeds
       `fatal-node-terminates-no-zombie` and the shared log-string scan layer);
     - emits **gcache file metrics** (gcache.page.* counts/bytes vs incarnation
       high-water) for `gcache-page-files-bounded`;
     - emits **boot-phase markers** (recover-pass start/end, mysqld exec, SQL-port up)
       so all-down/pc.recovery waits are attributable rather than unattributed
       timeouts;
     - honors a **hold-down knob** (marker file: do NOT restart mysqld until cleared)
       so simultaneous all-nodes-down scenarios are constructible instead of racy
       under restart-on-exit;
     - emits a **restart-accounting record** (JSONL) for every mysqld exit it observes:
       exit status/signal, graceful-vs-crash classification, and a **field-restart
       classifier** — whether shipped systemd (`Restart=on-abort` +
       `RestartPreventExitStatus=SIGABRT`; `unireg_abort(1)` exit-1 likewise excluded —
       sut-analysis §8.5) would have restarted this death. The workload/triage layer
       joins these records with per-liveness-property **time-to-detection /
       time-to-recovery buckets** so reports can say "field would be permanently down
       here" (catalog Shared conventions, "Run accounting").
     For *live* nodes the workload's log source is **`performance_schema.error_log`
     over SQL** — no file access needed from the workload container.
- **Network connections**: full mesh node↔node on 4567/tcp (gcomm), 4568/tcp (IST),
  4444/tcp (SST); accepts 3306/tcp from the workload container. Port 9200
  (clustercheck) not exposed in v1 (no proxy; the workload can run
  `scripts/clustercheck.sh` semantics itself via SQL).
  Addressing: give each node a **static IP** in the compose network and list peers by
  those IPs (or stable names resolved once). Rationale: gcomm resolves addresses
  exactly once at connect() and retries stale IPs forever (sut-analysis §8.3) — with
  Antithesis restarts potentially changing IPs, dynamic addressing would turn every
  restart scenario into the same DNS finding. Static IPs make that behavior a
  deliberately-chosen later target instead of ambient noise.
- **Volumes/state**: datadir (grastate.dat, gvwstate.dat, galera.cache, InnoDB) inside
  the container filesystem — Antithesis snapshots container state; no external volumes.
  Size the image filesystem so gcache (16M) + SST staging fits; note gcache and SST
  tmpdir live in the datadir (ENOSPC there kills IST donation — keep as a later
  deliberate fault, not an accident).
- **Stop semantics**: compose `stop_grace_period: 90s`. The SIGTERM handler first
  sleeps `pxc_maint_transition_period` (default **10s**, sys_vars.cc:8638-8642) before
  starting shutdown; Docker's default 10s grace guarantees SIGKILL at the *start* of
  every graceful stop → unclean shutdown → grastate seqno -1 → forced SST
  (sut-analysis §8.5). We keep the variable at its field default (the 10s sleep is
  itself an interesting behavior) and fix the harness side with a long grace period.

### workload (role: client — test driver)

- **Container name**: `workload` (replica count 1).
- **Image source**: new Dockerfile — slim Python base + `mysql` CLI client +
  `mysqlclient`/PyMySQL + the **Antithesis Python SDK**.
  - SDK language choice: **Python** — fastest iteration for SQL-driven workloads,
    first-class Antithesis SDK (assertions, random, lifecycle `setup_complete`). Go is
    the acceptable alternative if we later need high-concurrency load generation from
    one process. The SUT itself is C++; SUT-side instrumentation would use the
    **Antithesis C/C++ SDK (libvoidstar)** — explicitly a later phase (requires the
    source-build image above; no workload code depends on it).
- **What it runs**:
  1. Entrypoint: wait-for-cluster readiness loop (below) → seed schema, users, and the
     checksum-oracle bookkeeping tables → emit `setup_complete` (SDK lifecycle) →
     sleep forever.
  2. Hosts the test template at `/opt/antithesis/test/v1/pxc/` (test commands:
     parallel DML/DDL drivers, per-node targeted operations, `eventually_`/`finally_`
     cross-node checksum comparison, graceful-restart drivers via
     `mysqladmin shutdown` against a chosen node, self-isolation drivers via
     `SET GLOBAL wsrep_provider_options='gmcast.isolate=1'`, `pc.bootstrap`, etc.).
     Shared helpers under a `helper_` prefix inside the template directory.
     **Workload-mix requirement (gap-fill): a bulk-transaction driver** — multi-MB
     transactions (wide-row batches / LOAD-DATA-style inserts, 1-8M writesets plus an
     occasional over-`wsrep_max_ws_size` probe) sized against `gcache.size=16M`; without
     it the gcache page store is never touched and the page-store `Sometimes` guards
     (`gcache-page-files-bounded`, `gcache-crash-recovery-no-abort` page legs, the
     monitor-overflow amplifier) sit vacuous. See catalog Shared conventions, "Shared
     workload requirements".
     **Two further workload actors (gap-fill integration):**
     - **DDL issuer with an episode ledger** (`toi-nbo-ddl-completes-or-fails-cleanly`,
       feeds `gtid-executed-cluster-convergence`'s failed-TOI marker): issues TOI DDL as
       part of the baseline mix, records every DDL episode (statement, originator,
       outcome, timestamps) in a ledger the supervisor and checkers consume; the **NBO
       legs are FENCED** into their own workload phase per the poison-budget convention
       (an NBO in flight deterministically aborts joiners). The ledger is what makes the
       supervisor's post-shutdown grastate check evaluable (all-NBOs-terminated
       precondition).
     - **Backup/desync actor** (`ftwrl-backup-quiescent-or-fails`,
       `desync-ftwrl-composition-resyncs`): drives `SET GLOBAL wsrep_desync`,
       `LOCK INSTANCE FOR BACKUP`/`UNLOCK`, and FTWRL/`UNLOCK TABLES` against **one node
       at a time**, with **bounded hold durations** and every ON strictly **paired with
       an OFF** (operator-intent ledger); **fenced** per Shared conventions, since a
       desynced node sends no flow control — unfenced desync would fabricate stall
       findings in the FC/monitor family.
  3. `setup_complete` is emitted by the entrypoint **before** any test command runs;
     no `first_` command is used for readiness.
- **Network connections**: → all three nodes on 3306/tcp. Direct per-node connections
  are a *feature*: divergence checks, ER 1047 non-primary assertions, and stale-read
  properties all require knowing exactly which node answered — a proxy would erase
  that.

## Config sketch (my.cnf per node)

One shared file + tiny per-node fragment. Derived from
`mysql-test/suite/galera/galera_2nodes.cnf` / `galera_3nodes/galera_3nodes.cnf` and
`support-files/wsrep.cnf.sh`, minus MTR's distortions (relaxed EVS timers, sync_wait=15,
flush_log_at_trx_commit=2 — see sut-analysis §10.2).

```ini
# /etc/my.cnf — shared
[mysqld]
user=mysql
datadir=/var/lib/mysql
socket=/var/lib/mysql/mysql.sock
bind-address=0.0.0.0
skip-name-resolve

# --- startup-fatal PXC requirements (mysqld.cc:7306-7342; PXC-3129) ---
default-storage-engine=InnoDB
binlog_format=ROW                 # required
innodb_autoinc_lock_mode=2        # required
log_output=FILE                   # required
# innodb_page_size: leave at default 16384 — PXC requires 16KB (PXC-3129);
# do NOT set any other value.

# durability: MySQL default 1 kept (MTR always runs 2 — we deliberately don't)
innodb_flush_log_at_trx_commit=1
sync_binlog=1

# --- GTID (REQUIRED — added at gap-fill integration for
#     gtid-executed-cluster-convergence) ---
gtid_mode=ON
enforce_gtid_consistency=ON
log_replica_updates=ON
# Rationale: 8.4 defaults gtid_mode=OFF, which makes the GTID-convergence property
#   VACUOUS (no GTIDs minted on the wsrep path at all); the vendor's own galera GTID
#   tests run with exactly this trio (matches galera_gtid-master.opt). The wsrep GTID
#   plane (shared cluster sidno, per-node gno minting at group commit) only exists
#   with gtid_mode=ON.
# Side constraints: enforce_gtid_consistency restricts CREATE TEMPORARY TABLE inside
#   transactions — consistent with the property's workload rule (temp tables are a
#   known-legitimate local-GTID generator the workload avoids anyway); any
#   explicit-gtid_next workload legs must respect the PXC-4665 known won't-fix
#   avoidance rule (catalog Open Questions).
innodb_buffer_pool_size=256M      # small; Antithesis hosts are memory-constrained.
                                  # NB: gcs recv_q is sized from HOST memory,
                                  # cgroup-blind (gcs.cpp:402-418) — known distortion.

# --- wsrep ---
wsrep_provider=/usr/lib64/galera4/libgalera_smm.so
wsrep_cluster_name=antithesis-pxc
wsrep_cluster_address=gcomm://10.20.20.11,10.20.20.12,10.20.20.13
wsrep_sst_method=xtrabackup-v2    # needs bundled PXB + socat in image
wsrep_applier_threads=4           # >1: unlocks PXC-4652/4657-class bugs;
                                  # field default 1 under-exercises appliers everywhere
# wsrep_sync_wait NOT set — server default 0 (stale reads = shipped contract).
# Workload sessions SET SESSION wsrep_sync_wait per property set (0 vs 1/7).

pxc_encrypt_cluster_traffic=OFF   # v1 simplification, see justification below
# pxc_strict_mode: DECIDED (evaluation synthesis) — baseline keeps the field default
#   ENFORCING; it blocks PK-less DML, non-InnoDB DML, and LOCK TABLES
#   (sql_base.cc:6306-6350), which kills the PK-less/FK-cascade workload legs several
#   properties REQUIRE. The variable is dynamic: a dedicated workload phase/variant
#   lowers it (SET GLOBAL pxc_strict_mode=PERMISSIVE) for those legs
#   (bf-bf-lock-suppression, cross-node-row-equality PK-less leg, bf-abort-skip-awake
#   probe, MyISAM driver), then restores ENFORCING.
# pxc_maint_transition_period left at default 10 (field behavior; harness
#   compensates with stop_grace_period)
# wsrep_notify_cmd: NOT set in baseline. It is READ_ONLY (no runtime SET), so the
#   notify-cmd properties need a CONFIG VARIANT: one extra line
#   (wsrep_notify_cmd=/opt/antithesis/notify.sh, a shipped-example-style script baked
#   into the image) — the cheapest fault-composition surface in the catalog.

wsrep_provider_options='gcache.size=16M'
# repl.force_sst_after_inconsistency (PXC-5208, at HEAD): NOT set — default "no" is
#   the field/pre-fix behavior `evicted-node-rejoins-only-via-sst` tests first. The
#   option is RUNTIME-SETTABLE via SET GLOBAL wsrep_provider_options, so the opt-in
#   "yes" arm is a runtime config flip inside the fenced sabotage phase — no static
#   image/config variant needed (gap-fill integration).
# gcache.size=16M: default 128M means IST always succeeds in short runs and the
#   SST path + donor-selection races + gcache-rollover behavior are never reached.
#   16M makes the IST/SST boundary reachable under normal workload volume.
# Everything else at REAL defaults — explicitly do NOT copy MTR's relaxed
#   failure-detector timers (evs.suspect PT5S, inactive PT15S, install 7.5s,
#   max_install_timeouts=3, pc.announce PT3S...). MTR relaxes these to AVOID
#   membership churn; Antithesis wants failure detection at field settings.
```

```ini
# per-node fragment (node1 shown; nodes 2/3 substitute their IP/name)
[mysqld]
wsrep_node_name=node1
wsrep_node_address=10.20.20.11
wsrep_node_incoming_address=10.20.20.11:3306
wsrep_sst_receive_address=10.20.20.11:4444
```

Bootstrap: node1's *first* start uses `--wsrep-new-cluster` (empty `gcomm://` never
written into the config file, so restarts always use the full peer list); nodes 2/3
join via the shared `wsrep_cluster_address` and take SST/IST from node1.

Justifications for the flagged choices:

- **`pxc_encrypt_cluster_traffic=OFF`**: the default is ON and read-only, and when ON
  the bootstrap node auto-generates its own CA (ssl_init_callback.cc:506-548), SST
  needs matching certs on every node, and garbd/joiners need cert distribution. TLS
  sits *below* every subsystem we target (certification, monitors, quorum, SST
  orchestration) — it adds handshake state and cert plumbing without covering new
  target code, and it obscures what network faults do to the wire protocol. Cost: the
  §8.2 TLS findings (no peer verification, cert-rotation gap, mutually-untrusted-CA
  merge failure) are untestable in v1 — earmarked for a TLS variant image later.
- **`wsrep_sync_wait` left to sessions**: server default 0 is the shipped contract
  (stale reads permitted) and MTR never tests it; the workload runs both property sets
  by setting it per session, so no server-level choice needs to be made.
- **Timers at real defaults**: the entire MTR corpus is tuned to avoid membership
  churn (mysql-test-run.pl:4349 relaxes suspect/inactive/install/max_install); running
  real defaults under Antithesis network faults is precisely the whitespace.

## Readiness / setup_complete plan

The workload entrypoint gates `setup_complete` on **all three nodes fully Synced**:

1. Poll each node over SQL (retry loop, per-connection timeout ~5s) until, on all 3:
   - `wsrep_ready = ON`
   - `wsrep_local_state = 4` (Synced) — note JOINED→SYNCED additionally requires the
     recv queue to drain (gcs.cpp:689-716), so this is a real end-to-end signal
   - `wsrep_cluster_status = Primary`
   - `wsrep_cluster_size = 3`
   - `wsrep_local_state_uuid` identical across all 3
2. Create workload schema/users; run one round-trip write on node1 and read it back
   from nodes 2 and 3 with `wsrep_sync_wait=1` (proves the apply path end-to-end).
3. Emit `setup_complete` via the Antithesis SDK lifecycle call, then idle.

Deliberately *not* reused for readiness: `scripts/clustercheck.sh` — it returns 200
without validating the ability to commit (sut-analysis §8.7); its accuracy is a
*property to test*, not a harness dependency.

Fault-availability note (tenant config): **node termination (kill/stop) faults are
commonly disabled by default.** The crash-recovery target class (grastate/gcache/wsrep
XID recovery, torn-state SST loops — target #2 of the SUT analysis) requires ungraceful
death — flag for tenant configuration. **Mitigation (evaluation synthesis): the
workload→supervisor crash channel** (supervisor spec item 4 above) provides `kill -9`
with steerable timing without tenant changes; the platform-fault question remains open
for blast patterns the channel can't shape (see evaluation/synthesis.md, Bias 1).
*Graceful* restart cycles are driven by test commands (SQL `SHUTDOWN` against a chosen
node; the in-container supervisor restarts mysqld through the recovery dance), and
self-isolation via `gmcast.isolate=1` approximates partitions from inside the SUT on
top of Antithesis's own network faults.

Precondition-manufacturing note (evaluation synthesis): **GU_DBUG_SYNC** — galera's
release-usable provider sync points (sut-analysis §10.3, set via
`wsrep_provider_options`) can manufacture hard IST/donor race preconditions
(donor-pause-mid-IST, joiner-stall shapes) with no Debug build. Currently used by ZERO
properties. Availability in the PXC-vendored galera build is UNVERIFIED — one runtime
`SET GLOBAL wsrep_provider_options='dbug=...'` on the v1 image answers it; if present,
wire it into the SST/IST/donor property workloads.

Calibration note (evaluation synthesis): **run one calibration run per image tier
before pinning any numeric bound** (re-merge ~90s, recv-queue ~173 threshold,
drain/exit bounds, sst-ready ~220s, monitor-overflow reachability, achievable
writesets/s). Debug-tier throughput invalidates release-derived arithmetic; the first
run of each tier is the calibration run.

## Deliberately excluded (and why)

- **ProxySQL / HAProxy**: not in v1. Clients can and should connect directly — the
  workload needs per-node targeting for divergence, stale-read, and non-primary-error
  properties, which a proxy actively hides. A proxy adds a container, a health-check
  polling loop, and its own failure modes to every exploration branch. Tradeoff: the
  proxy-integration findings (clustercheck lies, pxc_maint_mode black-hole,
  ProxySQL-vs-clustercheck dual health regimes — sut-analysis §8.7) are real and
  field-relevant; they justify a *later* variant with one proxy container once the
  base harness finds its footing. The cheap middle ground available immediately: the
  workload executes the clustercheck *query* itself and asserts its accuracy against
  actual commit ability. (Synthesis note: the reimplementation needs a one-shot
  golden-equivalence check against `scripts/clustercheck.sh` semantics so the property
  tests the SUT contract, not our paraphrase of it.)
- **garbd (arbitrator)**: excluded from baseline (see Summary); a 2-node+garbd variant
  is a cheap later addition reusing the same node image (garbd ships in the build).
- **Async replication source/replica (Wsrep_async_monitor)**: the async-monitor
  subsystem is a top-5 target (young, regression-prone) but requires a 5th container
  (async source) and a distinct replication setup; it dilutes v1. Phase 2 candidate.
- **systemd inside containers**: entrypoint supervisor instead; the systemd-specific
  behaviors (RestartPreventExitStatus, log-grep recovery) are noted as test surfaces
  and partially reproduced by the supervisor deliberately.
- **A 4th/5th data node, even-size clusters**: quorum edge cases at even sizes are
  listed whitespace, but every container multiplies state space; 3 nodes covers the
  target list. Later variant if quorum properties prove fruitful.
- **Monitoring (PMM etc.), backup tooling**: not part of the code under test.
- **wsrep-lib/dbsim in-process simulator**: interesting fast inner loop, likely
  bit-rotted (sut-analysis §10.1); orthogonal to this topology — track separately.
- **Clone SST variant** (`wsrep_sst_method=clone`, new in 8.4.4): bug-dense and
  interesting, but one SST method at a time; xtrabackup-v2 is the field default.

## Assumptions

1. `build-ps/build-binary.sh` works against this checkout in a container (it is the
   documented Percona build path; PXB 8.4 tarball must be supplied to the build stage
   for `pxc_extra/pxb-8.4` bundling).
2. Stripping `-DNDEBUG` from RelWithDebInfo produces a usable binary (asserts were
   written to compile in Debug; an optimized-with-asserts build is nonstandard for
   this codebase and may hit debug-only side effects in assert expressions). Fallback
   is a plain Debug build.
3. Antithesis compose networking supports static IP assignment per container
   (standard docker-compose `ipam`/`ipv4_address`), and restarted containers keep
   their address when statically assigned.
4. The official `percona/percona-xtradb-cluster` Docker Hub image exists for 8.4
   (docs lead — verify tag availability if used for the smoke test).
5. Node-termination faults are disabled in this tenant's default webhook config until
   requested (per Antithesis defaults); graceful restarts via test commands carry the
   restart coverage until then.
6. One workload container generating load against all 3 nodes is sufficient
   concurrency to trigger certification conflicts/BF aborts (multi-session within the
   container; no need for per-node client containers).
7. 16M gcache is large enough that steady-state replication doesn't thrash SST
   constantly, small enough to overflow under sustained load — needs empirical tuning
   in the first runs.

## Open Questions

1. **Build**: does the assert-enabled (non-NDEBUG RelWithDebInfo) build boot and pass
   a basic 3-node MTR smoke locally? If Debug-only, what is the throughput penalty
   under Antithesis (affects how much workload fits a branch)?
2. **Entrypoint recovery dance**: verify the `--wsrep-recover` log-grep pattern
   question (sut-analysis open Q1 — 'WSREP:' vs '[WSREP]') while writing the
   supervisor; our implementation should parse whatever the binary actually emits.
3. **Static IPs vs restart**: confirm Antithesis-restarted containers retain their
   compose-assigned static IP; if not, the gcomm one-shot-DNS behavior becomes ambient
   in every restart scenario and needs a mitigation decision.
4. **Tenant faults — DECIDED (user, 2026-09-10)**: container-kill faults will be added
   to the fault-injector settings at some point; no immediate request. Until then, the
   workload→supervisor kill channel carries ungraceful-termination coverage and the
   tranche-2 kill-gated properties stay parked.
5. **SIGTERM path**: with `stop_grace_period: 90s`, confirm a graceful container stop
   actually yields a clean shutdown (grastate seqno != -1) — this validates both the
   grace-period choice and the 10s `pxc_maint_transition_period` interaction.
6. **gcache.size=16M**: tune after first runs — measure how often joins take IST vs
   SST; target a healthy mix rather than all-SST.
7. **Second image ordering**: when to add the pure-release (NDEBUG) image — after the
   first triage cycle, or immediately as a second environment? (Cost: doubles
   environment count; benefit: field-behavior properties.)
8. **SST user/auth**: RESOLVED — 8.4 auto-creates an internal locked
   `mysql.pxc.sst.user` for SST (sql/wsrep_sst.cc:1272-1295); no `wsrep_sst_auth` or
   manual credential setup is needed in the image.
9. **Workload SDK**: confirm Python SDK is acceptable to the customer (their stack is
   C++/bash); Go alternative costs little to switch to before the workload grows.
