# synced-node-recv-queue-bounded — Recv queue on a SYNCED node stays bounded by flow control

**Focus area:** Resource boundaries — backpressure, queue depth.
**Confidence:** High that the mechanism is as described (all lines below re-verified at commit f9ecb3e / galera @13ff9ed6); Medium on the exact numeric bound a workload check should use.

## Claim under test

Galera flow control claims to bound each node's applier/slave receive queue near
`gcs.fc_limit`: when `queue_len > upper_limit + fc_offset` the node broadcasts FC_STOP and the
whole cluster pauses replication until the node drains below `lower_limit` and sends FC_CONT.
PXC ships `gcs.fc_limit = 100` (vs upstream 16). If FC works, `wsrep_local_recv_queue` on a
node in state SYNCED never grows past upper_limit plus in-flight slack. If FC wedges, the
queue's only remaining backstops are the recv FIFO hard cap — sized from **host** physical
memory — and the 65536-slot monitor window (separate property
`monitor-window-overflow-unreachable`).

## Code paths (galera submodule, percona-xtradb-cluster-galera/)

- FC limits: `_set_fc_limits`, gcs/src/gcs.cpp:1034-1058.
  `upper_limit = fc_base_limit * sqrt(non_arb_memb_count)` (PXC uses non-arbitrator member
  count, :1038-1040), `lower_limit = upper_limit * fc_resume_factor`; both clamped to
  `gu_fifo_max_length(recv_q)` (PXC-only, :1049-1054). Defaults: `gcs.fc_limit` **100** under
  PXC vs 16 upstream (gcs/src/gcs_params.cpp:34-36). For a 3-node cluster:
  upper ≈ 173, lower ≈ 173 * fc_resume_factor.
- STOP trigger: `gcs_fc_stop_begin`, gcs.cpp:542-561 — requires
  `stop_count <= 0 && stop_sent_ <= 0 && queue_len > upper_limit + fc_offset &&
  state <= max_fc_state`.
- CONT trigger: `gcs_fc_cont_begin`, gcs.cpp:622-644 — requires `stop_sent_ > 0 &&
  (lower_limit >= queue_len || queue_decreased)`.
- **State gating**: `max_fc_state = sync_donor ? GCS_CONN_DONOR : GCS_CONN_JOINED`
  (gcs.cpp:441-442); state enum SYNCED=0 < JOINED=1 < DONOR=2 < JOINER=3 (gcs.cpp:88-101).
  So with default `gcs.sync_donor=no`, a DONOR/DESYNCED node sends **no** FC at all — its
  queue is legitimately unbounded. The property must therefore condition on
  `wsrep_local_state == 4 (Synced)` and `wsrep_desync_count == 0`, and skip RSU/desync phases.
- **Known wedge, documented in-code**: STOP/CONT reorder — the comment at gcs.cpp:582-591
  describes gcs_recv_thread deciding to send STOP, racing with the replication thread
  sending CONT first: "As the messages are swapped, the STOP has no corresponding CONT
  following and nodes stuck waiting for CONT."
- Hard backstop: recv_q created with `gu_avphys_bytes() / sizeof(gcs_recv_act) / 4` slots
  (gcs.cpp:402-419) — **host-memory-derived, cgroup-blind**. In a memory-limited container
  the FIFO cap can exceed what the cgroup allows; a wedged-FC queue OOMs the container long
  before the FIFO backpressures.
- Recovery valve (from sut-analysis §7.7, not independently re-verified this pass): the
  recv-side FC refcount is reset on view change (gcs.cpp:1196-1218) — so a wedge may self-heal
  when membership changes; failure to send CONT escalates to `gu_abort()` "Aborting to avoid
  cluster lock-up" (gcs.cpp:2469-2483).

## Failure scenario

1. Sustained multi-node write load; one node's applier is slowed (network throttle on its
   link, CPU starvation, or a pattern-C monitor bug such as PXC-4844/MDEV-38843).
2. Its queue crosses upper_limit; FC_STOP is due. Antithesis delays/reorders the STOP vs a
   concurrent CONT (the gcs.cpp:582-591 race), or a view change resets accounting at the
   wrong moment.
3. STOP is lost/never followed by CONT pairing correctly → the slow node stops sending FC,
   the rest of the cluster keeps replicating → `wsrep_local_recv_queue` grows into the
   thousands/millions with the node still reporting Synced → container memory exhaustion or
   monitor-window overflow.

## Suggested assertion (missing — no SDK instrumentation exists)

- **Type: Always** (safety — must hold on every evaluation). Workload-side: poll
  `SHOW GLOBAL STATUS LIKE 'wsrep_local_recv_queue'` + `wsrep_local_state` +
  `wsrep_desync_count` on every node every few seconds; assert
  `state==4 && desync_count==0 ⇒ recv_queue < THRESHOLD`.
- THRESHOLD must be far above upper_limit to absorb legitimate overshoot (STOP propagation
  is asynchronous; messages in flight keep arriving; network throttle widens the window).
  Suggested: 100 × upper_limit (~17,000 for 3 nodes at fc_limit=100) — a wedged queue blows
  through this within seconds of sustained load, a healthy one never approaches it.
- Companion **Sometimes**: `wsrep_flow_control_sent > 0` observed on some node (FC actually
  exercised; exported via stats, gcs.cpp:2793-2794 and the wsrep_flow_control_* status vars)
  — otherwise the Always is vacuous.
- SUT-side (missing, optional): Antithesis `Unreachable` at the gcs.cpp:582-591 comment's
  losing branch is not directly expressible; instead an `Always(stop_sent_ >= 0 &&
  stop_count >= 0)` invariant inside `gcs_handle_flow_control` would catch refcount
  underflow directly.

## Fault availability

Needs only network faults (delay/partition/throttle) + write load — default-on. Node
termination not required.

## Open questions

- What is the worst-case legitimate overshoot of `wsrep_local_recv_queue` above upper_limit
  under heavy network throttle? `(partial: no static bound exists in code — overshoot =
  aggregate replication rate × STOP delivery latency; FC events are ordinary group messages,
  so throttle stretches that latency to seconds; only hard caps are the host-memory recv
  FIFO and the 65536 monitor window. Empirical calibration run against a known-healthy build
  still required to pick THRESHOLD.)`

Resolved (see Investigation Log):

- **View-change FC reset fully clears a STOP/CONT wedge** — yes: every conf action zeroes
  `stop_sent_`/`stop_count` and continues the send monitor (gcs.cpp:1195-1201), and stale FC
  events are discarded by conf_id (:1073-1076). Because `stop_sent_` is reset, a node whose
  queue is still above the limit re-triggers `gcs_fc_stop_begin` afresh. Consequence: the
  Always must NOT be suppressed during membership churn — a wedge does not survive a view
  change, so any sustained over-threshold queue on a Synced node is a genuine finding
  regardless of churn.
- **`wsrep_local_state` stays 4 (Synced) during an FC pause** — confirmed in code:
  `gcs_handle_flow_control` (gcs.cpp:1070-1106) touches only FC counters and the send
  monitor; no `gcs_shift_state` on FC events (the only exception is the off-by-default
  `gcs.fc_auto_evict` abort path). Node state changes only via JOIN/SYNC/CC actions. The
  `state==4 && desync_count==0` conditioning is correct.

### Investigation Log

#### Worst-case legitimate overshoot of recv queue above upper_limit?

- Examined: `gcs/src/gcs.cpp` `_set_fc_limits` (:1034-1058), `gcs_fc_stop_begin`
  (:542-561), recv_q sizing (:402-419), FC event send/delivery path.
- Found: STOP is asynchronous — after `stop_sent_` increments, peers keep replicating until
  the STOP message is delivered back through the group; nothing in code bounds the number of
  in-flight writesets during that round-trip. Overshoot therefore scales with aggregate
  write rate × STOP delivery latency, which network throttle inflates arbitrarily.
- Not found: any static overshoot cap short of the host-memory FIFO length.
- Conclusion: tagged `(partial)` — the bound is empirical by construction; a calibration run
  is the designed next step, not further code reading.

#### Does the view-change FC refcount reset fully clear a STOP/CONT wedge?

- Examined: `gcs_handle_act_conf` (gcs.cpp:1150-1230), `gcs_handle_flow_control`
  (:1070-1106).
- Found: conf processing wakes the send monitor (`gcs_sm_continue` when `stop_count > 0`)
  and zeroes both counters (:1195-1201); FC events with stale conf_id are dropped
  (:1073-1076); reset `stop_sent_` lets a still-overloaded node immediately re-issue STOP.
- Conclusion: resolved — yes; wedges only live between view changes, and the queue-bound
  Always should hold through churn without suppression. Question removed.

#### Does `wsrep_local_state` stay 4 during an FC pause?

- Examined: `gcs_handle_flow_control` (gcs.cpp:1070-1106), state-shift call sites
  (`gcs_shift_state` callers), `fc_active`/stats export (:2777-2812).
- Found: FC events mutate only `stop_count`/`stop_sent_`/send monitor; the sole state
  transition reachable from the FC handler is the `fc_auto_evict` abort (default window 0 =
  disabled, gcs_params.cpp:41). SYNCED state is unaffected by pause.
- Conclusion: resolved — conditioning on `state==4` is correct. Question removed.
