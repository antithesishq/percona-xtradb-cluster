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

# Evaluation Synthesis — PXC 8.4.10 Property Catalog

Four evaluation lenses ran over the 59-property catalog: **antithesis-fit**,
**coverage-balance**, **implementability**, **wildcard**. Every finding is categorized
below as **Refinement** (specific fix — applied), **Gap** (missing failure
class/property — recorded as a targeted discovery assignment for the gap-fill round), or
**Bias** (systematic orientation problem — collected with evidence for human judgment,
not resolved here). Lens evidence files:
`antithesis/scratchbook/evaluation/{antithesis-fit,coverage-balance,implementability,wildcard}.md`.

**Totals: 18 refinements applied · 11 gaps recorded (all 11 closed at gap-fill
integration — 7 new properties + 5 workload/infra edits; see Gaps status below) · 4
biases collected.**

---

## Refinements (applied)

### R1. Build-order contradiction resolved; per-property phasing added; top-10 re-ranked
*Sources: wildcard F1/F2, antithesis-fit CW-1/CW-4, coverage-balance #6, implementability #2.*
The catalog assumed a release-primary image; the topology builds assert-enabled first.
All four lenses flagged the contradiction. **Resolution: the topology's plan wins** —
v1 = assert-enabled image (RelWithDebInfo minus NDEBUG); plain **Debug** build is the
DBUG tier (implementability verified that stripping NDEBUG does NOT enable DBUG —
`DBUG_OFF` stays set, so DBUG/DEBUG_SYNC-gated properties need plain Debug); release
(NDEBUG) image is a later phase.
**Applied:** `property-catalog.md` gains a **"Phasing and variants"** section with a
per-property image/phase-validity tag (`v1-assert` / `debug-DBUG` / `release` /
`v2-instrumented`, plus `kill-channel`, `variant`, `calibration`, `deferred` modifiers),
image-conditional-invariant notes (release-only semantics vacuous or inverted on the
assert image — e.g. illegal-transition log fallback, injected divergence assert-crashing
before a vote round), and a re-ranked v1-viable top-10: `failed-state-transfer-node-rejoins`
and `graceful-shutdown-bounded` promoted (per antithesis-fit P-11/P-12);
`acked-commit-durable-across-restart` and `grastate-se-checkpoint-agreement` moved to a
phase-2 tranche (NOT deleted). The catalog Assumptions "dual-image" bullet is rewritten
to the three-tier plan. `deployment-topology.md` build-variant section clarified:
assert-enabled ≠ DBUG; three tiers enumerated.
**Conflict resolved:** catalog-vs-topology build order — adopted topology (assert-first);
per antithesis-fit CW-4 the assert image is the denser oracle per CPU-hour and the
release image's unique properties are tagged `release` rather than blocking v1.

### R2. Workload→supervisor kill channel + supervisor extensions + live log source
*Sources: antithesis-fit CW-2/CW-3/P-15, implementability #1/#4/#9, wildcard C2/M4, coverage-balance #7.*
~1/3 of the catalog is termination-gated; the tenant's node-termination faults are
assumed disabled; the catalog's "workload drives in-container kill" fallback was not
realizable (the workload reaches nodes via SQL only; `SHUTDOWN` is graceful; no DBUG in
the v1 build).
**Applied to `deployment-topology.md`:** (a) a **workload→supervisor crash channel** —
the entrypoint supervisor polls a file/SQL-visible marker table and executes `kill -9`
on mysqld, giving steerable kill timing (better than platform faults for kill-mid-IST);
node-side kill test-command scripts on the pxc-node containers; (b) supervisor
extensions: boot-time grastate/`--wsrep-recover` probe emitting JSONL (near-free — the
supervisor already runs the recovery dance), log-tail watchdog for fatal markers, gcache
file metrics, boot-phase markers, and a **hold-down knob** so all-nodes-down scenarios
aren't racy under restart-on-exit; (c) `performance_schema.error_log` documented as the
live log source over SQL for running nodes.
**Conflict resolved:** antithesis-fit CW-2 ("workload-driven kill not realizable") vs
implementability #1 (supervisor-polled marker channel) — both are right: SQL-direct kill
is impossible, supervisor-mediated kill is the design. Adopted the supervisor channel.

### R3. pxc_strict_mode collision decided
*Source: implementability #5 (code-verified: ENFORCING blocks PK-less DML, non-InnoDB DML, LOCK TABLES — sql_base.cc:6306-6350).*
**Decision: baseline keeps the field default ENFORCING; a dedicated workload
phase/variant lowers it dynamically (it is a dynamic sysvar) for the PK-less /
FK-cascade legs** (`bf-bf-lock-suppression-no-divergence`'s required workload,
`cross-node-row-equality`'s PK-less leg, `bf-abort-skip-awake` probe, MyISAM driver).
**Applied:** documented in `deployment-topology.md` (config sketch note) and in
`property-catalog.md` Shared conventions with the affected-property list.

### R4. notify-cmd variant
*Sources: antithesis-fit P-13, implementability (wsrep_notify_cmd verified READ_ONLY).*
`notify-cmd-hang-does-not-block-commits` was dead by config omission, and the variable
cannot be set at runtime. **Applied:** `deployment-topology.md` adds a config variant —
one my.cnf line pointing at the shipped `wsrep_notify.sh`-style script — as the cheapest
fault-composition surface. Catalog phase tag: `v1-assert + variant(notify-cmd)`.
**Conflict resolved:** P-13 "cheapest, add to v1" vs implementability "blocked harder
than stated (READ_ONLY)" — both true; resolution is a static config variant, not a
runtime SET.

### R5. Quiesced checkpoints under faults reworded
*Source: implementability #6.*
The workload cannot pause faults mid-run on its own, so per-event `Always` checksum
invariants degrade under faults. **Applied:** affected invariants reworded to
"**opportunistic gated attempts under faults + guaranteed full-strength check in
`eventually_`/`finally_` (faults paused)**"; `ANTITHESIS_STOP_FAULTS` noted as the
mid-run quiet-window mechanism (resolves implementability's own uncertainty). Reworded
in `cross-node-row-equality` directly; the general rule and affected-property list live
in the catalog's new Shared conventions section.

### R6. Poison-budget scoping convention
*Sources: wildcard M1/M2, antithesis-fit CW-5.*
Sabotage/trigger-poisoning properties (`inconsistency-vote-evicts-divergent-minority`
divergence injection, `cluster-member-strings` sentinel node, privilege sweep) violate
other properties' `Always` invariants by construction, and ambient NBO DDL
deterministically aborts joiners (fabricating SST/IST liveness failures).
**Applied:** catalog-wide convention section — sabotage runs in **fenced
variants/phases** with excluded tables and suppression windows; ambient NBO DDL is
fenced into its own workload phase; and the **un-injected-vote rule**: any inconsistency
vote NOT attributable to injected sabotage is itself divergence evidence — added to
`cross-node-row-equality` (previously no property failed on it — wildcard M1). Also from
M1: write-once witness tables and checksum-evicted-nodes-before-rejoin noted as oracle
design guidance.

### R7. vote-message-payload: NULL-deref Unreachable dropped
*Source: antithesis-fit P-3.*
The `strlen(NULL)` SIGSEGV precondition at `gcs_group.cpp:1197` is undrivable by any
planned fault, ever (same-version senders always append ≥1 NUL byte; only transport
truncation or a foreign sender reaches it). **Applied:** the `Unreachable` dropped from
the invariant in `property-catalog.md` and the evidence file annotated; the
empty-vote-recompute and survivor-consistency legs kept. (The NULL-deref remains listed
under Bias 4 as a potential upstream report.)

### R8. monitor-window-overflow: designed-behavior carve-out + explorability demotion
*Sources: antithesis-fit P-5, implementability #8.*
Overflow under the desync+tiny-transaction-flood amplifier may be DESIGNED behavior
(a desynced node sends no FC by design), and the 1-11 min wedge accumulation likely
exceeds harness throughput/branch budgets — doubly so on Debug-tier builds.
**Applied:** carve-out note (an overflow reached only via deliberate desync+flood is not
automatically a finding) and demoted explorability expectations in catalog + evidence.

### R9. cluster-member-strings demoted to CI/variant-only
*Sources: antithesis-fit P-1, implementability (sentinel node's metacharacter name is
allowlist-rejected AT STARTUP, breaking the 3-node baseline).*
**Applied:** demoted to CI/deterministic-variant-only in the catalog (Priority Low,
phase tag `variant(sentinel-env), CI`); evidence annotated.

### R10. cert-interval-reject-symmetry reframed as one-shot oracle calibration
*Source: antithesis-fit P-6.* Not a standing property/variant — a one-shot negative-control
calibration for the checksum oracle. **Applied** in catalog + evidence.

### R11. applier-resize kept only as a rider
*Source: antithesis-fit P-7.* A deterministic loop test covers most value; keep the
convergence check as a rider on workloads that resize appliers anyway. **Applied.**

### R12. sst-ready kept as message, not implementation effort
*Source: antithesis-fit P-17.* The v1 remainder duplicates `restarted-node-rejoins-synced`'s
timeout; keep the property text as documentation of the control-channel hazard, fold the
check into the restarted-node timeout. Bound recalibrated to ~220s script-side (see R18).
**Applied.**

### R13. crash-recovery-grep + cluster-identity: v1 supervisor replaces the attack surface
*Sources: antithesis-fit P-18, implementability property notes, wildcard "Oddities".*
The v1 marker-file entrypoint supervisor replaces/removes the shipped wrapper +
persistent-EnvironmentFile attack surface these two properties target — they'd be
permanently green / testing harness code. **Applied:** both annotated in catalog +
evidence; a **field-faithful supervisor variant** recorded as future work.
`no-dual-bootstrap-after-full-shutdown` restated per implementability #4 as per-boot
supervisor emission + workload epoch ledger.

### R14. SR un-gated for v1 (non-crash legs)
*Source: antithesis-fit P-14 — `wsrep_trx_fragment_size` is SESSION_VAR HINT_UPDATEABLE
(sys_vars.cc:8575-8583), verified.*
No server config variant needed: the workload sets fragment size per session.
**Applied:** fault/config flags updated on `sr-fragment-cross-node-agreement` (crash legs
still kill-channel-gated) and `sr-rollback-fragment-noop` (non-crash legs v1-viable);
the checksum oracle's "SR under churn" `Sometimes` marked v1-viable; catalog-wide Open
Question and SR relationship-cluster note updated.
**Conflict resolved:** catalog "SR needs a config variant" vs P-14 session-settable —
code wins; un-gated.

### R15. GU_DBUG_SYNC recorded as a precondition-manufacturing mechanism
*Source: wildcard M6 (sut-analysis §10.3; used by zero properties).*
Release-usable provider sync points can manufacture hard IST/donor race preconditions
for free. **Applied:** noted in `deployment-topology.md` (verify with one `SET` at
runtime — availability in the PXC build of galera is the stated uncertainty) and brief
notes added to the SST/IST/donor evidence files (`restarted-node-rejoins-synced`,
`donor-returns-to-synced`, `gcache-recovered-ist-completeness`,
`ist-overlap-writesets-not-reapplied`).

### R16. Known-red pre-registration
*Source: antithesis-fit CW-6.*
`nonready-node-error-code-contract` (the 1213-for-unready TOI path is deterministic) and
`maint-mode-honors-operator-intent` (forced flip fires on every non-primary view) are
**expected-failing-from-run-one bug-finders**. **Applied:** both marked in catalog +
evidence with carve-out guidance so the permanent red doesn't mask regressions
(assert the *rest* of the contract around the known-failing arm; register the known arm
as a pre-registered finding at first triage).

### R17. Instrumentation strategy decided once
*Sources: wildcard C1/M5, antithesis-fit CW-7, implementability #3.*
~20 properties deferred SUT-side markers property-by-property. **Decision (applied as a
catalog-wide Shared-conventions entry): v1 uses a shared log-string scan layer** —
unified with the `mtr_warnings.sql` suppressed-warnings denylist as candidate log
assertions — **and v2 delivers ONE SDK (libvoidstar) patchset across mysqld + the two
vendored submodules** (galera, wsrep-lib), not per-property patches. The six properties
with **no day-one form** are listed: `bf-abort-skip-awake-victim-already-killed`,
`commit-cut-bounded-by-delivered-seqno`, `gcs-total-order-gap-free`,
`vote-message-payload-contract` (sharp form), `homogeneous-cert-version-match`,
`wsrep-xid-checkpoint-monotonic` (sharp form).

### R18. Calibration-run-first for all timing bounds
*Sources: implementability #2/#8/#12, wildcard F1, antithesis-fit P-5.*
Debug-tier slowdown invalidates every numeric calibration (1-11 min overflow, ~90s
re-merge, 173-queue threshold); no calibration run was scheduled. **Applied:** catalog
Shared-conventions rule — **no numeric bound is pinned until one calibration run per
image flavor**; `sst-ready` bound re-anchored to ~220s (script-side ~100s initial +
~120s idle-stall) pending calibration; topology Open Questions gain the calibration-run
item.

---

## Gaps (recorded — targeted discovery assignments for the gap-fill round)

**STATUS (gap-fill round integrated, 2026-09-10): all 11 gaps are closed.** Gaps 1-4, 7,
and 8 are **FILLED** by seven new catalog properties (provenance "evaluation gap-fill";
evidence files in `properties/`, entries integrated into `property-catalog.md`,
`property-relationships.md`, `deployment-topology.md`): Gap 1 →
`gtid-executed-cluster-convergence`; Gap 2 → `toi-nbo-ddl-completes-or-fails-cleanly`;
Gap 3 → `ftwrl-backup-quiescent-or-fails` + `desync-ftwrl-composition-resyncs`; Gap 4 →
`skip-locked-nowait-never-fatal`; Gap 7 → `cluster-address-live-set-rejoins`; Gap 8 →
`evicted-node-rejoins-only-via-sst`. Gaps 5, 6, 9, 10, 11 were **addressed as
workload/infra edits** rather than new properties (verified present): Gap 5 → catalog
Shared conventions "Bulk-transaction generator" + topology workload-mix requirement;
Gap 6 → catalog "Deferred phase: encryption variant" + `properties/deferred-encryption-phase.md`
placeholder (not a catalog property); Gap 9 → catalog Shared conventions "DDL generator —
online-FK shapes" (target lists updated in both evidence files); Gap 10 →
`log-scan-candidates.md` (mined denylist + gap-fill pattern additions); Gap 11 → catalog
Shared conventions "Run accounting" + topology supervisor restart-accounting JSONL.

1. **FILLED → `gtid-executed-cluster-convergence`** *(coverage #1, wildcard F3 — bug pattern H has
   ZERO coverage: PXC-4652 unlocked wsrep_sidno SIGSEGV reachable with the harness's own
   `applier_threads>1`; PXC-4526; local-GTID leak family; `wsrep_write_dummy_event`
   no-op).* GTID divergence does NOT reduce to `cross-node-row-equality`. Discovery
   focus: pattern-H property `gtid_executed` equality across Synced nodes at quiesced
   checkpoints, v1 topology, no async source. **Config decision needed: `gtid_mode=ON`**
   (not currently in the topology my.cnf) — record with the property.
2. **FILLED → `toi-nbo-ddl-completes-or-fails-cleanly`** (+ NBO unsafe-counter observable
   as a supervisor extension) *(coverage #2).* TOI no-retry suicide; unbounded NBO end wait; close-during-NBO; NBO
   `unsafe_` counter leak invisible to `grastate-se-checkpoint-agreement` (seqno=-1 is a
   legal branch); TOI×SST backup-lock (PXC-5244).
3. **FILLED → `ftwrl-backup-quiescent-or-fails` + `desync-ftwrl-composition-resyncs`**
   *(coverage #3, wildcard F3).*
   FTWRL-does-not-block-COMMIT breaks backup quiescence; `try_desync_and_pause` FTWRL
   indefinite hang; `SET wsrep_desync` bypass lets FTWRL resync a user-desynced node.
   Plain-SQL, fault-free — cheapest gap to fill.
4. **FILLED → `skip-locked-nowait-never-fatal`** (rider on the BF-conflict workload,
   with `Sometimes` markers) *(coverage #4; regression target PXC-5099, previously zero
   mentions in the catalog).*
5. **ADDRESSED (workload/infra edit)** — Large-transaction/bulk-writer workload
   requirement *(wildcard F3)*: catalog Shared conventions "Bulk-transaction generator" +
   topology workload-mix requirement. The gcache
   page-store `Sometimes` in `gcache-page-files-bounded` is vacuous at 16M gcache
   without a bulk writer; state the workload requirement as a property precondition.
6. **ADDRESSED (deferred-phase placeholder)** — Encryption/keyring *(coverage #5)*:
   catalog "Deferred phase: encryption variant" + `properties/deferred-encryption-phase.md`. Decide explicitly: likely a deferred-phase
   placeholder set — TLS cert rotation, untrusted-CA merge, keyring×SST, gcache
   encryption — recorded so the zero-coverage-of-default-ON-field-config is a decision,
   not an oversight.
7. **FILLED → `cluster-address-live-set-rejoins`** (rider on
   `restarted-node-rejoins-synced`) *(coverage #10).*
8. **FILLED → `evicted-node-rejoins-only-via-sst`** — PXC-5208 post-eviction rejoin
   contract stated as an invariant *(coverage #11).*
9. **ADDRESSED (workload edit)** — galera-index-online-fk repro added to the DDL
   statement-generator target lists *(wildcard F3)*: catalog Shared conventions
   "DDL generator — online-FK shapes"; target lists updated in the `no-mdl-bf-bf-abort`
   and `bf-bf-lock-suppression-no-divergence` evidence files.
10. **ADDRESSED (infra edit)** — CI-suppressed-warnings mining *(coverage #12, wildcard
    M5)*: §9.5 `mtr_warnings.sql` denylist mined into
    `antithesis/scratchbook/log-scan-candidates.md` (18 scan-as-finding + 25 annotate
    entries, plus 5 gap-fill pattern additions), consumed by R17's shared log layer.
11. **ADDRESSED (infra edit)** — Time-to-detection / field-restart-classifier accounting
    *(wildcard M4)*: catalog Shared conventions "Run accounting" + topology supervisor
    restart-accounting JSONL (exit status/signal, graceful-vs-crash,
    would-systemd-have-restarted classifier, detection/recovery buckets).

---

## Biases (collected for human judgment — RESOLVED 2026-09-10)

All four biases were presented to the user and decided on 2026-09-10. Decisions are
recorded inline below each item.

1. **Tenant node-termination fault enablement.** ~1/3 of the catalog (all of Category 2,
   half of state transfer, 2 of the original top-10) is blocked or degraded while
   kill/stop faults are disabled in the tenant webhook config. The R2 kill channel
   mitigates (and adds steerability platform faults lack), but the platform-fault
   question stands: workload-driven kills consume workload logic and branch budget that
   platform faults would provide for free, and all-node blast patterns differ. Evidence:
   antithesis-fit CW-1/CW-2, implementability #1, coverage-balance #7, topology Open
   Question 4. **Judgment needed: request tenant termination faults now, or ship v1 on
   the kill channel alone?**
   **DECISION (user, 2026-09-10): container-kill faults will be added to the fault
   injector settings at some point (no immediate request). Ship v1 on the supervisor
   kill channel; tranche-2 kill-gated properties stay parked until platform kills land.**
2. **Catalog orientation: code-invariant-heavy vs customer-workflow coverage.** The
   catalog is anchored in code-derived invariants; field-realism surfaces are thin or
   designed out: backup actors, proxy contract (ProxySQL/clustercheck consumers), garbd,
   clone SST, DNS/dynamic-IP behavior (static IPs delete the one-shot-DNS finding class
   — wildcard F4), X-protocol (autoinc's widest window unreachable on a 3306-only
   workload — wildcard M7, coverage #8). Evidence: coverage-balance #8/#13, wildcard
   F3/F4/M7. **Judgment needed: how much field realism should v1+v2 buy, and in what
   order?**
   **DECISION (user, 2026-09-10): minimal v1 as planned (3 PXC nodes + workload, TLS
   off, static IPs, direct SQL). Proxy/garbd/TLS/DNS/clone-SST realism deferred to a
   possible later variant; no commitment made.**
3. **Scale/sequencing risk: 59+ properties.** Even with phasing, the long tail
   (tranche 3 + deferred + variants + v2 instrumentation) may never ship; wildcard F2
   flags the unowned property×variant matrix as green-forever risk. The tranche plan
   (R1) is the mitigation on offer. Evidence: wildcard F2, antithesis-fit CW-1.
   **Judgment needed: is the phased tranche plan an acceptable shipping strategy, or
   should the catalog be cut harder?**
   **DECISION (user, 2026-09-10): keep all 66 properties documented with phase tags and
   implement in batches (tranche 1 first; tranche 2 when container kills land; later
   tranches as builds/instrumentation allow). No cuts.**
4. **Customer-reportable upstream defects sitting in the scratchbook.** Two validated
   doc/product defects: (a) `wsrep_certification_rules` is an inert sysvar (zero readers
   in-tree — catalog Open Questions); (b) `evs.max_install_timeouts` doc/code mismatch
   (3-vs-1 — wildcard Oddities). Arguably also: the NBO phase-one wsrep-XID on-disk
   decrease ("Yes, this is a bug. TODO." — confirmed real durability defect in
   `wsrep-xid-checkpoint-monotonic`) and the vote NULL-deref precondition (R7 dropped it
   as untestable, not as unreal). **Judgment needed: report these to Percona now, or
   hold until runs produce reproductions?**
   **DECISION (user, 2026-09-10): report nothing without a reproduction. Hold all four
   candidates until Antithesis runs produce concrete reproductions.**

---

## Lens conflicts resolved (summary)

| Conflict | Resolution |
|---|---|
| Build order: catalog release-primary vs topology assert-first (F1 vs catalog Assumptions) | Topology wins; catalog rewritten; per-property image tags added (R1). |
| Assert-enabled build conflated with DBUG build (CW-4 vs topology "Debug also unlocks DBUG") | Implementability's code check is decisive: NDEBUG-stripped RelWithDebInfo does NOT define DBUG; three-tier image plan (R1). |
| Kill fallback "workload drives in-container kill" (catalog) vs "not realizable via SQL" (CW-2) vs supervisor-marker channel (implementability #1) | Supervisor-mediated crash channel adopted; SQL-direct kill acknowledged impossible (R2). |
| notify-cmd "one config line, add to v1" (P-13) vs "READ_ONLY, blocked harder" (implementability) | Both correct: static config variant, not runtime SET (R4). |
| SR gated on a server config variant (catalog) vs session-settable fragment size (P-14) | Code verified; SR un-gated for non-crash legs (R14). |
| pxc_maint 10s-sleep MDL: catalog vs sut-analysis contradiction (coverage uncertainty) | The catalog already carries the corrected claim ("the sleep holds no MDL" — retraction noted in `maint-mode-honors-operator-intent`); sut-analysis is the stale document; no missing property. |
| monitor-overflow: "1-11 min arithmetic" (catalog) vs budget/throughput skepticism (P-5, implementability #8) | Both lenses agree against the catalog's optimism; demoted + carve-out (R8). |

## Not applied (noted)

- Coverage-balance uncertainty "7 high-churn tickets (PXC-4676, PXC-5106) undescribed"
  — verification task for the gap-fill/next research round, not a catalog edit.
- Wildcard M3 (workload `pc.bootstrap` lever can self-inflict a split-brain finding) —
  guard rule folded into the poison-budget convention text (R6) as workload guidance.
- Wildcard C5 (clustercheck reimplementation needs a golden-equivalence test) — workload
  implementation note; recorded in the topology clustercheck bullet.
- Wildcard C3 (checksum oracle circularly depends on sync-wait; triage ordering) —
  recorded in Shared conventions as a triage-ordering note.
