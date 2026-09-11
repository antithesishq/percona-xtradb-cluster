---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# sr-fragment-cross-node-agreement — Streaming-replication fragment state agrees cluster-wide, including after crash recovery

**Merged from two independent discoveries (focus 1: data integrity, as
`sr-fragment-cross-node-agreement`; focus 3: failure recovery, as
`sr-fragments-converge-after-crash`) — both agents independently found the same
NULL-seqno-deletion crash window, a strong confidence signal.**

**Type:** Safety | **Assertion:** `Always` — cross-node equality of persisted fragment
state at quiesced checkpoints must hold on every evaluation. (The SR path is config-gated,
so runs without SR never evaluate it — but within an SR-enabled run this is a hard
invariant, and the workload controls the config, so plain `Always` over
`AlwaysOrUnreachable`.) | **Confidence:** High on the divergence window's existence (the
deletion code carries an in-code comment naming the exact crash window); Medium on
end-to-end consequence severity (depends on orphan-cleanup behavior, an open question).

**FAULT REQUIREMENT: node termination (kill -9 + restart) — the window is a crash between
two local persistence steps. CONFIG REQUIREMENT: `wsrep_trx_fragment_size > 0` (SR is OFF
by default — needs a dedicated workload variant).**

## Property

With streaming replication enabled, at quiesced checkpoints and after any node crash and
recovery, the cluster-wide view of streaming transactions converges: for every
(node_uuid, trx_id), either all nodes (including a recovered origin) agree on the same set
of certified fragment seqnos in `mysql.wsrep_streaming_log`, or the transaction is fully
absent everywhere (rolled back cluster-wide). No node permanently retains fragments of a
transaction the origin no longer knows about; the origin never resurrects a transaction its
peers rolled back; with no SR transaction in flight, `wsrep_streaming_log` is empty on all
nodes. No SR transaction's final data is present on a strict subset of nodes (the end-state
half is delegated to `cross-node-row-equality` — this property adds the fragment-table
oracle, which detects the divergence *earlier* and attributes it to SR).

## The divergence window (code evidence, verified)

Fragment certification deliberately persists before certifying
(`wsrep-lib/src/transaction.cpp:1530-1740`, `certify_fragment`, called from
`streaming_step` `:1475`):

1. `append_fragment` writes the fragment row with **seqno undefined** (`:1618-1651`)
   — separate storage-service transaction, committed to InnoDB
   (`sql/wsrep_storage_service.cc:63-68`; schema `sql/wsrep_schema.cc:40-74`).
2. → crash point A ←
3. `provider().certify()` replicates + certifies the fragment cluster-wide (`:1664-1667`).
4. → crash point B ←
5. Meta update assigns the certified seqno to the local row + storage commit (`:1680-1690`).

Recovery (`Wsrep_schema::recover_sr_transactions`, `sql/wsrep_schema.cc:1122-1218`) scans
`wsrep_streaming_log` and — verified at `:1211-1218` with the in-code comment "This is
possible if the server crashes between inserting the fragment into table and updating the
fragment seqno after certification" — **DELETES any row whose seqno is NULL/undefined**.

Crash at point B is the divergence: the fragment was certified and assigned a seqno
cluster-wide; every *other* node applied it into its own `wsrep_streaming_log` (applier
path `server_state.cpp:366-410`). The recovering origin deletes its local copy.
Post-recovery: N-1 nodes hold a certified fragment of an in-flight SR transaction whose
origin has forgotten it exists.

Downstream cleanup is best-effort:

- `close_orphaned_sr_transactions` (`wsrep-lib/src/server_state.cpp:1567-1678`) triggers
  off view changes keyed on `equal_consecutive_views` (`:1587`) and **rolls back even if
  adopt fails**, logging "leaving stale entries ... removed manually" (`:1645-1648`) — the
  code itself documents the manual-cleanup end state. Rollbacker fragment removal:
  `sql/wsrep_thd.cc:178-199`.
- Rollback fragments for transactions with no streaming applier are *expected* and demoted
  to dummy writesets with a warning (`server_state.cpp:301-317`, `:392-398`, `:433-440`) —
  under membership churn a fragment can be silently dropped on a subset of nodes (see
  `sr-rollback-fragment-noop` for the rollback-side contract).
- A `#if 0`'d SR double-commit assert in `wsrep-lib/src/transaction.cpp:568-582` implies a
  known crash window orphaning `wsrep_streaming_log` rows that upstream chose not to
  enforce.
- wsrep-lib authors left debug-build crash points at exactly these spots
  (`crash_replicate_fragment_{before_certify,after_certify,success}`,
  `transaction.cpp:1663/:1671/:1697`) — usable deterministically in the assert-enabled
  image variant.
- The un-runnable galera_3nodes_sr GCF-810A/B/C suite (missing include files, verified)
  means SR crash consistency is effectively untested in this SUT today.

## Failure scenario (Antithesis recipe)

1. 3-node cluster, `wsrep_trx_fragment_size` small (e.g., 1-10 rows / low bytes) so every
   multi-row transaction streams fragments; mix fragment_unit ROWS/BYTES; some transactions
   commit, some roll back.
2. kill -9 nodes at random — the certify-vs-storage-commit window is microseconds wide per
   fragment but is crossed once per fragment per transaction, so fragment-heavy load makes
   it a large cumulative target.
3. Let the node recover and the cluster settle; also inject view changes (brief partitions,
   default-available) to exercise `close_orphaned_sr_transactions`.
4. At quiescence, compare `SELECT node_uuid, trx_id, COUNT(*), MAX(seqno) FROM
   mysql.wsrep_streaming_log GROUP BY 1,2` across all nodes.

Outcomes to catch: (a) the transaction later commits on peers using fragment N but
replays/aborts differently on the recovered origin → row divergence; (b) orphan cleanup
rolls the SR transaction back on peers but stale fragments persist on some nodes (the
"removed manually" path) → `wsrep_streaming_log` disagreement that later collides with a
reused trx id; (c) membership churn during streaming hits the missing-applier dummy path
on one node only → that node misses a fragment yet certifies the commit fragment;
(d) permanently stale fragment rows on N-1 nodes (unbounded table growth).

## Invariant (concrete check)

Workload-side, evaluated at quiesced checkpoints (no faults active, cluster Synced, no
open SR transactions per workload bookkeeping) and after every node restart/rejoin:

- `assert_always(fragment seqno sets in mysql.wsrep_streaming_log identical across all
  Synced primary-component nodes per (node_uuid, trx_id), "SR fragment state agrees
  cluster-wide")`.
- `assert_always(wsrep_streaming_log row count returns to ~0 at quiescence when all
  workload SR transactions have committed/rolled back, "no orphaned SR fragments
  accumulate")` — catches the leak shape without a per-row diff.
- `assert_sometimes(a node was killed while it had an SR transaction with >=1 certified
  fragment in flight, "crash-with-open-SR-transaction explored")` — arms the property.

## Instrumentation suggestions (all missing)

- **SUT-side `Sometimes`** at `wsrep_schema.cc:1211-1218` (NULL-seqno fragment deleted at
  recovery): the exact point-B signature — proves the window was hit and anchors replay;
  today it is completely silent (not even a log line).
- **SUT-side `Always`** at `server_state.cpp:1645-1648` ("stale entries must be removed
  manually" branch): assert it is never reached — it is a property violation being
  announced by the SUT itself; at minimum a workload log-watch trigger.
- **Missing**: re-enable the `#if 0` SR double-commit assert
  (`wsrep-lib/src/transaction.cpp:568-582`) as an SDK `Always` — the authors disabled it
  because it fired; each firing is triage-worthy.

## Timing / config dependencies

- Crash timing: the certify-to-storage-commit window is microseconds — Antithesis's value
  is precisely exploring this; debug builds add deterministic crash points.
- Membership churn during active streaming exercises orphan-cleanup and dummy-writeset
  paths (`equal_consecutive_views` logic).
- SR + TOI DDL concurrently is a suspected lock-order-inversion shape — same workload
  feeds both investigations.

## Open Questions

- Is the galera_3nodes_sr GCF-810 suite absence an extraction artifact or deliberate
  disablement? `(needs human input)` — if Percona runs these internally, some ground is
  covered and the property should emphasize the membership-churn variants MTR cannot
  model; the repo itself contains no runnable SR crash-consistency test (include files
  verified missing).

The four code-resolvable questions are resolved — see Investigation Log. Net effect: the
designed recovery paths converge in both directions; the property's real targets narrow to
the residuals — the `adopt_error` "removed manually" path, the `s_prepared` (XA) exemption
in orphan cleanup, and gcache-purge/SST edges. Fragment payload-hash comparison is NOT
needed (no cross-incarnation key collision is possible); the (server_id, trx_id, seqno)
set diff suffices.

### Investigation Log

#### Does cert-index preload / IST re-deliver the deleted fragment to the recovered origin?

- Examined: `gcomm/src/pc.cpp:285-330` + `gcomm/src/gcomm/uuid.hpp:86-105` (restart
  identity: pc.recovery restores the old UUID but calls `increment_incarnation()`; the
  incarnation is part of full-UUID equality, so a crash-restarted node ALWAYS rejoins with
  a different member UUID — fresh random UUID when gvwstate is absent);
  `wsrep-lib/src/server_state.cpp:366-454` (missing-context fragment apply creates a
  streaming applier), `sql/wsrep_schema.cc:1122-1250` (`recover_sr_transactions`),
  `server_state.cpp:1055-1084` (`on_primary_view` → recover appliers + close orphans).
- Found: position-dependent. If the origin's recovered position < F, IST re-delivers
  fragment F — and because the rejoined node's UUID differs from the old incarnation, F
  arrives as a *foreign* writeset, hits the missing-applier path, re-creates a streaming
  applier and re-appends the fragment; the next view's orphan cleanup then rolls it back
  (old server_id is absent from every future view). If later commits advanced the SE
  checkpoint past F before the crash, F is never re-delivered — but peers already rolled
  the orphan back at the crash view (see next entry), so the transaction ends absent
  everywhere either way.
- Conclusion: resolved — both branches converge BY DESIGN; the crash window is not a
  direct divergence generator on the designed path. Divergence requires a residual
  (adopt failure, prepared-state exemption, gcache-purged IST range), which is what the
  cross-node fragment diff is for.

#### Does `close_orphaned_sr_transactions` clean peers' copies when the ignorant origin rejoins?

- Examined: `server_state.cpp:1567-1680` (rollback condition:
  `equal_consecutive_views || not current_view_.is_member(server_id)`), `:1083` (invoked
  on EVERY primary view), UUID incarnation semantics above.
- Found: cleanup does not wait for (or depend on) the origin's rejoin — peers roll back
  the orphaned streaming applier and remove its fragments at the first primary view in
  which the old origin UUID is absent, i.e. the crash view itself. The rejoined origin has
  a different UUID, so the old server_id can never "return" and mask the orphan.
- Not found: any path where an absent-origin orphan survives a primary view — except
  `adopt_error` (rolls back but "leaving stale entries ... removed manually",
  `:1645-1648`) and transactions in `s_prepared` (XA), which are exempt by design.
- Conclusion: resolved — peers clean up reliably on the designed path; the two exemptions
  are the property's residual targets and the quiescence bound can be one view change +
  drain, not origin-rejoin-dependent.

#### Can trx_id reuse by the recovered origin collide with retained peer-side fragments?

- Examined: `galera/src/wsdb.cpp:105-165` (pthread_self keying applies only when
  trx_id == -1), `sql/sql_class.h:3455-3488` + `:5003` (PXC always assigns
  `wsrep_next_trx_id` from `query_id`, monotonic for the server's lifetime),
  streaming state keying: `streaming_appliers_map` and `mysql.wsrep_streaming_log` both
  key on (server_id/node_uuid, trx_id); UUID incarnation semantics above.
- Found: within one incarnation trx_ids never repeat (query_id monotonic); across
  restarts the server_id component always differs (incremented incarnation or fresh UUID),
  so a reused trx_id can never key-collide with retained old-incarnation fragments or
  "removed manually" leftovers.
- Conclusion: resolved — no wrong-data upgrade; stale-entry severity stays
  "leak/unbounded growth". Payload-hash comparison unnecessary.

#### Does a crash between `remove_fragments` and commit leave committed-trx fragments that recovery mishandles?

- Examined: `wsrep-lib/src/transaction.cpp:286-345` (`before_prepare`: for local
  streaming commits, `client_service_.remove_fragments()` executes INSIDE the transaction
  that is about to commit; the `crash_last_fragment_commit_{before,after}_fragment_removal`
  debug points bracket it), `recover_sr_transactions` (fragments WITH seqnos are re-applied
  into a recovery streaming applier).
- Found: fragment removal is transactional with the final commit — a crash before local
  commit rolls the removal back together with the data, leaving the fragment rows intact
  with assigned seqnos; recovery rebuilds a streaming applier from them. If the commit
  fragment had already certified, IST re-delivers it to that (server_id, trx_id)-matched
  recovery applier, completing the commit and the removal; if not, orphan cleanup rolls
  the trx back everywhere. There is no state in which the local table durably holds
  fragments of a locally-committed transaction.
- Conclusion: resolved — the opposite direction is handled by the same recovery/orphan
  machinery; both directions remain in scope for the cross-node diff (as the invariant
  already states), with triage now able to attribute each.

## Synthesis refinement (2026-09-10)

SR UN-GATED: wsrep_trx_fragment_size is SESSION_VAR HINT_UPDATEABLE (sys_vars.cc:8575-8583) — no server config variant; the workload enables SR per session (dedicated workload phase). Non-crash legs are v1-viable; the crash windows remain gated on the workload->supervisor kill channel.

#### Is the galera_3nodes_sr GCF-810 suite absence deliberate?

- Examined: `mysql-test/suite/galera_3nodes_sr/` (the GCF-810A/B/C test files and the
  include files they reference).
- Found: the test files exist in-tree but the include files they source are missing, so
  the suite cannot run — the repo contains no runnable SR crash-consistency test.
- Not found: any in-repo comment, commit message, or doc explaining whether the missing
  includes are an extraction/vendoring artifact or a deliberate removal of the suite.
- Conclusion: tagged `(needs human input — the repo contains no runnable SR
  crash-consistency test; include files verified missing)`. Only Percona/upstream can say
  whether the suite was meant to ship; the answer decides whether its scenarios are
  trusted prior art for this property's crash legs or must be re-derived from scratch.
