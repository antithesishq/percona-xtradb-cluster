---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# failed-state-transfer-node-rejoins — A node whose state transfer failed eventually rejoins (no crash loop, no orphan blockage)

**Slug:** `failed-state-transfer-node-rejoins`
**Type:** Liveness
**Confidence:** High on the individual failure mechanisms (each read in code); Medium on
which one dominates in practice
**Assertion type:** `Sometimes(cond)` — liveness: the meaningful condition "a node that
previously aborted/was killed during a state transfer reaches Synced" must become true at
least once per run. Paired with a second `Sometimes` ("a state transfer was interrupted")
so a never-fired rejoin assertion is distinguishable from the scenario never occurring.

## Property

A node whose state transfer (SST or IST) fails — donor death, network stall, joiner kill,
IST watchdog abort — eventually rejoins the cluster and reaches Synced after restart, once
faults heal: no crash→recover→crash loop, no restart permanently blocked by orphaned SST
processes, no indefinite wait.

## Failure mechanisms this must survive (code evidence, verified)

**IST watchdog abort (self-inflicted, network-fault-triggerable):**
- Each `recv_ordered` on the IST socket is wrapped by a `SocketWatchdog` with hardcoded
  `timeoutMs = 10000` (`percona-xtradb-cluster-galera/galera/src/ist.cpp:356-364`,
  `:503-513`); expiry shuts the socket down → `recv_IST` catches, logs "Receiving IST
  failed, node restart required", calls `st_.mark_corrupt(force_sst_after_inconsistency_)`
  and **`abort()`** (`replicator_str.cpp:1678-1718`). No fallback to SST in-process; no
  config knob for the 10s found.
- With the shipped default `repl.force_sst_after_inconsistency = no`
  (`replicator_smm_params.cpp:50`), `mark_corrupt(false)` still writes
  `UUID_UNDEFINED:-1` to grastate (`saved_state.cpp:298-317`) → next start requests full
  SST. **But** that write is warnings-only (`write_file` void-return); if it fails, the
  node restarts with the same position, ISTs again from the same stalled donor, and aborts
  again — the crash loop (PXC-4845 family, open question 24 in the SUT analysis).

**Donor-side death / stall:**
- Joiner's `sst_joiner_thread` blocks on untimed `my_fgets` (`sql/wsrep_sst.cc:634,:694`);
  mysqld's SST wait is unbounded (`while(!sst_received_)`). Bash-side bounds: donor 10s
  socat connect, joiner 100s metadata, 120s idle watchdog
  (`wsrep_sst_common.sh:1189-1193`). Donor retry loop re-streams up to 30× but the
  joiner's `socat -u TCP-LISTEN` accepts exactly ONE connection — retries connect to
  nothing (`wsrep_sst.cc:1462-1481` + script).
- Unbounded STR retry: `send_state_request` loops `usleep(1s)` forever on EAGAIN/ENOTCONN
  (`replicator_str.cpp:955-1050`); gives up only when the local monitor window (65536)
  fills → -EDEADLK "Application must be restarted".

**Orphaned process trees blocking the retry:**
- `posix_spawn` path sets no PDEATHSIG (`sql/wsrep_utils.cc:573-650`) → SIGKILL/OOM of
  mysqld leaves socat/xtrabackup/hidden post-processing mysqld alive, holding port 4444
  and datadir locks. The supervised restart's next SST cannot bind the listener; the
  hidden mysqld holds InnoDB locks on the datadir.
- `wsrep_sst_cancel` sends SIGTERM to the process group only, never verified, no SIGKILL
  escalation (`sql/wsrep_utils.cc:922-938`); the joiner script's SIGTERM trap does not
  exit (`wsrep_sst_xtrabackup-v2.sh:953-957`).

**NBO interaction (deterministic joiner abort):**
- A join while any NBO is in flight: joiner NULLs the SST request
  (`replicator_str.cpp:874-882`); donor returns -EAGAIN (`gcs.cpp` donor path) or
  -ENODATA → joiner `abort()` (`replicator_str.cpp:1004-1011`).

**Supervision reality check:** shipped systemd units use `Restart=on-abort` +
`RestartPreventExitStatus=SIGABRT` — the field default is that every one of these
`abort()`s leaves the node **permanently down**. The harness must choose restart policy
deliberately; this property assumes restart-on-abort supervision (and thereby also tests
what the field would see if operators restart by hand).

## Failure scenario (Antithesis recipe)

1. 3-node cluster under write load; force a node to need IST/SST (kill + restart, or
   partition long enough to fall behind, with small gcache to force SST variants).
2. During the transfer: stall the donor >10s (network fault or SIGSTOP node-hang — both
   available by default) → IST watchdog abort; or kill -9 the joiner/donor mid-SST.
3. Heal faults; supervisor restarts aborted/killed nodes.
4. Assert the previously-failed node reaches `wsrep_local_state = 4 (Synced)` and stays
   there; track abort counts per node to catch loops.

## Invariant (concrete checks)

- `assert_sometimes(state transfer observed to fail — joiner abort during IST/SST, donor
  death mid-transfer, "state transfer interruption explored")` (exploration guard).
- `assert_sometimes(node with a prior failed state transfer reaches Synced,
  "failed-state-transfer node rejoins")` — the liveness core.
- Companion boundedness (workload-side bookkeeping, reported as `Always`):
  `assert_always(consecutive state-transfer aborts on one node without an intervening
  successful transfer <= 3, "no state-transfer crash loop")` — 3 chosen to tolerate one
  unlucky re-abort while faults are still active; evaluate only in fault-free windows.
- Companion: after joiner kill + restart, port 4444 is bindable by the new SST within the
  recovery bound ("no orphaned SST listener blocks rejoin").

## Instrumentation suggestions (all missing)

- **SUT-side `Sometimes`** at `replicator_str.cpp:1714` (recv_IST catch block): "IST failed,
  node restart required" — makes the abort reason machine-visible and a replay anchor
  (currently only a log line followed by abort with cores suppressed by `gu_abort`).
- **SUT-side `Always`** after `mark_corrupt` in the same block: re-read grastate and assert
  it now demands SST — closes the warnings-only-write loop-hole that turns one abort into a
  crash loop.
- **SUT-side `Sometimes`** for each `sst_received` error-code branch
  (`replicator_str.cpp:78-202`: -ECANCELED deferred shutdown, -EAGAIN restore+abort,
  -EPIPE abort) — retry-outcome visibility; today the workload cannot distinguish which
  fallback the node took.
- Harness: override `gu_abort`'s core suppression; give SST children PDEATHSIG or run a
  reaper so orphan-tree findings are attributable.

## Fault requirements

- IST-abort arm: **network faults or node hang only — available by default** (donor stall
  >10s). No termination needed to *trigger*; but the node self-aborts, so the harness must
  **restart aborted processes** (supervision policy, not fault injection — flag that the
  shipped systemd policy would NOT).
- SST-kill and orphan-tree arms: **REQUIRE node termination (kill -9)** — flag to
  environment team.
- Small `gcache.size` variant to make SST (not just IST) reachable frequently.

## Open Questions

None — all three resolved (see Investigation Log). Consequences for the property:

- Deterministic crash-loop candidates with the position left intact DO exist beyond the
  disk-write-failure case: `:946` (state-request preparation failure — e.g. NBO-in-flight
  nulls the SST request and IST receiver prep fails, or an orphaned listener squats the IST
  port), `:1010` (-ENODATA on an IST-only request while the donor's gcache advances under
  load — position intentionally preserved "for the next attempt"), and `:177` (-EAGAIN
  restores the startup-time saved state). These justify treating the loop-bound assertion
  as a hard fail once faults are healed and NBOs have completed.
- The healthy-loaded-cluster watchdog bug-hypothesis is downgraded: the donor's IST sender
  cannot be stalled by FC pauses (dedicated thread streaming pre-stored gcache buffers), so
  firing the joiner watchdog requires a genuine >10s per-message stall (network fault, or
  donor disk stall on gcache page reads).
- No operator-equivalent cleanup step is needed in the harness for `sst_in_progress` debris.

### Investigation Log

#### Which `replicator_str.cpp` abort sites don't invalidate the position?

- Examined: every `abort()` in `replicator_str.cpp` (`:177, :946, :1010, :1095, :1290,
  :1324, :1375, :1420, :1449, :1717`) with surrounding state-file writes; `saved_state.cpp`
  restore/mark semantics.
- Found (position preserved → loop candidates): `:177` -EAGAIN restores the
  first-constructor saved state, -EPIPE writes nothing; `:946` StateRequest preparation
  failure aborts with no state write (reachable when `sst_req_len == 0` — NBO in flight —
  and IST receiver prep throws, e.g. bind failure on a squatted port: deterministic while
  the cause persists); `:1010` -ENODATA (IST-only request, donor moved on) deliberately
  keeps the position ("we can save it for the next attempt"); `:1290` graceful-shutdown
  restore (benign, operator-driven); `:1449` uuid sanity mismatch writes the current
  position safe (can't-happen path; would loop if the lineage mismatch persists).
- Found (position invalidated → loop only on failed write): `:1095` send failure marks
  unsafe (UNDEFINED:-1 → forces SST); `:1324` wrong-uuid received — writes the received
  state, whose uuid mismatch itself forces SST; `:1375` rolling-upgrade refusal
  (config-determined); `:1420` corrupt-unrecoverable; `:1717` recv_IST failure →
  mark_corrupt.
- Conclusion: RESOLVED — the loop is NOT closed solely by mark_corrupt; `:946` and `:1010`
  are targetable deterministic-loop variants (both NBO-correlated), `:177`/-EAGAIN loops
  while the donor-side SST pre-check failure persists. Loop-bound assertion upgraded to
  hard fail in fault-free, NBO-free windows.

#### Is the 10s IST watchdog per-message or effectively whole-transfer under donor FC pause?

- Examined: `galera/src/ist.cpp:355-436` (watchdog), `:503-513` (start/stop wraps each
  `recv_ordered`), `:911-1000` (Sender::send streams via `gcache_.seqno_get_buffers`),
  `:1001-1080` (AsyncSender runs on its own thread, `run_async_sender`/`gu_thread_create`).
- Found: per-message — the timer restarts on every received action. The donor-side sender
  is a dedicated thread reading already-stored gcache buffers; it takes no part in flow
  control and never waits for appliers, so a donor FC pause cannot starve the stream.
  Joiner-side, the watchdog is stopped while the receiver pushes into the apply queue, so
  slow joiner apply doesn't fire it either.
- Not found: any FC-coupled wait in the sender path; any config knob for the 10s.
- Conclusion: RESOLVED — a healthy-but-loaded cluster cannot fire the joiner watchdog via
  FC; residual triggers are network stalls >10s (default faults) and donor disk stalls on
  gcache page-file reads (disk throttle). Trigger condition in the property stays
  fault-scoped.

#### Does `sst_in_progress` debris confuse the retry?

- Examined: `scripts/wsrep_sst_xtrabackup-v2.sh:2167-2168` (stale marker → warning +
  re-touch), `:960-1001` (cleanup_joiner removes it only on estatus==0),
  `scripts/wsrep_sst_common.sh:196` (path), `support-files/mysql.server.sh:350-386` and
  `build-ps/rpm/mysql-systemd:50-115` (+ deb twin) — wrapper handling.
- Found: the SST script itself is never blocked by debris (warn + overwrite); mysqld ignores
  the file (sql `wsrep_sst_in_progress()` is a runtime flag, not the file). Wrappers use it
  two ways: extending the startup wait, and — when `$datadir/mysql` is missing — wiping the
  datadir and re-running `mysqld --initialize` (fresh node, full SST on join).
- Conclusion: RESOLVED — no operator-equivalent cleanup needed in the harness; the
  wipe-and-reinit wrapper path is a deployment behavior worth knowing when interpreting
  rejoin traces (node may come back with a fresh identity).

## Synthesis refinement (2026-09-10)

PROMOTED to the v1 top-10: the 10s IST-watchdog abort arm is v1's only default-fault path to ungraceful process death (with harness restart-on-abort supervision), making this the gateway to all incidental crash-recovery coverage before termination faults/kill channel land. Kill arms unchanged (+kill).
