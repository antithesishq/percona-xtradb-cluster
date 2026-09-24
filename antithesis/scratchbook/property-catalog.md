---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: 3db14d5c7111617eb27c6491ff4fabfcd939db88
updated: 2026-09-24
external_references:
  - path: https://docs.percona.com/percona-xtradb-cluster/8.4/
    why: Upstream product documentation (user-approved scope: repo + upstream docs)
  - path: https://galeracluster.com/library/documentation/
    why: Galera replication library documentation (user-approved scope: repo + upstream docs)
  - path: https://dev.mysql.com/doc/refman/8.4/en/
    why: MySQL 8.4 reference documentation (user-approved scope: repo + upstream docs)
---

# Property Catalog — Percona XtraDB Cluster 8.4.10

## Summary

This catalog synthesizes an 11-agent property-discovery ensemble over PXC 8.4.10 (server +
vendored galera provider + wsrep-lib + SST scripts + packaging wrappers), plus a
**post-evaluation gap-fill round** (provenance: "evaluation gap-fill"; see
`evaluation/synthesis.md` Gaps). It contains **66 properties in 9 categories**, each
backed by an evidence file at `antithesis/scratchbook/properties/{slug}.md`. Five
duplicate discoveries were merged (independent rediscovery is noted per property as a
confidence signal). Categories and properties within categories are ordered by priority:
the top of the file is the implement-first set. The gap-fill round added: Category 1
`gtid-executed-cluster-convergence`, `evicted-node-rejoins-only-via-sst`; Category 5
`toi-nbo-ddl-completes-or-fails-cleanly`, `skip-locked-nowait-never-fatal`; Category 8
`ftwrl-backup-quiescent-or-fails`, `desync-ftwrl-composition-resyncs`; Category 9
`cluster-address-live-set-rejoins` (per-category totals, cat 1→9: **10** / 7 / 9 / 7 /
**9** / 5 / 4 / **7** / **8**).

The recurring structural findings that shape the whole catalog: (a) release builds compile
out essentially all internal invariants (NDEBUG), so the SUT cannot detect its own silent
divergence — an **external cross-node checksum/effect-count oracle is mandatory shared
infrastructure**, and cross-node-row-equality is the terminal oracle most other data
properties reduce to; (b) Galera's inconsistency voting sees apply *errors* only —
successful-but-divergent applies are invisible to the SUT; (c) many "impossible state"
handlers are `unireg_abort(1)` node suicides whose exit status the shipped systemd units
refuse to restart; (d) the packaging/wrapper layer (bootstrap env, SST scripts, recovery
wrappers) is itself a defect surface, not just plumbing — though the notorious broken-grep
recovery scripts turned out to be vestigial (unshipped; see
`crash-recovery-grep-yields-true-position`).

**Implement-first (top 10) — v1 (assert-image) viable set, re-ranked at evaluation
synthesis** (see "Phasing and variants" below; the two termination-gated originals moved
to the phase-2 tranche, two v1-load-bearing properties promoted):

1. `cross-node-row-equality` — the terminal divergence oracle; nearly every data property
   reduces to it; default faults suffice.
2. `non-primary-rejects-writes` — race-free ack-journal formulation; day-one
   implementable, zero SUT instrumentation.
3. `first-committer-wins-loser-leaves-no-trace` — the flagship client-outcome oracle
   (loser leaves no trace / winner universal / no lost update).
4. `bf-replay-commits-exactly-once` — exactly-once effects under the densest
   timing-dependent machinery in the SUT (with `trx-replay-never-fatal` as its SUT-side
   twin).
5. `flow-control-pause-releases` — cluster-wide commit-freeze watchdog; catches the whole
   family of stall bugs as one liveness check.
6. `partition-heal-single-primary-remerge` — partition/heal liveness with real timer
   defaults MTR never tests.
7. `clustercheck-200-implies-write-progress` — health-check truthfulness; three verified
   green-but-dead mechanisms.
8. `no-mdl-bf-bf-abort` — the dominant recurring PXC bug class (missed cert keys →
    node suicide); the fatal branch is unconditional for non-SR BF-BF pairs, plus a
    residual unlocked mode-read race into the same funnel; log-grep detector is
    day-one adequate.
9. `failed-state-transfer-node-rejoins` — PROMOTED: the IST-watchdog abort arm is v1's
   only default-fault path to ungraceful death, making it the gateway to all incidental
   crash-recovery coverage in v1.
10. `graceful-shutdown-bounded` — PROMOTED: every v1 restart-driven exploration depends
    on graceful shutdown completing; it is both a property and harness-config guard.

**+1 (gap-fill): `gtid-executed-cluster-convergence` joins the implement-first set as an
11th entry, implemented with the checksum pass rather than displacing anything.** It is
High, v1-viable (given the required `gtid_mode=ON` config addition — see
deployment-topology.md), and the terminal oracle for a realized-bug pattern (pattern H)
that no existing top-10 member covers — GTID divergence does NOT reduce to
`cross-node-row-equality`. It displaces nothing because it shares the quiesced-checkpoint
/ `wsrep_sync_wait`-barrier infrastructure with `cross-node-row-equality` (#1): once that
checkpoint pass exists, the GTID equality check is one more query in the same pass, so
adding it costs a comparison, not an eleventh implementation effort.

**Phase-2 tranche (unchanged importance, deferred until the workload→supervisor kill
channel is proven — NOT deleted):** `acked-commit-durable-across-restart` (dead without
ungraceful termination), `grastate-se-checkpoint-agreement` (near-vacuous under
graceful-only restarts — both branches trivially agree).

## Implementation status (antithesis-workload, 2026-09-22)

One general-purpose test template, `pxc`, now exists at `antithesis/test/pxc/`, with all
logic and every SDK assertion in `antithesis/workload/pxcwl/`. The design is deliberately
**not** one template or one test case per property: broad swarm-parameterised traffic drives
the system, fault injection forces the interesting states, and catalog properties are
expressed as assertions layered over that one traffic stream. Per-property targeting is
deferred until triage shows the broad workload is missing something.

35 assertions are cataloged (including the pre-existing bootstrap `reachable`): 16 `always`,
5 `always_or_unreachable`, 13 `reachable` and 1 `unreachable`. The 13 reach claims exist to
prove the workload gets where it should. **Unfired reach claims after the first run are the
iteration list.**

Two of those came out of a fresh-context review that found real coverage holes: nothing had
compared the *results* of DDL across nodes (every DDL target is a scratch table outside the
checksum set, and a missing table scored as identical), and the cross-node causality claim
in `sync_wait_read` was computed and discarded. Both are now asserted — see
`../VALIDATION.md` for the full review findings, including a severe missing-`ROLLBACK` bug
that would have fired the no-trace assertion on a correct cluster.

### Post-triage corrections (run `afeec3df…-63-0`, 2026-09-23)

Three oracle defects found in triage, all workload-side, all fixed:

- `checks.compare_schemas` and `checks.compare_gtid_executed` folded a query
  exception into the value they compared across nodes. An exception never equals a
  real signature or GTID set, so one transient ER 1205 on one node reported every
  table as divergent. 287 + 88 of the run's counterexamples were this artifact.
  Unreadable nodes are now EXCLUDED from the comparison (the exclusion
  `compare_table_set` already made) and surfaced separately as
  `schema_unreadable` / `gtid_unreadable`. A comparison with fewer than two
  readable nodes makes no claim.
- `maint_mode_cycle` was missing from `leases.DISRUPTIVE`, so `graceful_shutdown`
  ran concurrently and the shutdown path's forced `pxc_maint_mode=SHUTDOWN`
  invalidated the hold. Now shares the disruption token.
- The maint-mode assertion claimed a two-way equality the property never meant.
  Of its 68 reds, 45 were `observed=SHUTDOWN` (documented carve-out: both
  `log_view` branches exempt SHUTDOWN), 21 were the forced-FLIP to MAINTENANCE
  (owned by `no-spurious-multi-major-detection`, not this property), and exactly
  **1 was the real forced-revert hijack**. Narrowed to the one direction the
  evidence file describes and renamed accordingly — see that entry below.

### Post-triage corrections (run `de51f0b9…-63-0`, 2026-09-23)

The two fixes above verified: schema divergence 88 → 0 counterexamples, and the
narrowed maint-mode property passed 48 evaluations with none. One new
workload-side oracle defect, which the quieter report made the largest red
(13.6% → 49.4%), fixed here:

- `clustercheck-200-implies-write-progress` fired 352 counterexamples and
  **every one of them carried `last_probe_commit_age_s: null`** — no successful
  probe write on record — with zero carrying a real age. Two causes, both fixed
  in `probe.py`:
  - Antithesis runs several instances of an `anytime_` command concurrently (one
    branch started 50 `anytime_cluster_probe` processes against 15 finished).
    All of them write the shared `progress` row, and the old whole-row
    read-modify-write let a process whose probe had just failed store back the
    `last_probe_commit_at` it had read seconds earlier, erasing a success
    another process had recorded in between. The evidence is in the age
    histogram: 319 zeros, 355 nulls, a thin tail of one-offs, and five
    **negative** ages — a timestamp written by a process whose clock read was
    ahead of the asserting one. `_merge_progress` now folds each field with an
    operator two writers can apply in any order (MAX for monotone clocks,
    COALESCE for latching marks, explicit NULL only from the process that saw
    the condition end) and re-reads inside the same transaction, so the
    assertion judges the merged view rather than one process's stale snapshot.
  - "No successful probe is on record" is not "the node refused writes". The
    claim now needs positive evidence either way: a probe that landed inside the
    trailing green window, or an unbroken run of refused writes covering it
    (`probe_failed_since`). With neither, nothing is asserted. Counterexamples
    now carry `probe_failed_seconds`, `probe_outcome` and `probe_reason`, which
    is what triage needed to tell a refused write from an unopenable socket.
- Two narrower fixes in the same path: an unreadable `pxc_maint_mode` no longer
  counts as green (`clustercheck_green` treats unknown as DISABLED, correct for
  mirroring the script's formula, wrong as evidence about a node we could not
  question), and the commit window is now the trailing `PXC_GREEN_WINDOW`
  rather than "any time since the node went green" — one probe landing as the
  green run opened used to exempt the node for as long as it then stayed wedged.

### Post-triage corrections (run `5a7b1d9f…-63-0`, 2026-09-24)

The availability fix verified: 49.4% → 20.9% counterexamples, evaluations 712 →
172, and **zero** failures without supporting evidence (every one now carries
`probe_failed_seconds ≥ 135s`, most with `probe_reason` = a write that *timed
out*, and `fc_paused_ns_delta` up to 10.4s). The reds that remain are the
flow-control leg this property was written for.

One workload correction, and it is the same defect `config.EPHEMERAL_TABLE`
already documents for tables, one level down:

- `ddl.py`'s `ddl_index`, `ddl_column` and `ddl_online_fk` flipped a coin
  between CREATE and DROP without knowing whether the object existed, so about
  half of every such statement was invalid on arrival — the class is
  ER_DUP_KEYNAME 1061, ER_DUP_FIELDNAME 1060, ER_FK_DUP_NAME 1826 and
  ER_CANT_DROP_FIELD_OR_KEY 1091, of which the measured run shows 1091 and 1826.
  PXC replicates
  the failing statement anyway and each one costs a cluster-wide inconsistency
  vote, which the **un-injected-vote rule** under `cross-node-row-equality`
  reads as divergence evidence. A fault-free local validation measured it
  exactly: 29 failed scratch DDLs, 29 voting rounds. All three shapes now read
  the server's catalog and emit whichever direction is legal, and skip entirely
  when the lookup fails or the table is momentarily absent. New reach claim
  `a data-definition statement dropped an object the catalog reported present`
  guards the lookup itself: a lookup that silently returned nothing would
  degrade the generator into create-only, and nothing else would say so.
  Foreign key names are scoped per **schema** in MySQL 8, not per table, so that
  lookup is schema-wide and a name already live on another table is dropped from
  its owner rather than re-added here — a per-table lookup would have been worse
  than the coin flip (three of four draws doomed instead of half). Skips are
  tallied (`{shape}:lookup_failed`, `{shape}:table_absent`) so a vanished table
  cannot silently zero out three of the six DDL shapes.

The catalog is read fresh before each statement rather than cached, because
`ddl_rename_swap` moves every index, column and constraint to the other table
name — and the swap comes from a different driver invocation, so no in-process
ledger can see it.

Why it mattered beyond noise, from the same run: one such statement failed to
apply on node1 at seqno 606 *during a partition*; node2 could not vote, took
`Can't vote when not at least JOINED. Assuming inconsistency. Full SST is
required`, and node1 then served a green health check through a 164-second
unbroken run of writes that timed out (`probe_failed_seconds: 164.4`), its
error log silent from vtime 95 to 245. `every Synced node agrees on the set of tables and
indexes` also went red (17 CE) with node1 permanently missing
`wl_scratch_ephemeral` while still Synced.

### Commands

| Command | Role |
| --- | --- |
| `first_seed_workload_schema` | Draws the timeline's swarm profile once, creates the schema, applies server posture |
| `parallel_driver_traffic` | The traffic generator: 7 action classes plus 10 admin levers |
| `anytime_cluster_probe` | Continuous checks of what each node claims against what it does |
| `eventually_verify_convergence` | Terminal oracle with faults stopped, drivers killed |
| `finally_verify_convergence` | Terminal oracle on timelines where every command completed |

### Covered — detected by the terminal oracle

The checksum and GTID comparison pass is the terminal oracle these reduce to
(see `property-relationships.md`). All are now **covered**:

`cross-node-row-equality`, `gtid-executed-cluster-convergence`,
`bf-bf-lock-suppression-no-divergence`, `ist-overlap-writesets-not-reapplied`,
`sr-fragment-cross-node-agreement`, `sr-rollback-fragment-noop`,
`autoinc-identity-no-cross-node-collision`, `privilege-context-divergence-never-evicts`,
`applier-threads-never-read-only`, `cert-interval-reject-symmetry` (as a standing check
rather than the reframed calibration).

### Covered — detected by the shared ack journal

The journal records every write as ATTEMPTED before issue, then resolves it to ACKED,
FAILED (clean rejection, node alive, provably no trace) or UNKNOWN (connection died;
outcome unknowable). Acked writes must be present, cleanly-failed writes must be absent,
and unknown writes get no verdict — which is what makes the check sound under faults.

`non-primary-rejects-writes`, `first-committer-wins-loser-leaves-no-trace`,
`retry-autocommit-exactly-once`, `bf-replay-commits-exactly-once`,
`sync-wait-reads-observe-acked-writes`, `acked-commit-durable-across-restart`
(**graceful-restart form only** — see gaps below).

### Covered — reconvergence, lineage, and continuous checks

`partition-heal-single-primary-remerge`, `restarted-node-rejoins-synced`,
`joiner-reaches-synced-after-state-transfer`, `donor-returns-to-synced`,
`failed-state-transfer-node-rejoins`, `full-cluster-restart-reaches-primary`,
`at-most-one-primary-component`, `cluster-identity-single-lineage`,
`no-dual-bootstrap-after-full-shutdown` (runtime form), `cluster-address-live-set-rejoins`,
`flow-control-pause-releases`, `synced-node-recv-queue-bounded`,
`commit-order-monitor-released-no-cluster-stall`, `local-monitor-freed-after-bf-abort`,
`monitor-window-overflow-unreachable` (as symptom),
`clustercheck-200-implies-write-progress`, `maint-mode-honors-operator-intent`
(**pre-registered KNOWN-RED**), `skip-locked-nowait-never-fatal`,
`applier-resize-converges`, `toi-nbo-ddl-completes-or-fails-cleanly` (TOI half),
`graceful-shutdown-bounded`, `desync-ftwrl-composition-resyncs` (desync and instance
backup-lock legs; FTWRL itself is blocked by `pxc_strict_mode=ENFORCING`).

### Covered by the build, not by an assertion

The images are an assert-enabled source build, so every native Galera/InnoDB assertion is
already an oracle and the workload's job is only to *reach* it: `no-mdl-bf-bf-abort`,
`trx-replay-never-fatal`, `illegal-wsrep-transition-never-taken`,
`gcs-total-order-gap-free`, `fatal-node-terminates-no-zombie`.

### Not covered, and why

| Not covered | Reason |
| --- | --- |
| The 11 `+kill` crash-recovery properties — `grastate-se-checkpoint-agreement`, `wsrep-xid-checkpoint-monotonic`, `interrupted-sst-forces-full-sst`, `gcache-crash-recovery-no-abort`, `gcache-recovered-ist-completeness`, `sst-grant-all-user-locked-or-absent`, `crash-recovery-grep-yields-true-position`, `gcache-page-files-bounded` (orphan half), and the ungraceful half of `acked-commit-durable-across-restart` | The supervisor's kill channel is a control file under `$PXC_STATE_DIR` on the **node** containers, and `config/docker-compose.yaml` declares no volumes, so the workload container has no filesystem path to it. The only node-down mechanisms available are SQL `SHUTDOWN` (graceful) and `gmcast.isolate` (logical). Unlocking this needs either platform container-kill faults or a per-node volume — an environment change, not a template change. |
| `notify-cmd-hang-does-not-block-commits`, `async-monitor-leave-mismatch-unreachable` | `wsrep_notify_cmd` is `READ_ONLY`; needs a my.cnf variant image. |
| `duplicate-gtid-skip-exactly-once` | Needs an async source topology, still an open catalog decision. |
| `inconsistency-vote-evicts-divergent-minority`, `evicted-node-rejoins-only-via-sst`, `cluster-member-strings-never-reach-shell` | Sabotage variants. Deliberate divergence injection would make the unconditional checksum `Always` permanently red in this shared environment; the poison-budget convention fences them into their own variant. |
| `bf-abort-skip-awake-victim-already-killed`, `commit-cut-bounded-by-delivered-seqno`, `vote-message-payload-contract`, `homogeneous-cert-version-match`, `no-spurious-multi-major-detection`, `rolling-upgrade-write-gate` | `v2-instrumented` / release-image work. `PXC_INSTRUMENT=0`, so there is no coverage-guided search and no thread-pausing faults. |
| `nonready-node-error-code-contract` | Known-red on a verified defect (unready TOI returns ER 1213, not 1047). Arming it buys a permanent red and no information; the workload tallies errno-per-shape as data instead, and a reach claim proves the 1047 path fires. |
| PK-less divergence at full strength | `pxc_strict_mode=ENFORCING` blocks PK-less DML. The workload opens a fenced PERMISSIVE window and compares `wl_nopk` under its **own** property name, so a red on this documented limitation cannot mask a red on the real content invariant. |

### Calibration values needing a first measurement

Every one of these is deliberately loose: a tight bound that fires on legitimate slow
recovery is worse than a loose one that still catches a true wedge. All live in
`workload/pxcwl/config.py` and are env-overridable.

| Value | Default | What sets it |
| --- | --- | --- |
| `PXC_GREEN_WINDOW` | 120s | Sustained clustercheck-green window that must contain a commit |
| `PXC_WEDGE_WINDOW` | 300s | Commit-progress freeze while all nodes claim health |
| `PXC_SHUTDOWN_BOUND` | 120s | SQL `SHUTDOWN` to port-closed |
| `PXC_RECV_QUEUE_SLACK` | 100x `fc_limit` | Recv-queue ceiling on a Synced, non-desynced node |

## Assumptions

- **Three-tier image strategy (REVISED at evaluation synthesis — adopts the topology's
  build order, resolving the former catalog-vs-topology contradiction)**: the **v1
  primary image is the assert-enabled build** (RelWithDebInfo with NDEBUG stripped);
  a **plain Debug image is the DBUG tier** (stripping NDEBUG does NOT define DBUG —
  `DBUG_OFF` remains set in non-Debug builds, so DBUG keywords / DEBUG_SYNC / SR crash
  points / the multi-major knob need CMAKE_BUILD_TYPE=Debug); the **release (NDEBUG)
  image is a later phase** for field-behavior properties (warn-and-proceed transitions,
  vote pipeline un-preempted by asserts). Per-property image/phase validity is tagged in
  "Phasing and variants" below; release-conditioned invariants are image-conditional
  (on the assert image, injected divergence may assert-crash BEFORE a vote round, and
  "logs and proceeds anyway" branches abort instead).
- **Harness supervises restarts itself**: shipped systemd units exclude SIGABRT/exit-1
  from restart (`RestartPreventExitStatus`) — field semantics would leave suicided nodes
  down. The harness restart-on-abort policy is a deliberate divergence from the field and
  must be recorded per run; liveness properties presume restart-on-death.
- **≥3 nodes, stable IPs**: gmcast resolves DNS once at connect
  (`gmcast.cpp:285-309`); IP churn on restart contaminates re-merge liveness properties
  unless deliberately used as an amplifier.
- **`wsrep_applier_threads > 1`** configured (field default 1 masks the entire
  parallel-apply divergence class); several properties list it as required.
- **Container stop grace > `pxc_maint_transition_period` (10s) + drain** — Docker's
  default 10s grace SIGKILLs nodes at the start of every graceful shutdown, turning every
  stop into crash recovery (see `graceful-shutdown-bounded`).
- **mysql.user (and the SST-user lifecycle rows) EXCLUDED from cross-node checksums** —
  legitimate per-node differences exist by design, including the donor-side unlocked
  `mysql.pxc.sst.user` created for every donation and the donor's copy shipped inside
  each SST stream (see `sst-grant-all-user-locked-or-absent`).
- **Harness reproduces the recovery dance** (`--wsrep_recover` →
  `--wsrep_start_position`) deliberately — the shipped RPM/deb flow is
  `/usr/bin/mysql-systemd galera-recovery` (working bracketed `[WSREP]` grep;
  `mysqld_safe` also works); the broken unbracketed-grep families
  (`mysqld_pre_systemd.in`, debian `mysql-helpers`) are vestigial, not shipped
  (`WITH_SYSTEMD` forced OFF in PXC builds) — see
  `crash-recovery-grep-yields-true-position`.
- gu_abort core suppression should be overridden in the image so fatal paths leave
  evidence.

## Phasing and variants (added at evaluation synthesis)

Every property carries an **image/phase validity tag**:

- `v1-assert` — meaningful on the v1 assert-enabled image with the v1 topology.
- `debug-DBUG` — needs the plain Debug image (DBUG keywords / DEBUG_SYNC / SR crash points).
- `release` — semantics specific to the NDEBUG field build (asserts would preempt or
  invert the tested mechanism).
- `v2-instrumented` — needs the v2 SDK (libvoidstar) patchset in mysqld/galera/wsrep-lib.
- Modifiers: `+kill` (needs the workload→supervisor kill channel or tenant termination
  faults), `+variant(x)` (needs a config/topology variant), `calibration` (one-shot
  oracle calibration, not a standing property), `rider` (folded into another property's
  workload), `deferred` (gated on an undecided topology/phase), `CI` (deterministic
  CI/unit test, not an Antithesis property).

| Property | Tag | Tranche |
|---|---|---|
| cross-node-row-equality | v1-assert | 1 |
| non-primary-rejects-writes | v1-assert | 1 |
| first-committer-wins-loser-leaves-no-trace | v1-assert | 1 |
| bf-replay-commits-exactly-once | v1-assert | 1 |
| flow-control-pause-releases | v1-assert | 1 |
| partition-heal-single-primary-remerge | v1-assert | 1 |
| clustercheck-200-implies-write-progress | v1-assert | 1 |
| no-mdl-bf-bf-abort | v1-assert (log detector; sharp form v2-instrumented) | 1 |
| failed-state-transfer-node-rejoins | v1-assert (kill arms +kill) | 1 |
| graceful-shutdown-bounded | v1-assert | 1 |
| gtid-executed-cluster-convergence | v1-assert +config(gtid_mode=ON) — gap-fill | 1-2 (with the checksum-oracle pass) |
| sync-wait-reads-observe-acked-writes | v1-assert (checksum-oracle dependency — implement early) | 2 |
| ist-overlap-writesets-not-reapplied | v1-assert (crash variant +kill) | 2 |
| at-most-one-primary-component | v1-assert (Always uncommitted unless workload issues pc.weight changes — add that action) | 2 |
| acked-commit-durable-across-restart | v1-assert +kill | 2 |
| grastate-se-checkpoint-agreement | v1-assert +kill (graceful-only = near-vacuous) | 2 |
| gcache-recovered-ist-completeness | v1-assert +kill | 2 |
| gcache-crash-recovery-no-abort | v1-assert +kill | 2 |
| restarted-node-rejoins-synced | v1-assert (PXC-4631 arm +kill: graceful-restart-then-kill-mid-IST sequence) | 2 |
| donor-returns-to-synced | v1-assert | 2 |
| joiner-reaches-synced-after-state-transfer | v1-assert (bounds after calibration) | 2 |
| sst-grant-all-user-locked-or-absent | v1-assert +kill | 2 |
| inconsistency-vote-evicts-divergent-minority | release (assert image may abort pre-vote) +variant(sabotage-fenced) | 2 |
| evicted-node-rejoins-only-via-sst | vote arm release +variant(sabotage-fenced); IST-failure/kill legs v1-assert +kill; +variant(force_sst_after_inconsistency=yes — runtime-settable) — gap-fill | 2 |
| bf-bf-lock-suppression-no-divergence | v1-assert +variant(strict-mode-lowered); SUT Sometimes v2-instrumented | 2 |
| privilege-context-divergence-never-evicts | v1-assert +variant(sabotage-fenced); SUT forms v2-instrumented | 2 |
| sr-fragment-cross-node-agreement | v1-assert (session-set fragment size); crash legs +kill | 2 |
| sr-rollback-fragment-noop | v1-assert (session-set fragment size); crash sub-case +kill | 2 |
| applier-threads-never-read-only | v1-assert; SUT Always v2-instrumented; restart variant +kill | 2 |
| autoinc-identity-no-cross-node-collision | v1-assert; torn-read leg v2-instrumented; X-plugin leg deferred (no X-protocol client in v1) | 2 |
| commit-order-monitor-released-no-cluster-stall | v1-assert (PXC-4845 variant +kill) | 2 |
| toi-nbo-ddl-completes-or-fails-cleanly | v1-assert (NBO legs +variant(NBO-fenced) per poison-budget; supervisor grastate-vs-DDL-ledger probe) — gap-fill | 2 |
| skip-locked-nowait-never-fatal | v1-assert; rider on the BF-conflict workload — gap-fill | rider |
| retry-autocommit-exactly-once | v1-assert; SUT Sometimes v2-instrumented | 2 |
| trx-replay-never-fatal | v1-assert (workload half); Unreachable v2-instrumented | 2 |
| synced-node-recv-queue-bounded | v1-assert (threshold after calibration) | 2 |
| gcache-page-files-bounded | v1-assert (supervisor file metrics); page-store Sometimes needs bulk-writer workload; orphan half +kill | 2 |
| notify-cmd-hang-does-not-block-commits | v1-assert +variant(notify-cmd config — wsrep_notify_cmd is READ_ONLY) | 2 |
| fatal-node-terminates-no-zombie | v1-assert (supervisor log-tail watchdog) | 2 |
| nonready-node-error-code-contract | v1-assert — KNOWN-RED from run one (pre-registered bug-finder) | 2 |
| maint-mode-honors-operator-intent | v1-assert — KNOWN-RED from run one (pre-registered bug-finder) | 2 |
| ftwrl-backup-quiescent-or-fails | v1-assert (fault-free core) — gap-fill | 2 |
| desync-ftwrl-composition-resyncs | v1-assert (backup/desync actor fenced — desync disables FC) — gap-fill | 2 |
| cluster-address-live-set-rejoins | v1-assert; rider on restarted-node-rejoins-synced — gap-fill | 2 |
| no-dual-bootstrap-after-full-shutdown | v1-assert restated (per-boot supervisor emission + workload epoch ledger); shipped-env vector deferred (field-faithful supervisor variant) | 3 |
| full-cluster-restart-reaches-primary | v1-assert +kill (all-down; supervisor hold-down knob) | 3 |
| interrupted-sst-forces-full-sst | v1-assert +kill | 3 |
| sst-ready-message-implies-listener | keep-as-message: folded into restarted-node timeout (bound ~220s after calibration) | 3 |
| local-monitor-freed-after-bf-abort | v1-assert (regression guard) | 3 |
| monitor-window-overflow-unreachable | release/log-fallback; explorability demoted; designed-behavior carve-out | 3 |
| illegal-wsrep-transition-never-taken | v2-instrumented (sharp); log fallback release-only (asserts preempt on v1 image; trx-side msg is debug-level → debug-DBUG) | 3 |
| vote-message-payload-contract | v2-instrumented + release (NULL-deref Unreachable DROPPED — undrivable) | 3 |
| gcs-total-order-gap-free | v2-instrumented | 3 |
| commit-cut-bounded-by-delivered-seqno | v2-instrumented | 3 |
| wsrep-xid-checkpoint-monotonic | sharp form v2-instrumented; workload proxy +kill | 3 |
| bf-abort-skip-awake-victim-already-killed | v2-instrumented +variant(strict-mode-lowered probe) | 3 |
| homogeneous-cert-version-match | v2-instrumented | 3 |
| no-spurious-multi-major-detection | release + v2-instrumented | 3 |
| crash-recovery-grep-yields-true-position | deferred (v1 supervisor replaces the shipped wrapper — would test harness code); field-faithful supervisor variant is future work | deferred |
| cluster-identity-single-lineage | deferred (v1 marker-file supervisor removes the persistent-env attack surface); field-faithful supervisor variant | deferred |
| cluster-member-strings-never-reach-shell | CI +variant(sentinel-env) — sentinel node name is allowlist-rejected at startup, breaking the 3-node baseline | deferred |
| cert-interval-reject-symmetry | calibration (one-shot negative-control calibration of the checksum oracle, not a standing property) | calibration |
| applier-resize-converges | rider (deterministic loop test covers most; keep the convergence check as a rider) | rider |
| duplicate-gtid-skip-exactly-once | deferred (async source topology) | deferred |
| async-monitor-leave-mismatch-unreachable | deferred (async source topology) + v2-instrumented | deferred |
| rolling-upgrade-write-gate | debug-DBUG | deferred |

Six properties have **no day-one form at all** (v2 instrumentation prerequisites):
`bf-abort-skip-awake-victim-already-killed`, `commit-cut-bounded-by-delivered-seqno`,
`gcs-total-order-gap-free`, `vote-message-payload-contract` (sharp form),
`homogeneous-cert-version-match`, `wsrep-xid-checkpoint-monotonic` (sharp form).

**Deferred phase: `encryption variant`** (gap-fill item 6 — recorded so zero coverage of
the default-ON field config `pxc_encrypt_cluster_traffic` is a *decision*, not an
oversight; v1 runs OFF per the topology). Three **placeholder items** (not full
properties — to be researched when the phase is scheduled; anchors and deferral rationale
in `antithesis/scratchbook/properties/deferred-encryption-phase.md`):

1. *TLS rotation / `socket.ssl_reload`* — MySQL-side cert rotation (`ALTER INSTANCE
   RELOAD TLS`) does not cover the Galera channel; separate provider knob.
2. *keyring × SST transition-key handling* — donor/joiner keyring disagreement detected
   only after transfer start; plaintext transition key in sst_info.
3. *gcache/disk-page encryption crash recovery* — encrypted-gcache recovery after kill
   (MTR's only combination axis; never crash-tested).

## Shared conventions (added at evaluation synthesis)

- **Quiesced checkpoints under faults**: the workload cannot pause faults mid-run on its
  own, so every per-event "quiesced checkpoint" `Always` (cross-node-row-equality,
  sr-fragment agreement/emptiness, donor-returns-to-synced state checks, post-IST
  checksums) is implemented as **opportunistic gated attempts while faults are active,
  plus a guaranteed full-strength check in `eventually_`/`finally_` test commands (faults
  paused by the harness)**. `ANTITHESIS_STOP_FAULTS` is the mechanism for a mid-run quiet
  window when one is genuinely needed. Checkpoint cadence is a budget/exploration
  tradeoff (quiesced rounds drain the interleavings faults create) — a flagship design
  parameter to revisit at first triage.
- **Poison-budget scoping**: sabotage/trigger-poisoning properties — divergence injection
  for `inconsistency-vote-evicts-divergent-minority`, the `cluster-member-strings`
  sentinel node, the privilege sweep — run only in **fenced variants/phases** with
  excluded tables and suppression windows, so they never violate the unconditional
  checksum/liveness `Always` invariants of the shared environment. Ambient NBO DDL is
  likewise fenced into its own workload phase (an NBO in flight deterministically aborts
  joiners, fabricating SST/IST liveness failures). The workload's `pc.bootstrap` lever is
  guarded the same way (it can self-inflict a split-brain "finding").
  **Un-injected-vote rule**: any inconsistency vote NOT attributable to injected sabotage
  is itself divergence evidence — `cross-node-row-equality` fails on it (touching traffic
  converts key/existence divergence into vote evictions BEFORE a quiesced checksum can
  see it; also checksum evicted nodes before rejoin, and prefer write-once witness tables
  that ROW full-image apply cannot self-heal).
- **Instrumentation strategy (decided once, not per property)**: v1 uses a **shared
  log-string scan layer** (supervisor log-tail + `performance_schema.error_log` over SQL
  for live nodes), unified with the `mtr_warnings.sql` suppressed-warnings denylist as
  candidate log assertions — mined into a concrete v1 assertion list at
  `antithesis/scratchbook/log-scan-candidates.md` (scan-as-finding patterns with their
  window conditions, plus annotated expected-under-faults patterns; gap-fill item 4);
  **v2 delivers one SDK (libvoidstar) patchset across mysqld +
  the two vendored submodules** (galera, wsrep-lib) covering all `v2-instrumented` tags —
  not per-property patches.
- **Shared workload requirements (gap-fill)**: two generators every relevant property's
  workload draws from, stated once here rather than per property:
  - **Bulk-transaction generator**: a dedicated actor issuing multi-MB transactions
    (wide-row multi-statement batches / LOAD-DATA-style bulk inserts), sized against the
    v1 `gcache.size=16M` — individual transactions in the 1-8M writeset range plus an
    occasional over-`wsrep_max_ws_size` probe. Without it the gcache page-store
    `Sometimes` guards sit **vacuous** under an OLTP-sized mix. Dependent properties:
    `gcache-page-files-bounded` (page-store-actually-used `Sometimes`),
    `gcache-crash-recovery-no-abort` (page-store recovery legs), the
    `monitor-window-overflow-unreachable` desync+flood amplifier (bulk writes accelerate
    window consumption), and the `wsrep_max_ws_size` rejection boundary (oversize
    writeset rejected cleanly, no divergence/stall — rider on the generator, not a new
    property).
  - **DDL generator — online-FK shapes** (from the disabled `galera-index-online-fk`
    repro, "fk_40 triggers inconsistency voting"): online `ALTER TABLE child ADD
    CONSTRAINT ... FOREIGN KEY ... ON DELETE CASCADE/SET NULL ON UPDATE CASCADE,
    ALGORITHM=INPLACE` under `foreign_key_checks=0`; `CREATE INDEX` on FK parents/children
    under concurrent FK DML; multi-FK single ALTER; FK referencing a non-unique secondary
    index (`restrict_fk_on_non_standard_key=OFF`); expected-error ALTERs
    (`ER_FK_NO_INDEX_PARENT/CHILD`, `ER_FK_DUP_NAME`) interleaved with cascading DML.
    Target properties: `no-mdl-bf-bf-abort` and `bf-bf-lock-suppression-no-divergence`
    (lists updated in both evidence files).
- **Run accounting (gap-fill)**: the harness restart-on-abort policy masks field
  permanent-down semantics, so every supervisor restart event is tagged with the mysqld
  exit status/signal and a **field-restart classifier**: would shipped systemd
  (`Restart=on-abort` + `RestartPreventExitStatus=SIGABRT`; exit-1 `unireg_abort`
  likewise not restarted — sut-analysis §8.5) have restarted it? Triage can then report
  "field would be permanently down here" per event. Per liveness property, record
  **time-to-detection and time-to-recovery buckets** (fault/death → first
  detector signal → back-to-Synced) so liveness verdicts carry duration data, not just
  pass/fail. Supervisor emits the records as JSONL (see topology supervisor extensions).
- **pxc_strict_mode**: baseline keeps the field default **ENFORCING**; a dedicated
  workload phase/variant lowers it dynamically for the PK-less/FK-cascade legs
  (`bf-bf-lock-suppression-no-divergence`'s required workload, cross-node-row-equality's
  PK-less leg, the bf-abort-skip-awake probe, any MyISAM driver). ENFORCING blocks
  PK-less DML, non-InnoDB DML, and LOCK TABLES (`sql_base.cc:6306-6350`).
- **Known-red pre-registration**: `nonready-node-error-code-contract` and
  `maint-mode-honors-operator-intent` are **expected to fail from run one** — they are
  deliberate bug-finders on verified defects. Register the known-failing arm as a
  pre-registered finding at first triage and carve it out (assert the rest of the
  contract) so a permanent red does not mask regressions elsewhere in the same property.
- **Calibration run first**: no numeric timing bound (re-merge ~90s, recv-queue ~173,
  drain/exit bounds, sst-ready ~220s, monitor-overflow reachability) is pinned until one
  calibration run **per image flavor** — Debug-tier throughput invalidates
  release-derived arithmetic.
- **Triage ordering note**: the checksum oracle depends on `wsrep_sync_wait` as its
  barrier; a red `sync-wait-reads-observe-acked-writes` therefore invalidates concurrent
  checksum verdicts — triage sync-wait first.

## Open Questions (catalog-wide)

- **Node-termination fault availability — DECIDED (user, 2026-09-10): platform container-kill faults will be enabled at some point, no immediate request; v1 ships on the supervisor kill channel and tranche 2 activates when platform kills land.** Prior state (partially resolved at synthesis): kill/restart
  faults are load-bearing for a large fraction of the catalog (all of Commit durability &
  crash recovery, half of State transfer). The former fallback ("workload drives docker
  stop / in-container kill") is NOT realizable — the workload reaches nodes via SQL only
  and `SHUTDOWN` is graceful. Resolution: the topology now specifies a
  **workload→supervisor crash channel** (supervisor polls a file/SQL-visible marker →
  `kill -9` mysqld; steerable kill timing) plus node-side kill test-command scripts.
  Properties needing ungraceful death are tagged `+kill` in "Phasing and variants". The
  tenant platform-fault question still stands (see evaluation/synthesis.md, Bias 1).
- **Checksum-oracle barrier semantics — RESOLVED (design level)**: appliers hold the
  *apply* monitor across the full commit (in both binlog configs), so `wsrep_sync_wait` —
  which waits on the apply monitor (`replicator_smm.cpp:1715-1722`) — is a sufficient
  per-node barrier for quiesced cross-node checksums. No extra
  equal-`wsrep_last_committed` + idle precondition is needed, modulo monitor-implementation
  bugs, which `sync-wait-reads-observe-acked-writes` exists to catch.
- **Shared observability: `wsrep_monitor_status (L/A/C)`** — PXC-only status variable
  printing `(last_entered, last_left)` per monitor. A frozen pair on one monitor while
  delivery continues is the direct external leak/stall signature for the whole
  monitor-release family; poll it from the workload. Replaces the two previously proposed
  SUT-side gauges.
- **XA excluded from the day-one workload**: unordered XA BF-abort recovery is partially
  unimplemented in PXC 8.4 (`commit_by_xid` stub — `assert(0)`/`error_not_implemented`,
  `wsrep_client_service.cc:330-334`). XA exactly-once/replay coverage needs its own later
  property; day-one replay/retry workloads must not issue XA.
- **PXC-4665 known won't-fix deadlock**: wsrep applier vs local connection under a
  contested explicit `gtid_next` (interleaving documented in `galera_gtid.test:36-57`;
  plain SQL on two nodes, no async channel needed). The workload must avoid holding open
  transactions under a contested explicit `gtid_next`, or classify the resulting liveness
  failure as a known issue rather than a new finding.
- **Async-replica topology decision**: two properties (`duplicate-gtid-skip-exactly-once`,
  `async-monitor-leave-mismatch-unreachable`) require an async MySQL source replicating
  into the cluster (`replica_parallel_workers > 1`, `preserve_commit_order=ON`). Decide
  whether the harness includes this topology or defers those properties.
- **Streaming replication — UN-GATED (synthesis)**: `wsrep_trx_fragment_size` is a
  session variable (SESSION_VAR + HINT_UPDATEABLE, `sys_vars.cc:8575-8583`) — no server
  config variant is needed. The workload enables SR per session, so the SR non-crash
  legs (sr-rollback churn, fragment-agreement quiescence checks, the checksum oracle's
  SR-under-churn `Sometimes`) are v1-viable today; only the crash sub-cases remain
  kill-channel-gated.
- **Negative finding (validated)**: `wsrep_certification_rules` (STRICT/OPTIMIZED) has
  ZERO readers in this tree — an inert sysvar in PXC 8.4.10. No property is built on it;
  any external doc claims about it do not apply to this build.
- **Clock jitter**: only secondary angles (sync-wait wall-clock deadline, FC-release
  clock-step) want it; no property requires it. gcomm uses monotonic clocks (confirmed).

---

## 1. Data integrity & divergence detection

The core promise of synchronous multi-master replication: every node holds the same data.
The SUT cannot verify this itself in release builds (voting only sees apply errors;
InnoDB deliberately suppresses applier-vs-applier lock conflicts), so these properties
build the external oracle and target the known silent-divergence generators.

### cross-node-row-equality — All Synced nodes hold identical data

| | |
|---|---|
| **Type** | Safety |
| **Property** | Quiesced per-table checksums are identical on all Synced primary-component nodes once they report equal `wsrep_last_committed`. |
| **Invariant** | `Always`: at quiesced checkpoints, per-table checksums match across nodes (mysql.user and SST-user lifecycle rows excluded). Under active faults this runs as **opportunistic gated attempts**; the **guaranteed full-strength check lives in `eventually_`/`finally_` commands with faults paused** (see Shared conventions; `ANTITHESIS_STOP_FAULTS` for mid-run quiet windows). Companion `Sometimes` guards for the hard-to-reach shapes: DDL+DML concurrency, PK-less DML (strict-mode-lowered variant), `applier_threads>1` load, SR under churn (v1-viable — session-set fragment size). **Un-injected-vote rule**: any inconsistency vote not attributable to injected sabotage counts as a failure of this property (divergence evidence — touching traffic converts divergence into evictions before a checksum can see it); prefer write-once witness tables and checksum evicted nodes before rejoin. |
| **Antithesis Angle** | Certification sees only declared keys; InnoDB suppresses applier-vs-applier conflicts (`lock0lock.cc:581-599`); voting fires only on apply ERRORS — successful-but-divergent apply is undetectable by the SUT. Fault-driven interleavings of parallel appliers are exactly what generates divergence. Barrier resolved: `wsrep_sync_wait` is a sufficient per-node checksum barrier (appliers hold the apply monitor across the full commit) — the checker needs no extra cross-node precondition. |
| **Why It Matters** | THE terminal oracle: nearly every other data property reduces to it. Silent divergence is the worst PXC failure mode — undetected until a vote or a customer notices. Default faults suffice. |

Priority: **High** — terminal oracle, implement first; unique (no SUT-side equivalent exists). Provenance: focus 1.

**Open Questions:**

- None

### gtid-executed-cluster-convergence — gtid_executed converges identically on all Synced nodes

| | |
|---|---|
| **Type** | Safety |
| **Property** | At quiesced checkpoints, all Synced primary-component nodes report the identical `@@global.gtid_executed` set, and no node holds GTIDs outside the cluster UUID (no errant server-uuid GTIDs), given a workload that avoids the known-legitimate local-GTID generators (temp tables, RSU, `sql_log_bin=0`). |
| **Invariant** | `Always` (pairwise `GTID_SUBTRACT` empty in both directions at quiesced checkpoints, `wsrep_sync_wait` barrier — opportunistic under faults + guaranteed `eventually_`/`finally_` cadence per Shared conventions) + `Always` (no server-uuid GTIDs on any node — the generalized leak-family guard; suppressed during fenced RSU/`sql_log_bin` sabotage phases) + `Sometimes` ×3 (failed-TOI seqno consumption observed; tagged-GTID commit — PXC-4526 shape; checkpoint taken under `applier_threads>1` load — PXC-4652 shape) + vacuity guard (`gtid_executed` actually grew). |
| **Antithesis Angle** | Each node independently mints the next gno in the shared cluster sidno at its own group commit (`binlog.cc:1740-1811`; sid = cluster state UUID, `wsrep_mysqld.cc:673-694`) — convergence is an emergent claim, not a mechanism. `wsrep_write_dummy_event` is a no-op (`wsrep_binlog.cc:393-402` ← `wsrep_mysqld.cc:2408/:2443`): a seqno consumed with nothing binlogged. PXC-4652's single-call-site lock fix (`binlog.cc:1775-1778`) is a regression target under `applier_threads=4`; PXC-4526 escalates GTID disagreement to eviction. GTID divergence ≠ row divergence — the checksum oracle is blind to this plane. |
| **Why It Matters** | Bug pattern H had ZERO catalog coverage; the local-GTID leak family (PXC-4313/4312/4504/4238/4034/4544) is open by construction; errant GTIDs silently break async failover and PITR long after the run that minted them. |

Priority: **High** — terminal oracle for the GTID plane, sibling of `cross-node-row-equality` (neither dominates the other); rides the same checkpoint pass. Requires the `gtid_mode=ON` / `enforce_gtid_consistency=ON` / `log_replica_updates=ON` config addition (see deployment-topology.md). Provenance: evaluation gap-fill (Gap 1).

**Open Questions:**

- Does a failed TOI/NBO diverge `gtid_executed` intra-cluster, or only binlog continuity? `(pin at first triage)`
- Is the legitimate server-uuid-GTID generator set fully enumerated? `(partial: temp-table DROP sql_table.cc:3726/:3848, RSU, sql_log_bin=0 confirmed)`
- Is joiner `gtid_executed` exactly equal to the donor's after SST? `(the post-SST cross-check in the server is an empty stub — wsrep_mysqld.cc:789-792)`

### inconsistency-vote-evicts-divergent-minority — Voting evicts exactly the divergent minority

| | |
|---|---|
| **Type** | Safety |
| **Property** | When nodes disagree on an apply outcome, the inconsistency vote evicts exactly the sabotaged/divergent set and no one else. |
| **Invariant** | `Always`: after a vote round, exactly the deliberately-sabotaged node set leaves the group — detected as provider-Disconnected/non-Primary state on the evicted nodes (release-build eviction CLOSES the provider; mysqld stays alive), never as process death. `Sometimes`: a real-disagreement vote round occurred (workload injects deterministic single-node divergence — no debug build needed). |
| **Antithesis Angle** | Vote × view-change races (`group_recount_votes` returns false on conf change, `gcs_group.cpp:962`); corrupt-node abstention shifts the majority (`:2399`); CONFIRMED: the V7 recompute regex matches only 4-5-digit codes — no match → empty-string vote, so two differently-failing nodes can out-vote the one consistent node (`:1102/:1139-1143`); CONFIRMED NULL-deref precondition at `:1195-1197`; residual self-eviction shape: one-shot vote_history erase-on-read + lose-by-default can spuriously self-evict a delayed voter on a recount. |
| **Why It Matters** | The vote is PXC's only built-in divergence responder; if it evicts the wrong node, the cluster keeps the corrupt data and discards the good copy. |

Priority: **High** — the SUT's only self-defense mechanism, with two confirmed defect preconditions in its own logic. Provenance: focus 7 (empty-vote collision independently confirmed by focus 4's `vote-message-payload-contract`).

**Open Questions:**

- None

### evicted-node-rejoins-only-via-sst — An inconsistency-evicted node returns only via full SST

| | |
|---|---|
| **Type** | Safety |
| **Property** | After any inconsistency eviction or IST apply failure (every `mark_corrupt` caller), the node's next return to Synced is preceded by a full SST — never IST, never a resume from its divergent InnoDB position. |
| **Invariant** | `Always` (on a node carrying an eviction marker since its last SST: "IST received/complete" never precedes the next completed SST before Synced — log-scan + supervisor file probe; an opt-in variant additionally asserts grastate is absent/UNDEFINED after the eviction shutdown) + `Always` terminal post-rejoin checksums — including checksumming the evicted node BEFORE rejoin, so ROW full-image apply cannot self-heal the proof away + `Sometimes` ×2 (a full-SST rejoin after eviction completed; a kill landed inside the eviction-to-exit window `+kill`). |
| **Antithesis Angle** | The contract is PXC-5208 (HEAD is literally the merge; `repl.force_sst_after_inconsistency` defaults **no**, so field runs the pre-fix behavior). Five-entry `mark_corrupt` funnel (`replicator_smm.hpp:388-412`, `replicator_smm.cpp:501-506/:2292-2301`, `replicator_str.cpp:1602-1612/:1710-1717`); grastate zeroing is warn-only on write failure; `restore_saved_state` bypasses the corrupt latch (`saved_state.cpp:242-250`); an idle grastate is an adoptable real-uuid:-1 (`:263-297`) — position arbitration can adopt the divergent SE checkpoint. Kill-before-fsync, disk faults, and the latch bypass are the targets. |
| **Why It Matters** | A divergence-evicted node ISTing back means the cluster re-admits known-corrupt data under a Synced badge. This is the terminal arm of the vote family — it closes the rejoin loop that `inconsistency-vote-evicts-divergent-minority` and `privilege-context-divergence-never-evicts` both flag as "the rejoin risk" — and the newest code at HEAD is a prime regression surface. |

Priority: **High** — realized-contract property on the newest merge in the tree; the vote arm shares the sabotage-fenced variant, the IST-failure/kill legs are v1 `+kill`; `force_sst_after_inconsistency=yes` is a runtime-settable (wsrep_provider_options) config arm, no image variant needed. Provenance: evaluation gap-fill (Gap 8).

**Open Questions:**

- Reconcile the fix narrative ("the cluster then offers IST from the InnoDB position") with the position-arbitration gate. `(partial: all funnel entries read; the connect-time position channel is not fully traced — either answer at first triage is a finding)`
- Are there inconsistency exits that bypass the funnel entirely (e.g. `sst_received` sanity throws)?

### bf-bf-lock-suppression-no-divergence — BF-BF lock suppression is always a true false-positive

| | |
|---|---|
| **Type** | Safety |
| **Property** | With parallel appliers (± `cert.optimistic_pa`), InnoDB's deliberate suppression of BF-vs-BF lock conflicts never lets two genuinely-conflicting writesets apply in the wrong order — cross-node data stays identical. |
| **Invariant** | Workload `Always` (cross-node checksums, shared oracle with distinct message) + SUT-side `Sometimes` (missing) at the suppression site `lock0lock.cc:581-599` for coverage. |
| **Antithesis Angle** | The suppression comment says conflicts between HP transactions are "supposed to be false positives" — true only if cert keys are complete. Workload REQUIREMENT: PK-less FK-child tables under cascading DML — `wsrep_certify_nonPK` does NOT cover FK cascade rows (cascades are InnoDB-internal, keying only the FK index value, `row0ins.cc:1363`). Both suppression sites are independently reachable (probe each with a distinct `Sometimes`); the `cert.optimistic_pa` toggle (unlocked param-set race, `certification.cpp:1403-1422`) is an amplifier, not a standalone generator. Requires `wsrep_applier_threads>1`. |
| **Why It Matters** | Bug pattern A's silent half: where a missed cert key has no MDL footprint, this suppression converts it directly into divergence with no error anywhere. |

Priority: **High** — the silent twin of the dominant bug class. Provenance: focus 2.

**Open Questions:**

- None

### privilege-context-divergence-never-evicts — Privilege-dependent statements never cause vote eviction

| | |
|---|---|
| **Type** | Safety |
| **Property** | A statement whose outcome depends on session privileges never produces a cross-node apply disagreement that evicts a node. |
| **Invariant** | Workload `Always`: after privilege-varied statement batches (one missing privilege at a time), `wsrep_cluster_size == N` and all nodes Synced/Primary. SUT `Unreachable` (missing) at the vote-eviction paths (`replicator_smm.cpp:2411-2413`, no-vote self-eviction `:626-637`). `Sometimes`: source rejected a statement with an ACL error. |
| **Antithesis Angle** | Needs no faults beyond the workload — appliers run with different effective privilege context than the originating session; 10+ historical tickets (pattern B). Class is open by construction: fixes are per-ticket source-side pre-checks. A vote-evicted node rejoins via IST with its divergent SE position under default `force_sst_after_inconsistency=OFF` — run the eviction workload paired with the checksum oracle. The DEFINER family (VIEW/EVENT/PROCEDURE/trigger) is verified symmetric pre-TOI — dropped from the first-candidate list. |
| **Why It Matters** | Validated regression family (PXC-4709 fix 072bda8b11f, PXC-4765): an ordinary GRANT/DEFINER statement can knock a node out of the cluster. |

Priority: **High** — realized-bug family, zero fault requirements, high yield workload generator. Provenance: focus 6.

**Open Questions:**

- Which TOI statement types are still asymmetric? `(partial: DEFINER family verified symmetric; the remaining surface — auth-policy checks, partial_revokes, roles, sql_mode-dependent DDL errors — is not statically enumerable; the sweep is the workload's job)`

### sr-fragment-cross-node-agreement — SR fragment state agrees cluster-wide after crash recovery

| | |
|---|---|
| **Type** | Safety |
| **Property** | With streaming replication enabled, the persisted fragment-seqno sets in `mysql.wsrep_streaming_log` are identical on all nodes per SR transaction (or the transaction is absent everywhere), and the table is empty cluster-wide at quiescence. |
| **Invariant** | `Always` at quiesced checkpoints and after every restart/rejoin (cross-node diff of fragment sets; row count returns to ~0). `Sometimes`: a node crashed with a certified fragment in flight. |
| **Antithesis Angle** | Verified crash hole: fragment persisted seqno-NULL → certify → crash before seqno-update commit; recovery DELETES the NULL-seqno row (`wsrep_schema.cc:1211-1218`, in-code comment names the window) while N-1 peers hold the certified fragment. Orphan cleanup is best-effort ("removed manually", `server_state.cpp:1645-1648`); a `#if 0`'d double-commit assert marks a second window. Recovery re-delivery converges by design (a restarted origin always rejoins with a new incarnation UUID; cleanup keys on origin absence per view; (server_id, trx_id) keying precludes collisions) — targets narrow to the `adopt_error` "removed manually" path, the `s_prepared` (XA) exemption in orphan cleanup, and gcache-purge/SST edges. **Crash legs require node termination (kill channel); SR itself is UN-GATED — `wsrep_trx_fragment_size` is session-settable, no config variant needed (synthesis).** |
| **Why It Matters** | The GCF-810 SR crash-consistency suite is un-runnable in-tree — this ground is tested nowhere today. Fragment divergence is an early, attributable precursor of row divergence. |

Priority: **High** — documented crash window, zero existing coverage; non-crash legs v1-viable (session-set SR), crash legs kill-channel-gated. Provenance: focus 1 + focus 3 (independent rediscovery, merged).

**Open Questions:**

- Is the GCF-810 SR crash-consistency suite absence deliberate? `(needs human input — the repo contains no runnable SR crash-consistency test; include files verified missing)`

### sr-rollback-fragment-noop — Spurious SR rollback fragments are no-ops; SR resolution is all-or-nothing

| | |
|---|---|
| **Type** | Safety |
| **Property** | SR rollback fragments (delivered at-least-once by design) are no-ops when redundant; every fragment applies exactly once; a resolved SR transaction is all-or-nothing identical across nodes. |
| **Invariant** | Workload `Always` (post-resolution effects match the client outcome everywhere; `wsrep_streaming_log` empty at quiescence) + SUT `Sometimes` (missing, `server_state.cpp:316`) + `AlwaysOrUnreachable` (missing, `:397/:439` dummy sites: trx being dummied is known-rolled-back). |
| **Antithesis Angle** | Missing-context degradation: one node dummies a commit fragment that peers apply — silent divergence; rollback fragments bypass flow control. The rollback-vs-replay collision is unreachable by construction (certified BF-aborts go to s_must_replay and never emit rollback fragments) — weight sits on the dummy-demotion machinery under membership churn. Requires SR-enabled sessions; crash sub-case needs node termination (flagged). |
| **Why It Matters** | Same untested SR territory; the dummy-demotion machinery under membership churn is a verified silent-divergence path. |

Priority: **Medium** — shares the SR variant with the fragment-agreement property; implement together. Provenance: focus 9.

**Open Questions:**

- None

### applier-threads-never-read-only — Applier threads never inherit a read-only context

| | |
|---|---|
| **Type** | Safety |
| **Property** | No wsrep applier/service/replayer thread applies a writeset with an inherited read-only session context, and read-only globals + applier-pool resizes never cause vote eviction. |
| **Invariant** | SUT `Always` (missing) at `Wsrep_applier_service::apply_write_set` (`wsrep_high_priority_service.cc:640`) AND at `apply_nbo_begin`: `!thd->tx_read_only`. Workload `Always`: toggling `read_only`/`super_read_only` + resizing appliers under load never shrinks the cluster. `Sometimes`: writesets applied while a read-only global was set. |
| **Antithesis Angle** | Regression target PXC-5229 (fix f2a34b69292 = three scattered per-init-site clears, no central guard) and PXC-4849. CONFIRMED live gap: the NBO worker THD (`wsp::thd`, `wsrep_utils.cc:940-951`) never clears `transaction_read_only` — the workload must run NBO DDL under read-only flips. `read_only`/`super_read_only` explicitly exempt appliers (`check_readonly`), so the poisoning flag set narrows to `{transaction_read_only}`; SET PERSIST spawn paths funnel through the cleared `init_wsrep_thread` (restart variant needs restart faults, flagged). |
| **Why It Matters** | A read-only-poisoned applier fails applies on one node → vote → eviction from an operator no-op. |

Priority: **Medium** — realized bugs with an un-generalized fix; cheap to add to the divergence workload. Provenance: focus 6.

**Open Questions:**

- None

### autoinc-identity-no-cross-node-collision — Auto-increment identity never collides across nodes

| | |
|---|---|
| **Type** | Safety |
| **Property** | INSERTs omitting the auto-inc key never certification-conflict on a disjoint-by-construction workload, and per-node (offset, increment) settings are pairwise distinct with increment == cluster size in a stable PC. |
| **Invariant** | `Always` (no cert conflict on disjoint identity inserts, with `wsrep_retry_autocommit=0` so retries can't mask) + `Always` (offsets pairwise distinct per stable view) + `Sometimes` (a view change overlapped an in-flight identity insert). |
| **Antithesis Angle** | A per-statement session refresh EXISTS (`reset_for_next_command` re-copies the globals), so the wide stale-session leg collapses. Live channels: (a) the refresh reads the globals UNLOCKED while `log_view` rewrites them under lock (`wsrep_server_service.cc:196-198`) — a torn (old-offset, new-increment) pair is a real collision channel; (b) statements in flight across a view change; (c) the X-plugin document-id aggregator's configure-once cache (`document_id_aggregator.cc:38-60`) — untouched by the refresh, the widest window. Vendor evidence: disabled galera-x test for "blinking auto-generated ids" (golden-file flakiness, not a portable repro). |
| **Why It Matters** | Stale session auto-inc settings → duplicate keys → spurious cert conflicts or (non-unique-key leg) silently colliding rows. |

Priority: **Medium** — clear mechanism, easy workload, modest blast radius. Provenance: focus 11.

**Open Questions:**

- None

## 2. Commit durability & crash recovery

What survives `kill -9`. PXC persists its replication position in two places written by
different subsystems (grastate.dat, InnoDB TRX_SYS XID) plus the gcache ring buffer, and
the arbitration between them after a crash is the historical bug nest. Everything here
**requires node-termination faults** unless noted.

### acked-commit-durable-across-restart — Acknowledged commits survive crash and rejoin

| | |
|---|---|
| **Type** | Safety |
| **Property** | Every client-acknowledged write is present on all Synced nodes after any single node crash/rejoin; with `innodb_flush_log_at_trx_commit=1`, after full-cluster crash. |
| **Invariant** | `Always` (ack journal: every acked unique-keyed write present cluster-wide after recovery). Companion `Sometimes`: a node was killed inside the certify-to-engine-commit window. |
| **Antithesis Angle** | Attacks the doc claim "lose any node ... without any data loss" (intro.rst:28-30) against the in-code caveat "successful return does not guarantee delivery to group" (`gcs_core.hpp:102`). Ack point verified: client OK follows self-delivery of the O_SAFE writeset — every PC member has physically received it — so kills may take the originator PLUS additional nodes, as long as one PC member survives with RAM intact. flush=2 is the MTR suite posture, NOT the shipped default (PXC ships MySQL's flush=1); MTR never tests durability composition. **Requires node termination**; disk faults widen the cluster-wide variant. |
| **Why It Matters** | The product's headline guarantee, tested nowhere in-tree. |

Priority: **High — moved to the phase-2 tranche (synthesis)**: dead without ungraceful termination (graceful restarts cannot challenge durability); implement as soon as the workload→supervisor kill channel is proven. Shared ack-journal infra with `non-primary-rejects-writes` should still be built in tranche 1. Provenance: focus 1 (cross-linked to focus 7's ack journal).

**Open Questions:**

- None

### grastate-se-checkpoint-agreement — grastate.dat agrees with the SE checkpoint at every start

| | |
|---|---|
| **Type** | Safety |
| **Property** | At every mysqld start on a used datadir: grastate.dat parses cleanly AND (seqno == -1 OR (UUID matches AND seqno == InnoDB SE checkpoint)). |
| **Invariant** | `Always`, evaluated by a harness startup probe (`--wsrep_recover` pre-join; the evidence is re-blanked after join). Companion `Sometimes`: both recovery branches (seqno=-1 and seqno!=-1) observed. |
| **Antithesis Angle** | grastate is rewritten in place, non-atomically, warnings-only on failure (`saved_state.cpp:364-405`); a stale non-(-1) seqno silently wins over the SE checkpoint (`replicator_smm.cpp:266-286`); wrappers institutionalize the trust. Kill points: shutdown (the fsynced grastate write happens in the signal-handler thread while InnoDB's final redo flush comes much later in the main thread — wide window, not one race), NBO/TOI unsafe_ window, post-IST blanking, and the provider pause() window (FTWRL/desync/donor — `pause()` stamps a real seqno into grastate MID-RUN, `replicator_smm.cpp:3385-3415`, reset only in `resume()`; a kill inside the pause yields grastate-ahead with no shutdown involved). **Requires node termination.** |
| **Why It Matters** | Realized bug PXC-4845 (cluster-wide FC lockup); the fix's own commit text concedes "no good solution for storing wsrep checkpoints in SE". Wrong position = silent lost transactions or skipped SST. |

Priority: **High — moved to the phase-2 tranche (synthesis)**: near-vacuous under graceful-only restarts (both files trivially agree — v1 would mostly validate the harness's own supervisor); the cheap boot-time probe ships in tranche 1 as a supervisor extension, but the property earns its keep once the kill channel lands. Provenance: focus 1 + focus 3 (merged).

**Open Questions:**

- None

### gcache-recovered-ist-completeness — A donor with crash-recovered gcache never serves silently-incomplete IST

| | |
|---|---|
| **Type** | Safety |
| **Property** | An IST-joined Synced node has identical state to the cluster; a donor whose gcache was crash-recovered serves complete IST or falls back to SST / refuses — never silently incomplete. |
| **Invariant** | `Always` (cross-node checksums after every IST join — shared oracle, distinct message). `Sometimes` guards: donor served IST after its own ungraceful restart; gcache `scan()` recovery ran non-trivially. |
| **Antithesis Angle** | gcache payload is never msync'd; truncated-tail recovery is warn-only (`gcache_rb_store.cpp:1307-1317`); page-store non-recovery only SHORTENS the served range (surprise SST — visible, not silent); the donor contiguity assert is commented out. The silent channels are metadata corruption inside the sanity epsilon (a flipped BUFFER_SKIPPED bit ships a real writeset as IST `T_SKIP` — silent omission) and the low-water-only range advertisement; payload corruption fails loudly at the joiner (writeset CRCs). **Requires node termination; disk faults widen.** |
| **Why It Matters** | PXC-5209 and MDEV-36621 are realized bugs in exactly this machinery (MDEV-36621 verified FIXED in the vendored galera 4.27 — regression target); lost writesets via post-crash IST are silent data loss on the joiner. |

Priority: **High** — realized bugs both sides (joiner and donor), directly feeds the terminal oracle. Provenance: focus 1.

**Open Questions:**

- None

### wsrep-xid-checkpoint-monotonic — The InnoDB wsrep XID checkpoint never regresses

| | |
|---|---|
| **Type** | Safety |
| **Property** | The TRX_SYS wsrep XID seqno never decreases within an unchanged cluster UUID for group-commit writes; any explicit-checkpoint-path writer (TOI end, NBO phase-one end, view/SST handlers) is carved out but individually tracked. |
| **Invariant** | SUT-side `Always` (missing) on every checkpoint write — re-enables the commented-out assert at `trx0sys.cc:481-482` as an SDK assertion: STRICT for group-commit writes (serialized under `LOCK_commit` — any firing there is a real bug); carve-out for any explicit `wsrep_set_SE_checkpoint` caller (they all write outside `LOCK_commit`), with each carved-out regression recorded via a companion `Sometimes` — the NBO phase-one decrease DOES reach disk (immediate flush of the stale seqno), a confirmed real durability defect. Weaker workload proxy: post-restart recovered position >= highest confirmed commit. |
| **Antithesis Angle** | The disabled assert's comment enumerates four known violation shapes (atomic DDL double-persist, TOI INSERT...SELECT, NBO "Yes, this is a bug. TODO.", non-group-commit paths). The SUT-side form needs no faults; the workload proxy needs kill/restart. |
| **Why It Matters** | A regressed XID mis-anchors every crash recovery (feeds `grastate-se-checkpoint-agreement`); PXC-4498/PXC-5286 lineage. |

Priority: **Medium** — high diagnostic value but requires SUT-side instrumentation to be sharp. Provenance: focus 1.

**Open Questions:**

- None

### gcache-crash-recovery-no-abort — Torn gcache recovery completes safely

| | |
|---|---|
| **Type** | Safety |
| **Property** | After kill -9, recovery of a torn galera.cache completes (gapless suffix or clean reset) without abort/bad-free/OOM, and no broken IST is served afterward. |
| **Invariant** | `Always` per restart-after-kill (process reaches ready or exits with explicit error; RSS/page-count bounded during recovery). |
| **Antithesis Angle** | Post-kill strong checks are skipped (offset=-1 → `scan()`, `gcache_rb_store.cpp:1332-1361`); PXC-5209's `do_sanity_checks` (`:1078-1116`) has an epsilon of ±5.6M seqnos; the page store is never recovered (`count_=0`). The OOM class is bounded, not eliminated (DeqMap null-padding of an in-epsilon bogus seqno ≈45MB @128M cache — RSS threshold sizeable from code as 8×size_cache_/24); orphan gcache.page.* accumulation across repeated crashes is UNBOUNDED (no startup cleanup anywhere); a donor-side companion check is required (the metadata shapes pass every local check). **Requires node termination**; small-gcache variant sharpens. |
| **Why It Matters** | PXC-5209 realized (crash-on-recovery); MDEV-36621 is the donor corollary. Recovery code that aborts turns one crash into a crash loop. |

Priority: **Medium** — realized bug, mechanism high-confidence, residual risk medium. Provenance: focus 3.

**Open Questions:**

- None

### cluster-identity-single-lineage — One cluster lineage, ever

| | |
|---|---|
| **Type** | Safety |
| **Property** | The set of `wsrep_cluster_state_uuid` over ready nodes always equals the recorded original lineage; no supervisor environment carries `--wsrep-new-cluster` outside an explicit bootstrap. |
| **Invariant** | `Always` (lineage set check) + `Always` (no `--wsrep-new-cluster` in any node's supervisor environment outside the recorded bootstrap) + `Sometimes` (a node restarted while the bootstrap env flag was still present). |
| **Antithesis Angle** | RETARGETED: `mysqld_bootstrap.in` is NOT shipped (zero build references); the shipped vector is `mysql@bootstrap.service` plus a persistent EnvironmentFile carrying `EXTRA_ARGS="--wsrep-new-cluster"` — persistent BY DESIGN (an enabled/reused bootstrap unit re-bootstraps on every start; the unit's own comments concede it), so no interruption timing is needed. The argv filter re-honors the env flag on every exec; sticky case: an empty `wsrep_cluster_address` at boot leaves the flag uncleared for a later `SET GLOBAL`. Combined with `safe_to_bootstrap=1` written on every singleton view, a routine restart founds a second lineage. **Needs restarts (composer-driven supervisor-env emulation suffices).** |
| **Why It Matters** | A sequential lineage fork is invisible to instantaneous two-primaries checks; clients transparently write to a new empty-ish cluster. |

Priority: **Medium — deferred (synthesis)**: the v1 marker-file supervisor (bootstrap guarded by a first-boot marker, no persistent env) REMOVES the shipped persistent-EnvironmentFile attack surface — in v1 the env-clause is vacuous and the property is permanently green / zero information. Deferred to the **field-faithful supervisor variant** (future work); the residual safe_to_bootstrap vector is covered by `no-dual-bootstrap-after-full-shutdown`'s restated form. Provenance: focus 11.

**Open Questions:**

- Does the harness run a persistent supervisor env (systemd or an equivalent persistent EnvironmentFile)? Without one there is no poisoning to test and the property narrows to the safe_to_bootstrap vector — decide at harness build. `(needs human input)`

### crash-recovery-grep-yields-true-position — The wrapper recovery dance yields the true position

| | |
|---|---|
| **Type** | Safety |
| **Property** | A restarted node's `--wsrep_start_position` equals the position printed by the same boot's recovery pass, and a previously-running node becomes ready within a bound or fails explicitly. |
| **Invariant** | `Always` (wrapper-layer probe comparing the two) + `Always` (bounded ready-or-error) + `Sometimes` on both grastate branches. |
| **Antithesis Angle** | RETARGETED to the SHIPPED flow: RPM/deb units route recovery through `/usr/bin/mysql-systemd galera-recovery`, whose bracketed `[WSREP] Recovered position:` grep works against the actual emission (`log_sink_trad.cc:333-337`); `mysqld_safe` also greps the bracketed form. The unbracketed-grep scripts (`mysqld_pre_systemd.in:116`, `build-ps/debian/extra/mysql-helpers:83`) are VESTIGIAL — `WITH_SYSTEMD` is forced OFF in PXC builds and they ship nowhere. Residual targets: stale `systemctl set-environment MYSQLD_RECOVER_START` replay; torn-grastate verbatim shortcut; wrapper-vs-server drift in the emitted line. **Needs unclean stops (termination or workload kill -9).** |
| **Why It Matters** | The packaging layer is what fields actually run; the property guards the shipped working recovery flow against drift (the anticipated broken-grep field outage dissolved — those scripts are unshipped). |

Priority: **Low — deferred (synthesis)**: the v1 entrypoint supervisor REPLACES the shipped wrapper layer, so in v1 this property tests harness code, not the SUT's packaging. Deferred to a **field-faithful supervisor variant** (running the shipped `mysql-systemd galera-recovery` flow inside the container) — recorded as future work. Provenance: focus 11.

**Open Questions:**

- None

## 3. State transfer lifecycle (SST/IST)

Joining, donating, and the shell-script layer in between. SST scripts run as root-ish
processes with kill windows, port squatting, and credential lifecycles; IST correctness
hinges on gcache bookkeeping and an overlap-dedup gate with realized bugs in both
directions.

### ist-overlap-writesets-not-reapplied — IST/live overlap deduplicates exactly once

| | |
|---|---|
| **Type** | Safety |
| **Property** | Writesets delivered via both IST and the live channel are applied exactly once (deduped by the `global_seqno <= apply_monitor.last_left()` gate); nothing outside that set is skipped. |
| **Invariant** | Workload `Always` (cross-node checksums after every JOINER→SYNCED + counter-table `v=v+1` expected-value check) + SUT `Sometimes` (missing) at the `replicator_smm.cpp:2252` overlap path. |
| **Antithesis Angle** | Gate safety rests on a "should be safe" drain-ordering comment; the guarding assert is NDEBUG'd. Both failure directions realized: PXC-4845 (wrong-skip → FC stall) and MDEV-36621 (lost writesets). Partition faults suffice; the crash-recovery variant needs node termination (flagged). |
| **Why It Matters** | Double-apply or skip during every IST rejoin — the most common recovery path — is silent divergence. |

Priority: **High** — realized bugs both directions, default faults reach it, expected-value oracle is sharp. Provenance: focus 9.

**Open Questions:**

- None

### restarted-node-rejoins-synced — A restarted node rejoins and reaches Synced

| | |
|---|---|
| **Type** | Liveness |
| **Property** | Every restarted node eventually reaches SYNCED via IST or SST, and the cluster converges (all Synced, size N, `wsrep_last_committed` within delta). |
| **Invariant** | Workload `Always` at final quiescence + missing `Sometimes` markers for the DISTINCT outcomes: joiner completed IST (`replicator_str.cpp:1516`); donor fell back IST→SST (`:591-597`); joiner completed full SST; killed-during-IST then recovered; cert-index preload without IST (`:1479`). |
| **Antithesis Angle** | The 10s IST SocketWatchdog (`ist.cpp:356-365`) is PER-MESSAGE — the liveness bound must tolerate ≥1 abort+restart and count consecutive IST-abort loops as failure. Safety companion finding: IST-only joins deliberately keep the old grastate through IST (PXC-4631) — graceful restart → IST-only join → kill -9 mid-IST → restart claims the pre-IST position while InnoDB is ahead → the next IST RE-APPLIES already-applied writesets (liveness→safety conversion; detectors are the checksum/expected-value oracles and `grastate-se-checkpoint-agreement`; trigger needs termination plus a graceful-restart-then-kill sequence). Also: stale donor gcache snapshot with zero IST safety gap (`gcs_group.cpp:1817`); unbounded STR/SST waits. **Node termination for full value (flagged); partitions cover the IST-rejoin half.** |
| **Why It Matters** | Rejoin is the recovery path every other property depends on; a node that can't come back converts every transient fault into permanent capacity loss. |

Priority: **High** — the central recovery-liveness property; its Sometimes markers structure exploration for the whole SST/IST category. Provenance: focus 8.

**Open Questions:**

- None

### donor-returns-to-synced — Donor/desynced node returns to SYNCED; desync accounting never leaks

| | |
|---|---|
| **Type** | Liveness (stable-state) |
| **Property** | After donation/desync ends and load quiesces, no node remains in Donor/Desynced; desync reference counting is balanced. |
| **Invariant** | Workload `Always` at quiesced checkpoints (no active desync source ⇒ state=4, `wsrep_desync=OFF`, and `wsrep_desync_count == 0` — the gcs-group counter IS an exported status variable, a direct leak oracle) + `Sometimes` (donor returned Synced after a faulted donation; `resume_and_resync` failure path — warn-only today). |
| **Antithesis Angle** | The permanent-desync mode is documented in-code (`gcs.cpp:2743-2755`: out-of-order JOIN seqnos → "will not become synced again unless temporarily removed"); the decrement assert is NDEBUG'd (`gcs_group.cpp:1305`); a PXC-only early return can drop a rebalancing JOIN; resync failures are swallowed. Three disagreeing bookkeepers (gcs_group count, wsrep-lib `desync_count_`, sysvar); the `gcs.cpp:2753` guard is send-side-only (lost-decrement paths live), and conf changes re-derive the count — a self-leak survives churn unmasked. No special faults needed. |
| **Why It Matters** | A stuck donor keeps `wsrep_ready=ON` and passes health checks while serving increasingly stale reads — invisible degradation (cross-links `clustercheck-200-implies-write-progress`). |

Priority: **Medium** — documented failure mode, workload-drivable, moderate findability. Provenance: focus 7 + focus 8 (merged).

**Open Questions:**

- RSU (`wsrep_OSU_method=RSU`) desync/resync under concurrent view change — same leak via `wsrep_RSU_end`'s swallowed failure? `(partial: the swallowed-failure pattern confirmed in code (wsrep_mysqld.cc:2938-2944); whether a view change can make the resync call fail mid-RSU is untested — include as a workload action, verify empirically)`

### interrupted-sst-forces-full-sst — An interrupted SST past the point of no return forces a full SST

| | |
|---|---|
| **Type** | Safety |
| **Property** | Once an SST has passed the no-way-back point and is interrupted, persisted state demands a full transfer (grastate absent / UUID UNDEFINED / seqno -1) and the next transfer is an SST, not IST. |
| **Invariant** | `AlwaysOrUnreachable`, evaluated on restarts after an observed interrupted SST past no-way-back (the qualifying path is rare and fault-dependent, so "never executed" is acceptable but any execution must satisfy it). |
| **Antithesis Angle** | Break points: warnings-only `mark_unsafe` write; -EAGAIN restore of stale first-constructor state (`saved_state.cpp:171-176`); clone-SST hand-written grastate with no fsync/rename (`wsrep_sst_clone.sh:1199-1224`); non-exiting SIGTERM trap; posix_spawn orphans (no PDEATHSIG) mutating the datadir under a restarted mysqld; fatal-signal handler self-deadlock on LOCK_wsrep_sst. Gate arming is externally observable: the "Proceeding with SST........." log line marks exactly the no-way-back boundary (script stderr lands in the mysqld error log; the wipe's `cpat` preserves `*.err`/`*.log`); InnoDB REFUSES a normal start over a half-wiped datadir — violations surface as a stuck-down node, except wrapper-script starts which wipe-and-reinitialize visibly. **Requires node termination.** |
| **Why It Matters** | A joiner that ISTs on top of a half-wiped datadir is silent corruption; orphaned SST processes corrupt the successor's datadir. |

Priority: **Medium** — high mechanism confidence, needs script-side observability work. Provenance: focus 3.

**Open Questions:**

- None

### failed-state-transfer-node-rejoins — A node whose SST/IST failed eventually rejoins

| | |
|---|---|
| **Type** | Liveness |
| **Property** | A node whose state transfer failed eventually reaches Synced after restart once faults heal — no crash loop, no restart blocked by orphaned SST processes. |
| **Invariant** | `Sometimes(rejoined-after-failed-transfer)` + `Sometimes(transfer failure observed)` + bounded consecutive same-cause aborts (workload watchdog). |
| **Antithesis Angle** | IST watchdog 10s (`ist.cpp:359`) → `mark_corrupt` + abort (`replicator_str.cpp:1678-1718`) — **default faults reach this arm** (needs harness restart-on-abort supervision; shipped systemd would NOT restart); kill arms need termination. `mark_corrupt(false)` still writes UNDEFINED:-1 via the warnings-only writer — a failed write loops the crash. Orphans hold port 4444; NBO-in-flight → deterministic joiner abort. Deterministic crash-loop candidates with the position PRESERVED: `replicator_str.cpp:946` (state-request prep failure — NBO-in-flight or squatted IST port) and `:1010` (-ENODATA on an IST-only request while the donor gcache advances under load). Donor FC pause CANNOT fire the joiner watchdog (the IST sender is a dedicated thread streaming pre-stored buffers) — a firing needs a genuine >10s per-message stall. |
| **Why It Matters** | Failed-transfer retry is the difference between a transient fault and a permanently lost node. |

Priority: **High — PROMOTED to the v1 top-10 (synthesis)**: the IST-watchdog abort arm is v1's only default-fault path to ungraceful process death (harness restart-on-abort supervision required), making this property the gateway to all incidental crash-recovery coverage before the kill channel/termination faults land; kill arms remain +kill. Provenance: focus 3.

**Open Questions:**

- None

### joiner-reaches-synced-after-state-transfer — JOINED nodes drain to SYNCED under load

| | |
|---|---|
| **Type** | Liveness |
| **Property** | Every JOINED node reaches SYNCED in bounded time despite continuing bounded-rate load. |
| **Invariant** | `Sometimes(JOINED→SYNCED under load)` + workload watchdog: state 3 persisting > N min after fault heal = failure. |
| **Antithesis Angle** | SYNC is gated on queue <= lower limit and sent one-shot per attempt (`sync_sent_`, `gcs.cpp:690-717` — reset on every conf change/send error/failed SYNC, so the feared lost-signal wedge does not exist); `gcs.cpp:2726-2751` documents a permanent never-synced mode. MTR never tests JOINED-drain under load. |
| **Why It Matters** | A perpetual JOINED node is capacity that never returns; the value is in FC-vs-SYNC-gate composition and drain timing under load, not a missing reset. |

Priority: **Medium**. Provenance: focus 5.

**Open Questions:**

- What is a fair drain bound N under throttled load? `(partial: gate mechanics confirmed in code, no code-side bound exists; set N from a calibration run — too small = false positives during legitimately slow catch-up)`
- Does the JOINED node's FC pause writers fast enough at fc_limit=100 with many client connections? `(partial: FC_STOP from JOINED confirmed in code; drain oscillation under many clients is empirical — set the write-rate cap from the same calibration run)`

### sst-ready-message-implies-listener — SST control messages are truthful and complete

| | |
|---|---|
| **Type** | Safety |
| **Property** | The joiner's "ready <addr>" implies a bound listener; every SST reaches Synced or fails explicitly within a bound; the donor control loop never acts on an unknown/spurious control word. |
| **Invariant** | `Always` (workload: SST completes or errors within a bound — recalibrated to ~220s from the concrete script-side bounds of ~100s initial + ~120s idle-stall, pinned after the calibration run; the earlier 10-min figure is superseded) + `Unreachable` (SUT-side, missing, `wsrep_sst.cc:1499`: unknown control word exits the donor loop) + `Sometimes` (SST completed under active faults). |
| **Antithesis Angle** | VERIFIED: unconditional "ready" echo after 300×0.2s (`xtrabackup-v2.sh:1317`) regardless of listener state. The torn-snapshot arm (spurious "continue" releasing the write freeze mid-backup, `:1487-1494`) is RESCOPED to rsync-enabled configs: only the rsync script emits "flush tables", and the default `wsrep_sst_allowed_methods` rejects rsync — in default config a spurious "continue" is a no-op. Caveat: an orphaned SST child holding the script's stdout pipe (no PDEATHSIG) can defeat the EOF that bounds mysqld's read — the orphan-tree arm of `failed-state-transfer-node-rejoins`. Stronger with node termination (flagged). |
| **Why It Matters** | The SST control channel is newline-delimited stdout between shell scripts — one stray line corrupts a state transfer. |

Priority: **Medium — keep-as-message (synthesis)**: the v1 remainder duplicates `restarted-node-rejoins-synced`'s timeout; fold the bounded-completion check into that property's watchdog and keep this entry as documentation of the control-channel hazard, not separate implementation effort. Provenance: focus 4.

**Open Questions:**

- None

### sst-grant-all-user-locked-or-absent — The SST superuser account is never left unlocked

| | |
|---|---|
| **Type** | Safety |
| **Property** | Outside in-flight SST post-processing AND outside an active donation, no node has an unlocked `'mysql.pxc.sst.user'@localhost` (account_locked='N' count == 0). |
| **Invariant** | `Always` (gated on no SST post-processing in flight AND node not Donor — `wsrep_local_state != 2`) + `Sometimes` (SST post-processing window actually crossed under faults) + `Sometimes` (donor killed mid-donation). |
| **Antithesis Angle** | The account is created unlocked with GRANT ALL BEFORE any transfer step (`wsrep_sst_common.sh:875-881`) and locked-never-dropped at the end (`:963-972`); three kill-9-before-lock error paths plus external-kill windows (no PDEATHSIG, non-exiting SIGTERM trap). Persists across restarts with `sql_log_bin=OFF` — invisible to replication and audit; propagates via future SSTs. Cleanest trigger = node kill during post-processing (flagged); fallbacks: disk-full tmpdir, sidecar kill of the temp mysqld. SECOND, donor-side lifecycle in server code: `wsrep_create_sst_user` (`wsrep_sst.cc:1272-1335`) creates the account UNLOCKED (role-scoped near-superuser) on the donor at every donation start, dropped only after the donor script exits (`:1551`) — a donor kill mid-donation leaves it until that node's next donation; and every SST stream ships the unlocked donor copy inside mysql.user to the joiner (joiner post-processing is what locks it). |
| **Why It Matters** | A leftover unlocked GRANT-ALL account is a security hole the SUT itself creates; exploitability medium (random discarded password) but persistence is silent. |

Priority: **Medium** — security blast radius, needs kill-window targeting. Provenance: focus 6.

**Open Questions:**

- None

### cluster-member-strings-never-reach-shell — Peer-supplied strings never reach /bin/sh unvalidated

| | |
|---|---|
| **Type** | Safety |
| **Property** | Peer self-reported node name/address and joiner SST payload never reach `/bin/sh -c` unvalidated. |
| **Invariant** | `Unreachable` (sentinel side-effect watcher: a harness node configured with metacharacter-laden `wsrep_node_name` must never cause the side effect) + `Sometimes` on the rejection paths (`wsrep_notify.cc:83-90`, `wsrep_sst.cc:1696-1707`) for non-vacuity. |
| **Antithesis Angle** | Regression targets PXC-5240/CVE-2026-49261 and PXC-5241/CVE-2026-48165 — both fixed by allowlists in this tree; the sink is `wsp::process {"sh","-c",str_}` (`wsrep_utils.cc:392`). Malicious member modeled via node config (sufficient — the vulnerable inputs are config-derived). Evidence also documents a residual snprintf-accumulation OOB in wsrep_notify. |
| **Why It Matters** | Cluster-member-to-RCE is the worst-case security escalation; two CVEs in this exact surface within the last year. |

Priority: **Low — DEMOTED to CI/variant-only (synthesis)**: the check is deterministic with no timing component (CI/unit territory), and the sentinel node's metacharacter-laden name is allowlist-REJECTED AT STARTUP, breaking the 3-node baseline — it needs its own fenced variant environment, and an ambient hostile name would pollute every other property's runs (poison-budget convention). Provenance: focus 6.

**Open Questions:**

- None

## 4. Cluster membership & quorum

Split-brain prevention, partition healing, bootstrap safety, and the group-communication
total-order axioms everything above assumes.

### at-most-one-primary-component — Never two disjoint Primary Components

| | |
|---|---|
| **Type** | Safety |
| **Property** | Nodes reporting Primary never form disjoint member sets. |
| **Invariant** | `Always`: per poll round, the `wsrep_incoming_addresses` sets of Primary-reporting nodes are never disjoint (disjointness makes the racy poll sound). `Sometimes` vacuity guard: a non-Primary window was actually observed. |
| **Antithesis Angle** | Asymmetric partitions (never tested in-tree) vs weighted quorum arithmetic (`pc_proto.cpp:555-575` strict >; tie → both non-prim); npvo tiebreak `:1064-1100` (loser gu_throw_fatals). Asymmetric-LEAVE double-credit is arithmetically precluded within a shared pc_view epoch — residual triggers are `pc.weight`-change races (explicit conservative non-prim shift in code: availability loss, not split-brain) and different-epoch merges. The disjointness check tolerates the confirmed status-variable lag window by construction. |
| **Why It Matters** | Split-brain is the cardinal cluster sin; every durability property presumes this one. |

Priority: **High** — foundational; default network faults are exactly its domain. Provenance: focus 7.

**Open Questions:**

- None

### partition-heal-single-primary-remerge — Partitions re-merge to a single Primary promptly

| | |
|---|---|
| **Type** | Liveness |
| **Property** | After a partition heals, all nodes converge to a single Primary Component and readiness within a bound (~90s to tune). |
| **Invariant** | `Sometimes(all nodes single-Primary + ready within bound of heal)` + end-of-run quiesced `Always` (converged at final quiescence). Bound is SPLIT: Primary-status convergence is membership-timer-bounded (state-transfer independent); the ready/Synced clause must be quiesced or transfer-aware. |
| **Antithesis Angle** | Real timer defaults (suspect 5s / inactive 15s / max_install 3 — MTR relaxes them). Targets: un()-state re-merge block (`pc_proto.cpp:964-972`); ALL-greatest-view-members-present requirement (`:1054`); `check_inactive` self-skip under CPU throttle (`evs_proto.cpp:899-909`); install-timeout "giving up" suicide escalation (`:680-739`). `auto_evict=0` default → a flapping node churns forever. |
| **Why It Matters** | Re-merge failure converts every transient partition into an outage; the giving-up abort + systemd SIGABRT-exclusion = permanently down node in the field. |

Priority: **High** — pure default-fault territory, untested at real timer values. Provenance: focus 7.

**Open Questions:**

- Validate the numeric bound. `(partial: code timer arithmetic gives ≈60s legitimate worst case for the membership part; empirical pinning of the ~90s value is a harness-tuning task)`

### no-dual-bootstrap-after-full-shutdown — safe_to_bootstrap never permits two founders

| | |
|---|---|
| **Type** | Safety |
| **Property** | After any full-cluster stop, at most one node's grastate carries `safe_to_bootstrap: 1`; at runtime all Primary nodes share one state UUID. |
| **Invariant** | `Always` at full-stop checkpoints (≤1 flag set) + runtime `Always` (single state UUID over Primary nodes) + `Sometimes` (flag-driven restart cycle exercised). |
| **Antithesis Angle** | The flag is set on EVERY singleton primary view (`replicator_smm.cpp:3245`); enforcement is connect-time only. The concurrent-LEAVE race is NOT winnable (S_LEAVING never GATHERs/installs) — the live trigger recipes are (a) a stale flag=1 surviving an unclean kill during the write-deferral window (re-persisting 0 is deferred while the unsafe counter is nonzero, so only unclean stops matter) and (b) the torn-file shape: the parser's constructor default is safe_to_bootstrap TRUE, so a missing or space-padded line reads as 1. Needs workload-driven stop/kill/restart cycles (docker stop with >10s grace, plus kills for the stale-flag shape — flagged). |
| **Why It Matters** | Dual bootstrap = two lineages = silent split of all future writes. |

Priority: **High** — cheap checks, catastrophic failure mode. **Restated for v1 (synthesis)**: the workload cannot read grastate files — the flag check is a per-boot supervisor emission (JSONL) consumed by the workload against an epoch ledger of observed bootstraps/UUIDs; the v1 marker-file supervisor removes the shipped-env vector (see `cluster-identity-single-lineage`), so v1 weight sits on the safe_to_bootstrap/torn-file shapes (+kill). Provenance: focus 7.

**Open Questions:**

- Does the harness restart policy mirror field automation (e.g. the operator's "most advanced node" recovery)? The severity claim assumes flag-driven bootstrap automation exists in the field. `(needs human input)`

### full-cluster-restart-reaches-primary — Full-cluster restart re-forms Primary within a bound

| | |
|---|---|
| **Type** | Liveness |
| **Property** | After all nodes stop ungracefully and restart, the cluster re-forms Primary via pc.recovery and every node reaches ready within a bound — without operator intervention. |
| **Invariant** | Workload `Always` after any all-down window: within T of the last node's start, all nodes Primary + ready=ON + ONE common state UUID (the UUID clause also catches dual bootstrap). Missing `Sometimes` markers: restored-prim wait entered (`pc.cpp:106-110`); PC restored from gvwstate. |
| **Antithesis Angle** | `pc.wait_restored_prim_timeout` default PT0S = wait FOREVER with the SQL port closed (verified `pc.cpp:92-114`, `defaults.cpp:67`) — invisible to health checks; gvwstate is deleted on graceful close so only ungraceful all-down reaches this path; re-merge needs ALL last-prim members; one-shot DNS amplifies on re-IP; a torn gvwstate is silently discarded. **Requires all-nodes-down (termination or harness fleet restart).** |
| **Why It Matters** | Full-datacenter power events are the canonical Galera incident; the undocumented indefinite wait is a field trap. |

Priority: **Medium** — high value but gated on the all-down capability. Provenance: focus 3 + focus 8 (same slug, reconciled).

**Open Questions:**

- Do packaged configs override `pc.wait_restored_prim_timeout` PT0S? `(needs human input — re-confirmed zero in-tree overrides; packaged docker entrypoints/operator configs live outside this repo)`

### vote-message-payload-contract — Vote messages are well-formed and votes are meaningful

| | |
|---|---|
| **Type** | Safety |
| **Property** | Inconsistency-vote messages honor their schema: recompute never manufactures colliding empty votes; post-eviction survivors agree. |
| **Invariant** | `AlwaysOrUnreachable` (non-empty source msg ⇒ non-empty code list in recompute) + workload `Always` (survivors consistent after any eviction). **The `strlen(NULL)` SIGSEGV `Unreachable` (`gcs_group.cpp:1197`) is DROPPED (synthesis)** — the precondition is undrivable by any planned fault, ever (same-version senders always append ≥1 NUL; only transport truncation or a foreign sender reaches it). The defect stays on the candidate upstream-report list. |
| **Antithesis Angle** | The recompute regex accepts only 4-5-digit codes; no match → empty-string vote (`:1139-1143`) — two differently-broken nodes can out-vote the consistent one (shared finding with `inconsistency-vote-evicts-divergent-minority`). Same-version senders always append ≥1 NUL byte (`gcs_core.cpp:1571-1594`) — the NULL-payload `Unreachable` fires only on transport truncation or a foreign sender; an empty vote can never collide with a Success vote (bit 63 forced), only with another empty vote — which is the defect. Harness negotiates wsrep V7 + GCS 6: TOI/NBO votes code-only, DML votes full locale-dependent text. |
| **Why It Matters** | A crashable NULL-deref in the arbitration path and a vote-collision defect in the recompute are both in the machinery that decides who keeps their data. |

Priority: **Medium** — SUT-side instrumentation needed for the sharp form. Provenance: focus 4.

**Open Questions:**

- None

### gcs-total-order-gap-free — Group communication delivers gap-free, FIFO, in-view

| | |
|---|---|
| **Type** | Safety (reachability-flavored) |
| **Property** | The total-order axioms hold: no per-source O_SAFE gaps at PC, no GCS send/recv FIFO violation, no EVS message accepted from an in-view-claiming-absent node. |
| **Invariant** | `Unreachable` ×3 (SUT-side, missing; distinct markers): PC O_SAFE gap (`pc_proto.cpp:1489`, gu_throw_fatal live in release); GCS FIFO violation (`gcs_core.cpp:639/:646` -ENOTRECOVERABLE); EVS out-of-view message (`evs_proto.cpp:2474-2479` — SILENTLY DROPPED in release). Weaker `AlwaysOrUnreachable` at the certification gap-absorb path. |
| **Antithesis Angle** | Same axiom is fatal at PC, ENOTRECOVERABLE at GCS, and silent at EVS/cert. Reframed: the EVS silent drop cannot create a LOCAL O_SAFE gap (the dropped source has no input-map slot) — it is the sole local observable of a view-id collision, whose blast lands cross-node at re-merge rather than as a precursor of the local PC fatal; the adjacent V_REG gcomm_assert is an opportunistic tripwire only (not reachable via network faults alone). MTR suppresses the related warnings. Harness note: patch gu_abort core suppression. |
| **Why It Matters** | Everything above GCS assumes gap-free total order; a silent EVS drop is an invisible precondition failure for the whole stack. |

Priority: **Medium** — passive tripwires that convert all fault load into checks; needs submodule instrumentation. Provenance: focus 4.

**Open Questions:**

- Can a collided-view divergence survive the re-merge state exchange and reach the PC per-source gap fatal, or is it always caught first by the state-exchange consistency fatals? `(partial: local starvation ruled out; PC last_seq persists across views and is exchanged at re-merge so divergence would surface there — the full re-merge trace was not completed)`

### commit-cut-bounded-by-delivered-seqno — Commit cuts never exceed delivered seqnos

| | |
|---|---|
| **Type** | Safety |
| **Property** | Every GCS_ACT_COMMIT_CUT value is within [0, highest locally delivered seqno] and monotonic per incarnation. |
| **Invariant** | `Always` (SUT-side, missing — only NDEBUG-stripped asserts guard it today: `gcs_action_source.cpp:120`, `replicator_smm.cpp:2332`) + `Sometimes` (commit cut advanced under faults). |
| **Antithesis Angle** | A too-large cut → UNTIMED `apply_monitor_.wait` blocks the recv thread (`monitor.hpp:365-373`) or purge wipes the cert index → silent divergence. The one known over-report vector (PXC's `sst_seqno_` forward adjust) is version-gated OFF in 8.4.10 (`str_proto_ver < 3`; this build negotiates 3) — dropped from the trigger list; the assertion stays as a zero-cost tripwire. Stronger with node termination (joiner paths) — flagged. |
| **Why It Matters** | The commit cut drives gcache purge and cert-index cleanup; one bad value silently deletes history other nodes still need. |

Priority: **Medium** — SUT-side only; cheap once instrumenting the provider. Provenance: focus 4.

**Open Questions:**

- Can a same-version, same-config cluster produce a violating commit cut at all? `(partial: the computation is a min over counted nodes' self-reports with a monotonic guard — violation requires a node to over-report its own applied seqno; the only known vector is version-gated off, but not every last-applied report site was exhaustively audited)`

## 5. Concurrency control & BF-abort machinery

The certify → BF-abort → replay pipeline: the densest cluster of timing-dependent
transitions in the SUT, with client-mutex-unlocked provider calls, a 12-branch status
switch, and several `unireg_abort(1)` funnels for "impossible" outcomes. All properties
here need `wsrep_applier_threads > 1`; none strictly needs special faults.

### no-mdl-bf-bf-abort — The MDL BF-BF suicide path is never reached

| | |
|---|---|
| **Type** | Safety (reachability-framed) |
| **Property** | Two high-priority threads never conflict on MDL — i.e., certification keys fully cover applier MDL footprints (the fatal branch is unconditional for non-SR BF-BF pairs; no ordering check softens it). |
| **Invariant** | `Unreachable` (SUT-side, missing) at both `unireg_abort(1)` branches in `wsrep_handle_mdl_conflict` (`wsrep_mysqld.cc:3308-3312`, `:3382-3385`; two distinct messages). Fallback: log-grep "MDL BF-BF conflict" + exit-1 death detector (the INFO line IS flushed before the abort — backstop viable). Companion `Sometimes` ×3 at the carve-out branches proving BF-adjacent MDL contention was generated: NBO-wait, DDL-BF-aborted-SR (`:3301-3306` — there is NO benign BF-BF ordering-resolution branch in this tree; the previously planned ":3305" probe is this DDL-vs-SR abort), and BF-aborted-local. |
| **Antithesis Angle** | Bug pattern A (PXC-4512/4657/4684/4789 lineage): hand-enumerated cert keys vs independently computed MDL footprints — harsher than first described: ANY non-SR BF-BF MDL conflict is deterministic node suicide (the earlier `wsrep_thd_order_before` "second channel" claim is INVALIDATED — that function is never called from the MDL path; its unsynchronized reads belong to the InnoDB lock layer). Residual read race into the second funnel: the UNLOCKED `wsrep_thd_is_BF(request_thd, false)` re-read at `:3378` can see a mode flip (replayer entering/leaving high-priority) after the locked read at `:3270`. The tolerated carve-outs are divergence-safe (they delay, never skip). Workload: TOI DDL storm (multi-table RENAME, FK cascades, TRUNCATE parents, `foreign_key_checks=0`, trigger inserts, no-op UPDATEs) concurrent with child DML. Default faults suffice. |
| **Why It Matters** | Every occurrence is a node suicide the field won't restart; the same root cause without an MDL footprint silently diverges data instead (see `bf-bf-lock-suppression-no-divergence`). |

Priority: **High** — dominant recurring bug class, open by construction. Provenance: focus 1 + focus 2 (merged).

**Open Questions:**

- Full enumeration of statements with MDL footprint > cert-key set (workload generator target list). `(partial: class is open by construction — cert keys are hand-enumerated per statement type while MDL footprints are computed independently; the historical generators are listed; a closed-form list is a workload-implementation work item, not a static-analysis deliverable)`

### trx-replay-never-fatal — Replay terminates only in success or cert-failure

| | |
|---|---|
| **Type** | Safety |
| **Property** | Transaction replay after BF abort ends only in {success, certification-failure}; the third-status funnel that kills the node is never reached. |
| **Invariant** | `Unreachable` (SUT-side, missing) before `unireg_abort(1)` at `wsrep_high_priority_service.cc:1054-1057` + `Sometimes` ×2 at the two legitimate outcome branches (replay checkpoints) + `Sometimes` on the NODE_FAIL/on_inconsistency path — the third statuses (provider_failed, connection_failed incl. provider close, fatal) are reachable BY DESIGN on local inconsistency or provider close, and the marker disambiguates designed suicide from genuine replay bugs. Boundary: the exactly-once effects contract lives in `bf-replay-commits-exactly-once`. |
| **Antithesis Angle** | BF-abort windows are verified unlocked provider calls (`transaction.cpp:1823`; `replicator_smm.cpp:1362`); race replay vs second BF abort (galera_UK_conflict shape), TOI MDL, partitions, SST cancel. Replay protection is victim-side and 4-layered (no bypass found); the residual channel is an MDL conflict against the replayer THD — owned by `no-mdl-bf-bf-abort`. Unordered XA replay-commit hits PXC's unimplemented `commit_by_xid` stub (`assert(0)`/`error_not_implemented`) — XA BF-abort recovery is partially unimplemented in 8.4; exclude XA from the day-one workload. |
| **Why It Matters** | Replay is the designated recovery for every certify-window race; a third status is node suicide from an ordinary conflict storm. |

Priority: **High** — SUT-side twin of the top-10 replay property. Provenance: focus 2 (cross-linked focus 9).

**Open Questions:**

- None

### bf-replay-commits-exactly-once — A replayed transaction's effects appear exactly once

| | |
|---|---|
| **Type** | Safety |
| **Property** | A BF-aborted committing transaction replays as the same writeset and its effects appear exactly once on every node, with a single correct client OK; the replayer is never BF-aborted a second time. |
| **Invariant** | Workload `Always` (acked unique-id effect counts: OK ⇒ exactly once everywhere, error ⇒ zero) + `Sometimes(wsrep_local_replays increased)` (vacuity guard). The fatal-funnel `Unreachable` belongs to `trx-replay-never-fatal`. |
| **Antithesis Angle** | galera_UK_conflict encodes the historical failure (replayer BF-aborted twice) as debug-sync'd slices; Antithesis explores the same interleavings natively under load. No MDL bypass of the replayer's layered victim-side BF-abort protection exists; XA is excluded day-one (`commit_by_xid` stub — see `trx-replay-never-fatal`). `wsrep_retry_autocommit=0` variant so replay outcomes surface. No special faults needed. |
| **Why It Matters** | Doubled or lost effects on the origin only = silent divergence no vote catches. |

Priority: **High** — top-10; the effects oracle doubles as shared infra. Provenance: focus 9 (cross-linked focus 2).

**Open Questions:**

- None

### commit-order-monitor-released-no-cluster-stall — Error paths never leak an ordering-monitor slot

| | |
|---|---|
| **Type** | Liveness |
| **Property** | No error path (failed TOI, apply+rollback error, empty writeset, killed applier) leaks an apply/commit-order monitor slot; after any fault episode all nodes drain and commit. |
| **Invariant** | Workload `Always` at drain points + `Sometimes` ×2 (the two realized trigger shapes reached) + `Unreachable` (monitor-window overflow as the terminal signature — owned by `monitor-window-overflow-unreachable`). Sharper detector: poll `wsrep_monitor_status (L/A/C)` — a frozen (last_entered, last_left) window on one monitor while recv processing idles localizes a leak long before any wall-clock bound trips. |
| **Antithesis Angle** | Trigger shapes: PXC-4844 (failed TOI leaves a dirty diagnostics area + empty writeset — the fix is consumer-side and generic, no per-carrier audit needed; still regression-test failed TOI/NBO + empty writesets), MDEV-38843 (apply+rollback error — fix verified present), with `applier_threads>1` and both `log_replica_updates`/`binlog_order_commits` combos (moves the release in/out of the binlog flush-queue mutex, `observer.h:356-373`). Run `wsrep_ignore_apply_errors=0`: nonzero CLOSES the MDEV-38843 seam at the price of masking divergence. PXC-4845 variant needs node termination (flagged); 4844/38843 shapes need default faults only. |
| **Why It Matters** | Realized-bug family; each leak is a cluster-wide commit freeze. |

Priority: **High** — three realized bugs in 18 months in exactly this seam. Provenance: focus 2.

**Open Questions:**

- Safe drain-window bound? `(partial: no code-side bound exists — FC pause is legitimately unbounded during state transfer; the numeric bound is a harness-tuning choice, with the `wsrep_monitor_status` gauge as the sharper detector)`

### toi-nbo-ddl-completes-or-fails-cleanly — Every TOI/NBO DDL reaches a terminal outcome with no residue

| | |
|---|---|
| **Type** | Liveness |
| **Property** | A DDL issued under TOI or NBO, concurrent with DML load, view changes, and joins, completes on all nodes or fails on the originator with a client-visible error within a bound — leaving no held TO slot, no wedged NBO, no node that must be re-initialized, and the NBO `unsafe_` counter back at 0 (the node can persist a real grastate position again). |
| **Invariant** | Workload `Always` per DDL episode (terminal outcome within T; success ⇒ schema identical at quiesce; error ⇒ schema unchanged everywhere) + residue probes: `FLUSH BINARY LOGS` produces a new file — rotation is SILENTLY SKIPPED while `wsrep_to_isolation > 0` (`binlog.cc:7881-7887`), so the probe is "no new binlog file appears", NOT an error; no session stuck in the TO stage > T. Supervisor `Always`: after a graceful shutdown, grastate UUID ≠ undefined ∧ seqno ≠ -1 whenever the DDL episode ledger shows all NBOs terminated (the observable of a leaked `unsafe_` count is on-disk UNDEFINED:-1 from `mark_unsafe`, `saved_state.cpp:257-275`). `Sometimes` ×4 (NBO completed under a fault; NBO-end resent across a view change; TOI failed cleanly with cluster size intact; a joiner was refused during an NBO). |
| **Antithesis Angle** | TOI is single-attempt (`wsrep_mysqld.cc:2557`; `poll_enter_toi` with zero deadline = already timed out); the NBO end wait is unbounded (`replicator_smm.cpp:1762-1830`); close-during-NBO is a self-declared hole (`:1807-1812`); failed TOI cleanup routes to `unireg_abort` (`wsrep_mysqld.cc:2428/:2463`); monitor-entry failure is `gu_throw_fatal` (`:1919-1922`). NBO-start nets +1 `unsafe_` (`:600-620` vs `:661-664`), balanced only at `to_isolation_end:1971` — a partition or close between the phases leaks the counter → forced SST on every future restart with zero errors logged. PXC-5244 lineage. |
| **Why It Matters** | The catalog covered the TOI *safety* twin (`no-mdl-bf-bf-abort`) but nothing owned DDL-*completion* liveness; the `unsafe_` leak is invisible to `grastate-se-checkpoint-agreement` (seqno=-1 is a legal branch there) and silently converts every restart into a full SST. |

Priority: **Medium-High** — verified unbounded waits and a silent-leak observable; NBO legs run fenced per the poison-budget convention (ambient NBO aborts joiners). Provenance: evaluation gap-fill (Gap 2).

**Open Questions:**

- Do all NBO abort paths route through the balancing `mark_safe`? `(partial: the completion path is traced; abort paths open)`
- What un-wedges peers after a failed NBO-end send?
- Bound T. `(calibration)`

### local-monitor-freed-after-bf-abort — MUST_REPLAY monitor reservations never wedge certification

| | |
|---|---|
| **Type** | Liveness |
| **Property** | The local-monitor slot reserved without cancellation for a MUST_REPLAY transaction (`replicator_smm.cpp:3676-3682`, "Return immediately without canceling local monitor") is always eventually filled by replay, or the node dies with it (no silent wedge). |
| **Invariant** | Workload `Always` at drain points (all nodes commit after every BF-abort storm) + `Sometimes("writeset dropped after SST cancel")` + external detector: `wsrep_monitor_status (L/A/C)` — a frozen local-monitor (last_entered, last_left) pair while delivery continues is the leak signature, pollable with zero SUT instrumentation. |
| **Antithesis Angle** | LocalOrder is strict (`last_left+1 == seqno`) — one hole halts the node's certification → cluster-wide FC stall. No cancellation path exists in galera, but none is needed: wsrep-lib makes rollback-without-replay structurally unreachable (s_must_replay only transitions to s_replaying; disconnect drives replay; replay failure kills the node — no silent wedge path found). SST_CANCELED writeset-dropping BYPASSES the local monitor entirely (holes accumulate; tolerated only because the node is expected to die) — probe any node still serving after an SST cancel. Kill-during-replay variant benefits from node termination (flagged, not required). |
| **Why It Matters** | Bug-pattern-C shape: a single leaked slot is a whole-cluster outage with healthy-looking status. |

Priority: **Medium** — the "possible live bug" rationale is withdrawn (rollback-without-replay structurally unreachable); retained as a cheap regression guard with the `wsrep_monitor_status` detector. Provenance: focus 2.

**Open Questions:**

- None

### bf-abort-skip-awake-victim-already-killed — The skip-awake branch never loses a KILL

| | |
|---|---|
| **Type** | Safety |
| **Property** | The BF-abort skip-awake branch (`service_wsrep.cc:192-198`) is taken only when the victim genuinely already has a kill signal pending — never as a lost wakeup. |
| **Invariant** | `AlwaysOrUnreachable` (SUT-side, missing) at the branch, QUALIFIED by signal flow: `signal=false` aborters (`THD::notify_shared_lock` → `wsrep_abort_thd(..., false)`) legitimately leave the aborter recorded with no KILL BY DESIGN — the check must record whether the recorded aborter's flow carried `signal=true` (or condition on the victim being parked in a killable wait), else it false-positives. + `Sometimes` (branch reached at all). |
| **Antithesis Angle** | `wsrep_aborter` is written under LOCK_wsrep_thd (`ha_innodb.cc:24439-24466`), read/written under LOCK_thd_data (`service_wsrep.cc:189-202`), and RESET by the owner thread under NO mutex; a third reader, `kill_one_thread`, SUPPRESSES an operator KILL while a stale aborter is recorded — a wedged session the operator cannot even kill. The MariaDB single-mutex fix (MDEV-23483) was NOT ported — confirmed regression gap. New candidate hang (reachability unproven): `notify_shared_lock` holds the victim's LOCK_thd_data across a chain that re-acquires the same mutex (`sql_class.cc:2023` → `ha_innodb.cc:24493-24497`) — probe with BF DDL against sessions holding table-level locks. |
| **Why It Matters** | A lost KILL converts one blocked transaction into a cluster outage; MariaDB fixed the analogous race with a single-mutex rework (MDEV-23483). |

Priority: **Medium** — needs SUT-side instrumentation to observe. Provenance: focus 2.

**Open Questions:**

- None

### retry-autocommit-exactly-once — Autocommit retry re-executes exactly once or not at all

| | |
|---|---|
| **Type** | Safety |
| **Property** | `wsrep_retry_autocommit` re-execution yields: client OK ⇒ effects exactly once; client error ⇒ zero effects; a certified writeset is only ever replayed, never re-executed as a new writeset. |
| **Invariant** | Workload `Always` (unique-id effect counts vs client outcome) + SUT `Sometimes` (missing) at the retry branch (`sql_parse.cc:8017-8021`). |
| **Antithesis Angle** | The seam is the 12-branch certify-status switch (including "CONN_FAIL if trx BF aborted O_o") deciding certified-abort vs plain-abort — a misclassification turns a replay into a fresh execution (double apply). The certified→s_aborted-while-peers-apply interleaving is structurally unreachable (all four post-ordering BF sites set MUST_REPLAY; CONN_FAIL is produced only pre-ordering); TOI retry is idempotent-by-reach; the retry re-runs sync-wait on a fresh view. Value concentrates on the effects-vs-outcome accounting as the regression oracle over this structural argument. |
| **Why It Matters** | Retry is on by default (1); a double-execution is a silent origin-only divergence. |

Priority: **Medium**. Provenance: focus 9.

**Open Questions:**

- None

### skip-locked-nowait-never-fatal — SKIP LOCKED / NOWAIT under BF lock traffic never kills the node

| | |
|---|---|
| **Type** | Safety |
| **Property** | `SELECT ... FOR UPDATE SKIP LOCKED` / `NOWAIT` under high-priority (applier/replayer) lock traffic always terminates legally — a subset of committed rows, or ER 1213/3572/1205 — never node death, never uncommitted rows. |
| **Invariant** | Workload `Always` (every SKIP LOCKED/NOWAIT statement returns a legal outcome; witness-value rows only — no uncommitted data) + `Sometimes` ("SKIP LOCKED returned ER_LOCK_DEADLOCK" — impossible in native InnoDB, so a precise zero-instrumentation marker that the wsrep BF-wait → `DB_SKIP_LOCKED` conversion fired, `row0sel.cc:4937/5078/5127`) + `Sometimes` (NOWAIT returned 3572/1205 under BF pressure) + log `Unreachable` ("Unknown error code" fatal, `row0mysql.cc:1224-1226`, plus the row0sel assertion signature). |
| **Antithesis Angle** | PXC-5099 regression territory (fix `0fbe08cfd7b` enumerates exactly 3 conversion sites; the underlying always-conflict rule `lock0lock.cc:603-610` is unchanged). Live residuals: an **unconverted `ut_d(ut_error)` at `row0sel.cc:4808-4812`** (release build silently SKIPS under NOWAIT — a semantics violation), and `DB_LOCK_NOWAIT` at the fixed sites still reaching `ut_d(ut_error)` — on the v1 assert image (UNIV_DEBUG on) a NOWAIT query traversing a BF-conflicted gap lock is a **candidate crash the fix never covered — a PRE-REGISTERED expectation and active bug-hunt arm**. MTR repro shape from mwb-1847 (BF-aborted FOR UPDATE leaves an HP supremum lock; SR `fragment_size=1` makes it deterministic). |
| **Why It Matters** | Job-queue workloads (the main SKIP LOCKED consumers) are exactly the hot-row pattern PXC deployments run; a realized crash bug (PXC-5099) with a visibly incomplete fix surface. |

Priority: **Medium** — implemented as a **rider** on the existing BF-conflict workload (`first-committer-wins-loser-leaves-no-trace` / `bf-replay-commits-exactly-once` already generate the required HP lock pressure); the NOWAIT-on-assert arm is a plausible early finding. Provenance: evaluation gap-fill (Gap 4).

**Open Questions:**

- Is `row0sel.cc:4808` reachable with SKIP LOCKED/NOWAIT under the wsrep always-conflict rule? If yes, v1 crashes there — pre-register.
- Spatial-index `DB_SKIP_LOCKED` × HP BF-waits (GIS leg)?

## 6. Flow control & resource bounds

Backpressure and the fixed-size structures behind it. FC is the only thing standing
between a slow node and the 65536-slot monitor rings; these properties layer: FC bounds
the queue, the queue bound protects the monitors, monitor overflow is unrecoverable.

### flow-control-pause-releases — Flow-control pauses always release

| | |
|---|---|
| **Type** | Liveness |
| **Property** | After an FC pause, cluster-wide commit progress resumes once faults heal and no member is in state transfer. |
| **Invariant** | `Sometimes` ×2 (pause observed; resume-after-pause observed) + workload watchdog: all-node commit freeze > N min while Primary/healthy = hard failure (`paused_ns` stops growing, `last_committed` advances). Watchdog constraint: a view change resets FC state and SELF-HEALS the wedge — the freeze window must undercut the view-change cadence, or the failure evidence must include "no view change during the frozen interval". Per-node gauges: `wsrep_flow_control_active` (honoring someone's STOP); `paused_ns` keeps growing during an ongoing pause, so "stops growing" is a sound release signal. |
| **Antithesis Angle** | Blocked writers spin in unbounded 1kHz EAGAIN loops (`replicator_smm.cpp:733, 820-822, 2075-2077`); a lost CONT = infinite spin with healthy status. STOP/CONT swap race (`gcs.cpp:582-591`); view-change FC refcount resets. Rollback-fragment FC bypass is real but SR-only and rate-bounded. `gcs.fc_auto_evict_window` defaults to 0 (off) — if a harness config enables it, the failure mode changes from silent freeze to node suicide. Clock-step-drops-FC-release sub-case flagged (needs clock jitter). |
| **Why It Matters** | The FC-wedge presents as a perfectly healthy cluster that commits nothing — the highest-support-volume symptom class. |

Priority: **High** — one watchdog catches the whole stall family; top-10. Provenance: focus 5.

**Open Questions:**

- None

### synced-node-recv-queue-bounded — A Synced node's recv queue stays bounded

| | |
|---|---|
| **Type** | Safety |
| **Property** | A Synced, non-desynced node's `wsrep_local_recv_queue` stays below ~100× the FC upper limit (≈173 for 3 nodes at fc_limit 100). |
| **Invariant** | `Always` (threshold check per poll — do NOT suppress during membership churn: every view change resets FC state and clears any STOP/CONT wedge, so a sustained over-threshold queue on a Synced node is a genuine finding regardless of churn) + companion `Sometimes(wsrep_flow_control_sent > 0)` guarding vacuity. Workload-only, default network faults. |
| **Antithesis Angle** | STOP/CONT swap race and view-change FC refcount resets let the queue grow toward the cgroup-blind host-memory FIFO cap. |
| **Why It Matters** | Queue growth on a "Synced" node is FC failing silently — the precursor to OOM and monitor overflow. |

Priority: **Medium** — early-warning layer of the stall stack. Provenance: focus 5.

**Open Questions:**

- Worst-case legitimate overshoot (sets the threshold)? `(partial: no static bound — overshoot = aggregate replication rate × STOP delivery latency, which throttle stretches; calibrate against a known-healthy build)`

### monitor-window-overflow-unreachable — The 65536-slot monitor windows never overflow

| | |
|---|---|
| **Type** | Reachability (negative) |
| **Property** | The monitor-window-overflow paths ("Deadlock is very likely" self-cancel spin; the STR "Slave queue grew too long ... Application must be restarted" give-up) are never entered. |
| **Invariant** | `Unreachable` (SUT-side, missing; `monitor.hpp:242-253`, `replicator_str.cpp:1030-1046`). The third candidate message ("Ran out of resources waiting to enter local monitor") is DEAD CODE inside a block comment — dropped from the detector list. Fallbacks: log-line detection; `recv_queue > 65536` proxy. |
| **Antithesis Angle** | The 1<<16 ring is the backstop behind FC's ~173 bound; reached when FC is absent (DONOR/desynced sends no FC — `gcs.cpp:441-442`) or a monitor-release bug freezes `last_left_` (PXC-4844 family). The LOCAL monitor always overflows first (the gcs local action id is assigned at DELIVERY while `last_left_` advances only at processing; apply/commit entrants are bounded by the applier count); `drain_seqno_` cannot cause spurious trips (never set on the local monitor). `interrupt()` also blocks on overflow, wedging BF-aborters. Overflow is unrecoverable; systemd won't restart the resulting exits. **Designed-behavior carve-out (synthesis)**: a desynced/donor node sends no FC *by design* — an overflow reached only via deliberate desync + tiny-transaction flood may be the designed backstop doing its job, not a finding; attribute before filing. **Explorability demoted**: the 1-11 min accumulation arithmetic likely exceeds harness write rates and branch budgets (doubly so on Debug-tier throughput) — treat the amplifier path as opportunistic, revisit after the calibration run. |
| **Why It Matters** | The terminal signature of the entire stall family — a layered oracle over the FC and monitor-release properties. |

Priority: **Medium** — fires only when the layers above fail; cheap log fallback. Provenance: focus 5.

**Open Questions:**

- Realistic time to a 65k gap in the harness (explorability)? `(partial: arithmetic bound established — a fully wedged/desynced node overflows the local-monitor window in ~1-11 min at 100-1000 writesets/s; in-harness rate needs a calibration run; desync + tiny-transaction flood is the right amplifier)`

### gcache-page-files-bounded — gcache page files stay bounded and don't leak across restarts

| | |
|---|---|
| **Type** | Safety |
| **Property** | Per node, gcache.page.* bytes stay under gcache.size + slack when no state transfer is active; zero stale-incarnation page bytes remain after restart re-sync. |
| **Invariant** | `Always` ×2 (in-run bound; post-restart zero-stale — measured as files ABOVE the new incarnation's reached index, since same-name files are ftruncated/reused) + `Sometimes` (page store actually used). |
| **Antithesis Angle** | `freeze_purge_at_seqno` has no auto-unfreeze anywhere (memory-only — restart clears it; the freeze variant needs the workload to keep the option set); IST donation holds the gcache seqno lock for the WHOLE transfer (a stalled-but-connected joiner freezes donor purge at full replication rate); PageStore does no directory scan on start (`count_=0`, `gcache_page_store.cpp:252-278`), but stale same-name files ARE ftruncated on reuse (`gu_fdesc.cpp:198-207`) — the durable leak is exactly files above the new incarnation's high-water index. ENOSPC in datadir kills IST donation + grastate writes. Orphan-across-restart half **requires kill/restart**; in-run growth needs only network faults + small gcache. |
| **Why It Matters** | Unbounded page files fill the datadir; a full datadir cascades into grastate write failures (feeding the recovery properties). |

Priority: **Medium**. Provenance: focus 5.

**Open Questions:**

- None

### notify-cmd-hang-does-not-block-commits — A hung wsrep_notify_cmd never stalls the cluster

| | |
|---|---|
| **Type** | Liveness |
| **Property** | A hung/slow `wsrep_notify_cmd` never prevents cluster-wide commit progress or view-change propagation. |
| **Invariant** | `Always` (some primary node committed within the last T, or no PC exists) + `Always` (view change propagates within T) + `Sometimes` (notify exceeded evs.suspect_timeout). |
| **Antithesis Angle** | The notify command runs synchronously and untimed (`wsrep_notify.cc:106-116`) from totally-ordered view processing (`wsrep_server_service.cc:241,403`; `wsrep_sst.cc:1694`); the shipped example script connects back into mysqld mid-view-change. Wider blast radius than first thought: `log_state_change` runs while HOLDING the server_state mutex (`server_state.cpp:1489`) — a hung script blocks every thread touching server_state; gcomm keeps servicing EVS from its own thread, so the hung node stays a member while not applying → cluster-wide FC is the structural worst case (no self-eviction path). Notify fires on every delivered view, including joiner pre-connection phases where the back-connection can hang under network faults. |
| **Why It Matters** | A standard operational hook (used by ProxySQL/orchestration setups) that can freeze the node — and via monitors, the cluster — by hanging. |

Priority: **Medium** — needs a notify-cmd-configured variant: `wsrep_notify_cmd` is READ_ONLY (no runtime SET), so the topology now defines a config variant (one my.cnf line + a shipped script) — the cheapest fault-composition surface in the catalog (synthesis). Provenance: focus 11.

**Open Questions:**

- None

## 7. Client contracts & causality

What the application is promised: error-code semantics, first-committer-wins, causal
reads, and write refusal outside the Primary Component.

### non-primary-rejects-writes — Non-Primary/unready nodes never acknowledge writes

| | |
|---|---|
| **Type** | Safety |
| **Property** | A node outside the Primary Component or not yet Synced (donor excepted) never acknowledges a data-changing statement; every acked write survives partition heal. |
| **Invariant** | `Always` (ack journal: every acked unique-keyed write present exactly once cluster-wide after convergence — race-free formulation) + `Sometimes(ER 1047 from a partitioned node)` + `Sometimes(ER 1047 while JOINER)`. |
| **Antithesis Angle** | Gates verified: only the `execute_command` gate holds the line for normal queries (COM_QUERY carries CF_SKIP_WSREP_CHECK past the dispatch gate); provider backstop CONN_FAIL below S_JOINED; the crack is "successful return does not guarantee delivery to group" + the view-callback→ready-flag ordering window — resolved as benign by construction: an ack requires a seqno assigned in PRIMARY total order, so a commit completing inside the flag lag survives heal (keep the tolerance window on the secondary must-error check; the ack-journal `Always` stays strict). Checker rule: with `wsrep_reject_queries` never set, every ER 1047 "WSREP has not yet prepared node" implies unready/non-primary (`pxc_maint_mode` has no 1047 path). Day-one implementable, zero SUT instrumentation, default partitions. |
| **Why It Matters** | An acked write that existed only on a discarded minority is the classic lost-write incident. |

Priority: **High** — top-10; foundational ack-journal infrastructure. Provenance: focus 7 + focus 8 (merged); error-code concern split to `nonready-node-error-code-contract`.

**Open Questions:**

- None

### first-committer-wins-loser-leaves-no-trace — First committer wins; loser leaves no trace

| | |
|---|---|
| **Type** | Safety |
| **Property** | For conflicting transactions: the ER-1213 loser is committed on no node; the OK winner is on every node and never disappears; exactly one of a conflicting pair wins (no lost update). |
| **Invariant** | `Always` ×3 (loser-no-trace; winner-universal; conflict-round serializes on a designated counter row) + `Sometimes` (a cert conflict actually produced a 1213 loser while its rival committed — vacuity guard). Ambiguous outcomes (connection dropped mid-COMMIT) tracked as "unknown" and asserted only to converge consistently. |
| **Antithesis Angle** | The BF-abort/replay machinery between certification and commit is the densest timing seam in the SUT; a wrong branch yields a phantom commit (client told deadlock, data committed) or the inverse (client OK, quietly aborted locally — `commit_order_leave`'s non-assert failure path). The "unknown" bucket is precisely {no reply; error + node death; XA s_prepared limbo}; the retry-vs-replay double-apply race is REFUTED (replay completes synchronously before the retry decision). Default faults suffice. |
| **Why It Matters** | The documented contract ("FIRST WRITE WINS") every Galera application is built on; no existing test checks it continuously. |

Priority: **High** — top-10 flagship client-outcome oracle. Provenance: focus 4 (evidence file present; absent from the compact return — recovered at synthesis).

**Open Questions:**

- None

### sync-wait-reads-observe-acked-writes — sync_wait reads observe all previously-acked writes

| | |
|---|---|
| **Type** | Safety |
| **Property** | With the relevant `wsrep_sync_wait` bit set, a read that starts after a write was acknowledged anywhere observes that write — including the first statement of an explicit transaction. |
| **Invariant** | `Always` ×2 (general reads; explicit-transaction first statement — distinct skip logic `wsrep_mysqld.cc:1503`) + `Sometimes` (sync-wait actually waited under faults). |
| **Antithesis Angle** | Verified: the wait is on the *apply* monitor, not the commit monitor (`replicator_smm.cpp:1715-1722`, with an in-code "hack" timed wait) and carries a wall-clock deadline (`:1683`) — clock-fault sub-angle flagged; the core needs no special faults. Resolved: apply-monitor release implies InnoDB visibility in BOTH binlog configs (appliers hold the apply monitor across the commit callbacks) — the dual-config variant is optional, and this answers the catalog-wide barrier question; the property remains as the verifier that the monitor implementation delivers the design. Non-primary mid-wait fails cleanly (ER 1205, no false success). Residual (liveness, not staleness): the untimed `gu_cond_wait` in `gcs_core_caused` can hang on provider close. |
| **Why It Matters** | Causal reads are the advertised way to get read-your-writes on any node; an apply-vs-commit visibility gap breaks it invisibly. |

Priority: **High** — cheap to check, directly probes the catalog-wide barrier question. Provenance: focus 4.

**Open Questions:**

- None

### nonready-node-error-code-contract — Unavailability errors carry the right code

| | |
|---|---|
| **Type** | Safety |
| **Property** | ER 1213 (deadlock, retriable) is returned only when the node was ready/Primary; unavailability yields 1047; sync-wait failure is not mislabeled 1205. |
| **Invariant** | `Always` (per client error observed, cross-checked against node state) + `Sometimes` (each error path exercised). An expected-to-fail `Always` here is a client-livelock finding, not a test bug. |
| **Antithesis Angle** | Verified: TOI when `!wsrep_ready` returns 1213 (`wsrep_mysqld.cc:3016-3024`) — a retriable code for a non-retriable condition → DDL clients hammer a down node; sync-wait failure returns ER_LOCK_WAIT_TIMEOUT (`:1541`) with the true cause hidden in a message admission. Deterministically reachable: the event scheduler's ON COMPLETION NOT PRESERVE auto-drop reaches `to_isolation_begin` ungated (a deterministic mislabeled-1213 driver); `wsrep_replicate_myisam` DML→TOI is a third entry point. |
| **Why It Matters** | Middleware and retry loops key on 1213/40001; wrong codes turn outages into livelocks. Distinct concern from `non-primary-rejects-writes` (whether writes get through) — kept separate, cross-linked. |

Priority: **Medium — KNOWN-RED from run one (synthesis)**: the unready-TOI 1213 path is deterministic — pre-register it as an expected finding (bug-finder) at first triage and carve that arm out so the rest of the error-code contract still guards regressions (see Shared conventions, known-red pre-registration). Provenance: focus 4.

**Open Questions:**

- Does field middleware (percona-toolkit / ProxySQL retry logic) actually key on 1213/40001 (impact sizing)? `(needs human input)`

## 8. Operational & observability truthfulness

The SUT's self-reporting: health checks, maintenance mode, shutdown, state-machine
legality, and process-death cleanliness. These properties keep every *other* oracle
honest.

### clustercheck-200-implies-write-progress — Health-check 200 implies the node can actually commit

| | |
|---|---|
| **Type** | Safety |
| **Property** | A node serving sustained HTTP 200 from clustercheck can commit writes: every sustained-200 window contains at least one committed probe write. |
| **Invariant** | `Always` (per sustained-200 window ⇒ ≥1 committed probe write in the window) + `Sometimes` (node observed in state 2 while serving health checks). |
| **Antithesis Angle** | Three verified green-but-dead mechanisms: FC pause; donor/desync state 2 with `wsrep_ready=ON` (`wsrep_server_service.cc:349-386` fall-through); InnoDB writes frozen on an untimed event (`os0file.cc:234-237`). clustercheck never checks commit ability (`clustercheck.sh:83-93`). Confirmed: `wsrep_local_state` STAYS 4 during an FC pause — the health check is truly blind to it, the FC leg is at full strength. The AVAILABLE_WHEN_READONLY leg is DROPPED from the premise: no in-tree writer flips `read_only` on a serving node, and probes against a read-only node fail fast rather than hang. |
| **Why It Matters** | Load balancers route on this check; a green-but-frozen node blackholes traffic. Also keeps the harness's own up/down model honest. |

Priority: **High** — top-10; triple-verified mechanism, trivially implementable probe. Provenance: focus 11.

**Open Questions:**

- T tuning vs legitimate long pauses? `(partial: lower bound = FC hysteresis + evs.suspect_timeout; final value needs one measurement in the first triage round)`

### fatal-node-terminates-no-zombie — A fatally-failed node actually dies

| | |
|---|---|
| **Type** | Safety |
| **Property** | Once a node emits a fatal marker, the process exits within a bound — never a zombie that holds group membership without progress. |
| **Invariant** | `AlwaysOrUnreachable` (fatal marker ⇒ exit within T; external watchdog sidecar) + inverse zombie detector (alive + no progress + not-in-SST for T_long) + `Sometimes` (fatal path during SST — the crash-dense window). |
| **Antithesis Angle** | VERIFIED self-deadlock: `handle_fatal_signal` → `wsrep_handle_fatal_signal` (`signal_handler.cc:402-416`) → `wsrep_sst_cancel` → BLOCKING `mutex_lock(LOCK_wsrep_sst)` (`wsrep_sst.cc:553-554, 591`); SST windows are crash-dense and hold that mutex in untimed waits. Marker inventory resolved: abort-family fatals log BEFORE the vulnerable path — only raw SIGSEGV/SIGBUS can wedge markerless, and the inverse zombie detector covers exactly that subclass. gcomm keeps answering keepalives from its own thread while the handler is wedged (the zombie stays a member → cluster-wide FC). NEW wedge site: `unireg_abort` itself calls `wsrep_sst_cancel(true)` (`mysqld.cc:2870`) — post-marker, so the log watchdog catches it. gu_abort suppresses cores. |
| **Why It Matters** | A wedged half-dead node corrupts every other property's up/down oracle and can hold EVS membership, stalling the cluster. |

Priority: **High** — oracle-integrity property; the deadlock is verified reachable. Provenance: focus 11.

**Open Questions:**

- T_long tuning for the inverse zombie detector? `(partial: detector scope narrowed to markerless SIGSEGV/SIGBUS crashes; threshold needs one harness measurement, gated on SST-not-in-progress observables)`

### maint-mode-honors-operator-intent — Maintenance mode is never silently reverted

| | |
|---|---|
| **Type** | Safety |
| **Property** | An operator-set `pxc_maint_mode=MAINTENANCE` is never flipped back to DISABLED by a view change until the operator clears it (SHUTDOWN excepted). |
| **Invariant** | Workload `Always` vs an operator-intent ledger + `Sometimes` ×2 (missing markers) on the forced-flip and forced-revert branches — `Sometimes`, NOT `Unreachable`, in the release image: every non-primary view fires the forcing branch in a homogeneous cluster (see Angle), and the next primary view force-reverts. |
| **Antithesis Angle** | VERIFIED: `log_view` — the forced flag's SOLE writer (tree-wide sweep) — force-flips the mode (`wsrep_server_service.cc:196-231`), and an operator SET never clears the forced flag (update fn is a no-op, `wsrep_var.cc:1096`). Release-reachable with plain network faults: every NON-PRIMARY view carries `appl_proto_ver = -1` (`GCS_QUORUM_NON_PRIMARY`, `gcs_state_msg.hpp:78-85`), `log_view` runs for ALL view statuses, and -1 < V4 forces MAINTENANCE + the forced flag under default ENFORCING; the next primary view force-reverts to DISABLED. An operator drain set before/during the partition is thus silently ERASED at heal — the node re-enters rotation mid-maintenance. The DBUG multi-major knob remains a debug-image amplifier, not a prerequisite. (The earlier 10s-SET-sleep-holds-MDL claim is retracted — the sleep holds no MDL and only delays the operator's session.) |
| **Why It Matters** | clustercheck requires DISABLED for 200 → a spurious flip returns a draining node to rotation mid-maintenance; forced-MAINTENANCE cluster-wide = all-503 blackhole. The forced-flip/revert hijack of operator intent is a real release-build finding reachable with default network faults. |

**Implemented as:** `an operator-set pxc_maint_mode=MAINTENANCE is never reverted to DISABLED`
(`always_or_unreachable`, `workload/pxcwl/levers.py::maint_mode_cycle`). Renamed 2026-09-23
from "pxc_maint_mode matches the last operator-set value until the operator changes it",
which promised a two-way equality this property never meant. Carve-outs: `observed ==
SHUTDOWN`, and the intent-DISABLED/forced-MAINTENANCE direction. A view change is NOT
carved out — the forced-revert hijack fires precisely on one.

Priority: **High — KNOWN-RED from run one (synthesis)**: the forced flip fires on every non-primary view under partitions, so the `Always` is expected to fail immediately — pre-register the forced-flip/revert arm as an expected finding (bug-finder) with a carve-out so the remainder of the intent ledger still guards regressions (see Shared conventions). Provenance: focus 8, reconciled with focus 10's non-primary protocol -1 chain (which supersedes the earlier "Unreachable in release" conclusion).

**Open Questions:**

- Does ProxySQL v2's native Galera support (which no longer reads pxc_maint_mode) change what the customer considers "operator intent"? Affects real-world weight, not correctness. `(needs human input)`

### graceful-shutdown-bounded — Graceful shutdown completes within a bound

| | |
|---|---|
| **Type** | Liveness |
| **Property** | A SIGTERM'd node exits within a bound (≥ the 10s maintenance sleep + applier drain) and rejoins via IST on restart when gcache suffices. |
| **Invariant** | Workload `Always` (exit within T) + `Sometimes` (graceful restart rejoined via IST — promotable to `Always` under a documented write-budget precondition: the donor's IST gate requires the joiner inside the cached range minus a ~0.8% safety gap, plus clean grastate and matching uuid; `wsrep_local_cached_downto` is the observable) + `Sometimes` (applier-drain loop >30s — missing SUT counter). |
| **Antithesis Angle** | `wsrep_wait_appliers_close` is `while(true) sleep(1)` with no deadline (`mysqld.cc:12619-12637`); a monitor-wedged applier blocks shutdown forever. SIGTERM sleeps `pxc_maint_transition_period=10s` in the signal thread — equal to Docker's default grace → default containers SIGKILL every "graceful" stop → unclean grastate → SST on every stop. SQL `SHUTDOWN` takes the exact same 10s sleep path — use a `pxc_maint_transition_period=0` variant to skip it. No injected faults required. |
| **Why It Matters** | Turns every routine restart into crash recovery in default container setups; the unbounded drain converts any monitor wedge into an unstoppable process. |

Priority: **High — PROMOTED to the v1 top-10 (synthesis)**: every v1 restart-driven exploration (graceful SHUTDOWN cycles via the supervisor) depends on graceful shutdown completing — it belongs in the first tranche both as a property and as the harness-configuration guard (stop grace). Provenance: focus 8.

**Open Questions:**

- What is the correct exit-time bound T under quiesced faults? `(partial: lower bound = pxc_maint_transition_period (10s) + applier drain; needs one harness measurement — too tight = false positives, too loose hides wedges)`

### ftwrl-backup-quiescent-or-fails — A successful FTWRL yields a genuinely frozen snapshot point

| | |
|---|---|
| **Type** | Safety |
| **Property** | After `FLUSH TABLES WITH READ LOCK` returns success on node X, X is a quiescent snapshot point until `UNLOCK TABLES`; if quiescence cannot be established, FTWRL fails with a visible error — never a non-quiescent success. |
| **Invariant** | `Always` (while a successfully-returned FTWRL is held on X: `wsrep_last_committed` on X is frozen, and no write acknowledged via X commits; other nodes keep committing — holds kept short and FC-aware) + `Sometimes` ×3 (ER_QUERY_INTERRUPTED under the TOI-vs-backup-lock contention path; a backup-locked FTWRL retry succeeded after ≥1 failure; a commit was acked elsewhere while X was frozen — non-vacuity of the freeze). |
| **Antithesis Angle** | FTWRL does NOT block COMMIT via MDL in PXC — commit skips the COMMIT intention lock for wsrep sessions (`handler.cc:1841-1848`); quiescence is re-provided *solely* by the provider pause, whose contract is pause-or-fail (`lock.cc:1175-1290`: backup-locked → `try_desync_and_pause(30s)`; plain → untimed `desync_and_pause`; failure → ER_QUERY_INTERRUPTED). The pause stamps a real seqno into grastate MID-RUN (`replicator_smm.cpp:3409-3410`) — a backup workload widens the grastate-vs-SE kill window that `grastate-se-checkpoint-agreement` targets. |
| **Why It Matters** | Every live-node backup (xtrabackup et al.) stands on this contract; a violated pause is a torn backup — the worst kind of silent failure, discovered only at restore time. |

Priority: **Medium-High** — plain-SQL, fault-free core (the cheapest gap the evaluation found); its sibling `desync-ftwrl-composition-resyncs` owns the liveness/composition half. Provenance: evaluation gap-fill (Gap 3).

**Open Questions:**

- Is the paused-at seqno ≥ every commit acked via X before FTWRL returned?
- Do any internal threads bypass both the MDL skip and the pause? `(partial: non-wsrep client THDs still take the COMMIT MDL; internal threads unverified)`

### desync-ftwrl-composition-resyncs — Desync/FTWRL/backup compositions always return and resync

| | |
|---|---|
| **Type** | Liveness |
| **Property** | Any interleaving of `SET GLOBAL wsrep_desync`, `LOCK INSTANCE FOR BACKUP`, and FTWRL/UNLOCK with replication load, view changes, and donations: every statement returns within a bound; after all operator actions are released the node ends Synced with `wsrep_desync_count=0` and `wsrep_desync=OFF` (ledger-aware); operator desync intent is never silently reverted. |
| **Invariant** | `Always` statement-liveness (each FTWRL/UNLOCK/desync toggle returns — success or error — within T) + `Always` quiesced end-state vs the operator-intent ledger (all released ⇒ state 4 ∧ desync_count 0 ∧ desync OFF; intent leg: desync ON at quiesce ⇒ state 2) + `Sometimes` ×4 (FTWRL taken while desynced; a toggle refused during a pause; FTWRL overlapped a donation; the "GCS desync returned seqno" warning observed). |
| **Antithesis Angle** | `try_desync_and_pause` warns-only on a non-consecutive pause seqno and then re-enters the local monitor at it (`replicator_smm.cpp:3466-3476` — an unfilled gap wedges FTWRL beyond the 30s retry's reach); plain `pause()` drains untimed (`:3385-3416`); `resume_and_resync` swallows failure (`server_state.cpp:721-742`) → UNLOCK returns success on a permanently desynced node; `SET wsrep_desync` bypasses wsrep-lib accounting entirely (`wsrep_var.cc:692-745`) — three disagreeing bookkeepers. |
| **Why It Matters** | Same operator surface as `ftwrl-backup-quiescent-or-fails` (which owns snapshot safety); coordination: `donor-returns-to-synced` owns donation-driven desync leaks, `flow-control-pause-releases` owns the downstream commit freeze — this property owns the operator-composition wedge/leak shapes joining that stall family. |

Priority: **Medium-High** — in-code confessions on both the hang and the swallow; the backup/desync workload actor is fenced (desync disables FC on the node). Provenance: evaluation gap-fill (Gap 3).

**Open Questions:**

- Is the non-consecutive `enter(lo2)` gap structurally unfillable in some race (permanent wedge), or always eventually filled?
- Legitimate T for plain FTWRL under load? `(partial: untimed drain confirmed in code; the envelope is calibration)`

### illegal-wsrep-transition-never-taken — The wsrep state machines never take unallowed transitions

| | |
|---|---|
| **Type** | Reachability (negative) |
| **Property** | The wsrep-lib transaction (13×13) and server (9×9) state matrices — the spec — are never violated; release builds currently log "unallowed state transition" and proceed anyway. |
| **Invariant** | `Unreachable` ×3 (missing; wsrep-lib submodule patch at `transaction.cpp:1410-1416`, `server_state.cpp:1476-1481` after the s_disconnecting ignore-escape, `replicator_smm.cpp:3893-3910`). Fallback: `Always` (log never contains "unallowed state transition") — weaker (the transaction-side message is debug-level). |
| **Antithesis Angle** | Assertion-erosion oracle: turns ALL fault load into checks passively and anchors replay at the earliest corruption point. |
| **Why It Matters** | Warn-and-proceed transitions are how e.g. `wsrep_ready` can flip ON early (feeds `non-primary-rejects-writes`); the matrices are the closest thing to a formal spec in the tree. |

Priority: **Medium** — high leverage; the wsrep-lib patch is confirmed feasible (sources vendored, compiled via `ADD_SUBDIRECTORY`), so the log-scan is demoted to fallback. Provenance: focus 11.

**Open Questions:**

- Does the violation branch ever fire in practice, or is it genuinely unreachable? Either answer is valuable. `(partial: statically unresolvable by design — the run answers; both violation branches re-verified at f9ecb3e)`

## 9. Configuration, topology & version skew

Runtime reconfiguration under load, unvalidated cross-node config invariants, the
async-replication side-channel, and version-gate machinery that must stay dormant in a
homogeneous cluster.

### applier-resize-converges — Applier-thread resize converges without stalling replication

| | |
|---|---|
| **Type** | Liveness (with a safety edge) |
| **Property** | `SET GLOBAL wsrep_applier_threads=N` under load converges to exactly N appliers with replication progressing throughout; repeated/concurrent resizes never lose count or drop to zero appliers. |
| **Invariant** | Workload `Always` (settled `wsrep_thread_count == wsrep_applier_threads + 1` — N appliers + 1 rollbacker — within T of the last SET, with `wsrep_last_committed` advancing; quiesce NBO DDL around the check: NBO workers transiently inflate the counter) + `Sometimes` (shrink and grow under load). |
| **Antithesis Angle** | Verified locking asymmetry: the setpoint (`wsrep_slave_count_change`) is computed and consumed under two different mutexes (`wsrep_var.cc:673-690` vs `wsrep_high_priority_service.cc:956-965`) — concurrent SETs themselves are serialized by the global-sysvar write lock, so the realistic race is SET-vs-applier-exit (appliers exit only between writesets, after releasing both monitors); the last-applier exit guard (`replicator_smm.cpp:517-524`) is the SUT's own floor protection. No faults required at all. |
| **Why It Matters** | The catalog's own harness config (`applier_threads>1`) makes this path load-bearing; a zero-applier wedge is a cluster-wide FC stall. |

Priority: **Rider (synthesis)** — a deterministic resize-loop test covers most of the value; keep only the settled-count convergence check as a rider on workloads that resize appliers anyway. Zero fault requirements; guards the ensemble's own config choice. Provenance: evidence file present; absent from all compact returns (recovered at synthesis, focus 8 area).

**Open Questions:**

- None

### cluster-address-live-set-rejoins — Runtime wsrep_cluster_address SET returns and the node rejoins

| | |
|---|---|
| **Type** | Liveness (with a safety edge) |
| **Property** | `SET GLOBAL wsrep_cluster_address` under write load returns within T_set; the node rejoins Synced within T_rejoin with cluster size N; survivors stay Primary throughout; concurrent SETs never crash or wedge the node. |
| **Invariant** | `Always` (SET returns ≤ T_set; node Synced ≤ T_rejoin; survivors Primary — bounds pinned at calibration) + `Sometimes` (a SET executed with writes in flight) + `Sometimes` (two SETs raced, both returned, node Synced afterwards). |
| **Antithesis Angle** | The handler (`wsrep_var.cc:550-595`) drops `LOCK_global_system_variables` mid-update (the in-code comment admits the race); it performs a full disconnect (rolls back the setter's own trx, closes ALL client connections, drains appliers — `wsrep_mysqld.cc:1367-1394`) then reconnects; a both-locks-free window at `:584-592`; verify is a stub (any string passes, `:523-526`); the SET returns success even when the reconnect fails — a silently detached node; the global `char*` is freed/re-strdup'd while unlocked readers deref it (`:597-605`; `wsrep_thd.cc:125-130/:309`). Workload rule: only valid full gcomm strings, one already-joined node at a time (the empty-address park and `--wsrep-new-cluster`-equivalent consume are fenced levers, not ambient actions). |
| **Why It Matters** | A routine operator action (re-pointing a node); a hang holds `LOCK_wsrep_cluster_config` forever; a false-success SET is a silently detached node. As a rider it buys v1 SQL-only leave/rejoin churn without the kill channel — de-risking evaluation Bias 1. |

Priority: **Medium** — **rider on `restarted-node-rejoins-synced`** (identical rejoin detector/Synced bound/convergence checks; the SET is a third trigger for the leave→rejoin cycle needing no kill channel and no process restart), with its own slug and assertion messages because its failure modes (SET hangs, double-SET race, false success) are not expressible as arms of the host property. Provenance: evaluation gap-fill (Gap 7).

**Open Questions:**

- Can `wsrep_stop_replication` deadlock against the setter's own session holding the cluster_config lock? `(the T_set bound is the instrument)`
- SET-to-empty (deliberate detach) is deferred to its own fenced phase.

### cert-interval-reject-symmetry — Certification interval verdicts are symmetric across nodes

| | |
|---|---|
| **Type** | Safety |
| **Property** | With uniform provider cert params, the interval-rejection verdict (`cert.max_length`/`length_check`) is the same on every node — no writeset dummied on some nodes and applied on others. |
| **Invariant** | `Always` (cross-node checksums at sync barriers; variant with uniform `cert.max_length=512` to heat the branch) + `Sometimes` (rejection observed) + missing SUT detail event at `certification.cpp:437-444`. |
| **Antithesis Angle** | Confirmed: `gcs_state_msg` carries NO cert params; runtime SET is rejected; params are hidden, startup-only, "EXTREMELY important same on all nodes" per in-code comment — but unenforced; TEST_FAILED→mark_dummy is silent (no vote). Join asymmetry ruled out under uniform params (dense-trx-map cut arithmetic + FIFO send-monitor ordering + shared trim horizon) — no membership-quiescent qualifier needed; a checksum failure at a join boundary is a genuine finding. Also the negative-control calibration for the checksum oracle. |
| **Why It Matters** | A per-node verdict split is deterministic divergence with zero error signal. |

Priority: **Reframed (synthesis): one-shot oracle calibration, not a standing property/variant.** Run the uniform-`cert.max_length=512` heat-the-branch exercise once as the negative-control calibration of the checksum oracle; do not maintain a standing variant for it. Provenance: focus 10.

**Open Questions:**

- Should Percona gossip/validate cert params across the group? The uniformity requirement is stated nowhere users can see. `(needs human input — params are Flag::hidden, absent from the docs, and not exchanged in gcs_state_msg)`

### duplicate-gtid-skip-exactly-once — Duplicate-GTID transactions skip with zero effects

| | |
|---|---|
| **Type** | Safety |
| **Property** | A duplicate explicit-GTID transaction reaching PXC (async re-delivery or client `gtid_next`) is skipped with zero effects, and the async monitor absorbs the skip without crash or stall. |
| **Invariant** | Workload `Always` (channel marker rows exactly once; `gtid_executed` converges) + `Sometimes` (channel advanced past a skip) + missing SUT pair: `Always` + `Unreachable` at the seqno-mismatch `unireg_abort` (`wsrep_mysqld.cc:2948-2956`). |
| **Antithesis Angle** | Regression family PXC-4664/4688 (nullptr SIGSEGV) and PXC-4823 (permanent stall). The fix's admitted residual deadlock is **PXC-4665, a known won't-fix** — an applier-vs-local deadlock under contested explicit `gtid_next` (plain SQL on two nodes, no async channel; interleaving in `galera_gtid.test:36-57`) — the workload must avoid holding open transactions under a contested `gtid_next` or classify a hit as a known issue. Binlog rotation DOES restart sequence numbers against retained monitor state — the stale-skip hazard is a deterministic ordering bypass, not a crash. `wsrep_use_async_monitor=OFF` is a supported config; no OFF variant needed here. **Requires an async replication channel into the cluster (`replica_parallel_workers>1`, `preserve_commit_order=ON`) — topology decision.** |
| **Why It Matters** | Async-source-into-PXC is a common migration topology; three realized bugs in the skip path. |

Priority: **Medium** — gated on the async-topology decision. Provenance: focus 9.

**Open Questions:**

- None

### async-monitor-leave-mismatch-unreachable — The async-monitor leave mismatch never fires

| | |
|---|---|
| **Type** | Safety |
| **Property** | `Wsrep_async_monitor::leave()` never observes a seqno mismatch (whose handler is `unireg_abort(1)`). |
| **Invariant** | `Unreachable` (SUT-side, missing; `wsrep_async_monitor.cc:80-91`) + `AlwaysOrUnreachable` pairing (every enter has a matching leave) + `Sometimes` (monitor exercised under rotation). |
| **Antithesis Angle** | Verified asymmetry: `enter` early-returns for killed workers, `leave` is unconditional (`wsrep_mysqld.cc:2954-2956`). FLUSH BINARY LOGS exercises the stale-skipped-seqnos bypass (sequence restarts per binlog file; monitor sized by `opt_replica_parallel_workers`, not applier count) — rotation itself is crash-safe (FIFO front-equality, no monotonicity assumption); the stale-skip ordering bypass is observable via the workload's monotonic counter. **Requires the async source→PXC topology.** Regression family PXC-4173/4664/4688/4823. |
| **Why It Matters** | Four realized bugs; the mismatch handler is node suicide. |

Priority: **Medium** — same topology gate as the GTID property; implement together. Provenance: focus 2.

**Open Questions:**

- Does an MTS transaction retry re-`enter()` a seqno a prior `leave()` already popped? If so, the retrying worker waits forever on a seqno that will never reach the queue front — a stall shape distinct from PXC-4823. `(partial: retry path traced — the coordinator-side schedule() is not re-run, and a temp-error retry after wsrep_before_prepare pops the seqno; whether the wsrep hooks re-run enter() on the retry execution path is unconfirmed)`

### no-spurious-multi-major-detection — The multi-major gate never trips in a homogeneous cluster

| | |
|---|---|
| **Type** | Safety |
| **Property** | An all-8.4.10 cluster (protocol V7) never trips multi-major detection **on a Primary view**: `log_view` never forces MAINTENANCE while the delivered view is Primary, and the write block never fires on a Synced/Primary node. Non-primary views reach the forcing branch with protocol -1 BY CONSTRUCTION — expected behavior of this code as shipped, covered by a companion marker. |
| **Invariant** | `Unreachable` ×2 (missing), both CONDITIONED on `view.status() == primary` and excluding the DBUG path: the forced-MAINTENANCE branch `wsrep_server_service.cc:209-215` and the non-DBUG write-block branch `sql_parse.cc:1888-1912`. Companion `Sometimes` ("multi-major forcing fired on a non-primary view") — guaranteed reachable; anchors the operator-drain-hijack exploration for `maint-mode-honors-operator-intent`. Workload: `pxc_maint_mode` never differs from DISABLED (or the workload's own setting) *while the node reports Synced/Primary*. |
| **Antithesis Angle** | RE-SCOPED: every non-primary view carries `appl_proto_ver = -1` (`GCS_QUORUM_NON_PRIMARY` → `galera_view_info_create` → `log_view`), so the original unconditioned `Unreachable` was wrong — the branch fires on every partition in a homogeneous release cluster. Primary-view downgrades below V4 are DOUBLY guarded (members advertise a compile-time maximum; quorum has an anti-downgrade clamp), so a primary-view firing = real negotiation bug with total-write-outage blast radius. Release-build complement to `rolling-upgrade-write-gate` (do not run both in one variant). |
| **Why It Matters** | The gate machinery is new and its false-positive cost on a primary view is total write unavailability; the non-primary firing feeds the release-reachable operator-drain hijack owned by `maint-mode-honors-operator-intent`. |

Priority: **Medium** — passive tripwire, release image. Provenance: focus 10.

**Open Questions:**

- None

### rolling-upgrade-write-gate — The multi-major write gate blocks writes correctly when active

| | |
|---|---|
| **Type** | Safety |
| **Property** | While the multi-major gate is active on a node (ENFORCING/MASTER), every data-changing statement to it fails with no cluster-wide data change, reads succeed, and on clear the forced MAINTENANCE reverts and writes resume. |
| **Invariant** | `AlwaysOrUnreachable` (gate active ⇒ CF_CHANGES_DATA fails ER_UNKNOWN_ERROR, no data change) + `Sometimes` (gate cleared and writes resumed — revert liveness). |
| **Antithesis Angle** | SPLIT scope: the maint-forcing/revert state machine — including the operator-drain hijack via `wsrep_pxc_maint_mode_forced` — has a RELEASE-build, network-faults-only trigger (every non-primary view carries protocol -1; owned by `maint-mode-honors-operator-intent`). The *write-blocking* half on a node that is otherwise Synced/Primary and accepting statements **REQUIRES DEBUG BUILD** (`simulate_wsrep_multiple_major_versions` DBUG): during non-Primary the wsrep-readiness gate rejects writes before the upgrade gate is consulted. All SQL funnels through `mysql_execute_command`; escapes, all by design: `wsrep_on=OFF` sessions, non-CF_CHANGES_DATA statements, appliers. |
| **Why It Matters** | Pre-verifies the 8.4→next rolling-upgrade write fence before it's ever exercised in the field. |

Priority: **Low** — debug-build variant only; forward-looking. Provenance: focus 10.

**Open Questions:**

- None

### homogeneous-cert-version-match — Cert version-match rejection never fires in a single-version cluster

| | |
|---|---|
| **Type** | Safety |
| **Property** | In a single-version cluster, the `trx_cert_version_match` rejection (and the adjust_position cert-index wipe) never fires — both branches silently dummy/discard writesets and are divergence precursors. |
| **Invariant** | `Unreachable` (missing) at `certification.cpp:421-427` (details trx/cert versions + seqno) + optional `Unreachable` at the `:1127-1140` index wipe. Forward-compatible: flips to `Sometimes` in a future mixed-version phase. |
| **Antithesis Angle** | Branch inputs are recomputed on churn paths (SST/IST joins with cert preload — preload skips the interval check but NOT the version check; every primary view; gcache recovery; provider re-init). The `version_ = -1` sentinel window cannot coexist with certification (both inside the CC LocalOrder critical section; live processing suspended on the transfer path) — no startup exclusion needed; any firing is a real bug. gcache-recovery coverage benefits from node termination (flagged). |
| **Why It Matters** | Tripwire on version-gated branches that are dead-by-construction today; pre-instruments the future upgrade phase. |

Priority: **Low** — tripwire; near-zero cost once provider instrumentation exists. Provenance: focus 10.

**Open Questions:**

- None
