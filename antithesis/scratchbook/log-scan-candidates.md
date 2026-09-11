---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
external_references:
  - path: https://docs.percona.com/percona-xtradb-cluster/8.4/
    why: Upstream product documentation (user-approved scope: repo + upstream docs)
  - path: https://galeracluster.com/library/documentation/
    why: Galera replication library documentation (user-approved scope: repo + upstream docs)
  - path: https://dev.mysql.com/doc/refman/8.4/en/
    why: MySQL 8.4 reference documentation (user-approved scope: repo + upstream docs)
---

# Log-scan candidates — the mtr_warnings.sql Galera denylist mined into v1 assertions

Source: the Galera suppression block of
`mysql-test/include/mtr_warnings.sql:371-455` — every message Percona's own CI
deliberately cannot see (sut-analysis §9.5: "ready-made Antithesis assertion
candidates"). Consumed by the **shared log-string scan layer** (catalog Shared
conventions, instrumentation strategy): supervisor log-tail for dead/booting nodes,
`performance_schema.error_log` over SQL for live nodes.

Classification: **scan-as-finding** = raise a finding when matched (some entries only
outside a stated window — the scanner needs the supervisor's boot-phase markers and the
workload's phase/epoch ledger to evaluate windows); **expected-under-faults** = do not
alert; annotate the run (several carry a rate escalation note). The fenced
sabotage/poison phases (catalog poison-budget convention) suppress the apply-error
findings for their excluded tables only.

## Scan-as-finding (18 entries)

| # | Pattern(s) | Window condition | Why a finding / property tie |
|---|---|---|---|
| 1 | `Query apply failed` | Always, except fenced sabotage phases | An apply error on one node = divergence evidence and a vote trigger; ties to the un-injected-vote rule (`cross-node-row-equality`), `inconsistency-vote-evicts-divergent-minority`. |
| 2 | `Query apply warning:` | Always, except fenced sabotage phases | Softer sibling of #1 — per-node apply anomaly with no client-visible error. |
| 3 | `Ignoring error for TO isolated action:` | Outside fenced DDL-error/privilege-sweep phases | A TOI DDL failed on THIS node but not cluster-wide → per-node schema divergence; feeds `privilege-context-divergence-never-evicts`. |
| 4 | `Replica SQL: Error 'Duplicate entry` | Always, except fenced sabotage phases | Applier duplicate-key = the classic silent-divergence-turned-loud signature. |
| 5 | `Gap in state sequence. Need state transfer.` | Outside join/rejoin windows | A Synced node discovering a seqno gap outside any join = lost writesets (`gcs-total-order-gap-free`'s v1 log fallback, `gcache-recovered-ist-completeness`). |
| 6 | `Ignoring possible split-brain (allowed by configuration) from view` | Always | The harness never sets `pc.ignore_sb` — this line firing means config drift or a pc-layer bug; ties to `at-most-one-primary-component`. |
| 7 | `SQL statement.*was not replicated`; `SQL statement was ineffective` | Outside non-primary windows and fenced wsrep-off phases | A statement silently not replicated on a Primary node = replication skip. |
| 8 | `No existing UUID has been found.*Generating a new UUID` | First boot of a node only | On an established datadir = identity loss / datadir reset (`cluster-identity-single-lineage` residual). |
| 9 | `Could not open saved state file for reading:`; `Could not open state file for reading:`; `No persistent state found. Bootstraping with default state`; both `gvwstate.dat` missing-file patterns | First boot only | grastate/gvwstate vanished on an established datadir = state-file loss; feeds `grastate-se-checkpoint-agreement`. |
| 10 | `Failed to prepare for incremental state transfer: Local state UUID (00000000-...) does not match group state UUID` | First join of a node only | Zeroed local UUID after the node has ever been Synced = lost identity → surprise SST. |
| 11 | `Failed to prepare for incremental state transfer: Local state seqno is undefined:` | Only after an UNGRACEFUL stop (kill channel / crash) | After a graceful stop, seqno -1 means the shutdown was not clean — the exact failure `graceful-shutdown-bounded` guards (stop-grace SIGKILL hazard). |
| 12 | `Node is not a cluster node. Disabling pxc_strict_mode`; `pxc_strict_mode can be changed only if node is cluster-node` | Outside `--wsrep-recover` probe boots | A running cluster node deciding it is not one = unexpected mode transition. |
| 13 | `Quorum: No node with complete state` | Outside orchestrated all-down/full-restart scenarios | No possible donor in a formed cluster = state-bookkeeping loss; feeds `full-cluster-restart-reaches-primary` triage. |
| 14 | `Trying to access missing tablespace.*`; `Allocated tablespace ID .*, old maximum was.*` | Only in crash-recovery boots (after kill) | On a graceful-restart boot = InnoDB metadata damage without a crash. |
| 15 | `Replica I/O.*: Get master clock failed with error:.*` | Always (v1) | v1 has no async replication channel — any async-replica I/O error is unexplained (revisit when the async-source topology lands). |
| 16 | `InnoDB: Error: Table "mysql"."innodb_table_stats" not found` | Always (low severity) | Upgrade-era artifact; should not occur on fresh 8.4 datadirs. |
| 17 | `wsrep_sst_receive_address is set to '127.0.0.1`; `Failed to guess base node address`; `Guessing address for incoming client connections failed` | Always (config guard) | The harness sets every address explicitly (static IPs) — any guess/localhost fallback means the address config regressed (gmcast one-shot-DNS hazard class). |
| 18 | `Toggling wsrep_on to OFF/ON will affect sql_log_bin` (both directions) | Outside phases that deliberately toggle `wsrep_on` | The baseline workload never toggles `wsrep_on`; unexpected toggling = escaped workload logic or injected SQL (low severity). |

## Expected-under-faults / not-applicable (25 entries — annotate, do not alert)

| # | Pattern(s) | Annotation |
|---|---|---|
| 1 | ` *down context*` | gcomm teardown noise during view changes/partitions. |
| 2 | ` Failed to send state UUID:*` | Transient send failure under network faults. |
| 3 | `gcs_caused() returned -107/-57/-1` (3 patterns) | Transport disconnect family under partitions. |
| 4 | `Transport endpoint is not connected`; `Socket is not connected` | Same family, raw form. |
| 5 | `Could not find peer` | gmcast membership churn. |
| 6 | `discarding established (time wait)` | gmcast connection churn. |
| 7 | `Action message in non-primary configuration from member`; `SYNC message from member`; `JOIN message from member .* in non-primary configuration`; `Last Applied Action message in non-primary configuration from member` | Stale messages straddling a view change — expected around partitions. |
| 8 | `install timer expired` | EVS install timeout under partition. **Rate-escalate**: persistent repetition in a healed network = membership livelock (`partition-heal-single-primary-remerge`). |
| 9 | `sending install message failed: Resource temporarily unavailable` | EVS under partition. |
| 10 | `no nodes coming from prim view, prim not possible` | Expected while no primary exists; the liveness verdict belongs to `full-cluster-restart-reaches-primary` / `partition-heal`, not the scanner. |
| 11 | `last inactive check more than` | Timer starvation under CPU/network faults. **Rate-escalate**: on an unfaulted node it indicates internal stall. |
| 12 | `Failed to report last committed` | Transient commit-cut report failure. **Rate-escalate**: persistent = commit-cut starvation (`commit-cut-bounded-by-delivered-seqno` context). |
| 13 | `but it is impossible to select State Transfer donor: Resource temporarily unavailable` | All donors busy/desynced — expected under join storms; liveness owned by `failed-state-transfer-node-rejoins` timeout. |
| 14 | `is not in state transfer` | SST state-machine race on interrupted transfers. **Rate-escalate**: feeds `interrupted-sst-forces-full-sst` triage. |
| 15 | `Peer (IST receiver).*Terminating IST AsyncSender` | Donor notices joiner death mid-IST — "entirely timing-driven, harmless" per the in-tree comment; expected under the kill channel. |
| 16 | `Initial position was provided by configuration or SST` (both variants) | Normal join/SST flow. |
| 17 | `Refusing exit for the last slave thread` | Applier shutdown-ordering edge during stop. |
| 18 | `binlog cache not empty (0 bytes) at connection close` | Known benign noise. |
| 19 | `Warning: Using a password on the command line interface can be insecure` | SST script invocation noise. |
| 20 | `InnoDB: Resizing redo log from`; `Starting to delete and rewrite log files`; `New log files created, LSN=` | Startup/redo-resize housekeeping. |
| 21 | `InnoDB High Priority being used` | Normal BF applier operation. |
| 22 | `Table without explict primary key (not-recommended) and certification of nonPK table is OFF too` | Expected ONLY in the strict-mode-lowered PK-less workload phase; outside that phase, treat as a finding (escaped PK-less DDL). |
| 23 | `--wsrep-causal-reads` deprecation trio (3 patterns) | n.a. — the workload uses `wsrep_sync_wait`, never `causal_reads`. |
| 24 | `IP address '127.0.0.2' could not be resolved` | n.a. — MTR localhost topology only. |
| 25 | `Percona-XtraDB-Cluster prohibits setting binlog_format to STATEMENT or MIXED` | Expected only if a probe phase deliberately attempts it; otherwise n.a. |

## Gap-fill additions (5 patterns — DDL/operational discovery round)

Provenance: **evaluation gap-fill** (Gaps 2/3/4 — `toi-nbo-ddl-completes-or-fails-cleanly`,
`ftwrl-backup-quiescent-or-fails`, `desync-ftwrl-composition-resyncs`,
`skip-locked-nowait-never-fatal`). Not from the mtr_warnings.sql denylist — these come
from the pause/desync/TOI/lock-conversion code paths read at f9ecb3e.

| # | Pattern(s) | Classification / window | Why / property tie |
|---|---|---|---|
| G1 | `GCS desync returned seqno` | Expected-under-workload (backup/desync actor phases); annotate | The non-consecutive pause-seqno warn-only path in `try_desync_and_pause` (`replicator_smm.cpp:3466-3476`) — the precondition of the FTWRL local-monitor gap wedge. Doubles as a `Sometimes` coverage marker for `desync-ftwrl-composition-resyncs`. **Rate-escalate**: repeated hits with a wedged FTWRL = the hang shape. |
| G2 | `Resume and resync failed` | Scan-as-finding, always | `resume_and_resync` swallows this failure (`server_state.cpp:721-742`) and UNLOCK returns success on a permanently desynced node — the line is the only evidence (`desync-ftwrl-composition-resyncs`, `donor-returns-to-synced`). |
| G3 | `Server pausing failed`; `Server paused at` | `Server pausing failed`: scan-as-finding outside backup/desync actor phases (inside them it accompanies the legal ER_QUERY_INTERRUPTED branch — annotate). `Server paused at`: annotate | Pause-or-fail contract of FTWRL (`ftwrl-backup-quiescent-or-fails`); `Server paused at <seqno>` marks the window where grastate carries a real mid-run seqno — the kill-recipe marker for `grastate-se-checkpoint-agreement`. |
| G4 | `Node will be left in inconsistent state` | Scan-as-finding, except fenced sabotage phases | The failed-TOI cleanup path heading into `unireg_abort` (`wsrep_mysqld.cc:2428/:2463`) — a DDL episode terminating in node suicide rather than a clean client error (`toi-nbo-ddl-completes-or-fails-cleanly`). |
| G5 | `Unknown error code` (InnoDB fatal, `row0mysql.cc:1224-1226`) | Scan-as-finding, always | The terminal signature of an unconverted wsrep BF-wait error code escaping to the row layer — the crash arm of `skip-locked-nowait-never-fatal` (PXC-5099 residual surface); pre-registered expectation on the assert image. |

## Notes for the scanner implementation

- Window evaluation needs two inputs already specified elsewhere: the supervisor's
  **boot-phase markers** (first-boot / recover-pass / crash-vs-graceful classification —
  topology supervisor extensions, plus the restart-accounting JSONL) and the workload's
  **phase/epoch ledger** (fenced sabotage phases, PK-less phase, orchestrated all-down).
- Patterns are the mtr_warnings.sql regexes verbatim where possible; keep them anchored
  to the same text so drift between this list and the in-tree denylist is
  greppable at update time.
- `Query apply failed` / TOI-error / duplicate-entry findings should carry the un-injected-vote
  rule's attribution step before filing (catalog Shared conventions).
- Not mined here: the non-Galera portions of mtr_warnings.sql (generic MySQL noise) and
  per-test `mtr.add_suppression` calls; the galera-index-online-fk suppressions
  (`Query apply failed`, FK-constraint errors) confirm entry 1's finding status — Percona
  suppresses it precisely because the repro diverges nodes.
