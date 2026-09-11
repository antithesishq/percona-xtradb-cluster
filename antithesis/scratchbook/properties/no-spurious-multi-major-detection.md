# no-spurious-multi-major-detection — Evidence

> **PROPERTY INVALIDATED AS ORIGINALLY STATED — RE-SCOPED (2026-09-10).** The forced-
> MAINTENANCE branch is REACHABLE in a homogeneous cluster: every NON-PRIMARY view carries
> `protocol_version = -1` (see Investigation Log), so any partition that puts a node in
> non-Primary fires the branch with no fault in the negotiation machinery. The
> `Unreachable`-across-partitions framing is wrong. Re-scoped property below.

**Property (re-scoped one-liner):** In a homogeneous single-version cluster (all nodes PXC
8.4.10, protocol V7), the multi-major-version detection never fires **on a Primary view**:
`log_view` never forces `pxc_maint_mode = MAINTENANCE` while the delivered view is Primary,
and the rolling-upgrade write block never rejects a statement while the node is Synced in a
Primary component.

**Type:** Safety (impossible-state guard). **Assertion:** `Unreachable` at the forced-
MAINTENANCE branch (`sql/wsrep_server_service.cc:209-215`), **conditioned on
`view.status() == wsrep::view::primary`** and excluding the DBUG path — non-primary views
legitimately(?) reach the branch with proto -1 and must not trip it. Workload-side check:
`pxc_maint_mode` never differs from DISABLED (or the workload's own setting) *while the
node reports Synced/Primary*; during non-Primary windows a forced MAINTENANCE is expected
behavior of this code as shipped. **Confidence:** high — the trigger condition, its inputs,
the negotiated version, and the non-primary -1 path were all read directly.

## Mechanism

- `sql/wsrep_server_service.cc:200` — `wsrep_protocol_version` is overwritten from
  `view.protocol_version()` on EVERY view, under `LOCK_global_system_variables`. The value is
  the group-negotiated application protocol (min across members, established at GCS state
  exchange — `gcs/src/gcs_state_msg.cpp` carries per-node gcs/repl/appl proto versions
  :154-155,:205 and quorum selects the common version).
- :202 `multi_version_cluster = wsrep_protocol_version < WsrepVersion::V4`.
- :204-215 forced MAINTENANCE when `multi_version_cluster && pxc_strict_mode > PERMISSIVE`
  (default ENFORCING qualifies) — on a homogeneous 8.4.10 cluster the negotiated version is
  V7 (`wsrep_max_protocol_version = V7`, sql/wsrep_mysqld.cc:137-139; history :109-136), so
  this branch must be dead.
- Downstream blast radius if it ever fires spuriously:
  - Every ENFORCING node rejects all `CF_CHANGES_DATA` statements with ER_UNKNOWN_ERROR
    (`sql/sql_parse.cc:1867-1916`, call :3809) — total write outage.
  - `pxc_maint_mode = MAINTENANCE` on every node simultaneously → clustercheck 503
    everywhere → proxies mark the whole cluster down even for reads (focus 12 §7
    "rolling-upgrade black-hole", here without any actual upgrade).
  - The outage self-heals only on a subsequent view that recomputes the version (:216-230
    revert path) — until then the cluster is Primary, Synced, and unusable.
- Why a spurious firing is plausible enough to guard: the value is recomputed on every view
  from freshly exchanged state messages; candidate corruption paths are a joiner
  mid-SST/IST advertising a wrong/zero appl proto in its state message, a stale state
  message surviving a partition merge, or a torn read of the multi-byte enum (plain global,
  written under LOCK_global_system_variables but read unlocked in the per-statement gate at
  sql_parse.cc:1884). gcs protocol negotiation is also downgraded by *quorum selection*,
  not node version — any bug that folds a bogus member entry into the min drags the whole
  cluster below V4.
- Related version-negotiation observability: `SHOW STATUS LIKE 'wsrep_protocol_version'`
  exposes the provider-side negotiated version; the server-side `WsrepVersion` enum tracks
  `view.protocol_version()`. The workload can poll both.

## Antithesis angle

Network partitions, asymmetric partitions, and join/leave churn (all default-on faults)
hammer exactly the state-exchange → quorum → view → `log_view` pipeline that recomputes the
version. SST/IST rejoins add the "node with freshly reset state advertises its versions"
case. Full-cluster restart adds the pc.recovery/restored-view path. This property turns any
miscomputation anywhere in that pipeline into an immediately attributable signal instead of
an unexplained cluster-wide write outage.

## Instrumentation suggestions (all missing)

- SUT-side `Unreachable` ("spurious multi-major detection: pxc_maint_mode forced in
  homogeneous cluster") inside the :209-215 branch, guarded to exclude the
  `simulate_wsrep_multiple_major_versions` DBUG path so the debug-injection property
  (rolling-upgrade-write-gate) doesn't trip it, **and conditioned on
  `view.status() == primary`** (non-primary views reach the branch with proto -1 by
  construction — see the finding above). Works in the release image — the branch itself is
  live code.
- Optional companion `Sometimes` ("multi-major forcing fired on a non-primary view") at the
  same branch for the non-primary case — confirms the partition-driven path was exercised
  and anchors the operator-drain-hijack exploration for
  `maint-mode-honors-operator-intent`.
- Optional companion, distinct message, SUT-side `Unreachable` at the real (non-DBUG)
  block branch in sql_parse.cc:1888-1912 ("rolling-upgrade write block fired in homogeneous
  cluster") — catches the case where the gate fires without the maint-mode forcing.
- Workload-side `Always`: `pxc_maint_mode` on every node is DISABLED (or a value the
  workload itself set within the last transition window) whenever polled; and
  `wsrep_protocol_version` status equals the expected constant on all nodes.

## Config / fault requirements

- Release build sufficient (preferred — this is the field configuration); also valid in the
  debug image provided the DBUG flag is not set in this variant.
- Network faults default-on suffice; node termination (if enabled) adds the restart/SST
  paths; no clock faults needed.
- Do NOT combine in the same variant with rolling-upgrade-write-gate's debug injection —
  the two properties partition the same branch by trigger source.

## Open Questions

None — both resolved (see Investigation Log). Key outcomes:

- Joiners advertise a compile-time maximum, and quorum has an anti-downgrade clamp — the
  original "bogus joiner drags the min below V4" scenario is doubly guarded on PRIMARY
  views. The realistic trigger turned out to be elsewhere: NON-PRIMARY views carry -1 by
  construction, which is why the property was re-scoped to primary views.
- `wsrep_protocol_version` is an aligned signed-long enum: unlocked reads cannot tear on
  supported platforms; stale reads are transient and benign for this property (formally a
  C++ data race — memory-ordering hygiene, not negotiation logic).

## Non-primary -1 finding: consequences beyond this property

The reachable chain (release build, network faults only, default `pxc_strict_mode =
ENFORCING`):

1. Node lands in a non-Primary component → gcs delivers a non-prim CC with
   `conf.appl_proto_ver = quorum.appl_proto_ver = -1` (`GCS_QUORUM_NON_PRIMARY`,
   gcs/src/gcs_state_msg.hpp:78-85; gcs/src/gcs_group.cpp:2317).
2. `galera_view_info_create` copies it (`view_info->proto_ver = conf.appl_proto_ver`,
   galera/src/galera_info.cpp:39); wsrep-lib passes it through
   (wsrep_provider_v26.cpp:357-367) and `on_view` calls `log_view` for ALL view statuses
   including non_primary (wsrep-lib/src/server_state.cpp:1125-1156).
3. `log_view` (sql/wsrep_server_service.cc:200-215): `wsrep_protocol_version =
   (WsrepVersion)(-1)`; the enum is `: long` (include/service_wsrep.h:31-40) so
   `-1 < V4` → `multi_version_cluster = true` → forced `pxc_maint_mode = MAINTENANCE`,
   `wsrep_pxc_maint_mode_forced = true`.
4. On the next Primary view (V7) the else-branch reverts to DISABLED.

Consequences: (a) **operator-drain hijack is reachable in RELEASE builds with plain network
faults** — if the operator had set MAINTENANCE before the partition, the non-prim view
overwrites the ownership flag to "forced", and the post-heal Primary view silently reverts
the operator's MAINTENANCE to DISABLED (un-draining the node). This strengthens
`maint-mode-honors-operator-intent` and gives `rolling-upgrade-write-gate`'s hazard 1 a
no-debug-build trigger. (b) The write gate at sql_parse.cc:1884 is also active during
non-Primary, but the wsrep-readiness gate (:3787-3808) rejects writes first, so the
client-visible error is unchanged; the maint-mode/health-check effect is the observable
one. (c) The startup/shutdown "zero view" (replicator_smm.cpp:557-563) also carries -1 but
shutdown is guarded by `pxc_maint_mode == SHUTDOWN`.

### Investigation Log

#### What does a joiner advertise as its appl protocol version before first sync?

(2026-09-10, open-questions pass)

- Examined: gcs/src/gcs_group.cpp (group ctor :91-128, state msg creation :176-177, quorum
  install :529-536, proto checks :364-384), gcs/src/gcs_state_msg.cpp quorum selection
  :930-1017 (min over all states :960-973; anti-downgrade clamp :976-987 keyed on
  `GCS_STATE_MSG_NO_PROTO_DOWNGRADE_VER = 6`), gcs/src/gcs_core.cpp / gcs.cpp create path,
  galera/src/replicator_smm_params.cpp:32 (`MAX_PROTO_VER = 11`, `proto_max`).
- Found: every node — joiner or member, synced or not — advertises its *static supported
  maximum* (the provider's compile-time max, optionally capped by the `proto_max` config),
  not a sync-state-dependent value. The quorum takes min over all exchanged states, then
  clamps back up to the representative's previous-primary protocol (`prim_*_ver`) when
  state-msg version >= 6, so a low advertisement cannot downgrade an established primary.
- Found (the actual reachable trigger, different from the hypothesized one): NON-PRIMARY
  configuration changes carry `appl_proto_ver = -1` from `GCS_QUORUM_NON_PRIMARY` and this
  value reaches `log_view` unfiltered — chain documented above.
- Conclusion: resolved. Original question answered (advertised max + anti-downgrade clamp;
  no realistic primary-view trigger). Property invalidated as originally scoped and
  re-scoped to primary views; the non-prim behavior is recorded as a release-build finding
  feeding `maint-mode-honors-operator-intent`.

#### Is the unlocked read of wsrep_protocol_version torn/stale?

(2026-09-10, open-questions pass)

- Examined: include/service_wsrep.h:31-42 (`enum wsrep_version : long`),
  sql/wsrep_mysqld.cc:137-139 (definition/init), write site
  wsrep_server_service.cc:195-231 (under LOCK_global_system_variables), read sites
  sql_parse.cc:1883-1886 and wsrep_mysqld.cc:680 (unlocked).
- Found: the global is a naturally-aligned signed `long`; on the supported platforms
  (x86-64, aarch64) aligned word-size loads/stores do not tear. No atomics/fences, so a
  reader may observe a stale value for an unbounded-but-practically-short window; the value
  only changes at view delivery. Formally a C++ data race (UB), practically benign.
- Not found: any platform in the support matrix where tearing is possible; any reader that
  makes a persistent decision from a single stale read (the gate is re-evaluated per
  statement).
- Conclusion: resolved — no torn reads; staleness is transient and cannot cause a
  *sustained* spurious gate independent of views. Fix altitude if ever addressed:
  `std::atomic`/memory-ordering hygiene, not negotiation logic.
