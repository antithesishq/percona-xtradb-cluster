# ist-overlap-writesets-not-reapplied — writesets already applied via IST are never applied to the database again

**Focus area:** Idempotency and Replay. **Commit:** f9ecb3ebe8ff (branch 8.4).
**Confidence:** High — the skip mechanism is confirmed in code, and the drain-ordering
"should be safe" comment is now verified to be backed by a real interlock (fifo-get
cancellation at CC pop; see Investigation Log). Realized bugs (MDEV-36621, PXC-4845)
remain as evidence that the surrounding machinery has failed both ways in the field.

## Claim under test

A rejoining node can receive the same writeset twice: once through IST (donor streams
`[first_needed, group_seqno]`) and again through the live group channel (writesets delivered
while the join was in progress). The dedup contract:

1. Any writeset whose `global_seqno <= apply_monitor_.last_left()` is treated as already applied:
   it is added to the certification index **only** (never applied to the database) via
   `handle_trx_overlapping_ist`.
2. Conversely, no writeset that was *not* applied by IST is ever skipped — a wrongly-skipped
   writeset is a silently lost transaction on this node only.
3. After the joiner reaches SYNCED, its data is bit-identical to the donor/group.

This also covers the crash-restart replay case: after kill+recovery the node's position comes
from the InnoDB wsrep XID (updated in the same mtr as the commit, so apply+checkpoint are
atomic); IST re-delivers from checkpoint+1, and any overlap with the live queue must dedup by
the same mechanism.

## Code paths

- Gate: `percona-xtradb-cluster-galera/galera/src/replicator_smm.cpp:2248-2254` (`process_trx`):
  "SST thread drains monitors after IST, so this **should be** safe way to check if the ts was
  contained in IST" → `if (ts.global_seqno() <= apply_monitor_.last_left())
  handle_trx_overlapping_ist(ts_ptr); return;`.
- Skip handler: `replicator_smm.cpp:2195-2222` (`handle_trx_overlapping_ist`) — asserts
  `global_seqno <= last_left` (debug only, NDEBUG in release), enters the local monitor, and if
  `global_seqno > cert_.position()` appends to the cert index and marks committed
  (`cert_.append_trx` + `set_trx_committed`), never calling `apply_trx`. GCache buffer
  aliasing handled by `get_real_ts_with_gcache_buffer` (:2150-2193) so IST-preload and GCS
  copies of the same seqno share a buffer.
- Analogous logic for conf changes: `replicator_smm.cpp:2813` (`skip_prim_conf_change` cross-ref
  comment).
- Cert-index preload during IST: `replicator_str.cpp:673-723` and `ist.cpp` preload paths —
  writesets below the joiner's applied position update the certification index only; DB effects
  come solely from writesets above it.
- Position recovery inputs: InnoDB wsrep XID (`trx0sys.cc:571` read; monotonicity assert
  commented out at :481-482), grastate.dat (`saved_state.cpp`), and the PXC forward-adjust of
  `sst_seqno_` (`replicator_str.cpp:1242-1277`).
- Realized bugs in this machinery: **PXC-4845** — grastate ahead of SE checkpoint → joiner
  detects IST gap, *skips IST but keeps running with monitors uninitialized (last_left == -1)*
  → every live writeset queued forever → cluster-wide FC stall (fix only converts hang to
  shutdown). **MDEV-36621** (upstream) — gcache `seqno_release` freed locked buffers → "IST
  didn't contain all write sets" → lost writesets on the joiner.

## Failure scenarios

- **Double apply:** `last_left` lags actual IST-applied state at the moment the first live
  writeset is processed (monitor drain race) → `apply_trx` re-executes an applied writeset →
  duplicate-key error on INSERT (→ inconsistency vote → node evicted) or, worse, silently doubled
  effects for idempotent-looking statements (UPDATE t SET x=x+1 → wrong value, **no vote**, silent
  divergence).
- **Wrong skip:** `last_left` (or recovered position) ahead of actually-applied state — e.g. the
  PXC-4845 grastate-vs-SE-checkpoint divergence, or a stale non-(-1) grastate seqno trusted at
  startup (`replicator_smm.cpp:266-286`) — → live writesets classified as IST-overlap and only
  cert-indexed → transactions missing from this node only → silent divergence.
- **Cert-index asymmetry:** overlap trx appended to the index with a different outcome than on
  established nodes (the append result is explicitly discarded, `(void)cert_.append_trx` :2218)
  → later certification verdicts differ on the joiner → divergence long after the join.

## Suggested assertions (all missing)

- **Primary (workload, Always):** "post-rejoin convergence" — after any node transitions
  JOINER→SYNCED, a cross-node table checksum (e.g. `CHECKSUM TABLE` / ordered row digest over
  workload tables, run under `wsrep_sync_wait` or after quiescing) matches the group. `Always`:
  every join must converge. This is the external oracle the SUT lacks (no state hash; voting only
  sees apply *errors*).
- **Workload invariant (Always):** counters-style table (`UPDATE ... SET v = v + 1` by key)
  cross-checked against workload-side expected values — specifically catches the
  silent double-apply/skip cases that duplicate-key-error-based detection misses.
- **Coverage (SUT-side, missing, Sometimes):** at `replicator_smm.cpp:2252`
  (`handle_trx_overlapping_ist` invoked) — `Sometimes(live writeset overlapped IST and was
  skipped)`. This path only runs in a narrow join window; explicit coverage tells us the dedup
  logic was actually exercised rather than vacuously passing.
- **SUT-side (missing, Always):** inside `handle_trx_overlapping_ist`, assert
  `real_ts->global_seqno() <= apply_monitor_.last_left()` still holds after the gcache buffer
  swap and local-monitor entry (the current `assert` at :2200 vanishes under NDEBUG).

## Fault / workload requirements

- Needs joins under live load: network partition (default-on) that isolates one node long enough
  to fall behind but within gcache range → heal → IST rejoin while writes continue. Small
  `gcache.size` sharpens the SST/IST boundary.
- The crash-restart replay variant needs **node termination** (often disabled) — flag: without
  kill faults, only partition-induced IST is testable; grastate/XID recovery paths stay cold.
- Donor selection races (stale gcache low-water snapshot, `gcs_group.cpp:1789-1845`; PXC zeroes
  the IST safety gap :1817) are exercised by loading the donor during the join.

## Open questions

None. (All three original questions resolved by code investigation — see Investigation Log.
The workload check suffices; the SUT-side `Sometimes` at :2252 remains valuable for coverage,
not for closing a race.)

### Investigation Log

#### Is the "SST thread drains monitors after IST" ordering enforced against concurrent applier dispatch?

- Examined: `gcs/src/gcs.cpp:2430-2540` (gcs_recv, gcs_resume_recv),
  `galera/src/replicator_smm.cpp:470-535` (async_recv), `:2646-2692` (process_conf_change),
  `:996` in replicator_smm.hpp (resume_recv), `replicator_str.cpp:1495-1516` (post-IST drain).
- Found: when a `GCS_ACT_CCHANGE` is popped, `gu_fifo_cancel_gets(conn->recv_q)` blocks all
  other applier threads from popping GCS actions (gcs.cpp:2457-2464). Appliers seeing
  `-ECANCELED` only help apply IST events (`recv_IST(recv_ctx)`, replicator_smm.cpp:488-495).
  `resume_recv()` (→ `gu_fifo_resume_gets`) is called only after `process_prim_conf_change`
  — and therefore after `request_state_transfer` including `apply_monitor_.drain(sst_seqno_)`
  (replicator_str.cpp:1502-1510) — returns (replicator_smm.cpp:2681). The whole ST also runs
  inside the CC's local-monitor slot (:2665-2682).
- Not found: any path that resumes fifo gets before the drain (only two resume_recv call
  sites: :2658 SST_CANCELED early-out and :2681).
- Conclusion: RESOLVED — no live writeset can reach `process_trx` before the post-IST drain
  completes; the `last_left` gate never races the drain. After resume, `last_left` is
  monotonic ≥ IST end, so overlap trxs deterministically take the skip path. Question
  dropped; confidence upgraded.

#### Does the cert-index-only append use the same certification outcome established nodes computed (dummy vs real)?

- Examined: `galera/src/write_set_ng.cpp:124-142` (Header::set_seqno),
  `write_set_ng.hpp:118` (F_CERTIFIED), `trx_handle.hpp:470-580` (unserialize, mark_certified,
  mark_dummy/mark_dummy_with_action), `certification.cpp:180-345` (check_against/
  certify_and_depend_v3to6/certify_v3to6/do_ref_keys), `:415-510` (do_test), `:1161-1176`
  (test), `:1228-1330` (append_trx, append_dummy_preload), `replicator_smm.cpp:2144-2222`
  (get_real_ts_with_gcache_buffer, handle_trx_overlapping_ist).
- Found: the certification outcome is *stamped into the writeset buffer itself* by the first
  certification pass: `Header::set_seqno` writes `F_CERTIFIED` + `pa_range` (in-code comment:
  "certification outcome") + seqno and re-checksums (write_set_ng.cpp:124-142). Unserializing
  a stamped buffer yields `certified_=true` and depends = seqno − pa_range
  (trx_handle.hpp:503-514). For `certified()==true` trxs, `check_against` can never report
  CONFLICT — the conflict condition requires `trx->certified() == false`, comment: "Already
  certified trxs show up here during index rebuild" (certification.cpp:216-224) — so the
  joiner's append deterministically returns TEST_OK and populates the index with the same
  keys/depends the established nodes computed. Group-wide dummies travel as skip-marked
  gcache entries → delivered meta-only → joiner appends a keyless placeholder
  (`append_dummy_preload` certification.cpp:1311-1329; overlap path `mark_dummy_with_action`
  on a size-0 buffer, replicator_smm.cpp:2168-2172), matching established nodes' cert-fail
  path which leaves the index unpopulated (do_test_v3to6 asserts index size unchanged on
  failure).
- Not found (residual nuance): the `gu::NotFound` branch of `get_real_ts_with_gcache_buffer`
  (:2185-2192) returns the raw GCS buffer (unstamped, `certified_=false`) — that branch
  re-runs full certification and its equivalence rests on the deterministic-replay argument
  (identical preloaded index state), not the stamp.
- Conclusion: RESOLVED — the discarded `append_trx` result is benign because the outcome was
  pre-determined by the stamped header; dummy/real classification propagates via the gcache
  skip flag. Question dropped.

#### Is `report_last_committed` after overlap skip equivalent to the full apply path's commit-cut bookkeeping?

- Examined: `certification.cpp:1339-1400` (set_trx_committed deps_set handling),
  `:1385-1391` (append_trx deps_set insert skip), `certification.hpp:75-97`
  (purge_trxs_upto + disabled assert), `replicator_smm.cpp:2155-2161` (real_ts constructed
  with local seqno `WSREP_SEQNO_UNDEFINED`), `:2195-2221` (overlap handler), `:650-670`
  (full apply path).
- Found: deps-set tracking is skipped symmetrically for `local_seqno == WSREP_SEQNO_UNDEFINED`
  (IST-origin) trxs on both insert (append_trx :1385-1391) and erase (set_trx_committed
  :1339-1355); the overlap `real_ts` is built with UNDEFINED local seqno (:2160-2161). In the
  NotFound branch (original ts, real local seqno) the insert is immediately followed by the
  erase within the same handler — balanced either way. The "safe to discard seqno may
  decrease" issue behind the disabled assert (certification.hpp:88-92) cannot over-purge:
  `purge_trxs_upto` clamps to `std::min(seqno, stds)` (:83-92) — the assert concerns the
  caller-supplied argument, not purge safety.
- Not found: any deps_set/commit-cut leak path through the overlap handler.
- Conclusion: RESOLVED — bookkeeping is symmetric and purge is clamped; not a divergence
  channel. Question dropped.

## Synthesis refinement (2026-09-10)

GU_DBUG_SYNC note: provider sync points are a candidate mechanism for freezing the joiner/donor around the overlap window (global_seqno <= last_left gate); availability in the PXC build unverified — one runtime SET answers it.
