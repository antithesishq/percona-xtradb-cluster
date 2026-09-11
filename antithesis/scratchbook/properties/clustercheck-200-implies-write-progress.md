# clustercheck-200-implies-write-progress

**Property:** A node that the shipped health-check surface advertises as available
(clustercheck HTTP 200, and equivalently `wsrep_ready=ON` + `wsrep_cluster_status=Primary` +
`wsrep_local_state` in {4, or 2 with AVAILABLE_WHEN_DONOR=1} + `pxc_maint_mode=DISABLED`)
can actually complete a trivial committed write within a bound.

**Confidence:** High that the *mechanism gap* exists (verified in code, three independent
ways a 200-advertising node cannot commit). Medium on the exact bound to use.

## Why this is wildcard territory

Every other focus tests whether the SUT *is* correct. This tests whether the SUT *tells the
truth about itself* — the property load-balancers, operators, and k8s probes actually depend
on. None of the health checkers validate the ability to commit; three verified mechanisms
let a node advertise healthy while every write hangs indefinitely.

## Code evidence (verified at commit f9ecb3e)

1. **clustercheck formula** — `scripts/clustercheck.sh:83-93`: returns 200 iff
   `wsrep_cluster_status=='Primary' && (wsrep_local_state==4 || (==2 && AVAILABLE_WHEN_DONOR==1)) && pxc_maint_mode=='DISABLED'`.
   It never looks at `wsrep_ready`, `wsrep_reject_queries`, flow-control state, recv-queue
   depth, or `innodb_disallow_writes`. (Read-only check exists but only when
   `AVAILABLE_WHEN_READONLY=0`, `:95-113`.)
2. **DONOR keeps wsrep_ready=ON and status Primary** — verified fall-through in
   `Wsrep_server_service::log_state_change`, `sql/wsrep_server_service.cc:349-386`:
   `wsrep_ready=true` is set only in the `s_synced` case, and entering `s_donor` never
   clears it; `s_synced`/`s_joined`/`s_donor` all fall through to
   `wsrep_cluster_status="Primary"`.
3. **Frozen InnoDB writes are invisible to the checker** — `WAIT_ALLOW_WRITES()` macro,
   `storage/innobase/os/os0file.cc:234-237`: `os_event_wait(srv_allow_writes_event)` with
   **no timeout**, sprinkled across InnoDB file-write paths (`:1618,:3191,:3247,:3314,...`).
   Set during xtrabackup SST donation (`sql/wsrep_sst.cc` sst_disallow_writes path); the
   variable is a deprecated plain global any SUPER session can flip
   (`ha_innodb.cc:25682`, warning only). A donor with AVAILABLE_WHEN_DONOR=1 is state 2 →
   200 → every write on it hangs forever.
4. **FC pause is invisible** — a Synced (state 4) node whose commits are blocked by
   cluster-wide flow-control pause (one wedged applier anywhere, PXC-4844/MDEV-38843
   family; `gcs.cpp` stop/cont hysteresis) still returns 200.
5. Same class, opposite sign: `pyclustercheck.py.in` is Python 2 with a string-vs-int
   donor-state comparison — flaps 200/503 (sut-analysis focus 12 §10); the reverse
   direction (healthy node advertised down) matters for "operator drains all nodes"
   scenarios but is secondary here.

## Failure scenario

Three-node cluster behind a proxy keyed on clustercheck. Node A donates SST (or an operator
sets `wsrep_desync=ON`, also state 2), or a network hang wedges one applier so FC pauses
the cluster. All nodes keep returning 200. The proxy keeps routing writes; every client
write hangs until client-side timeout; application-level outage with all backends "green".
In the frozen-InnoDB variant the node holds the writes forever (untimed event wait).

## Testable formulation (workload-side, no SUT instrumentation needed)

For each node, the workload continuously: (a) computes the clustercheck predicate from the
same three SHOW statements the script runs (or curls the script on 9200), and (b) attempts
`INSERT INTO health_probe ... ; COMMIT` with a client-side timeout T.

- `Always`: "a node that has advertised available continuously for the last T seconds has
  completed at least one committed write in that window." Rationale for `Always`: the
  guarantee is a per-evaluation invariant on the advertised-healthy state — every time we
  observe a sustained 200 window, commit ability must hold; a single violation is the bug.
  The T-window converts the underlying bounded-liveness into a safety check and absorbs
  transient FC pauses (pick T well above `evs.suspect_timeout`+FC hysteresis; e.g. 60-120s).
- `Sometimes`: "a node was observed in state 2 (donor/desynced) while serving the
  health-check" — confirms the interesting sub-state was actually explored.
- Run one variant with AVAILABLE_WHEN_DONOR=1 (the documented proxy config the property
  attacks) and one with 0.

## Instrumentation suggestions (all missing)

- Workload: health-probe writer + clustercheck-predicate poller per node (described above).
- Optional SUT-side `Sometimes` in `log_state_change` when entering `s_donor` with
  `wsrep_ready` still true (`sql/wsrep_server_service.cc:384-386`) — marks the divergent
  window for exploration.

## Fault requirements

Default-on network faults/hangs suffice: a hang on one node's applier induces FC pause; a
partition during SST donation extends the donor window. `wsrep_desync=ON` and SST can be
driven entirely from the workload (start a joiner / set desync). No node termination or
clock faults needed.

## Open questions

- What T makes the property tight but not flaky under legitimate long FC pauses (large
  writesets)? `(partial: lower bound = FC hysteresis + evs.suspect_timeout; final value
  needs one measurement in the first triage round)`

Resolved (see Investigation Log):

- `wsrep_local_state` **stays 4 during an FC pause** — the FC leg is at full strength.
- Read-only leg: decided **skip initially** — no in-tree writer flips `read_only` on a
  serving node, so the leg has zero expected yield unless the workload sets it; probes on a
  read_only node fail fast (error), they don't hang. Revisit only if runs show
  `read_only=ON` unbidden.

### Investigation Log

#### Does `wsrep_local_state` stay 4 during an FC pause?

- Examined: `percona-xtradb-cluster-galera/galera/src/replicator_smm_stats.cpp` (status
  export, `state2stats` :9-25, export :365-371), all `state_.shift_to(...)` sites in
  `galera/src/replicator_smm.cpp`, FC handling in `gcs/src/gcs.cpp` (fc paths around
  :550,:633,:694-710).
- Found: `wsrep_local_state` is `state2stats(state_())` — a pure function of the
  ReplicatorSMM state machine (`S_SYNCED` → `WSREP_MEMBER_SYNCED` = 4). Every `shift_to`
  site is a connection/join/donor/sync event; none is in a flow-control path. gcs FC only
  *reads* `conn->state` (`conn->state <= conn->max_fc_state` gates) and never mutates it;
  FC surfaces exclusively via the `fc_*` stats (`replicator_smm_stats.cpp:313-332`,
  `wsrep_flow_control_status`, `fc_active/fc_requested`).
- Not found: any state demotion on FC stop/cont.
- Conclusion: resolved — state stays 4 (Synced) during FC pause; a fully FC-frozen node
  keeps returning 200. The FC leg of the property is at full strength.

#### What T makes the property tight but not flaky?

- Examined: same FC code; no static answer exists — legitimate pause length depends on
  workload writeset size and fc_limit hysteresis.
- Conclusion: tagged `(partial)` — needs one empirical measurement in the first triage
  round; start at 60-120s per the testable formulation.

#### Should the harness also assert on the read_only leg (AVAILABLE_WHEN_READONLY)?

- Examined: `scripts/clustercheck.sh` (both arg-parse branches default
  `AVAILABLE_WHEN_READONLY=1`; read-only check only when set to 0, and it checks
  `read_only` only); tree-wide grep for writers of `opt_readonly`/`opt_super_readonly`
  (`sql/mysqld.cc:1296-1297,9864,14996` — sys_var plumbing/startup only);
  `scripts/wsrep_sst_common.sh:847` (`--read_only=OFF` — applies to the *temporary
  upgrade mysqld* spawned during SST, a separate process, not the serving node).
- Found: with the default (1), a read_only node advertises 200 — but a write probe on it
  fails **fast** with an error (ER_OPTION_PREVENTS_STATEMENT class), it does not hang, so
  this leg is a different failure shape from the three hang mechanisms. Checking
  `read_only` alone is sufficient when the check is enabled: `super_read_only=ON` forces
  `read_only=ON`.
- Not found: any in-tree code path that spontaneously sets `read_only`/`super_read_only`
  on a serving node. The PXC-4849/PXC-5229 "appliers inherit read-only" claim could not be
  validated against this tree (issues not vendored) — per validating-claims it stays out
  of the property premise.
- Conclusion: resolved as a scoping decision — do not assert the read-only leg initially;
  the workload should simply never set `read_only` (or exclude windows where it does). Add
  a sub-property only if triage shows read_only=ON without workload action.
