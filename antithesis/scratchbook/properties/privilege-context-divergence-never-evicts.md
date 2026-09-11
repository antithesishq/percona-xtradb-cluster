---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# privilege-context-divergence-never-evicts — Privilege-context asymmetry between source session and root applier never evicts a node

**Type:** Safety
**Confidence:** High that the bug class is real and recurring (two independent fix commits
read in full, both describing the identical mechanism); Medium that the class is closed —
the fixes are per-statement patches, and the vendor's own pattern list (sut-analysis §9.2
pattern B) counts 10+ tickets in this family.

## What led here

Structural asymmetry: a statement executes on the originating node under the *client
session's* privilege context, but on every other node it is executed by a wsrep applier
thread running in **root/system context with no ACL checks**. TOI statements are replicated
*before* local execution. So any statement whose outcome (error vs success, or specific
error code) depends on the session's privileges produces a source-fails/applier-succeeds
split. Inconsistency voting hashes apply outcomes (gcs_group.cpp group_recount_votes
:939-1080); the source votes "failure", the appliers vote "success", and one side is evicted
("Leaving cluster") — a cluster availability loss caused by a *legal, correctly-rejected*
statement.

## Validated instances (mechanism confirmed from fix commits, not ticket text)

- **PXC-4709** (commit 072bda8b11f / merge 9e36d807655, 8.4 branch): CREATE/ALTER USER by a
  user lacking AUTHENTICATION_POLICY_ADMIN. Commit message verbatim: "TOI is replicated
  before actual execution. On source node it fails, but on replicated node, it is executed
  by wsrep_applier thread working in root user context, so it doesn't fail remotely. It
  kicks-in inconsistency voting protocol which evicts the node." Fix: *source-side*
  pre-replication validation of the authentication policy; skip TOI on validation failure
  while preserving the original error flow (sql/auth/sql_user.cc +157 lines).
- **PXC-4765** (merge 4fbf72409f5, commit chain via 66828f7f419): CREATE TRIGGER with
  DEFINER ≠ current user, without SUPER/SET_USER_ID. Same shape: originating node rejects,
  applier applies → "inconsistency and node leaving the cluster". Fix: explicit definer
  privilege/existence checks so "both locally executed and Galera-applied transactions
  follow the same access control logic".
- Related same-family (from sut-analysis pattern B, not individually re-validated):
  PXC-4644 (ER_ROW_IS_REFERENCED vs _2 — error-code variant of the same vote-divergence),
  PXC-4887, PXC-4284, PXC-4268, PXC-4336, PXC-4362. Vote protocol V7 (PXC-5286) reduces
  TOI/NBO votes to " Error_code: NNNN;" but its own commit documents residual divergence
  for DML apply errors (full locale-dependent text) — the vote *comparator* was hardened,
  not the privilege asymmetry that feeds it.

Why the class is open by construction: each fix adds a bespoke source-side check for one
statement type. Any privilege- or environment-sensitive statement not yet patched (candidate
surface: CREATE VIEW/EVENT/PROCEDURE with DEFINER, GRANT/REVOKE edge cases, statements
gated by partial_revokes, roles, `--skip-grant-tables` interplay, sql_mode-dependent DDL
errors) retains the asymmetry. ACL notify hooks are *removed entirely* for CREATE/ALTER
USER under WSREP (sql/sql_user.cc:3288-3329,:4016-4064, documented TOI↔ACL-lock deadlock) —
this area is heavily #ifdef-forked and fragile.

## Failure scenario

Workload runs a mix of user-management/DDL statements from **restricted accounts** (missing
one specific privilege at a time) concurrently on multiple nodes, under default-on network
faults (which add view churn and replay). A single unpatched statement type → one node's
apply outcome differs → vote → a healthy node is ejected. Field impact: an unprivileged
(or merely under-privileged) SQL user can knock nodes out of the cluster — availability DoS
via ordinary SQL, no cluster-channel access needed. Post-eviction the node stays down
(shipped systemd: RestartPreventExitStatus=SIGABRT) and, with
`repl.force_sst_after_inconsistency` default OFF, may even rejoin via IST into corrupt state.

## Suggested assertions (all missing)

- **Always (workload-side, the practical form):** after every privilege-varied
  statement batch completes (and any induced faults heal), `wsrep_cluster_size == N` and
  every node reports `wsrep_local_state_comment == 'Synced'`,
  `wsrep_cluster_status == 'Primary'`. Rationale: the guarantee is "never evicted for this
  reason", checked continuously; Always matches an every-evaluation invariant. To keep the
  signal attributable, the workload should avoid *other* known eviction triggers (no
  wsrep_ignore_apply_errors, no non-InnoDB writes, uniform config).
- **Unreachable (SUT-side, galera/src/replicator_smm.cpp:2411-2413 "inconsistent with
  group. Leaving cluster." path, and the no-vote self-eviction path :626-637):** "node
  evicted by inconsistency voting" — under a workload that issues only statements that are
  legal-or-correctly-rejected, this path must never fire. Marking it also gives Antithesis
  a replay anchor on near-misses.
- **Sometimes (workload-side):** "a privilege-restricted statement was rejected on the
  source with an ACL error" — confirms the workload actually creates the asymmetric
  condition (otherwise the Always is vacuous).

## Fault-availability notes

Core scenario needs NO faults at all — pure workload. Default-on network faults add value
(view changes interleaved with TOI). Node termination not required. This makes it one of
the cheapest high-value properties in the catalog.

## Observations

- The applier's root context is not incidental: appliers must bypass ACL to apply
  anything. The only correct fix shape is source-side outcome pre-validation (make the
  source's decision before replicating), which is why each statement type needs its own
  patch — hence "not closed by construction".
- Direction matters: pattern B here is source-fails/applier-succeeds. The mirror case
  (source-succeeds/applier-fails) is covered by the read-only-context property
  (applier-threads-never-read-only) — the two properties together bound the
  privilege/session-context asymmetry from both sides.
- MTR coverage exists per-ticket (pxc_create_user_auth_policy.test, trigger definer tests,
  pxc_inconsistency_voting*), but each is a fixed scenario; nothing sweeps the statement ×
  missing-privilege matrix, and MTR suppresses degraded-state warnings in CI
  (mtr_warnings.sql:372-455 includes "Query apply failed").

## Open Questions

- Which statement types beyond CREATE/ALTER USER and CREATE TRIGGER still carry the
  asymmetry? Why it matters: determines workload generator coverage; each unpatched type is
  a latent single-statement cluster DoS. A systematic sweep (every TOI-replicated statement
  × each revocable privilege) is exactly what Antithesis can automate and MTR does not.
  `(partial: the DEFINER-privilege family is verified symmetric for VIEW/EVENT/SP/TRIGGER —
  see Investigation Log; the remaining surface (auth-policy-style checks, partial_revokes,
  roles, sql_mode-dependent DDL errors) cannot be enumerated statically and is precisely
  the workload sweep's job)`

### Investigation Log

#### Is the "root-running applier vs restricted source" mechanism real (vs reporter guess)?

- Examined: full commit messages of PXC-4709 (072bda8b11f) and PXC-4765 (4fbf72409f5 chain);
  vote machinery references replicator_smm.cpp:1432-1470, :2379-2434,
  gcs_group.cpp:939-1152; sut-analysis §6.3 and §9.2-B.
- Found: both vendor fix commits state the mechanism explicitly ("executed by wsrep_applier
  thread working in root user context"); fixes are source-side pre-validation, confirming
  the asymmetry is the accepted root cause, not user misconfiguration.
- Not found: any generic guard making applier outcomes privilege-equivalent to source
  outcomes; each fix is statement-specific.
- Conclusion: bug class validated from primary evidence (fix diffs/messages); property
  targets the open class, with the fixed statements as regression anchors.

#### Do DEFINER-bearing CREATE VIEW / EVENT / PROCEDURE go through a PXC-4765-style pre-TOI check, or only triggers?

- Examined: `sql/sql_view.cc:505-682` (CREATE VIEW: `check_valid_definer` at `:534`, TOI
  at `:675`); `sql/sql_parse.cc:3223-3266` (`sp_process_definer` →
  `check_valid_definer`), `:4820` (runs before `Events::create_event`) and `:5425` (runs
  before `sp_create_routine`); `sql/events.cc:371` (event TOI, after the definer check);
  `sql/sp.cc:814/:977/:998/:1176` (SP TOI sites, all downstream of `sp_process_definer`);
  `sql/sql_trigger.cc:409-451` (PXC WSREP-specific pre-TOI definer/SUPER/SET_ANY_DEFINER
  check) vs post-TOI `table_trigger_dispatcher.cc:207`.
- Found: for VIEW, EVENT and stored routines the *upstream* definer-privilege validation
  already executes on the source BEFORE the TOI begin point, so a rejected DEFINER
  statement is never replicated — no asymmetry. Triggers needed the PXC-4765 patch because
  their `check_valid_definer` ran post-TOI; the patched pre-TOI block now covers them.
- Conclusion: resolved — the DEFINER candidates are covered; they drop out of the
  first-candidates list. Workload should still include them as regression anchors but
  should weight the un-enumerable remainder (auth-policy analogues, partial_revokes,
  roles) higher.

#### After vote-eviction with `repl.force_sst_after_inconsistency=OFF`, does the node rejoin via IST carrying divergent state?

- Examined: `galera/src/saved_state.cpp:280-360` (`mark_corrupt(bool)`,
  `unlink_state_file`), `replicator_smm.hpp:388-412` (`mark_corrupt_and_close` passing
  `force_sst_after_inconsistency_`), the unlink log text ("Removed state file ... will
  request a full SST on the next start. Remove it manually to force SST"), and the
  recovery-position arbitration documented in `grastate-se-checkpoint-agreement`
  (grastate seqno -1 → app-supplied `--wsrep_start_position` wins).
- Found: with the shipped default OFF, eviction zeroes grastate in place (UUID UNDEFINED,
  seqno -1) but keeps the file. The InnoDB SE checkpoint still carries the divergent
  position; the standard wrapper recovery dance (`--wsrep_recover` →
  `--wsrep_start_position`) re-presents it on restart, so the donor serves IST on top of
  the divergent datadir whenever the position is within its gcache. Only the non-default
  ON deletes grastate to force full SST — the unlink message itself confirms deletion is
  what forces SST.
- Conclusion: resolved — yes, IST-rejoin-with-divergent-state is the shipped default
  behavior. The follow-on pairing with the cross-node checksum oracle is justified: the
  post-rejoin checksum check should be part of any run that exercises vote eviction.
