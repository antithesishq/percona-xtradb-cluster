# no-dual-bootstrap-after-full-shutdown — Evidence

**Focus area:** Distributed coordination — safe_to_bootstrap uniqueness / bootstrap safety.
**Confidence:** High that the flag mechanics are as described (read directly); Medium-High that
the dual-persist scenario is reachable (the concurrent-graceful-shutdown race is inferred from
the quorum formula's `left` credit, not yet reproduced).

## Claim under test

After a full cluster shutdown, at most one node's `grastate.dat` carries `safe_to_bootstrap: 1`,
so a restart procedure that bootstraps only where the flag is 1 can never create two divergent
clusters. The cluster-level invariant: all nodes agree on `wsrep_cluster_state_uuid` forever —
two independently bootstrapped histories never both accept writes.

## Code (validated, commit f9ecb3e; galera @13ff9ed6)

- `galera/src/replicator_smm.cpp:3245` — `safe_to_bootstrap_ = (view_info->memb_num == 1);`
  set on EVERY primary conf change (surrounded by asserts that my_state > NON_PRIM :3249).
  Any singleton PRIMARY view arms the flag.
- Enforcement is connect-time only: `replicator_smm.cpp:412-421` — bootstrap requested
  (`--wsrep-new-cluster` or `gcomm://`) with `safe_to_bootstrap_ == false` → WSREP_NODE_FAIL
  with the "edit grastate.dat manually" hint. Nothing prevents two nodes both having 1.
- Persistence: `galera/src/saved_state.cpp:210-235` (`set`), :294 (`write_file`) — grastate is
  rewritten IN PLACE, no temp+rename, write failures are warnings (`write_file` returns void);
  PXC-only early-out :218 skips the write when values are unchanged. Also persisted on close:
  `replicator_smm.cpp:315` (`st_.set(state_uuid_, last_committed(), safe_to_bootstrap_)`).
- Quorum formula credits graceful leavers: `gcomm/src/pc_proto.cpp:555-575` —
  `left ∩ pc_view` counts toward the survivor's numerator. In a 2-member pc_view, receiving
  the peer's LEAVE yields 1*2+1 > 2 → singleton PRIMARY view → flag set to 1.
- How the last-man-standing design works (and where it cracks): sequential graceful shutdown
  gives exactly one node a singleton primary view. **Concurrent** graceful shutdown of the
  last two nodes is a race: each sends LEAVE; if each side delivers the other's LEAVE before
  processing its own close, BOTH get singleton primary views and BOTH persist
  `safe_to_bootstrap: 1`. Asymmetric partition at shutdown time (LEAVE delivered one way only)
  gives the same outcome for one node while the other keeps 1 from an earlier epoch.
- Operator-shaped voids (document, don't test as bugs): manual grastate edit (documented
  procedure `doc/source/howtos/crash-recovery.rst` region), `pc.bootstrap=true` on both sides,
  interrupted bootstrap script leaving `--wsrep-new-cluster` in the systemd manager
  environment (`scripts/mysqld_bootstrap.in`).
- Divergence consequence: two bootstrapped clusters get distinct `state_uuid` histories; a
  node from cluster B later pointed at cluster A takes full SST (UUID mismatch,
  `replicator_str.cpp:777-857` → last_applied=-1) — B's committed writes are silently
  discarded. Galera docs call dual-bootstrap divergence "impossible to re-merge".

## Failure scenario

1. 3-node cluster under acked-write load with client journaling.
2. Antithesis kills node 3 (or partitions it away), leaving {1,2} Primary.
3. Trigger shapes for dual `safe_to_bootstrap:1`:
   a. Stop nodes 1 and 2 gracefully at (near-)the same instant — LEAVE cross-delivery race.
   b. Asymmetric partition between 1 and 2, then graceful stop of both — one sees the other
      as `left`, gets a singleton primary view; the other may already hold 1 from an earlier
      singleton epoch (e.g., it was the bootstrap node and no 2-member primary view was ever
      processed after the last restart).
4. Restart procedure (harness): start each node with bootstrap-if-flag-set (mirroring
   documented ops automation, e.g. the k8s operator's recovery). If two nodes carry 1, both
   bootstrap → two Primary Components of size 1 with different UUIDs → both accept writes.
5. Violation observed: `wsrep_cluster_state_uuid` differs across nodes accepting writes, or
   post-merge, journaled acked writes are missing (SST discarded one history).

## How to check (workload-side)

- After every full-cluster stop, before restart: read all grastate.dat files; count
  `safe_to_bootstrap: 1`. `Always`: count ≤ 1 following any *graceful* full shutdown.
  (After unclean shutdown all nodes legitimately have seqno -1 and whatever stale flag —
  restrict the check to shutdowns where every node exited cleanly.)
- Runtime invariant: all nodes reporting Primary share one `wsrep_cluster_state_uuid`
  (subsumed by at-most-one-primary-component but checkable cheaply here since the UUIDs
  genuinely differ after dual bootstrap — no disjointness analysis needed).
- Acked-write durability across the full-stop/restart cycle (unique-key journal replay).

## Assertion type

- `Always`: "at most one safe_to_bootstrap:1 after graceful full shutdown" — safety, evaluated
  at each full-stop checkpoint by the workload/first-start hook.
- Companion `Sometimes`: "a graceful full shutdown followed by flag-driven restart completed"
  — guards vacuity.

## Instrumentation suggestions (missing)

- SUT-side `Sometimes` at `replicator_smm.cpp:3245` when the flag flips to true — singleton
  primary views under churn are exactly the states to steer toward.
- SUT-side `Always` at connect (`replicator_smm.cpp:412`): bootstrap accepted ⇒ flag was 1
  (already enforced; assertion documents/exports the decision to the report).
- Workload hook (not SUT): a "grastate audit" container step at boot.

## Fault requirements

- **Requires node stop/restart orchestration** — flag this: node termination as an injected
  fault is often disabled, but this property works with workload-driven graceful
  `docker stop`/start cycles (SIGTERM grace must exceed pxc_maint_transition_period=10s +
  shutdown time, else Docker's 10s default SIGKILLs and the shutdown isn't graceful — see
  sut-analysis §12). Network faults sharpen the LEAVE-race but aren't strictly required.

## Open questions

- Does the harness restart policy mirror any real automation (percona operator's
  "most advanced node" recovery)? The property's severity claim assumes flag-driven bootstrap
  automation exists in the field; confirm with customer. `(needs human input)`

Resolved (see Investigation Log): the concurrent-LEAVE race is NOT winnable — the EVS state
machine precludes both nodes minting singleton prim views; the live trigger recipes are now
(a) stale flag=1 surviving an unclean kill during the write-deferral window and (b) the
torn-file / missing-line shape where the constructor default `true` wins. The assertion is
unchanged; the Antithesis Angle trigger list should be read with this correction.

### Investigation Log

#### Is the concurrent-LEAVE race actually winnable?

- Examined: `gcomm/src/evs_proto.cpp:4681-4733` (`handle_leave`), `:4736-4761`
  (`handle_install` in S_LEAVING), `:660-670` (S_LEAVING timer resend), `:1933-1975`
  (`send_leave`); `gcomm/src/pc_proto.cpp:555-575` (quorum with `left` credit).
- Found: to mint `safe_to_bootstrap: 1`, a node must deliver *itself* a singleton PRIMARY
  view crediting the peer as `left`. That requires processing the peer's LEAVE while still
  S_OPERATIONAL — only then does `handle_leave` shift to S_GATHER (`:4720-4726`) and drive a
  new install. A node already in S_LEAVING records the peer's LEAVE but never shifts to
  GATHER, and `handle_install` in S_LEAVING early-returns without installing any view
  (`:4747-4761`) — it can only shift to CLOSED. Mutual pre-close delivery is temporally
  contradictory: each node would have to receive the other's LEAVE (sent at close initiation)
  before initiating its own close — t_A > t_B + δ and t_B > t_A + δ′ cannot both hold.
- Conclusion: resolved — at most one node of a gracefully-stopping pair gets the singleton
  prim view; concurrent graceful shutdown cannot produce dual flag=1 by itself. `pc.linger`
  does not change the state-machine argument. Remaining mint vectors: stale flag (below),
  torn file (below), operator/script paths (documented void).

#### Is the flag re-persisted to 0 promptly when a 2+-member primary view forms?

- Examined: `galera/src/replicator_smm.cpp:3245` (flag update), `:3040-3070` (end of prim
  conf-change processing), `:315` (close-path write); `galera/src/saved_state.cpp:209-239`
  (`set` with unsafe-counter early-out), `:258-296` (`mark_unsafe`/`mark_safe`).
- Found: yes — `st_.set(state_uuid_, WSREP_SEQNO_UNDEFINED, safe_to_bootstrap_)` runs at the
  end of every primary conf change (`replicator_smm.cpp:3070`), and the PXC value-change check
  forces a write when the flag flips. Caveat: `SavedState::set` skips the *file* write while
  the unsafe counter (in-flight local commits) is nonzero (`saved_state.cpp:224-227`); the
  deferred write lands at the next unsafe→0 transition (`mark_safe`, `:285-296`). Under
  continuous overlapping write load the on-disk flag can lag the in-memory value.
- Conclusion: resolved — prompt when idle, deferred under load. The lag matters only for
  unclean kills (graceful close writes the correct flag at `replicator_smm.cpp:315`), and the
  property's ≤1-flag check is already restricted to graceful full shutdowns. For automation
  robustness, note: a stale flag=1 after an unclean kill co-occurs with grastate
  UUID=UNDEFINED/seqno=-1 (mark_unsafe wrote it), so flag-driven automation that also checks
  seqno != -1 is immune to this shape.

#### Torn grastate parse — where does a garbage/missing flag value land?

- Examined: `galera/src/saved_state.cpp:21-177` (constructor + parser), `:363-414`
  (`write_file` in-place rewrite with space padding, fflush+fsync).
- Found: the constructor defaults are `safe_to_bootstrap_(true)` (`:28`) and
  `saved_safe_to_bootstrap_(true)` (`:47`). The parser only overwrites the default when it
  sees a line whose first token is exactly `safe_to_bootstrap:` (`:124-127`). Three shapes:
  (1) line present with garbage value — C++11 `num_get` stores `false` on parse failure →
  safe; (2) line missing or space-padded away — reachable because `write_file` rewrites in
  place from offset 0 and pads the tail with spaces (`:383-384`); a crash between fwrite and
  fsync can leave the tail line blanked → default **true** survives; (3) file unreadable/absent
  — "Bootstraping with default state" (`:57-60`) → flag true (by design for fresh nodes, but
  it also applies to a corrupted existing datadir whose grastate is unreadable).
- Conclusion: resolved — spurious flag=1 via torn write is reachable in the missing-line and
  unreadable-file shapes (not the garbage-value shape). Both co-occur with a damaged/absent
  uuid/seqno, so the workload's grastate audit should record uuid+seqno alongside the flag,
  and the dual-bootstrap `Always` should treat "flag=1 with unparseable uuid/seqno" as its own
  violation flavor (automation trap) rather than folding it into the count.

## Synthesis refinement (2026-09-10)

Restated for v1: the workload cannot read grastate — the flag check becomes a per-boot supervisor JSONL emission consumed against a workload epoch ledger of observed bootstraps/state UUIDs. The v1 marker-file supervisor removes the shipped-env vector (deferred to the field-faithful supervisor variant with cluster-identity-single-lineage); v1 weight sits on the safe_to_bootstrap/torn-file shapes (+kill).
