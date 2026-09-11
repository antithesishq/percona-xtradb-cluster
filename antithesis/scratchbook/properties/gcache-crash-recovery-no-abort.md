---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# gcache-crash-recovery-no-abort — Torn gcache after unclean kill never aborts recovery

**Slug:** `gcache-crash-recovery-no-abort`
**Type:** Safety
**Confidence:** High on mechanism (code read directly); Medium on residual reachability
(the PXC-5209 fix is in-tree at f9ecb3e — this is a regression/edge-completeness property)
**Assertion type:** `Always` — evaluated at every restart after an unclean kill: the node
must get through gcache recovery (successfully or via clean cache reset) without process
abort, OOM, or bad-free. Workload-side; SUT-side `Unreachable` candidates listed below as
missing instrumentation.

## Property

A node restarted after an ungraceful kill always completes galera.cache recovery: it either
recovers a valid, gapless suffix of cached writesets or falls back to a clean cache reset
(and hence SST) — it never crashes, corrupts memory, or balloons memory during recovery, and
it never subsequently serves IST from a corrupt cache.

## Why the crash case is exactly where checks are weakest (code evidence)

- The preamble carries `seqno_min/max/offset` **only on graceful close**: `write_preamble
  (synced=true)` is called only from `close_preamble()` in the destructor
  (`gcache/src/gcache_rb_store.cpp:745-762`, `:1053-1056`). After an unclean kill,
  `offset == -1` → recovery takes the linear-probe `scan()` path (`:1332-1361`) and the
  preamble-seqno sanity check (`:1104-1116`) is **skipped**.
- The remaining plausibility guard `do_sanity_checks()` (`:1078-1116`, added by the
  PXC-5209 fix) bounds `bh->size` by `size_cache_` and `seqno_g` by
  `preamble_seqno_{min,max} ± max_seqnos` where the epsilon is ≈ 5.6M seqnos for a 128MB
  cache — a wide window for garbage that still passes.
- `scan()` **warns-only** on a truncated last segment (`:1313-1316`); a mapping exception →
  clear map + full silent reset (`:1279-1297`, `:1441-1457`); seqno collision → discard
  BOTH buffers (`:1195-1265`).
- **The page store is never recovered at all**: `gcache_page_store.cpp` restarts with
  `count_ = 0` (`:264`), no directory scan — writesets spilled to `gcache.page.NNNNNN`
  vanish from `seqno2ptr` and orphan page files accumulate (`O_TRUNC` commented out,
  `galerautils/src/gu_fdesc.cpp:37`).
- RB payload is mmap'd and never msync'd during operation (only preamble writes + dtor;
  `MMapFactory` create(sync=false), `gcache_rb_store.cpp:121` area) — after kill -9 the
  on-disk cache content is whatever the kernel happened to flush: torn buffers are the
  *expected* input to recovery, not an exotic one.

## Realized bug this generalizes (validated)

**PXC-5209** (fix 383df32d591 + galera 96e28073, 2026-05-10): `gcache.recover` trusted the
on-disk BufferHeader blindly; a corrupt cache produced a bogus pointer free or a ~10^12
seqno inserted into `seqno2ptr_` → OOM. Percona's own regression tests
(`pxc_gcache_corrupt_{size,seqno}.test`) byte-patch the cache file with Perl — i.e., the
vendor validates this property with hand-made corruption; Antithesis produces the *real*
torn-cache distribution by killing under load. The fix added heuristic checks
(`do_sanity_checks`), not a checksum — corruption patterns that stay inside the epsilon
window still enter `seqno2ptr_`.

Donor-side corollary (upstream **MDEV-36621** — a *lead*, not a verified in-tree defect:
no corresponding fix commit exists in this tree and the freed-buffer mechanism was not
traced here): recovered/purged cache serving IST handing out freed buffers → "IST didn't
contain all write sets". Independently of that ticket, the donor-side conclusion stands on
this file's own trace of `ist_proto.hpp`/`write_set_ng` at f9ecb3e: a recovered-but-wrong
cache is a *silent divergence* vector for the next joiner, not just a local crash.

## Failure scenario (Antithesis recipe)

1. Sustained write load (large-ish transactions so the page store spills — set
   `gcache.size` small, e.g. 16-64MB, to force rollover and page files).
2. kill -9 nodes at random points; restart them.
3. The restarted node runs gcache recovery on a torn mmap; property checks fire on the
   restart outcome.
4. Variant: after the restarted node recovers, force a *different* node to IST from it
   (kill/restart the third node) — exercises the recovered-cache-as-donor path.

## Invariant (concrete checks)

Workload-side, per restart-after-unclean-kill:

- `assert_always(node reaches provider-initialized state (log "Recovering GCache..." phase
  completed, mysqld alive, port eventually open or SST requested) within bound,
  "gcache recovery after unclean kill never aborts the node")`.
- Companion boundedness check (catches the PXC-5209 OOM shape and the orphan-page leak):
  `assert_always(count(gcache.page.*) and RSS during recovery below threshold,
  "gcache recovery memory/page-file growth bounded")`.
- Donor-side companion (stronger, needs cross-node data): after an IST whose donor had
  previously crash-recovered, the joiner's applied range is gapless (joiner reaches Synced
  and cross-node checksum matches — delegate the checksum to the cluster-consistency
  property; here assert the joiner does not hit "Receiving IST failed").

## Instrumentation suggestions (all missing)

- **SUT-side `Unreachable`** at the corrupt-buffer fatal paths:
  `gcache_mem_store.cpp:42-44` (corrupt buffer header), `GCache_seqno.cpp:80-86` (seqno
  reuse), and the `do_sanity_checks` failure branch (`gcache_rb_store.cpp:1092-1116`) —
  the last one is *reachable by design* (it is the detection working), so instrument it as
  `Sometimes` ("gcache corruption detected and handled") instead, and keep `Unreachable`
  for the paths that mean detection failed.
- **SUT-side `Sometimes`**: "recovery took the scan() fallback path" (`:1332`) and
  "recovery performed full cache reset" (`:1441-1457`) — these are the interesting recovery
  subphases and are invisible to the workload; as exploration hints they push Antithesis
  toward torn-preamble states.
- **SUT-side `Always`** in `RingBuffer::recover`: after recovery, `seqno2ptr_` forms a
  contiguous range and every mapped buffer lies within the segment — the invariant the
  PXC-5209 fix approximates with epsilon checks.
- Note for the harness: `gu_abort()` suppresses core dumps (`setrlimit(0)` +
  `PR_SET_DUMPABLE 0`, `galerautils/src/gu_abort.c:29-58`) — patch/override in the test
  image or triage loses the stack.

## Fault requirements

- **REQUIRES node termination (kill -9 + restart)** — graceful shutdown writes a synced
  preamble and recovery takes the strong-checks path; the property is specifically about
  the unclean case. Flag to environment team.
- Disk faults (torn write / partial page flush) deepen coverage but kill -9 against a
  never-msync'd mmap already produces torn state naturally.
- Small `gcache.size` in at least one config variant to reach rollover + page store.

## Open Questions

None. (All three resolved 2026-09-10 — see Investigation Log. Net effect on the property:
(1) the donor-side companion check IS required — payload corruption fails loudly at the
joiner (writeset CRCs), but two metadata-only shapes pass the epsilon AND every downstream
check silently: a flipped `BUFFER_SKIPPED` flag (writeset shipped as IST `T_SKIP` → silent
omission) and a corrupt `bh->type` (silently degraded to `T_SKIP` via an NDEBUG'd assert's
`default:` arm) — only the cross-node checksum catches these; (2) the PXC-5209 OOM class
is BOUNDED but not eliminated — DeqMap null-padding of an accepted in-epsilon bogus seqno
costs up to ~45MB @128M cache / ~716MB @2G cache, so the RSS-bounded check keeps value
with a threshold sized to `8 * size_cache_/24` bytes of padding; (3) orphan `gcache.page.*`
accumulation is UNBOUNDED (no startup cleanup, monotonic in-run `count_`, names restart at
000000) — the page-file-count Always needs its threshold to count orphans across restarts,
and this is a legitimate slow-burn disk-leak finding in its own right; (4) freeze_purge is
provider-memory-only — restart resets it, recovery is NOT pathological in default configs;
the pathological variant requires the operator to have persisted the option at the MySQL
layer, which additionally bypasses validation and is inconsistently enforced.)

### Investigation Log

#### Which corruption shapes pass the do_sanity_checks epsilon and cause downstream harm?

Investigated 2026-09-10.

- Examined: `do_sanity_checks` (`gcache_rb_store.cpp:1076-1131`) + PXC-5209 fix commit
  `96e28073` (ancestor of HEAD, verified); `gcache_bh.hpp` in full (BufferHeader layout —
  24 bytes packed, `BH_test` :129-146); scan-side mutation :1188-1189; downstream:
  `replicator_str.cpp:673-723` (cert preload — presence-only via `seqno_lock`), `ist.cpp:
  640-712, 905-985` (donor ships bytes verbatim), `ist_proto.hpp:630-640, 760-785`
  (type/flags routing), `write_set_ng.cpp:165-245` + `write_set_ng.hpp:813-871` (joiner
  CRCs), `replicator_str.cpp:1587, 1706, 1726` (release-build verify_checksum sites),
  `trx_handle.hpp:445` (NDEBUG-only early check).
- Found: exactly two fields are sanity-checked (`bh->size` vs cache size — near-dead
  check; `seqno_g` within ±`size_cache_/24` ≈ 5.6M @128M of preamble/map bounds). NO
  payload checksum exists in gcache (BufferHeader has no CRC; the only hash use is
  comparing two COLLIDING buffers). `bh->flags` accepts any of {0,1,2,3} — a flipped
  BUFFER_SKIPPED becomes IST `T_SKIP` (`ist_proto.hpp:781-783`): silent omission.
  `bh->type` is never validated; `ordered_type()`'s `default:` arm silently degrades to
  `T_SKIP` (asserts NDEBUG-elided) or mis-routes as `GCS_ACT_CCHANGE`. An accepted bogus
  in-epsilon seqno makes DeqMap pad the gap with 8-byte null entries — bounded RAM cost
  (~45MB @128M, ~716MB @2G): the PXC-5209 OOM is bounded, not eliminated. PAYLOAD
  corruption, by contrast, is caught loudly: joiner verifies writeset header CRC + payload
  CRC (`write_set_ng`), re-verified at `replicator_str.cpp:1726` etc. → IST fails with an
  error, not divergence. Cert preload checks presence only, never integrity.
- Conclusion: RESOLVED — local abort/OOM detection does NOT suffice; the donor-side
  companion (cross-node checksum after IST-from-recovered-donor) is load-bearing because
  the two metadata shapes (flags/type) are end-to-end silent. Instrumentation refinement:
  an SDK `Unreachable` (or Always) on `ist_proto.hpp`'s `default:` degrade arm and a
  `Sometimes` on "T_SKIP sent for a buffer recovered by scan()" would make the silent
  shapes directly observable.

#### Is orphan gcache.page.* accumulation bounded?

Investigated 2026-09-10.

- Examined: `gcache_page_store.cpp` in full (ctor :252-278 — `count_=0`, no dir scan;
  `remove_file` :50-80 reachable only from the runtime discard thread over pages THIS
  process created; dtor :288-313 drains only on clean shutdown; `make_page_name` :42-48 —
  names restart at `gcache.page.000000`; `cleanup()` budget :200-208 blind to orphans),
  `gcache_page.cpp:57-93` (reused index: ftruncate/prealloc, only first header cleared;
  `O_TRUNC` disabled at `gu_fdesc.cpp:38`), repo-wide grep for unlink/cleanup in gcache,
  galera, SST scripts, sql/.
- Found: nothing deletes leftover page files at startup; `count_` is monotonic within a
  run; successive crash points leave orphans at distinct high indices that are reclaimed
  only by coincidental index reuse. Only implicit bound: ENOSPC (throws). Each orphan is
  >= gcache.page_size (default 128M, larger for oversized writesets).
- Conclusion: RESOLVED — unbounded. The boundedness check must (a) treat page-file COUNT
  growth across kill/restart cycles as the signal (monotone growth across N cycles with
  cleanup never observed = violation of the bounded-leak expectation), or (b) be split
  into its own slow-burn `Always` ("gcache.page.* bytes in datadir < threshold(cycles)").
  Threshold cannot assume any GC exists, because none does.

#### Can a crash while freeze_purge is set leave recovery pathological?

Investigated 2026-09-10.

- Examined: `gcache_params.cpp:32-33, 63-66, 100-122, 230-256` (param registration —
  NOT read_only; runtime setter validates seqno exists and propagates to both Params and
  RingBuffer), `gcache_rb_store.cpp:134-135` (ctor hard-resets
  `freeze_purge_at_seqno_(SEQNO_ILL)`), `write_preamble` :734-795 (no freeze field),
  enforcement at `GCache_memops.cpp:58` and `gcache_rb_store.cpp:204`,
  `replicator_smm_params.cpp:144` (startup path parses option string straight into
  gu::Config, bypassing the setter), `sql/wsrep_var.cc:316-332` (refresh_provider_options
  reads full option string back → SET PERSIST captures it), grep for auto-unfreeze (none).
- Found: within the provider the freeze is memory-only; restart clears it; a crash while
  frozen leaves no gcache-level frozen state — only the consequence (a cache full of
  unpurged buffers + the overflow pages the freeze forced, which are then orphaned per the
  previous question). HOWEVER, if the option was persisted at the MySQL layer
  (my.cnf/mysqld-auto.cnf via SET PERSIST of wsrep_provider_options), the next start is
  frozen FROM BOOT via the Params copy, with the `seqno2ptr.find(seqno)` existence check
  bypassed (startup path skips the setter) and inconsistent enforcement (Params path
  frozen, RingBuffer path not).
- Conclusion: RESOLVED — default-config recovery is not pathological; no threshold
  relaxation needed for the standard variants. Config variants that use the freeze feature
  (e.g. xtrabackup-SST-style flows) should avoid persisting it; the
  persisted-option-from-boot shape is a distinct operator-error hazard, noted here but not
  promoted to a property (requires an operator action outside the harness's planned
  workload).
