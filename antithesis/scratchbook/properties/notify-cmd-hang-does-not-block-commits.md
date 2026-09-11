# notify-cmd-hang-does-not-block-commits

**Property:** A slow or hung `wsrep_notify_cmd` script on one node never blocks cluster-wide
commit progress: with a notify command configured (a documented, supported operator
integration), commits on *other* nodes keep succeeding within a bound while the script runs,
and the affected node either completes view/state processing after the script returns or is
cleanly evicted — the cluster never wedges.

**Confidence:** High that the execution is synchronous and untimed in the view/state path
(verified). Medium on the blast radius (node-local stall vs cluster-wide FC stall) — that
is precisely what the test discriminates.

## Why this is wildcard territory

This is the notify-cmd × view-processing coupling: an *operator integration hook* wired
synchronously into the replication control path. Focus agents test the SUT's own protocol;
none test that a supported extension point is isolated from it. The shipped example script
makes it worse: it connects back into the local mysqld mid-view-change.

## Code evidence (verified at commit f9ecb3e)

1. **Synchronous, untimed execution** — `sql/wsrep_notify.cc:106-116`:
   `wsp::process p(cmd_ptr, "r", NULL); p.wait();` — no timeout, no async dispatch.
2. **Called from the view/state-processing path** — `wsp::node_status::set`
   (`sql/wsrep_utils.h:29-36`) → `wsrep_notify_status`; call sites:
   `sql/wsrep_server_service.cc:241` (`log_view`, i.e. inside totally-ordered view
   processing on a high-priority applier), `:403` (`log_state_change`), and
   `sql/wsrep_sst.cc:1694` (donor transition). Views are processed in total order — while
   the applier executes the script, that node cannot advance past the view.
3. **Self-connect example** — the shipped `wsrep_notify.sh` pipes SQL back into the node
   (analysis focus 9 §8.3); if the node is not yet accepting connections at that state
   transition (e.g. joiner), the script blocks on connect → the state transition blocks on
   the script.
4. **Buffer-accounting bug in the same function** — `sql/wsrep_notify.cc:53-104`:
   `cmd_off += snprintf(...)` accumulates *would-have-written* lengths; on truncation
   `cmd_len - cmd_off` goes negative (huge as size_t) and `cmd_ptr + cmd_off` exceeds the
   64KB stack buffer on subsequent calls; the guard `if (cmd_off == cmd_len)` tests exact
   equality only, after the writes. Reaching truncation needs a ~64KB command/member list —
   improbable in a small harness; recorded as an observation, not the tested invariant.
5. Note: the PXC-5240/CVE-2026-49261 injection is *fixed* at this commit
   (`is_valid_node_name/addr`, `wsrep_notify.cc:24-44`) — the security regression test
   belongs to focus 6. This property is about the untouched liveness coupling.
6. Related degraded path with identical shape: `wsrep_sst.cc` donor control-channel reads
   are untimed `my_fgets` (analysis §7.2) — if the notify property fires, expect the same
   class there.

## Failure scenario

Operator configures `wsrep_notify_cmd` (per docs) for proxy/state integration. A network
hang (default-on fault) makes the script's back-connection to mysqld or its downstream
(DNS, config store) stall. The node sits inside `log_view` executing the script; it stops
acking/applying past the view; its recv queue grows; flow control pauses the whole cluster.
A single hung shell script silently halts every commit on every node — with all nodes
"Synced/Primary" per the health surface (compounds with
clustercheck-200-implies-write-progress).

## Testable formulation

Harness: configure `wsrep_notify_cmd` on all nodes to a small script that logs its
invocation and sleeps `rand(0..S)` seconds (S spanning past `evs.suspect_timeout=5s` and
`evs.inactive_timeout=15s`), occasionally connecting back to the local mysqld (the
documented example's shape). Workload commits continuously on all nodes.

- `Always` (bounded progress): "at all times, at least one node in the primary component
  completed a commit within the last T seconds, OR no primary component exists" — evaluated
  by the workload's per-node commit heartbeat. `Always` because cluster-wide commit
  availability under a single slow hook must hold on every evaluation; T must exceed
  S + view-processing time (start T ≈ 2×S + 30s).
- `Always` (eventual view completion): "every view change observed via one node's
  `wsrep_cluster_conf_id` is observed by all surviving primary-component nodes within T."
- `Sometimes`: "a notify script invocation overlapped a view change on another node" and
  "a notify invocation took > evs.suspect_timeout" — confirms the dangerous overlap was
  explored.

## Instrumentation suggestions (all missing)

- Workload-side commit heartbeat + conf_id tracker (above).
- Notify script itself doubles as instrumentation: log start/end with timestamps to a file
  the workload reads (script runtime distribution, invocation-vs-view correlation).
- Optional SUT-side `Sometimes` before `p.wait()` (`wsrep_notify.cc:108`) — cheap
  exploration anchor for "notify running".

## Fault requirements

Default-on network hangs/throttling suffice (they stall the script's back-connection and
create the view churn that triggers invocations). No node termination or clock faults
required.

## Open questions

None — all three resolved (see Investigation Log). Consequences:

- **The script runs on an applier / server-state-transition thread; gcomm keeps servicing
  EVS from its own dedicated thread.** `log_view` is invoked from
  `wsrep::server_state::on_view` with a high-priority (applier) service
  (wsrep-lib/src/server_state.cpp:1156); `log_state_change` is invoked from
  `server_state::state()` **while holding the server_state mutex**
  (wsrep-lib/src/server_state.cpp:1489) — a hung script there blocks every thread that
  touches server_state, widening the blast radius beyond view processing. gcomm runs its
  own thread (GCommConn::run, created via gu_thread_create, gcs/src/gcs_gcomm.cpp:461,
  :531) and keeps answering EVS keepalives regardless — so the hung node stays a member
  while not applying: the structural expectation is the worst case (FC → cluster-wide
  stall), which is exactly what the property is written to fail on. No self-eviction path
  exists for a hung script.
- **Yes, notify fires on the joiner during SST-phase states**: `log_state_change` fires on
  every wsrep server-state transition (server_state.cpp:1489), including
  joiner/initializing/initialized transitions before mysqld accepts client connections;
  the shipped example back-connects via `mysql -h$HOST -P$PORT`
  (support-files/wsrep_notify.sh:98). An unbound port fails fast (ECONNREFUSED), but under
  network faults the connect can hang, and `p.wait()` is untimed — blocking the state
  transition while holding the server_state mutex. The dedicated `Sometimes(joiner
  completed SST with notify_cmd set)` is warranted.
- **Per-view invocation confirmed**: `log_view` calls `local_status.set(local_status.get(),
  &view)` (sql/wsrep_server_service.cc:241) and `node_status::set` runs the command
  whenever `view != 0` even with unchanged status (sql/wsrep_utils.h:29-34) — the script
  executes on every delivered view under churn (cost/latency driver for the harness
  script's sleep distribution).

### Investigation Log

#### Which thread runs `log_view`/the script; does gcomm keep servicing EVS while it blocks?

- Examined: wsrep-lib/src/server_state.cpp on_view (:860-890, log_view at :1156), state()
  transition code (:1450-1496, log_state_change at :1489 under `lock`), recovery-path
  log_view (:877); gcs/src/gcs_gcomm.cpp GCommConn thread creation (:461) and run loop
  (:531); sql/wsrep_notify.cc:106-116 (synchronous p.wait()).
- Found: view processing (and thus the script) executes on the applier/high-priority thread
  delivering the ordered CC event; log_state_change additionally runs with the
  server_state mutex held. gcomm's EVS servicing lives in a separate dedicated thread
  created at connection time and does not depend on applier progress.
- Not found: any timeout around the script, or any path where a blocked applier starves
  EVS keepalives.
- Conclusion: resolved — hung script ⇒ node remains a live member while not applying ⇒ FC
  stalls the cluster (worst case confirmed structurally); clean eviction is not the
  expected outcome. Question removed.

#### Does `wsrep_notify_status` fire on the joiner during SST states?

- Examined: wsrep-lib/src/server_state.cpp state() (:1489 fires on every allowed
  transition, incl. joiner-phase rows of the transition matrix at :1450-1470);
  sql/wsrep_server_service.cc log_state_change (:380-403); support-files/wsrep_notify.sh
  (:98 mysql back-connection).
- Found: every server-state transition triggers log_state_change → local_status.set →
  wsrep_notify_status; joiner-phase transitions occur before the SQL port accepts
  connections; the example script's mysql client then fails fast (refused) or hangs under
  network faults; p.wait() is untimed and the server_state mutex is held.
- Conclusion: resolved — yes; a hanging back-connection can block the join. Added-value
  marker `Sometimes(joiner completed SST with notify_cmd set)` confirmed worthwhile.
  Question removed.

#### Does the script run on every view even with unchanged status?

- Examined: sql/wsrep_utils.h node_status::set (:29-34); sql/wsrep_server_service.cc
  log_view (:241).
- Found: `set` short-circuits only when `status == new_status && view == 0`; log_view
  always passes `&view`, so the command runs per delivered view regardless of status.
- Conclusion: resolved — per-view invocation confirmed; under fault-driven view churn the
  script fires frequently, which both amplifies the tested coupling and drives harness
  cost. Question removed.

## Synthesis refinement (2026-09-10)

wsrep_notify_cmd is READ_ONLY — blocked harder than stated (no runtime SET). Resolution: the topology defines a config variant (one my.cnf line + a shipped-example-style script baked into the image) — the cheapest fault-composition surface in the catalog; add to v1 variants.
