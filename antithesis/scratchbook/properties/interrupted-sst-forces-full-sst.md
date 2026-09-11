---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# interrupted-sst-forces-full-sst — A joiner killed mid-SST never restarts claiming a safe position

**Slug:** `interrupted-sst-forces-full-sst`
**Type:** Safety
**Confidence:** Medium-High (enforcement chain read directly; the torn states require
specific windows whose reachability is the open question — which is exactly what Antithesis
is for)
**Assertion type:** `AlwaysOrUnreachable` — the check only evaluates on restarts that follow
an interrupted SST (a workload-dependent, fault-driven path); a run that never interrupts an
SST is acceptable, but every occurrence must satisfy the invariant.

## Property

After a state snapshot transfer is interrupted at any point past the "no way back" mark
(datadir wipe begun), the joiner's persisted recovery state demands a full state transfer on
next start: `grastate.dat` is absent, or has `uuid == UNDEFINED`, or `seqno == -1`. The node
never restarts presenting a "safe" pre-SST position over a wiped/half-transferred datadir,
and never rejoins via IST from a position it no longer holds.

## Enforcement chain and where it can break (code evidence, verified)

The intended design is sound and single-threaded through grastate:

1. `ReplicatorSMM::request_state_transfer`
   (`percona-xtradb-cluster-galera/galera/src/replicator_str.cpp:1141-1163`): when a
   non-trivial SST is required, `st_.mark_unsafe()` is called **before** the request is
   sent, and the first unsafe increment writes `UUID_UNDEFINED:-1` to disk
   (`saved_state.cpp:259-273`). PXC-4631 made this *conditional* — IST-only joins keep
   their safe position on purpose.
2. The SST script preserves grastate through the wipe (`cpat` pattern,
   `scripts/wsrep_sst_xtrabackup-v2.sh:836`) and disarms the safe-failure escape at the
   "no way back" point: `SAFE_EXIT_CODE_OVERRIDE=` cleared at `:2384` with the in-code
   comment "From now on, there is no way back: we will receive SST, or the node won't
   work", then the background transfer starts (`:2386`) and the datadir is wiped
   (`find ... -exec rm`, `:2394-2397`).

Links that can break the chain:

- **`mark_unsafe`'s disk write is warnings-only** (`SavedState::write_file` returns void;
  `fwrite`/`fflush`/`fsync` failures log and return, `saved_state.cpp:364-405`). A failed
  write means the process proceeds into SST believing the state is marked unsafe while the
  on-disk file still says safe. A subsequent kill -9 anywhere during the wipe/transfer
  leaves safe grastate + destroyed datadir. Nothing retries the failed write (the PXC-only
  early-out at `saved_state.cpp:216-228` skips rewrites when values look unchanged).
- **The -EAGAIN recovery path restores stale state**: `sst_received` with -EAGAIN →
  `restore_saved_state()` then `abort()` (`replicator_str.cpp:78-202` error dispatch;
  restore at `saved_state.cpp:246-249`). `restore_saved_state` restores
  **first-constructor-call** values (static `first_time_`, `saved_state.cpp:21,:171-176`)
  — if state changed since the very first provider init in this process lineage, the
  restored position is arbitrarily old.
- **Clone SST hand-writes grastate** with `cat <<EOF`, no fsync, no rename
  (`scripts/wsrep_sst_clone.sh:1199-1224`), deriving the position by grepping a log for
  `[WSREP] Recovered position` with an unevidenced fallback to the donor GTID
  (`RP_PURGED_EMERGENCY`). A kill during this window leaves a torn or wrong grastate that
  the startup scripts trust verbatim (`mysqld_safe.sh:273-279`).
- **The joiner script's SIGTERM trap does not exit** (`sig_joiner_cleanup`,
  `wsrep_sst_xtrabackup-v2.sh:953-957`) — a SIGTERM'd script *continues* against a
  half-transferred dir; `cleanup_joiner` keeps `sst_in_progress` on failure and
  `kill -KILL -$$` only when estatus>=128 (`:960-1000`).
- **mysqld SIGKILL orphans the whole SST tree**: the `posix_spawn` path sets only
  `SETPGROUP`, no `PR_SET_PDEATHSIG` (`sql/wsrep_utils.cc:573-650`; the fork path *does*
  set PDEATHSIG at `:462-470`) — after OOM-kill/SIGKILL of mysqld, socat/xtrabackup/the
  hidden post-processing mysqld keep mutating the datadir while a fresh supervised mysqld
  starts on the same datadir and reads grastate.
- **The fatal-signal handler can self-deadlock during SST**: `wsrep_handle_fatal_signal` →
  `wsrep_sst_cancel` → `mysql_mutex_lock(&LOCK_wsrep_sst)` (`sql/wsrep_sst.cc:591`,
  `:553-554`) — if the crashing thread holds it, mysqld never finishes dying: the node is
  neither up nor down, and the SST child tree keeps running.

## Failure scenario (Antithesis recipe)

3-node cluster, one node cycled to force SSTs (wipe its grastate or use
`repl.force_sst_after_inconsistency`-independent means, e.g., small gcache + long
disconnect so IST is impossible):

1. Trigger SST; kill -9 the joiner mysqld (and/or the script's process tree, and/or the
   donor) at random points: before the wipe, mid-wipe, mid-stream, during
   post-processing (hidden mysqld), during clone-SST grastate write.
2. On the joiner's next start, read `grastate.dat` before mysqld initializes.
3. Also inject disk write faults during `mark_unsafe` to exercise the warnings-only write.

Violation consequence: the restarted node presents a stale safe position → mysqld_safe
passes it verbatim as `--wsrep_start_position` → node ISTs from a position whose data it
does not have → permanent silent divergence on that node (reads served from a hole), or
InnoDB re-creates missing tablespaces and the node "recovers" empty.

## Invariant (concrete check)

Sidecar/workload-side, evaluated at each joiner start where the previous incarnation's SST
was observed to start and not complete (workload tracks "SST started" via donor state /
`wsrep_local_state_comment` or the joiner log line "Proceeding with SST"):

- `assert_always_or_unreachable(grastate absent || uuid == 00000000-… || seqno == -1,
  "restart after interrupted SST demands a new full state transfer")`.
- Companion: `assert_always_or_unreachable(new joiner's next state transfer is an SST, not
  an IST (log: "Prepared SST request" + wipe, vs "Prepared IST"),
  "no IST from a wiped datadir")`.

## Instrumentation suggestions (all missing)

- **SUT-side `Always`** in `request_state_transfer` (`replicator_str.cpp:1157-1162`):
  after `st_.mark_unsafe()`, re-read/verify the on-disk file actually says undefined —
  converts the silent write failure into a property violation at the exact moment it
  matters.
- **SUT-side `Sometimes`**: "SST canceled/interrupted after datadir wipe began" — an SDK
  event here is a high-value exploration hint and replay anchor. (CORRECTED: the subphase is
  externally observable after all — "Proceeding with SST........." error-log line at the
  boundary; script stderr → mysqld error log; cpat preserves `*.err`/`*.log` through the
  wipe — so this marker is a convenience, not a prerequisite. See Investigation Log.)
- **SUT-side `Unreachable`** in `restore_saved_state` when `first_time_` values are stale
  relative to current `state_uuid_` — the "restored arbitrarily old state" path.
- Script-side (bash SDK or log-marker convention): emit distinct markers at
  `SAFE_EXIT_CODE_OVERRIDE` clear (`:2384`), wipe start (`:2394`), stream complete,
  post-processing start/end — these are the retry-outcome/recovery-subphase boundaries the
  workload cannot currently observe.

## Fault requirements

- **REQUIRES node termination (kill -9 of joiner mysqld and/or SST script tree; donor kill
  for the -EAGAIN path)** — flag to environment team. Network faults alone interrupt SSTs
  (socat stall → 120s idle watchdog SIGKILLs the transfer, `wsrep_sst_common.sh:1189-1193`)
  and are worth a variant, but those paths run the orderly `safe_exit` cleanup; the crash
  windows need kills.
- Disk write faults on the datadir volume strengthen the `mark_unsafe` link.
- Harness supervision must restart killed nodes (shipped systemd units would; note
  RestartPreventExitStatus excludes SIGABRT — decide policy explicitly).

## Open Questions

None — all three resolved (see Investigation Log). Key outcomes:

- The no-way-back point IS externally observable without script instrumentation: the
  "Proceeding with SST........." error-log line is emitted at exactly the boundary, script
  logging goes to stderr → mysqld error log, and the wipe's `cpat` pattern explicitly
  preserves `*.err`/`*.log` files inside the datadir. Script-side SDK markers stay
  nice-to-have.
- InnoDB on a normal start over a half-wiped datadir REFUSES (no silent re-init) —
  violations surface as a stuck-down node — except when the packaging wrapper scripts are in
  the start path: they wipe-and-`mysqld --initialize` a datadir that lacks `mysql/` and has
  a leftover `sst_in_progress` marker, after which the node rejoins empty via full SST
  (visible, not silent).
- The GRANT-ALL post-processing account is cataloged separately as
  `sst-grant-all-user-locked-or-absent` — not folded into this property.

### Investigation Log

#### Can the workload reliably distinguish "SST passed the no-way-back point" from outside?

- Examined: `scripts/wsrep_sst_xtrabackup-v2.sh:2364-2400` (SAFE_EXIT_CODE_OVERRIDE clear
  :2384 → background receiver :2386 → "Proceeding with SST........." :2388 → wipe
  :2394-2398), `:836` (cpat wipe-exclusion pattern), `scripts/wsrep_sst_common.sh:286-304`
  (wsrep_log_* → stderr, i.e. the pipe mysqld captures into its error log).
- Found: a dedicated log line sits at the exact boundary; the log destination survives the
  wipe two ways (stderr→mysqld error log outside the datadir in container deployments; and
  `cpat` prunes `.*\.err$` / `.*\.log$` from the `find ... -exec rm`, so even a
  datadir-resident error log survives). Filesystem signature also available:
  `sst_in_progress` present (created at SST start, :2167-2168) + system tablespace files
  disappearing while grastate.dat is preserved by cpat.
- Conclusion: RESOLVED — the AlwaysOrUnreachable gate arms on the "Proceeding with SST" log
  marker (or the file-disappearance signature); script-side markers are not a prerequisite.

#### Does InnoDB on a half-wiped datadir refuse to start, or re-initialize?

- Examined: `storage/innobase/handler/ha_innodb.cc:6279-6307` (innobase_init_files:
  `create = (dict_init_mode == DICT_INIT_CREATE_FILES)`;
  `srv_sys_space.check_file_spec(create, ...)` → `innodb_init_abort()` on failure),
  `storage/innobase/srv/srv0start.cc:1620+` (srv_start takes create_new_db from the caller
  only), packaging wrappers `build-ps/rpm/mysql-systemd:50-66`,
  `support-files/mysql.server.sh:350-386`.
- Found: on a normal (non `--initialize`) start, dict_init_mode is CHECK_FILES →
  create=false → a missing system tablespace fails check_file_spec and the server refuses to
  start. mysqld never self-initializes. The only re-initialization path is the packaging
  wrapper: if `$datadir/mysql` is absent and `sst_in_progress` exists it runs
  `rm -rf $datadir/*` + `mysqld --initialize` (mysql-systemd:58-64; mysql.server.sh does the
  same) — the node then bootstraps a fresh local DB and full-SSTs on join (uuid mismatch).
- Conclusion: RESOLVED — refusal (liveness-visible) is the server behavior; harness images
  that exec mysqld directly get refusal, wrapper-based images get visible wipe-and-reinit.
  The silent-divergence variant of this failure requires the stale-grastate path (covered by
  the property's main invariant), not InnoDB re-init.

#### Fold the GRANT-ALL post-processing account check into this property, or catalog separately?

- Examined: the property catalog (category 3) — `sst-grant-all-user-locked-or-absent`
  exists as its own property with its own evidence file and investigation log.
- Conclusion: RESOLVED — cataloged separately; removed from this property's scope.
