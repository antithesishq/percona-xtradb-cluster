# acked-commit-durable-across-restart — Acknowledged commits survive node crash and rejoin

**Cross-link (synthesis):** shares the client-side ack journal infrastructure with
`non-primary-rejects-writes` (whose Always is "acked write present exactly once cluster-wide
after convergence"). Boundary: that property covers acks under partitions/readiness churn
with no process death; this property covers acks across node crash/restart (and the
flush=1 full-cluster-crash variant). One journal implementation serves both.

**Type:** Safety | **Assertion:** Always | **Confidence:** High (single-node variant), Medium (cluster-wide variant)

**FAULT REQUIREMENT: node termination (kill/restart) — often DISABLED by default in
Antithesis; must be enabled for this property. The cluster-wide variant additionally
benefits from disk faults.**

## What led to this property

Marketing/doc claim: "loose any node at any point ... without any data loss"
(doc/source intro.rst:28-30) vs the in-code caveat "successful return code does not
guarantee delivery to group" (`gcs/src/gcs_core.hpp:102`). The commit protocol places
replication+certification at `wsrep_before_prepare` (`sql/handler.cc:2580`), so by the time
a client receives OK on COMMIT the writeset has been replicated in total order — the claim
is structurally plausible for single-node loss but rests on a long chain: monitors,
gcache, IST/SST, grastate/XID recovery. MTR always runs `innodb_flush_log_at_trx_commit=2`
and never tests durability composition (sut-analysis §10.2); "acked commit survives
cluster-wide power loss" requires flush=1 and is tested nowhere (§6.7).

## Mechanism / code involved

- Commit path: `wsrep_before_prepare` = replication + certification
  (`sql/handler.cc:2568-2619`), then binlog group commit, `commit_order_enter/leave`
  (wsrep-lib `transaction.cpp:525-595`), engine commit, client ack.
- Recovery chain a crashed node depends on: InnoDB wsrep XID
  (`storage/innobase/trx/trx0sys.cc:529`, redo-logged), grastate.dat
  (`galera/src/saved_state.cpp:364` — in-place, non-atomic, warn-only failure),
  `--wsrep-recover` (`sql/wsrep_mysqld.cc:1349-1364`), then IST from donor gcache or SST.
- The surviving-majority side is what actually guarantees the data after a single-node
  crash; the crashed node's own recovery correctness is covered by the sibling properties
  (grastate-se-checkpoint-agreement-at-startup, gcache-recovered-ist-completeness).
- Wsrep-lib's three numbered commit guarantees ("ordered trx cannot be BF-aborted and can
  always finish committing", `wsrep-lib/src/transaction.cpp:598-608`) are enforced by
  NDEBUG-compiled-out asserts; the non-assert failure path of `commit_order_leave` quietly
  aborts the ordered txn locally while peers commit (sut-analysis focus 11 §4.6) — an
  ack-then-lose or peer-vs-local divergence seed.

## Failure scenario

1. Single-node: client gets OK on COMMIT against node A; node A is killed within
   milliseconds; the transaction is missing on B/C (would contradict the verified O_SAFE
   receipt guarantee — i.e., a real EVS/gcs bug if it ever happens; see Investigation
   Log), or missing on A after it rejoins (bad recovery position causing IST to skip it —
   PXC-4845 family).
2. Cluster-wide (flush=1 variant): all nodes killed; after recovery+bootstrap, an
   acknowledged transaction is absent (XID checkpoint behind acked commit, grastate/XID
   disagreement, torn TRX_SYS write).

## Invariant / how to check

Workload-side `Always`: the workload writes self-describing rows (client id, monotonic
sequence, node written to) and records, for each COMMIT, whether the OK packet was
received. After any node restart+rejoin (and at quiesced checkpoints), assert every
acknowledged row is present on every Synced primary-component node. Ambiguous outcomes
(connection died before OK) are recorded as "indeterminate" and asserted only for
*presence-consistency* (present everywhere or nowhere — ties into
cross-node-row-equality), never for durability. `Always` because a lost acked commit is
unconditionally a bug.

Companion `Sometimes`: a node kill landed inside the commit window (between certify and
engine commit) at least once — otherwise the interesting interleaving was never explored.

## Timing / config dependencies

- Run two variants: `innodb_flush_log_at_trx_commit=1` (the SHIPPED default — PXC keeps
  MySQL's default 1, no shipped template overrides it; durability contract, cluster-wide
  kill legal) and `=2` (the MTR-suite posture and a common field tuning, though no in-tree
  doc endorses it; only node-loss leaving >=1 survivor is a fair test — cluster-wide kill
  may legitimately lose the last second).
- `wsrep_sync_wait` on the *reading* connections (or an equal-`wsrep_last_committed`
  barrier) when verifying presence, else stale reads produce false alarms.
- The crashed node's rejoin path (IST vs SST) is chosen by gcache coverage — vary write
  volume so both paths occur.
- Note: Docker default 10s stop grace SIGKILLs PXC shutdown (pxc_maint_transition_period
  default 10s eats the whole grace, `sql/sys_vars.cc:8638-8642`) — even "graceful" harness
  stops are effectively kills unless grace is raised; decide deliberately.

## SUT-side instrumentation suggestions (all missing)

- **missing**: `Always` in `Wsrep_high_priority_service`/`transaction::commit_order_leave`
  failure path (wsrep-lib `transaction.cpp` around :595) asserting an ordered transaction
  never fails to commit locally after certification success (turns the "quietly aborts
  locally while peers commit" path into a first-class signal).
- **missing**: `Reachable` on the replay path (`start_of_replay_trx`,
  `replicator_smm.cpp:1224`) — replay-under-crash is exactly where ack-vs-loss races live.

## Open questions

None. (Both resolved 2026-09-10 — see Investigation Log. Net effect: (1) the kill scope
can be widened — killing the originator PLUS additional nodes is a fair test as long as
at least one primary-component member survives with RAM intact, because the ack point
guarantees group-wide physical receipt (O_SAFE), not merely ordering; (2) the check stays
in its survivors + post-rejoin form — no in-tree basis exists for a local-durability
assertion under flush=2; and (3) a catalog correction: flush=2 is the MTR test-suite
posture, NOT the shipped default — PXC ships MySQL's default flush=1 (no shipped config
template overrides it), so the "field-typical =2" variant is a deliberate extra, not the
default posture.)

### Investigation Log

#### What is the exact ack point relative to group delivery — received by all, or only ordered?

Investigated 2026-09-10.

- Examined: `sql/handler.cc:2579-2580`, `sql/wsrep_trans_observer.h:234-259`,
  `wsrep-lib/src/transaction.cpp:364, 1884-1897`, galera `wsrep_provider.cpp:614-645`,
  `replicator_smm.cpp:748-847, 1308-1364`, `gcs/src/gcs.cpp:1813-1828, 2183-2266`,
  `gcs_core.hpp:102-103`, `gcs_group.hpp:204-218`, `gcs_gcomm.cpp:658-660`,
  `gcomm/src/gcomm/order.hpp:33-38`, `evs_proto.cpp:2096, 3161-3167, 3206-3215`,
  `evs_input_map2.cpp:201-210`, `pc_proto.cpp:631-636`; fsync sweep of the galera tree.
- Found: `gcs_repl`/`gcs_replv` blocks until SELF-DELIVERY in total order (send + condvar
  wait, woken only when the receive thread pops the node's own action with its global
  seqno, `gcs.cpp:1813-1828`; seqno assigned only in PRIMARY, `gcs_group.hpp:204-212`).
  Writesets are shipped at `O_SAFE` (`gcs_gcomm.cpp:658-660`): "it is guaranteed that all
  the nodes in group have received the message" (`order.hpp:33-38`), enforced at delivery
  by `safe_seq` = min over every member's all-received-up-to (`evs_input_map2.cpp:201-210`).
  The `gcs_core.hpp:102` caveat ("successful return code does not guarantee delivery")
  applies to `gcs_core_send` only; the blocking self-delivery wait recovers the stronger
  guarantee. Weakenings: transitional (view-change) delivery drops to FIFO
  (`evs_proto.cpp:3206-3215`, justified by EVS message recovery on survivors); "received"
  means received into gcomm RAM — NO per-writeset fsync anywhere on the receive path
  (gcache never synced per writeset); remote apply/commit is NOT waited on.
- Conclusion: RESOLVED — at client OK, the writeset (i) has a total-order seqno in a
  primary component, (ii) is certified, (iii) is committed locally (durably only under
  flush=1), and (iv) has been physically received into RAM by EVERY member of the primary
  component. Therefore simultaneous kill of the originator + any subset leaving >=1
  survivor is a fair single-node-loss-class test (the survivor provably holds the bytes
  and delivers them, at worst under the transitional FIFO rule). No remote durability
  exists: full-cluster kill is only fair under flush=1. Caveat: a lone survivor drops to
  non-Primary and needs `pc.bootstrap` to proceed — a liveness/operator concern, not data
  loss (`doc/source/howtos/crash-recovery.rst:146`).

#### Under flush=2, does PXC claim durability via the other nodes only, or local durability too?

Investigated 2026-09-10.

- Examined: all of `doc/source/**/*.rst` (grep for flush_log/durability/fsync), galera
  `docs/` (contains no technical docs), `support-files/wsrep.cnf*`,
  `build-ps/{rpm,debian/extra}/wsrep.cnf`, `mysql-test/suite/galera*/**.cnf` (330 hits of
  `=2`), `storage/innobase/handler/ha_innodb.cc:24702-24707` (sysvar default).
- Found: NO doc in this tree mentions `innodb_flush_log_at_trx_commit` at all. The only
  durability prose is the unqualified `doc/source/intro.rst:25-27` "You can loose any node
  at any point of time, and the cluster will continue to function without any data loss"
  (not conditioned on flush settings or failure counts), and
  `doc/source/howtos/crash-recovery.rst:159-176` conceding that after an all-node power
  failure "you cannot be sure that all nodes are consistent with each other". The server
  default is 1 (`ha_innodb.cc:24702-24707`) and NO shipped config template overrides it —
  `=2` appears only in the MTR suite (test-speed convenience, uncommented) and two dead
  galera 5.1/5.5 sample cnf files. The popular "=2 is safe because the cluster is your
  durability" rationale has no textual basis in-tree.
- Conclusion: RESOLVED — PXC makes no local-durability claim under flush=2 (it makes no
  flush-conditioned claim at all), so the check correctly asserts presence on survivors +
  the post-rejoin node only; no local-recovery assertion is warranted. The intro.rst
  claim is the guarantee under test, interpreted as survivor-based. Shipped default
  posture is flush=1, which strengthens the case for running the flush=1 variant as
  primary.

## Synthesis refinement (2026-09-10)

Moved from the top-10 to the phase-2 tranche (NOT deleted): dead without ungraceful termination — graceful restarts cannot challenge durability. Implement once the workload->supervisor kill channel (deployment-topology.md) is proven; the shared ack journal still ships in tranche 1 with non-primary-rejects-writes.
