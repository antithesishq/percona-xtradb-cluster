# homogeneous-cert-version-match — Evidence

**Property (catalog one-liner):** In a single-version cluster, certification never rejects a
writeset for protocol-version mismatch: the `trx_cert_version_match` TEST_FAILED branch
never executes, and `adjust_position` never wipes the certification index for a version
change.

**Type:** Safety (impossible-state guard / silent-divergence tripwire). **Assertion:**
`Unreachable` at galera/src/certification.cpp:421-427 (primary), with an optional companion
`Unreachable` (distinct message) at the `version != version_` index-wipe in
`adjust_position` (:1127-1140). `Unreachable` matches the semantics exactly: in a
homogeneous cluster both branches are impossible states whose execution silently converts
real writesets into no-ops or discards the conflict-detection index — precursor states of
undetectable divergence. **Confidence:** high that the branches are dead in a homogeneous
cluster (version sources traced below); the value is as a cheap tripwire over paths (IST
preload, gcache recovery replay, full restart) where version bookkeeping could desync.

## Mechanism (percona-xtradb-cluster-galera @13ff9ed6)

- `galera/src/certification.cpp:396-412` — `trx_cert_version_match(trx_version,
  cert_version)`: cert protocols ≤3 accept only their exact writeset version; protocol ≥4
  accepts versions 3..cert_version. Documented purpose: rolling-upgrade compatibility.
- `do_test` :421-427: mismatch → `log_warn` ("trx protocol version ... does not match
  certification protocol version") → `return TEST_FAILED`. Downstream, `test()`
  (:1160-1173) calls `trx->mark_dummy()` (:1172): **the writeset is silently converted to a
  no-op**. The seqno is consumed in total order, no error is raised, no inconsistency vote
  is possible (votes require apply errors). If this ever happens on a strict subset of
  nodes, those nodes silently drop a transaction the rest of the cluster commits.
- Version sources:
  - `version_` (certification protocol) is set only in `assign_initial_position`
    (:1045-1107, `version_ = version` :1106) and `adjust_position` (:1111-1160,
    `version_ = version` :1145) — both driven by the group-negotiated protocol carried on
    configuration changes. Homogeneous 8.4.10 ⇒ constant.
  - `trx->version()` is the writeset wire version stamped by the originator — constant in a
    homogeneous cluster. A peer sending a bad version byte is caught earlier and fatally
    (`write_set_ng.hpp:169-171` aborts on bad version byte), so this branch guards the
    subtler case: *locally consistent but wrong* version bookkeeping.
  - `adjust_position` :1127-1140: on `version != version_` the whole trx map is purged and
    the cert index cleared — correct during a real protocol upgrade, catastrophic if
    triggered spuriously (all in-flight dependency/conflict state discarded while
    transactions are being certified).
- IST carve-out worth watching: preloaded writesets (`certified()==true`) skip the interval
  check but NOT the version check (:421 runs first) — so a donor serving cert-index preload
  writesets recorded under a different bookkept version would trip this branch on the
  joiner. In a homogeneous cluster this "different version" can only arise from a
  bookkeeping bug (e.g., gcache recovery after unclean restart replaying writesets whose
  header version was mis-read, or `assign_initial_position` called with the `-1` init
  sentinel at a wrong moment — the `case -1:` is accepted :1053-1060).

## Antithesis angle

The branch's inputs are recomputed across the highest-churn recovery paths: SST/IST joins
(assign_initial_position + cert-index preload), configuration changes (adjust_position on
every primary view), full-cluster restart with gcache recovery (preamble/scan paths,
sut-analysis §4.3), and provider re-init. Fault injection drives exactly these paths;
the property converts "a writeset quietly became a dummy on one node" — otherwise
observable only as end-state checksum divergence with zero log signal beyond one warn line —
into an immediate, located assertion failure and replay anchor.

## Instrumentation suggestions (all missing)

- SUT-side `Unreachable` ("cert version mismatch in homogeneous cluster") at
  certification.cpp:421-427, carrying details `(trx_version, cert_version, seqno)`.
- SUT-side `Unreachable` ("cert index wiped for version change in homogeneous cluster") at
  certification.cpp:1127-1140.
- Zero workload cost; both work in the release image (the branches are live code, not
  asserts). If SUT-side instrumentation is deferred, an interim log-scrape detector for
  "does not match certification protocol version" gives the same signal without replay
  anchoring.
- These same assertions become deliberate `Reachable`/`Sometimes` targets if a
  mixed-version harness is ever built — the instrumentation is forward-compatible with
  that roadmap; only the assertion type flips.

## Config / fault requirements

- Single-version cluster (the harness default). Release or debug build.
- Network faults (default-on) for view churn and IST/SST joins; node termination (if
  enabled) adds the gcache-recovery replay path — flag: without kill faults, drive unclean
  restarts via the harness or accept reduced coverage of the recovery path.

## Open Questions

None — the version=-1 sentinel window is resolved (see Investigation Log): no writeset can
be certified while `version_ == -1`, so the `Unreachable` needs no startup-window
exclusion.

### Investigation Log

#### Can assign_initial_position run with version = -1 while writesets are certified before a real version is assigned?

(2026-09-10, open-questions pass)

- Examined: all call sites of `assign_initial_position` / `adjust_position` —
  `replicator_smm.cpp:286` (ctor, uses `trx_params_.version_`, real), `:346`
  (`shift_to_CLOSED` corrupt-state cleanup, node closing), `:3029`
  (`reset_index_if_needed`, the -1 sentinel path), `:3301` (`process_prim_conf_change`
  ordered-CC tail, real version via `establish_protocol_versions`);
  `replicator_str.cpp:1382` (galera-3 SST compat), `:1859` (first IST preload trx, uses
  `ts->version()`), `:1902`/`:1937`/`:1964` (IST CCs, real version); the CC local-monitor
  bracket (`process_conf_change`, replicator_smm.cpp:2665-2683); certification serialization
  (`cert()` enters LocalOrder at :2204-2221); `async_recv`'s -ECANCELED → `recv_IST` loop
  (:488-495); `reset_index_if_needed` (:2972-3036) incl. the `index_reset` condition and
  `pending_cert_queue_.clear()`.
- Found: the only -1 assignment on a live node is `reset_index_if_needed` at :3029
  (`trx_proto_ver = -1` for `PROTO_VER_ORDERED_CC`+). It executes inside the CC's
  LocalOrder critical section (:2665-2683), and certification of every writeset is
  serialized through the same LocalOrder monitor in local order — so no writeset can reach
  `do_test` between the -1 reset and the real-version assignment. The real version is
  restored either (a) in the same critical section for an in-order CC
  (`cert_.adjust_position(..., trx_params_.version_)` at :3301), or (b) on the state
  transfer path, by the first IST preload trx (:1859, `ts->version()`) or the IST CC
  (:1937) — and during state transfer live GCS processing is suspended (`as_->process`
  returns -ECANCELED and appliers loop in `recv_IST`), so nothing is certified until the
  preload has set a real version. Dummy-only IST is covered by the :1937 CC assignment
  (comment at replicator_str.cpp:1848-1856). The ctor path (:286) never uses -1; the
  corrupt path (:346) runs while shifting to CLOSED with receivers drained.
- Not found: any interleaving where `Certification::append_trx`/`do_test` can observe
  `version_ == -1`.
- Conclusion: resolved — the branches guarded by this property are genuinely dead in a
  homogeneous cluster including the init/join windows; the `Unreachable` can be placed
  without any exclusion, and a firing is a real bug, not a startup artifact.
