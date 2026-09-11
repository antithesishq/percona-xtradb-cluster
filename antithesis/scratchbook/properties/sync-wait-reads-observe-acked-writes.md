# sync-wait-reads-observe-acked-writes

**Focus:** Protocol contracts — the wsrep_sync_wait causality contract.
**Confidence:** High on mechanism (code read); medium on whether a violation is reachable
in a same-version cluster — that is exactly what Antithesis should decide.

## Claimed contract

`wsrep_sync_wait` (bitmask: 1=READ/SELECT, 2=UPDATE/DELETE, 4=INSERT/REPLACE, 8=SHOW;
`wsrep_api.h:1216-1240`, wsrep-system-index.rst:1309-1366): when the relevant bit is set,
a statement does not execute until the node has caught up with *all writes acknowledged
anywhere in the cluster before the statement began*. This is the only read-your-writes /
monotonic-read mechanism PXC offers (default is 0 — stale reads are the shipped contract).

## Implementation and its gaps (all verified at commit f9ecb3e)

- Gate: `wsrep_sync_wait` (`sql/wsrep_mysqld.cc:1510-1552`) → `sync_wait(-1)` →
  provider `ReplicatorSMM::sync_wait` (`galera/src/replicator_smm.cpp:1678-1746`).
- `gcs_.caused()` obtains the group's current last-delivered GTID, then the node waits on
  **`apply_monitor_`, not `commit_monitor_`** (:1715-1722). The in-code justification: the
  apply monitor "is always released only after the whole transaction is over". This is the
  load-bearing claim the property tests — commit-order release happens *inside* the binlog
  flush stage (interim commit, `rpl_commit_stage_manager.cc:104-133` →
  `replicator_smm.cpp:1517`), and whether apply-monitor release truly implies InnoDB
  visibility depends on `opt_log_replica_updates && opt_binlog_order_commits` gating
  (`wsrep_trans_observer.h:356-373`).
- The wait deadline is **wall clock**: `gu::datetime::Date::calendar()` (:1683) +
  `repl.causal_read_timeout` (default PT30S). Backward clock step → premature "Unplanned"
  timeout → spurious failure; forward step → spurious failure too. Failure surfaces as
  mislabeled ER 1205 (see `nonready-node-error-code-contract`).
- **Skips** (`wsrep_must_sync_wait`, `wsrep_mysqld.cc:1495-1505`): dirty reads; replaying;
  and `!thd->in_active_multi_stmt_transaction()` — the 2nd+ statement of an explicit
  transaction never sync-waits (by design: snapshot taken at first statement). The *first*
  statement of a `BEGIN; SELECT ...` must still wait — that boundary is a good adversarial
  target.
- The timed monitor wait is a self-described "hack" to avoid deadlock with monitor drain
  during configuration changes (:1708-1714) — view changes racing sync-wait are the
  interesting interleaving.

## Failure scenario

Client W writes k=v on node A, gets OK. Client R (knows of W's ack via the workload's own
coordination) then reads k on node B with `wsrep_sync_wait=1`. Under FC stalls, view
changes, or the interim-commit window, B's sync-wait returns success while the InnoDB read
view does not yet contain v → silently stale read despite the causality contract. The
alternative failure is the loud one: sync-wait times out and returns mislabeled 1205.

## Suggested assertions (all missing)

- **Always (workload-side):** with sync_wait bit set for the statement class, a read that
  starts after write X was acked observes X (version-numbered rows; reader asserts
  `version >= last_acked_version` for the key). Message: "sync_wait read observes all
  previously acked writes". This is the core safety property.
- **Always (workload-side):** the first statement of an explicit transaction
  (`BEGIN; SELECT`) obeys the same bound — separate assertion message
  ("sync_wait applies to transaction-opening statement") because the skip logic
  (`:1503`) is a distinct code path.
- **Sometimes (workload-side):** "sync_wait completed after waiting through a view change" —
  drives the drain-race window the in-code hack comment admits is fragile.
- **Sometimes (SUT-side, missing — v2 instrumentation):** the view-change/drain overlap is
  externally invisible (the workload cannot tell whether its sync-wait actually spanned a
  configuration change), so the natural anchor is an SDK `Sometimes` inside the sync-wait
  retry path at `replicator_smm.cpp:1708-1714` — fired when the timed monitor wait is
  re-entered because a view change raced the drain. This is the replay/branch anchor for
  the fragile window; the workload-side `Sometimes` above is only a coarse proxy for it.

## Assertion-type rationale

`Always` for the causality checks: the guarantee must hold on every sync-wait read; a
single stale read is a violation. `Sometimes` for the view-change overlap because it is a
meaningful rare state, not an invariant.

## Fault requirements

Network faults (default-on) drive FC stalls and view changes. The wall-clock deadline
angle **requires clock jitter faults — often disabled; flag to harness planning**. The
core causality assertion works without clock faults.

## Open questions

None — both resolved (see Investigation Log). Net effect: the design-level barrier claim
holds in *both* binlog configs (the interim-commit gating moves only the commit-monitor
release, never the apply-monitor release), so a dual-config harness variant is optional,
not required. The property remains valuable as the verifier that the monitor
implementation actually delivers the design (the in-code "hack" timed wait and
drain-race window are still the target). Noted residual (minor, liveness not staleness):
`gu_cond_wait` inside `gcs_core_caused` (gcs_core.cpp:1634) is untimed — if the provider
connection closes mid-causal-probe the caller could hang; the wall-clock deadline covers
only the -EAGAIN retry loop and the monitor wait.

### Investigation Log

#### Does apply-monitor release imply InnoDB visibility when ordered commit is disabled?

- Examined: `galera/src/replicator_smm.cpp:575-673` (apply_trx, applier side),
  `:1517-1602` (commit_order_leave / release_commit), `:1678-1759` (sync_wait +
  last_committed_id), wsrep-lib `src/transaction.cpp:626-671` (after_commit),
  `sql/wsrep_trans_observer.h:344-394` (wsrep_ordered_commit / wsrep_after_commit).
- Found: the apply monitor is released only after the full commit on every path. Remote
  (applier) writesets: `apply_monitor_.leave(ao)` at replicator_smm.cpp:657 runs after
  `ts.apply(...)` returns, and the PXC comment at :611-614 confirms commit callbacks are
  part of the apply action — the applier's engine commit (via ha_commit, which returns
  only after the trx is committed in InnoDB even under binlog group commit) completes
  first. Local transactions: release via `wsrep_after_commit` → `after_commit()` →
  `provider().release()` → `release_commit` → leave at :1586, invoked after
  ha_commit_trans finished. The `!opt_log_replica_updates || !opt_binlog_order_commits`
  gate (wsrep_trans_observer.h:364) only decides *where* `ordered_commit()` (the
  COMMIT-monitor release) runs — early in the binlog flush stage vs inside
  wsrep_after_commit (:389); it never touches the apply monitor.
- Not found: any path releasing the apply monitor before the engine commit completed
  (release_rollback enters/leaves for non-committed ts, which is the rollback case).
- Conclusion: RESOLVED — apply-monitor release ⇒ InnoDB commit completion in both
  configs; the design-level stale-read window does not exist. This also answers the
  catalog-wide "is sync_wait a sufficient checksum barrier" question at the design
  level: per-node, yes (modulo monitor-implementation bugs, which this property tests).

#### Does a non-primary transition mid-wait fail cleanly (correct error, no false success)?

- Examined: `gcs/src/gcs_core.cpp:1211-1243` (core_msg_causal), `:1614-1651`
  (gcs_core_caused), `galera/src/galera_gcs.hpp:149-165` (timed retry wrapper),
  `galera/src/replicator_smm.cpp:1688-1745` (sync_wait error funnel).
- Found: the causal token is itself a totally-ordered group message evaluated against
  the group state at its own self-delivery point: PRIMARY → current act_id; non-prim →
  -EPERM; state exchange → -EAGAIN (retried under the wall-clock deadline →
  -ETIMEDOUT). Any error throws → sync_wait returns WSREP_TRX_FAIL → ER 1205 (the
  mislabel tracked in `nonready-node-error-code-contract`), never success. If the token
  IS delivered while PRIMARY, every writeset it covers precedes it in total order and is
  therefore delivered and applied locally before the apply monitor reaches the target —
  the subsequent success is causally valid even if the node drops out of primary during
  the monitor wait. UUID change mid-wait surfaces as gu::NotFound → WSREP_TRX_MISSING.
- Not found: any stale-success path. (Untimed gu_cond_wait hang on provider close noted
  above as a liveness residual.)
- Conclusion: RESOLVED — clean failure guaranteed; the workload can treat any sync-wait
  success as a valid causal barrier claim and assert version visibility on it.
