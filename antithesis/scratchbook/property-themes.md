---
updated: 2026-10-05
purpose: Group the property catalog into themes that the Percona team cares about.
  Every triage report sorts its findings by these themes.
sources: property-catalog.md (status as of 2026-09-28), property-relationships.md,
  triage-scope.md, the assertion names in workload/pxcwl/oracles.py and
  pxc-node/entrypoint.sh
---

# Property Themes

Percona owns the replication stack: Galera, wsrep-lib, the wsrep layer in the
server, the `pxc_*` features, and the SST scripts. Every theme below is about
that code. Upstream MySQL defects are not a theme. Triage marks them "out of
scope" (see `triage-scope.md`).

## Status words

| Status | Meaning |
| --- | --- |
| **Tested** | A harness assertion checks it in every run. |
| **Partial** | An assertion checks part of it. The note says which part is missing. |
| **Build check** | No harness assertion. The Debug build has a native assert at the bad state, and the harness reports any mysqld death by site. |
| **Not yet** | Nothing checks it. The note says what blocks it. |

## The themes

### T1. Data agreement: nodes never silently diverge

All Synced nodes hold the same rows, schema and GTID history. Release builds
cannot see a successful-but-wrong apply, so this external check is the only
detector. Most data bugs end here.

| Property | Status | Note |
| --- | --- | --- |
| `cross-node-row-equality` | Tested | Terminal checksum on every replicated table |
| `gtid-executed-cluster-convergence` | Tested | |
| Schema agreement (tables, indexes, definitions) | Tested | |
| PK-less table agreement | Tested | Fenced window with `pxc_strict_mode` lowered |
| `autoinc-identity-no-cross-node-collision` | Tested | Through the terminal checksum |
| `bf-bf-lock-suppression-no-divergence` | Tested | Through the terminal checksum. Sees the end state, not the cause |
| `privilege-context-divergence-never-evicts` | Tested | Same |
| `applier-threads-never-read-only` | Tested | Same |
| `sr-fragment-cross-node-agreement`, `sr-rollback-fragment-noop` | Tested | Same. Streaming replication is on per session |
| `inconsistency-vote-evicts-divergent-minority` | Not yet | Needs a fenced variant that injects divergence on purpose |
| `evicted-node-rejoins-only-via-sst` | Not yet | Same |

### T2. Client contract: what the client was told is true

An acknowledged commit is on every node, a rejected write leaves no trace, and
a retried or replayed transaction applies once. This is the promise an
application builds on.

| Property | Status | Note |
| --- | --- | --- |
| `non-primary-rejects-writes` | Tested | Shared ack journal |
| `first-committer-wins-loser-leaves-no-trace` | Tested | |
| `bf-replay-commits-exactly-once` | Tested | Counter floor and ceiling checks |
| `retry-autocommit-exactly-once` | Tested | Same |
| `sync-wait-reads-observe-acked-writes` | Tested | Triage this first: the checksum uses `wsrep_sync_wait` as its barrier |
| `acked-commit-durable-across-restart` | Partial | The journal check does not care how a node went down. A run must still show that it saw a kill-then-rejoin |
| `nonready-node-error-code-contract` | Not yet | Known defect: an unready TOI returns ER 1213, not ER 1047. Counted as data, not asserted |
| `duplicate-gtid-skip-exactly-once` | Not yet | Needs an async source replicating into the cluster |

### T3. Conflict handling never kills a node

Certification, brute-force (BF) aborts, replay and replicated DDL (TOI/NBO)
are the densest Percona-only code. The recurring PXC bug class is a missed
certification key that turns into a node suicide.

| Property | Status | Note |
| --- | --- | --- |
| `no-mdl-bf-bf-abort` | Build check | |
| `trx-replay-never-fatal` | Build check | |
| `illegal-wsrep-transition-never-taken` | Build check | |
| `skip-locked-nowait-never-fatal` | Tested | |
| `toi-nbo-ddl-completes-or-fails-cleanly` | Partial | TOI half tested. NBO needs its own fenced phase |
| `bf-abort-skip-awake-victim-already-killed` | Not yet | Needs SDK instrumentation inside mysqld |

### T4. The cluster keeps moving: no stalls

Strict commit-order monitors and flow control mean one leaked slot can freeze
the whole cluster while every node still says it is healthy.

| Property | Status | Note |
| --- | --- | --- |
| `flow-control-pause-releases` | Tested | Commit-progress watchdog |
| `synced-node-recv-queue-bounded` | Tested | |
| `commit-order-monitor-released-no-cluster-stall` | Tested | As a stall symptom only |
| `local-monitor-freed-after-bf-abort` | Tested | Same |
| `monitor-window-overflow-unreachable` | Tested | Same |
| `graceful-shutdown-bounded` | Tested | |
| `notify-cmd-hang-does-not-block-commits` | Not yet | `wsrep_notify_cmd` is read-only; needs a config variant |
| `gcache-page-files-bounded` | Not yet | Needs file-size metrics from the node |

### T5. Membership and quorum: one cluster, one primary

Partitions heal to one primary, bootstrap never makes two clusters, and
group communication keeps total order.

| Property | Status | Note |
| --- | --- | --- |
| `at-most-one-primary-component` | Tested | |
| `partition-heal-single-primary-remerge` | Tested | |
| `full-cluster-restart-reaches-primary` | Tested | |
| `no-dual-bootstrap-after-full-shutdown` | Tested | Runtime form |
| `cluster-identity-single-lineage` | Partial | One-UUID check at the end of a run. Sequential forks over time are not checked |
| `cluster-address-live-set-rejoins` | Tested | |
| `gcs-total-order-gap-free` | Build check | |
| `vote-message-payload-contract`, `commit-cut-bounded-by-delivered-seqno` | Not yet | Need SDK instrumentation |

### T6. State transfer and rejoin (SST/IST)

A node that leaves comes back Synced, a donor recovers, and a failed or
interrupted transfer never leaves a node down or wrong. The SST scripts are
Percona-written shell, so script safety belongs here too.

| Property | Status | Note |
| --- | --- | --- |
| `restarted-node-rejoins-synced` | Tested | |
| `joiner-reaches-synced-after-state-transfer` | Tested | |
| `donor-returns-to-synced` | Tested | |
| `failed-state-transfer-node-rejoins` | Tested | |
| `ist-overlap-writesets-not-reapplied` | Tested | Through the terminal checksum |
| `sst-ready-message-implies-listener` | Partial | Only through the rejoin timeout |
| `interrupted-sst-forces-full-sst` | Not yet | Needs a check after a kill during SST |
| `gcache-recovered-ist-completeness` | Not yet | Same, kill on the donor |
| `sst-grant-all-user-locked-or-absent` | Not yet | Security: the SST superuser left unlocked after a kill |
| `cluster-member-strings-never-reach-shell` | Not yet | Security: peer strings into `/bin/sh`. Better as a CI test |

### T7. Crash recovery: a killed node restarts from the right place

After a hard kill, `grastate.dat`, the InnoDB XID checkpoint and gcache must
agree, or the node rejoins at the wrong position. Default launches now include
node kills, so this theme is the largest open opportunity.

| Property | Status | Note |
| --- | --- | --- |
| `gcache-crash-recovery-no-abort` | Partial | Any abort is caught by the mysqld death properties. Recovered content is not checked |
| `grastate-se-checkpoint-agreement` | Not yet | Needs a check at each node start |
| `wsrep-xid-checkpoint-monotonic` | Not yet | Same |
| `crash-recovery-grep-yields-true-position` | Not yet | Tests the shipped wrapper; the harness replaces it today |

### T8. Operator and health truthfulness

What a node reports (clustercheck, `pxc_maint_mode`, status variables) matches
what it does. Load balancers and operators act on these signals.

| Property | Status | Note |
| --- | --- | --- |
| `clustercheck-200-implies-write-progress` | Tested | |
| `maint-mode-honors-operator-intent` | Tested | Expected red: a known forced-revert defect |
| `applier-resize-converges` | Tested | |
| `desync-ftwrl-composition-resyncs` | Partial | Desync and backup-lock legs. FTWRL is blocked by `pxc_strict_mode=ENFORCING` |
| `ftwrl-backup-quiescent-or-fails` | Not yet | Same block |
| `fatal-node-terminates-no-zombie` | Build check | |

### T9. Upgrades, async replicas and encryption

Mixed-version clusters, an async source feeding the cluster, and encrypted
cluster traffic. Nothing tests these yet. Ask Percona which matter most.

| Property | Status | Note |
| --- | --- | --- |
| `no-spurious-multi-major-detection`, `homogeneous-cert-version-match` | Not yet | Need a release image and SDK instrumentation |
| `rolling-upgrade-write-gate` | Not yet | Needs a debug knob; cannot share a run with the property above |
| `async-monitor-leave-mismatch-unreachable` | Not yet | Needs an async source topology |
| Encryption phase (TLS reload, keyring with SST, encrypted gcache) | Not yet | Placeholder. See `properties/deferred-encryption-phase.md` |

## Triage: put every finding in one theme

Every triage report groups its findings under T1 to T9. Each finding carries
four labels:

1. **Theme**: T1 to T9, from the tables below.
2. **Owner**: Percona or upstream MySQL (`triage-scope.md`). Upstream findings
   go in an "Out of scope: upstream MySQL" list, not in a theme.
3. **Build tier**: `[prod]`, `[debug-only]` or `[prod?]` (`workload/README.md`,
   "Property labels").
4. **Cause class**: SUT, harness or undecidable (`triage-scope.md`). Harness
   findings go in a "Harness fixes" list, not in a theme.

A failing `[coverage]` claim is not a finding. Report it as a coverage gap
under its theme.

### Named assertions

| Assertion | Theme |
| --- | --- |
| `[prod] replicated table content is identical on every Synced node` | T1 |
| `[prod] replicated table definitions are identical on every Synced node` | T1 |
| `[prod] every Synced node agrees on the set of tables and indexes` | T1 |
| `[prod] primary-key-less table content is identical on every Synced node` | T1 |
| `[prod] gtid_executed is identical on every Synced node` | T1 |
| `[prod] workload observed a write key it never minted` | T1 (check first that it is not a harness defect) |
| `[prod] every acknowledged write is present on every Synced node` | T2 |
| `[prod] a cleanly failed write is absent from every Synced node` | T2 |
| `[prod] counter total is at least the acknowledged increment count` | T2 |
| `[prod] counter total never exceeds acknowledged plus unresolved increments` | T2 |
| `[prod] a sync-wait read on another node sees an acknowledged write` | T2 |
| `[prod] a locking read ends in a result set or a documented lock error` | T3 |
| `[prod] no data-definition statement is left unresolved after reconvergence` | T3 |
| `[prod] cluster commit progress never freezes while every node reports Synced` | T4 |
| `[prod] a Synced node keeps its receive queue below the flow-control bound` | T4 |
| `[prod] a graceful shutdown closes the port within the configured bound` | T4 |
| `[prod] at most one primary component exists at any observation` | T5 |
| `[prod] all nodes share one cluster state UUID at terminal convergence` | T5 |
| `[prod] the cluster returns to three Synced nodes after fault injection stops` | T6 |
| `[prod] a node advertising availability for a sustained window has committed a write in that window` | T8 |
| `[prod] an operator-set pxc_maint_mode=MAINTENANCE is never reverted to DISABLED` | T8 |
| `[prod] applier thread count reaches the configured setpoint after a resize` | T8 |

### mysqld deaths: sort by site

The death properties (`... mysqld assertion failed at <site>`, `... called
gu_abort after <cause>`, `... stopped itself after <cause>`, `... died on fatal
signal <N>`) have no fixed theme. Find the first Percona-owned frame or the log
subsystem (`triage-scope.md`, "follow the caller"), then use this table.

| Site or cause | Theme |
| --- | --- |
| `galera/src/certification*`, `wsrep_append_keys` and other cert-key builders, BF-abort and MDL BF-BF paths, `wsrep-lib` transaction and replay code, `wsrep_high_priority_service`, TOI/NBO | T3 |
| `galera/src/monitor*`, flow control (`gcs_fc*`), applier shutdown waits | T4 |
| `gcomm/`, `gcs/` group communication, `pc.*`, `evs.*`, `pc.wait_prim_timeout` | T5 |
| SST/IST code (`wsrep_sst*`, `ist*`, SST scripts), an inconsistency vote that a joining node cannot cast | T6 |
| `gcache/`, `saved_state`, `grastate.dat`, XID checkpoint recovery | T7 |
| `pxc_maint_mode`, `wsrep_*` status and readiness reporting | T8 |
| Version checks, protocol upgrade | T9 |

When a site fits two rows, choose the theme of the first Percona frame. When
nothing fits, write "unplaced" and say why. Do not guess.

### Coverage claims

| Claim | Theme |
| --- | --- |
| `a streaming-replication transaction committed with fragments` | T1 |
| `a write outcome was unknown after a connection failure` | T2 |
| `a client received ER 1047 from a node that was not ready` | T2 |
| `a non-Primary node rejected ROLLBACK of an open transaction` | T2 |
| `a certification conflict returned ER 1213 to a client` | T3 |
| `a SKIP LOCKED statement returned ER 1213` | T3 |
| `a data-definition statement completed under concurrent replicated writes` | T3 |
| `a data-definition statement dropped an object the catalog reported present` | T3 |
| `flow control was engaged by some node` | T4 |
| `a writeset larger than four megabytes was committed` | T4 |
| `a transaction of at least one hundred statements committed` | T4 |
| `the cluster was observed with fewer than three members and later returned to three` | T5 |
| `terminal verification bootstrapped a cluster that had lost its primary component` | T5 |
| `a state transfer was served to a joining node` | T6 |
| `a live node's error log recorded a failed state transfer` | T6 |
| `a failed state transfer fell back to IST instead of killing the node` | T6 |
| `a node died in a boot whose state transfer had failed` | T6 |
| `a live node's error log recorded an inconsistency verdict` | T1 |
| `a node died after the cluster declared it inconsistent` | T1 |
| `a node died before mysqld reached ready for connections` | T7 |
| `a node died in a way the shipped systemd unit would not restart` | T8 |
| `terminal verification completed a quiesced three-node comparison` | T1 |

A new assertion must get a row in one of these tables when it is added.

## Candidate findings so far

Earlier triage recorded these. Re-check each in a current run before you
present it as a bug.

| Theme | Finding | Where recorded |
| --- | --- | --- |
| T3 | The cert-key builder (`wsrep_append_keys` → `wsrep_innobase_mysql_sort`) passes an odd length into a collation function and trips its assert | `triage-scope.md`, run `2af893bc…-63-0` |
| T2 | An unready node returns ER 1213 for TOI instead of ER 1047 | `property-catalog.md`, `nonready-node-error-code-contract` |
| T6 | A node that gets an inconsistency vote while mid-IST assumes it is inconsistent and does not come back | `property-catalog.md`, run `93ec5045…-63-2` |
| T8 | clustercheck returns 200 while every write times out under flow control (up to 164 s) | `property-catalog.md`, runs `5a7b1d9f…-63-0` and `93ec5045…-63-2` |
| T8 | A non-primary view reverts an operator's `pxc_maint_mode=MAINTENANCE` | `property-catalog.md`, run `afeec3df…-63-0` |
| T1 | A node that rejoins after a non-Primary view writes every later GTID under the inverted cluster UUID: `wsrep_init_sidno` (`sql/wsrep_mysqld.cc:673`) runs while `wsrep_protocol_version` is still -1 | runs `1ef68127…-63-5`, `17eb4031…-63-5`, no-fault `d986f39c…-63-5` |
| T6 | A graceful stop of an SST donor aborts mysqld: `sst_donor_thread` calls `sst_sent` (`sql/wsrep_sst.cc:1543`) after the provider is unloaded, and the `provider not loaded` exception is not caught | run `1ef68127…-63-5` |
| T2 | A COMMIT whose outcome is unknown (the node goes non-Primary after send, before its own copy returns) reaches the client as ER 1213, yet the other component orders it and it lands on every node. `replicator_smm.cpp:824-842` → `wsrep-lib/src/transaction.cpp:1959-1984` → `sql/handler.cc:2581-2597` | run `17eb4031…-63-5` (wid 50331649, vtime 84-98) |
| T1 | A streaming-replication transaction is lost on its origin node: COMMIT returns 1213 while the node goes non-Primary, the commit fragment (seqno 615) is ordered on the other two nodes, and the origin applies it by IST without the row. Mechanism past the 1213 is inferred, not proven | run `17eb4031…-63-5` (wl_witness wid 134217729, vtime 87-113) |
