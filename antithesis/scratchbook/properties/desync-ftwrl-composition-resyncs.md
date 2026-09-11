---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# desync-ftwrl-composition-resyncs — Operator desync/FTWRL/backup compositions always return; the node ends Synced with balanced accounting

**Provenance: evaluation gap-fill (synthesis Gap 3 — coverage #3).** Sibling of
`ftwrl-backup-quiescent-or-fails` (which owns the snapshot-quiescence *safety* contract);
this property owns the *liveness/composition* contract of the same operator surface:
FTWRL statements return, UNLOCK restores, desync toggles compose, and no interleaving
leaves the node permanently desynced or the FTWRL thread wedged.

**Type:** Liveness | **Assertion:** workload `Always` (every FTWRL/UNLOCK/desync-toggle
statement returns — success or error — within bound T; at quiesced checkpoints with all
operator actions released: `wsrep_local_state=4` ∧ `wsrep_desync_count=0` ∧
`wsrep_desync=OFF`; intent leg: `wsrep_desync=ON` at quiesce ⇒ `wsrep_local_state=2`) +
`Sometimes` companions | **Confidence:** High on the hang and swallow mechanisms (in-code,
some with in-code confessions); Medium on findability of the non-consecutive-seqno hang
(needs a desync/pause race that Antithesis timing exploration is well-suited to).

## Claim under test

Any interleaving of the operator backup/maintenance actions — `SET GLOBAL wsrep_desync=
ON/OFF`, `LOCK INSTANCE FOR BACKUP`/`UNLOCK INSTANCE`, `FLUSH TABLES WITH READ LOCK`/
`UNLOCK TABLES` — with each other and with replication load, view changes, and donations:

1. every statement terminates within a bound (no FTWRL wedged forever inside the provider
   pause machinery);
2. after all actions are released, the node returns to Synced with `wsrep_desync_count=0`
   (no permanent desync);
3. operator intent survives the composition: a user-desynced node (`wsrep_desync=ON`) is
   never silently resynced by an intervening FTWRL/UNLOCK cycle, and vice versa a
   released node is never left desynced.

## Code paths (verified at f9ecb3e)

- **The non-consecutive-seqno hang inside `try_desync_and_pause`**
  (`percona-xtradb-cluster-galera/galera/src/replicator_smm.cpp:3419-3511`): after
  guarded checks (`would_block` `:3425-3428`, drained-monitor checks `:3444-3459` — all
  bail cleanly with `WSREP_SEQNO_UNDEFINED`), it calls `gcs_.desync(seqno_l)` and
  *assumes* `seqno_l == local_seqno+1`; on mismatch it only **warns**
  (`:3466-3471`; the assert is compiled out in release), then does
  `local_monitor_.leave(lo); local_monitor_.enter(lo2)` at `seqno_l` (`:3474-3476`,
  in-code "this part is ugly"). Entering the local monitor at a non-consecutive seqno
  blocks until every seqno in the gap has left — under concurrent local actions that gap
  may never fill → **FTWRL blocks indefinitely inside one "non-blocking" attempt**, past
  the 30s retry timeout's reach (the timeout only bounds the outer 100ms retry loop,
  `wsrep-lib/src/server_state.cpp:693-719`).
- **Plain-FTWRL untimed pause**: without `LOCK INSTANCE FOR BACKUP`, a Synced node's
  FTWRL takes `desync_and_pause()` → `pause()` which holds the local monitor and
  `drain_monitors(upto)` **untimed** (`replicator_smm.cpp:3385-3416`) — the
  deadlock-avoidance retry machinery protects only the backup-locked flow
  (`sql/lock.cc:1236-1273`); plain FTWRL retains the documented indefinite-block shape
  (e.g. vs an applier stuck on anything).
- **Swallowed resync failure**: `resume_and_resync`
  (`wsrep-lib/src/server_state.cpp:721-742`) catches, logs "Resume and resync failed,
  server may have to be restarted", and returns void → `UNLOCK TABLES` reports success
  while the node stays desynced forever. Called from `unlock_global_read_lock`
  (`sql/lock.cc:1148-1151`, only when Synced-at-lock-time and paused; non-synced path
  `:1143-1147` resumes without resync).
- **Desync bookkeeping bypass**: `SET GLOBAL wsrep_desync` calls `provider().desync()/
  resync()` directly in the *check* function (`sql/wsrep_var.cc:692-743`; update fn no-op
  `:745`), bypassing wsrep-lib's `desync_count_` — so FTWRL's pause path and the user
  toggle keep **disagreeing counters** (three bookkeepers: gcs_group per-node count =
  status var `wsrep_desync_count`, wsrep-lib `desync_count_`, the sysvar). Toggling
  wsrep_desync while the provider is paused is refused (ER_UNKNOWN_ERROR, `:716-723` —
  verified in `donor-returns-to-synced`'s investigation log), which narrows but does not
  eliminate the interleavings.
- **Leak oracle**: `wsrep_desync_count` status variable (gcs-group count exported
  end-to-end, `gcs_group.cpp:2445-2467` → `replicator_smm_stats.cpp:434-470`; established
  in `donor-returns-to-synced`).
- Permanent-desync mode documented in-code for the donation composition:
  `gcs/src/gcs.cpp:2743-2755` (owned by `donor-returns-to-synced`; this property adds the
  FTWRL/user-toggle drivers to the same oracle).

## Failure scenario

Backup actor runs `LOCK INSTANCE FOR BACKUP; FLUSH TABLES WITH READ LOCK` on node X while
the workload toggles `wsrep_desync` and a view change lands mid-attempt. `gcs_.desync()`
returns a seqno that is not `local_seqno+1` (warned, not handled); the FTWRL thread parks
in `local_monitor_.enter(lo2)` on a gap that never fills. The backup job hangs forever
holding the GRL; every later FTWRL/desync statement on X queues behind it; X eventually
trips cluster-wide flow control. Alternatively: everything returns, but
`resume_and_resync` swallowed a provider error and X serves ever-staler reads as a
permanent invisible desync (state 2, `wsrep_ready=ON`).

## Suggested implementation

Plain SQL, fault-free core:

- **Workload `Always` (statement liveness)**: run every operator statement (FTWRL,
  UNLOCK TABLES, SET wsrep_desync, LOCK/UNLOCK INSTANCE) with a client-side stopwatch;
  each must return within T (calibrate; components: `wsrep_desync_pause_retry_timeout`
  30s default + slack — no numeric bound pinned before a calibration run). A statement
  exceeding T is a violation even if the server is otherwise healthy.
- **Workload `Always` (quiesced composition end-state)**: at quiesced checkpoints with
  the workload's operator ledger empty (all toggles OFF, no FTWRL held, no donation in
  progress): `wsrep_local_state=4` ∧ `wsrep_desync_count=0` ∧ `wsrep_desync=OFF`. With
  the ledger showing `wsrep_desync=ON` deliberately held: `wsrep_local_state=2` (intent
  honored — an FTWRL cycle interleaved earlier must not have resynced it).
- **`Sometimes` companions**: (a) FTWRL was issued while `wsrep_desync=ON` (composition
  actually exercised); (b) a desync toggle was refused with ER_UNKNOWN_ERROR during a
  pause (the guard fired); (c) FTWRL overlapped an SST/IST donation on the same node
  (the three-way composition — donor + user desync + FTWRL — reaching the
  `gcs_join` out-of-order territory of `donor-returns-to-synced`); (d) the
  "GCS desync returned seqno" warning line appeared (`replicator_smm.cpp:3469-3471`) —
  the property's sharpest precursor, via the shared log-scan layer.
- **SUT-side (missing, v2)**: `Unreachable` at the `resume_and_resync` catch block
  (`server_state.cpp:738-741`) — the code's own "server may have to be restarted" is an
  admission this path is a danger state; and an `Always` at
  `replicator_smm.cpp:3468` (`seqno_l == local_seqno+1`) restoring the compiled-out
  assert non-fatally.

## Fault / config / phase flags

- Phase tag: **v1-assert**, fault-free core; network faults (default) supply the view
  changes that race desync/JOIN messages; no kill channel needed.
- Config: none (all actions are plain SQL / dynamic globals).
- Workload fencing: the backup/desync actor targets one node at a time with bounded hold
  times and pairs every ON with an OFF (Shared conventions — otherwise it fabricates
  failures in `flow-control-pause-releases`, `synced-node-recv-queue-bounded` (desync
  disables FC → unbounded queue by design), and the checksum oracle's Synced-node scope).
- Coordination: `donor-returns-to-synced` owns donation-driven desync and already asserts
  the same quiesced end-state — shared oracle, distinct assertion messages and distinct
  drivers; `ftwrl-backup-quiescent-or-fails` owns the frozen-snapshot safety half;
  `flow-control-pause-releases` owns the cluster-wide freeze this property's wedge would
  eventually cause (a wedge should fail THIS property's statement-liveness clause first,
  giving a sharper attribution).

## Open Questions

- Is the non-consecutive-seqno `enter(lo2)` gap actually fillable-in-principle in all
  legal interleavings (i.e., the hang requires a lost/blocked local action), or does a
  specific desync-vs-local-replicate race produce a structurally unfillable gap? Answer
  changes whether the hang is "rare timing" or "deterministic once the race lands" —
  affects expected findability and triage of a red statement-liveness clause.
- What T is legitimate for plain FTWRL (no backup lock) under load? Its pause path is
  untimed by design; under sustained apply traffic the drain can be legitimately long.
  May need the invariant scoped to "FTWRL returns within T after apply traffic to the
  node quiesces" or the plain-FTWRL leg demoted to the backup-locked flow only.
  `(partial: the untimed drain is confirmed code; what unbounded-but-progressing looks
  like in practice needs the calibration run)`

### Investigation Log

#### What T is legitimate for plain FTWRL (no backup lock) under load?

- Examined: `galera/src/replicator_smm.cpp:3385-3416` (`desync_and_pause` → `pause()`
  monitor drain), `sql/lock.cc:1236-1273` (retry machinery scope),
  `wsrep-lib/src/server_state.cpp:693-719` (the 100ms/30s retry loop).
- Found: the plain-FTWRL pause path drains monitors untimed by design; the
  `wsrep_desync_pause_retry_timeout` bound protects only the backup-locked
  (`try_desync_and_pause`) flow.
- Not found: an empirical envelope for a legitimate untimed drain under sustained apply
  traffic — distinguishing "unbounded but progressing" from a wedge needs the calibration
  run (per Shared conventions, no numeric bound pinned before calibration).
- Conclusion: tagged `(partial: the untimed drain is confirmed code; the envelope is
  calibration)`.
