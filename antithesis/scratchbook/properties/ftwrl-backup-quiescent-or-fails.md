---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# ftwrl-backup-quiescent-or-fails — A successful FTWRL yields a genuinely frozen snapshot point; otherwise it fails visibly

**Provenance: evaluation gap-fill (synthesis Gap 3 — coverage #3, wildcard F3).** The
catalog had no property on the backup workflow itself, despite three verified sharp edges:
FTWRL does not block COMMIT in PXC, the FTWRL-vs-applier backup-lock deadlock is "solved"
by making FTWRL fail, and the provider pause stamps grastate mid-run.

**Type:** Safety | **Assertion:** workload `Always` (while a successfully-returned FTWRL
is held on node X: `wsrep_last_committed` on X does not advance, and no write acknowledged
via X commits) + `Sometimes` companions on the degraded/retry branches | **Confidence:**
High — the pause-or-fail contract is explicit in `lock.cc`, and the MDL COMMIT skip is a
deliberate `#ifdef WITH_WSREP` divergence.

## Claim under test

Backup tooling's core assumption: after `FLUSH TABLES WITH READ LOCK` returns success on a
PXC node, that node is a quiescent snapshot point — no transaction commits on it (locally
originated or replicated) until `UNLOCK TABLES`. In PXC the *mechanism* backup tools
assume (the global COMMIT MDL) is deliberately disabled for wsrep transactions; the
guarantee is re-provided solely by pausing the galera provider — and the code promises
that if the pause cannot be established, **FTWRL fails with an error rather than returning
a non-quiescent success**. That pause-or-fail contract is the assertable property. The
documented-degraded behavior (LOCK INSTANCE FOR BACKUP → FTWRL failing with
ER_QUERY_INTERRUPTED whenever writesets are pending) is legal and gets a `Sometimes`, not
a failure.

## Code paths (verified at f9ecb3e)

- **FTWRL does NOT block COMMIT via MDL in PXC**: `sql/handler.cc:1841-1848` — commit
  skips acquiring the `MDL_key::COMMIT` intention lock for `WSREP(thd)` sessions
  ("Taking explicit lock will block background/applier thread from aborting a local
  thread lock so avoid taking this lock here"). Any quiescence therefore comes from the
  provider pause, not from MySQL's GRL machinery.
- **The pause-or-fail contract**: `Global_read_lock::make_global_read_lock_block_commit`
  `sql/lock.cc:1175-1290`:
  - node not Synced (e.g. already desynced/donor) → `server_state.pause()` (`:1227-1228`);
  - Synced AND session holds `MDL_key::BACKUP_LOCK` (i.e. `LOCK INSTANCE FOR BACKUP`
    precedes FTWRL — the xtrabackup flow) → `try_desync_and_pause(
    wsrep_desync_pause_retry_timeout * 1000)` (`:1269-1271`) — non-blocking attempts every
    100ms up to the timeout (`wsrep-lib/src/server_state.cpp:693-719`), avoiding the
    in-code-documented deadlock (applier holding a TOI DDL blocked on the backup lock
    while pause waits for the applier — full scenario spelled out in the comment
    `lock.cc:1236-1268`);
  - Synced, no backup lock → `desync_and_pause()` (`:1273`) → plain `pause()` — which
    drains monitors **untimed** (`replicator_smm.cpp:3385-3416`);
  - pause failed → `WSREP_INFO("Server pausing failed")` + `ER_QUERY_INTERRUPTED`, FTWRL
    returns error (`:1277-1283`). **So: FTWRL success ⇒ provider paused at a definite
    seqno** (`wsrep_locked_seqno`, logged as "Server paused at: N").
- **What a paused provider blocks**: replicated writesets stop applying (monitors
  drained/held); local commits cannot replicate. Local sessions attempting TOI DDL on the
  paused node get `ER_CANT_UPDATE_WITH_READLOCK` (`sql/wsrep_mysqld.cc:3051-3057`);
  DML commit attempts block/fail at replication rather than at the (skipped) COMMIT MDL.
- **Provider pause stamps grastate mid-run**: `pause()` calls
  `st_.set(state_uuid_, last_committed, ...)` (`replicator_smm.cpp:3409-3410`,
  try-variant `:3505-3506`) — a REAL seqno lands in grastate.dat while mysqld runs, reset
  only in `resume()`. A kill inside the backup window yields grastate-ahead — owned by
  `grastate-se-checkpoint-agreement` (kill leg), cross-referenced here because the backup
  workload is what makes that window wide.
- Percona's SST donor flow uses the same primitives plus `innodb_disallow_writes`
  (`sql/wsrep_sst.cc:1471-1535`) — donor-side composition is owned by
  `donor-returns-to-synced` / `clustercheck-200-implies-write-progress`; this property is
  the *operator-driven* (plain SQL) backup surface.

## Failure scenario

Antithesis-shaped violation: FTWRL returns success but a replicated writeset commits on
the node afterwards (pause bookkeeping bug, a monitor-release path that bypasses the
pause, or the `try_desync_and_pause` local-monitor re-entry landing at the wrong seqno —
see `desync-ftwrl-composition-resyncs`), so `wsrep_last_committed` advances while the
"backup" runs → the backup tool streams a torn snapshot with no error anywhere. The field
consequence is a backup that restores to an inconsistent database — the worst silent
failure a backup workflow can have.

## Suggested implementation

Plain SQL, fault-free core (faults enrich):

- **Workload `Always`**: dedicated backup-actor connection to node X: optionally
  `LOCK INSTANCE FOR BACKUP` → `FLUSH TABLES WITH READ LOCK`. If FTWRL errors
  (ER_QUERY_INTERRUPTED etc.) → legal, record `Sometimes`. If FTWRL succeeds: sample
  `wsrep_last_committed` on X, hold for a randomized interval while the rest of the
  workload hammers OTHER nodes with commits, re-sample: **must be unchanged**; also
  assert no write issued through X during the hold was acknowledged. Then
  `UNLOCK TABLES` (± `UNLOCK INSTANCE`).
- **Cross-check (same `Always`, second clause)**: during the hold, other nodes MUST still
  be able to commit for a while (FTWRL on one node is not a cluster freeze) — bounded by
  flow control eventually pausing the cluster when X's recv queue fills
  (`gcs.fc_limit`); keep the hold shorter than the FC trip point or gate the clause on
  FC state (`flow-control-pause-releases` owns the cluster-freeze watchdog).
- **`Sometimes` companions (distinct messages)**: (a) FTWRL succeeded on the
  backup-locked path after ≥1 retry (retry loop exercised — observable via timing or the
  "try_desync_and_pause timed out" absence + >100ms latency); (b) FTWRL failed with
  ER_QUERY_INTERRUPTED while an applier held a TOI DDL wait (the documented
  deadlock-avoidance degradation actually observed); (c) a commit was acknowledged on
  another node while X was frozen.
- **Instrumentation**: none required day-one; the "Server paused at: N" / "Server pausing
  failed" log lines (`lock.cc:1281-1287`) join the shared log-scan layer as cross-checks.

## Fault / config / phase flags

- Phase tag: **v1-assert**, fault-free core (the cheapest gap-fill in the catalog);
  network faults during the hold sharpen (pause vs view-change races). No kill channel
  needed (the kill-during-pause grastate angle belongs to
  `grastate-se-checkpoint-agreement` +kill).
- Config: none (FTWRL/LOCK INSTANCE are plain SQL; `wsrep_desync_pause_retry_timeout`
  default 30s, `sql/sys_vars.cc:8500-8508`, `wsrep_mysqld.cc:162`).
- Workload note: the backup actor must run against one node at a time and release within
  bounded time, or it self-inflicts flow-control pauses that other properties will see —
  same fencing discipline as other operator actions (Shared conventions).
- Coordination: composition/liveness (does the node come BACK, does desync accounting
  balance) is the sibling property `desync-ftwrl-composition-resyncs`; donation-driven
  desync is `donor-returns-to-synced`.

## Open Questions

- Window between MDL blocks-commit acquisition and the provider pause inside
  `make_global_read_lock_block_commit` (`lock.cc:1204-1283`): commits landing in that
  window are before FTWRL *returns*, so the stated invariant (frozen after success
  returns) is unaffected — but confirm the client-visible ordering: is the "paused at"
  seqno always ≥ any commit acknowledged before FTWRL returned on that node? If not, a
  backup tool reading `wsrep_locked_seqno` gets a stale position marker.
- Does a local (non-wsrep, e.g. `wsrep_on=OFF` session or sql_log_bin=0) commit bypass
  both the skipped COMMIT MDL *and* the provider pause? If yes, the invariant must be
  scoped to replicated commits (`wsrep_last_committed`) and the workload must not issue
  wsrep-off writes through a frozen node. `(partial: handler.cc skip is conditional on
  WSREP(thd) — non-wsrep THDs still take the COMMIT MDL, so they ARE blocked by FTWRL;
  unverified for internal/system threads)`

### Investigation Log

#### Does a local non-wsrep commit bypass both the skipped COMMIT MDL and the provider pause?

- Examined: `sql/handler.cc:1841-1848` (the `#ifdef WITH_WSREP` COMMIT-MDL skip) and the
  pause machinery in `sql/lock.cc:1175-1290`.
- Found: the COMMIT-MDL skip is conditional on `WSREP(thd)` — non-wsrep client THDs
  (e.g. `wsrep_on=OFF` sessions) still acquire the `MDL_key::COMMIT` intention lock, so
  they ARE blocked by a held FTWRL through the normal GRL machinery.
- Not found: verification for internal/system threads — whether any internal writer
  commits without a client THD taking that MDL path while the provider pause only covers
  replicated commits.
- Conclusion: tagged `(partial: non-wsrep client THDs still take the COMMIT MDL;
  internal threads unverified)`.
