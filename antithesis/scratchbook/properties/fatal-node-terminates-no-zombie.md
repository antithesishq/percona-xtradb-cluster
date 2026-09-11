# fatal-node-terminates-no-zombie

**Property:** A node that enters a fatal path (fatal signal handler, gu_abort,
unireg_abort) actually terminates: the mysqld process exits within a bound. A node is never
left "neither up nor down" — process alive, ports held, no replication progress, no crash
artifacts — after declaring itself fatally broken.

**Confidence:** High on the self-deadlock mechanism (verified: the fatal signal handler
takes a blocking mutex). Medium on how often Antithesis will drive a crash while
LOCK_wsrep_sst is held — SST windows are long and crash-dense, so plausible.

## Why this is wildcard territory

Every focus assumes failure is binary: a node is up or it crashed. The verified mechanism
below creates a third state — a half-dead mysqld — which silently invalidates *other*
properties' oracles (membership counts, restart assumptions, "node down" detection) and, in
production, defeats supervision: systemd sees a live main PID and does nothing, the proxy
sees an open port. This is also the "diagnostics/error artifacts survive failures" class:
the same code region suppresses core dumps.

## Code evidence (verified at commit f9ecb3e)

1. **Blocking mutex inside the fatal signal handler** — `sql/signal_handler.cc:402-416`:
   `handle_fatal_signal()` → `wsrep_handle_fatal_signal(sig)`;
   `sql/wsrep_sst.cc:591`: `wsrep_handle_fatal_signal() { wsrep_sst_cancel(false); }`;
   `sql/wsrep_sst.cc:553-554`: `wsrep_sst_cancel` begins with
   `if (mysql_mutex_lock(&LOCK_wsrep_sst)) abort();` — an ordinary blocking lock. If the
   crashing thread (or any live thread that will never release it — e.g. one blocked in the
   untimed SST waits, `wsrep_sst.cc:634/:694/:1471-1512`) holds `LOCK_wsrep_sst`, the
   handler never returns: no crash report, no exit, no restart. The handler also calls
   `WSREP_INFO`/`sst_process->terminate()` — non-async-signal-safe work compounding the
   hazard (malloc locks, etc.).
2. **Re-entry guard makes it a one-shot** — `signal_handler.cc:404-409`: a second fatal
   signal during the wedged handler does `_exit` only if it arrives on a thread that runs
   the handler again; a single-threaded wedge just stays wedged.
3. **Crash artifacts suppressed** — `galerautils/src/gu_abort.c:29-58`: `gu_abort()` sets
   core limit 0 + `PR_SET_DUMPABLE 0` before SIGABRT — Galera-initiated aborts leave no
   core by design (harness should override; sut-analysis §12).
4. **Supervision blind spots** — shipped units: `Restart=on-abort` +
   `RestartPreventExitStatus=SIGABRT` (and exit 1 not restarted): even *clean* fatal exits
   are deliberately not restarted; a *wedged* handler is invisible to exit-status logic
   entirely. The zombie state is thus stable for the rest of the run.
5. SST is the crash-dense window: kill/fault during SST is a first-class Antithesis
   scenario for other properties, and SST code paths are exactly where LOCK_wsrep_sst is
   held (`wsrep_sst.cc:267,554,585` and the sst_joiner/donor threads).

## Failure scenario

Joiner is mid-SST; a native assert/segv/gu_abort fires on a thread while another thread
holds LOCK_wsrep_sst inside an untimed `my_fgets` wait (donor died — default-on network
fault). The fatal handler blocks on the mutex. The process stays alive indefinitely: port
3306/4567 bound or half-bound, no writesets applied, no crash log tail, no restart. Peers
may or may not evict it depending on whether gcomm threads are still servicing keepalives —
either way the cluster runs degraded with a ghost member and zero operator signal.

## Testable formulation

- `AlwaysOrUnreachable` (main): "whenever a node emits a fatal marker (fatal-signal banner
  from `print_fatal_signal`, 'Aborting' / gu_abort preamble, 'unallowed'-fatal WSREP_ERROR)
  in its error log, the mysqld process exits within T seconds (default T=60)."
  `AlwaysOrUnreachable` because the fatal path is optional — a clean run never enters it,
  and that must not fail the property; but any entry must satisfy prompt termination.
  Implemented by an external per-node watchdog (test composer sidecar): tail the error log
  for fatal markers, then poll the PID.
- `Unreachable` (sharper, SUT-side, missing): an Antithesis `unreachable("fatal handler
  blocked on LOCK_wsrep_sst")` fired by a watchdog thread... not implementable in-process
  post-crash; instead instrument *before* the lock: record handler-entry timestamp in a
  pre-allocated global; the sidecar asserts exit-within-T from that. Practical version
  remains the log-marker watchdog above.
- `Sometimes`: "a fatal path was entered while an SST was in progress" — confirms the
  dangerous overlap was explored (join fatal-marker detection with sst_in_progress
  observable/log lines).

## Instrumentation suggestions (all missing)

- Sidecar watchdog per node: fatal-marker tail + PID liveness + T-timer → workload-side
  `always` assertion (no SUT patch needed).
- Harness build: neutralize `gu_abort`'s core suppression (patch or LD_PRELOAD
  setrlimit/prctl no-ops) so every genuine abort leaves triage artifacts — this serves all
  properties, not just this one.
- Optional minimal SUT patch: replace the blocking lock in `wsrep_sst_cancel` with
  `trylock` when called from the signal path (that is the obvious *fix*; for testing we
  instead want the unpatched behavior + watchdog to detect it).

## Fault requirements

Needs fatal paths to fire. Default-on network faults during SST/IST already reach many
abort sites (§7.3 self-destruct inventory: -ENODATA joiner abort, IST watchdog abort,
gcs CONT-send abort...). Node-termination faults not required (the property is about what
happens *after* the SUT decides to die), but a debug/assert-enabled image variant multiplies
trigger frequency. Flag: if the platform's process-kill faults are enabled, exclude
SIGKILL-by-harness from the "fatal marker" definition — only SUT-initiated fatals count.

## Open questions

- Tuning T_long for the inverse zombie detector (raw-signal crashes only — see resolved
  marker inventory below) without false positives on legitimate long SSTs. `(partial:
  detector scope narrowed to markerless SIGSEGV/SIGBUS crashes; threshold needs one
  harness measurement, gated on SST-not-in-progress observables)`

Resolved (see Investigation Log):

- **Marker inventory**: abort-family fatals reliably log *before* the vulnerable
  wsrep_sst_cancel path — `unireg_abort` logs "Aborting" (ER_ABORTING) and flushes the
  error log first; `gu_abort` logs "Terminated." before `abort()`; InnoDB fatal asserts
  log before raising SIGABRT. Only raw fatal signals (SIGSEGV/SIGBUS with no preceding
  log) can wedge markerless — the inverse detector covers exactly that subclass.
- **gcomm keeps running while the handler is wedged**: gcomm has its own dedicated thread;
  the fatal handler wedges only the crashing thread → the zombie stays a live member
  answering keepalives (unless the crashing thread *was* the gcomm thread), recv queue
  grows, cluster-wide FC pause follows. Confirms cluster-wide impact — priority stands.
- **New wedge site found**: `unireg_abort` itself calls `wsrep_sst_cancel(true)`
  (`sql/mysqld.cc:2870`) in normal thread context — the same blocking `LOCK_wsrep_sst`
  acquisition — so clean-path aborts can also hang, but *after* the "Aborting" marker,
  so the log-marker watchdog catches them.

### Investigation Log

#### Which fatal-marker strings are reliably emitted before the handler can wedge?

- Examined: `sql/signal_handler.cc:402-421` (`handle_fatal_signal` ordering),
  `sql/mysqld.cc:2830-2870` (`unireg_abort`),
  `percona-xtradb-cluster-galera/galerautils/src/gu_abort.c:29-58`.
- Found: three fatal families with markers *preceding* the vulnerable code:
  (1) `unireg_abort`: `flush_error_log_messages()` then
  `LogErr(ERROR_LEVEL, ER_ABORTING)` at `mysqld.cc:2859` run *before*
  `wsrep_sst_cancel(true)` at `:2870` — marker guaranteed and flushed;
  (2) `gu_abort`: `gu_info("...: Terminated.")` (`gu_abort.c:46-49`) before `abort()`;
  the subsequent SIGABRT then enters `handle_fatal_signal` → `wsrep_handle_fatal_signal`;
  (3) InnoDB `ut_a`/`ib::fatal` print their assertion text before raising SIGABRT.
  For raw SIGSEGV/SIGBUS, `handle_fatal_signal` prints nothing before
  `wsrep_handle_fatal_signal(sig)` (`signal_handler.cc:416`); `print_fatal_signal` runs
  after (`:419-421`) — markerless wedge possible for this subclass only. Re-entry guard
  confirmed: a *second* fatal signal on any thread hits `s_handler_being_processed` and
  `_exit`s (`:404-409`).
- Also found (new): `unireg_abort` is a fourth entry into the blocking
  `LOCK_wsrep_sst` acquisition (`mysqld.cc:2870`), in normal thread context.
- Conclusion: resolved — the log-marker watchdog covers gu_abort/unireg_abort/assert
  fatals; the inverse zombie detector is required only for raw-signal crashes. T_long
  tuning remains `(partial)`.

#### Does gcomm keep answering keepalives while the handler is wedged?

- Examined: `percona-xtradb-cluster-galera/gcs/src/gcs_gcomm.cpp` — `GCommConn` class
  (:164-389), `GCommConn::connect` spawning a dedicated network thread via
  `gu_thread_create` (:461) that runs the asio event loop (`GCommConn::run`, :389).
- Found: EVS keepalive/liveness traffic is serviced by that dedicated gcomm thread. A
  fatal-signal handler wedged on `LOCK_wsrep_sst` blocks only the thread that received
  the signal; POSIX does not suspend other threads. So unless the crashing thread is the
  gcomm thread itself, the zombie continues to answer keepalives and remains a group
  member; the gcs recv queue backs up behind dead appliers → FC pause propagates
  cluster-wide.
- Not found: any code that halts the gcomm thread from the fatal handler (the handler
  only calls `wsrep_sst_cancel`, which targets the SST process).
- Conclusion: resolved at mechanism level — zombie stays a member with cluster-wide FC
  impact; the run confirms empirically via peers' `wsrep_cluster_size` vs the zombie's
  progress. Strengthens the property's priority rationale.
