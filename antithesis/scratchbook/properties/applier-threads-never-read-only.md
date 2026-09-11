---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# applier-threads-never-read-only — Internal wsrep threads never inherit a read-only execution context

**Type:** Safety
**Confidence:** High — fix commit f2a34b69292 (PXC-5229, 2026-06-02) read in full; all three
THD-init clearing sites located in the tree; regression test examined.

## What led here

The mirror image of the privilege-divergence property: here the *source succeeds* and the
*applier fails*, because the applier THD silently inherited a restrictive session context
from a global variable at spawn time. PXC-5229: `SET GLOBAL transaction_read_only=1`
followed by `SET GLOBAL wsrep_applier_threads=N` (dynamic, sql/wsrep_var.cc:679) spawns
fresh applier THDs (`start_wsrep_THD` → `init_wsrep_thread`, sql/mysqld.cc:12427/:12348)
that inherit `tx_read_only=true`. Every writeset they apply fails with
`ER_CANT_EXECUTE_IN_READ_ONLY_TRANSACTION` — enforced at open_tables/lock_tables AND inside
InnoDB (per the fix's own commit message, a SQL-layer-only bypass is "insufficient/unsafe")
— which feeds inconsistency voting and evicts the node. An operator-level, SQL-only action
on one node removes that node from the cluster.

## Code paths (validated at f9ecb3e)

The fix clears the flag at three THD-initialization sites:

- sql/mysqld.cc `init_wsrep_thread()` (~:12348, fix added
  `thd->variables.transaction_read_only = false; thd->tx_read_only = false;`) — applier and
  rollbacker threads created via `start_wsrep_THD`.
- sql/wsrep_server_service.cc `init_service_thd()` (fix added the same two lines) — the
  storage-service / high-priority service THDs (SR fragment persistence, view logging).
- sql/wsrep_thd.cc `wsrep_copy_session_from_thd()` :462-463 (pre-existing) — the replayer
  THD (sql/wsrep_high_priority_service.cc:1018).

Related, NOT covered by this fix:
- PXC-4849: a joiner started with `super_read_only=1` aborts the event scheduler at startup
  — same family (internal thread meets read-only global), different flag, different
  subsystem.
- The SST post-processing mysqld defends itself the blunt way: hardcoded
  `--read_only=OFF --super_read_only=OFF` on its command line
  (scripts/wsrep_sst_common.sh:847) — evidence the vendor knows the whole family is
  dangerous.
- The commit message itself flags a residual: `transaction_read_only` has NO_CMD_LINE "but
  persist works, which can be used as a hack" — `SET PERSIST` reintroduces the global at
  next startup, exercising the startup-spawn path rather than the resize path.

## Failure scenario

Workload occasionally flips `SET GLOBAL transaction_read_only` / `read_only` /
`super_read_only` on individual nodes (all legitimate operator actions — read-only replicas,
maintenance fencing, backup windows) while concurrently resizing `wsrep_applier_threads`
and running a write workload from other nodes. Pre-fix: the resized node's new appliers fail
every write apply → vote → eviction. Post-fix the three cleared sites should prevent it —
but any *new* internal-THD creation path (NBO dedicated applier,
sql/wsrep_high_priority_service.cc:706; future subsystems) that forgets the clear regresses
silently, and the `super_read_only` interaction (PXC-4849) is still only partially handled.

## Suggested assertions (all missing)

- **Always (SUT-side — the precise form):** at applier writeset-apply entry
  (`Wsrep_applier_service::apply_write_set`, sql/wsrep_high_priority_service.cc:640) assert
  `!thd->tx_read_only && !thd->variables.transaction_read_only`. Always is correct: the
  invariant must hold on every evaluation, and the fix's own title states it as such
  ("WSREP applier threads should never have tx_read_only=true").
- **Always (workload-side approximation):** while any node has a read-only global set and
  applier threads are being resized under write load, every node stays
  Synced/Primary and no error log contains `Cannot execute statement in a READ ONLY
  transaction` from a `system user` thread.
- **Sometimes (workload-side):** "applier threads were resized while a read-only global was
  set and writes were in flight" — confirms the triggering interleaving was reached (it
  needs three concurrent conditions; without this marker the Always can be vacuous).

## Fault-availability notes

No injected faults strictly required — the trigger is a runtime-config interleaving, which
the workload drives itself. Network faults (default-on) add view churn that multiplies
applier restarts. Node termination useful only for the SET PERSIST variant (restart-spawned
appliers); flag that variant as dependent on restart capability.

## Observations

- This is squarely the "privilege/session-context asymmetry, applier-fails direction". With
  privilege-context-divergence-never-evicts (source-fails direction) it brackets the class:
  *the applier's execution context must be a superset of what any legal writeset needs, and
  the source's context must be pre-validated for anything the applier can't check.*
- The fix ships a regression MTR test (pxc_5229_wsrep_applier_tx_read_only.test) asserting
  node_2 stays Primary/Connected/Ready/Synced after the resize — good template for the
  workload-side check, but it tests exactly one interleaving with one flag.
- Applier count sizing bugs interact here: `wsrep_slave_count_change` races
  (sut-analysis §5.6) mean resizes under load are themselves flaky — the same workload
  exercises both.

## Open Questions

None — all three resolved; see Investigation Log. Net effect — the property SHARPENS:

- **Confirmed uncleared path (finding):** the NBO worker THD is created via `wsp::thd`
  (`sql/wsrep_utils.cc:940-951`), which clears binlog bits and grants full access but does
  NOT clear `variables.transaction_read_only`/`tx_read_only`; a fresh THD copies the
  globals, so an NBO DDL applied while `SET GLOBAL transaction_read_only=1` is in effect
  runs on a read-only-poisoned THD. This is exactly the next-regression shape the property
  hypothesized. The workload MUST include NBO DDL (`wsrep_OSU_method=NBO`) under
  read-only flips, and the proposed apply-entry `Always` assertion should also cover the
  NBO begin path (`apply_nbo_begin`), not just `apply_write_set`.
- The property's flag set for applier writes narrows to `{transaction_read_only}`:
  `read_only`/`super_read_only` are explicitly bypassed for appliers in the single gate.
- SET PERSIST + restart is covered for regular appliers/rollbackers (all spawn paths run
  the cleared `init_wsrep_thread`); the NBO gap is the remaining channel there too.

### Investigation Log

#### Does the NBO dedicated applier THD pass through one of the three cleared init sites?

- Examined: `sql/wsrep_high_priority_service.cc:706-870` (`apply_nbo_begin` — worker
  thread body), `sql/wsrep_utils.cc:940-951` (`wsp::thd` ctor), grep for other `wsp::thd`
  users (only the NBO worker and the SST donor helper at `wsrep_sst.cc:1399`).
- Found: No. The NBO worker constructs a bare `new THD` via `wsp::thd wthd(true)` — no
  `init_wsrep_thread`, no `init_service_thd`, no `wsrep_copy_session_from_thd`. The ctor
  clears binlog option bits and sets full master_access but never touches
  `transaction_read_only`/`tx_read_only`; THD construction copies
  `global_system_variables`, so the poisoned global is inherited. The worker does set
  `thd->wsrep_applier = true` (so it bypasses `check_readonly`), but
  `transaction_read_only` is enforced independently of that gate (SQL layer + InnoDB, per
  the PXC-5229 fix's own commit message).
- Conclusion: resolved — confirmed uncleared path; NBO under read-only flips is the
  highest-value workload variant for this property.

#### Do `read_only`/`super_read_only` ever gate applier writes?

- Examined: `sql/auth/sql_authorization.cc:1905-1948` (`check_readonly` — the single
  enforcement gate for both `read_only` and `super_read_only`).
- Found: explicit exemption: `if (WSREP(thd) && thd->wsrep_applier) return false;`
  ("Ignore readonly for background wsrep applier too (like slave thread)"), plus the
  PXC-internal-user exemption; `thd->slave_thread` (set for wsrep threads in
  `init_wsrep_thread`) exempts them a second way. Both flags short-circuit before any
  SUPER/`opt_super_readonly` logic.
- Conclusion: resolved — appliers are exempt from `read_only`/`super_read_only`; the
  property's flag set for the apply path is `{transaction_read_only}` only. PXC-4849
  (event scheduler on a `super_read_only` joiner) is a different, non-applier subsystem —
  keep it out of this property's scope.

#### Is `SET PERSIST transaction_read_only=1` + restart handled for startup-spawned appliers?

- Examined: `sql/mysqld.cc:12348-12380` (`init_wsrep_thread` — clears both flags with an
  explicit comment: "A freshly created applier THD would inherit a global
  transaction_read_only ... also propagates into InnoDB"); spawn plumbing
  `sql/wsrep_thd.cc:99-140` (`wsrep_create_appliers` → `create_wsrep_THD` →
  `start_wsrep_THD`); startup call sites `sql/wsrep_mysqld.cc:1300` (first applier during
  wsrep_init_startup) and `sql/mysqld.cc:11060` (remaining `wsrep_slave_threads - 1`);
  rollbacker `wsrep_thd.cc:312`.
- Found: every applier/rollbacker spawn path — startup (both orderings) and dynamic
  resize — funnels through `start_wsrep_THD` → `init_wsrep_thread`, which clears the
  flags after the persisted global has been applied. No separate joiner/SST-order spawn
  path bypasses it.
- Conclusion: resolved — SET PERSIST + restart is covered for regular appliers; the only
  uncleared internal-thread path found in-tree is the NBO worker above.

### Investigation Log

#### What exactly did the PXC-5229 fix change, and is it a single point of repair?

- Examined: `git show f2a34b69292` (full diff: sql/mysqld.cc +8, sql/wsrep_server_service.cc
  +2, new MTR test); sql/wsrep_thd.cc:454-464; grep for callers of
  `wsrep_copy_session_from_thd` (one: replayer, wsrep_high_priority_service.cc:1018);
  scripts/wsrep_sst_common.sh:847.
- Found: three distinct THD-init sites now clear the flag (two added by the fix, one
  pre-existing for the replayer); the fix is additive per-init-site, not a central guard at
  apply time — no assertion exists at the apply entry point.
- Not found: any clearing for the NBO applier's THD path (not traced end-to-end); any
  general applier exemption for read_only/super_read_only.
- Conclusion: regression-target claim validated; the missing central apply-time assertion is
  precisely the SDK instrumentation this property proposes.
