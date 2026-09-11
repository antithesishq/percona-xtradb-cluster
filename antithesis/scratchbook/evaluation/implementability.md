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

# Implementability Evaluation — Property Catalog vs v1 Deployment Topology

Lens: for each property, can the invariant actually be checked given the planned v1
environment (3 PXC nodes from a custom assert-enabled image, entrypoint supervisor with
`--wsrep-recover` dance and restart-on-exit, 1 Python-SDK workload client, SQL-only
observability, static IPs, default network faults, node-termination faults
tenant-config-gated, graceful restarts via SQL `SHUTDOWN`, `gcache.size=16M`,
`wsrep_applier_threads=4`, `pxc_encrypt_cluster_traffic=OFF`, xtrabackup-v2 SST)?
Deliberately problem-biased. Code claims below were re-verified at f9ecb3e where cited.

Verified-in-this-evaluation facts used throughout:

- `pxc_strict_mode` is a **dynamic** global (`sql/sys_vars.cc:8623-8628`,
  `ON_UPDATE(pxc_strict_mode_update)`); default ENFORCING **blocks DML on PK-less
  tables** (`sql/sql_base.cc:6306-6350`), **blocks DML on non-InnoDB tables**
  (`sql/sql_base.cc:6255-6304`), and **blocks LOCK TABLES / FLUSH TABLE ... WITH READ
  LOCK/FOR EXPORT** (`sql/sql_parse.cc:1824-1852`).
- `wsrep_notify_cmd` is **READ_ONLY GLOBAL** (`sql/sys_vars.cc:8434-8437`) — cannot be
  set at runtime; a notify-cmd property needs a config/image variant.
- `wsrep_on` is SESSION_ONLY and settable (`sql/sys_vars.cc:8406-8410`,
  `sql/wsrep_var.cc:69-105`; the SUPER check is commented out) — workload-injected
  single-node divergence (`SET SESSION wsrep_on=OFF; UPDATE ...`) is constructible over
  plain SQL.
- `wsrep_trx_fragment_size` is a SESSION_VAR (`sql/sys_vars.cc:8579`) — SR sessions are
  workload-constructible without a server config variant.
- wsrep-lib and galera are vendored git submodules built in-tree (`.gitmodules`:
  `wsrep-lib`, `percona-xtradb-cluster-galera`) — SUT-side patches are *feasible* in the
  custom image build, but every proposed patch lands in the same novel Dockerfile.
- `existing-assertions.md`: zero SDK instrumentation exists — every "SUT-side (missing)"
  marker in the catalog is net-new C/C++ work, which the topology explicitly defers
  ("C/C++ SDK instrumentation of mysqld/libgalera is a later phase").

---

## A. Catalog-wide findings

### A1. Node-termination gap guts the crash-recovery third of the catalog (largest single risk)

**Scope:** all of Category 2 (7 properties, two of them top-10: `acked-commit-durable-
across-restart` #3, `grastate-se-checkpoint-agreement` #6), plus the crash arms of
`sr-fragment-cross-node-agreement`, `sr-rollback-fragment-noop`, `interrupted-sst-forces-
full-sst`, `failed-state-transfer-node-rejoins` (kill arms), `sst-grant-all-user-locked-
or-absent` (kill windows), `restarted-node-rejoins-synced` (graceful-then-kill safety
finding), `gcache-page-files-bounded` (orphan-across-restart half), `homogeneous-cert-
version-match` (gcache-recovery leg), `crash-recovery-grep-yields-true-position`
(unclean-stop branch), `no-dual-bootstrap-after-full-shutdown` (stale-flag shape),
`full-cluster-restart-reaches-primary` (ungraceful all-down).

**Problem.** The catalog's Category 2 header says "Everything here requires
node-termination faults unless noted"; the topology's Assumption 5 says those faults are
disabled in the tenant default and the *only* workload-drivable restart is graceful SQL
`SHUTDOWN`. Graceful shutdown writes a clean grastate (seqno != -1), drains appliers, and
closes InnoDB cleanly — it exercises **none** of the kill windows these properties are
built on (grastate-ahead-of-SE, torn gcache, XID regression on crash, SST kill-before-
lock, certified-fragment-in-flight). Day-one, ~14 properties — including two of the
implement-first ten — are unimplementable or reduced to their trivial branch. The catalog
flags this as an open question but its priority ordering does not reflect it.

**Additional wrinkle:** even when termination faults are enabled, they are not
*steerable* — the workload cannot request "kill node2 during SST post-processing" or
"kill inside the certify-to-commit window". Properties whose `Sometimes` guards name
narrow kill windows (`sst-grant` post-processing crossing, `acked-commit` certify-to-
engine-commit kill, `grastate-se` pause()-window kill) depend on random termination
landing in windows that are milliseconds-to-seconds wide; expect very low hit rates
without an amplifier.

**Suggested action (three concrete unblocks, in preference order):**
1. Resolve the tenant fault config *before* sequencing implementation; if enabled,
   re-rank; if not:
2. Add a **workload→supervisor crash channel**: the entrypoint supervisor (already
   planned) polls a local side channel the workload can reach over SQL — e.g. the
   supervisor tails a `crash_requests` table via the local socket, or watches for a
   workload-created named marker (`CREATE TABLE crash_now_node2`) — and executes
   `kill -9 $MYSQLD_PID`. ~20 lines of bash; makes kills both available and *targetable*
   at specific workload phases (solving the steerability problem too).
3. If the image ends up as plain `CMAKE_BUILD_TYPE=Debug` (topology's stated fallback),
   the MySQL DBUG facility is compiled in and `SET SESSION debug='d,crash_commit_before'`
   -style keywords give SQL-drivable, *positioned* crashes. Note this does NOT work for
   the topology's *preferred* flavor (RelWithDebInfo minus `-DNDEBUG`) — see A2.

### A2. Build-flavor inversion: catalog assumes release-primary, topology builds assert-enabled-primary

**Scope:** catalog-wide assumption vs topology decision; concretely distorts
`illegal-wsrep-transition-never-taken`, `inconsistency-vote-evicts-divergent-minority`,
`gcache-crash-recovery-no-abort`, `commit-cut-bounded-by-delivered-seqno`,
`monitor-window-overflow-unreachable`, `gcs-total-order-gap-free`,
`no-spurious-multi-major-detection`, `rolling-upgrade-write-gate`, and every calibrated
timing bound.

**Problem.** The catalog's Assumptions block states "primary test image is the release
(NDEBUG) build; an assert-enabled (debug) image variant is assumed available". The
topology inverts this: v1 is assert-enabled, release "later". Consequences:

- **Invariants that encode release-only semantics change meaning or go vacuous.**
  `illegal-wsrep-transition`'s premise is "release builds log 'unallowed state
  transition' and proceed anyway" — with live asserts, wsrep-lib aborts at the same
  sites, so the log-scan fallback can never fire and the proposed `Unreachable` patch is
  partially redundant with the native assert (a crash IS the signal; fine, but the
  property as written checks the wrong thing on this image). `inconsistency-vote`'s
  invariant says eviction is detected "as provider-Disconnected ... never as process
  death" — explicitly a release-build behavior; on the assert image, a deliberately
  divergent node may abort on a live assert *before or during* the vote, making "never
  process death" a designed false positive. `gcache-crash-recovery-no-abort` asserts
  recovery "completes without abort" over machinery whose strong checks are exactly the
  NDEBUG'd ones — on the assert image, tolerated-in-release torn shapes may abort *by
  design*, failing the property with a finding that doesn't reproduce in the field.
- **DBUG/DEBUG_SYNC are NOT unlocked by stripping `-DNDEBUG`.** MySQL gates DBUG on
  `DBUG_OFF` (set for all non-Debug build types) — the preferred flavor gives live
  `assert()` but no `SET debug=...` keywords, no DEBUG_SYNC, no wsrep-lib SR crash
  points. The catalog's debug-gated items (`rolling-upgrade-write-gate` DBUG knob, SR
  deterministic crash points, multi-major DBUG amplifier) and the A1 mitigation #3 all
  need plain Debug specifically. The topology text conflates the two flavors' benefits.
- **Every calibrated number assumes release throughput.** The 1-11 min
  monitor-overflow accumulation (at 100-1000 writesets/s), ~173 recv-queue threshold,
  60-90 s convergence bounds, and the FC/drain watchdog bounds were derived from code
  arithmetic at release-ish rates. Debug builds of mysqld are typically 2-5x slower, and
  the Python workload plus Antithesis scheduling throttle further. Bounds must be
  recalibrated *per image flavor* or liveness properties will false-positive (too-tight
  bounds) and reachability targets (65k gap) will silently become unreachable in budget.

**Suggested action:** add a per-property "image flavor" column to the catalog (release /
assert / plain-Debug / any); rewrite the three release-semantics invariants above with
image-conditional expected outcomes; schedule an explicit calibration run per flavor
before enabling numeric-bound liveness assertions; decide plain-Debug vs
RelWithDebInfo-minus-NDEBUG *by property need* (SR crash points + DBUG crash channel
argue for plain Debug despite the slowdown).

### A3. All "SUT-side (missing)" assertions are deferred by the topology — several properties have no day-one form at all

**Scope:** sharp forms of ~17 properties; complete blockage of 6.

The topology defers libvoidstar/C++ SDK work to a later phase, and existing-assertions.md
confirms zero instrumentation exists. Sorting the catalog by what remains without SUT-side
work:

**No meaningful day-one form (SUT-side is the whole property):**
- `bf-abort-skip-awake-victim-already-killed` — the qualified `AlwaysOrUnreachable` at
  `service_wsrep.cc:192-198` requires in-process signal-flow context; no log or SQL
  fallback exists. Unimplementable until instrumentation phase.
- `commit-cut-bounded-by-delivered-seqno` — SUT-side only by its own text.
- `gcs-total-order-gap-free` — `Unreachable`×3 inside gcomm/gcs; the EVS drop is
  *silent* in release, so not even a log fallback.
- `vote-message-payload-contract` — the NULL-deref `Unreachable` and recompute contract
  are SUT-side; only the weak "survivors consistent after eviction" workload leg remains
  (subsumed by the checksum oracle).
- `homogeneous-cert-version-match` — both `Unreachable`s live in `certification.cpp`.
- `wsrep-xid-checkpoint-monotonic` — sharp form re-enables the disabled assert in
  `trx0sys.cc`; the weaker workload proxy additionally needs kills (A1). Double-gated.

**Workload/log fallback exists but is weaker and needs harness plumbing (A4):**
`no-mdl-bf-bf-abort` (log line + exit-1 detector — viable via supervisor),
`trx-replay-never-fatal` (same funnel), `monitor-window-overflow-unreachable` (log line +
`wsrep_local_recv_queue > 65536` proxy over SQL), `illegal-wsrep-transition` (log scan;
note the transaction-side message is debug-log-level — requires `wsrep_debug` set, which
is settable but floods logs), `interrupted-sst-forces-full-sst` (boot-time
grastate probe + "Proceeding with SST" log marker), `sst-ready-message` (workload bound
only), `no-spurious-multi-major` (SQL poll of `pxc_maint_mode` vs Synced/Primary is a
genuine workload form).

**Workload form is the primary form; SUT-side is coverage garnish (fine day-one):**
`cross-node-row-equality`, `bf-bf-lock-suppression`, `privilege-context`,
`applier-threads-never-read-only`, `retry-autocommit`, `sr-rollback-fragment-noop`
(workload legs), `ist-overlap`.

**Feasibility of the proposed patches when the phase arrives:** wsrep-lib and galera are
vendored submodules compiled via the top-level build — patching is mechanically feasible
inside the custom image build. But note the concentration risk: the same first-of-its-kind
Dockerfile must (a) run `build-ps/build-binary.sh`, (b) strip `-DNDEBUG` against explicit
CMake resistance (top-level CMakeLists.txt forces it), (c) patch `gu_abort.c` core
suppression, (d) eventually apply SDK patches to two submodules and mysqld, and (e)
bundle PXB. Any one failing blocks the run. Suggested action: land (a)+(e) with an
*unpatched* build first and smoke-test, then layer patches one per image revision.

### A4. Cross-container file/log observability: the topology plans none of what nine properties assume

**Scope:** `grastate-se-checkpoint-agreement`, `no-dual-bootstrap-after-full-shutdown`,
`interrupted-sst-forces-full-sst`, `gcache-page-files-bounded`,
`fatal-node-terminates-no-zombie`, `cluster-identity-single-lineage`,
`crash-recovery-grep-yields-true-position`, log-fallbacks in A3, `gcache-recovered-
ist-completeness` (`scan()` ran non-trivially marker).

**Problem.** The workload container reaches nodes over SQL (3306) only; node datadirs are
container-internal ("no external volumes"); there are no sidecars. But the evidence files
assume file/log/process access: the grastate probe is specified "workload/sidecar-side ...
parse grastate.dat"; `no-dual-bootstrap` says "read all grastate.dat files" after a full
stop (cross-container reads at a moment when every SQL port is down — impossible from the
workload as topologized); `fatal-node-terminates` explicitly requires "an external
per-node watchdog (test composer sidecar): tail the error log ... + PID liveness";
`gcache-page-files` needs periodic `du` of `gcache.page.*`; `interrupted-sst` needs the
"Proceeding with SST" error-log marker.

**This is solvable without new containers, but it is unplanned work that belongs in the
topology document:** the entrypoint supervisor is *in* the node container with full
file/log/PID access, and it already runs `--wsrep-recover` at every boot. Extend it to:
1. **Boot-time probe** (before starting mysqld): parse grastate.dat, compare with the
   `--wsrep-recover` output it already captures (this is `grastate-se-checkpoint-
   agreement` nearly for free — the strongest quick win in the catalog), emit the
   grastate `safe_to_bootstrap` value and prior exit status.
2. **Log-tail watchdog thread**: fatal-marker regexes + PID liveness + exit-within-T
   (`fatal-node-terminates`), plus the specific fallback lines ("MDL BF-BF conflict",
   "unallowed state transition", monitor-overflow messages, "Proceeding with SST").
3. **Periodic file metrics**: gcache.page.* byte counts, datadir free space.

Assertion transport: there is **no bash SDK** — the supervisor must either write
Antithesis fallback-format JSONL to the SDK output location, or (cleaner) install the
Python SDK in the *node* image and ship a tiny probe script. Either way this is a new
image responsibility the topology must own. For *live* nodes, note that
`performance_schema.error_log` exposes the error log over SQL — the workload can do
log-grep-style checks on reachable nodes with zero node-side work; the supervisor tail is
still required for dead/wedged nodes (which is exactly when it matters).

**Residual real limitation:** cross-node *simultaneous* file state ("at most one
safe_to_bootstrap:1 across nodes while all are down") cannot be observed atomically by
anyone. Restate as: each node emits its pre-boot flag value at every boot; the workload
keeps a full-stop epoch ledger and asserts ≤1 flag among the boots following each graceful
full-stop epoch. Same strength, actually implementable.

### A5. `pxc_strict_mode=ENFORCING` (topology default) blocks required workload shapes — verified in code

**Scope:** `cross-node-row-equality` (PK-less DML companion `Sometimes` — one of its four
named hard-to-reach shapes), `bf-bf-lock-suppression-no-divergence` (its stated workload
REQUIREMENT is "PK-less FK-child tables under cascading DML"), `bf-abort-skip-awake`
(probe recipe "BF DDL against sessions holding table-level locks"),
`nonready-node-error-code-contract` (`wsrep_replicate_myisam` DML→TOI third entry point).

**Problem.** The topology keeps `pxc_strict_mode` at default ENFORCING ("field
behavior"). Verified at f9ecb3e: ENFORCING rejects DML on tables without an explicit
primary key (`sql/sql_base.cc:6306-6350`), DML on non-InnoDB tables
(`:6255-6304`), and LOCK TABLES/FTWRL-per-table (`sql/sql_parse.cc:1824-1852`). Under the
planned config, the workload cannot even *populate* a PK-less FK-child table, so the
flagship divergence property's most important generator (the silent half of bug pattern A)
is unreachable, and its `Sometimes(PK-less DML)` guard will correctly report vacuity
forever. Neither document acknowledges the collision.

**Mitigation is cheap but must be deliberate:** `pxc_strict_mode` is dynamic — the
workload can `SET GLOBAL pxc_strict_mode=PERMISSIVE` for the PK-less/LOCK-TABLES phases
(a per-node global; set on all three; PERMISSIVE keeps warnings flowing as coverage
markers). This diverges from field posture and itself perturbs behavior (strict-mode
checks vanish cluster-wide while set), so either scope it to dedicated workload phases
with restore, or run a PERMISSIVE config variant. Also note `wsrep_certify_nonPK=ON`
(default) is what makes PK-less DML *replicable*; the ENFORCING block sits in front of it.

### A6. Mid-run quiesced checkpoints under continuous faults: the per-event `Always` framings over-promise

**Scope:** `cross-node-row-equality` ("at every quiesced checkpoint"), `ist-overlap`
("after every JOINER→SYNCED"), `gcache-recovered-ist-completeness` ("after every IST
join"), `sr-fragment` ("at quiesced checkpoints and after every restart/rejoin"),
`cert-interval-reject-symmetry`, `no-dual-bootstrap` full-stop checkpoints.

**Assessment of the checksum oracle plan concretely.** The mechanism is sound and fully
SQL-implementable: pause the workload's own writers (in-process flag — trivial, single
workload container), poll `wsrep_last_committed` equal across all reachable
Synced/Primary nodes, per-node `wsrep_sync_wait` read as the barrier (verified in the
evidence file: the apply monitor is held across the full commit, so sync_wait is the
*stronger* barrier — no extra cross-node precondition), then ordered per-table checksums,
re-check `wsrep_last_committed` unchanged, compare. Exclusions (mysql.user, SST-user
rows) are per-table/per-row filters — fine.

**The gap is availability of the precondition, not the mechanism.** The workload can
pause its own writers but **cannot pause fault injection mid-run**; during an active
partition or a node's recovery there is no moment when all three nodes are Synced/Primary
with equal last_committed. So "after every IST join" degrades to "after those IST joins
that a calm-enough window follows" — the checker must be written as attempt-with-timeout
→ skip (and a skip under a live partition is correct, not a failure). The guaranteed
full-strength comparisons are the `eventually_*` / `finally_*` commands, which run with
faults paused (the platform's quiet-period / `ANTITHESIS_STOP_FAULTS` behavior) — the
terminal all-node checksum is therefore rock-solid and should be the anchor `Always`;
mid-run checks are opportunistic amplifiers that shorten time-to-detection.

**Suggested action:** reword the per-event `Always` invariants as "at every *successful*
quiesce (gated, timeout-skipped)" plus a `Sometimes(quiesce succeeded shortly after an
IST join / SR churn / …)` vacuity guard per interesting trigger, and state that the
terminal finally-checksum is the authoritative form. Otherwise implementers will either
block the workload waiting for unreachable preconditions or report false failures.

### A7. Exact expected-value oracles are unconstructible under faults; ack-journal interval accounting is required

**Scope:** `ist-overlap-writesets-not-reapplied` ("counter-table `v=v+1` expected-value
check"), `retry-autocommit-exactly-once`, `bf-replay-commits-exactly-once`,
`acked-commit-durable` (ack journal).

Under fault injection, every write ends in one of {acked, errored, unknown} (connection
dropped mid-COMMIT, node died). An *exact* expected value for a `v=v+1` counter is
unknowable — each unknown contributes 0-or-1. `first-committer-wins` already models this
correctly ("ambiguous outcomes tracked as unknown and asserted only to converge
consistently"); `ist-overlap`'s expected-value check and `retry-autocommit`'s effect
counts must adopt the same discipline: assert `acked_count <= observed <= acked_count +
unknown_count`, plus strict cross-node equality of `observed` (the sharp part — a
double-apply on one node breaks equality regardless of the interval). Unique-keyed
inserts (the ack-journal design) dodge the problem for identity checks but not for
increment counters. This is a checker-design constraint, not a blocker — flagging so the
"expected-value" phrasing doesn't get implemented literally.

### A8. Timing/budget practicality of the named bounds

- `sst-ready-message-implies-listener`: the workload `Always` is "SST completes or errors
  within T ≈ 10 min". A 10-minute open window per observation is at the edge of
  Antithesis branch budgets; with 16M gcache and a small dataset, real SSTs should
  complete in well under a minute — calibrate T down hard (the catalog's own script-side
  bounds sum to ~220 s) or the property will almost never conclude.
- `monitor-window-overflow`: the 1-11 min single-wedge accumulation assumes 100-1000
  writesets/s sustained *while wedged*. A Python workload against a Debug-flavor mysqld
  under Antithesis throttling plausibly delivers an order of magnitude less → hours, i.e.
  unreachable. The `Unreachable` tripwire itself is free (keep it); the *expectation* of
  exercising it needs the catalog's own amplifier (SET GLOBAL wsrep_desync=ON — SQL-
  drivable, good) plus a tiny-transaction flood phase, and even then treat reachability
  as aspirational on the assert image.
- Watchdog-style liveness (`flow-control-pause-releases` N-min freeze + "no view change
  during the frozen interval" evidence clause; `joiner-reaches-synced` drain bound;
  `graceful-shutdown-bounded` exit bound; `partition-heal` ~90 s): all implementable via
  SQL polling (`wsrep_monitor_status`, `paused_ns`, `wsrep_last_committed`, view seqno),
  but every one carries an unset constant with a "needs one calibration run" note — and
  no calibration run is scheduled anywhere in the plan. Suggested action: make the first
  triage cycle an explicit calibration milestone: run with watchdogs in log-only mode,
  derive bounds, then arm.

### A9. All-nodes-down scenarios are awkward under a restart-on-exit supervisor

**Scope:** `full-cluster-restart-reaches-primary`, `acked-commit-durable` flush=1
full-cluster-crash variant, `no-dual-bootstrap` full-stop checkpoints.

The supervisor restarts mysqld "after a short delay" — constructing a window where all
three are down simultaneously via sequential SQL `SHUTDOWN`s is a race against the first
node's restart; and once down, nodes are SQL-unreachable, so the workload cannot extend
the window. Additionally, during `pc.recovery`'s restored-prim wait the SQL port is
closed (catalog: wait-forever with PT0S default), so the *only* workload-observable
failure signature is "port never opens" — a bare timeout with poor attribution.
Suggested action: give the supervisor a workload-settable hold-down (same side channel as
A1 mitigation #2: "stay down N seconds" / "stay down until marker cleared"), which makes
full-stop windows deterministic; have the supervisor emit boot-phase markers (recovering /
waiting-for-prim / serving) so the timeout is attributable.

---

## B. Property-level findings not covered above

- **`crash-recovery-grep-yields-true-position` — the property now tests harness code,
  not the SUT.** The topology deliberately replaces the shipped recovery wrappers with
  its own supervisor implementation of the `--wsrep-recover` dance. The property's
  invariant ("wrapper-layer probe comparing the two") therefore validates the harness's
  own bash against mysqld output — a useful self-check, but it no longer guards the
  shipped `mysql-systemd galera-recovery` flow the property text says it guards. Either
  invoke the actual shipped script (`/usr/bin/mysql-systemd galera-recovery` ships in the
  built tarball/RPM) inside the supervisor, or note the property has become a harness
  self-test (it is already Priority: Low; this pushes it lower).
- **`cluster-identity-single-lineage` — half the invariant is vacuous by harness
  design.** The "no `--wsrep-new-cluster` in any supervisor environment" clause targets
  the persistent-EnvironmentFile poisoning vector; the topology uses a first-boot marker
  file, not a persistent env, so there is nothing to poison (the catalog's own open
  question, now answerable: no). Remaining implementable content: the runtime lineage-set
  check over `wsrep_cluster_state_uuid` (plain SQL — fine) and the safe_to_bootstrap
  vector (owned by `no-dual-bootstrap`). Trim the property or fold it in.
- **`notify-cmd-hang-does-not-block-commits` — variant-gated harder than stated.**
  Verified: `wsrep_notify_cmd` is READ_ONLY (`sys_vars.cc:8434-8437`); the workload cannot
  configure it at runtime. Needs a config variant *and* a hangable notify script baked
  into the node image. Not v1; the catalog says "needs a notify-cmd-configured variant"
  but doesn't note that this means a distinct environment, i.e. real cost.
- **`cluster-member-strings-never-reach-shell` — the sentinel design conflicts with the
  3-node baseline.** A node configured with a metacharacter-laden `wsrep_node_name` is
  *rejected by the fixed allowlists* (that's the fix under regression test) — most likely
  at startup, leaving a permanently-down third node and a degraded 2-node cluster for
  every other property in the run. Plus the side-effect sentinel (e.g. touched file) and
  the rejection-path `Sometimes` are node-local observables (A4). This wants its own
  short-lived variant environment (or a 4th sacrificial node), not a slot in the baseline
  workload. The catalog does not flag the environment cost.
- **`duplicate-gtid-skip-exactly-once` / `async-monitor-leave-mismatch-unreachable` —
  cleanly deferred, consistent.** Both require the async source→PXC topology the v1
  design excludes. No mismatch — but both carry Priority: Medium without a "not
  implementable in v1" tag; tag them so they don't enter the first implementation queue.
- **`sst-ready-message-implies-listener` rsync arm** — correctly rescoped by the catalog
  to rsync-enabled configs; note that reaching it needs *both* `wsrep_sst_method=rsync`
  and a loosened `wsrep_sst_allowed_methods` — a config variant the topology explicitly
  rejects for v1 ("one SST method at a time"). Out of v1 scope; consistent.
- **`inconsistency-vote-evicts-divergent-minority`** — the divergence injection is
  constructible (`SET SESSION wsrep_on=OFF` verified session-settable), and detection of
  the evicted node (provider Disconnected / non-Primary) is SQL-observable. Two caveats:
  the release-vs-assert behavior drift (A2), and the injection needs the workload to
  target *exactly one* node and record it in an intent ledger so the checker can assert
  "exactly the sabotaged set left" — checker bookkeeping, feasible.
- **`gcache-recovered-ist-completeness` / `restarted-node-rejoins-synced` `Sometimes`
  guards** — several guards require knowing *which* node was killed and *whether* its
  restart was ungraceful. Antithesis fault injections are invisible to the workload;
  infer via the supervisor boot probe (A4 item 1: prior-exit-status + boot counter
  emitted per boot) plus the workload's own ledger of SHUTDOWNs it issued (anything else
  = ungraceful). Feasible, but only with the A4 supervisor extension; pure-SQL inference
  (uptime resets) is racy.
- **`autoinc-identity-no-cross-node-collision`** — offsets/increments and cert conflicts
  are SQL-observable; the widest claimed window is the **X-plugin document-id
  aggregator**, which the planned SQL-only workload never touches — exercising it needs
  mysqlx-protocol sessions (Python `mysqlx` connector; port 33060). Small unplanned
  addition; without it the property silently tests only the narrow windows.
- **`sst-grant-all-user-locked-or-absent`** — the `account_locked` scan is plain SQL and
  cheap; but the gate "no SST post-processing in flight" is not SQL-observable on a
  joiner whose mysqld is not yet serving. Practical gate: assert only on nodes reporting
  Synced (and not Donor) — slightly weaker than the stated gate but implementable. The
  trigger (kill mid-post-processing) is A1-gated.
- **`donor-returns-to-synced`** — hinges on `wsrep_desync_count` being an exported status
  variable as the catalog asserts; if the name differs in 8.4.10's status output the
  direct leak oracle degrades to state polling. Verify variable name in the first smoke
  run (one `SHOW GLOBAL STATUS LIKE 'wsrep_desync%'`).
- **`graceful-shutdown-bounded`** — implementable (workload measures SHUTDOWN→port-close;
  `pxc_maint_transition_period` is dynamic for the =0 variant). One note: the property's
  most interesting field finding (Docker 10s grace SIGKILLs every stop) is *designed out*
  by the topology's 90s grace — correct harness hygiene, but record that this leg is
  deliberately not tested, and that Antithesis-driven container stops (if the tenant
  enables stop faults) would use the compose grace, not 10s.

---

## C. Passes — implementable as specified against the v1 topology

Day-one, zero SUT instrumentation, SQL-only observables, default network faults (plus
workload-driven graceful restarts where noted):

- `non-primary-rejects-writes` — ack journal + ER-1047 markers; race-free formulation;
  terminal convergence check under paused faults. The catalog's "day-one implementable"
  claim survives scrutiny.
- `first-committer-wins-loser-leaves-no-trace` — the unknown-outcome bucket is the right
  design and generalizes (A7).
- `bf-replay-commits-exactly-once` + `retry-autocommit` workload legs — effect counts
  via unique-keyed rows; `wsrep_local_replays` is a status variable; variants via
  dynamic `wsrep_retry_autocommit`.
- `cross-node-row-equality` core mechanism — barrier verified sound; gating per A6;
  PK-less leg per A5.
- `sync-wait-reads-observe-acked-writes` — per-session `wsrep_sync_wait`, pure SQL.
- `at-most-one-primary-component` — per-round polling of `wsrep_incoming_addresses`
  sets; disjointness formulation tolerates poll skew by design.
- `partition-heal-single-primary-remerge` — default network faults are its domain;
  bound needs calibration (A8).
- `flow-control-pause-releases`, `synced-node-recv-queue-bounded`,
  `local-monitor-freed-after-bf-abort`, `commit-order-monitor-released` workload legs —
  all ride the `wsrep_monitor_status` / `paused_ns` / recv-queue status variables; the
  catalog's substitution of status-variable polling for the two previously proposed
  SUT-side gauges was the right implementability call.
- `clustercheck-200-implies-write-progress` — workload runs the check's SQL semantics
  itself (topology and catalog agree); probe writes are trivial.
- `maint-mode-honors-operator-intent` — SET/poll `pxc_maint_mode` + intent ledger;
  release-reachable trigger is plain network faults; works on either image flavor.
- `applier-resize-converges` — `SET GLOBAL wsrep_applier_threads` + `wsrep_thread_count`;
  zero faults needed.
- `privilege-context-divergence-never-evicts`, `applier-threads-never-read-only`
  (workload halves) — GRANT/readonly-toggle batches are plain SQL; pair with the
  checksum oracle.
- `donor-returns-to-synced`, `joiner-reaches-synced-after-state-transfer`,
  `restarted-node-rejoins-synced` (IST half), `failed-state-transfer-node-rejoins`
  (IST-watchdog arm — the catalog correctly notes default faults reach it and the
  planned supervisor restart-on-abort satisfies its supervision requirement).
- SR *non-crash* legs of `sr-fragment-cross-node-agreement` / `sr-rollback-fragment-noop`
  — `wsrep_trx_fragment_size` verified session-settable; `mysql.wsrep_streaming_log`
  readable over SQL; crash arms A1-gated.
- `no-mdl-bf-bf-abort` fallback form — TOI DDL storm is plain SQL; the INFO log line +
  exit-1 detection lands once the A4 supervisor watchdog exists (the one plumbing
  dependency).

Topology choices that check out against catalog requirements: `wsrep_applier_threads=4`
(catalog requires >1), static IPs (gmcast one-shot DNS), 3 nodes (voting/donor-choice
minimums), 16M gcache (makes IST/SST boundary and page-store reachable — note the
gcache-derived thresholds in `gcache-crash-recovery` (~45MB @128M) must be rescaled to
the 16M config), real EVS timers, `pxc_encrypt_cluster_traffic=OFF` (no catalog property
needs TLS; the TLS findings are explicitly earmarked for a later variant), direct
per-node client connections (required by divergence/1047/stale-read checkers), 90s stop
grace (prevents accidental SIGKILL-on-stop contaminating every graceful-restart
property).

---

## D. Uncertainties

1. **Assert-image bootability** — whether RelWithDebInfo-minus-NDEBUG builds and passes a
   3-node smoke is the topology's own open question; every A2 consequence is conditional
   on which flavor actually ships.
2. **Behavior of specific NDEBUG'd sites on the assert image** — I flagged
   `inconsistency-vote` and `gcache-crash-recovery` as likely to abort where release
   tolerates; the precise set of live asserts on those paths was not enumerated. A
   first-run triage will reveal which properties need image-conditional invariants.
3. **`wsrep_desync_count` exact status-variable name/semantics in 8.4.10** — asserted by
   the catalog, not re-verified here.
4. **Antithesis termination-fault steerability and all-node blast** — whether enabled
   termination faults can produce simultaneous all-down states, and at what frequency
   kills land inside millisecond-scale windows, is a platform question; the A1
   supervisor crash channel sidesteps both if the answer is unfavorable.
5. **`performance_schema.error_log` completeness for wsrep/galera lines** — provider
   (libgalera) log lines route through the server error log and should appear; if any
   fallback line is emitted only to stderr outside the log sink, the SQL-side log checks
   miss it (supervisor tail still catches it).
6. **Sustained workload throughput in-harness** — every reachability-by-accumulation
   estimate (monitor overflow, FC oscillation, recv-queue growth) hangs on writesets/s
   the Python client actually achieves against the chosen image flavor; unmeasured.
7. **Whether mid-run workload phases can be timed to platform quiet periods** — the
   terminal finally-check is guaranteed fault-paused; whether any mid-run mechanism
   exists to request a quiet window (beyond opportunistic gating) affects how much A6
   coverage degradation actually bites.
