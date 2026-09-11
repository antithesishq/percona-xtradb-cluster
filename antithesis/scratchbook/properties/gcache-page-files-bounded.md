# gcache-page-files-bounded — gcache page files never grow without bound or leak across restarts

**Focus area:** Resource boundaries — disk-space leak, missing cleanup, ENOSPC blast radius.
**Confidence:** High on the code mechanisms (all re-verified at galera @13ff9ed6); Medium on
how easily the page store is exercised without shrinking gcache.size.

## Claim under test

When the gcache ring buffer (`gcache.size`, default 128M) is full of un-discardable
writesets, allocation overflows to on-disk page files `gcache.page.NNNNNN` in
`wsrep_data_home_dir` (= datadir by default). Pages are claimed to be transient: released as
soon as fully unused (defaults `gcache.keep_pages_size=0`, `gcache.keep_pages_count=0` →
"release everything releasable", gcache/src/gcache_params.cpp:18,31). Two distinct unbounded
behaviors exist in code:

1. **Purge freeze wedge**: PXC-only `gcache.freeze_purge_at_seqno` blocks discard from a
   seqno onward with **no auto-unfreeze found anywhere**.
2. **Orphan pages across restarts**: the page store starts every process incarnation at
   `count_ = 0` with an empty `pages_` list and **never scans the directory** — files left by
   a crashed incarnation are invisible to accounting and cleanup.

Because gcache shares the datadir volume, unbounded page growth ends in ENOSPC in
`wsrep_data_home_dir`, which kills IST donation and grastate writes — a local disk leak that
escalates to cluster-level failures.

## Code paths (galera submodule)

- Overflow chain: `GCache::malloc` tries mem → rb → **ps** (page store)
  (gcache/src/GCache_memops.cpp:117-123).
- Freeze: `Params::skip_purge(seqno)` returns true for `seqno >= freeze_purge_at_seqno_`
  (gcache/src/GCache.hpp:261-273; setter :273 — no code path resets it except an explicit
  operator `SET`; default -1 = disabled, gcache_params.cpp:33). Enforced in
  `GCache::discard_seqno` (GCache_memops.cpp:55-60, bails out of the discard loop) and in the
  RB purge path (gcache/src/gcache_rb_store.cpp:204). Frozen purge ⇒ rb never frees ⇒ every
  new writeset lands in the page store ⇒ disk grows at replication-traffic rate.
- Equivalent non-config wedge: `seqno_locked_` held by a stalled IST donor session — discard
  bails when `seqno >= seqno_locked` (GCache_memops.cpp:41-52). A donor stuck serving a hung
  joiner freezes purge for the duration.
- Page release policy (PXC variant): release while
  `(!keep_size_ && !keep_page_) || total_size_ > keep_size_ || pages_.size() > keep_page_`
  and the oldest page is fully released (gcache/src/gcache_page_store.cpp:185-220,
  `delete_page()`). A single long-lived un-released buffer in the oldest page blocks the
  whole chain (deque discipline).
- Orphans: `PageStore` ctor sets `count_(0), pages_(), total_size_(0)` — no
  opendir/readdir/scan of pre-existing `gcache.page.*` (gcache_page_store.cpp:252-278; the
  only file deletions are of pages tracked in `pages_`, :51-70, :122). Page names restart at
  `gcache.page.000000` each incarnation (make_page_name, :43-48). File creation flags have
  `O_TRUNC` deliberately commented out (galerautils/src/gu_fdesc.cpp:37-38), so re-opened
  names inherit stale size/content; names the new incarnation never reaches persist forever.
- Ring-buffer recovery ignores pages entirely: seqnos that lived in pages vanish from
  seqno2ptr on restart (sut-analysis §4.3 — PageStore "NOT recovered at all").

## Failure scenario

- In-run: partition a joiner mid-IST (donor holds `seqno_locked_`) or set
  `freeze_purge_at_seqno` (operator-shaped action via `wsrep_provider_options`) under write
  load with a small `gcache.size` (e.g. 16M in the harness) → `gcache.page.*` grows
  monotonically until the datadir volume fills → grastate write failures (which are
  silently ignored — saved_state write_file returns void) and failed IST/SST donation.
- Across restarts: crash a node (kill -9) while pages exist → restart → pages from the old
  incarnation are orphaned; repeat crash/restart cycles under load → monotonic accumulation
  of stale page bytes never reclaimed until a human deletes them.

## Suggested assertion (missing — no SDK instrumentation exists)

- **Type: Always** (safety: a resource cap must hold on every check). Workload/sidecar
  polls per node: `sum(bytes of gcache.page.* in wsrep_data_home_dir)` and asserts
  `< CAP` where CAP ≈ gcache.size + 2×gcache.page_size + keep_pages_size slack, evaluated
  only when no state transfer is in progress on that node (donor lock legitimately holds
  pages during IST). A second **Always**: immediately after a node restarts and reaches
  Synced with no state transfer running, orphan page bytes (files older than process start)
  are 0 — this encodes "no leak across restarts" and will fail against current code by
  design if the orphan mechanism is real (regression-shaped: it documents a genuine defect
  to report, not an expected pass).
- Config note for the harness: set `gcache.size` small (16-32M) so ordinary load reaches the
  page store; otherwise the property is `AlwaysOrUnreachable` in practice. A companion
  `Sometimes(page files created)` guards against vacuity.
- SUT-side (missing): `Reachable` in `PageStore::new_page` (page-store engaged) and
  `Always(total_size_ <= configured cap)` in `PageStore::free/release_page` would localize
  which release condition failed.

## Fault availability

In-run growth: network faults + load + small-gcache config — default-on faults suffice.
Orphan accumulation: **requires node termination/restart (kill -9 + restart)** — flag: node
termination is often disabled; workload-driven `kill -9` of mysqld inside the container plus
supervisor restart is an alternative if the harness supervises processes directly.

## Open questions

None — all three resolved (see Investigation Log). Consequences:

- **No auto-unfreeze of `freeze_purge_at_seqno` exists anywhere in the repo** (gcache,
  galera, gcs, sql/, SST scripts all checked). It is set only via the provider-options
  param path (gcache_params.cpp:227-253, accepts "now"/seqno/-1) and reverts to the
  configured default (-1 = off) at process restart since runtime provider-option SETs are
  not persisted. The freeze variant is an operator foot-gun that persists until manual
  clear or restart — real, but requires the workload to keep the option set.
- **Stale same-name page files ARE ftruncated to the new size** (gu_fdesc.cpp:198-207:
  `current_size > size_` → ftruncate down; smaller → prealloc up). So per-name waste is
  bounded by `gcache.page_size`, and cross-restart orphan growth is NOT monotonic per name:
  the persistent leak is exactly the page files whose indices exceed the new incarnation's
  high-water mark (each incarnation restarts at gcache.page.000000 and reuses/truncates
  names it reaches). The post-restart zero-stale Always should therefore measure files
  *above* the current incarnation's reached index (or files not currently tracked), and the
  worst-case leak per crash cycle is (prior max index − new max index) × page_size.
- **IST donation holds the gcache seqno lock for the whole transfer**: `seqno_lock(first)`
  is taken at donor selection (replicator_str.cpp:586) and ownership passes to the async
  IST sender (run_ist_senders, :478-506); `seqno_unlock()` runs only in `Sender::send_done`
  after ALL writesets are served (ist.cpp:901-906) or in `~Sender` on abnormal teardown
  (ist.cpp:863-873). A stalled-but-connected joiner freezes donor purge from `first` for
  the entire stall — the in-run disk-growth scenario accrues at full replication rate for
  the duration.

### Investigation Log

#### Is there any auto-unfreeze of `freeze_purge_at_seqno`?

- Examined: repo-wide grep for freeze_purge (sql/, storage/, scripts/, build-ps/,
  percona-xtradb-cluster-galera/*); gcache_params.cpp param-set path (:227-253);
  GCache.hpp:261-291; gcache_rb_store.{hpp,cpp}.
- Found: the only writers are the param-set path (operator `SET
  wsrep_provider_options='gcache.freeze_purge_at_seqno=...'`, values "now"/seqno/-1) and
  the constructor default (SEQNO_ILL). No server-side, SST-script, or replicator caller;
  no reset on view change, donor completion, or any event.
- Not found: any automated setter/unsetter.
- Conclusion: resolved — no auto-unfreeze; freeze persists until explicit -1 or restart.
  Question removed.

#### Does Page construction ftruncate a stale same-name file?

- Examined: gcache_page.cpp Page ctor (:59-95) → FileDescriptor(name, size, allocate=true,
  sync) → galerautils/src/gu_fdesc.cpp size-ctor (:106-212).
- Found: open uses O_CREAT without O_TRUNC (CREATE_FLAGS, :37-38), then explicitly
  compares sizes: `current_size > size_` → `ftruncate(fd_, size_)` (:198-207);
  `current_size < size_` → prealloc/write_byte. So reused names are resized to the new
  page size.
- Conclusion: resolved — per-name waste bounded by page size; the durable leak is
  higher-index orphans the new incarnation never reaches. Property's orphan half refined
  accordingly (still a genuine defect: those files are invisible to accounting and never
  deleted). Question removed.

#### Is IST `seqno_locked_` whole-transfer or per-buffer?

- Examined: replicator_str.cpp slg guard (:468-475), process_state_req IST branch
  (:550-640), run_ist_senders (:477-506); galera/src/ist.cpp Sender dtor (:863-873),
  send_done (:900-908).
- Found: `gcache_.seqno_lock(first)` before serving; guard's unlock_ is set false when the
  async sender takes ownership ("seqno will be unlocked when sender exists");
  `gcache_.seqno_unlock()` only in send_done (after all writesets sent) or Sender dtor.
- Not found: any per-buffer or periodic release during an in-flight transfer.
- Conclusion: resolved — whole-transfer lock; a hung joiner freezes donor purge for the
  stall duration (teardown of the sender on connection error does release it). Question
  removed.
