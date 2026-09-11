# joiner-reaches-synced-after-state-transfer — A JOINED node reaches SYNCED under sustained load

**Focus area:** Resource boundaries — capacity limit at the JOINED→SYNCED gate; backpressure
adequacy.
**Confidence:** High on mechanism (gate re-verified in source); Medium on what time bound is
fair under Antithesis-throttled networks.

## Claim under test

After completing IST/SST a node is JOINED and must drain its accumulated receive queue to
`lower_limit` before it may send the SYNC message and become SYNCED — only then does
`wsrep_ready` turn ON and the node serve traffic. Flow control from the JOINED node
(JOINED ≤ max_fc_state) is supposed to throttle the cluster enough that drain always wins
against incoming load. The claim: **every node that reaches JOINED subsequently reaches
SYNCED in bounded time, even with the workload still writing**. The failure mode is a
capacity trap: sustained load keeps `queue_len > lower_limit` forever, the node stays
JOINED indefinitely — cluster capacity silently reduced by one node, with rejoin-after-fault
never completing.

## Code paths (galera submodule / sql/)

- The gate: `gcs_send_sync_begin` — SYNC sent only when `GCS_CONN_JOINED == state &&
  lower_limit >= queue_len && !sync_sent()` (gcs/src/gcs.cpp:690-717); becoming synced resets
  `fc_offset` (:1024-1029).
- The counterpressure that should guarantee progress: JOINED nodes still send FC_STOP
  (`state <= max_fc_state` with max_fc_state = JOINED by default, gcs.cpp:441-442, 542-561),
  so a joiner that can't catch up pauses the writers. If FC and the SYNC gate interact
  correctly, JOINED is transient; the property checks that composition.
- `sync_sent` one-shot: set true when SYNC sent (:697), reset on state shift/CC. A lost SYNC
  message or a missed reset (`sync_sent_` stuck true) leaves the node JOINED with the gate
  permanently closed — the message is sent at most once per drain event
  (gcs_send_sync_end :719-734, resets to false on send error only).
- wsrep_ready only on synced: Wsrep_server_service::log_state_change — READY flipped at
  s_synced (sql/wsrep_server_service.cc:356-380, per sut-analysis §6.12/§6.14).
- Related joiner-side throttle: during actual state transfer the *cluster* pause deadline can
  be ETERNITY (`gcs_fc.cpp:104-129` max_throttle=0) — the pre-JOINED phase has its own
  liveness hazards covered under `flow-control-pause-releases`.
- Historical wedge shape (same family): "out of order seqnos leaving desync_count
  permanently non-zero ... node will not become synced again unless temporarily removed from
  group" — documented in-code at gcs.cpp:2726-2751 (sut-analysis §6.12); desync/donor
  interleavings can permanently block the JOINED→SYNCED edge.

## Failure scenario

1. 3-node cluster under continuous moderate write load.
2. Antithesis partitions node C long enough to lag (IST-range) or long enough to need SST;
   heals the partition. C goes JOINER → JOINED.
3. Load continues; C must drain to lower_limit (~fc_resume_factor × 173 for defaults). If
   the FC interplay is broken — e.g. C's FC_STOPs are outpaced, a SYNC message is dropped in
   a view change and `sync_sent_` never resets, or desync_count is wedged non-zero — C stays
   JOINED forever: `wsrep_local_state=4` never reached, `wsrep_ready=OFF`, but the node
   applies writesets and looks alive.

## Suggested assertion (missing — no SDK instrumentation exists)

- **Type: Sometimes(cond)** — liveness. `Sometimes(node observed in state JOINED (3) and
  later observed SYNCED (4) with wsrep_ready=ON)`, evaluated per node by the workload
  poller. This both proves the run exercises rejoin and gives Antithesis the interesting
  semantic state to steer toward.
- Workload watchdog for the violation: if a node reports `wsrep_local_state=3` continuously
  for > N minutes while cluster faults are healed and write load is at the workload's normal
  (bounded) rate, flag a workload-side **Always** failure ("JOINED is always transient").
  N must exceed worst-case legitimate catch-up: bound the workload's write rate so drain
  rate under FC provably exceeds it.
- SUT-side (missing): `Reachable` at gcs_send_sync_end (gcs.cpp:724 "SENDING SYNC") and at
  the JOINED→SYNCED shift (:1024) — cheap replay anchors for the exact edge.

## Fault availability

Rejoin cycles are inducible with network partitions alone (IST on heal) — default-on faults
suffice. SST-flavored rejoins are richer with node kill/restart — flag: node termination
often disabled; partitions longer than gcache retention force SST without any kill.

## Open questions

- What is a fair N given Antithesis network throttling can legitimately slow drain?
  `(partial: gate mechanics fully confirmed in code — no code-side bound exists; N must be
  set from a calibration run on a healthy build with faults on. Too small → false positives
  during legitimately slow catch-up.)`
- Does the JOINED node's own FC actually pause writers fast enough at PXC's fc_limit=100
  with 3 nodes (upper≈173) when the workload uses many client connections? `(partial:
  FC_STOP from JOINED state confirmed in code (max_fc_state includes JOINED,
  gcs.cpp:441-442,542-561); whether drain oscillates under many clients is empirical —
  set the workload write-rate cap from the same calibration run.)`

The `sync_sent_` lost-signal question is resolved — the wedge the property feared is
handled in code (see Investigation Log); the property remains valuable for the
composition/timing of FC vs the SYNC gate, not for a missing reset.

### Investigation Log

#### Is `sync_sent_` reset on every configuration change/state shift?

- Examined: all `sync_sent` references in `gcs/src/gcs.cpp` (:241-250 accessors, :690-717
  gcs_send_sync_begin, :719-741 gcs_send_sync_end, :1010-1030 gcs_become_synced, :1226 conf
  change handler, :1480-1500 GCS_ACT_SYNC handling).
- Found: `sync_sent(false)` on (a) every delivered configuration change — reset inside the
  fifo-locked CC-handling block at :1226; (b) SYNC core-send error (:734); (c) a
  group-failed SYNC — self-delivered `GCS_ACT_SYNC` with `rcvd.id < 0` resets the flag and
  immediately resends (`gcs_send_sync`, :1489-1495); (d) becoming SYNCED (:1025). So a CC
  between SYNC-send and SYNC-delivery cannot strand `sync_sent_=true`: either the SYNC comes
  back failed (reset+resend) or the CC itself resets the flag.
- Residual nuance: after a reset the gate is only re-evaluated inside `gcs_recv` pops
  (gcs.cpp:2443), so a *totally idle* group could defer the re-send until the next delivered
  action — moot under the workload's continuous writes.
- Conclusion: RESOLVED — no missing-reset wedge; assertion bound does not need tightening
  for that failure mode. Remaining questions are calibration items (tagged partial above).
