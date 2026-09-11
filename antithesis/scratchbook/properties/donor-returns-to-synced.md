---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# donor-returns-to-synced — Donor/desynced node returns to SYNCED; desync accounting never leaks

**Merged from two independent discoveries (focus 8: lifecycle, as
`donor-returns-to-synced`; focus 7: distributed coordination, as
`donor-desync-count-returns-to-zero`) — both agents found the same in-code-documented
permanent-desync mode from different directions.**

**Type:** Liveness (stable-state) | **Assertion:** workload `Always` at quiesced
checkpoints ("no active desync source ⇒ desync_count==0 ∧ wsrep_local_state=4") +
`Sometimes` companions | **Confidence:** High on mechanism (the permanent-desync mode is
documented in-code and the resync swallow is explicit); Medium on findability (the
out-of-order window needs a view change in a narrow race — exactly what Antithesis
explores).

## Claim under test

A node that leaves SYNCED to serve a state transfer (DONOR) or because it was desynced
(`wsrep_desync=ON`, FTWRL/backup pause, RSU) returns to SYNCED within a bounded time after
the donation/desync ends and load quiesces. Desync reference counting is always balanced:
after every SST/IST donation, `wsrep_desync` toggle, RSU cycle, or any interleaving of
these with view changes, `desync_count` returns to 0. No node is left permanently
DONOR/Desynced (`wsrep_local_state=2`) while the cluster believes it healthy.

## Code paths (verified at f9ecb3e; galera @13ff9ed6)

- **Desync counting in the group**: `gcs/src/gcs_group.cpp:1293-1310`
  (`gcs_group_handle_join_msg`) — a JOIN message "to" a donor decrements
  `sender->desync_count`; only at `desync_count == 0` does status go JOINED (then SYNC per
  `gcs/src/gcs.cpp:689-716` once the queue drains). **The `assert(sender->desync_count >
  0)` before the decrement is compiled out under NDEBUG** — an unbalanced decrement in
  release wraps/never-zeroes silently.
- **Documented permanent-desync failure mode, in-code**: `gcs/src/gcs.cpp:2743-2755`
  (`gcs_join`): "If the DONOR does desync in combination with SST donation, the gcs_join()
  calls from resync() and sst_sent() might come with out of order seqnos, leaving the
  desync_count in gcs_group permanently in non-zero value. In this case the node will not
  become synced again unless it is temporarily removed from the group." The guard
  (`:2753-2755`: allow JOIN when not JOINER, or negative code, or seqno >= join_gtid) is
  the mitigation this property stress-tests.
- **PXC-only early return**: `gcs.cpp:2724-2741` — `gcs_join` returns GCS_CLOSED_ERROR
  when conn->state >= CLOSED (evicted-mid-SST case): a JOIN that would have rebalanced the
  count can be legitimately dropped — the rebalancing message is not guaranteed delivered.
- **Resync failure swallowed**: `wsrep-lib/src/server_state.cpp:721-744`
  (`resume_and_resync`) — catch → `log_warning "Resume and resync failed, server may have
  to be restarted"` and returns. A failed resync (e.g. provider call fails during a
  concurrent view change) leaves the node desynced forever, silently serving reads. Same
  pattern in `wsrep_RSU_end` (`sql/wsrep_mysqld.cc:2938-2944`).
- **Two disagreeing desync accountings**: `SET GLOBAL wsrep_desync` calls
  `provider().desync()/resync()` directly in the *check* function
  (`sql/wsrep_var.cc:692-743`; the update fn `wsrep_desync_update` is a no-op `:745`),
  bypassing `wsrep::server_state::desync()/resync()` which maintain `desync_count_`
  (`wsrep-lib/src/server_state.cpp:1416-1447`, `include/wsrep/server_state.hpp:756`).
  FTWRL's `resume_and_resync` consults only the wsrep-lib counter (`desynced_on_pause_`) —
  an interleaved user desync + FTWRL can resync a node the operator wanted desynced, or
  under-count and leave it desynced. Three disagreeing bookkeepers total: gcs_group
  per-node count, wsrep-lib `desync_count_`, and the sysvar.
- **Donor keeps serving**: SYNCED→DONOR leaves `wsrep_ready=ON` and
  `wsrep_cluster_status=Primary` (`sql/wsrep_server_service.cc:383-388` fall-through,
  verified) — a leaked donor state is invisible to `wsrep_ready`-based health checks; only
  `wsrep_local_state`/`wsrep_local_state_comment` expose it (cross-link:
  `clustercheck-200-implies-write-progress`).
- Donor donation lifecycle: `replicator_str.cpp:508-625` (`process_state_req` — drains
  apply+commit monitors to donor_seq `:528-531`, S_DONOR `:533`, IST senders `:612-625`);
  `sql/wsrep_sst.cc:1628` donor thread blocks untimed on script completion.

## Failure scenario

Interleave: joiner triggers SST selection of node D; operator (workload) flips
`wsrep_desync=ON/OFF` on D around the same time; a view change lands between D's
`resync()` and `sst_sent()` JOIN messages so their seqnos arrive out of order — or the
JOIN is dropped at `gcs.cpp:2737-2740`, or `resume_and_resync` swallows a provider error
during FTWRL under churn. Result: `desync_count` stuck > 0 → D stays in Donor/Desynced,
exempt from flow control, falling behind, serving increasingly stale reads with
`wsrep_ready=ON`, until an operator removes it from the group.

## Suggested implementation

- **Workload-side (primary, `Always`)**: continuously sample `wsrep_local_state` per node.
  At quiesced checkpoints (SST/IST finished, no RSU active, workload desync toggles all
  paired OFF, settle time T elapsed): every node reports `wsrep_local_state = 4`,
  `wsrep_local_state_comment = 'Synced'`, `wsrep_desync = OFF`. Workload actions to drive
  the states: periodic `SET GLOBAL wsrep_desync=ON; ... OFF`, `FLUSH TABLES WITH READ
  LOCK; UNLOCK TABLES`, RSU-mode DDL, plus join events (from the restart property) to
  force real donations.
- **`Sometimes` markers** (workload + missing SUT instrumentation, distinct outcomes):
  - "donor completed IST donation and returned to SYNCED" / "donor completed SST donation
    and returned to SYNCED" (state 2 observed, later back at 4, under faults);
  - "resume_and_resync failure path entered" (`server_state.cpp:738-743`) — danger state
    the code only warns about;
  - "gcs_join out-of-order guard triggered" (`gcs.cpp:2753-2755`) — confirms the
    mitigation is exercised.
- **SUT-side `Always` (missing)**: at `gcs_group.cpp:1305`, `sender->desync_count > 0`
  before decrement — restores the compiled-out assert as a non-fatal Antithesis property
  in release builds; still the highest-value SDK line for catching the wrap at the moment
  it happens. **CORRECTED (investigation):** the gcs-group self count IS exposed as the
  `wsrep_desync_count` status variable (`gcs_group.cpp:2445-2467` →
  `replicator_smm_stats.cpp:434-470` dynamic status-map appendage) — the workload should
  assert `wsrep_desync_count == 0` at quiesced checkpoints directly. The wsrep-lib
  `desync_count_` remains unexposed.

## Fault requirements

Mostly fault-independent triggers (workload drives desync/FTWRL/RSU; joins drive
donation) — network faults (default-on) supply the view changes that race the JOIN
messages; CPU throttle on the donor widens JOIN/conf-change races. Node termination
enriches (SST donations, joiner killed mid-SST) but is not required for the desync-leak
core; the workload can restart the joiner itself.

## Open Questions

- RSU (`wsrep_OSU_method=RSU`) desync/resync under concurrent view change — same leak via
  `wsrep_RSU_end`'s swallowed failure? Worth a dedicated workload action. `(partial: the
  swallowed-failure pattern is confirmed in code (wsrep_mysqld.cc:2938-2944); whether a
  view change can actually make the provider resync call fail mid-RSU is untested — treat
  as a workload action to include, verified empirically)`

All other questions resolved (see Investigation Log). **Property improved by Q4's answer:**
the gcs-group desync counter IS externally visible as the `wsrep_desync_count` status
variable — the workload can assert `wsrep_desync_count == 0` at quiesced checkpoints
directly, a much sharper oracle than `wsrep_local_state` alone. The prior "(no desync
counter in provider stats)" note was wrong — it missed the dynamic status-map appendage.

### Investigation Log

#### Is the `gcs.cpp:2753` guard sufficient for ALL JOIN orderings?

- Examined: `gcs/src/gcs.cpp:2721-2770` (gcs_join + PXC GCS_CLOSED_ERROR early return),
  `gcs/src/gcs_group.cpp:1271-1410` (gcs_group_handle_join_msg decrement conditions),
  `:1960-2012` (desync_count increments in state-request handling).
- Found: the guard is send-side only — it ensures JOINs are sent even with out-of-order
  seqnos (except in JOINER state), fixing the specific resync-vs-sst_sent suppression the
  comment describes. Balance still requires every increment (:1991) to be paired with a JOIN
  *delivered while the sender's status is still DONOR* (:1293-1311). Lost-decrement paths
  remain: the PXC GCS_CLOSED_ERROR early return (gcs.cpp:2737-2740), JOINs delivered after a
  status change, and swallowed resync failures (wsrep-lib). In release builds a decrement at
  count 0 goes negative silently (assert :1306 compiled out) and `if (0 == desync_count)`
  never fires — the permanent-DONOR mode.
- Not found: any receive-side reconciliation beyond the per-view re-derivation (next entry).
- Conclusion: RESOLVED — the guard is narrow; the leak surface the property tests is still
  live in 8.4.10. The property covers the dropped-JOIN and swallowed-resync paths plus any
  ordering that lands a JOIN outside DONOR status.

#### Does a conf change reset per-node desync_count on surviving members?

- Examined: `gcs/src/gcs_node.cpp:225-280` (gcs_node_update_status, called from
  group_post_state_exchange via gcs_group.cpp:471), `gcs_group.cpp:2405-2415` (state msg
  carries the node's own desync_count).
- Found: at every conf change each node's count is re-derived: non-DONOR status →
  `desync_count = 0` (gcs_node.cpp:273-276); DONOR status → restored from the node's OWN
  state message (:240-247), which self-reports its own group bookkeeping. A leaked
  *self*-count therefore survives view changes (state msg carries the wrong value forward);
  only other nodes' stale views of a demoted node get cleaned.
- Conclusion: RESOLVED — view churn does not mask a self-leak on the stuck donor; the
  checker does not need special fault-free-window scheduling for that case (still good
  practice for the demotion corner).

#### Does `SET GLOBAL wsrep_desync=OFF` fail-but-read-OFF while donation holds the group desync?

- Examined: `sql/wsrep_var.cc:693-746` (wsrep_desync_check/update).
- Found: the provider call happens in the *check* function; on `resync()` failure the SET
  statement errors (ER_CANNOT_USER "'resync'") and the sysvar is NOT updated — it still
  reads ON. No silent fail-but-read-OFF exists. Same-value SETs are no-op warnings. Toggles
  are refused outright while the provider is paused (FTWRL) with ER_UNKNOWN_ERROR
  ("Explictly desync/resync of already desynced/paused node is prohibited", :716-723). If
  resync() *succeeds* while a concurrent donation still holds the group count, the sysvar
  legitimately reads OFF while `wsrep_local_state=2` until the donation's own JOIN lands —
  transient, not a leak.
- Conclusion: RESOLVED — workload must handle SET errors (retry) and must not treat
  transient OFF+state-2 during donation as a violation; assert sysvar-OFF ⇒ state-4 only at
  quiesced checkpoints (as the invariant already does).

#### Is `wsrep_local_state=2` the only externally visible signature of a leaked desync_count?

- Examined: `gcs/src/gcs_group.cpp:2445-2467` (gcs_group_get_status inserts
  "desync_count"), `gcs/src/gcs_core.cpp:1694-1700`, `gcs/src/gcs.cpp:2835-2845`,
  `galera/src/replicator_smm_stats.cpp:434-470` (gcs status map appended to the wsrep stats
  vars returned to the server).
- Found: the per-node (self) gcs_group desync count is exported end-to-end as a status
  variable (`wsrep_desync_count`) via the dynamic status-map appendage — the earlier partial
  note only checked the static stats array (:145-199) and missed this.
- Conclusion: RESOLVED — `wsrep_desync_count` is directly assertable workload-side; update
  the invariant to `wsrep_desync_count == 0 ∧ wsrep_local_state = 4` at quiesced
  checkpoints. The SUT-side restore-the-assert instrumentation remains a nice-to-have, no
  longer the only visibility into the counter.

## Synthesis refinement (2026-09-10)

GU_DBUG_SYNC note: galera's release-usable provider sync points can manufacture donor-pause/resync race preconditions for free (no Debug build); availability in the PXC-vendored build unverified — one runtime SET GLOBAL wsrep_provider_options answers it.
