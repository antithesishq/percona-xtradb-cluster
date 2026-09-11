# evicted-node-rejoins-only-via-sst — Evidence

**Focus area:** Evaluation gap-fill — the PXC-5208 post-eviction rejoin contract stated as
an invariant (evaluation/synthesis.md Gap 8). This closes the loop that
`inconsistency-vote-evicts-divergent-minority` and
`privilege-context-divergence-never-evicts` both reference as "the rejoin risk": what a
divergence-evicted node is allowed to do when it comes back.
**Confidence:** High on the mark_corrupt/grastate mechanics (all call sites read at
f9ecb3e / galera 350f76f3 — HEAD is literally the PXC-5208 merge, f9ecb3ebe8f); medium on
the pre-fix IST-into-divergence trigger path (the fix's own narrative and the code's
position-arbitration gate are in tension — see Open Questions).

## Claim under test

After a node is expelled for data inconsistency (inconsistency vote, self-declared
inconsistency, or IST apply failure — every path that calls `SavedState::mark_corrupt`),
its next return to Synced must go through a **full SST**. It must never IST back in
carrying its divergent state, and it must never resume from its (divergent) InnoDB
position. This is exactly the contract PXC-5208 (server commit `374bb9e907e` + galera
`350f76f3`, merged 2026-09-07 as the current HEAD) hardens, and which its four MTR tests
check fault-free:

- `pxc_5208_force_sst_after_inconsistency{,_default}.test` — vote eviction, opt-in vs
  default;
- `pxc_5208_force_sst_after_ist_failure.test`,
  `pxc_5208_force_sst_after_ist_receiver_failure.test` — the IST-failure arms.

The opt-in test header states the historical failure this closes: *"Historically the node
... marks its state as corrupt. That alone does not force a full SST on the next restart;
the recovered position comes out of InnoDB, the cluster then offers IST, and the
inconsistent dataset survives."* `repl.force_sst_after_inconsistency` ships **default
"no"** (`percona-xtradb-cluster-galera/galera/src/replicator_smm_params.cpp:50`), so the
shipped behavior is the pre-fix behavior — the property tests the contract the fix says
matters, on the configuration the field actually runs, plus the opt-in variant.

## Code (validated; paths under percona-xtradb-cluster-galera/ unless noted)

### The eviction funnel — every entry calls mark_corrupt

1. Vote loss / no-vote self-eviction → `on_inconsistency()` →
   `mark_corrupt_and_close()` — `galera/src/replicator_smm.hpp:388-412`
   (`st_.mark_corrupt(force_sst_after_inconsistency_)` at :400).
2. `async_recv` on `GcsActionSource::INCONSISTENCY_CODE` —
   `galera/src/replicator_smm.cpp:501-506`. **Changed by the fix itself**: previously bare
   `st_.mark_corrupt()`, now `mark_corrupt_and_close()` (initiates the cluster leave) —
   fresh behavior change, prime regression surface.
3. Unexpected exception applying a remote action → "Node consistency compromized, leaving
   cluster..." → `mark_corrupt_and_close()` — `replicator_smm.cpp:2292-2301` (release
   build keeps draining the queue afterwards; debug asserts).
4. IST writeset apply failure → `st_.mark_corrupt(force_sst_after_inconsistency_)` then
   rethrow — `galera/src/replicator_str.cpp:1602-1612`.
5. `recv_IST` failure ("node restart required") → same mark_corrupt then `abort()` —
   `replicator_str.cpp:1710-1717`. Pre-fix PXC had NO mark_corrupt here at all — the node
   aborted with grastate intact.

### What mark_corrupt does (galera/src/saved_state.cpp)

- `mark_corrupt(bool remove_state_file)` (:300-325): zeroes grastate.dat **in place** to
  `UUID_UNDEFINED:-1` (preserving safe_to_bootstrap) and sets the in-memory `corrupt_`
  latch; with the option ON it additionally `unlink_state_file()` (:343-361 — unlink
  failure is warn-only, "Remove it manually").
- The `corrupt_` latch gates later position writes: `set()` no-ops once corrupt (:214), so
  the clean-shutdown position write (`shift_to_CLOSED` → `st_.set`,
  `replicator_smm.cpp:316-320`) and the pause() stamp (`replicator_smm.cpp:3385-3411`,
  also via `set()`) cannot resurrect a position.
- **Bypass**: `restore_saved_state()` (:242-250) writes the boot-time captured position
  UNCONDITIONALLY — no `corrupt_` check. Its call sites are SST-failure paths
  (`replicator_str.cpp:169` EAGAIN "retry with IST instead of full SST after restart";
  `:1288` graceful-shutdown-during-SST). If any of those can execute after a
  mark_corrupt (evicted node's provider still draining, or an eviction landing during a
  concurrent state-transfer edge), the divergent-era position is re-persisted and the
  corrupt latch is defeated. Statically these paths look disjoint from eviction
  (they belong to a JOINER, evictions to a Synced applier), but fault interleavings are
  exactly what Antithesis explores.
- `write_file` (:364-410) is **warn-only on every failure** (fwrite/fflush/fsync each log
  and return): under disk faults the zeroing can silently not land while the in-memory
  latch claims it did.

### Why a NOT-zeroed grastate lets the divergent node IST back in

- An idle healthy node's on-disk grastate is `<real-cluster-uuid>:-1` (mark_safe writes
  uuid_ with seqno -1 once the unsafe count drains, :263-297; post-transfer blanking
  `replicator_str.cpp:1436` sets exactly this shape).
- At next start, the position arbitration (`replicator_smm.cpp:266-289`) adopts the
  server-provided recovered position (`--wsrep_start_position` = divergent InnoDB SE
  checkpoint) **precisely when grastate uuid matches and grastate seqno == -1** — the
  normal crash-recovery flow. The joiner then advertises the divergent position; the donor
  sees a matching uuid and a seqno inside its gcache and serves IST; the divergent rows
  survive under a Synced state. Nothing in the SUT ever compares data (voting only sees
  apply errors).
- So the invariant's kill window is any execution where a mark_corrupt-worthy event is NOT
  followed by a durable zeroing before the next boot: kill -9 between the vote outcome and
  mark_corrupt's fsync; write_file failure under disk faults; the restore_saved_state
  bypass; or an eviction path that misses the funnel entirely.

### Rejoin decision mechanics (for the detector)

With grastate zeroed or absent, the arbitration adopts nothing → the node joins with an
undefined position → full SST (this is what the MTR tests observe: grastate removed →
"can only rejoin via SST"). IST after eviction therefore requires a surviving/resurrected
real position — which is exactly the violation.

## Failure scenario

1. Fenced sabotage phase (same poison-budget scoping as
   `inconsistency-vote-evicts-divergent-minority`, whose workload this property extends):
   inject deterministic single-node divergence (wsrep_on=OFF local DELETE, then replicate
   the conflicting DML — the PXC-5208 tests' own recipe) → the node is vote-evicted;
   error log shows "Inconsistency detected: Inconsistent by consensus on ...".
2. The supervisor restarts the evicted node (deliberate divergence from field systemd
   semantics — recorded per catalog assumptions). Faults active around eviction/shutdown/
   restart: kill -9 inside the eviction-to-shutdown window, disk write faults during
   grastate zeroing, partitions during the rejoin.
3. Expected: the node reaches Synced only after a full SST (donor logs an SST request; the
   joiner never logs an IST-completed marker between eviction and the next Synced), and
   post-rejoin its checksums match the cluster (feed the shared oracle; also checksum the
   evicted node BEFORE rejoin per the un-injected-vote rule — ROW full-image apply can
   self-heal the very rows that prove the violation).
4. Violations: joiner completes IST after an eviction with no intervening SST; node
   restarts straight to Synced claiming its old position (no state transfer at all);
   force_sst=ON variant leaves grastate.dat present after eviction shutdown; divergent
   rows survive on a Synced node post-rejoin.

## How to check (workload/supervisor-side)

- `Always`: for every node with an eviction marker in its error log since its last SST,
  the next transition to Synced is preceded by a full SST on that node (log-scan:
  "Inconsistency detected"/"Removed state file" vs SST-script/IST markers; the shared v1
  log layer). Equivalently: "Receiving IST" / IST-complete never appears on a node between
  an eviction marker and the next completed SST.
- `Always` (opt-in variant only): after eviction shutdown, grastate.dat is absent
  (supervisor file probe — mirrors the MTR `--file_exists` checks).
- `Always` (terminal): post-rejoin cross-node checksums match (shared oracle, distinct
  message).
- `Sometimes` (vacuity guards): an evicted node completed a full-SST rejoin; a kill landed
  between the vote outcome and process exit (the mark_corrupt durability window — needs
  the kill channel).

## Assertion type

- `Always` for the never-IST-after-eviction contract — safety; every rejoin of an evicted
  node is an evaluation and every one must satisfy it. (Not `AlwaysOrUnreachable`: the
  workload deliberately drives evictions, so "never evaluated" would itself be a workload
  bug — the `Sometimes` guard makes that visible.)
- `Sometimes` for the two guards above — the second is the timing state that makes the
  `Always` earn its keep.

## Instrumentation notes (missing)

- SUT-side `Sometimes` in `SavedState::mark_corrupt` (saved_state.cpp:301) with the
  remove_state_file flag as detail — direct coverage signal for the funnel, distinguishes
  the five entry paths only via log context today.
- SUT-side `Unreachable`: `restore_saved_state()` invoked while `corrupt_` is set
  (saved_state.cpp:242) — the latch-bypass precondition; cheap and sharp.
- SUT-side `Always` at the position arbitration (replicator_smm.cpp:266): adopted seqno
  ≥ 0 implies grastate was not corrupt-zeroed this boot. v2 patchset material.

## Fault / config / phase flags

- Vote-eviction arm: no faults strictly needed (workload sabotage), but **fenced
  variant/phase required** (sabotage violates the ambient checksum `Always` — same scoping
  as inconsistency-vote). On the v1 assert image, injected divergence may assert-crash
  before a vote round — the reliable form of this arm is **release-image**, mirroring
  `inconsistency-vote-evicts-divergent-minority`'s tag.
- IST-failure arms and the mark_corrupt durability window: **+kill** (workload→supervisor
  crash channel), disk faults widen.
- Config variant: `repl.force_sst_after_inconsistency=yes` (runtime-settable via
  `wsrep_provider_options`, replicator_smm_params.cpp:211-214) — run both; the default-no
  arm is the field contract, the yes arm is the fix's regression test.
- Supervisor must restart evicted/aborted nodes (catalog assumption; shipped systemd would
  not).
- Phase: **release +variant(sabotage-fenced)** for the vote arm; IST-failure/kill legs
  v1-assert +kill.

## Open questions

- Reconcile the fix narrative with the position-arbitration gate: the PXC-5208 test
  header says pre-fix (and shipped-default) behavior is "the recovered position comes out
  of InnoDB, the cluster then offers IST" — but `mark_corrupt` has always zeroed grastate
  to `UUID_UNDEFINED:-1`, and the arbitration (replicator_smm.cpp:266-276) refuses the
  InnoDB position unless grastate uuid MATCHES. Either (a) the narrative describes the
  windows where zeroing doesn't land (kill/write-failure/bypass — in which case the
  shipped default-no is nearly as safe as yes and the property's reds concentrate in the
  fault windows), or (b) a position channel bypasses the ctor gate (e.g. the
  wsrep-lib `initialized()`/connect-time position), in which case IST-into-divergence is
  reachable even with clean zeroing and the property should fail fast. `(partial: ctor
  gate, corrupt_ latch, and all five funnel entries read directly; the
  server→wsrep-lib→provider position flow at connect time not fully traced — the first
  triage of a red/green here settles it, and either answer is a finding: (a) narrows the
  fix's value to unlink-vs-zero robustness, (b) is a live defect channel)`
- Does anything OUTSIDE the funnel decide "this node is inconsistent" without
  mark_corrupt (e.g. wsrep-lib-level SST/position sanity throws,
  `server_state.cpp:833-870`)? If yes, those exits keep a real grastate and the invariant
  must cover them too; the detector (eviction marker → next-Synced-requires-SST) already
  keys on log markers, so extending is a marker-list edit, not a redesign.

### Investigation Log

#### How does an evicted node's divergent position survive to be offered IST (default config)?

- Examined: `saved_state.cpp` full file (mark_corrupt :300-325, set gate :214,
  restore_saved_state :242-250, mark_safe :263-297, write_file :364-410, unlink :343-361);
  `replicator_smm.hpp:388-412`; `replicator_smm.cpp:266-289` (ctor arbitration), :501-506,
  :2292-2301, :316-320 (shift_to_CLOSED), :3385-3417/:3514-3527 (pause/resume);
  `replicator_str.cpp:169/:1288` (restore_saved_state callers), :1602-1612, :1710-1717;
  the four pxc_5208 MTR tests; server commit 374bb9e907e and galera commit 350f76f3
  (full diff).
- Found: every eviction path zeroes grastate before exit in the current tree; the
  corrupt_ latch blocks set()-based resurrection; restore_saved_state bypasses the latch
  but its callers are joiner-side SST paths; write_file failures are warn-only; the fix's
  own new code is the recv_IST mark_corrupt and the async_recv close-behavior change.
- Not found: a clean-execution path (no fault, no bypass) where the default-no
  configuration leaves an adoptable position — which is what the fix narrative implies
  existed. Recorded as the first open question rather than assumed either way.
- Conclusion: invariant stated at the contract level (never IST after eviction), which is
  correct under both readings; failure-scenario weight placed on the fault windows the
  code exhibits (kill-before-fsync, warn-only writes, latch bypass).
