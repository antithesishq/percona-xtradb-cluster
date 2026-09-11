---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# graceful-shutdown-bounded — Graceful shutdown under load completes within a bound and leaves a recoverable position

**Focus area:** Lifecycle transitions — graceful shutdown under load; shutdown dropping
in-flight work; SIGTERM vs container grace.

## Claim under test

A node given a graceful stop (SIGTERM or SQL `SHUTDOWN`) while applying replication load
terminates within a bounded time, and its on-disk state (grastate.dat with a valid
UUID:seqno) allows the subsequent restart to rejoin via IST rather than full SST. Neither
half is bounded by construction today — this property measures whether the unbounded waits
ever wedge in practice.

## Code paths (verified at commit f9ecb3e)

- **Pre-shutdown sleep**: the SIGTERM/SIGQUIT handler first sets
  `pxc_maint_mode = SHUTDOWN` and does `sleep(pxc_maint_transition_period)` in the signal
  thread (`sql/mysqld.cc:4395-4404`, "Will sleep for %lu secs before initiating
  shutdown"). Default 10s (`sql/sys_vars.cc:8639-8642`, `DEFAULT(10)`, range 0-3600).
  **Docker's default stop grace is exactly 10s → a default PXC container is SIGKILLed at
  the moment real shutdown begins → every "graceful" container stop is actually unclean →
  grastate seqno -1 → forced state transfer on restart.** The same sleep runs in the
  `SET GLOBAL pxc_maint_mode` path while holding MDL (`sql/sql_parse.cc:4518-4523`, per
  sut-analysis).
- **Unbounded applier drain**: `wsrep_wait_appliers_close` (`sql/mysqld.cc:12619-12637`) —
  `while (true) { count appliers; if (count > 2) sleep(1); else break; }` — no deadline,
  no escalation. A wedged applier (commit-order monitor never released — the PXC-4844 /
  MDEV-38843 / PXC-4823 bug family) blocks shutdown forever.
- Provider close waits up to 10 min for receivers (`galera/src/replicator_smm.cpp:326-339`,
  per sut-analysis §6.14); `wsrep_ready_wait` untimed (`sql/wsrep_mysqld.cc:817-825`,
  verified).
- **Clean-state write**: on graceful close the provider persists grastate with the real
  seqno (SavedState written at `shift_to_CLOSED`, `galera/src/saved_state.cpp` — in-place
  non-atomic rewrite, write failures are warnings; PXC-only early-out at :216-228 skips
  rewriting "unchanged" values). gvwstate.dat is deleted on graceful close
  (`gcomm/src/pc.cpp:262`). A graceful shutdown that is SIGKILLed mid-drain loses the
  clean grastate → next start needs wsrep-recover and possibly SST.
- Systemd context: `TimeoutStopSec` default 90s minus the 10s sleep leaves ≤80s for
  flush+close before SIGKILL (sut-analysis §8.5).

## Failure scenario

1. **Wedge**: under write load + a fault that stalls one applier (network hang against the
   group during commit-order wait, or the PXC-4844-class stale-Diagnostics_area monitor
   leak), SIGTERM arrives; `wsrep_wait_appliers_close` loops forever logging "Waiting for
   (N) applier thread(s)". The node is half-shut: SQL port closed, group membership
   lingering, supervisor eventually SIGKILLs → unclean state → SST on restart — or with no
   supervisor timeout, a permanent zombie.
2. **Silent SST tax**: with default container grace, every stop is unclean; a rolling
   restart of a 3-node cluster degenerates into serial full SSTs — availability erosion
   that looks like "slow restarts", root cause the 10s sleep. The property makes this
   visible.

## Suggested implementation

- **Workload-side (primary)**: the workload (or harness) initiates graceful stops with a
  generous grace (>> 10s + drain time, e.g. 120s+) so SIGKILL doesn't mask the SUT's own
  behavior. Assert `Always`: "a node given SIGTERM/SHUTDOWN exits (process gone) within T
  seconds" (T set from pxc_maint_transition_period + expected drain, e.g. 90s under
  quiesced faults).
- **Companion check (workload)**: after a graceful stop + restart cycle with bounded
  intervening writes (gcache 128M default retains them), the rejoin is IST, not SST —
  observable from the joiner's `wsrep_local_state_comment` never showing SST /
  `wsrep_cluster_status` timeline, or (better) the SUT-side markers from
  `restarted-node-rejoins-synced`. Expressed as `Sometimes`("graceful restart rejoined via
  IST") at minimum — an Always form needs the gcache-coverage precondition made precise
  first (see open question).
- **Sometimes markers (missing, distinct outcomes)**:
  - "shutdown applier-drain loop exceeded 30s" (danger state inside
    `wsrep_wait_appliers_close` — a counter on the sleep(1) loop);
  - "graceful shutdown persisted grastate with seqno != -1" (SavedState write on
    shift_to_CLOSED) — proves the clean path executed;
  - "graceful shutdown completed under active write load" (workload-side).

## Assertion type

Liveness — workload-side `Always` on the bounded-exit condition. The IST-rejoin companion
is a separate `Sometimes` (promotable to Always once the gcache precondition is pinned).

## Fault requirements

No injected fault types strictly required (the workload drives shutdowns), but the property
is only interesting *with* concurrent network faults/hangs (default-on) stressing the
drain. **Harness requirement (flag): container stop grace must exceed
pxc_maint_transition_period + drain bound, otherwise every result is contaminated by
SIGKILL; alternatively set pxc_maint_transition_period=0 in one variant.** Node
termination faults not needed.

## Confidence

High — both unbounded constructs read directly; the applier-wedge family that would trip
this is the #1 recurring bug pattern C in the ticket history.

## Open questions

- What is the correct exit-time bound T under quiesced faults? `(partial: lower bound =
  pxc_maint_transition_period (10s) + applier drain; needs one measurement in the harness
  — drain of a 100-writeset queue. Too-tight T makes false positives; too-loose hides
  wedges.)`

Resolved (see Investigation Log):

- **IST-on-rejoin can be promoted to Always under an explicit write budget**: the donor
  gate is `joiner_seqno >= lowest_cached_seqno + safety_gap` where
  `safety_gap = min(max_cached_range/128, 1M)` (`gcs_group.cpp:1788-1846`) — bounded
  writes alone aren't enough, the budget must clear that ~0.8% margin, plus clean
  grastate (seqno != -1), matching uuid, and an eligible donor. With those enforced, the
  always-SST regression (grastate clean-write failure) becomes directly detectable.
- **SQL `SHUTDOWN` takes the exact same 10s sleep path as SIGTERM** — there is no faster
  path; use `pxc_maint_transition_period=0` in one variant instead.

### Investigation Log

#### Does SQL `SHUTDOWN` take the same pxc_maint sleep path as SIGTERM?

- Examined: `sql/sql_parse.cc:3080-3100` (shutdown helper for SQLCOM_SHUTDOWN),
  `sql/mysqld.cc:2799-2825` (`kill_mysql`), `sql/mysqld.cc:4385-4404` (signal-thread
  SIGTERM/SIGQUIT case).
- Found: the SHUTDOWN statement handler calls `kill_mysql()` (`sql_parse.cc:3095`);
  `kill_mysql` does `pthread_kill(signal_thread_id.thread, SIGTERM)` (`mysqld.cc:2819`);
  the signal thread's SIGTERM case unconditionally (when WSREP_ON) sets
  `pxc_maint_mode = SHUTDOWN` and `sleep(pxc_maint_transition_period)`
  (`mysqld.cc:4395-4403`).
- Conclusion: resolved — identical path, same 10s sleep; the workload cannot pick a
  faster graceful route. Variant knob: set `pxc_maint_transition_period=0`.

#### Precise precondition for guaranteed IST on rejoin — can IST be asserted Always?

- Examined: `percona-xtradb-cluster-galera/gcs/src/gcs_group.cpp:1788-1846`
  (`group_find_ist_donor`), surrounding donor-by-name/by-state helpers,
  `replicator_smm_stats.cpp` (`local_cached_downto` export from `gcache_.seqno_min()`).
- Found: donor selection refuses IST when `ist_seqno < safe_ist_seqno` where
  `safe_ist_seqno = lowest_cached_seqno + safety_gap`,
  `safety_gap = min((conf_seqno - lowest_cached_seqno) >> 7, 1<<20)`; the gap is waived
  only for ist-only requests (PXC `ist_only` branch). Also requires
  `lowest_cached_seqno != GCS_SEQNO_ILL` (some node actually has a cache) plus the
  standard preconditions: joiner grastate uuid matches and seqno != -1 (clean shutdown
  wrote SavedState).
- Not found: any guarantee tying "bounded write volume" alone to IST — the safety gap
  means a joiner within the cached range but inside the ~0.8% margin still gets SST.
- Conclusion: resolved (mechanism pinned) — Always-promotion is legitimate iff the
  workload enforces: (a) stop was graceful (grastate seqno valid), (b) intervening
  writes ≤ gcache coverage minus safety_gap on at least one surviving donor (observable
  via `wsrep_local_cached_downto`), (c) no donor restarted in the window. Sizing the
  concrete write budget for the harness's gcache (default 128M) is a workload-time
  calculation, not a further code question.

## Synthesis refinement (2026-09-10)

PROMOTED to the v1 top-10: every v1 restart-driven exploration depends on graceful shutdown completing — first-tranche both as a property and as the harness stop-grace configuration guard. Exit-bound pinned only after the per-tier calibration run.
