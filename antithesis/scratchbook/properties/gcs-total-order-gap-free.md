# gcs-total-order-gap-free

**Focus:** Protocol contracts — the total-order / gap-freedom axioms of the group
communication stack, and the inconsistent enforcement across layers.
**Confidence:** High (all three enforcement sites read directly at commit f9ecb3e).

## Claimed contract

The entire PXC correctness story rests on one axiom: EVS delivers O_SAFE messages to every
member gap-free and in identical total order (doc/source/manual/certification.rst:91-103;
wsrep_api.h:365-367). The same axiom appears at three layers with **three different
enforcement policies** — the inconsistency is itself the finding:

| Layer | Site | Release-build behavior on violation |
|---|---|---|
| PC | `gcomm/src/pc_proto.cpp:1486-1497` — per-source O_SAFE seq must be `last_seq+1` | `gu_throw_fatal` — **live in release**, process dies (and `gu_abort()` suppresses the core dump) |
| GCS | `gcs/src/gcs_core.cpp:639-655` — local action returned by the group must match the send FIFO head (id and size) | `-ENOTRECOVERABLE` → `usleep(1s)` + `gu_abort` (gcs_core.cpp:1355-1362) |
| Certification | `galera/src/certification.cpp:1242-1257` — `global_seqno == position_+1`, `last_seen` within index | **log_debug only, then `position_ = trx->global_seqno()`** — gap absorbed silently ("perfectly normal if trx rolled back"; the stricter assert was "given up", :1121-1122 per sut-analysis) |
| EVS input filter | `gcomm/src/evs_proto.cpp:2474-2479` — message whose source claims the current view but is **not in it** | `log_warn` + `assert(0)` (compiled out) + **silent drop** |

The EVS silent drop is the sharpest edge: dropping a message from a
claims-current-view-but-absent node consumes nothing from that source's sequence, so if the
node is later (re)admitted or its earlier messages were already counted, the drop
manifests downstream as exactly the PC gap that `gu_throw_fatal` punishes — i.e. one
layer's "harmless warning" is another layer's fatal axiom violation.

## Failure scenario

Antithesis network faults (partition, reorder-adjacent effects via connection churn and
relaying, asymmetric partitions, GMCast segment relay) drive membership churn: a node is
expelled from the view while its in-flight messages are still being relayed; peers receive
messages from an in-view-claiming absent source (evs_proto.cpp:2474) or observe a
per-source O_SAFE gap (pc_proto.cpp:1489). Result in release builds ranges from a silent
drop (potential divergence precursor) to a simultaneous multi-node `gu_throw_fatal`
(correlated cluster death for a transient network condition).

## Suggested assertions (all missing — SUT-side SDK instrumentation)

- **Unreachable (SUT-side, pc_proto.cpp:1489 branch):** "PC observed O_SAFE sequence gap".
  Distinct message per layer; today this is a crash — the SDK assertion adds triage signal
  before the abort and survives even if the abort is ever softened.
- **Unreachable (SUT-side, gcs_core.cpp:639 and :646 branches):** "GCS send/recv FIFO
  violation (total order broke for local action)".
- **Unreachable (SUT-side, evs_proto.cpp:2477 branch):** "EVS message from node claiming
  current view but absent from it". **Highest value of the three**: in release builds this
  is completely silent today (`assert(0)` compiled out), yet it is the precursor state for
  both the PC fatal and the certification silent-absorb. MTR even suppresses related
  warnings in CI (mtr_warnings.sql — "Gap in state sequence", "JOIN message from member ...
  in non-primary configuration"), so no existing test can see it.
- **AlwaysOrUnreachable (SUT-side, certification.cpp:1242):** when the seqno-gap branch is
  taken, the gap is attributable to a locally-rolled-back trx (the claimed benign cause) —
  practical proxy: gap size accounted against dummy/rolled-back writesets. If that
  attribution can't be computed cheaply, downgrade to a `Sometimes` ("certification
  absorbed a seqno gap") so runs that hit it become inspectable, rather than asserting an
  invariant the layer explicitly chose not to enforce.

## Assertion-type rationale

`Unreachable` matches "impossible states / critical failure paths that must never be
observed": these branches are the system's own definition of a broken axiom. They are also
ideal exploration anchors — Antithesis gets credit for approaching them. The certification
site gets the weaker type because upstream deliberately tolerates gaps there.

## Fault requirements

Default-on network faults are exactly the right adversary (this is pure
membership/transport protocol). No node termination or clock faults required — though
termination adds the expelled-node-mid-relay shape. Note for harness: `gu_abort()`
suppresses core dumps (gu_abort.c:29-58) — patch or wrap in the image so the PC/GCS fatal
paths remain triageable.

## Open questions

- Can a collided-view divergence (the precondition of the EVS drop) survive a re-merge state
  exchange and reach the PC per-source gap fatal, or is it always caught first by the
  state-exchange consistency fatals ("last prims / TO seqs not consistent")? `(partial: local
  starvation ruled out — the drop's source has no input-map slot, so it is outside the local
  delivery set and PC tracking; PC last_seq does persist across views and is exchanged in
  state messages, so a cross-node divergence would surface at re-merge, but the full re-merge
  trace was not completed)`

Resolved (see Investigation Log): the EVS silent drop cannot create a *local* O_SAFE gap in
the current view — its correct framing is "the only local observable of a view-id collision,"
whose damage manifests cross-node at re-merge, not as the local PC fatal. The adjacent V_REG
gcomm_assert is not reachable via network faults alone; keep it as an opportunistic tripwire
only. The three planned `Unreachable` markers stand unchanged.

### Investigation Log

#### Can the evs_proto.cpp:2474 drop starve a to-be-delivered O_SAFE sequence (creating the PC gap)?

- Examined: `gcomm/src/evs_proto.cpp:2420-2511` (input filtering incl. the drop branch),
  `:1173-1241` (`deliver_reg_view` incl. the same-view-id abort guard at `:1230-1236`),
  `:4785-4800` (regenerated installs get *higher* view seq); `gcomm/src/pc_proto.cpp:1463-1509`
  (`handle_user` per-source gap check), `gcomm/src/pc_message.hpp:180` (`last_seq_` is
  persistent per-node state), `pc_proto.cpp:430` (last_seq set to 0 only at instance
  creation), `:785`/`:1240` (last_seq compared in state exchange).
- Found: the drop fires only when the source has `index() == invalid_index` — no input-map
  slot — meaning the source is not a member of the receiver's installed current view while
  claiming that view's id. Locally, delivery sets and PC per-source tracking cover only view
  members, so the dropped source's messages were never going to be delivered or counted by
  this receiver: no local per-source O_SAFE gap can result in the current view. Reaching the
  branch at all implies two different views sharing one view id (the exact catastrophe the
  `deliver_reg_view` gcomm_assert aborts to prevent; regenerated installs bump the seq, so a
  same-version representative shouldn't mint the collision). Cross-view residual: PC
  `last_seq` persists across views and is part of state-exchange consistency checks, so a
  collided-view divergence would be confronted at re-merge — via state-exchange fatals
  (`is_prim` "last prims not consistent" / "TO seqs not consistent",
  `pc_proto.cpp:923-935`) and only conceivably via the `:1489` gap fatal if the exchange
  passed.
- Not found: a completed trace showing whether a collided-view re-merge can pass
  `validate_state_msgs`/state exchange and reach `:1489` (the remaining `(partial)`).
- Conclusion: the EVS `Unreachable` keeps full value as the sole local observable of a
  view-id collision; its evidence framing changes from "precursor of the local PC fatal" to
  "cross-node divergence detector whose blast lands at re-merge."

#### Can network faults present an out-of-view source during V_REG (pc_proto.cpp:1479)?

- Examined: `gcomm/src/pc_proto.cpp:1463-1509` (`handle_user`: the membership check runs only
  when `prim() == false`; in prim, `instances_.find_checked` would throw for an unknown
  source), EVS delivery discipline (input-map slots exist only for current-view members;
  upcalls are strictly ordered — pending old-view messages are delivered before the new view
  upcall; V_TRANS deliveries from previous-view members arrive while PC's current view is the
  trans view).
- Found: by ordered-upcall construction, once PC's `current_view_` is a V_REG, every
  subsequent user message source is a member of that view; out-of-view sources are only
  presentable during V_TRANS, which is exactly what the `gcomm_assert(type == V_TRANS)`
  encodes.
- Not found: any fault-reachable reordering that delivers a non-member user message after a
  V_REG view upcall (would require an EVS delivery-ordering bug).
- Conclusion: resolved — not a fault-reachable fourth death site; worth at most a free
  opportunistic `Unreachable` if the provider is instrumented anyway, not a targeted one.
