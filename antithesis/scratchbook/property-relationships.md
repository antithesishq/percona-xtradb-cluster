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

# Property Relationships — PXC 8.4.10

Lightweight map of clusters that share evidence, code paths, or failure mechanisms, plus
suspected dominance relations noticed during synthesis. Every slug exists in
`property-catalog.md`.

## Dominance relations (one property implying/absorbing another)

- **`cross-node-row-equality` is the terminal oracle.** Any silent divergence produced by
  `bf-bf-lock-suppression-no-divergence`, `cert-interval-reject-symmetry`,
  `ist-overlap-writesets-not-reapplied`, `sr-fragment-cross-node-agreement`,
  `sr-rollback-fragment-noop`, `autoinc-identity-no-cross-node-collision`,
  `privilege-context-divergence-never-evicts`, `applier-threads-never-read-only`,
  `retry-autocommit-exactly-once`, or `bf-replay-commits-exactly-once` ultimately fails it.
  The specific properties exist because they detect earlier, attribute causes, and steer
  exploration — not because the terminal check misses the end state. Implement the shared
  checksum infrastructure once; give each property its own assertion message.
- **`gtid-executed-cluster-convergence` (gap-fill) is a SIBLING terminal oracle to
  `cross-node-row-equality` — neither dominates the other.** GTID sets are replication
  metadata: two nodes can hold identical rows with diverged `gtid_executed` (leaked local
  GTID, skipped gno), and identical GTID sets prove nothing about row content. The row
  checksum is blind to the GTID plane and vice versa. They share the same quiesced
  checkpoint / `wsrep_sync_wait` barrier pass — implement the GTID equality check as one
  more query inside the checksum pass.
- **`monitor-window-overflow-unreachable` is a layered (last-resort) oracle**: it fires
  only when the layers above it fail — `flow-control-pause-releases` (FC wedge),
  `synced-node-recv-queue-bounded` (queue bound), `local-monitor-freed-after-bf-abort` /
  `commit-order-monitor-released-no-cluster-stall` (leaked slots freeze `last_left_`).
  Overflow reached = at least one of those already violated.
- **`full-cluster-restart-reaches-primary`'s single-UUID clause subsumes the runtime check
  of `no-dual-bootstrap-after-full-shutdown`**, and both are dominated long-term by
  `cluster-identity-single-lineage` (sequential forks invisible to instantaneous checks).
  Keep all three: they trigger at different times with different fault requirements.
- **`fatal-node-terminates-no-zombie` protects every other property's oracle**: a zombie
  node corrupts up/down bookkeeping for all liveness watchdogs and quiesced checkpoints.
  Treat as infrastructure-priority even though it is "just" one safety property.
- **`non-primary-rejects-writes` and `acked-commit-durable-across-restart` share one ack
  journal**; the first dominates the second in fault-free-restart runs, the second extends
  the same journal across kill/restart. `first-committer-wins-loser-leaves-no-trace`
  extends the same journal with conflict-outcome semantics (loser/winner/unknown buckets).
- **`trx-replay-never-fatal` and `bf-replay-commits-exactly-once`** are two halves of one
  mechanism (termination vs effects); the `Unreachable` at the fatal funnel lives only in
  the former.
- **`grastate-se-checkpoint-agreement` dominates the workload proxy of
  `wsrep-xid-checkpoint-monotonic`** (a regressed XID usually surfaces as a two-file
  disagreement at next start), but the SUT-side monotonic form catches regressions that
  never reach a restart.

## Cluster: grastate/XID recovery (crash-recovery position agreement)

`grastate-se-checkpoint-agreement`, `wsrep-xid-checkpoint-monotonic`,
`crash-recovery-grep-yields-true-position`, `interrupted-sst-forces-full-sst`,
`gcache-crash-recovery-no-abort`, `cluster-identity-single-lineage`,
`ftwrl-backup-quiescent-or-fails` (gap-fill), `toi-nbo-ddl-completes-or-fails-cleanly`
(gap-fill)

Shared code: `saved_state.cpp` (warnings-only, non-atomic writer — single point of failure
for four of these), `replicator_smm.cpp:266-286` trust arbitration, `trx0sys.cc` XID
checkpoint, shipped wrapper scripts (`mysqld_safe.sh`, `build-ps/*/mysql-systemd`,
`mysql@bootstrap.service` + its persistent EnvironmentFile — the unbracketed-grep scripts
`mysqld_pre_systemd.in`/`mysql-helpers` and `mysqld_bootstrap.in` turned out unshipped).
Shared fault: node termination — in v1 supplied by the workload→supervisor kill channel
(evaluation synthesis; see deployment-topology.md). Shared harness prerequisite: the
startup probe / recovery-dance wrapper (now a supervisor extension emitting JSONL) —
build it once, all five properties evaluate inside it. Synthesis phasing:
`crash-recovery-grep-yields-true-position` and `cluster-identity-single-lineage` are
deferred to a field-faithful supervisor variant (the v1 supervisor replaces the shipped
wrapper surface they target). The
wrapper log-grep question is RESOLVED (the shipped `mysql-systemd galera-recovery` flow
greps the bracketed form that actually prints), which weakened
`crash-recovery-grep-yields-true-position` to a drift guard (priority lowered to Low);
`grastate-se-checkpoint-agreement` gains the new pause()-window kill recipe instead.
Gap-fill additions: `ftwrl-backup-quiescent-or-fails` joins because the provider pause
stamps a real seqno into grastate MID-RUN (`replicator_smm.cpp:3409-3410`) — a backup
workload actor widens the grastate-vs-SE kill window this cluster targets; and
`toi-nbo-ddl-completes-or-fails-cleanly` joins via the NBO `unsafe_`-counter leak, which
writes on-disk UNDEFINED:-1 (`mark_unsafe`) — invisible to
`grastate-se-checkpoint-agreement` (seqno=-1 is a legal branch there) and evaluable only
against the DDL episode ledger (supervisor extension).

## Cluster: monitor-release stall (bug pattern C)

`local-monitor-freed-after-bf-abort`, `commit-order-monitor-released-no-cluster-stall`,
`monitor-window-overflow-unreachable`, `flow-control-pause-releases`,
`synced-node-recv-queue-bounded`, `bf-abort-skip-awake-victim-already-killed`,
`notify-cmd-hang-does-not-block-commits`, `graceful-shutdown-bounded`,
`desync-ftwrl-composition-resyncs` (gap-fill)

Shared mechanism: strict LocalOrder/CommitOrder monitors (one hole halts the pipeline) +
flow control as the only backpressure. All present the same external symptom — healthy
status, zero commits — so they share one commit-progress watchdog with per-property
`Sometimes` markers and distinct trigger shapes, plus the PXC-only `wsrep_monitor_status
(L/A/C)` status variable as the shared per-monitor leak gauge (no SUT instrumentation
needed). `graceful-shutdown-bounded` joins via
`wsrep_wait_appliers_close`'s unbounded loop (a wedged monitor makes shutdown eternal).
`desync-ftwrl-composition-resyncs` joins as the operator-composition member: FTWRL/desync
drive the same pause/monitor machinery from SQL (untimed `pause()` drain, the
non-consecutive-seqno local-monitor gap wedge, `resume_and_resync` swallow), and a
permanently desynced node is the designed-FC-off precondition of
`monitor-window-overflow-unreachable`'s amplifier.
Distinguishing failures at triage requires the per-shape markers, not the watchdog alone.

## Cluster: certification determinism (silent divergence generators)

`cross-node-row-equality`, `bf-bf-lock-suppression-no-divergence`, `no-mdl-bf-bf-abort`,
`cert-interval-reject-symmetry`, `homogeneous-cert-version-match`,
`privilege-context-divergence-never-evicts`, `applier-threads-never-read-only`,
`inconsistency-vote-evicts-divergent-minority`, `vote-message-payload-contract`,
`evicted-node-rejoins-only-via-sst` (gap-fill)

Gap-fill addition: `evicted-node-rejoins-only-via-sst` **closes the rejoin loop** this
cluster leaves open — both `inconsistency-vote-evicts-divergent-minority` and
`privilege-context-divergence-never-evicts` flag "a vote-evicted node rejoins via IST
with its divergent SE position under default `force_sst_after_inconsistency=OFF`" as the
downstream risk; the new property owns that contract (every `mark_corrupt` path ⇒ next
Synced only via full SST), sharing the sabotage-fenced eviction workload and the
checksum-evicted-nodes-before-rejoin oracle guidance.

Shared root cause family: per-node inputs to a supposedly deterministic decision (cert
keys vs MDL footprints; unvalidated cert params; privilege context; read-only context;
vote-code recompute). `no-mdl-bf-bf-abort` and `bf-bf-lock-suppression-no-divergence` are
the *same root cause* (missed cert keys) observed through different layers — crash vs
silent. (Synthesis: `cert-interval-reject-symmetry` is reframed as a one-shot
negative-control calibration of the checksum oracle, not a standing member; the sabotage
legs of the vote/privilege properties run only in fenced variants per the poison-budget
convention in property-catalog.md.) The two vote properties share the confirmed empty-vote-collision finding; a
failure in either undermines the SUT's only divergence responder, raising the stakes of
the external checksum oracle. Shared config: `wsrep_applier_threads > 1`. Shared workload:
the DDL/FK/trigger statement generator target list (open question in
`no-mdl-bf-bf-abort`).

## Cluster: replay/BF-abort machinery

`trx-replay-never-fatal`, `bf-replay-commits-exactly-once`,
`local-monitor-freed-after-bf-abort`, `retry-autocommit-exactly-once`,
`first-committer-wins-loser-leaves-no-trace`, `no-mdl-bf-bf-abort` (residual unlocked
mode-read race into the second MDL funnel — the torn `wsrep_thd_order_before` channel
was invalidated), `skip-locked-nowait-never-fatal` (gap-fill — RIDER: the BF-conflict
workload here already generates the HP applier lock pressure it needs; its
ER_LOCK_DEADLOCK-from-SKIP-LOCKED `Sometimes` is a free zero-instrumentation marker that
the wsrep BF-wait conversion fired)

Shared code: wsrep-lib `transaction.cpp` 12-branch certify switch and unlocked provider
windows; `replicator_smm.cpp` replay + `handle_local_monitor_interrupted`;
`wsrep_high_priority_service.cc` replayer dtor funnel. One high-conflict workload
(hot rows + UK churn + `retry_autocommit=0` variant) feeds all six. The "unknown outcome"
bucket definition in `first-committer-wins-loser-leaves-no-trace` and the replay-status
enumeration in `trx-replay-never-fatal` are the same investigation.

## Cluster: SST script layer

`sst-ready-message-implies-listener`, `sst-grant-all-user-locked-or-absent`,
`cluster-member-strings-never-reach-shell`, `interrupted-sst-forces-full-sst`,
`failed-state-transfer-node-rejoins`, `fatal-node-terminates-no-zombie`

Shared mechanism: shell scripts coordinating via stdout lines, with no PDEATHSIG, a
non-exiting SIGTERM trap, port squatting, and kill windows around credential and grastate
manipulation. `fatal-node-terminates-no-zombie` joins via the verified
`LOCK_wsrep_sst` self-deadlock — fatal signals *during SST* are the crash-dense case.
Kill-timing during SST post-processing is the shared trigger; one composer action
(kill node mid-SST at varied offsets) exercises the whole cluster.

## Cluster: state-transfer lifecycle liveness

`restarted-node-rejoins-synced`, `joiner-reaches-synced-after-state-transfer`,
`donor-returns-to-synced`, `failed-state-transfer-node-rejoins`,
`full-cluster-restart-reaches-primary`, `partition-heal-single-primary-remerge`,
`cluster-address-live-set-rejoins` (gap-fill — RIDER on
`restarted-node-rejoins-synced`: `SET GLOBAL wsrep_cluster_address` is a third trigger
for the same leave→rejoin cycle, needing no kill channel and no process restart; own
slug/messages for its SET-hang / double-SET / false-success failure modes)

Chain structure: partition heals (`partition-heal-single-primary-remerge`) → node rejoins
via IST/SST (`restarted-node-rejoins-synced`, retry loop covered by
`failed-state-transfer-node-rejoins`) → JOINED drains (`joiner-reaches-synced-after-state-transfer`)
→ donor recovers (`donor-returns-to-synced`); `full-cluster-restart-reaches-primary` is
the all-nodes-down degenerate case. They share the quiesced "everyone Synced, one UUID"
end-state check and differ in which transition's `Sometimes` markers they own — implement
the markers per property, the end-state check once.

## Cluster: IST correctness (data path)

`ist-overlap-writesets-not-reapplied`, `gcache-recovered-ist-completeness`,
`gcache-crash-recovery-no-abort`, `gcache-page-files-bounded`,
`commit-cut-bounded-by-delivered-seqno`

Shared substrate: gcache bookkeeping decides what IST can serve; the commit cut decides
what gcache purges; the overlap gate decides what the joiner re-applies. A violation of
`commit-cut-bounded-by-delivered-seqno` (over-purge) manifests downstream as
`gcache-recovered-ist-completeness` or `ist-overlap-writesets-not-reapplied` failures —
attribution requires the SUT-side commit-cut probe. PXC-4845/MDEV-36621 sit in this
cluster (both directions of the overlap gate).

## Cluster: health/observability truthfulness

`clustercheck-200-implies-write-progress`, `donor-returns-to-synced`,
`maint-mode-honors-operator-intent`, `nonready-node-error-code-contract`,
`non-primary-rejects-writes`, `illegal-wsrep-transition-never-taken`,
`fatal-node-terminates-no-zombie`

Shared theme: what the node *reports* vs what it *does*. The donor `wsrep_ready=ON`
fall-through is simultaneously the exception in `non-primary-rejects-writes`, the leak
signature in `donor-returns-to-synced`, and green-but-dead mechanism #2 in
`clustercheck-200-implies-write-progress`. `illegal-wsrep-transition-never-taken` is the
upstream guard: warn-and-proceed transitions are how the readiness flags get out of sync
in the first place. `maint-mode-honors-operator-intent` couples to clustercheck through
the DISABLED-required-for-200 rule — and now to partition faults: the forced-flip/revert
branches fire in the RELEASE image on every non-primary view (protocol -1 chain), so the
operator-drain hijack is a network-faults-only finding (priority raised to High).

## Cluster: identity/lineage & bootstrap

`no-dual-bootstrap-after-full-shutdown`, `cluster-identity-single-lineage`,
`full-cluster-restart-reaches-primary`, `at-most-one-primary-component`,
`grastate-se-checkpoint-agreement` (torn-parse default-TRUE hazard)

Shared substrate: `safe_to_bootstrap` in grastate (set on every singleton view; parse
default TRUE on torn files), gvwstate/pc.recovery, the bootstrap wrapper env mutation.
Escalation ladder: `at-most-one-primary-component` (instantaneous split) →
`no-dual-bootstrap-after-full-shutdown` (restart-window split) →
`cluster-identity-single-lineage` (sequential fork). Same recorded-original-UUID
bookkeeping serves the last two.

## Cluster: SR (streaming replication) — config-gated variant

`sr-fragment-cross-node-agreement`, `sr-rollback-fragment-noop`,
`non-primary-rejects-writes` (SR-straddling-partition open question),
`trx-replay-never-fatal` (rollback-fragment-meets-replay open question)

All require `wsrep_trx_fragment_size > 0` — UN-GATED at evaluation synthesis: the
variable is session-settable (SESSION_VAR HINT_UPDATEABLE), so one dedicated workload
*phase* (not a server config variant) serves all; non-crash SR legs are v1-viable,
crash sub-cases remain kill-channel-gated.
Shared machinery: `wsrep_streaming_log` storage service, `close_orphaned_sr_transactions`,
the dummy-writeset demotion path (the demotion path is also the divergence mechanism in
`sr-rollback-fragment-noop`). The `#if 0`'d double-commit assert appears in both SR
evidence files.

## Cluster: async-replication topology — gated on topology decision

`duplicate-gtid-skip-exactly-once`, `async-monitor-leave-mismatch-unreachable`,
`applier-resize-converges` (async-monitor sizing hazard)

First two require an async source replicating into the cluster; same regression family
(PXC-4664/4688/4823 overlap). `applier-resize-converges` joins only in that topology (the
async monitor is sized by `opt_replica_parallel_workers`, not applier count — resizing
appliers above it is a known stall recipe).

## Cluster: version-gate tripwires (dormant machinery)

`no-spurious-multi-major-detection`, `rolling-upgrade-write-gate`,
`homogeneous-cert-version-match`, `maint-mode-honors-operator-intent`

Shared machinery: protocol-version detection and `wsrep_pxc_maint_mode_forced`. The
release image runs the two `Unreachable` tripwires — `no-spurious-multi-major-detection`
(now CONDITIONED on primary views: non-primary views carry protocol -1 by construction
and fire the forcing branch, covered by a companion `Sometimes`) and
`homogeneous-cert-version-match` — plus `maint-mode-honors-operator-intent`, whose
forced-flip/revert branches are release-reachable with plain network faults via the same
non-primary -1 chain. The debug image adds `rolling-upgrade-write-gate`'s write-block
half via the DBUG knob. Do not run `no-spurious-multi-major-detection` and
`rolling-upgrade-write-gate` in the same variant (the DBUG knob makes the former's
Unreachable trivially fire).
