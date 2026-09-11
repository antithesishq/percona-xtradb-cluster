# gcache-recovered-ist-completeness — IST served from a crash-recovered gcache is complete or refused

**Type:** Safety | **Assertion:** Always | **Confidence:** Medium

**FAULT REQUIREMENT: node termination (kill/restart) — often DISABLED by default in
Antithesis; the donor must crash-recover its gcache for the interesting case to exist.
Disk faults (torn mmap pages) widen coverage further.**

## What led to this property

After an ungraceful stop, the gcache ring buffer (`galera.cache`, mmap'd, payload never
msync'd during operation — `gcache/src/gcache_rb_store.cpp:176-178`, MMapFactory
create(sync=false) :121) is reconstructed by a linear-probe `scan()` whose integrity
checks are weakest exactly in the crash case: the preamble carries seqno_min/max/offset
only on graceful close (`write_preamble(synced)` :745-762), so after a crash offset=-1
and the strict preamble-seqno sanity check is skipped. Verified at branch tip
(`gcache_rb_store.cpp:1100-1131`): the remaining plausibility checks use an epsilon of
`size_cache_/sizeof(BufferHeader)+1` seqnos (~5.6M for 128MB) — wide enough to admit
substantial corruption. Verified: a truncated last segment is **warn-only** ("Failed to
scan the last segment to the end. Last events may be missing. Last recovered event: ...",
:1307-1317) — recovery proceeds. The overflow page store is not recovered at all
(`gcache_page_store.cpp` count_=0, no dir scan).

Realized bugs: PXC-5209 (BH_test trusted corrupt on-disk BufferHeader → bogus free /
~10^12 seqno → OOM; the pxc_gcache_corrupt_{size,seqno}.test byte-patch the cache —
directly Antithesis-shaped) and upstream MDEV-36621 ("IST didn't contain all write sets").
Donor selection additionally uses a **stale gcache low-water snapshot** from state-exchange
time (`gcs/src/gcs_group.cpp:2386`, `group_find_ist_donor` :1789-1845, PXC zeroes the
safety gap for IST-only requests :1817), re-validated only at `seqno_lock`
(`replicator_str.cpp:586-597`).

## Mechanism / code involved

- Recovery: `open_preamble/recover/scan` (`gcache_rb_store.cpp:798, 1426, 1134`); mapping
  exception → full silent reset → forced SST (:1279-1297, :1441-1457) — the *safe*
  failure; seqno collision → discard both buffers (:1195-1265).
- Donor IST service: `process_state_req` (`replicator_str.cpp:508`) → gcache
  `seqno_lock`; NotFound → full SST fallback (:557-597); IST-only request → -ENODATA →
  joiner abort.
- IST sender: `run_ist_senders` (:612-625), `ist.cpp` sender; donor-side contiguity assert
  is commented out (`gcs_group.cpp:1857-1885` FIXME) — nothing on the donor asserts the
  served range is gapless.
- Joiner: `recv_IST` (`replicator_str.cpp:1632`); cert-index preload (:673-723).

## Failure scenario

Donor crashes and restarts; gcache scan() recovers a plausible-but-wrong set of writesets
(truncated tail warn-only; stale/torn buffers inside epsilon; page-store contents gone but
referenced history assumed contiguous). A joiner then requests IST; the donor serves a
range it advertises as [first, last] that is actually missing or corrupting writesets in
the middle/tail. Joiner completes "successfully" and reports Synced while missing
transactions → silent divergence (caught only by the cross-node checksum), or applies
garbage → apply error → eviction of the *joiner* (misattributed blame), or PXC-5209-style
donor OOM/crash during recovery.

## Invariant / how to check

Workload-side `Always`: whenever a node completes a join via IST and reports Synced,
its per-table checksums equal the donor's/cluster's (specialization of
cross-node-row-equality, evaluated specifically at the join-completion event with the
join type recorded from `wsrep_local_state` transitions / logs). Equivalently: an
IST-joined Synced node never lacks a transaction the cluster acked before its join.
`Always` because every successful IST must yield an identical state; "donor refuses and
falls back to SST" and "joiner aborts with a detected gap" are both acceptable outcomes —
only *silent* incompleteness violates the property.

Companion `Sometimes` (coverage guards, essential here): (a) a donor served IST after its
own ungraceful restart (crash-recovered gcache actually used as IST source); (b) the
gcache scan() recovery path ran and recovered a non-empty seqno range.

## Timing / config dependencies

- Sequence to explore: kill donor mid-load → restart donor (gcache scan recovery) → kill
  a second node → have it rejoin via IST *from the recovered donor*. In a 3-node cluster
  Antithesis will find this ordering if kills are enabled.
- gcache small enough (default 128M; consider smaller) that the ring wraps under load —
  wrap + crash is where scan() segment logic is trickiest; also drives the unrecovered
  page-store path via large transactions.
- `repl.force_sst_after_inconsistency` default OFF ships; leave OFF in one variant so a
  previously-evicted node's stale gcache/grastate can be re-offered (interaction with
  PXC-5208).
- gcache lives in the datadir volume — ENOSPC interacts (separate liveness concern).

## SUT-side instrumentation suggestions (all missing)

- **missing**: `Always` in `RingBuffer::scan()` at the warn-only truncated-segment branch
  (`gcache_rb_store.cpp:1307-1317`): assert the recovered [seqno_min, seqno_max] is
  gapless in seqno2ptr_ (any gap = must not be advertised); or `Unreachable` on the
  warn-branch itself if truncation is expected to be impossible when advertised.
- **missing**: `Always` on the donor IST sender: the served range equals the requested
  range with no seqno gaps (re-instates the commented-out contiguity FIXME at
  `gcs_group.cpp:1857-1885` as a non-fatal SDK assertion).
- **missing**: `Reachable` on the full-silent-reset path (:1441-1457) — confirms the safe
  fallback is actually exercised.

## Open questions

None. (All three resolved 2026-09-10 — see Investigation Log. Net effect on the property:
the advertisement IS truthful (derived from the post-scan, post-gap-trim `seqno2ptr`, never
from the preamble), page-store non-recovery is a range-SHORTENING channel (surprise SST),
not silent incompleteness, and MDEV-36621 is fixed in the vendored galera — so the
remaining silent-incompleteness channels are precisely: (a) gcache-METADATA corruption
inside the PXC-5209 epsilon that flips `bh->flags` (a flipped BUFFER_SKIPPED bit turns a
real writeset into IST `T_SKIP` — silent omission with no error anywhere) or corrupts
`bh->type` (silently degraded to `T_SKIP` via an NDEBUG-elided assert's `default:` arm, or
mis-routed as a CC), and (b) the protocol-level weakness that only the LOW water is
exchanged — `group_find_ist_donor` assumes contiguity from `cached` up to the group seqno
and works from a state-exchange-time snapshot. Payload corruption is NOT silent: the
joiner verifies writeset header+payload checksums (`replicator_str.cpp:1726`,
`write_set_ng.hpp:868-870`) and IST fails loudly. The property's weight therefore shifts
exactly as anticipated: keep the cross-node-checksum Always (it is what catches the
flag/type metadata shapes), keep the crash-recovered-donor Sometimes guards, and treat
MDEV-36621 as a regression target.)

### Investigation Log

#### Is the post-scan advertisement truthful (truncated tail, preamble interaction)?

Investigated 2026-09-10.

- Examined: `gcache_rb_store.cpp` `scan()` :1133-1409 (seqno_max update :1298,
  truncated-tail branch :1305-1320), `recover()` :1424-1655 (gapless-suffix trim
  :1461-1488), `open_preamble` :796-1050, `write_preamble` :734-795; `GCache.hpp:105-117`
  (`seqno_min()`), `GCache.cpp:84-85`; `gcs_group.cpp:2385-2395` (state msg `cached` =
  `gcache_seqno_min`), :1637-1654, :1787-1845 (`group_find_ist_donor`), :2332;
  `gcs_state_msg.cpp:404-409`, `gcs_node.hpp:154-163`; `replicator_smm.cpp:288` +
  `GCache_seqno.cpp:17-48` (`seqno_reset`).
- Found: the advertised `cached` low-water comes from `GCache::seqno_min()` over the
  LIVE, post-recovery `seqno2ptr` — never from the preamble (preamble seqnos are used
  only as sanity BOUNDS to `recover()` and for logging; the `synced` flag gates nothing).
  A truncated tail shrinks `seqno_max` to the last recovered event; `recover()` then trims
  to the gapless suffix ("found gapless sequence"). At replicator startup `seqno_reset`
  trims the map to the recovered DB position or clears it entirely (advertises SEQNO_ILL)
  when the map can't vouch for the position.
- Found (residual weaknesses): only the low water is exchanged — the upper end is inferred
  from `group->quorum.act_id` (contiguity assumed); `gcs_node_cached()` is a snapshot from
  the last state exchange, so the donor's real low water can rise above the advertised one
  between configuration changes (re-validated only at `seqno_lock`,
  `replicator_str.cpp:586-597` — full-SST fallback, benign direction).
- Conclusion: RESOLVED — advertisement is truthful; the silent-incompleteness channel
  narrows to mid-range metadata corruption within the epsilon (see summary above) plus the
  low-water-only protocol assumption. Partial tag removed.

#### Is MDEV-36621 fixed in the vendored Galera 4.27 (@13ff9ed6)?

Investigated 2026-09-10.

- Examined: galera submodule git history — commit `05ad4ae4` ("MDEV-36621: galera.GCF-360
  test: IST failure", 2026-03-16, Hemant Dangi), verified `git merge-base --is-ancestor
  05ad4ae4 HEAD` → IS an ancestor of 13ff9ed6; the diff (`GCache_seqno.cpp:210-256`) and
  the added regression test `top_level_seqno_lock_protects_ist_buffers`.
- Found: the fix clamps `seqno_release()`'s batch end to `seqno_locked - 1`
  (`std::min(s_end, seqno_locked - 1)`) and stops the release loop at the lock boundary,
  so buffers locked for IST can no longer be released/discarded mid-IST. Also noted: the
  pre-fix failure was DETECTED by the joiner ("IST didn't contain all write sets,
  expected last: N last received: -1") — loud, not silent.
- Conclusion: RESOLVED — fixed in the vendored tree; the donor-side race narrows to
  selection-vs-purge (full-SST fallback, benign). Keep as a regression target: the fix is
  a boundary clamp in hot purge logic, exactly the kind of edge Antithesis re-tests for
  free via the existing Sometimes guards.

#### Can page-store non-recovery silently shrink the served range without reflecting it?

Investigated 2026-09-10.

- Examined: `GCache_memops.cpp:104-125` (mem→rb→ps allocation cascade),
  `gcache_rb_store.cpp:380-402` (RB refuses >1/2 cache or >free), `gcache_page_store.cpp:
  239-286` (ctor: `count_=0`, no dir scan), `recover()` gap trim :1461-1488, collision
  handling :1195-1270, `gu_deqmap.hpp:341-392` (null padding = real holes),
  `GCache_seqno.cpp:397-398` (`seqno_get_buffers` stops at first discontinuity, "#643").
- Found: mid-range seqnos CAN live only in page files (large writeset or transient
  RB-full). After crash, pages are not scanned, so those seqnos are holes in the rebuilt
  map — and `recover()`'s backward gapless walk discards everything BELOW the first hole,
  raising the advertised low water. The recovered map contains only RB pointers; no
  dangling page references exist. Second line of defense: `seqno_get_buffers`
  independently stops at the first discontinuity.
- Conclusion: RESOLVED — page-store absence is a range-shortening channel (unexpected SST
  instead of IST — a liveness/cost effect), NOT a silent-incompleteness channel. The
  advertised range never includes page-resident seqnos after recovery. Caveat recorded:
  correctness rests on the single backward walk at `gcache_rb_store.cpp:1466` and is not
  re-verified later — the SUT-side "recovered seqno2ptr is contiguous" Always suggested
  below remains the sharp instrumentation for it.

## Synthesis refinement (2026-09-10)

GU_DBUG_SYNC note: provider sync points are a candidate mechanism for holding a donor mid-IST to compose with crash-recovered-gcache preconditions; availability in the PXC build unverified (one runtime SET answers it). Crash legs ride the workload->supervisor kill channel.
