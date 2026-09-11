# cluster-address-live-set-rejoins — Evidence

**Focus area:** Evaluation gap-fill — runtime `SET GLOBAL wsrep_cluster_address` under
write load (evaluation/synthesis.md Gap 7; documented race in `sql/wsrep_var.cc:550-595`).
**Rider decision:** implemented as a **rider on `restarted-node-rejoins-synced`** — the
rejoin detector, Synced-bound, and convergence checks are identical; the SET is simply a
third trigger for the leave→rejoin cycle (alongside graceful restart and kill), one that
needs **no kill channel and no process restart** — it is v1's cheapest way to exercise
disconnect/reconnect/IST machinery from pure SQL. It still gets its own slug and assertion
messages because its failure modes (SET hangs, double-SET race, false-success SET) are not
expressible as arms of the existing property.
**Confidence:** High — the entire handler chain read directly; the race is admitted in an
in-code comment.

## Claim under test

`wsrep_cluster_address` is a dynamic global (`sql/sys_vars.cc:8269-8275`, guarded by
`PLock_wsrep_cluster_config`). Setting it at runtime is a documented operator action
(re-pointing a node at the cluster). The implied contract: the SET statement completes,
the node drops out of and returns to the cluster (Synced) within a bound, no other node is
disturbed, and no state is corrupted — even under concurrent writes and concurrent SETs.

## Code (validated, commit f9ecb3e)

- **Update handler** `wsrep_cluster_address_update` (`sql/wsrep_var.cc:550-595`):
  1. **Unlocks `LOCK_global_system_variables` mid-update** (:565) — the in-code comment
     admits it: "releasing LOCK_global_system_variables may cause race condition, if there
     can be several concurrent clients changing wsrep_provider".
  2. `wsrep_stop_replication(thd, false)` (:566 → `sql/wsrep_mysqld.cc:1367-1394`):
     disconnects the provider and waits for `s_disconnected`, **rolls back the setter's
     own transaction**, **closes ALL client connections**
     (`wsrep_close_client_connections(true,false)`), waits for all appliers to exit.
  3. `wsrep_start_replication()` (:568 → `wsrep_mysqld.cc:1448-1492`): reconnects
     (`Wsrep_server_state::instance().connect`, :1471), recreates the rollbacker and 1
     applier, handles deferred first-initialization (:572-579), then creates the remaining
     `wsrep_slave_threads - 1` appliers (:581).
  4. Mutex re-juggle (:584-592): unlocks `LOCK_wsrep_cluster_config`, re-locks
     `LOCK_global_system_variables` then `LOCK_wsrep_cluster_config` — a window where
     BOTH locks are free mid-operation.
- **Verify is a stub**: `wsrep_cluster_address_verify` returns 0 for anything
  (:523-526) — garbage addresses reach the provider connect path.
- **Empty address is legal and detaching**: `wsrep_start_replication` returns true
  WITHOUT connecting when the address is empty (:1461-1464 "wait for address") — `SET
  GLOBAL wsrep_cluster_address=''` parks the node out of the cluster indefinitely.
- **False success**: the update handler returns `false` (success) even when
  `wsrep_start_replication()` fails (:568-582 — the failure branch just skips
  applier creation) — the client's SET succeeds while the node is left detached with no
  appliers.
- **Unlocked readers of the global char\***: `wsrep_cluster_address_init` (:597-605)
  `my_free`s and re-`my_strdup`s the global; readers elsewhere dereference it without the
  config mutex (`sql/wsrep_thd.cc:125-130` applier-creation error path, `:309`
  rollbacker gate; `sql/mysqld.cc:8877`). A reader racing the free/strdup window is a
  use-after-free candidate.
- **Bootstrap flag interplay**: `wsrep_start_replication` consumes `wsrep_new_cluster`
  (:1466-1467). If a boot with `--wsrep-new-cluster` never connected (empty address at
  boot — the sticky case already noted in `cluster-identity-single-lineage`), a later
  runtime SET performs a BOOTSTRAP connect — a workload-reachable second-lineage lever;
  the rider's workload must only SET valid full gcomm strings on an already-joined node
  (poison-budget rule), leaving the bootstrap vector to the lifecycle properties.
- Applier-count interplay: appliers are recreated from `wsrep_slave_threads` (:570/:581)
  — a concurrent `wsrep_applier_threads` resize (see `applier-resize-converges`) during
  the window compounds; the two riders share a workload phase but must not run
  concurrently with each other in v1 (attribution).

## Failure scenario

1. 3-node cluster under steady write load. Workload occasionally (low frequency — each
   SET is a full leave+rejoin, budget like a restart) issues
   `SET GLOBAL wsrep_cluster_address='gcomm://<current peers>'` on ONE non-quorum-critical
   node at a time, sometimes with in-flight transactions on that node, sometimes two
   concurrent SETs from two sessions on the same node.
2. Expected: the SET returns; the node's clients get killed/errored (never silently lost
   acked writes — the ack journal from `non-primary-rejects-writes` covers the client
   side); the node rejoins via IST and reaches Synced within the calibrated rejoin bound;
   cluster size returns to N; survivors never lose Primary.
3. Violations: the SET never returns (stop hangs waiting on a wedged applier/connection —
   the handler holds `LOCK_wsrep_cluster_config` throughout, so a second SET then blocks
   forever behind it); mysqld crash (use-after-free on the global string, or the admitted
   double-SET race at the :584-592 window); node left permanently detached while the SET
   reported success; rejoin turns into an SST loop or a non-Primary wedge.

## How to check (workload-side; rider on restarted-node-rejoins-synced)

- `Always`: every issued `SET GLOBAL wsrep_cluster_address` returns within T_set
  (calibrated; covers the stop-replication drain) — and within T_rejoin afterwards the
  node reports Synced and `wsrep_cluster_size == N` cluster-wide (the existing rejoin
  detector, distinct message).
- `Always`: survivors remain Primary and writable throughout the cycle (the SET must be a
  one-node event).
- `Sometimes`: a SET was issued while the node had write transactions in flight;
  `Sometimes`: two SETs raced on the same node (both returned, node Synced after) — the
  admitted-race probe.

## Assertion type

- `Always` for bounded-return + bounded-rejoin + one-node-blast-radius — liveness with a
  calibrated bound and a safety edge, evaluated on every SET cycle (matches the
  `graceful-shutdown-bounded` pattern of bounded-or-failed).
- `Sometimes` for the two race-shape guards — they are the semantic states that make the
  race window real, and vacuity guards for the rider (no SET issued → rider silent).

## Instrumentation notes (missing)

- None required for v1 (pure workload + log scan). A SUT-side `Sometimes` at
  `wsrep_var.cc:590-592` (the both-locks-free window) would confirm the race window is
  actually being hit under concurrent SETs — v2 nicety, not a blocker.

## Fault / config / phase flags

- No faults required; network faults during the rejoin half widen (and are already the
  bread and butter of `restarted-node-rejoins-synced`). No kill channel needed — this is
  deliberately a v1, SQL-only leave/rejoin trigger.
- Config: none (the variable is dynamic; unlike `wsrep_provider`, which is READ_ONLY in
  this tree, `sys_vars.cc:8241-8246` — narrowing the in-code comment's provider-race to
  cluster_address-vs-cluster_address).
- Phase: **v1-assert, rider** (folded into `restarted-node-rejoins-synced`'s workload
  phase; low SET frequency).

## Open questions

- Can `wsrep_stop_replication` deadlock against the setter's own session state (it
  rollbacks the setter's trx at :1381 and closes other connections at :1387 while holding
  `LOCK_wsrep_cluster_config`)? A hang here is precisely what the `Always` bound detects;
  no static answer attempted — the bound is the instrument.
- Should the SET-to-empty-string detach (legal, parks the node) be exercised? It converts
  the rider into a controlled leave-without-rejoin — useful for `non-primary-rejects-writes`
  but it breaks this rider's rejoin bound. Deferred: if added later, gate it behind its own
  phase with an explicit re-SET to a valid address as the recovery arm.

### Investigation Log
