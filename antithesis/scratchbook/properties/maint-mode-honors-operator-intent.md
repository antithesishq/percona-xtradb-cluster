---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# maint-mode-honors-operator-intent — Operator-set pxc_maint_mode survives view changes

**Focus area:** Lifecycle transitions — pxc_maint_mode forced-flip during view changes vs
operator intent; runtime reconfiguration racing membership events.

## Claim under test

`pxc_maint_mode` is the drain signal load balancers (ProxySQL/HAProxy via clustercheck)
key on. Once an operator sets `pxc_maint_mode=MAINTENANCE` on a node, the server must not
flip it back to DISABLED on its own — otherwise a drained node silently re-enters the
traffic pool mid-maintenance. The server *does* have a self-mutation path on every view
change; this property pins down when it may fire.

## Code paths (verified at commit f9ecb3e)

- **View-change mutation**: `Wsrep_server_service::log_view`,
  `sql/wsrep_server_service.cc:196-231` (under LOCK_global_system_variables):
  - if cluster protocol < V4 and `pxc_strict_mode > PERMISSIVE` → `pxc_maint_mode =
    MAINTENANCE; wsrep_pxc_maint_mode_forced = true` (:203-215); also forced by
    `DBUG_EVALUATE_IF("simulate_wsrep_multiple_major_versions")`. RELEASE-REACHABLE in a
    homogeneous cluster: every NON-PRIMARY view carries `appl_proto_ver = -1`
    (`GCS_QUORUM_NON_PRIMARY`) and `log_view` runs for all view statuses, so -1 < V4
    fires this branch on every partition — see
    `no-spurious-multi-major-detection.md` ("Non-primary -1 finding") for the full chain;
  - else, **if `wsrep_pxc_maint_mode_forced` is set, flips mode to DISABLED**
    (:217-228) — regardless of whether the operator meanwhile *also* wanted MAINTENANCE.
- **Operator SET never clears the forced flag**: `pxc_maint_mode_update` is a no-op
  (`sql/wsrep_var.cc:1096`); `pxc_maint_mode_check` (:1039-1094) only blocks changes while
  `forced && strict_mode >= ENFORCING`, and allows DISABLED↔MAINTENANCE otherwise. So with
  `pxc_strict_mode <= PERMISSIVE`: forced episode begins (forced=true, mode=MAINTENANCE);
  operator independently confirms/intends MAINTENANCE; upgrade completes; next view →
  else-branch sees forced=true → **flips to DISABLED, erasing the operator's drain**.
- **SHUTDOWN mode interaction**: signal handler sets `pxc_maint_mode =
  PXC_MAINT_MODE_SHUTDOWN` then sleeps 10s (`sql/mysqld.cc:4395-4404`); both log_view
  branches carve out `!= SHUTDOWN`, so shutdown wins — correct. The SET path compares a
  cached value and sleeps `pxc_maint_transition_period` (10s) inside SQLCOM_SET_OPTION
  (`sql/sql_parse.cc:4519-4523`) — but the sleep runs *after* `sql_set_variables()`
  returned and holds **no MDL and no LOCK_global_system_variables** (a table-less SET
  takes no table MDL; the sys-var mutex is released inside `sql_set_variables`). It only
  blocks the operator's own session for 10s. (An earlier draft called this a
  cluster-wide TOI-blocking hazard — corrected; see Investigation Log.)
- Health-check coupling: `scripts/clustercheck.sh` returns 200 only when
  `pxc_maint_mode==DISABLED` (sut-analysis §8.7) — a spurious DISABLED flip returns a
  drained node to rotation; a spurious MAINTENANCE (rolling-upgrade forcing on ALL nodes)
  marks the entire cluster down simultaneously (the documented black-hole,
  sut-analysis §8.7/focus 12 §7).

## Failure scenario

Rolling-upgrade shape (simulated in a homogeneous harness via the DBUG knob, or real with
mixed images): protocol drops below V4 → every node forces MAINTENANCE (all health checks
503 — total traffic black-hole, itself a reportable outcome); operator additionally drains
node A for hardware work; last old node leaves → next view flips A (and everyone) to
DISABLED → load balancer sends traffic to A mid-maintenance. Simpler homogeneous case: any
code path that sets `wsrep_pxc_maint_mode_forced` without a matching operator-visible
signal makes the next view change mutate an operator-owned variable.

## Suggested implementation

- **Workload-side (primary)**: workload maintains its own intent ledger. When it sets
  MAINTENANCE on node X, until it sets DISABLED it periodically asserts `Always`:
  "pxc_maint_mode on X is MAINTENANCE (or SHUTDOWN if a stop was initiated)". Membership
  churn (partitions, joins) runs concurrently to generate view changes.
- **Sometimes markers**:
  - "view change forced pxc_maint_mode to MAINTENANCE" (**missing SUT instrumentation** at
    `wsrep_server_service.cc:214` — distinct outcome, danger state for the all-nodes-down
    black-hole);
  - "view change reset forced pxc_maint_mode to DISABLED" (:226) — the flip under test;
  - "operator SET pxc_maint_mode completed during a view change window" (workload-side).
- **Debug-image lever**: `DBUG_EVALUATE_IF("simulate_wsrep_multiple_major_versions")`
  (`wsrep_server_service.cc:206-207`) makes the forcing branch reachable on a
  Synced/Primary node — set via `SET GLOBAL debug='+d,...'` in the debug build variant.
  It is an AMPLIFIER, not a prerequisite: in the release image the forcing branch fires
  on every non-primary view (protocol -1 chain above), so plain network partitions drive
  the forced-flip/revert state machine and the operator-drain hijack with no debug build.

## Assertion type

`Always` (safety) — the workload-side intent check must hold at every poll while the
operator drain is active. The two SUT-side markers are `Sometimes` companions in BOTH
images. RECONCILIATION (2026-09-10): an earlier draft of this file declared the
release-image markers `Unreachable`, reasoning that forced=true requires mixed major
versions. The sole-writer finding stands (`log_view` is the only writer), but the
reachability conclusion is superseded by the non-primary protocol -1 chain traced in
`no-spurious-multi-major-detection.md`: `log_view` runs on non-primary views with
`protocol_version = -1 < V4`, so the forcing branch fires in a homogeneous RELEASE
cluster on every partition, and the next primary view force-reverts. Release markers are
therefore `Sometimes`; the operator-drain hijack is a real release-build finding.

## Fault requirements

Network faults (default-on) generate the view changes AND the forcing branch (non-primary
views carry protocol -1). No node termination or clock faults required; no debug build
required — the DBUG knob remains available in the debug image as an amplifier for
forcing on a Synced/Primary node. True mixed-version clusters are out of scope for now
(sut-analysis open question 27).

## Confidence

High on mechanism (all three writers read directly: log_view, check/update fns, signal
handler) and high on release reachability (the non-primary protocol -1 chain was read
end-to-end in `no-spurious-multi-major-detection.md`).

## Open questions

- Does ProxySQL v2's native Galera support (which no longer reads pxc_maint_mode,
  sut-analysis §8.7) change what the customer considers "operator intent"? Affects the
  property's real-world weight, not its correctness. `(needs human input)`

Resolved (see Investigation Log):

- `wsrep_pxc_maint_mode_forced` has **no writer besides `log_view`** (tree-wide sweep,
  all file types incl. plugin/ and components/). NOTE: the original conclusion drawn from
  this ("release-image markers are `Unreachable`") is superseded — `log_view` itself is
  release-reachable on every non-primary view (protocol -1 chain, see
  `no-spurious-multi-major-detection.md`), so release markers are `Sometimes`. The
  catalog Invariant reflects the reconciled conclusion.
- The 10s SET-path sleep holds **no MDL** — it cannot block TOI DDL; the follow-on
  liveness property is dropped.

### Investigation Log

#### Is there any writer of `wsrep_pxc_maint_mode_forced` besides `log_view`?

- Examined: tree-wide grep (no extension filter, whole repo incl. `plugin/`,
  `components/`, `percona-xtradb-cluster-galera/`, `wsrep-lib/`, scripts).
- Found: exactly six references — writes only at `sql/wsrep_server_service.cc:215`
  (set true) and `:227` (set false), both inside `Wsrep_server_service::log_view`;
  definition `sql/wsrep_mysqld.cc:176`; extern `sql/wsrep_mysqld.h:160`; read
  `sql/wsrep_var.cc:1053`; read `wsrep_server_service.cc:218`.
- Not found: any other writer anywhere in the tree.
- Conclusion (as originally written): forced=true reachable only via group protocol < V4
  (mixed majors) → release markers `Unreachable`.
- **Reconciliation correction (2026-09-10)**: the premise "protocol < V4 requires mixed
  majors" is false — non-primary views carry `appl_proto_ver = -1` in ANY cluster
  (`GCS_QUORUM_NON_PRIMARY`), and `log_view` runs for all view statuses, so the forcing
  branch fires in a homogeneous release cluster on every partition (full chain in
  `no-spurious-multi-major-detection.md`, same-day investigation). The sole-writer fact
  stands; the release markers are `Sometimes`, the operator-drain hijack is
  release-reachable with network faults, and the catalog entry was updated accordingly
  (priority raised to High).

#### Does the 10s sleep inside SQLCOM_SET_OPTION hold MDL that TOI DDL needs?

- Examined: `sql/sql_parse.cc` SQLCOM_SET_OPTION case (:4478-4523): sequence is
  `check_table_access` → `open_tables_for_query` → `sql_set_variables` → `my_ok` →
  cached-mode compare → `sleep(pxc_maint_transition_period)` at `:4522`; sys-var locking
  in `sql_set_variables`/`set_var::update`; `pxc_maint_mode_check/update`
  (`sql/wsrep_var.cc:1039-1096`).
- Found: a `SET GLOBAL pxc_maint_mode=...` statement has no tables → `all_tables` is
  null → `open_tables_for_query` acquires nothing; `LOCK_global_system_variables` is
  taken and released inside `sql_set_variables` (per-variable update), i.e. *before* the
  sleep; `pxc_maint_mode_update` is a no-op so no variable-specific lock persists. During
  the sleep the session holds no MDL, no global sys-var mutex.
- Not found: any lock held across the sleep. (Caveat: if the operator issues the SET
  inside an open transaction already holding MDL from earlier statements, that unrelated
  hold is extended by 10s — operator-owned, not a SUT hazard.)
- Conclusion: resolved — no TOI-blocking; the sleep only delays the operator's session.
  Evidence-file body corrected; the proposed follow-on liveness property is dropped.

#### Does ProxySQL v2's native Galera support change what the customer considers "operator intent"?

- Examined: sut-analysis §8.7 (health-check/proxy integration survey: ProxySQL v2's
  native Galera support reads wsrep state variables directly and no longer consults
  `pxc_maint_mode`, while clustercheck-based integrations — ProxySQL v1 scheduler,
  HAProxy httpchk — still gate on it, i.e. two health regimes coexist in the field);
  this repo (no proxy integration ships in-tree beyond `scripts/clustercheck.sh`);
  deployment-topology.md (no proxy container in v1, so nothing in the harness answers
  it empirically either).
- Found: the property's *correctness* is unaffected — the forced-flip erases the
  variable's value regardless of who reads it. What changes is real-world *weight*:
  if the customer fronts PXC with ProxySQL v2 (native), a hijacked `pxc_maint_mode`
  affects only clustercheck consumers; if they use clustercheck-based routing, the
  hijack silently returns a draining node to rotation.
- Not found: anything in the repo or upstream docs stating which proxy generation this
  customer actually fields — that is deployment knowledge only the customer has.
- Conclusion: tagged `(needs human input)` — ask the customer which proxy/health-check
  integration fronts their clusters; the answer weights the property, it does not
  change the invariant.

## Synthesis refinement (2026-09-10)

KNOWN-RED pre-registration: the forced flip fires on every non-primary view under partitions — the Always is expected to fail from run one (deliberate bug-finder). Pre-register the forced-flip/revert arm as a known finding with a carve-out so the remaining intent-ledger checks still guard regressions.

## First-run evidence (run `afeec3df4338f14ada334136f1794bac-63-0`, 2026-09-23)

The property fired 776 times: 708 green, 68 red. Breaking the reds down by
`(operator_intent, observed)` is what made the entry above actionable, because
only one of the three shapes is this property:

| count | intent | observed | verdict |
|---|---|---|---|
| 45 | MAINTENANCE | SHUTDOWN | **not a violation** — the signal handler sets SHUTDOWN (`sql/mysqld.cc:4395-4404`) and both `log_view` branches carve out `!= SHUTDOWN`. Shutdown wins by design. Compounded by a harness bug: `maint_mode_cycle` was not in `leases.DISRUPTIVE`, so the `graceful_shutdown` lever ran concurrently with the hold. |
| 21 | DISABLED | MAINTENANCE | **not a violation of THIS property** — the forced-FLIP branch (`wsrep_server_service.cc:203-215`) firing on a non-primary view, exactly as the "Non-primary -1 finding" chain predicted. Owned by `no-spurious-multi-major-detection`. |
| 1 | MAINTENANCE | DISABLED | **the finding.** The forced-REVERT hijack this property was written to catch. |
| 1 | DISABLED | SHUTDOWN | carve-out, as row 1. |

So the release-build, network-faults-only reachability predicted in the
Antithesis Angle is CONFIRMED on both arms: the forced flip is common (21
observations) and the operator-drain erasure reproduced once in 120 minutes.

Consequences for the implementation, applied 2026-09-23:

- The workload assertion was a two-way equality, which is broader than the
  claim. It is now one-directional (intent MAINTENANCE reverted to DISABLED)
  and renamed to `an operator-set pxc_maint_mode=MAINTENANCE is never reverted
  to DISABLED`.
- SHUTDOWN and the forced-FLIP direction are carved out explicitly.
- A view change is deliberately NOT carved out. An earlier attempt at this fix
  gated the claim on `wsrep_cluster_conf_id` being unchanged across the hold;
  that was wrong and was reverted — `log_view` runs ON the view change, so the
  gate would have suppressed the single real observation above.
- `maint_mode_cycle` joined `leases.DISRUPTIVE` so it can no longer overlap
  `graceful_shutdown`. The SHUTDOWN carve-out stays anyway, because an
  unrelated crash-restart can still set it.

**Open question resolved:** none. The ProxySQL v2 question above is untouched.
