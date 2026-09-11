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

# Coverage-Balance Evaluation — Property Catalog vs SUT Analysis & Topology

Lens: is the 59-property SET the right portfolio for this SUT's risk profile? Method:
every high-risk area in `sut-analysis.md` (executive-summary top-5, hotspot table §9.1,
recurring patterns A–K §9.2, ranked regression targets §9.3, churn signals §9.4,
whitespace §10.4, wildcard findings §11) was mapped against the catalog's 59 properties
and against `deployment-topology.md`'s v1 constraints. Cross-checks were done by grep
against the catalog and evidence files (e.g. zero hits for `gtid_executed` outside the
async-gated property, zero hits for SKIP LOCKED/PXC-5099, FTWRL, garbd, keyring,
encryption).

## Headline

The catalog covers the ensemble's top-5 consensus targets well — silent divergence,
crash-recovery position, SST/IST, Galera-monitor stalls, split-brain/bootstrap all have
multiple anchored properties, and the layered-oracle structure (terminal checksum +
attribution properties + backstop tripwires) is sound portfolio design. The imbalances
are at the edges: an entire recurring bug pattern (H — GTID on the wsrep path) has zero
coverage; two ranked regression targets (PXC-5099, PXC-4652) map to no property; the
TOI/NBO DDL *lifecycle* and the backup/FTWRL/desync *composition* exist only as angle
notes inside other properties; the encryption/keyring surface (a default-ON field
config) has zero properties even as deferred placeholders while dormant version-gate
machinery got three; and two catalog-vs-topology contradictions (primary image build
type; node-termination availability) put a large fraction of the implement-first set at
sequencing risk.

## Findings

### F1 (catalog-wide) — Bug pattern H (GTID on the wsrep path) has zero coverage; two ranked regression targets fall through the crack

- **Concern**: sut-analysis §9.2 pattern H is a recurring (≥3 tickets, actually ~8)
  class: PXC-4526 (8.4 tagged GTIDs truncated under CHECKSUM_ALG_UNDEF → **eviction**;
  ranked-target table §9.3, 2025-09-09), PXC-4652 (unlocked `wsrep_sidno` →
  `rpl_gtid_owned` corruption → SIGSEGV; ranked-target table, **explicitly requires
  `wsrep_applier_threads>1` — exactly the config the harness sets**), and the local-GTID
  leak family PXC-4313/4312/4504/4238/4034/4544. §6.9 additionally documents
  `wsrep_write_dummy_event` as a **no-op** (`wsrep_binlog.cc:394-402`) called on TOI/NBO
  begin-failure → seqno consumed cluster-wide with nothing in the local binlog →
  GTID/binlog discontinuity for async replicas; and §11 (focus 12 §4) notes the hidden
  post-SST mysqld may mint GTIDs no other node has (open question 38). The catalog's only
  GTID property, `duplicate-gtid-skip-exactly-once`, is about the async-monitor skip path
  and is gated on the async-source topology (excluded from v1). **No property asserts
  cluster-wide `gtid_executed` convergence** — the observable sut-analysis §6.9 itself
  names (`galera_gtid.test:26-30`, equality under `wsrep_sync_wait=7`).
- **Why it doesn't reduce to the terminal oracle**: GTID-set divergence (local leaks,
  dummy-event holes) occurs with **identical row data** — `cross-node-row-equality`
  passes while the GTID/binlog stream any downstream async replica or failover tooling
  consumes is broken. This is exactly the cross-cutting cache/replication-analogue gap
  class (binlog+GTID for downstream consumers) that per-focus discovery misses.
- **Scope**: catalog-wide (missing property + missing workload dimension: tagged GTIDs
  `gtid_next='UUID:tag:N'`, `sql_log_bin=0` sessions, DROP IF EXISTS, RESET BINARY LOGS).
- **Allocation contrast**: category 9 spends 3 properties on dormant version-gate
  machinery (two Low, one Medium — tripwires for a homogeneous cluster) while pattern H
  gets 0.
- **Suggested action**: add a `gtid-executed-cluster-convergence` property (workload-only:
  per-node `gtid_executed` equality at quiesced sync barriers; `Sometimes` markers for
  TOI-failure dummy events and PXC-4652's applier-parallel path; PXC-4526 needs tagged
  GTIDs in the workload). Independent of the async-topology decision — it runs on the v1
  3-node topology as-is.

### F2 (catalog-wide) — TOI/NBO DDL lifecycle liveness is unowned; the NBO `unsafe_` leak has no detector

- **Concern**: sut-analysis §6.13 states a property almost verbatim — "TOI DDL during
  join/FC/kill either completes everywhere or fails on the originator; never leaves a
  permanently held TO slot" — with concrete mechanisms: PXC TOI does not retry
  (single-attempt `poll_enter_toi` → `e_deadlock_error`), `to_isolation_begin`
  monitor-entry failure → `gu_throw_fatal` (**node suicide from ordinary DDL**,
  `replicator_smm.cpp:1911-1915`), **NBO end wait unbounded** (`:1805-1820`), closing
  during NBO → "node left in inconsistent state, must be re-initialized by full SST"
  (`:1808-1812`), and the known-broken NBO 2nd-phase cert-index-clear-while-MDL-held
  ("it is as it is", `wsrep_mysqld.cc:3280-3296`, §11). No catalog property owns any of
  these; TOI/NBO appear only as angles inside `commit-order-monitor-released…` (failed-TOI
  monitor leak), `no-mdl-bf-bf-abort` (NBO-wait carve-out `Sometimes`),
  `applier-threads-never-read-only` (NBO THD), and `failed-state-transfer-node-rejoins`
  (NBO-in-flight joiner abort).
- **Second, sharper sub-gap**: sut-analysis §4.2 states an explicit property claim —
  "`unsafe_` returns to 0 after every NBO completes/aborts/partitions mid-NBO; **a leak =
  permanent forced SST on every restart**" (unbalanced `mark_unsafe` at
  `replicator_smm.cpp:603/:619` vs single decrement `:1971`). No property owns it, and it
  is **invisible to `grastate-se-checkpoint-agreement`**: a leaked `unsafe_` forces
  on-disk seqno -1, which is the *legal* branch of that property's invariant. The failure
  presents as "every restart takes SST" — degradation none of the SST/IST liveness
  properties distinguish from a legitimately insufficient gcache.
- **Third sub-gap**: TOI DDL × active SST donation (donor holds the PXB backup DDL lock
  for the whole SST — lock-ddl=ON forced, PXC-5244/PXB-3818, §8.1; "TOI DDL during SST =
  interaction hazard") has no owner and no `Sometimes` marker anywhere.
- **Scope**: catalog-wide (missing 1–2 properties + workload actions). NBO properties need
  the SR-style config note (`wsrep_OSU_method=NBO` per statement; tech preview).
- **Suggested action**: add `toi-nbo-ddl-completes-or-fails-cleanly` (liveness: DDL storm
  under partitions/FC/joins → every DDL ends in success-everywhere or originator-error
  within a bound; `Unreachable` at the `to_isolation_begin` `gu_throw_fatal`) and a
  `nbo-unsafe-counter-balanced` check (cheapest observable: after NBO churn + graceful
  restart with warm gcache, node rejoins via IST not SST — or a SUT-side probe on
  `unsafe_`).

### F3 (catalog-wide) — Backup/FTWRL/desync/pause composition is uncovered

- **Concern**: the operational scenario every PXC deployment runs (backup via
  FTWRL/`LOCK INSTANCE FOR BACKUP`/`wsrep_desync`) spans three documented hazards with no
  property owner: (a) `try_desync_and_pause` re-enters the local monitor at a
  non-consecutive seqno after a warn-only mismatch → **FTWRL / `wsrep_desync=ON` hangs
  indefinitely** (focus 3 §5.6, `replicator_smm.cpp:3419-3511`); (b) `SET GLOBAL
  wsrep_desync` bypasses wsrep-lib desync accounting entirely (check-fn calls provider
  directly, update-fn no-op, `wsrep_var.cc:693-743`) → **FTWRL can resync a
  user-desynced node** (§6.10); (c) **FTWRL does not block COMMIT in PXC**
  (`handler.cc:1841-1857` skips the MDL COMMIT lock for wsrep THDs) — backup tooling's
  commit-quiescence assumption is structurally broken (§7.5/focus 12 §9). The catalog
  touches the area only obliquely: `donor-returns-to-synced` (desync refcount leak),
  `grastate-se-checkpoint-agreement` (kill inside the pause() window),
  `clustercheck-200…` (frozen-InnoDB-writes symptom). None asserts that a
  FTWRL/desync/backup-lock cycle under load completes, releases, and leaves the three
  desync bookkeepers agreeing.
- **Scope**: catalog-wide (one composition property + workload actions: FTWRL loops,
  LOCK INSTANCE FOR BACKUP, `wsrep_desync` toggles under load — all plain SQL, no faults
  or instrumentation needed; the cheapest kind of addition).
- **Suggested action**: add `ftwrl-desync-cycle-completes-and-balances` (liveness: FTWRL
  acquires+releases within bound under load; safety: post-cycle
  `wsrep_desync/wsrep_desync_count`/provider state agree; `Sometimes`: FTWRL overlapped a
  donation and a view change). This also gives the pause()-window kill recipe of
  `grastate-se-checkpoint-agreement` its trigger workload.

### F4 (catalog-wide) — Ranked regression target PXC-5099 (pattern I: wsrep × InnoDB lock-wait surface) has no property and no workload coverage

- **Concern**: sut-analysis §9.2-I / §9.3: SELECT FOR UPDATE **SKIP LOCKED** — the wsrep
  patch makes any lock request potentially wait on an HP applier → `DB_SKIP_LOCKED` trips
  a **release-fatal assert** (`row0mysql.cc:1221`); fixed 2026-03-13 at a single call
  site with the underlying issue ("wsrep makes lock waits appear where InnoDB doesn't
  expect") explicitly unchanged — the textbook shape of a fragile fix. Grep confirms zero
  mentions of SKIP LOCKED/NOWAIT/PXC-5099 in the catalog or any evidence file; none of
  the conflict-workload descriptions (hot rows + UK churn, `no-mdl-bf-bf-abort` DDL
  storm) include locking-clause reads.
- **Scope**: small — a workload dimension plus one marker; but it is a top-15 ranked
  regression target with literally no path to being exercised by the current plan.
- **Suggested action**: add SKIP LOCKED / NOWAIT / high-priority-wait statements to the
  shared conflict workload with a `Sometimes(skip-locked-under-applier-conflict)` marker;
  the failure detector is the existing node-death/assert oracle (folds into the
  `trx-replay-never-fatal` / replay-cluster infrastructure rather than needing a
  standalone property).

### F5 (catalog-wide) — Encryption/keyring surface: zero properties for a default-ON field configuration

- **Concern**: `pxc_encrypt_cluster_traffic` defaults ON (read-only) in the field; the
  topology sets it OFF for v1 (justified), and the catalog contains **zero** properties —
  not even config-gated/deferred ones — for the whole surface. Compare: SR (also off by
  default) gets 2 properties gated on a config variant; the async topology gets 2 gated
  on a topology decision. The sut-analysis findings left orphaned: cert rotation via
  ALTER INSTANCE RELOAD TLS does not cover the Galera channel → **expiry breaks ALL
  inter-node TLS simultaneously** (cluster-wide outage class, §8.2); two independently
  bootstrapped halves have mutually-untrusted auto-generated CAs and **can never merge**
  (§8.2 — a partition-heal permanent-failure mode invisible to the v1
  `partition-heal-single-primary-remerge`); keyring×SST ordering (donor untimed SQL
  keyring check, joiner filesystem JSON grep, disagreement detected only after transfer
  starts, plaintext transition key in sst_info, keyring reload after SST
  `wsrep_mysqld.cc:1327-1330`, keyring-before-Galera init reordering §11); and gcache
  encryption — **the only combination axis in the entire MTR corpus** (§10.1) — plus the
  PXC-only `galera_rotate_gcache_key` vtable entry (§3.1), untested by anything.
- **Scope**: catalog-wide (missing deferred family). Not a v1 blocker, but the catalog is
  the document of record for what should eventually be tested; today it silently
  inherits the topology's simplification.
- **Suggested action**: add 2–3 explicitly variant-gated properties (TLS-variant image:
  `cluster-tls-rotation-no-simultaneous-outage`, `split-bootstrap-ca-merge-detected`;
  keyring variant: keyring×SST completes or fails explicitly), mirroring how SR/async
  gating is recorded, so the debt is visible in the catalog rather than only in the
  topology's "earmarked for later" note.

### F6 (catalog-wide) — Catalog assumes release-image-primary; topology builds assert-enabled first — several properties' invariants are conditioned on the image the v1 harness won't run

- **Concern**: catalog Assumptions: "primary test image is the release (NDEBUG) build;
  an assert-enabled (debug) image variant is assumed available". Topology Key decisions:
  "Start with **assert-enabled** build ... add pure-release (NDEBUG) image later" (open
  question 7 leaves the release image unscheduled). Directly affected invariants:
  `inconsistency-vote-evicts-divergent-minority` asserts eviction is "detected as
  provider-Disconnected ... (release-build eviction CLOSES the provider; mysqld stays
  alive), **never as process death**" — in an assert-enabled image the same scenario can
  legitimately die on a previously-NDEBUG'd assert, making the `Always` fire on a
  non-bug; `illegal-wsrep-transition-never-taken` targets warn-and-proceed behavior that
  *only exists* under NDEBUG; `no-spurious-multi-major-detection` and
  `maint-mode-honors-operator-intent` are explicitly scoped "release image";
  `monitor-window-overflow-unreachable` and the `replicator_smm.cpp:3893-3910`
  release-misbehavior sites are NDEBUG-conditioned. Conversely ~20 properties carry
  "(missing)" SUT-side SDK assertions while the topology defers C/C++ instrumentation
  (libvoidstar) to a later phase — their sharp forms are unimplementable in v1 and only
  some state workload fallbacks.
- **Scope**: catalog-wide consistency/sequencing, not correctness of any single property.
- **Suggested action**: reconcile the dual-image order once, then tag every property with
  (a) which image its invariant is valid in and (b) day-one vs instrumentation-gated.
  Without this, the first triage cycle will misclassify assert-deaths as findings against
  release-conditioned `Always` invariants.

### F7 (catalog-wide) — Node-termination gating concentrates ~40% of the catalog, including implement-first entries, behind an unresolved fault-availability question with no designed substitute

- **Concern**: all of category 2 (7 properties, including top-10 #3
  `acked-commit-durable-across-restart` and #6 `grastate-se-checkpoint-agreement`), half
  of category 3, and flagged variants elsewhere require kill/restart. Topology assumption
  5: termination faults disabled in the tenant default; the stated fallback is *graceful*
  restarts via SQL SHUTDOWN — which specifically does NOT exercise the crash windows these
  properties target (unclean grastate, torn gcache, kill-inside-pause). The catalog's own
  open question says "the workload must drive kills/restarts itself (docker stop /
  in-container kill)" but the topology's workload container has no mechanism to kill -9 a
  mysqld in another container, and the entrypoint supervisor design doesn't mention a
  kill hook. Net: two of the ten implement-first properties may be untriggerable in v1
  as designed.
- **Scope**: catalog-wide sequencing; deployment design.
- **Suggested action**: either resolve tenant termination faults before v1, or add a
  designed kill channel (e.g. a tiny in-node sidecar/SQL-triggered `kill -9 $(pidof
  mysqld)` test command) and record it; otherwise demote the termination-gated top-10
  entries out of implement-first for v1.

### F8 — `autoinc-identity-no-cross-node-collision`: its widest claimed window is untestable in the deployed topology

- **Property**: `autoinc-identity-no-cross-node-collision`.
- **Concern**: the property names "the X-plugin document-id aggregator's configure-once
  cache (`document_id_aggregator.cc:38-60`) — untouched by the refresh, **the widest
  window**" as a live channel, but the topology's workload is classic-protocol only
  (mysql CLI / PyMySQL, port 3306); no mysqlx client, no 33060 exposure. The strongest
  leg of the property can never fire.
- **Suggested action**: either add an X-protocol session to the workload (cheap —
  `mysqlx` via the same connector) or annotate the leg as out-of-scope-by-topology so
  triage expectations are calibrated.

### F9 — Config-uniformity divergence family is half-covered

- **Properties**: `cert-interval-reject-symmetry` (exists); nothing for the server-side
  half.
- **Concern**: sut-analysis §6.2 flags two peer config-uniformity divergence channels
  with equal weight: hidden provider cert params (covered) and "**Nothing validates
  cross-node lower_case_table_names / collations / charsets** ... cert keys are raw name
  bytes with no case folding → conflicting txns certified as non-conflicting →
  undetected divergence" (uncovered — no property, and the harness's single shared
  my.cnf makes the misconfig unreachable by construction, so it also can't emerge from
  exploration). The catalog covered the harder-to-reach half and skipped the
  operator-realistic half (lctn mismatch is a classic real-world PXC misconfiguration).
- **Suggested action**: a deliberate config-asymmetry variant (one node
  `lower_case_table_names` differing) with the existing checksum oracle, or an explicit
  catalog note that config-asymmetry divergence is out of scope and why.

### F10 — Runtime-reconfig-under-load whitespace has one property for a multi-sysvar surface

- **Properties**: `applier-resize-converges` (only entry).
- **Concern**: sut-analysis §10.4 lists "runtime config change under load (~35
  galera_var_* tests all quiescent)" as Antithesis whitespace, and §3.8 enumerates the
  live-reconfig surface: `wsrep_cluster_address` (live rejoin under load),
  `wsrep_desync` (bypasses wsrep-lib accounting), `wsrep_provider_options` (arbitrary
  option string incl. pc.bootstrap/gmcast.isolate under load). Only the applier resize
  got a property; `cert.optimistic_pa` SET-under-load is demoted to an amplifier note.
  A `SET GLOBAL wsrep_cluster_address` live-rejoin under load has no owner at all.
- **Suggested action**: fold reconfig actions into the standing workload (cluster_address
  re-set, desync toggle — partially serves F3) and add `Sometimes` markers; a dedicated
  property is optional, the workload dimension is the real gap.

### F11 — PXC-5208 (newest ranked regression target) is covered only as an angle note, not an owned invariant

- **Properties**: `privilege-context-divergence-never-evicts` (angle),
  `inconsistency-vote-evicts-divergent-minority`.
- **Concern**: PXC-5208 (2026-09-07 — days before the catalog date; #1 in the ranked
  table) concerns what an inconsistency-evicted node does on restart under the shipped
  default `force_sst_after_inconsistency=OFF`: grastate preserved → rejoin via IST into
  corrupt state (sut-analysis §6.3 + open question 21). The catalog's treatment is one
  sentence inside the privilege property's angle ("run the eviction workload paired with
  the checksum oracle"). No property's invariant states the post-eviction contract
  ("an evicted node never reaches Synced again while still divergent").
- **Suggested action**: promote to an explicit clause of
  `inconsistency-vote-evicts-divergent-minority` (post-eviction restart: node either
  takes SST or the checksum oracle flags it before it serves) with its own `Sometimes`
  (evicted node restarted and rejoined) — the machinery all exists; only ownership is
  missing.

### F12 — CI-suppressed warning list not systematically mined

- **Properties**: partially `gcs-total-order-gap-free`, vote properties.
- **Concern**: sut-analysis §9.5 calls the mtr_warnings.sql suppressions ("Gap in state
  sequence", "Quorum: No node with complete state", "Failed to report last committed",
  "Ignoring possible split-brain", "install timer expired", "Query apply failed"…)
  "ready-made Antithesis assertion candidates — CI cannot see them by design". A few
  overlap existing properties; most have no log-scan assertion in the catalog. This is
  the cheapest possible coverage (passive log greps) left on the table.
- **Suggested action**: one log-scan assertion pack property (Always: none of the
  suppressed-in-CI degraded-state lines appear, with per-line carve-outs where a
  property already owns the mechanism), or distribute the lines as fallback detectors
  into the owning properties.

### F13 — Component blind spots acknowledged by topology but invisible in the catalog: garbd, clone SST, DNS re-resolution

- **Concern** (three small items, same shape — deliberate v1 exclusions that leave no
  trace in the catalog, unlike SR/async which are recorded as gated):
  - **garbd**: the third shipped build artifact (§2); zero catalog mentions. The
    arbitrator path (data-less quorum member, its own SST-request handling) is a real
    field topology.
  - **clone SST**: PXC-4469 shows 4-commit churn (§9.4); `interrupted-sst-forces-full-sst`
    even cites a clone-script break point (`wsrep_sst_clone.sh:1199-1224` hand-written
    grastate, no fsync/rename) — but the deployment runs xtrabackup-v2 only, so that
    cited break point is unreachable as deployed.
  - **DNS one-shot resolution**: §8.3 calls it the "highest-value external-dep finding
    for containers"; the topology deliberately designs it out with static IPs. Correct
    call for v1 noise control, but no deferred property records it.
- **Suggested action**: add a "deferred variants" section to the catalog (garbd variant,
  clone-SST variant, dynamic-DNS variant) so exclusions are owned decisions with
  re-entry criteria, not silent drops.

### F14 — Minor: optional guarantee-void levers (`wsrep_certify_nonPK=OFF`, per-node `pxc_strict_mode`, `wsrep_dirty_reads`) untested and unrecorded

- **Concern**: sut-analysis §6.1 lists documented guarantee-void levers as "fault
  injectors". The catalog exercises PK-less tables under the *default* certify_nonPK=ON
  (`bf-bf-lock-suppression…` FK-cascade angle) but has nothing for certify_nonPK=OFF,
  per-node strict-mode asymmetry (divergence-by-config), or the `wsrep_dirty_reads`
  read contract on non-primary nodes. These are low-priority by design (documented
  voids), but per-node strict-mode asymmetry is operator-reachable and produces exactly
  the silent divergence class the catalog centers on.
- **Suggested action**: at most one config-asymmetry variant note (can share F9's
  variant); explicitly document the rest as out-of-scope voids.

## Passes

- **Top-5 consensus targets**: all covered with multiple properties and realized-bug
  anchors. Target #1 (silent divergence) gets the strongest allocation — terminal oracle
  + 8 attribution properties + the vote-integrity pair — matching its "worst failure
  mode" status and the §10.5 oracle-gap argument.
- **Patterns A, B, C, E, F, G, J, K**: each maps to ≥1 property with the realized tickets
  cited (A: `no-mdl-bf-bf-abort` + `bf-bf-lock-suppression…`; B: privilege/vote trio; C:
  monitor-release cluster of 5; E: nine SST/IST properties matching hotspot #1's 169
  commits; F: three gcache properties; G: XID pair; J: `applier-threads-never-read-only`;
  K: `cluster-member-strings-never-reach-shell` against both 2026 CVEs).
- **Assertion-type balance**: ~41 safety / ~12 liveness / ~3 reachability-negative with
  consistent `Sometimes` vacuity guards is a reasonable mix for a
  consistency-critical SUT; the layered design (FC watchdog → queue bound → monitor
  overflow backstop; instantaneous split → restart-window split → sequential-lineage
  fork) is deliberate and documented in property-relationships.md.
- **MTR-distortion reversal**: the portfolio consciously tests what MTR structurally
  cannot (flush=1, real EVS timers, applier_threads>1, sync_wait=0-and-1, real
  partitions) — directly responsive to §10.2.
- **Health/observability truthfulness category**: unusual and well-motivated
  (clustercheck lies, maint-mode hijack, zombie nodes) — keeps every other oracle honest
  and matches §8.7's field-integration findings.
- **Negative findings pruned honestly**: the catalog demotes properties when rationale
  died (`crash-recovery-grep…` to Low after the vestigial-script discovery;
  `local-monitor-freed…` "possible live bug" withdrawn) rather than padding the count.

## Uncertainties

- **maint-mode 10s-sleep/MDL contradiction**: the catalog retracts "the sleep holds no
  MDL" while sut-analysis asserts twice (§6.14, §8.7) that the SET path sleeps 10s
  *holding MDL after open_tables_for_query* (`sql_parse.cc:4518-4523`) with a
  concurrent-TOI cluster-wide block. One of the two artifacts is wrong; if the
  sut-analysis is right, a small liveness property (SET pxc_maint_mode vs concurrent TOI
  DDL) is missing. Needs a code re-check.
- **Unexamined high-churn tickets**: §9.4 lists PXC-4676 (7 commits), PXC-5106 (5,
  reverted twice then re-landed), PXC-4800, PXC-4741, PXC-4593, PXC-4645, PXC-4255 with
  no description anywhere in the research set — coverage of these cannot be confirmed
  or denied from the artifacts. If any is in a subsystem outside the covered clusters,
  it is an invisible gap.
- **Whether GTID convergence has a hidden subsumption argument**: I found none —
  `gtid_executed` divergence with identical rows is real (local GTID leak family) — but
  if the ensemble deliberately dropped pattern H for a reason, it is recorded nowhere.
- **In-container kill capability**: F7's severity depends on whether the harness can
  deliver in-container SIGKILL via test commands (topology is silent); if yes, F7
  reduces to a documentation fix.
- **Stale-read (sync_wait=0) property set**: sut-analysis (focus 10) prescribes "test 0
  and 1 as separate property sets"; the catalog has the =1 set
  (`sync-wait-reads-observe-acked-writes`) and arguably covers the =0 contract via
  `first-committer-wins…` (no phantom/rolled-back data observable). Whether a distinct
  =0 property (e.g. session-consistency floor under dirty_reads=OFF) is worth adding is
  a judgment call, not a clear gap.
- **Memory/disk-fault under-investment**: whitespace §10.4 lists disk faults (ENOSPC/EIO
  vs the warnings-only grastate writer) and memory pressure (cgroup-blind recv_q) with
  only "widens/flagged" mentions in the catalog. Whether the platform's fault set makes
  these first-class triggers viable determines if this is a gap or a correct deferral.
