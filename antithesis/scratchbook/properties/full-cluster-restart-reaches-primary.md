---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# full-cluster-restart-reaches-primary — Full-cluster restart re-forms Primary and opens for service within a bound

**Focus area:** Lifecycle transitions — full-cluster shutdown/bootstrap, pc.recovery.

**Provenance note:** independently discovered by focus 3 (failure recovery) and focus 8
(lifecycle) under the same slug; this file is the reconciled evidence covering both
angles. Focus-3 addition folded in: a torn gvwstate.dat is *silently discarded* by the
strict PXC parse (the node then behaves as if no restored view exists — a different,
bootstrap-dependent path), so a crash mid-gvwstate-write changes which liveness scenario
the restart lands in; the checker's "within T of last node start" formulation covers both.

## Claim under test

After all nodes stop (crash or ungraceful kill) and all are restarted, the cluster
re-forms a Primary Component via pc.recovery (gvwstate.dat) and every node reaches
`wsrep_ready=ON` within a bounded time — without operator intervention
(`pc.bootstrap`/`--wsrep-new-cluster`). The known hole: the default configuration can wait
**forever** before opening the SQL port.

## Code paths (verified at commit f9ecb3e; galera paths in percona-xtradb-cluster-galera/)

- **Indefinite wait is the default**: `gcomm/src/pc.cpp:92-114` — when a restored view is
  `V_PRIM` and `pc.wait_restored_prim_timeout == PT0S` (the default,
  `gcomm/src/defaults.cpp:67`), `wait_prim = false` and the code logs "The server will
  wait indefinitely to reach PC." mysqld meanwhile blocks in untimed
  `wsrep::server_state::wait_until_state` (`wsrep-lib/src/server_state.cpp:1498-1517` —
  plain condvar loop, throws only if the state goes disconnecting) and never opens the
  SQL port. If even one node of the restored view stays down (or restarts with a new
  IP — DNS resolved once at connect, `gcomm/src/gmcast.cpp:285-309`), the remaining nodes
  can wait forever.
- **pc.recovery state**: gvwstate.dat written on every primary view
  (`gcomm/src/pc.cpp:299-316` — restore at construct, incarnation bump, rewrite), and
  **deleted on graceful close** (`PC::close` → `ViewState::remove_file(conf_)`,
  `gcomm/src/pc.cpp:262`). So the restored-PRIM wait path is reached only after
  *ungraceful* full-cluster stops — exactly the Antithesis crash case. After a graceful
  full stop there is no gvwstate and the cluster needs explicit bootstrap
  (safe_to_bootstrap) — a different, operator-dependent path this property excludes.
- Re-merge preconditions: restored-prim re-formation requires ALL non-evicted members of
  the greatest last-prim view present (`gcomm/src/pc_proto.cpp:1052-1056`, per
  sut-analysis §6.4) — one flapping node blocks it indefinitely.
- Related bootstrap-safety context (separate property territory, noted for the relationship
  file): `safe_to_bootstrap_ = (memb_num == 1)` set on every singleton primary view
  (`galera/src/replicator_smm.cpp:3245`) means two singleton partitions killed and
  restarted can *both* bootstrap → split brain. This property's checker (single Primary
  component, one cluster UUID) would also catch that outcome if the workload/harness ever
  bootstraps.

## Failure scenario

Antithesis kills all N nodes (or the harness restarts the whole fleet); all restart with
gvwstate.dat present. One node's container gets a new IP or is delayed past the others'
gmcast reconnect horizon. The other N-1 nodes sit in "waiting indefinitely to reach PC"
with the SQL port closed — the cluster never returns, no error, no timeout, nothing for an
operator to act on except logs. Confirmed shape by `galera_restored_pc.test`; the parameter
is undocumented (in-repo docs don't mention `pc.wait_restored_prim_timeout`).

## Suggested implementation

- **Workload-side (primary)**: after any window in which all nodes were simultaneously
  down, assert at final quiescence `Always`: "within T of the last node's process start,
  every node reports wsrep_cluster_status=Primary, wsrep_ready=ON, and one common
  wsrep_cluster_state_uuid". The single-UUID clause makes the checker also catch
  dual-bootstrap split brain.
- **Sometimes markers (missing, distinct outcomes)**:
  - "primary component restored from gvwstate.dat" (the "Restoring primary-component from
    disk successful" branch, `gcomm/src/pc.cpp:301-305`);
  - "restored-prim wait entered with PT0S timeout" (`pc.cpp:106-110`) — the danger state;
  - "full cluster re-formed Primary after total outage" (workload-side, on the checker
    passing after an all-down window) — confirms the scenario was generated at all.
- Config note: run one variant with the default `PT0S` (field behavior — the liveness
  property will fail if Antithesis finds the wedge, which is the point) and optionally one
  with a finite `pc.wait_restored_prim_timeout` to compare.

## Assertion type

Liveness — workload-side `Always` on the bounded re-formation condition at quiescence. The
danger-state marker is a separate `Sometimes`.

## Fault requirements

**Requires the ability to take all nodes down ungracefully — node termination faults or a
harness-driven full-fleet restart. Flag: node termination is often disabled by default;
without it this property's trigger never occurs (network partitions alone don't clear the
processes, and graceful stops delete gvwstate and change the scenario).** Clock jitter is
not required. DNS/IP churn on restart (container re-IP) is a valuable amplifier given the
one-shot address resolution.

## Confidence

High on mechanism (indefinite-wait branch read directly, default confirmed); high on
real-world impact (full-datacenter power events are the canonical Galera incident); the
main uncertainty is harness capability to produce all-down windows.

## Open questions

- Do Percona's packaged my.cnf / container entrypoints override
  `pc.wait_restored_prim_timeout`? If any shipped config sets it non-zero, the property
  should test that value instead and the "indefinite" branch becomes an explicit
  misconfiguration test. `(needs human input — re-confirmed no override anywhere in-tree:
  build-ps/, scripts/, support-files/, mysql-test/ carry no wsrep_provider_options default
  for it; packaged docker entrypoints/operator configs live outside this repo)`

Resolved (see Investigation Log): the state exchange provably elects the most-advanced
recovered state, so no separate behind-node-election safety companion is needed here — the
residual risk reduces to recovered-position truthfulness, already owned by
`grastate-se-checkpoint-agreement` and `crash-recovery-grep-yields-true-position`. The
bootstrap env-poisoning vector is an environment-design note owned by
`cluster-identity-single-lineage`, not an open question on this property.

### Investigation Log

#### Can the restored PC elect a behind node's state without SST after staggered crashes?

- Examined: `gcs/src/gcs_state_msg.cpp:486-514` (`state_nodes_compare`), `:537-615`
  (`state_quorum_inherit`), `:617-687` (remerge candidate matching/selection), `:689-745`
  (`state_quorum_remerge` incl. the pc-recovery comment), `:804`/`:895`
  (quorum->act_id = rep->received in remerge/bootstrap paths).
- Found: in every quorum path the representative is the most-advanced node — inherit path:
  highest `received` (applied seqno) among JOINED/DONOR nodes, tie-break higher `prim_seqno`;
  remerge/bootstrap path: candidates keyed by (state_uuid, received, prim_seqno), winner =
  highest `prim_seqno` then highest `state_seqno`. `quorum->act_id` is set from the winner's
  `received`; behind nodes then fail `state_transfer_required` and take IST/SST from the
  advanced state. The release build performs the same selection (the `:498/:502` asserts about
  received/prim_seqno ordering consistency are debug-only, but their violation would still
  resolve to the max-received node in the inherit path).
- Not found: any path that installs a lower `received` as group state while a more advanced
  JOINED node is present in the exchange.
- Conclusion: resolved — pc.recovery + state exchange elect the most-advanced *reported*
  state. The remaining safety exposure is a node under-/over-reporting its recovered position,
  which is exactly the territory of `grastate-se-checkpoint-agreement` and
  `crash-recovery-grep-yields-true-position`; no new companion property needed here.

#### Do packaged configs override pc.wait_restored_prim_timeout?

- Examined (this pass): grep of `build-ps/`, `scripts/`, `support-files/`, `mysql-test/` for
  `wait_restored_prim`; `gcomm/src/defaults.cpp:67` and `gcomm/src/conf.cpp:123-124,193`
  confirming the PT0S default registration.
- Found: no in-tree override anywhere; default is PT0S (wait forever).
- Conclusion: remains `(needs human input)` — only field-side packaging (docker entrypoints,
  k8s operator) outside this repo could override it; ask the customer.

#### Interrupted-bootstrap env poisoning — relevant to this property?

- Examined: `scripts/mysqld_bootstrap.in:30-39`; harness-supervision assumptions in the
  catalog's Assumptions section.
- Found: the vector exists only under systemd supervision using the bootstrap wrapper; a
  docker-compose harness never runs it, and the catalog already carries the vector as
  `cluster-identity-single-lineage`.
- Conclusion: resolved as an environment-design note — decide supervisor shape at environment
  design time; no open question remains on this property.
