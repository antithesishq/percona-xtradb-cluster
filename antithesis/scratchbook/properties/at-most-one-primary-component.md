# at-most-one-primary-component — Evidence

**Focus area:** Distributed coordination — quorum / split-brain.
**Confidence:** High (core mechanism read directly; scenario is the canonical Galera safety claim).

## Claim under test

At no time do two disjoint sets of nodes both report `wsrep_cluster_status = Primary` with
different cluster views. Galera's Primary Component (PC) protocol claims exactly one
partition can hold quorum.

## Code (validated against this repo, commit f9ecb3e; galera submodule @13ff9ed6)

All paths under `percona-xtradb-cluster-galera/`:

- `gcomm/src/pc_proto.cpp:555-575` — `have_quorum()`: weighted sum, strict `>` —
  `weighted_sum(memb ∩ pc_view)*2 + weighted_sum(left ∩ pc_view) > weighted_sum(pc_view.members)`.
  Nodes that *gracefully left* count toward the survivor's quorum; partitioned (failed) nodes
  do not. Falls back to unweighted counts if any weight is missing.
- `gcomm/src/pc_proto.cpp:578-598` — `have_split_brain()`: the exact-tie case (`==`), used only
  to select the log message when `pc.ignore_sb` is set. At default config an exact 50/50 split
  makes BOTH sides non-primary (quorum test is strict `>`).
- `gcomm/src/pc_proto.cpp:601-644` — `handle_trans()`: no quorum → `mark_non_prim()` +
  deliver non-prim view, UNLESS `pc.ignore_sb` (:614-620) or `pc.ignore_quorum` (:621-627) is
  set — both are runtime-settable via `SET GLOBAL wsrep_provider_options`, so the guarantee is
  voidable from SQL.
- `gcomm/src/pc_proto.cpp:1064-1100` — `handle_state()`: conflicting-primaries detection on
  re-merge. Tiebreak: with `pc.npvo=false` (default, `gcomm/src/defaults.cpp:64`) the node
  whose `last_prim()` is GREATER (newer) hits `gu_throw_fatal` and **aborts the process**
  (:1092-1098); the older prim view wins. So "two primaries merged" is resolved by killing one
  node, not by reconciling data — any writes accepted by the aborted side are lost.
- `gcomm/src/pc_proto.cpp:1345` (region) — `handle_trans_install` hard
  `gcomm_assert(have_quorum(...))`; `gcomm_assert` = `gu_throw_fatal`, LIVE in release
  (`gcomm/exception.hpp:21-22`).
- Weighted arithmetic input: `pc.weight` default 1 (`defaults.cpp:69`), runtime-settable per
  node — weight changes concurrent with a partition are a known-hard corner (Galera docs
  describe transitional weight semantics).

## Failure scenario

1. 3- or 5-node cluster under write load. Antithesis injects an asymmetric partition
   (A sees B, B does not see A) — exactly what the in-tree tests never do (`gmcast.isolate`
   is symmetric, MTR runs on 127.0.0.1).
2. EVS on the two sides converges to different views; PC evaluates quorum on each side.
3. Bug shapes: (a) both sides conclude quorum (e.g., asymmetric views where each side counts
   the other in `left` or in `members` inconsistently — the quorum formula counts gracefully-left
   members positively, and a LEAVE message delivered to only one side is an asymmetry the
   protocol must handle); (b) weight change (`pc.weight`) in flight during the partition;
   (c) flapping link causing rapid V_TRANS/V_REG churn where a stale `pc_view_` is used as the
   denominator.
4. Violation: two disjoint Primary Components both accept and certify writes → divergent
   histories; on heal, one side is killed by the npvo tiebreak (data loss) or, worse, the merge
   succeeds silently.

## How to check (workload-side)

Poll every node ~1/s: `wsrep_cluster_status`, `wsrep_cluster_state_uuid`,
`wsrep_cluster_conf_id`, `wsrep_cluster_size`, `wsrep_incoming_addresses`. Violation predicate:
two nodes simultaneously (same poll round, tolerating one poll period of skew) report
`Primary` with views whose member sets are disjoint (sum of the two `wsrep_cluster_size`
values ≤ N and conf_ids incomparable / state histories diverged). Because polling is racy,
the strict form is: two nodes report Primary with **disjoint** `wsrep_incoming_addresses`
sets. Disjointness is what makes it sound — overlapping views are just staleness.

Secondary oracle (stronger, catches transient dual-PC): monotonic per-partition writes with
node-tagged unique keys; after heal, if both sides committed writes in the same seqno range
with different content, dual-PC existed.

## Assertion type

- `Always`: "no two disjoint node sets simultaneously Primary" — safety, must hold on every
  evaluation. Workload-side (SDK in the test driver).
- Companion `Sometimes`: "a partition produced a non-Primary side that later re-merged" —
  confirms the interesting state is actually reached (otherwise the Always is vacuous).

## Instrumentation suggestions (all missing — no Antithesis SDK anywhere in tree)

- SUT-side `Always` at `pc_proto.cpp` quorum decision (`handle_trans` :612 and
  `handle_reg`/install path): assert `!(have_quorum && have_split_brain)` and emit the
  computed weighted sums as detail — makes the arithmetic itself checkable.
- SUT-side `Reachable` on `mark_non_prim()` (:632) and on the conflicting-prims branch
  (`pc_proto.cpp:1079-1098`) — the npvo tiebreak path is exercised by no MTR test with real
  partitions; hitting it at all is signal.
- SUT-side `Unreachable` on the `gu_throw_fatal` at :1094 under default config *if* the
  harness never runs `pc.bootstrap` on both sides — reaching it without operator error means
  the protocol itself manufactured two prims.

## Fault requirements

- Network partitions including **asymmetric** ones (default-on in Antithesis) — primary lever.
- No node termination required for the base property; clock jitter not required (gcomm is
  monotonic-clock only, `gu::datetime::Date::monotonic()` throughout).

## Open questions

None — all three original questions resolved (see Investigation Log). Consequences folded in:

- The asymmetric-LEAVE split-brain recipe is arithmetically precluded within a shared pc_view
  epoch; the trigger list shrinks to weight-change races (which have an explicit conservative
  in-code defense) and different-pc_view-epoch merges (guarded by the conflicting-prims npvo
  tiebreak). The `Always` disjointness check stays exactly as specified — it now primarily
  guards those residual channels plus any protocol bug Antithesis manufactures.
- Poll-based checking must tolerate the confirmed status-variable lag window; the disjointness
  formulation already does (a stale "Primary" reading carries the old overlapping member set).

### Investigation Log

#### Can asymmetric LEAVE delivery make both sides pass the strict-`>` test?

- Examined: `gcomm/src/pc_proto.cpp:555-575` (`have_quorum`), `:578-598`
  (`have_split_brain`), `:601-644` (`handle_trans`); `gcomm/src/evs_proto.cpp:1173-1241`
  (`deliver_reg_view`), `:1243-1308` (`deliver_trans_view`), `:4681-4733` (`handle_leave`).
- Found: (a) a node enters a view's `left` set only via `mn.leaving()` — i.e. only if it sent
  an EVS LEAVE, which happens solely on its own close path; a node cannot be a live `member`
  of one concurrent view and `left` of another. (b) Both partition sides descending from the
  same primary component evaluate quorum against the same `pc_view_` denominator. Arithmetic:
  with disjoint member splits m_A, m_B, leavers L (creditable to both sides at most once each
  per side), partitioned weight p, the two numerators sum to at most
  2·(W(m_A)+W(m_B)+W(L)) = 2·(W(pc_view) − p); both sides exceeding W(pc_view) strictly would
  require p < 0. Impossible — even with the peer's LEAVE cross-delivered to both sides. The
  same holds for the unweighted fallback.
- Found (residual channels): both sides passing requires *different* denominators, i.e.
  different `pc_view_` epochs — a non-prim node cannot regain prim through `handle_trans`, and
  the divergent-epoch merge case is detected in `handle_state` (`pc_proto.cpp:1064-1100`,
  npvo tiebreak `gu_throw_fatal` on one side); or config overrides `pc.ignore_sb` /
  `pc.ignore_quorum` (`:614-627`).
- Conclusion: resolved — not reachable via LEAVE asymmetry; trigger list shrinks as described.

#### Does a pc.weight change replicate transactionally w.r.t. concurrent view changes?

- Examined: `gcomm/src/pc_proto.cpp:1710-1769` (`set_param` PcWeight), `:219-252`
  (`send_install` with weight), `:1332-1416` (`handle_trans_install` F_WEIGHT_CHANGE),
  `:1104-1153` (state-exchange weight reconciliation).
- Found: weight changes are only accepted in S_PRIM and propagate as a group-total-ordered
  install message flagged F_WEIGHT_CHANGE, applied at delivery on every node. The race window
  (weight-change install delivered in a V_TRANS view while the previous pc_view has
  partitioned members) is explicitly handled: the node deliberately goes non-prim
  ("Weight changing trans install leads to non-prim", `:1358-1371`) because the partitioned
  component's disposition of the change is unknowable. State exchange additionally reconciles
  weight maps (`:1125-1139` "overriding reported weight").
- Conclusion: resolved — not transactional, but totally ordered with a conservative
  availability-sacrificing fallback; the race is a spurious-non-prim liveness shape, not a
  split-brain recipe. The fallback branch itself is a good `Sometimes` target for the
  partition-heal property's workload (weight churn under partitions).

#### Is there a window where the status variable says Primary after the provider delivered non-prim?

- Examined: `sql/wsrep_server_service.cc:349-404` (`log_state_change`),
  `sql/wsrep_mysqld.cc:217`, `sql/mysqld.cc:13541`.
- Found: `wsrep_cluster_status` is assigned only inside `Wsrep_server_service::log_state_change`,
  which runs when the wsrep-lib server-state transition is processed from the provider's
  ordered event stream (recv-queue). The PC's internal non-prim decision precedes that by the
  view-delivery + state-transition latency, which grows with recv-queue depth under load.
- Conclusion: resolved — the window exists and is unbounded under queue backlog; the checker
  must keep the disjoint-`wsrep_incoming_addresses` formulation (stale Primary readings show
  the old, overlapping member set, so disjointness remains sound regardless of lag).
