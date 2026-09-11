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

# Antithesis-Fit Evaluation — Property Catalog (59 properties)

Lens: for each property, does verifying it require exploring a state space a
deterministic test cannot cover (timing races, partial failure, fault
interleavings)? Inverse lens: is any property's Antithesis value underestimated?
Evaluated against the **planned v1 environment** from
`antithesis/scratchbook/deployment-topology.md`: 3 PXC nodes + 1 Python workload
client, default network faults only, **no node-termination faults**, restarts
driven only by workload SQL `SHUTDOWN` + in-container supervisor,
**assert-enabled build first**, **zero SUT-side SDK instrumentation**
(existing-assertions.md; C/C++ libvoidstar explicitly "later phase"), no async
replica, no proxy, no `wsrep_notify_cmd` configured, xtrabackup-v2 only, no
clock faults, no rsync SST.

Deliberate bias: this document hunts problems. Passes are listed at the end.

---

## Catalog-wide findings

### CW-1. Roughly a third of the catalog is dead or near-vacuous in v1, and the catalog's own priority ordering does not reflect it

The catalog flags fault requirements inline (disciplined — credit where due),
but the "implement-first" ordering and the per-property priorities are
phase-blind. Cross-referencing every property against the v1 environment:

**Fully dead or near-vacuous in v1** (no meaningful evaluation possible):

| Property | v1-killing gate | Later value? |
|---|---|---|
| `acked-commit-durable-across-restart` | node termination | Yes — headline claim |
| `gcache-recovered-ist-completeness` | node termination (± disk) | Yes |
| `gcache-crash-recovery-no-abort` | node termination | Yes |
| `wsrep-xid-checkpoint-monotonic` | SUT instrumentation AND termination (proxy form) | Yes |
| `cluster-identity-single-lineage` | termination + a persistent-supervisor-env design v1 deliberately avoids (marker-file guard) | Yes, if harness adds env emulation |
| `sr-fragment-cross-node-agreement` | SR sessions AND termination | Yes |
| `interrupted-sst-forces-full-sst` | node termination (kill inside SST past no-way-back) | Yes |
| `sst-grant-all-user-locked-or-absent` | kill windows during SST post-processing / donation | Yes |
| `no-dual-bootstrap-after-full-shutdown` | unclean stops; v1 marker-file guard removes the harness-side vector entirely | Yes |
| `full-cluster-restart-reaches-primary` | ungraceful all-down (gvwstate deleted on graceful close — the pc.recovery path is unreachable via SQL SHUTDOWN) | Yes |
| `duplicate-gtid-skip-exactly-once` | async source topology | Yes (phase 2) |
| `async-monitor-leave-mismatch-unreachable` | async topology AND SUT instrumentation | Yes (phase 2) |
| `rolling-upgrade-write-gate` | DBUG knob — needs a full Debug build, which v1's preferred image does not provide (see CW-4) | Marginal |
| `vote-message-payload-contract` | SUT instrumentation; NULL-deref arm undrivable by ANY planned fault (see P-3) | Partial |
| `gcs-total-order-gap-free` | submodule SDK instrumentation | Yes (release image) |
| `commit-cut-bounded-by-delivered-seqno` | SUT instrumentation (the `Always` is "missing") | Yes (release image) |
| `bf-abort-skip-awake-victim-already-killed` | SUT instrumentation ("needs SUT-side instrumentation to observe") | Yes |
| `homogeneous-cert-version-match` | provider instrumentation | Yes (tripwire) |
| `notify-cmd-hang-does-not-block-commits` | `wsrep_notify_cmd` not configured in the v1 config sketch — dead by omission, though the fix costs one config line (see P-13) | Yes — should be v1 |

**Partially degraded in v1** (core alive, flagged arms dead):
`grastate-se-checkpoint-agreement` (probe runs every restart but every
interesting kill window — shutdown race, `pause()` window, post-IST blanking —
is unsteerable; graceful restarts should trivially agree),
`restarted-node-rejoins-synced` (kill-mid-IST and the PXC-4631
liveness→safety conversion dead; IST-rejoin-after-partition alive),
`failed-state-transfer-node-rejoins` (kill arms dead; IST-watchdog arm ALIVE —
see P-11), `sr-rollback-fragment-noop` (crash sub-case dead; rest alive via
session SR — see P-14), `ist-overlap-writesets-not-reapplied` (crash variant),
`commit-order-monitor-released-no-cluster-stall` (PXC-4845 variant),
`gcache-page-files-bounded` (orphan-across-restart half),
`local-monitor-freed-after-bf-abort` (kill-during-replay variant),
`sst-ready-message-implies-listener` (SUT `Unreachable` + orphan arm + rsync
arm dead; the workload timeout `Always` alive),
`crash-recovery-grep-yields-true-position` (the seqno=-1 `Sometimes` branch is
unreachable without unclean stops; the probe otherwise compares two strings the
same code produced — near-tautological under graceful-only restarts).

**Consequence for the top-10 implement-first list**: #3
(`acked-commit-durable-across-restart`) and #6
(`grastate-se-checkpoint-agreement`) are respectively dead and near-vacuous in
the environment the team plans to build first. An implementer following the
list will spend two of their first six slots on properties that cannot produce
signal until tenant faults change. **Suggested action**: annotate the top-10
(and each priority) with a v1/v-later phase tag, or re-order the top-10 for the
v1 fault set (candidates to promote: `flow-control-pause-releases`,
`maint-mode-honors-operator-intent`, `ist-overlap-writesets-not-reapplied` are
already high; `failed-state-transfer-node-rejoins` and
`graceful-shutdown-bounded` deserve promotion — see P-11/P-12).

### CW-2. The catalog's own fallback for missing termination faults is not realizable in the specified topology

The catalog's open question says: "If the platform config disables
[kill/restart faults], the workload must drive kills/restarts itself (docker
stop / in-container kill)". But the deployment topology gives the workload
container exactly one channel into the nodes: SQL on 3306. SQL cannot
`kill -9`; `SHUTDOWN` is graceful by design; the assert-enabled non-Debug build
has no DBUG crash keywords. Docker-level stop is not workload-drivable inside
an Antithesis run. As written, **the fallback does not exist**, and all of
Category 2 stays dead until tenant config changes.

**Suggested action** (cheap, standard): place test-command scripts on the
pxc-node containers themselves (`/opt/antithesis/test/v1/...` directories are
per-container, not workload-only) — e.g. an `anytime_kill9_mysqld.sh` that
`kill -9`s the local mysqld and lets the existing supervisor run the recovery
dance. This unlocks the entire crash-recovery category (top-10 #3 and #6, plus
8+ partially-gated properties) with zero tenant changes, and makes kill *timing*
steerable (kill-during-SST, kill-during-IST windows) in a way even platform
termination faults are not. The absence of this option from both catalog and
topology is the single highest-leverage gap in the whole plan.

### CW-3. One incidental crash-recovery channel does exist in v1 — and neither document accounts for it

SUT self-fatals are ungraceful deaths: the IST 10s SocketWatchdog abort
(reachable with default network faults, per `failed-state-transfer-node-rejoins`),
every `unireg_abort(1)` funnel, `gu_abort()`, and — in the v1 assert-enabled
build — every firing `assert()`. The supervisor restarts them. So grastate/
gcache/XID recovery code WILL execute in v1, but only *incidentally*, gated on
some other defect or designed abort firing first, at unsteerable points. Two
consequences the catalog should state: (a) crash-recovery properties are not
"off" in v1, they are conditional oracles that should still be armed (cheap
probes at every restart), and (b) any crash-recovery finding in v1 is
confounded with whatever abort caused it. **Suggested action**: arm the
restart-time probes (`grastate-se-checkpoint-agreement`,
`gcache-crash-recovery-no-abort` bounded-recovery check) in v1 as passive
checks; do not *drive* them until CW-2's kill channel exists.

### CW-4. The assert-enabled v1 build inverts the premises of several properties and partially duplicates the SUT-side instrumentation program

The catalog's structural thesis — "release builds compile out essentially all
internal invariants, so SDK assertions must re-add them" — is written against
the NDEBUG image. v1 is the assert-enabled image. Effects the catalog does not
work through:

- **Redundancy**: every "SUT-side `Unreachable`/`Always` (missing)" that
  shadows a native `assert()` (`monitor.hpp` internal consistency,
  `illegal-wsrep-transition-never-taken`'s matrices,
  `commit-cut-bounded-by-delivered-seqno`'s stripped asserts,
  `gcs-total-order-gap-free`'s gcomm_asserts) is already enforced in v1 by the
  live assert — as a crash, which Antithesis detects natively. The SDK versions
  add reporting quality and `Sometimes` coverage signal, not detection. Their
  cost/benefit belongs to the *release-image phase*, and the catalog should say
  so rather than presenting them as uniform prerequisites.
- **Premise inversion**: `illegal-wsrep-transition-never-taken` is specified
  around release behavior ("log and proceed anyway"); in v1 the violation
  branch aborts before the release-level log line exists. The property's log
  fallback is thus release-image-only; in v1 it reduces to "the process didn't
  assert-crash", which Antithesis already checks for free.
- **Behavioral contamination risk**: `inconsistency-vote-evicts-divergent-minority`
  and `privilege-context-divergence-never-evicts` depend on the *release-mode*
  divergence→apply-error→vote pipeline. In an assert build, deliberately
  injected divergence (`wsrep_on=OFF` local writes) may trip an assert on the
  divergent node *before* the vote round occurs, converting "vote evicts
  exactly the sabotaged set" into "node suicides" — a different mechanism than
  the property claims to test. Nothing in the catalog or topology addresses
  whether the vote machinery behaves identically under live asserts.
  **Suggested action**: smoke-test the divergence-injection workload on the
  assert build before trusting vote-property results; expect these two
  properties to give their intended signal only on the release image.
- **DBUG ≠ assert**: the catalog's dual-image assumption ("an assert-enabled
  (debug) image ... unlock[s] deterministic SR crash points and the DBUG
  multi-major knob") conflates two build axes. `DBUG`/`DEBUG_SYNC` are gated by
  `DBUG_OFF`/`WITH_DEBUG`, not by `NDEBUG`; the topology's preferred v1 flavor
  (RelWithDebInfo minus `-DNDEBUG`) yields live asserts but **no DBUG knobs**.
  Properties leaning on DBUG (`rolling-upgrade-write-gate`, the SR crash
  points, the multi-major amplifier) need a third image tier (full Debug) that
  is only the topology's fallback, not its plan. The catalog's "debug build"
  flags should be split into "assert build" vs "DBUG build".

### CW-5. Cross-property workload/assertion conflicts within a shared environment

The catalog resolves one such conflict (`no-spurious-multi-major-detection` vs
`rolling-upgrade-write-gate`: "do not run both in one variant") but misses at
least three others:

1. **`privilege-context-divergence-never-evicts` vs
   `inconsistency-vote-evicts-divergent-minority`**: the former installs an
   `Unreachable` at the vote-eviction paths (`replicator_smm.cpp:2411-2413`,
   `:626-637`); the latter's workload *deliberately drives vote evictions*
   ("workload injects deterministic single-node divergence"). Run together, the
   `Unreachable` fires on designed sabotage. It must be conditioned on the
   workload class (or scoped to runs without divergence injection).
2. **Divergence-injection workloads vs the terminal checksum oracle**:
   `cross-node-row-equality` is an unconditional `Always` at quiesced
   checkpoints, but the vote-eviction workload creates real cross-node
   divergence *by construction* until eviction resolves it. The checksum oracle
   needs node/table scoping rules for sabotage phases (the catalog specifies
   exclusions only for `mysql.user`/SST rows). Without this, the flagship
   oracle fails on self-inflicted state.
3. **NBO-driving workloads vs SST/IST liveness**: `applier-threads-never-read-only`
   (NBO DDL under read-only flips) and `no-mdl-bf-bf-abort` (NBO-wait
   `Sometimes`) put NBO in the ambient workload, but an in-flight NBO makes a
   concurrent join *deterministically* abort the joiner
   (`replicator_str.cpp:874-882`, catalog's own `failed-state-transfer` notes)
   and SST donation return -EAGAIN. Under partition faults + supervisor
   restarts, ambient NBO converts designed refusals into
   `restarted-node-rejoins-synced` / `failed-state-transfer` liveness failures
   or crash-loop classifications. NBO needs to be a fenced workload phase, not
   ambient — no property or assumption says this.

### CW-6. Predicted-failing `Always` assertions ship as day-one known issues

Two `Always` properties encode violations the ensemble has already verified
statically as unconditionally reachable:

- `nonready-node-error-code-contract`: "Verified: TOI when `!wsrep_ready`
  returns 1213" — the property will fail the first time DDL hits a non-ready
  node, deterministically. The catalog even supplies a deterministic driver
  (event-scheduler auto-drop).
- `maint-mode-honors-operator-intent`: the forced-flip/force-revert chain is
  "release-reachable with plain network faults" and "guaranteed reachable" per
  the companion marker — an operator drain set before any partition is erased
  at heal, every time.

Finding known bugs is the point of the exercise, but a permanently-red `Always`
is triage noise from run one and (worse) masks *new* regressions behind the
same assertion. **Suggested action**: pre-register both as expected findings
with carve-outs (assert the *rest* of the contract strictly; track the known
shape as a distinct `Sometimes`-style known-issue marker), and note that the
1213-when-unready half needs no Antithesis at all (see P-2).

### CW-7. The SUT-instrumentation dependency is understated as a program risk

12+ properties (all "SUT-side (missing)" invariants plus most coverage
`Sometimes` markers) require C/C++ SDK instrumentation of mysqld,
libgalera_smm.so, and two vendored submodules (wsrep-lib, galera) — while
existing-assertions.md records zero instrumentation and the topology defers
libvoidstar indefinitely ("later phase"). Several of these are the catalog's
*sharp* forms, with the workload form explicitly labeled "weaker"
(`wsrep-xid-checkpoint-monotonic`) or "fallback"
(`no-mdl-bf-bf-abort` log-grep, `monitor-window-overflow` log-grep). The
catalog should mark, per property, which fallback is good enough to ship v1 on
— by my reading: log-grep fallbacks are adequate for `no-mdl-bf-bf-abort`,
`trx-replay-never-fatal`, and `monitor-window-overflow` (all log-then-die
paths with flushed markers), inadequate for `illegal-wsrep-transition`
(debug-level message) and `bf-abort-skip-awake` (no log signal at all).

### CW-8. Search-budget composition: the workload carries almost every oracle

Because v1 has no SUT instrumentation, nearly all detection is workload polling
(checksums under `wsrep_sync_wait`, status-variable scans, ack journals,
`wsrep_monitor_status`, log greps). Quiesced checksum rounds *pause the
interesting concurrency* — each checkpoint drains the very interleavings the
faults create. The catalog never discusses checkpoint cadence as a
budget/exploration tradeoff (too frequent = the workload spends the run
quiescing; too rare = divergence found long after the causal moment, hurting
triage). This is a real design parameter for the flagship oracle, not a detail.

---

## Property-specific findings

### P-1. `cluster-member-strings-never-reach-shell` — integration-test territory, and the sentinel design fights itself

The "malicious member" is modeled as *static node config* (metacharacter-laden
`wsrep_node_name`). The input is fixed at container build time; the
allowlist validation it probes (`wsrep_notify.cc:83-90`, `wsrep_sst.cc:1696-1707`)
is deterministic string filtering with no timing or fault dimension. A single
integration test (start a node with the hostile name, observe rejection, assert
no side effect) covers it completely; running it as an ambient Antithesis
property re-evaluates the same fixed input every branch. Worse, a permanently
hostile node name either (a) gets rejected at every notify/SST invocation —
polluting every SST-related property's logs and possibly SST outcomes in every
run — or (b) requires a dedicated environment variant for one deterministic
check. CVE regression-guarding is legitimate, but it belongs in CI.
**Suggested action**: demote to a deterministic startup/integration test
outside the Antithesis budget; keep at most a passive log-grep for the sink
(`sh -c` argument logging) if nearly free.

### P-2. `nonready-node-error-code-contract` — the core claims are deterministic

All three verified mechanisms (TOI-when-unready→1213 at
`wsrep_mysqld.cc:3016-3024`; sync-wait-failure→1205 at `:1541`; the
event-scheduler driver) are fixed-input, fixed-output code paths already
confirmed by static reading. A deterministic test proves each in seconds.
Antithesis's residual value here is only the *cross-product* — error codes
observed across all node states the faults produce — which is genuinely
non-deterministic but is a thin layer over `non-primary-rejects-writes`'s
existing per-error bookkeeping. Combined with CW-6 (the `Always` is
known-failing), this property is mostly consuming catalog space that a CI test
plus one extra checker rule in the ack-journal infrastructure would cover.

### P-3. `vote-message-payload-contract` — the headline `Unreachable` cannot be driven by any fault in any planned environment

The catalog itself proves the point: "same-version senders always append ≥1
NUL byte ... the NULL-payload `Unreachable` fires only on transport truncation
or a foreign sender." The harness is homogeneous (no foreign sender), and
Galera transport is TCP with message checksums — network faults corrupt or drop
connections; they do not deliver truncated-but-accepted vote messages. The
`strlen(NULL)` SIGSEGV precondition is therefore unreachable under every
planned fault set, forever — a permanently-vacuous marker whose only cost is
instrumentation effort in the hardest-to-instrument layer (gcs submodule).
The empty-vote-collision leg (workload `Always` on survivor consistency) is the
real content and rides on the vote workload anyway. **Suggested action**: drop
the NULL-deref `Unreachable` from the plan (file it as a code-review finding —
it already is one); keep the survivor-consistency check inside
`inconsistency-vote-evicts-divergent-minority`.

### P-4. `autoinc-identity-no-cross-node-collision` — the live channels need faults v1 doesn't have; the rest is unit-testable

Channel (a), the torn (offset, increment) read, is an instruction-scale window:
an unlocked two-field read racing `log_view`'s locked rewrite. Without
thread-pausing faults (which require the deferred libvoidstar instrumentation),
hitting it depends on natural scheduler jitter aligning a per-statement refresh
with a view change — possible but poorly amplified; the property's `Sometimes`
("view change overlapped an in-flight identity insert") will report coverage of
the *statement* overlap, not the *torn-read* overlap, overstating what was
tested. Channel (c), the X-plugin document-id cache, requires an X-protocol
workload nobody else needs — an entire protocol surface added for one Medium
property. The offsets-pairwise-distinct leg is config arithmetic checkable
deterministically. **Suggested action**: keep the cheap `Always` legs riding on
the existing workload, but re-cost the priority: the differentiating channels
are effectively gated on thread-pausing instrumentation (flag it like the other
gates), and the X-plugin leg should be explicitly deferred or dropped.

### P-5. `monitor-window-overflow-unreachable` — explorability arithmetic likely doesn't fit a branch, and the "amplifier" targets designed behavior

The catalog's own calibration: a fully wedged node overflows in ~1-11 minutes
at 100-1000 writesets/s. A throttled 3-node Antithesis harness with a
256M buffer pool and a Python workload is unlikely to sustain the top of that
range; at 10-100 ws/s the window is 10 minutes to 2 hours of *sustained
one-node wedge with continuing cluster load* — plausibly beyond branch budgets,
making the `Unreachable` near-vacuous and the log fallback silent. Separately,
the proposed amplifier ("desync + tiny-transaction flood") drives a node that
legitimately sends no FC while desynced; if the amplifier succeeds, the
overflow give-up path fires as arguably *designed* terminal behavior
("Application must be restarted") — the property gives no rule for
distinguishing a monitor-release *bug* reaching overflow from a
deliberately-starved desynced node reaching it. **Suggested action**: keep the
log-grep as a free tripwire; drop the dedicated amplifier until the
FC/monitor-release layers above it have produced a wedge organically; define
the desync carve-out before any firing is triaged.

### P-6. `cert-interval-reject-symmetry` — the analysis already closed the channel it tests

The evidence chain concludes: join asymmetry "ruled out under uniform params"
via cut arithmetic + FIFO ordering + shared trim horizon, and runtime SET of
cert params is rejected — so in a uniform-config harness the verdict-split
mechanism has no known trigger. What remains is a negative-control calibration
of the checksum oracle (valuable, but that's one run, not a standing property)
plus a special `cert.max_length=512` variant to heat a branch whose asymmetry
was just argued impossible. Medium priority overstates this; it's a
calibration task plus a tripwire. **Suggested action**: run the heated variant
once as oracle calibration; do not maintain it as a standing environment
variant.

### P-7. `applier-resize-converges` — concurrency stress a deterministic harness could mostly cover

"No faults required at all"; concurrent SETs are serialized by the sysvar lock,
so the residual race is SET-vs-applier-exit — reachable by a plain loop test
(resize under load) in MTR-style CI, which the vendor plausibly lacks but which
doesn't need Antithesis's fault engine. The Antithesis increment is composition
with view changes and NBO-worker churn. Fine as a cheap rider on the ambient
workload; would be misallocated as anything more. Priority Medium is
defensible only because the harness's own `applier_threads=4` choice makes it
self-protective.

### P-8. `privilege-context-divergence-never-evicts` — half fuzzing, half Antithesis; the halves should be priced separately

The privilege-varied statement sweep is *input-space* exploration (one missing
privilege at a time × statement types) with no timing component: any given
(privilege, statement) asymmetry reproduces deterministically. That half is a
combinatorial integration test / fuzz harness. The genuinely Antithesis-shaped
half is the *consequence* pipeline: eviction → IST rejoin with divergent SE
position under `force_sst_after_inconsistency=OFF` → checksum oracle — which
needs faults and churn. High priority is defensible for the composed property,
but an implementer should know the sweep itself can (and should) run fast and
deterministically first, reserving Antithesis time for sweep × churn. Also
subject to the CW-5 vote-path conflict.

### P-9. `at-most-one-primary-component` — the `Always` is likely never challenged by the v1 workload

With 3 equal-weight nodes, clean majority arithmetic makes split-brain
unreachable through partitions alone; the catalog's residual triggers are
`pc.weight`-change races and different-epoch merges. No property or topology
section commits the workload to issuing `pc.weight` changes, and
different-epoch merges want unclean bounces (termination-gated). The check is
nearly free (poll-based), so keeping it is right — but its v1 role is a safety
net, not an explored property, and its `Sometimes` vacuity guard (non-Primary
window observed) will pass while the interesting preconditions are never
generated. **Suggested action**: add `SET GLOBAL wsrep_provider_options='pc.weight=N'`
to the workload action set if this property is meant to be more than passive.

### P-10. `grastate-se-checkpoint-agreement` — in v1 it degenerates to testing the harness's own supervisor

Under graceful-only restarts, grastate is written by a clean shutdown and
*should* trivially satisfy the probe; every interesting generator (kill in the
signal-handler-vs-redo-flush window, kill inside provider `pause()`, post-IST
blanking, NBO unsafe_ window) needs termination. The one thing the v1 probe
does exercise is the harness's own recovery-dance implementation — useful as
harness validation, but the catalog sells it as top-10 #6 on the strength of
PXC-4845, none of whose preconditions v1 can create (modulo CW-3 incidental
aborts). Phase-tag it.

### P-11. Underestimated: `failed-state-transfer-node-rejoins` is the best crash-recovery vehicle v1 has

Its IST-watchdog arm is the only *default-fault-reachable* path to an
ungraceful node death in v1 (10s per-message stall → `mark_corrupt` + abort →
supervisor restart → real crash recovery). That makes it not just one Medium
property but the *gateway* through which `grastate-se-checkpoint-agreement`,
`gcache-crash-recovery-no-abort`, and `restarted-node-rejoins-synced`'s
recovery arms get any v1 evaluation at all (CW-3). It also carries two
deterministic crash-loop candidates with the position preserved
(`replicator_str.cpp:946`, `:1010`) reachable via port-squat/load — real
unhappy paths nobody tests. **Suggested action**: promote to High for v1 and
implement early; pair its abort events with the restart-time probes so
incidental crash-recovery coverage is captured rather than wasted.

### P-12. Underestimated: `graceful-shutdown-bounded` is load-bearing v1 infrastructure

Every v1 restart-driven exploration flows through SQL `SHUTDOWN` → 10s
maintenance sleep → applier drain (`while(true) sleep(1)`, unbounded) →
supervisor. A monitor-wedged applier turns the *restart driver itself* into a
hang, silently killing the restart dimension of every other property for the
rest of the branch. In v1 this property is not a Medium peer of the others; it
is a precondition for the restart-based exploration working at all.
**Suggested action**: implement in the first tranche, with the exit-bound
watchdog treated as harness-critical.

### P-13. `notify-cmd-hang-does-not-block-commits` — dead by config omission, though it's the cheapest fault-composition surface on offer

The v1 my.cnf sketch configures no `wsrep_notify_cmd`, so the property is
untestable as planned — yet the mechanism (synchronous, untimed script run
while holding the server_state mutex, firing on every view including joiner
phases, with the *shipped example script* connecting back into mysqld) means
that one config line + the stock script turns every network fault into a
potential cluster-stall probe with zero new code. For a catalog hungry for
default-fault-reachable liveness properties, deferring this one is
inconsistent. **Suggested action**: add the notify script to the v1 image; the
property's `Always` bounds ride on the existing commit-progress watchdog.

### P-14. SR gating is overstated: `wsrep_trx_fragment_size` is a session variable

Verified at `sql/sys_vars.cc:8575-8583`: `HINT_UPDATEABLE SESSION_VAR`. The
catalog's framing ("the SR properties need a workload variant with
`wsrep_trx_fragment_size > 0`", "gated on SR config variant") reads as an
environment/config gate; in fact any workload session can turn SR on per-query.
Consequences: `sr-rollback-fragment-noop`'s non-crash arms (dummy-demotion
under membership churn — a verified silent-divergence path) are v1-viable
*today*, and the SR `Sometimes` guard inside `cross-node-row-equality` ("SR
under churn") is reachable without any environment change.
`sr-fragment-cross-node-agreement` stays termination-gated. **Suggested
action**: reclassify the SR gate as workload-side; pull
`sr-rollback-fragment-noop`'s churn arms into v1.

### P-15. `restarted-node-rejoins-synced` — the highest-value arm hides a required *sequence* fault no one owns

The PXC-4631 liveness→safety conversion (graceful restart → IST-only join →
kill -9 *mid-IST* → restart claims pre-IST position → re-applies writesets)
needs a graceful-restart-then-kill *sequence* with the kill landing inside an
IST window. Platform termination faults kill at random times; hitting mid-IST
requires either luck × many branches or a workload-driven targeted kill
(CW-2's node-side test command, triggered when the workload observes state 3/
IST in progress). The catalog flags "needs termination" but not that *random*
termination poorly amplifies this arm. Worth one sentence in the evidence,
because it strengthens the case for workload-side kill commands over relying on
platform faults.

### P-16. Async-monitor properties: catalog priority contradicts the SUT analysis's threat ranking

sut-analysis ranks `Wsrep_async_monitor` a top-5 Antithesis target (young,
regression-prone, four realized bugs, node-suicide handler, invisible to
performance_schema); the topology defers the async topology to phase 2; the
catalog rates both properties Medium and gates them correctly — but nothing
records the aggregate effect: **the #4 threat area has zero coverage in the
plan of record and no committed date**. That's a coherent decision only if made
explicitly. Also note `duplicate-gtid-skip-exactly-once`'s PXC-4665 won't-fix
carve-out requires workload discipline (no open transactions under contested
`gtid_next`) that must be written into the *shared* workload conventions, not
just this property, once phase 2 lands — contested `gtid_next` is plain SQL two
sessions can produce by accident.

### P-17. `sst-ready-message-implies-listener` — what's left after the rescopes is thin for a standing property

The torn-snapshot arm is rescoped away (rsync-only, config rejected by
default); the unknown-control-word `Unreachable` is instrumentation-gated; the
orphan arm belongs to `failed-state-transfer`. The remainder is "SST completes
or errors within ~10 min" — a timeout bound already implied by
`restarted-node-rejoins-synced`'s convergence check. In v1 this property is
approximately a duplicate liveness bound. Fine to keep as a distinct message on
the shared watchdog; not fine to spend separate implementation effort on.

### P-18. `no-dual-bootstrap-after-full-shutdown` / `cluster-identity-single-lineage` — v1 harness design deliberately removes the attack surface the properties need

The topology's marker-file bootstrap guard and non-persistent supervisor env
are *correct harness engineering* and simultaneously make both properties
vacuous: no harness path re-delivers `--wsrep-new-cluster`, and the
`safe_to_bootstrap` shapes need unclean stops. The catalog's open questions
gesture at this ("depends on harness supervisor design") but don't draw the
conclusion: testing these requires a *deliberately field-faithful* (i.e.,
unsafe) supervisor variant — a separate environment that emulates the systemd
EnvironmentFile persistence — or they should be marked out-of-scope until one
exists. Half-measures (running them against the safe supervisor) produce
permanent green with zero information.

---

## Passes (checked, look right)

- **Flagship set is genuinely Antithesis-shaped**: `cross-node-row-equality`
  (external oracle for divergence the SUT structurally cannot see),
  `non-primary-rejects-writes` (race-free ack-journal under partitions),
  `first-committer-wins-loser-leaves-no-trace`,
  `bf-replay-commits-exactly-once` / `trx-replay-never-fatal` (unlocked-mutex
  BF-abort windows — timing seams no fixed-input test reaches),
  `ist-overlap-writesets-not-reapplied` (partition-driven, realized bugs both
  directions, sharp expected-value oracle),
  `flow-control-pause-releases` + `synced-node-recv-queue-bounded` (layered
  liveness stack with view-change self-heal correctly handled in the watchdog
  design), `partition-heal-single-primary-remerge` (real timers vs MTR-relaxed
  — exactly the whitespace), `maint-mode-honors-operator-intent`
  (release-reachable, network-faults-only, genuinely novel finding),
  `sync-wait-reads-observe-acked-writes`, `clustercheck-200-implies-write-progress`,
  `fatal-node-terminates-no-zombie` (oracle-integrity role well argued),
  `donor-returns-to-synced` (exported `wsrep_desync_count` as a zero-cost leak
  oracle), `commit-order-monitor-released-no-cluster-stall`,
  `no-mdl-bf-bf-abort` (log-grep fallback makes it v1-viable without
  instrumentation). These would all waste a deterministic test's time and
  reward Antithesis's search.
- **Assertion-type discipline is mostly good**: vacuity guards (`Sometimes`)
  accompany nearly every `Always`/`Unreachable`; `AlwaysOrUnreachable` is used
  correctly for rare-qualifier properties (`interrupted-sst-forces-full-sst`);
  the racy-poll soundness argument for `at-most-one-primary-component`
  (disjointness) is correct; the FC watchdog's view-change-reset caveat is the
  kind of detail that prevents false liveness findings.
- **Fault-requirement flagging is consistently present inline** (even though
  phase consequences aren't aggregated — CW-1).
- **Known-issue hygiene**: PXC-4665 won't-fix and the XA `commit_by_xid` stub
  are correctly fenced out of day-one workloads.
- **Barrier resolution** (`wsrep_sync_wait` sufficient for quiesced checksums,
  with `sync-wait-reads-observe-acked-writes` guarding the residual) is sound
  layering: the oracle's precondition is itself a tested property.
- **Negative findings recorded** (`wsrep_certification_rules` inert; broken-grep
  scripts vestigial; clock jitter not required) — prevents wasted effort.

## Uncertainties

- **Assert-build behavior under injected divergence** (CW-4): whether
  `wsrep_on=OFF` sabotage reaches the vote round or trips an assert first on
  the assert-enabled image — not determinable statically; needs one smoke run.
  If asserts fire first, both vote properties silently test the wrong
  mechanism in v1.
- **Tenant default fault set details**: whether "default network faults"
  include CPU throttle (relevant to `check_inactive` self-skip in
  `partition-heal`) and any disk faults (several "widen" legs). I treated both
  as absent; if throttle is present, `partition-heal`'s self-skip arm is
  stronger in v1 than assumed.
- **Achievable writeset rate in the harness** (P-5): the monitor-overflow
  explorability argument depends on it; needs the calibration run the catalog
  itself proposes.
- **Whether node-side test commands are acceptable harness practice for this
  customer** (CW-2's kill channel): standard Antithesis mechanics support it,
  but the topology's single-workload-container design may be a deliberate
  constraint I couldn't confirm.
- **`Sometimes` marker fate without SDK instrumentation**: many coverage
  markers are specified as "missing" SUT-side `Sometimes`; I could not
  determine from the catalog whether the team intends log-grep equivalents in
  v1 for exploration guidance, or to run v1 with essentially no coverage
  signal beyond workload-observable events. The answer changes how much
  exploration steering v1 actually gets.
- **X-plugin availability in the v1 image** (P-4 channel (c)): the topology
  never mentions mysqlx; if the plugin isn't loaded, that channel is dead in
  v1 regardless of workload.
