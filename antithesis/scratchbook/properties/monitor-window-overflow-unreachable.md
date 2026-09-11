# monitor-window-overflow-unreachable — The 65536-slot monitor window never overflows

**Focus area:** Resource boundaries — hard capacity limit behind flow control; queue depth
backstop.
**Confidence:** High on mechanism (monitor template and all give-up sites re-verified);
Medium-high that reaching it indicates a real upstream failure (FC or monitor-release bug)
rather than tuning.

## Claim under test

Each of the three ordering monitors (local/apply/commit) is a fixed ring of
`process_size_ = 1<<16` slots indexed by `seqno & 0xFFFF`. A seqno more than 65536 ahead of
`last_left_` cannot enter: `would_block()` returns true and callers either block on a
condvar, spin with "Deadlock is very likely", or give up with -EDEADLK and demand an
application restart. Flow control (queue bound ~173 at defaults) keeps healthy nodes ~380×
below this limit, so **the window should never fill**. Reaching it means the layered
backpressure failed: a wedged applier/monitor-release bug (pattern C: PXC-4844, MDEV-38843,
PXC-4845), a desynced/DONOR node (no FC) falling 65k+ writesets behind, or an FC wedge. The
code's own responses at that point are the property's smoking guns — they are logged
capitulations, not recovery.

## Code paths (galera submodule)

- Ring: `static const ssize_t process_size_ = (1ULL << 16)` and mask
  (galera/src/monitor.hpp:51-52); `would_block(seqno)`: `seqno - last_left_ >= process_size_
  || seqno > drain_seqno_` (:339-343).
- Spin capitulation: `self_cancel()` loops `while (obj_seqno - last_left_ >= process_size_)`
  logging "Trying to self-cancel seqno out of process space ... **Deadlock is very likely.**"
  with `// TODO: exit on error` (:242-253).
- Even "read" APIs block on overflow: `interrupt()` waits in the same condition (:275-284) —
  a BF-abort against an overflowed monitor blocks the aborter too, spreading the stall to
  client threads.
- Give-up sites converting overflow to node suicide/restart-required:
  - STR: "Slave queue grew too long while trying to request state transfer ... Application
    must be restarted." → -EDEADLK (galera/src/replicator_str.cpp:1030-1046). **This is the
    only live EDEADLK give-up.**
  - desync paths — CORRECTED (this pass): the "Ran out of resources waiting to desync"
    -EDEADLK throw at replicator_smm.cpp:3545-3551 is **dead code** — it sits inside a
    `/* #706 ... */` block comment; the live behavior of `desync()` is an untimed blocking
    `local_monitor_.enter()` (:3552), i.e. FTWRL/`wsrep_desync=ON` **hangs** on overflow
    rather than erroring. The PXC-only `try_desync_and_pause` (:3426) checks `would_block`
    and declines gracefully (returns WSREP_SEQNO_UNDEFINED) — no capitulation log. The
    log-message detector list must therefore drop "Ran out of resources waiting to enter
    local monitor" (never printed in this tree); the desync-hang manifestation is only
    catchable via the workload watchdog.
- Feeder mechanism (why 65k separation is reachable at all): a DONOR/DESYNCED node sends no
  FC (gcs.cpp:441-442, state gating in :542-561) and rollback fragments bypass FC via
  `gcs_sm_grab` (replicator_smm.cpp:719-729); a single never-released monitor slot (pattern
  C bugs) freezes `last_left_` while certification keeps assigning seqnos to incoming
  writesets until the recv FIFO (host-memory-sized) fills.

## Failure scenario

1. Node B is desynced (RSU, `wsrep_desync=ON`, or DONOR serving a slow SST under network
   throttle) — its FC is off; or an applier on B wedges via a pattern-C monitor-release bug.
2. Cluster write load continues; B's local seqno stream advances 65536+ past `last_left_` of
   the stuck monitor.
3. Any thread hitting `enter/self_cancel/interrupt` on B now blocks or spins at 1 Hz warns;
   BF aborts wedge; FTWRL throws EDEADLK; if B was requesting STR it aborts with "Application
   must be restarted". The node is unrecoverable without restart, and given the shipped
   systemd units' RestartPreventExitStatus, possibly permanently down in the field.

## Suggested assertion (missing — no SDK instrumentation exists)

- **Type: Unreachable** — this is an impossible-by-design state whose observation is always a
  finding. Three concrete detectors:
  1. SUT-side SDK `Unreachable("monitor window overflow: self-cancel out of process space")`
     inside the `self_cancel` while-loop body (monitor.hpp:242-253) — the highest-value spot;
     also one in the STR give-up branch (replicator_str.cpp:1032-1040). Marked **missing**.
  2. Log-based fallback (no code change): alert on "Deadlock is very likely" and "Slave
     queue grew too long" in mysqld error logs. (Corrected: "Ran out of resources waiting
     to..." is dead code in this tree and never prints — see Code paths.) No drain-phase
     exclusion is needed: neither message can be produced by the drain term (see
     Investigation Log).
  3. Workload proxy: `wsrep_local_recv_queue > 65536` on any node (necessarily implies the
     apply monitor window is at risk) — cheap but less precise.
- Rationale for Unreachable over Always: there is no invariant to evaluate on each pass —
  the property is that a specific capitulation path is never entered; entering it once is
  the bug/finding.
- Note: with desync/RSU in the workload mix this may legitimately fire under extreme
  sustained load — that is still a reportable finding (the SUT's own log calls it a likely
  deadlock), but triage should record whether desync was operator-initiated.

## Fault availability

Reaching 65k queue depth needs a long-lived one-node stall + sustained writes: achievable
with network throttle/partition of a DONOR or desynced node (default-on faults) plus a
write-heavy workload and small writesets. No node termination or clock faults required,
though kill-induced SSTs raise the hit rate.

## Open questions

- How fast can a 3-node Antithesis cluster realistically accumulate 65536
  delivered-but-unprocessed actions on one node? `(partial: arithmetic bound established —
  local_act_id increments per delivered action even while processing is stopped
  (gcs.cpp:1802), so at a 100-1000 writesets/s cluster rate a fully wedged/desynced node
  overflows the local-monitor window in ~1-11 min of sustained flood; the host-memory recv
  FIFO will not backpressure first. The desync + tiny-transaction flood recipe is the right
  amplifier; in-harness rate still needs a calibration run.)`

Resolved (see Investigation Log):

- **Which monitor overflows first: the local monitor, always.** Apply/commit monitor
  entrants are applier threads — each holds at most one slot and enters in processing
  order, so their entered-vs-left spread is bounded by `wsrep_applier_threads` (≪ 65536);
  under a pattern-C wedge they freeze but block in-order in `may_enter`, never in
  `would_block`. The local monitor is the one whose window compares the gcs local action id
  (assigned at *delivery* into the recv FIFO, gcs.cpp:1802) against `last_left_` (advanced
  only at *processing*), so a processing wedge with continuing delivery opens a 65k gap
  there. SDK `Unreachable` placement: local-monitor capitulations — the `self_cancel` spin
  (monitor.hpp:242-253) and the STR give-up (replicator_str.cpp:1030-1046).
- **`drain_seqno_` cannot cause spurious log capitulations.** The `self_cancel` and
  `interrupt` wait loops test only `seqno - last_left_ >= process_size_` — no drain term.
  The drain term in `would_block` affects (a) `pre_enter`, i.e. silent untimed blocking of
  apply/commit-monitor entrants during donor-selection drains (by design), and (b) nothing
  at the three `would_block` call sites, which are all on `local_monitor_` — and
  `drain()` is never called on the local monitor (only on apply/commit:
  replicator_str.cpp:529/:531/:1504, replicator_smm.cpp:2374-2375), so its `drain_seqno_`
  stays GU_LLONG_MAX forever. Log detectors need no drain-phase exclusion.

### Investigation Log

#### How fast can the harness accumulate 65536 undelivered writesets on one node?

- Examined: `gcs/src/gcs.cpp` delivery loop (:1770-1830, local_act_id assignment at :1802),
  recv_q sizing (:402-419), FC state gating (:441-442).
- Found: local action ids are assigned by the gcs recv thread at delivery, before
  application processing; a DONOR/desynced node (FC off) or a wedged-processing node keeps
  accumulating delivered actions at the full cluster replication rate. FIFO cap is
  host-memory-derived (≫ 65536 entries for small writesets). Arithmetic: 65536 / (cluster
  writesets per second) = time-to-overflow; minutes at flood rates.
- Not found: actual achievable writeset rate in the Antithesis harness (no harness exists
  yet to measure).
- Conclusion: tagged `(partial)` — explorable in-run with the desync + tiny-trx flood
  recipe if rates reach ~10²/s; calibration run decides whether to keep the Unreachable or
  downgrade to the recv-queue proxy.

#### Does a pattern-C wedge freeze the commit monitor too — which monitor's overflow fires first?

- Examined: `galera/src/monitor.hpp` enter/pre_enter/may_enter/leave/update_last_left
  (:100-200, :425-470), self_cancel/interrupt (:232-305), `gcs/src/gcs.cpp` local_act_id
  (:1802, :2772-2775), would_block callers (replicator_str.cpp:1032,
  replicator_smm.cpp:3426/:3545).
- Found: apply/commit monitor slots are occupied only by applier threads calling enter() in
  their own context; a frozen `last_left_` blocks the next in-order entrant via
  `may_enter`, so the entered-spread never approaches 65536 (bounded by thread count). The
  local monitor's seqnos are gcs local action ids that advance at delivery regardless of
  processing, so only the local monitor develops a 65k spread; its entry points (STR
  request, desync, pause, self-cancel of local actions) are where would_block/pre_enter
  trip.
- Conclusion: resolved — overflow always manifests on the local monitor; SDK call sites are
  the local-monitor self_cancel spin and the STR give-up branch. Question removed.

#### Can `drain_seqno_` trip would_block spuriously during donor drains?

- Examined: `monitor.hpp` would_block (:339-343), self_cancel (:232-253), interrupt
  (:275-284), pre_enter (:437-450), drain (:346-365); all `drain()` and `would_block()`
  call sites in galera/src.
- Found: self_cancel/interrupt loops exclude the drain term; drains are only ever executed
  on apply_monitor_ and commit_monitor_ (replicator_str.cpp:529/:531/:1504,
  replicator_smm.cpp:2374-2375); every would_block caller uses local_monitor_, whose
  drain_seqno_ is never set. During drains, far-ahead entrants to apply/commit monitors
  block silently in pre_enter (untimed) — by design, no log emitted.
- Also found (correction recorded in Code paths): the desync -EDEADLK "Ran out of
  resources" throw (replicator_smm.cpp:3545-3551) is commented out (`/* #706 */`); live
  desync blocks untimed instead, and try_desync_and_pause (:3426) declines gracefully.
- Conclusion: resolved — no spurious drain-induced log capitulations; detector list
  corrected. Question removed.

## Synthesis refinement (2026-09-10)

Two framing changes: (1) designed-behavior carve-out — a desynced/donor node sends no FC by design, so an overflow reached only via deliberate desync + tiny-transaction flood may be the designed backstop, not a finding; attribute before filing. (2) Explorability demoted — the 1-11 min accumulation arithmetic likely exceeds harness write rates/branch budgets (worse on the Debug tier); treat as opportunistic, revisit after the per-tier calibration run.
