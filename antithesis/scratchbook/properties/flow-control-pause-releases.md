# flow-control-pause-releases — A flow-control pause is always eventually released

**Focus area:** Resource boundaries — backpressure release, unbounded retry/spin loops.
**Confidence:** High on mechanism (spin loops and STOP/CONT race re-verified in source);
Medium on the best observable for "released".

## Claim under test

Flow control is a *temporary* backpressure mechanism: once the slow node drains below
`lower_limit` it sends FC_CONT and cluster-wide replication resumes. A lost/mispaired CONT
permanently freezes commit traffic on **every** node while all of them remain Primary/Synced
— a whole-cluster wedge with healthy-looking status. This is the liveness complement of
`synced-node-recv-queue-bounded` (which watches the queue; this watches the writers).

## Code paths (galera submodule)

- The documented wedge: STOP/CONT reorder, gcs/src/gcs.cpp:582-591 — "the STOP has no
  corresponding CONT following and nodes stuck waiting for CONT." The guard is serialization
  on `fc_lock` + the `stop_sent_` counter (:578-603, :630-643); Antithesis explores the
  interleaving with the gcs recv thread and replication threads.
- What a paused writer does: `ReplicatorSMM::replicate()` retries `gcs_.replv()` on -EAGAIN
  in an **unbounded** 1 kHz spin: `while (rcode == -EAGAIN && trx.state() != S_MUST_ABORT &&
  (usleep(1000), true))` (galera/src/replicator_smm.cpp:820-822). Same pattern for SR/rollback
  fragments — `// TODO: Break loop after some timeout` ... `while (rcode == -EAGAIN &&
  (usleep(1000), true))` (:733-734; rollback fragments bypass FC via `gcs_sm_grab`, :719-729)
  — and preordered writesets (:2075-2077). A permanently-lost CONT turns every committing
  client thread into an infinite 1 kHz spinner; no timeout, no error, no diagnostics.
- Escalation path that *should* prevent silent wedge on the send side: failure to send CONT
  → `gu_abort()` "Aborting to avoid cluster lock-up" (gcs.cpp:2469-2483 per sut-analysis
  §7.3) — but that covers send *failure*, not reordering.
- Donor/joiner variant: joiner max FC throttle `gcs_fc.cpp:104-129` — `max_throttle=0` makes
  the pause deadline `GU_TIME_ETERNITY` ("Replication paused until state transfer is
  complete"), so a hung SST/IST holds the whole cluster paused indefinitely (sut-analysis
  §6.12; and the FC-release condvar deadline uses CLOCK_REALTIME, gcs.cpp:1537/1579 — a
  backwards clock step silently drops the pending release; **that sub-case needs clock
  faults, which are often disabled — flag**).
- Wedged-applier feeders (bug pattern C, realized bugs): PXC-4844 (commit-order monitor never
  released after failed TOI), MDEV-38843 (apply+rollback error → seqno stuck, "node stays
  PRIMARY while silently locking the cluster"), PXC-4845. Any of these turns one node into a
  permanent FC_STOP source: the pause is then *correctly* never released — the property
  violation localizes the bug to the monitor-release path.

## Failure scenario

1. Write load on all nodes; Antithesis throttles one node so it oscillates around
   upper/lower FC limits, generating a high rate of STOP/CONT pairs.
2. A reorder/lost-message window (gcs.cpp:582-591) leaves `stop_count > 0` on peers with no
   CONT in flight.
3. All commits cluster-wide block in the replicate() EAGAIN spin; status on every node still
   shows Primary/Synced; clustercheck returns 200 (it never inspects FC state — sut-analysis
   §8.7).

## Suggested assertion (missing — no SDK instrumentation exists)

- **Type: Sometimes(cond)** — liveness. Two conditions, both meaningful:
  1. `Sometimes(fc_pause_observed)`: some node reports `wsrep_flow_control_sent > 0` /
     `wsrep_flow_control_paused_ns` increasing (stats export gcs.cpp:2793-2794). Confirms the
     run actually exercises FC.
  2. `Sometimes(fc_pause_released)`: after (1), `wsrep_flow_control_paused_ns` stops growing
     AND `wsrep_last_committed` advances on all nodes while the cluster is Primary and no
     member is in state transfer. This is the release event.
- Workload watchdog (turns the wedge into a hard failure): a writer thread that records
  commit-progress timestamps per node; if `wsrep_last_committed` is frozen on all nodes for
  > N minutes while cluster_status=Primary, no node in state 2/3 (donor/joiner), and network
  faults have healed, emit a workload-side **Always** violation ("cluster commit progress
  never wedges while healthy"). The Sometimes pair guides exploration; the watchdog catches
  the bug.
- SUT-side (missing): an SDK `Always(stop_count/stop_sent_ consistency)` in
  gcs_handle_flow_control, and a `Reachable` on the FC-CONT-after-STOP resume path, would
  give Antithesis a replay anchor at the exact race.

## Fault availability

Core scenario needs network faults + load only (default-on). The CLOCK_REALTIME
lost-FC-release sub-case needs clock jitter (often disabled — flagged). SST-throttle variant
benefits from node restarts to force state transfers but can also be induced by long
partitions (IST on heal).

## Open questions

None — all three resolved (see Investigation Log). Key consequences folded in above and
below:

- **View change self-heals the wedge** (resolved yes): `gcs_handle_act_conf` resets
  `stop_sent_ = 0; stop_count = 0` and calls `gcs_sm_continue` if `stop_count > 0`
  (gcs.cpp:1195-1201), and FC events carrying an old `conf_id` are discarded
  (gcs.cpp:1073-1076). **Watchdog design constraint:** the freeze window N must be shorter
  than the natural view-change cadence under fault churn, or the wedge self-heals before
  detection; alternatively the watchdog should require "no view change during the frozen
  interval" as part of the failure evidence.
- **Per-node pause observables** (resolved): `wsrep_flow_control_active` = ON iff
  `stop_count > 0` — the node is currently honoring someone's STOP (gcs.cpp:2178-2181,
  :2799); `wsrep_flow_control_requested` / PXC's `wsrep_flow_control_status` = ON iff this
  node's own STOP is outstanding (`stop_sent_ > 0`, :2800/:2805). Crucially,
  `wsrep_flow_control_paused_ns` **does keep growing during an ongoing pause** — an
  in-progress pause is added at sample time (gcs_sm.cpp `gcs_sm_stats_get`, "taking sample
  in a middle of a pause"). So the release condition ("paused_ns stops growing") is sound,
  and `flow_control_active` gives a direct per-node wedge gauge.
- **Rollback-fragment FC bypass** (resolved, scope narrowed): `gcs_sm_grab` waits only on
  `entered >= GCS_SM_CC` and ignores `sm->pause` (gcs_sm.hpp:565-593), and the grab flag is
  plumbed for rollback fragments (replicator_smm.cpp:715-729 → gcs.cpp:2135-2136). But
  `provider().rollback()` is called only from wsrep-lib `streaming_rollback`
  (wsrep-lib/src/transaction.cpp:2087) — **only SR transactions emit rollback fragments**.
  The queue-growth-during-pause coupling is real but gated on the SR config variant and
  rate-bounded (one fragment per BF-aborted SR trx); reaching monitor-window overflow via
  this channel alone is implausible.

Bonus finding: `gcs.fc_auto_evict_window` (default **0 = disabled**,
gcs_params.cpp:41) — when enabled, a node whose send monitor was paused ≥ threshold
fraction of the window **aborts itself** ("Triggering automatic eviction ... will be
aborted", gcs.cpp:1088-1103). Off by default, so a wedge does not self-terminate; if a
harness config enables it, the property's failure mode changes from silent freeze to node
suicide.

### Investigation Log

#### Is a STOP/CONT wedge cleared by the next view change?

- Examined: `gcs/src/gcs.cpp` `gcs_handle_act_conf` (:1150-1230), `gcs_handle_flow_control`
  (:1070-1106), FC event conf_id field.
- Found: on every configuration action the recv path locks `fc_lock`, wakes the send
  monitor if `stop_count > 0` (`gcs_sm_continue`), and zeroes both `stop_sent_` and
  `stop_count` (:1195-1201). Independently, incoming FC events with a stale `conf_id` are
  dropped (:1073-1076), so a delayed pre-view STOP cannot re-wedge after the reset.
- Not found: any path where the reset is skipped for a Primary→Primary view.
- Conclusion: resolved — yes, any membership change clears the wedge on every node. The
  watchdog window must undercut view-change cadence under faults (or record absence of view
  changes across the frozen interval). Question removed.

#### What does `wsrep_flow_control_paused` measure across the wedge; do waiters report paused?

- Examined: `gcs/src/gcs.cpp` `gcs_get_stats` (:2777-2812), `fc_active` (:2178),
  `gcs/src/gcs_sm.hpp` (:434-477), `gcs/src/gcs_sm.cpp` `gcs_sm_stats_get` (:194-240),
  `galera/src/replicator_smm_stats.cpp` (:177-188, :313-331).
- Found: `paused_ns`/`paused_avg` come from each node's own *send monitor*; the pause state
  is set on every node that receives a STOP (`gcs_sm_pause` on first STOP, :1081-1086).
  `gcs_sm_stats_get` adds the in-progress pause (`if (paused) tmp.paused_ns += now -
  pause_start`), so `wsrep_flow_control_paused_ns` grows monotonically during a wedge on
  every stalled node. `wsrep_flow_control_active` = `stop_count > 0` (honoring a STOP);
  `wsrep_flow_control_requested`/`wsrep_flow_control_status` = own `stop_sent_ > 0`.
- Conclusion: resolved — waiters do report the pause; use `flow_control_active` (gauge) plus
  growing `paused_ns` per node for the release condition. Question removed.

#### Do rollback fragments' FC bypass grow a paused node's queue during a pause?

- Examined: `gcs/src/gcs_sm.hpp` `gcs_sm_grab` (:565-593), `gcs/src/gcs.cpp` sendv grab
  branch (:2127-2140), `galera/src/replicator_smm.cpp` `send()` (:683-745),
  `galera/src/wsrep_provider.cpp` rollback entry (:400-433), `wsrep-lib/src/transaction.cpp`
  `streaming_rollback` (:2060-2100).
- Found: `gcs_sm_grab` does not check `sm->pause` — the bypass is real; rollback fragments
  are ordinary group-replicated GCS_ACT_WRITESET actions landing in every node's recv queue.
  The only caller of `provider().rollback()` is wsrep-lib `streaming_rollback` (SR
  transactions only, `wsrep_trx_fragment_size > 0`).
- Not found: any non-SR path that sends with grab=true.
- Conclusion: resolved — mechanism confirmed but gated on the SR variant and rate-bounded;
  noted as a coupling amplifier, not a standalone overflow channel. Question removed.
