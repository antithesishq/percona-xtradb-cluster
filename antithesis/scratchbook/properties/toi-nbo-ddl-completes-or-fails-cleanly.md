---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# toi-nbo-ddl-completes-or-fails-cleanly — Every TOI/NBO DDL reaches a terminal outcome; NBO never leaks the unsafe counter

**Provenance: evaluation gap-fill (synthesis Gap 2 — coverage #2).** The catalog covered
the TOI *safety* twin (`no-mdl-bf-bf-abort`: missed cert keys → MDL BF-BF suicide) and
shutdown (`graceful-shutdown-bounded`), but nothing owned DDL-*completion* liveness: a DDL
statement that neither completes nor fails, a permanently held TO slot, a wedged NBO, or
the NBO `unsafe_`-counter leak that silently converts every future restart into a full SST.

**Type:** Liveness | **Assertion:** workload `Always` (every issued TOI/NBO DDL reaches a
terminal outcome — success on all nodes or an error on the originator — within bound T;
post-episode probes: TO slot released, NBO unsafe balance restored) + `Sometimes`
companions | **Confidence:** High on the mechanisms (no-retry TOI, unbounded NBO end wait,
close-during-NBO self-declared hole, and the unsafe_ imbalance are all in-code); Medium on
the exact unsafe_ balance arithmetic across the three marking paths (open question below).

## Claim under test

A DDL statement issued under `wsrep_OSU_method=TOI` (default) or `NBO` (Tech Preview),
concurrent with DML load, view changes, and joins, either:

- completes on every node (schema converges), or
- fails on the originator with a client-visible error,

within a bounded time — and afterwards leaves no residue: no permanently held TO slot
(`wsrep_to_isolation` returns to 0), no NBO wedged between phase one and phase two, no node
that "must be re-initialized either by full SST or from backup", and — the NBO-specific
observable — the galera `unsafe_` counter returns to 0 so the node can persist a real
grastate position again.

## Code paths (verified at f9ecb3e; galera + wsrep-lib vendored in-tree)

### TOI lifecycle

- Entry: `wsrep_to_isolation_begin` `sql/wsrep_mysqld.cc:2993` → `wsrep_TOI_begin` `:2473`
  → `cs.enter_toi_local(...)` `:2557-2558` — called **without a deadline**;
  `poll_enter_toi` (`wsrep-lib/src/client_state.cpp:582-638`) treats a zero `wait_until`
  as immediately timed out, so certification failure is **a single attempt →
  `e_deadlock_error`** (PXC TOI does not retry; the NBO phase-two call `:2833-2836` DOES
  pass a `lock_wait_timeout` deadline — asymmetry).
- Begin-failure cleanup: `wsrep_TOI_begin_failed` `sql/wsrep_mysqld.cc:2403-2429` — if
  `leave_toi_local` fails, `unireg_abort(1)` (`:2428`); NBO twin `wsrep_NBO_begin_failed`
  `:2437-2464` (`:2463`). Abort-as-error-handling: a failed *cleanup* is node suicide.
- End: `wsrep_TOI_end` `:2622-2657` — decrements the global `int wsrep_to_isolation`
  (`:2623`; plain int, no lock, not a status variable). Increment sites `:2609` (TOI) and
  `:2778` (NBO phase one); NBO decrement only for local execution
  (`wsrep_NBO_end_phase_two` `:2887`, comment `:2874-2888` documents the invariant being
  hand-maintained across 3 functions).
- **Leaked-TO-slot observable**: `MYSQL_BIN_LOG::rotate` silently skips rotation while
  `wsrep_to_isolation > 0` (`sql/binlog.cc:7881-7887`, returns 0 — no error). A leaked
  slot = binary logs never rotate again. Workload probe: `FLUSH BINARY LOGS` at a
  quiesced checkpoint (no DDL in flight) must produce a new binlog file.
- Provider side: `to_isolation_begin` `galera/src/replicator_smm.cpp:1853-1926`
  (`percona-xtradb-cluster-galera/`) — monitor-entry failure is `gu_throw_fatal`
  (`:1898-1900` unrecognized retval; `:1919-1922` "unable to enter commit monitor") =
  process abort, not statement error.

### NBO lifecycle (Tech Preview — fenced workload phase per Shared conventions)

- Two phases: `wsrep_NBO_begin_phase_one` `sql/wsrep_mysqld.cc:2659`, end phase one
  `:2789`, `wsrep_NBO_begin_phase_two` `:2826`, `wsrep_NBO_end_phase_two` `:2863`. Remote
  apply spawns a dedicated NBO worker THD (`apply_nbo_begin`
  `sql/wsrep_high_priority_service.cc:706`).
- **Unbounded NBO end wait**: `wait_nbo_end` `replicator_smm.cpp:1762-1830` — after
  sending the NBO-end writeset, loops on `nbo_ctx->wait_ts()` (1s poll, `nbo.hpp:57-76`)
  forever; exits only on end-writeset arrival, view-change abort (resend), or node close.
- **Self-declared close-during-NBO hole** (`replicator_smm.cpp:1807-1812`): "Closing
  during nonblocking operation. Node will be left in inconsistent state and must be
  re-initialized either by full SST or from backup." → `WSREP_FATAL`.
- NBO-end send failure returns `WSREP_CONN_FAIL`/`WSREP_NODE_FAIL` to the client
  (`:1789-1802`) — the statement errors but peers still hold the NBO MDL + cert entry;
  what un-wedges them is exactly what this property probes (see open questions).
- **NBO cert-index clear vs MDL** (`sql/wsrep_mysqld.cc:3280-3296`, in-code "it is as it
  is"): NBO cert entries are cleared at 2nd-phase *begin* while MDL is still held; the
  workaround blocks the applier in the SQL layer (the `no-mdl-bf-bf-abort` NBO-wait
  carve-out). Target interleaving: conflicting DML certified during the phase transition.
- **NBO blocks SST**: joiner nulls its SST request while any NBO is ongoing
  (`replicator_str.cpp:870-884` "Node can receive IST only"); doc
  `doc/source/features/nbo.rst:18`. This is why the NBO phase must be fenced — ambient
  NBO deterministically fabricates SST/IST liveness failures in other properties.

### The unsafe_ counter (grastate persistence)

- `apply_trx` marks unsafe for `nbo_start()` (`replicator_smm.cpp:600-604`) AND again for
  `is_toi()` (`:617-620` — NBO-start writesets carry F_ISOLATION so both fire), but marks
  safe once (`:661-664`) → net +1 held for the NBO's duration. The balancing decrement is
  `to_isolation_end` `:1971` (`if (trx.nbo_start() == false) st_.mark_safe()`), reached
  when the NBO-end writeset is processed. Local originator path: `to_isolation_begin`
  `:1909-1912` mark_unsafe / `to_isolation_end` `:1966-1971` mark_safe.
- `SavedState::mark_unsafe` (`galera/src/saved_state.cpp:257-275`): the 0→1 transition
  **writes `WSREP_UUID_UNDEFINED:-1` to grastate.dat on disk**. `mark_safe` (`:277-297`)
  rewrites the real position only when the counter returns to 0. PXC-only early-out in
  `set()` (`:216-228`) skips the rewrite when values are unchanged — a previously *failed*
  write is never retried.
- **Leak consequence**: unsafe_ stuck > 0 → grastate stays `00000000-…:-1` → at next
  start the UUID mismatch invalidates the SE-checkpoint recovery path → **full SST on
  every restart, forever**, with zero errors logged at any point (the skip is
  `log_debug`). Invisible to `grastate-se-checkpoint-agreement`: seqno==-1 is one of its
  *legal* branches.

## What IS observable (grounding the invariant)

`unsafe_` has no status variable and mark_unsafe/mark_safe log at debug level only.
Grounded observables:

1. **Post-shutdown grastate content** (primary): after a graceful shutdown of a node
   whose last NBO/TOI completed or aborted (workload-known), grastate.dat must contain
   the real cluster UUID and seqno ≥ 0 — the R2 supervisor already reads grastate at
   every boot (boot-time JSONL probe); extend it to also parse the file at
   post-shutdown/pre-start. `seqno==-1` there = leak (or a torn shutdown — the supervisor
   knows which shutdowns were graceful).
2. **SST-vs-IST accounting**: a graceful-shutdown → restart cycle that results in full
   SST when IST was expected (gcache intact, position valid) is the leak's downstream
   signature — cross-check with `failed-state-transfer-node-rejoins` logs.
3. **Leaked TO slot**: `FLUSH BINARY LOGS` + `SHOW BINARY LOGS` probe (above).
4. **Wedged NBO**: `mysql.wsrep_streaming_log`-style visibility does not exist for NBO;
   use workload bookkeeping (every NBO DDL the workload issued must terminate) plus
   processlist stage strings (`wsrep: initiating TOI for write set`,
   `stage_wsrep_completed_TO_isolation`).

## Failure scenario

Workload issues `ALTER TABLE ... , ALGORITHM=INPLACE` under `wsrep_OSU_method=NBO` while
Antithesis partitions the originator between NBO phase one and phase two. The NBO-end send
returns -ENOTCONN → client gets an error, but the peers' NBO worker THDs keep MDL and the
cert NBO entry; a joiner arriving now gets "Node can receive IST only"; the originator's
`unsafe_` count never rebalances because the end writeset never applies. Cluster looks
healthy; every subsequent restart of the affected node forces a full SST, and the DDL is
neither applied everywhere nor rolled back anywhere.

## Suggested implementation

- **Workload `Always` (per DDL episode)**: issue TOI/NBO DDL with a unique marker
  (comment or table name); assert terminal outcome within bound T (calibrate — TOI cert
  is single-attempt so client errors are fast; NBO end can legitimately resend across a
  view change): either the client call returned success AND (quiesced) the schema change
  is visible on all Synced nodes (`SHOW CREATE TABLE` / information_schema comparison —
  feeds the checksum oracle's DDL+DML `Sometimes`), or it returned an error AND (quiesced)
  the schema is unchanged on all nodes. Divergent schema (applied on some nodes only) is
  a violation of this property *and* upstream evidence for `cross-node-row-equality`.
- **Workload `Always` (post-episode residue probes at quiesced checkpoints)**: FLUSH
  BINARY LOGS rotation probe; no session stuck > T in a wsrep TOI/NBO processlist stage.
- **Supervisor `Always`**: post-graceful-shutdown grastate has UUID != undefined and
  seqno != -1 when the workload's DDL ledger shows all NBOs terminated (extends the R2
  boot probe).
- **`Sometimes` companions (distinct messages)**: (a) an NBO DDL completed successfully
  under an active fault; (b) an NBO end was aborted by a view change and resent
  (`replicator_smm.cpp:1817-1822` path — log/debug observable via workload outcome:
  success after partition heal); (c) a TOI DDL failed cleanly on the originator with
  ER_LOCK_DEADLOCK while the cluster stayed size N; (d) a joiner was refused SST during
  an NBO ("Node can receive IST only" log line) — proves the fenced phase actually
  overlapped a join attempt (fence-internal only).
- **SUT-side (missing, v2)**: an SDK `Always` at `SavedState::mark_safe` asserting
  `count >= 0` (the existing `assert(count >= 0)` at `saved_state.cpp:283` is compiled
  out in release), and a `Sometimes` at the `wait_nbo_end` resend path.

## Fault / config / phase flags

- Phase tag: **v1-assert**; the NBO legs run in a **fenced workload phase/variant**
  (Shared conventions — ambient NBO aborts joiners; NBO is Tech Preview, so treat NBO
  results as a workload variant, not baseline). TOI legs run in the baseline workload
  (TOI is the default OSU method).
- Faults: network partitions/delays (default-on) drive the interesting interleavings
  (view change between NBO phases, partition during TOI apply). Node termination (+kill)
  enriches the close-during-NBO hole but is not required for the wedge/leak core.
- Config: none beyond defaults; `wsrep_OSU_method` is session-settable per statement.
- Coordination: `no-mdl-bf-bf-abort` owns the MDL BF-BF suicide (safety);
  `graceful-shutdown-bounded` owns shutdown liveness; `grastate-se-checkpoint-agreement`
  owns kill-window grastate/SE mismatches — this property owns DDL-completion liveness
  and the NBO unsafe-balance observable that grastate-agreement's legal seqno==-1 branch
  cannot see.

## Open Questions

- Exact unsafe_ balance arithmetic on *remote* nodes: the NBO-start apply nets +1
  (`:600-604` + `:617-620` − `:661-664`); is the remote balancing mark_safe reached via
  the NBO worker's `end_nbo_phase_two` → provider leave → `to_isolation_end:1971`, and
  does EVERY NBO abort path (phase-two failure, view-change abort of the worker, worker
  THD error) route through it? If any abort path skips it, the leak is reachable without
  any fault at all — which raises priority. `(partial: completion path traced to
  to_isolation_end:1971; abort paths not exhaustively traced)`
- After a failed NBO-end send (client got WSREP_CONN_FAIL), what legitimately un-wedges
  the peers' NBO workers — does the originator's client retry of the same DDL re-send the
  end writeset (wsrep_mysqld NBO retry logic), or is the NBO permanently wedged until the
  originator leaves the group? Determines whether the workload should retry the DDL
  before calling a wedge a violation.
- Bound T for NBO completion under churn: an NBO legitimately spans view changes
  (resend). Calibrate per Shared conventions (no numeric bound pinned before a
  calibration run per image flavor).

### Investigation Log

#### Do all NBO abort paths route through the balancing `mark_safe` (unsafe_ arithmetic on remote nodes)?

- Examined: `galera/src/replicator_smm.cpp` apply-side marking (`:600-604` nbo_start
  mark_unsafe, `:617-620` is_toi mark_unsafe, `:661-664` single mark_safe),
  `to_isolation_begin`/`to_isolation_end` (`:1909-1912`, `:1966-1971`, balancing decrement
  `:1971`), `wait_nbo_end` (`:1762-1830`), and the NBO phase functions in
  `sql/wsrep_mysqld.cc` (`:2659`, `:2789`, `:2826`, `:2863`).
- Found: the *completion* path is fully traced — a remote NBO-start apply nets +1 unsafe_,
  and the balancing `mark_safe` is reached via `to_isolation_end:1971` when the NBO-end
  writeset is processed.
- Not found: an exhaustive trace of the abort paths (phase-two failure, view-change abort
  of the NBO worker THD, worker error) confirming each one routes through
  `to_isolation_end`. If any abort path skips it, the grastate leak is reachable without
  faults, which would raise priority.
- Conclusion: tagged `(partial: completion path traced to to_isolation_end:1971; abort
  paths not exhaustively traced)`.

### Generator hygiene (2026-09-24)

Until 2026-09-24 this property's episode ledger was dominated by DDL that was
invalid before it left the client: `ddl.py` chose CREATE or DROP by coin flip
without consulting the schema. Those episodes reached `FAILED` with a shape
errno (1060/1061/1091/1826) and were terminal, so they never violated the
"no unresolved DDL" invariant — but they crowded the ledger and, worse, each
one bought a cluster-wide inconsistency vote. See
[cross-node-row-equality](cross-node-row-equality.md) for the measurement and
for the outage one of them caused under partition.

The generator now reads `information_schema` before each statement and emits
only the legal direction (schema-wide for foreign key names, which MySQL scopes
per schema rather than per table), so a `FAILED` episode with a shape errno is
from here on a *race* or a real finding, not the normal case. That makes the episode
ledger usable as evidence for the completion-liveness question this property
actually owns.
