# partition-heal-single-primary-remerge — Evidence

**Focus area:** Distributed coordination — partition heal, view-change liveness, failure
detection at real (non-MTR) timeouts.
**Confidence:** High on mechanisms (all read directly); the numeric bound is a parameter to
tune in the harness, not a code-derived constant.

## Claim under test

After a network partition heals (and no further faults are injected), all non-evicted nodes
re-merge into a single Primary Component and reach `wsrep_ready=ON` within a bounded time
(bound ≈ small multiple of EVS timers; propose 90s wall time as the initial assertion bound).

## Code (validated, commit f9ecb3e; galera @13ff9ed6)

- Timers, real defaults (`gcomm/src/defaults.cpp`): evs.suspect PT5S (:28), evs.inactive PT15S
  (:30), inactive_check PT0.5S (:27), keepalive PT1S (:32), join_retrans PT1S (:36),
  max_install_timeouts 3 (:53), auto_evict 0 (:56), pc.announce PT3S (:60), pc.wait_prim
  PT30S (:67), pc.linger PT20S (:71). Install timeout = inactive/2 = 7.5s.
  **MTR relaxes all of these** (`mysql-test/mysql-test-run.pl:4349`: suspect 12s, inactive
  30s, max_install 1) — the corpus is tuned to avoid membership churn; Antithesis at real
  defaults is whitespace.
- Failure-detection self-skip: `gcomm/src/evs_proto.cpp:899-909` — `check_inactive()` skips
  the ENTIRE check when the previous check ran > 3×check_period ago (CPU starvation, slow
  handler on the single-threaded gcomm loop) → detection of a dead peer is delayed
  indefinitely under repeated skips.
- Install-timer escalation: `evs_proto.cpp:680-739` — attempts < max: only inconsistent nodes
  marked inactive; attempt == max (3): ALL others marked inactive + self-isolate for
  suspect+inactive = 20s (:726-729); attempt > max: `gu_throw_fatal` "giving up" —
  **process suicide** (:731-739). ~4 consecutive failed installs (~30s of asymmetric
  connectivity) can kill a node outright.
- Re-merge gating in PC: `gcomm/src/pc_proto.cpp:944-1062` — when no member arrives from a
  prim view, re-bootstrapping the prim requires (a) NO node in `un()` (unknown) state
  (:964-972 — "unable to rebootstrap new prim"), and (b) ALL non-evicted members of the
  greatest last-prim view present (:1054). A flapping third node keeps others in `un()` →
  re-merge blocked indefinitely; a permanently-dead member of the greatest view blocks (b)
  forever on that path.
- Conflicting-prims merge: `pc_proto.cpp:1064-1100` — heal after dual `pc.bootstrap` (or a
  dual-PC bug) aborts one node (npvo tiebreak). Not part of the liveness bound; excluded by
  workload discipline (never bootstrap both sides).
- Full-cluster-restart variant: `gcomm/src/pc.cpp:104-115` — restored V_PRIM view
  (pc.recovery=true default) + `pc.wait_restored_prim_timeout` default PT0S → "server will
  wait indefinitely to reach PC"; mysqld blocks untimed
  (wsrep-lib `src/server_state.cpp:1498-1518`) with the SQL port closed.
- gmcast reconnect: `gcomm/src/gmcast.cpp:820-829,1039-1113` — 1s retries, INT_MAX attempts;
  BUT `handle_established` discards a fresh inbound connection when `retry_cnt >
  max_retries` (:726-734), and addresses are resolved exactly once at connect
  (`gmcast.cpp:285-309`) — heal after an address change never completes (container IP churn).
- Post-merge sync gate: JOINED→SYNCED requires recv queue ≤ lower limit
  (`gcs/src/gcs.cpp:689-716`) — sustained write load can hold a re-joined node out of
  `wsrep_ready` arbitrarily long; the bound must be asserted under paused/light load, or the
  assertion becomes flaky by design.

## Failure scenario

1. 3-node cluster, moderate load. Antithesis partitions node C (symmetric or asymmetric) for
   30-120s, then heals. Variants: flap the partition at 1-10s period (targets `un()` blocking
   and install-timer escalation); CPU-throttle a node during heal (targets check_inactive
   self-skip and the single-mutex gcomm loop).
2. Expected: majority {A,B} stays Primary throughout; C goes non-Primary within ~15s of the
   cut; after heal, EVS merges within a few join_retrans/install rounds; C returns through
   JOINED→SYNCED (IST from gcache) and `wsrep_ready=ON`.
3. Bug shapes: C stuck non-Primary forever (un()-state deadlock, discarded inbound connection
   at :726-734, permanent `S_GATHER` churn with auto_evict=0); C hits the "giving up"
   `gu_throw_fatal` (then stays down — shipped systemd excludes SIGABRT from restart); whole
   cluster loses Primary although a majority was always mutually connected.

## How to check (workload-side)

- Liveness: after last injected network fault + settle time T (start with 90s), every running
  node reports `wsrep_cluster_status=Primary`, identical `wsrep_cluster_conf_id`, cluster_size
  == number of running nodes, `wsrep_ready=ON`. Implement as `Sometimes` (eventually
  condition) or as an end-of-test `Always` on the quiesced state.
- Safety companion: the majority side NEVER loses Primary while it retains ≥ quorum weight and
  intra-majority connectivity (needs fault-log awareness — only assertable when the injected
  partition provably left a connected majority; otherwise skip the round).
- `Sometimes`: node observed non-Primary then later Synced again (the re-merge actually
  happened at least once per run).

## Assertion type

- `Sometimes(all_nodes_single_primary_after_heal)` — liveness; Antithesis semantics: it must
  become true at least once after each heal window; end-of-run quiesced `Always` variant for
  the final state. (Chose Sometimes as primary because bounded-time liveness under ongoing
  fault injection can't be an Always without gating on the fault schedule.)

## Instrumentation suggestions (missing)

- SUT-side `Reachable` on `pc_proto.cpp:1056` "re-bootstrapping prim from partitioned
  components" and on `evs_proto.cpp:726` max-install isolate — rare paths worth steering
  toward.
- SUT-side `Unreachable` on `evs_proto.cpp:735-739` (install-timeout give-up suicide) for
  runs whose fault schedule keeps asymmetry < the ~30s escalation budget.
- SUT-side `Sometimes` at `check_inactive` self-skip (`evs_proto.cpp:904`) — confirms the
  CPU-starvation degradation is being explored; a counter of consecutive skips > K is a
  degraded-mode detail worth reporting.

## Fault requirements

- Network partitions incl. asymmetric + flapping (default-on); CPU throttling (default-on) for
  the self-skip path. Node hang (SIGSTOP) exercises the same detection path. Clock jitter NOT
  needed (gcomm is monotonic-only). Node termination optional (kill+restart variant overlaps
  the bootstrap property).

## Open questions

- What is the right numeric bound? `(partial: code timer arithmetic gives ≈60s legitimate
  worst case for the membership part; empirical validation under the harness before pinning
  90s remains — a harness-tuning task, not a code question)`

Resolved (see Investigation Log): the `un()` block is a genuine indefinite-block liveness
shape (un clears only on successful new-PC install), and the bound should be split —
Primary-status convergence is membership-timer-bounded and state-transfer independent, while
the ready/Synced clause must be quiesced or transfer-aware.

### Investigation Log

#### Can a node stay `un()` across arbitrarily many view attempts under flapping?

- Examined: `gcomm/src/pc_proto.cpp:863-889` (`cleanup_instances`), `:944-972` (`is_prim`
  un-block), `:1140-1150` (`handle_state` un propagation), `:1373-1401`/`:1438-1457`
  (`handle_trans_install`/reg-install marking absent nodes un).
- Found: `set_un(true)` is applied to nodes absent from the current view when an install is
  delivered while the previous pc_view partitioned, and propagates between nodes via state
  messages. `set_un(false)` happens in exactly one place — `cleanup_instances()`, which runs
  only after a new PC is successfully installed (S_PRIM, V_REG). The re-bootstrap block at
  `:956-962` applies only to un-nodes *absent from the current view*, and only on the
  all-nodes-non-prim re-bootstrap path (`is_prim()` with no prim claimant).
- Conclusion: resolved — yes: while flapping prevents any PC install, the un flag persists
  across arbitrarily many view attempts, and whenever the flagged node is out of view it
  blocks re-bootstrap indefinitely. Scope caveat: this bites only after the whole component
  lost prim (e.g. flap during install / 50-50 tie); if a majority side retained prim
  throughout, the un mechanism never gates it. Workload shape: drive ALL nodes non-prim, heal
  with one node still flapping → assert the re-merge block clears once flapping stops and the
  node stays present (or is taken down permanently *and* a prim claimant exists).

#### What is the right numeric bound?

- Examined: `gcomm/src/defaults.cpp:27-71` (all timers), `gcomm/src/evs_proto.cpp:680-739`
  (install escalation), `gcomm/src/pc.cpp:92-114` (wait_prim).
- Found: legitimate worst-case membership re-merge from timer composition ≈
  suspect(5) + install(7.5)×3 + self-isolate(20, only on max-attempt escalation) +
  announce(3) + wait_prim(30) — ~60s without the isolate leg, ~85s with it.
- Not found: any code-derived bound for the JOINED→SYNCED drain under load (workload
  dependent by construction).
- Conclusion: tagged `(partial)` — arithmetic supports 90s as a starting bound for the
  membership clause; empirical tuning in the harness is still required before trusting
  failures. Assert the ready clause separately (next entry).

#### Must the bound be IST-vs-SST aware?

- Examined: rejoin path split in `replicator_str.cpp` (IST vs SST decision), gcache coverage
  dependence; `gcs/src/gcs.cpp:689-716` (SYNC gating on recv-queue drain).
- Found: Primary-status + conf_id convergence is purely a membership outcome — bounded by the
  gcomm timers above regardless of state transfer. Only the `wsrep_ready`/Synced clause
  depends on transfer type and load.
- Conclusion: resolved by splitting the assertion: (1) all-nodes-single-Primary within the
  membership bound of heal; (2) all-nodes-Synced asserted at quiescence (or gated on observed
  transfer type: `wsrep_local_state=3` vs SST-in-progress), with gcache sized so short
  partitions always IST if a bounded form of (2) is wanted.
