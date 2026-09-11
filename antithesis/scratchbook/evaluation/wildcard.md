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

# Wildcard Evaluation — PXC 8.4.10 Property Catalog

Deliberately adversarial pass over the catalog (59 properties), the SUT analysis, the
deployment topology, the relationships map, and the existing-assertions scan. This lens
questions the framing itself, hunts for angles no property models, and cross-cuts the
other three lenses (Antithesis Fit, Coverage Balance, Implementability). Findings are
ordered catalog-wide first, then narrower. Where a finding overlaps another lens's remit,
the overlap is stated and only the cross-cutting residue is developed here.

---

## 1. Framing challenges

### F1 (catalog-wide, HIGH): The catalog and the topology disagree about which build is primary — and the catalog's reachability analysis is build-conditional

The property catalog's Assumptions say: "Dual-image strategy: **primary test image is the
release (NDEBUG) build**; an assert-enabled (debug) image variant is assumed available as
a 'spec oracle'" (property-catalog.md, Assumptions). The deployment topology says the
opposite: "Build type: **Start with assert-enabled build** (RelWithDebInfo with NDEBUG
removed, or Debug); add pure-release (NDEBUG) image later" (deployment-topology.md, key
decisions table and Instrumentation section).

This is not a paperwork nit. A large fraction of the catalog's per-property reasoning is
explicitly conditioned on release-build (NDEBUG) semantics:

- `inconsistency-vote-evicts-divergent-minority`: "release-build eviction CLOSES the
  provider; mysqld stays alive" — in an assert-enabled build, several vote-adjacent
  asserts (`certification.cpp` `assert(!inconsistent_)`, the vote-path
  `assert(0==e.data_len())` at `replicator_smm.cpp:626-637`) fire and turn eviction into
  SIGABRT. The property's invariant ("detected as provider-Disconnected ... never as
  process death") is written for the image the topology does not build first.
- `illegal-wsrep-transition-never-taken`: its whole premise is "release builds log and
  proceed anyway." On the v1 assert-enabled image the transition matrices are enforced
  by `assert(0)` — the property degenerates to native crash detection and its carefully
  designed markers never fire as designed.
- `maint-mode-honors-operator-intent` and `no-spurious-multi-major-detection` are both
  scoped "in the release image" for their reachability arguments.
- `monitor-window-overflow-unreachable` and the `enter_apply_monitor` corruption path
  (sut-analysis §11) "only exist when asserts are compiled out."

In the other direction, several silent-divergence generators (the catalog's crown
jewels) may be *pre-empted* on the assert-enabled image: ~119 asserts in
`transaction.cpp` and ~60 in `certification.cpp` (sut-analysis §11 NDEBUG meta-issue)
will often abort at the corruption point before the external checksum oracle ever
observes row inequality. That is arguably better detection — but it means the top-10
priority ordering (computed around "external oracle is mandatory because the SUT can't
see divergence") was ranked for a build that is not v1, and the first triage cycles will
produce SIGABRT findings whose catalog-designed assertion messages never attach.

Two concrete secondary hazards of the assert-first choice, both already in the evidence
but not connected to the decision:

1. **Assert expressions with side effects exist in this tree**: the broken
   `assert(locked_ = true)` (assignment) in `TrxHandleLock::unlock`
   (`trx_handle.hpp:1156`, sut-analysis §5.6) *executes and mutates state* in the
   assert-enabled image and is compiled out in release — the two images have genuinely
   different behavior on that path. The topology's own Assumption 2 ("may hit debug-only
   side effects in assert expressions") is thus already confirmed by the ensemble's own
   evidence; nobody joined the two facts.
2. **All calibrated liveness bounds are build-dependent.** At least 8 properties carry
   "needs calibration run" open questions for their T values
   (`joiner-reaches-synced-after-state-transfer`, `flow-control-pause-releases`,
   `clustercheck-200-implies-write-progress`, `fatal-node-terminates-no-zombie`,
   `graceful-shutdown-bounded`, `partition-heal-single-primary-remerge`,
   `synced-node-recv-queue-bounded`, `monitor-window-overflow-unreachable`). If those
   are calibrated on a Debug build (topology's fallback, with an acknowledged throughput
   penalty) they will be wrong — likely too loose — for the release image added later,
   silently weakening every liveness property in the field-behavior variant.

**Suggested action**: Resolve the contradiction explicitly in both documents. Whichever
image is v1, add a per-property "build validity" tag (release-only / debug-only /
both-with-different-observables) — the catalog already flags debug-*required* properties
but never flags release-*required* reasoning. Re-derive the implement-first list for the
actual v1 image. Plan calibration per image, not once.

### F2 (catalog-wide, HIGH): The variant matrix is unowned — 59 properties quietly assume ~10 distinct configurations and no document reconciles them

Reading across the Antithesis Angle rows, the catalog's properties collectively require:
release image; assert-enabled image; `wsrep_trx_fragment_size>0` (SR variant); an async
source topology (5th container); a `wsrep_notify_cmd`-configured variant; a
`cert.optimistic_pa=yes` variant; `wsrep_retry_autocommit=0` sessions;
`cert.max_length=512` variant; `pxc_maint_transition_period=0` variant;
`force_sst_after_inconsistency` ON and OFF; a metacharacter-`wsrep_node_name` sentinel
node; a TLS variant (deferred); a DBUG multi-major variant (debug); "do not run
`no-spurious-multi-major-detection` and `rolling-upgrade-write-gate` in the same
variant." The relationships doc groups a few of these (SR cluster, async cluster,
version-gate cluster) but there is no catalog-wide table of *which properties run in
which environment*, and no statement of how many environments the project will actually
sustain.

The failure mode this invites is specific: a property gets implemented against the
default environment where its trigger config is absent, its `Sometimes` vacuity guard
never fires, and it sits green forever — dead weight that *looks* like coverage. The
catalog's own discipline (vacuity guards everywhere) will detect this at triage, but
only if someone audits never-fired Sometimes markers per variant; nothing assigns that
audit.

Relatedly, the sequencing risk on 59 properties is real but under-structured. Counting
invariant clauses, the catalog implies roughly 150+ distinct assertion instances plus
shared infrastructure (checksum oracle, ack journal, startup probe, supervisor,
`wsrep_monitor_status` poller, log scanner). The top-10 helps, but even the top-10
straddles tiers: #10 (`no-mdl-bf-bf-abort`) wants a SUT-side Unreachable in mysqld, #3
requires node-termination faults that topology Assumption 5 says are disabled by
default, #6 requires the harness startup probe. There is no explicit "day-one set"
defined as *workload-SQL-only against the default environment* — by my reading that set
is: `non-primary-rejects-writes`, `cross-node-row-equality`,
`first-committer-wins-loser-leaves-no-trace`, `sync-wait-reads-observe-acked-writes`,
`clustercheck-200-implies-write-progress`, `flow-control-pause-releases`,
`partition-heal-single-primary-remerge`, `at-most-one-primary-component`,
`bf-replay-commits-exactly-once` — nine properties, which is a perfectly good phase 1
if it were written down.

**Suggested action**: Add a per-property `environment/variant` field and an
`instrumentation tier` field (workload-only / status-var / log-scan / harness-probe /
SDK-in-server / SDK-in-submodule) to the catalog; publish the variant×property matrix;
define phase gates so the long tail has explicit owners and triggers rather than being
"later."

### F3 (catalog-wide, HIGH): The workload model is transaction-DML-plus-DDL-storm; several things Percona's customers actually run have no property forcing them

The catalog's implicit workload is: concurrent SQL transactions, hot-row conflicts, a
TOI DDL storm, per-node targeted probes. Field PXC deployments are dominated by three
additional pressures, all evidenced in the SUT analysis and none forced by any property:

1. **Backups against a live cluster.** XtraBackup-against-a-donor is *the* flagship
   Percona pairing, and the SUT analysis contains two verified backup-shaped findings
   with no owning property: **FTWRL does not block COMMIT in PXC** (`handler.cc:1841-1857`
   skips the MDL COMMIT lock when WSREP — sut-analysis §7.5; breaks every backup tool's
   commit-quiescence assumption), and the `acquire_shared_backup_lock` failure path that
   causes **voteless unilateral self-eviction** (`wsrep_high_priority_service.cc:453-456`
   → `replicator_smm.cpp:626-637`). FTWRL appears in the catalog only as a *trigger* for
   the grastate pause()-window kill. A cheap property exists here: "a FTWRL/BACKUP-lock
   holder never causes eviction, and FTWRL's documented guarantee holds or its PXC
   deviation is pinned as intended behavior." The workload should include a
   backup-ish actor (FTWRL / LOCK INSTANCE FOR BACKUP / long `mysqldump`-style read txn)
   as an ambient mix component — it also heats the pause() window, desync paths, and
   the lock-ddl SST interaction (PXC-5244) essentially for free.
2. **Outbound async replication (PXC as GTID source).** The SUT analysis documents a
   whole GTID-leak family (pattern H: PXC-4313/4312/4504/4238/4034/4544, tagged GTIDs
   PXC-4526, unlocked `wsrep_sidno` PXC-4652) and the verified
   `wsrep_write_dummy_event` **no-op** (`wsrep_binlog.cc:394-402`) that consumes a
   cluster seqno with nothing in the local binlog — "GTID/binlog discontinuity for async
   replicas" (sut-analysis §6.9). The catalog covers *inbound* async
   (`duplicate-gtid-skip-exactly-once`) but has **no property for cluster-wide
   `gtid_executed` set equality**, despite it being a one-SELECT check at the same
   quiesced checkpoints the checksum oracle already needs, with an in-tree precedent
   (`galera_gtid.test:26-30`). This is the cheapest genuinely-missing data-integrity
   property in the whole review: no new topology, no new faults, catches all of family
   H, and directly guards the customer topology (PXC → async replica) that Percona's
   own docs promote.
3. **Large transactions / LOAD DATA.** Nothing in the catalog forces writesets large
   enough to exercise `wsrep_max_ws_size` rejection, gcache page-store spill (which
   `gcache-page-files-bounded` *needs* — its `Sometimes(page store actually used)` will
   sit vacuous under an OLTP-sized mix with a 16M gcache unless the workload
   deliberately writes multi-MB transactions), or fragment-count-heavy SR. One
   bulk-writer actor covers all three.

Additionally, one ready-made repro from the tree is oddly unclaimed: `disabled.def`
lists `galera-index-online-fk` as disabled because "fk_40 **triggers inconsistency
voting**" — i.e., Percona has a known, reproducible cluster-inconsistency generator
(online ALTER ADD INDEX on FK tables) that was disabled rather than fixed (sut-analysis
§9.5). The `no-mdl-bf-bf-abort` DDL-storm list (RENAME, TRUNCATE parents, trigger
inserts, no-op UPDATEs) does not include online ALTER-on-FK-tables. It should be the
first statement in the generator.

**Suggested action**: Add a backup-actor and a bulk-writer to the workload spec; add a
`gtid-executed-cluster-agreement` property (quiesced `Always`, shares the checksum
checkpoint); add online ALTER-on-FK to the DDL generator target list.

### F4 (catalog-wide, MEDIUM): Two topology decisions silently delete or distort findings the SUT analysis called out as top-value — with no deferred-property record

1. **Static IPs erase the DNS one-shot-resolution finding.** sut-analysis §8.3 calls
   gcomm's resolve-once-retry-stale-IP-forever behavior "the highest-value external-dep
   finding for containers" (it is exactly the K8s-operator incident shape). The topology
   deliberately pins static IPs so this never fires ambiently — a defensible harness
   choice — but the catalog contains **no property slug** for it, so the deferral exists
   only as a sentence inside deployment-topology.md. Deferred work that has no artifact
   in the catalog will not ship. The same pattern nearly happened to the TLS findings
   (§8.2), which at least got an explicit "earmarked for a TLS variant" note.
2. **TLS OFF changes dual-bootstrap semantics relative to the field.** With
   `pxc_encrypt_cluster_traffic=ON` (the read-only field default), two independently
   bootstrapped halves auto-generate mutually untrusted CAs and **can never merge**
   (sut-analysis §8.2) — the field failure mode of a dual bootstrap is a permanent
   partition. In the v1 OFF harness, the halves *can* attempt re-merge, so
   `cluster-identity-single-lineage` / `no-dual-bootstrap-after-full-shutdown` will
   explore merge behaviors that are unreachable in default field deployments, and will
   miss the field's actual outcome. Not wrong, but the divergence should be recorded on
   those two properties the same way the supervisor-restart divergence is recorded
   catalog-wide.

Also under this heading: the catalog's bootstrap/lineage severity claims lean on field
recovery automation (systemd EnvironmentFile, operator "most advanced node" logic) that
lives in percona-docker / percona-operator repos **outside the approved scope**. The
open questions flag this honestly, but the scope limitation should be surfaced to the
customer as a named residual risk: the bootstrap automation most container users
actually run is untested by this plan.

---

## 2. Missing angles

### M1 (catalog-wide, HIGH): Silent divergence is partially self-masking under this workload — the terminal oracle's sensitivity is a function of workload shape, and no property or evidence file models it

Two mechanisms convert or erase silent divergence *before* the quiesced checksum runs,
and neither is discussed anywhere in the catalog:

1. **ROW full-image self-healing.** With `binlog_format=ROW` and default
   `binlog_row_image=FULL`, every UPDATE writeset carries the complete after-image and
   the applier locates rows by PK. A silently divergent **non-key column** on one node
   is silently *overwritten back into agreement* by the next UPDATE that touches the
   row. Divergence persists to checksum time only on rows never rewritten after the
   divergence event.
2. **Traffic-induced vote conversion.** A divergence in **row existence or key columns**
   (missing row, extra row, different PK) converts the next touching writeset into an
   apply error (key-not-found / duplicate-key) on the divergent node → inconsistency
   vote → eviction. The workload's own probe traffic (ack journal, clustercheck probe
   writes, FC watchdog commits) is precisely such touching traffic. Net effect: hot-row
   divergences surface as *evictions* attributed to whatever property owns
   cluster-size checks, and the checksum `Always` — which is scoped to "Synced
   primary-component nodes" — never sees the divergent state because the divergent node
   is no longer Synced.

Consequences for the catalog:

- `cross-node-row-equality`'s yield estimate is overstated for hot-row mixes and
  understated for append-only mixes. The workload should include **write-once witness
  tables** (rows inserted, never updated) so divergence persists to checkpoint, plus
  high-churn tables to drive the vote path — these are different oracles for the same
  bug class and should be named as such.
- An unexplained inconsistency vote (eviction with no injected sabotage) is *itself
  divergence evidence* and should feed `cross-node-row-equality` triage; today it would
  be filed under `inconsistency-vote-evicts-divergent-minority`, whose Always only
  checks that the *sabotaged* set left. A vote eviction in a run with no sabotage
  currently has no property that fails. Add a marker: `Sometimes`/alarm on "vote round
  occurred with zero injected divergence" — that is a silent-divergence detection, the
  single most valuable event the harness can produce.
- The divergent node's data should be **checksummed while evicted, before rejoin** —
  the catalog already notes that under `force_sst_after_inconsistency=OFF` the evicted
  node rejoins via IST "with its divergent SE position", but no property captures the
  evicted-state snapshot that would prove *which* node held the good data (the vote's
  central correctness question).

### M2 (multi-property, HIGH): The catalog's own trigger workloads violate its own oracles — sabotage and sentinel configs are run-wide poisons with no scoping rules

Two properties require the harness to deliberately break invariants that other
properties assert:

1. `inconsistency-vote-evicts-divergent-minority`: "workload injects deterministic
   single-node divergence" (presumably `wsrep_on=OFF` local writes). That injected
   divergence **is a violation of `cross-node-row-equality`** and of the vote-free
   cluster-size clauses in `privilege-context-divergence-never-evicts` /
   `applier-threads-never-read-only`. Nothing in the catalog or relationships doc says
   how these coexist: separate variant? sabotage-scoped tables excluded from checksums
   (like the mysql.user exclusion)? time-windowed suppression? Without an explicit
   scoping rule, the first sabotage run generates a "terminal oracle" finding that is
   actually the harness working as designed — a credibility-burning first triage.
2. `cluster-member-strings-never-reach-shell`: the sentinel node runs with a
   metacharacter-laden `wsrep_node_name` for the whole run. But the CVE fixes under test
   are **allowlist rejections** — the same name will be *rejected* at the
   `wsrep_notify.cc:83-90` / `wsrep_sst.cc:1696-1707` gates, which plausibly degrades
   or fails that node's SST/notify participation for the entire run, contaminating every
   liveness property that counts on 3 healthy nodes. The property's own `Sometimes` on
   the rejection paths confirms the rejections will be firing constantly.

**Suggested action**: Add a catalog-wide "poison budget" rule: any property whose
trigger deliberately violates another property's invariant must name its scoping
mechanism (dedicated variant, excluded table set, or suppression window) in its
invariant row. Right now zero of them do.

### M3 (harness-integrity, MEDIUM): The workload's own recovery levers can cause the flagship split-brain finding

The topology hands the workload `SET GLOBAL wsrep_provider_options='pc.bootstrap=true'`
as a recovery lever. The SUT analysis itself notes (§6.4) that manual pc.bootstrap on
both halves produces divergence "impossible to re-merge" — i.e., an unguarded workload
recovery action can *manufacture* an `at-most-one-primary-component` violation that is
operator error, not a SUT bug. Same class: driving `pc.ignore_sb` / a second
`--wsrep-new-cluster` from the supervisor. The catalog never states the guard.

**Suggested action**: codify a harness rule — pc.bootstrap only when (a) no node
anywhere reports Primary, (b) issued to exactly one node, (c) recorded in the run log so
any subsequent dual-primary observation can be attributed. Cheap, and it protects the
highest-severity property from self-inflicted false positives.

### M4 (catalog-wide, MEDIUM): Field-restart-policy shadow accounting and time-to-X are absent as property classes

The harness supervisor restarts on every exit — a recorded divergence from shipped
systemd (`RestartPreventExitStatus=SIGABRT` + exit 1). But recording the divergence per
run is weaker than *measuring* it: every property that "passes" only because the
supervisor restarted a suicided node is masking a field outage. Two cheap catalog-wide
additions:

1. **Shadow field-policy classifier**: the supervisor already sees every exit
   status/signal; tag each restart with "field systemd would NOT have restarted this"
   and emit a `Sometimes` per abort class. A liveness property that recovered only via
   a field-forbidden restart is a *different finding* (permanent node loss in the field)
   than one that recovered via an allowed restart — today triage cannot tell them apart
   without log spelunking. The bounded-consecutive-same-cause-abort check exists only
   inside `failed-state-transfer-node-rejoins`; generalize it to all abort classes
   (crash-loop detector).
2. **Time-to-detection/recovery buckets**: the catalog is purely binary. For the
   product whose only self-defense is voting, "how many writesets does divergence
   survive before a vote fires" and "how long from partition heal to first committed
   write" are field-relevant distributions that Antithesis Sometimes markers can bucket
   at near-zero cost (e.g. `Sometimes(vote within 100 writesets of injected
   divergence)`). This also converts the many uncalibrated liveness bounds (F1) from
   pass/fail cliffs into measured curves during the calibration phase.

### M5 (catalog-wide, MEDIUM): The CI-suppressed-warning list is a ready-made oracle nobody claimed

sut-analysis §9.5 inventories `mysql-test/include/mtr_warnings.sql:372-455`: Percona's
own CI globally suppresses "Gap in state sequence. Need state transfer.", "Quorum: No
node with complete state", "Failed to report last committed", "Ignoring possible
split-brain", "install timer expired", "Query apply failed", etc. — and explicitly
labels them "ready-made Antithesis assertion candidates — CI cannot see them by
design." The catalog uses log-scanning as a *fallback* for a few specific properties but
never systematizes this: a single shared log-denylist property ("none of these ~12
suppressed degraded-state lines appear outside a fault window" — or at minimum a
`Sometimes` per line for exploration signal) converts the entire suppression list into
coverage at the cost of one log scanner the harness needs anyway (several properties
already assume log-grep fallbacks). This is the cheapest unclaimed idea in the
evidence base.

### M6 (multi-property, MEDIUM): GU_DBUG_SYNC — the tree's release-usable precision fault injector — is inventoried in the SUT analysis and used by zero properties

sut-analysis §10.3 documents that Galera's GU_DBUG_SYNC points are settable **in release
builds** via `SET GLOBAL wsrep_provider_options='dbug=d,<point>'`, lists ~30 points, and
notes that the state-transfer/view-change race sites (`process_primary_configuration`,
`after_shift_to_joining`, `recv_IST_after_apply_trx`, `recv_IST_after_conf_change`,
`sst_received_decrease_state_seqno`, `ist_sender_send_after_get_buffers`) are "never
used by any MTR test." The catalog's hardest-to-time properties are exactly the ones
these points control:

- `gcache-recovered-ist-completeness` / donor-purge-between-selection-and-service (the
  MDEV-36621 shape): `ist_sender_send_after_get_buffers` freezes the donor mid-IST at
  will, with default network faults doing the rest.
- `restarted-node-rejoins-synced`'s kill-during-IST sequences:
  `recv_IST_after_apply_trx` gives a deterministic mid-IST pause to kill into.
- The sst_mutex_ release race (`replicator_str.cpp:1169-1180`):
  `before/after_send_state_request` brackets it exactly.

Using these is pure workload code (SQL SET against the target node) — no build changes,
no SDK. That several properties' Antithesis Angles worry about "hard to reach" windows
while the SUT analysis inventories a knob that opens those windows suggests the
catalog synthesis dropped the §10.3 inventory. One EXECUTE-type point
(`sst_received_decrease_state_seqno`) is even a built-in state corruptor.

**Suggested action**: add a "provider sync points" subsection to the workload plan;
annotate the 4-5 properties above with the specific point that manufactures their
precondition. This materially raises the explorability of the SST/IST cluster — the
area both the SUT analysis and bug history rank #1-#3 — without touching Implementability's
instrumentation budget.

### M7 (single-property, LOW): The X-protocol leg of `autoinc-identity-no-cross-node-collision` is unreachable in the planned harness

The property names the X-plugin document-id aggregator's configure-once cache as "the
widest window," but the topology's workload is a Python SQL client on 3306; nothing
speaks X protocol (33060), so that leg can never be exercised. Either add a mysqlx
session to the workload or strike the leg from the property so its non-firing isn't
mistaken for safety.

---

## 3. Cross-cutting the lenses

### C1 (catalog-wide, HIGH): "SUT-side (missing)" is deferred property-by-property; the systematic decision the catalog never makes

By count, roughly 20 properties carry an invariant clause tagged "SUT-side ... missing"
— Unreachables at abort funnels (`no-mdl-bf-bf-abort`, `trx-replay-never-fatal`,
`monitor-window-overflow-unreachable`, `async-monitor-leave-mismatch-unreachable`,
`vote-message-payload-contract`, `gcs-total-order-gap-free`,
`homogeneous-cert-version-match`, `no-spurious-multi-major-detection`, ...), Always at
checkpoint writers (`wsrep-xid-checkpoint-monotonic`,
`commit-cut-bounded-by-delivered-seqno`), Sometimes coverage probes at suppression
sites. Each property individually gestures at a fallback (log-grep, exit-status
detector, status-variable proxy), and the topology defers all C/C++ SDK work to "a later
phase" — but no document decides the question once: **is submodule/server SDK
instrumentation in scope for this engagement, and if so, when?**

The cross-lens tension: Fit will rate several of these as the highest-uniqueness
properties (passive tripwires that convert all fault load into checks);
Implementability will note that patching mysqld + two vendored submodules + rebuilding
is a real maintenance surface across rebases. The catalog resolves this nowhere, so each
property's "sharp form" is indefinitely aspirational while its text presents the sharp
form as *the* property.

**Suggested reformulation** (systematic, not per-property):

- **v1**: one shared log-scan assertion layer. Nearly every "missing Unreachable" in the
  catalog sits immediately after a WSREP_ERROR/WSREP_FATAL/INFO log emission that the
  evidence files already quote ("MDL BF-BF conflict", "Slave queue grew too long",
  "unallowed state transition", the mtr_warnings list from M5). Enumerate the exact
  strings once, in one file, with one scanner emitting per-string SDK events from the
  workload container. This gets ~80% of the tripwire value at ~5% of the cost and
  unifies M5.
- **v2**: one SDK patchset, built as a single reviewed commit across
  mysqld/galera/wsrep-lib (the catalog already confirmed wsrep-lib is vendored and
  compiled via ADD_SUBDIRECTORY), targeted only at sites where log lines are absent or
  post-crash-unreliable (the raw SIGSEGV zombie subclass in
  `fatal-node-terminates-no-zombie`, the strlen(NULL) precondition in the vote handler).

Decide it once; annotate every affected property with its tier.

### C2 (multi-property, MEDIUM): The node-termination fallback is load-bearing and hand-waved

Catalog open question #1 says if the platform disables kill faults, "the workload must
drive kills/restarts itself (docker stop / in-container kill)." Inside an Antithesis
compose run the workload container can do neither directly: it has no docker socket and
no cross-container process access. The actual mechanisms available are (a) tenant-side
enablement of termination faults (topology open Q4 — a request, not a plan), (b) SQL
`SHUTDOWN` (graceful only — explicitly *not* sufficient for the crash-recovery
category, whose whole point is unclean death), (c) a purpose-built kill channel: e.g., a
supervisor in each node container watching a flag file the workload can set via SQL
(`SELECT ... INTO OUTFILE` into a watched path) or a tiny TCP endpoint. Option (c) is a
real piece of harness engineering that all of category 2 (7 properties), half of
category 3, and top-10 items #3 and #6 depend on — and it appears in no document. Fit
will score those properties high; Implementability may not notice the fallback is
fictional as written because the catalog asserts it exists.

**Suggested action**: design the kill channel now (the entrypoint supervisor is already
being written — a `kill -9` flag-file watcher is ~10 lines) and note which properties
degrade vs. disappear if tenant termination faults stay off.

### C3 (oracle-dependency, MEDIUM): The checksum oracle depends on the correctness of a property under test, and the triage ordering is unstated

The catalog resolved the barrier question by standing the quiesced checksum on
`wsrep_sync_wait` (apply-monitor wait), and honestly notes this holds "modulo
monitor-implementation bugs, which `sync-wait-reads-observe-acked-writes` exists to
catch." The unstated consequence: a sync-wait bug produces **false
`cross-node-row-equality` failures** (checksum read stale state), and a triager who
starts from the terminal oracle will chase phantom divergence. The relationships doc
maps dominance (specific → terminal) but not this reverse dependency (oracle →
property-under-test).

**Suggested action**: one line in the relationships doc and in the checksum checker
itself: on row-equality failure, re-read the disagreeing node after a second barrier +
a delay; if the second read agrees, file against `sync-wait`, not row-equality. Also
applies to `wsrep_last_committed`-equality preconditions.

### C4 (catalog-wide, MEDIUM): Watchdog windows vs. exploration horizon — a test-composition constraint no lens owns

The liveness properties carry windows of ~90s (partition heal), minutes (FC freeze,
drain bounds), up to ~10min (SST completion). Antithesis explores branching histories
where wall-clock is expensive; a property whose violation requires a 10-minute quiet
observation window after the interesting fault will rarely be *confirmed* within a
branch even when the wedge is real, and worse, watchdog timers that keep running
*during* fault injection will fire on legitimately-stalled states (the catalog knows
this for FC — the view-change self-heal caveat — but the general rule is unstated).
The composition rule the catalog needs: fault windows early, bounded quiesce phase
late, watchdogs armed only in the quiesce phase, with per-property "fault-active"
suppression documented. The checksum quiesce and the commit-progress watchdogs also
mutually interfere (quiescing the workload for a checksum stops the very probe writes
the FC watchdog counts) — the orchestrator that sequences oracle phases is shared
infrastructure nobody has specced.

### C5 (single-property, LOW): `clustercheck-200-implies-write-progress` reimplements a script the harness doesn't run — pin the equivalence

The topology (correctly) doesn't expose port 9200 and has the workload run "the
clustercheck *query* itself." The property's finding value depends on the
reimplementation matching `clustercheck.sh:83-93` exactly (including
`AVAILABLE_WHEN_DONOR` and `pxc_maint_mode` handling and the pyclustercheck
divergences the SUT analysis found). A one-time golden test of the reimplementation
against the real script inside a node container closes the gap; otherwise a "200 lies"
finding invites the rebuttal "your 200 isn't our 200."

---

## 4. Oddities (fit no category)

- **O1 — The topology doc is stale against the catalog and will misdirect the harness
  build.** deployment-topology.md (04:48) still treats the recovery log-grep mismatch as
  a live suspected bug ("suspected pattern-mismatch bug", open Q2) and cites it as a
  *rationale* for the supervisor design; property-catalog.md (06:06) resolved it —
  the broken-grep scripts are vestigial/unshipped and the shipped flow greps the working
  bracketed form. Similarly the build-order contradiction (F1). Whoever implements from
  the topology doc re-litigates settled questions or builds the wrong v1 image. The
  topology doc needs one reconciliation pass against the final catalog.
- **O2 — Probe traffic perturbs the IST/SST boundary the harness was tuned to reach.**
  gcache.size=16M was chosen so IST/SST boundaries are reachable "under normal workload
  volume" (topology). The oracle traffic itself (ack journal, watchdog probes,
  clustercheck probes, checksum reads' sync-wait writes) adds replication volume that
  advances gcache purge — shrinking donor IST windows and shifting the empirically-tuned
  IST-vs-SST mix. Not a defect, but the gcache tuning run (topology open Q6) must be done
  *with the full oracle suite running*, or the tuning is for a workload that won't exist.
- **O3 — No field-default control variant.** Every run has `wsrep_applier_threads=4`
  (field default 1) and gcache 16M (default 128M) — justified for bug yield, but it
  means zero coverage of the configuration most customers actually run, and any
  found-bug's field impact statement inherits an asterisk. One periodic all-defaults
  variant (default appliers, default gcache, TLS ON) is cheap insurance and doubles as
  the negative control for oracle calibration (`cert-interval-reject-symmetry` already
  wants a calibration control).
- **O4 — `wsrep_certification_rules` inertness is a doc-vs-code finding worth filing
  upstream, independent of testing.** The catalog validated the sysvar has zero readers
  in 8.4.10 (Open Questions). That's a customer-reportable documentation/product defect
  discovered by the research itself; it shouldn't stay buried in a testing scratchbook.
  Same for the evs.max_install_timeouts doc/code discrepancy (3 vs 1) already noted in
  sut-analysis §14.5.
- **O5 — The 59-property count is honest** (verified against the evidence directory: 59
  slug files, all referenced in the catalog; five merges are disclosed with provenance).
  Also worth stating as a *pass*: the catalog demotes its own findings when evidence
  dissolves them (`crash-recovery-grep-yields-true-position` High→Low with the rationale
  withdrawn in print, `local-monitor-freed-after-bf-abort` "possible live bug"
  withdrawn) — unusual and valuable epistemic hygiene; the demotions survived into the
  relationships doc consistently.

---

## 5. What survived adversarial reading (explicit passes)

- The **external checksum oracle is genuinely mandatory** — the voting-sees-only-errors
  argument (sut-analysis §6.3, open Q35) held up under attack; M1 refines its
  sensitivity model but does not weaken the necessity claim.
- The **race-free ack-journal formulation** of `non-primary-rejects-writes` (assert on
  post-convergence presence, not on instantaneous state) is the right shape for
  Antithesis and survives the readiness-flag-lag objection the catalog itself raised
  and resolved.
- **mysql.user / SST-user checksum exclusions** show the ensemble already understood
  per-node-legitimate divergence — the same mechanism M2 asks to be generalized.
- The **known-issue guards** (PXC-4665 won't-fix deadlock, XA `commit_by_xid` stub
  exclusion) preempt the two most likely false-finding generators in the replay/GTID
  space.
- The **Sometimes-vacuity discipline** is applied essentially everywhere it matters;
  F2's complaint is about auditing them per-variant, not about their absence.
- The **dominance map** (relationships doc) correctly prevents double-counting between
  the terminal oracle and its attributing specialists, and the "keep all three
  lineage properties, they trigger at different times" reasoning is sound.

## 6. Uncertainties

- **M1's ROW self-heal claim** rests on default `binlog_row_image=FULL` and PK-based row
  lookup in the wsrep applier; if PXC's applier path additionally compares full
  before-images (it should not, with PKs present), the self-heal window narrows and
  divergences convert to votes even faster — which *strengthens* the
  vote-as-divergence-evidence recommendation and weakens only the witness-table urgency.
  Verify with one deliberate divergence experiment in the first harness smoke.
- **C2's claim that tenant termination faults are off** is topology Assumption 5, not a
  confirmed tenant config; if they are on, C2 reduces to "document it."
- Whether GU_DBUG_SYNC `dbug=` option-string survives in the PXC fork's release build
  exactly as sut-analysis §10.3 describes (dispatch verified at
  `replicator_smm_params.cpp:162-173`, but I did not independently re-verify the release
  ifdef guards) — one SET statement in the smoke test answers it.
- The instrumentation-tier counts in C1 (~20 properties) are my tally from invariant
  rows; a maintainer recount during the proposed field-addition would firm them up.
- F3's `gtid_executed`-equality property assumes log-bin/GTIDs are enabled in the
  harness config; the topology my.cnf sketch does not set `gtid_mode`/`log_bin`
  explicitly (8.4 has binlog on by default, GTID mode off by default) — the property
  needs `gtid_mode=ON`, which is itself the common field posture but is a config
  decision the topology hasn't made. Flag at harness build.
