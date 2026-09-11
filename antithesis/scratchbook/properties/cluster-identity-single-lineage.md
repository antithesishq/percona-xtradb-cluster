# cluster-identity-single-lineage

**Property:** Exactly one cluster identity ever exists: after the initial deliberate
bootstrap, every node that reaches `wsrep_ready=ON` reports the same
`wsrep_cluster_state_uuid` as the original lineage, and no plain (non-bootstrap) start ever
mints a new cluster UUID — regardless of prior interrupted bootstrap attempts, crashes, or
partitions.

**Confidence:** High on the poisoning mechanism (verified in the shipped script). High on
the oracle being implementable (single SQL status variable). Medium on whether default-on
faults alone can interrupt the bootstrap script at the right instant — the harness can also
drive it.

## Why this is wildcard territory

Focus 7 (distributed coordination) will test split-brain as *two simultaneous primary
components* via `wsrep_cluster_status`. This property is different on two axes: (a) the
violation vector is **environment poisoning of the supervisor**, not quorum logic — the
node is *told* to bootstrap by leftover state from a previous, unrelated operator action;
and (b) the oracle is **lineage (UUID) based**, which also catches the *sequential*
variant: a node quietly re-founding a cluster and later confusing joiners/SST — invisible
to instantaneous two-primaries checks and to any single node's own view.

## Code evidence (verified at commit f9ecb3e)

1. **The poisoning window** — `scripts/systemd/mysqld_bootstrap.in:30-39`:
   `systemctl set-environment MYSQLD_OPTS="--wsrep-new-cluster"` → `systemctl start ...` →
   `systemctl unset-environment MYSQLD_OPTS`. `systemctl set-environment` mutates the
   **systemd manager's global environment**, persistent for the manager's lifetime and
   applied to *every future start of the unit*. `systemctl start` blocks for the whole
   (potentially long: quorum wait, SST) startup or its job timeout; the script has no trap.
   Interrupt it (operator ^C, ssh drop, timeout, host issue) between set and unset and the
   flag persists: **every subsequent ordinary `systemctl start` is a bootstrap**.
   Same pattern with the same no-trap shape for `MYSQLD_RECOVER_START`
   (`scripts/systemd/mysqld_pre_systemd.in:133-139`, cleared only by `--post`).
   Debian variant: `build-ps/debian/extra/mysql.bootstrap:4` (EXTRA_ARGS file-based).
2. **What --wsrep-new-cluster does downstream** — bootstrap connect honors
   `safe_to_bootstrap` (`galera/src/replicator_smm.cpp:412-420`): with grastate
   `safe_to_bootstrap: 1` (which is set on *every* singleton primary view, `:3245`) or an
   absent/blank grastate, the poisoned start founds a **new cluster with a new state UUID**.
   Two poisoned/singleton nodes → two lineages, both Primary, both size ≥1: clients write
   to both; the datasets can never be merged (`doc: failover.rst:88-94` "impossible to
   re-merge").
3. **Lineage observable** — `wsrep_cluster_state_uuid` status var (group state UUID);
   grastate.dat `uuid:` field on disk; both change only when a new cluster is founded or a
   node joins a different lineage.
4. Adjacent same-class vector kept in scope by the same oracle: the two-singleton
   `safe_to_bootstrap` double-bootstrap after full crash (focus 7 will likely test the
   quorum half; the UUID oracle catches the outcome no matter the vector), and
   `pc.bootstrap=true` via provider options (workload must not issue it in this variant, or
   issue it deliberately on exactly one node).

## Failure scenario

Harness (or operator) bootstraps node 1; the bootstrap wrapper is interrupted mid-`start`
by a fault. Later, node 1 restarts "normally" — but the supervisor environment still
carries `--wsrep-new-cluster`. Node 1 happens to hold `safe_to_bootstrap: 1` (it was last
partitioned into a singleton before its crash). It founds lineage B while nodes 2 and 3
continue lineage A. The proxy sees all nodes healthy; writes fork permanently. No quorum
rule was violated at any single instant from any node's local view.

## Testable formulation

Precondition: the harness supervises mysqld through a wrapper that faithfully reproduces
the set-env → start → unset-env bootstrap dance (or uses systemd in-container). If the
harness passes `--wsrep-new-cluster` only via a one-shot explicit invocation, this property
degrades to the safe_to_bootstrap-only variant — still worth running with the UUID oracle.

- `Always` (main): "the multiset of `wsrep_cluster_state_uuid` values across all nodes with
  `wsrep_ready=ON` has exactly one distinct element, equal to the UUID recorded at initial
  bootstrap." Evaluated continuously by the workload (it records the lineage UUID once
  after first bootstrap). `Always` because any second lineage at any instant is
  unrecoverable data forking — the strongest possible violation semantics.
- `Always` (vector-specific): "outside an explicitly requested bootstrap, the supervisor
  environment contains no `--wsrep-new-cluster`" — checked by the wrapper before every
  start (workload-visible file/env dump). Catches the poisoning *before* it converts to a
  fork, giving a much shorter repro.
- `Sometimes`: "a bootstrap wrapper invocation was interrupted after set-environment and
  before unset-environment" — the test composer intentionally injects this interruption;
  the assertion confirms the window was explored.

## Instrumentation suggestions (all missing)

- Workload lineage tracker: poll `SHOW STATUS LIKE 'wsrep_cluster_state_uuid'` +
  `wsrep_ready` on all nodes; assert single-lineage.
- Wrapper-level env audit before each start (one `grep` in the entrypoint, exported to the
  workload).
- Test-composer action: "interrupt bootstrap wrapper mid-start" (SIGKILL the wrapper — not
  mysqld — between set and unset), then later issue a plain start.

## Fault requirements

The interruption itself is most reliably produced by the test composer (kill the wrapper
script), which needs no platform fault at all. Node restarts are needed to convert
poisoning into a fork — same flag as crash-recovery-grep-yields-true-position: either
enable node-termination faults or drive kill/restart from the workload. Default-on network
partitions supply the singleton views that set `safe_to_bootstrap: 1`.

## IMPORTANT correction (2026-09-10 investigation): the cited poisoning script is not shipped

`scripts/systemd/mysqld_bootstrap.in` (the `systemctl set-environment MYSQLD_OPTS` dance
at :30-39) has **zero build references** — it is not in `scripts/CMakeLists.txt` at all,
and the whole `scripts/systemd/` family is only processed under `IF(WITH_SYSTEMD)`
(`scripts/CMakeLists.txt:574`), which the PXC spec forces OFF (`-DWITH_SYSTEMD=OFF` at
`build-ps/percona-xtradb-cluster.spec:843/:898/:961`) and debian rules never enables.
The SHIPPED bootstrap mechanism on both RPM and Debian is `mysql@bootstrap.service` +
EnvironmentFile (`/etc/sysconfig/mysql.bootstrap` resp. Debian equivalent) containing
`EXTRA_ARGS=" --wsrep-new-cluster "` (`build-ps/rpm/mysql.bootstrap:4`,
`build-ps/debian/extra/mysql.bootstrap:4`), consumed by
`ExecStart=/usr/sbin/mysqld $EXTRA_ARGS $_WSREP_START_POSITION`
(`build-ps/rpm/mysql@.service:96`). This vector is *persistent by design* — no
interruption needed: an enabled (or habitually reused) `mysql@bootstrap` unit re-bootstraps
on every start including reboot, which the unit's own comments concede
(`build-ps/rpm/mysql@.service:22-24`: "you may not want to enable mysql@bootstrap ...
a bootstrapped mysqld coming up on reboot"). The property's oracle (lineage UUID) is
unchanged; the vector-specific `Always` should audit `$EXTRA_ARGS`/the EnvironmentFile
(and any harness-equivalent flag file) before ordinary starts, and the `Sometimes`
"wrapper interrupted between set-env and unset-env" reframes to "a bootstrap-flagged start
was issued when the workload did not intend a bootstrap". The interrupted-`systemctl
set-environment` shape remains real for the shipped units' `_WSREP_START_POSITION`
(manager-env, set by ExecStartPre) but that variable carries a position, not a bootstrap
flag, and is unset at the start of every ExecStartPre chain.

## Open questions

- Does the harness run systemd (or an equivalent persistent supervisor env / persistent
  EnvironmentFile) in-container? If supervision is a plain shell loop, the shipped
  `EXTRA_ARGS` EnvironmentFile vector must be emulated (a flag file the entrypoint
  consumes) to stay faithful; decide at harness build. What changes: without persistent
  supervisor state there is no poisoning to test and the property narrows to the
  safe_to_bootstrap vector. `(needs human input)`

### Investigation Log

#### Does the argv filter (`wsrep_mysqld.cc:1425`) neutralize env-supplied bootstrap flags?

Investigated 2026-09-10.

- Examined: `sql/wsrep_mysqld.cc:1419-1467` (`wsrep_filter_new_cluster`, flag definition
  and consumption), `sql/mysqld.cc:1801-1847, 8873-8877, 10071-10088` (sole call site),
  `sql/wsrep_var.cc:550-580`, shipped unit files.
- Found: the filter is the flag's *implementation*, not a neutralizer —
  `--wsrep-new-cluster` is not a registered my_getopt option, so `mysqld_main` lifts it
  out of argv into the process-global `wsrep_new_cluster` before `load_defaults()` and
  before the `orig_argc/orig_argv` snapshot (so in-process `RESTART` does not silently
  re-bootstrap). The global is re-initialized false and re-scanned on EVERY fresh exec:
  a supervisor that passes the flag on every start bootstraps on every start. The
  `wsrep_new_cluster = false` one-shot at `:1467` is within-process only. Extra sticky
  case found: if `wsrep_cluster_address` is empty at boot, `wsrep_start_replication`
  returns before clearing the flag, so a later `SET GLOBAL wsrep_cluster_address` will
  bootstrap (`wsrep_mysqld.cc:1461-1467`, `wsrep_var.cc:568`).
- Also found (vector correction): `MYSQLD_OPTS` appears only in unshipped files
  (`scripts/systemd/mysqld.service.in:60`, `packaging/deb-in/...service.in:32`); shipped
  units use `$EXTRA_ARGS` (EnvironmentFile, persistent on disk) — see the correction
  section above.
- Conclusion: RESOLVED — the filter does NOT defuse env/argv-supplied bootstrap flags on
  supervisor restarts; the poisoning-to-fork conversion is real. The vector shape changes
  from "interrupted set-env/unset-env window" to "persistent EnvironmentFile / bootstrap
  unit reused outside an intended bootstrap", which is easier to reach (no interruption
  timing needed).

#### Where does the workload persist the recorded original UUID across its own restarts?

Investigated 2026-09-10.

- Examined: nature of the question (workload implementation choice, not a SUT fact);
  testable-formulation section above.
- Found: nothing in the SUT constrains this; any location durable across workload
  restarts works (file on the workload container's own volume, written once at first
  bootstrap, read-only thereafter). The assertion must compare against this recorded
  UUID rather than instantaneous cross-node agreement (sequential forks are invisible to
  agreement checks) — already stated in the testable formulation.
- Conclusion: RESOLVED as an implementation directive — the workload records
  `wsrep_cluster_state_uuid` after the initial deliberate bootstrap into a file on its own
  persistent volume and treats it as the lineage anchor. No remaining factual
  uncertainty; carried as a workload-build requirement, not an open question.

#### Does the harness have a persistent supervisor environment?

Investigated 2026-09-10.

- Examined: shipped supervision mechanisms (see correction section): systemd units +
  EnvironmentFile on both RPM and Debian; no shell-loop supervisor ships.
- Found: the faithful-emulation target is now precisely known (EnvironmentFile with
  `EXTRA_ARGS=" --wsrep-new-cluster "`, plus manager-env `_WSREP_START_POSITION`), but
  whether the Antithesis harness runs systemd in-container or emulates it is an
  environment-design decision that code cannot answer.
- Conclusion: `(needs human input)` — environment/harness owner must decide; the property
  degrades gracefully to the safe_to_bootstrap vector either way.

## Synthesis refinement (2026-09-10)

DEFERRED: the v1 marker-file supervisor (first-boot bootstrap guard, no persistent env) removes the persistent-EnvironmentFile attack surface — the env clause is vacuous and the property is permanently green in v1. Revive under the field-faithful supervisor variant; the residual safe_to_bootstrap vector is covered by no-dual-bootstrap-after-full-shutdown's restated form.
