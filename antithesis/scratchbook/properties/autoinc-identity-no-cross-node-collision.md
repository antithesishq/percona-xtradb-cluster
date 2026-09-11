# autoinc-identity-no-cross-node-collision

**Property:** Under `wsrep_auto_increment_control=ON` (default), auto-increment values and
X-protocol document `_id`s generated concurrently on different nodes never collide — i.e., a
pure auto-inc-keyed INSERT (no explicit key value) on one node never fails certification
against another node's pure auto-inc INSERT, across arbitrary membership churn.

**Confidence:** High on the mechanism (verified: the collision-avoidance scheme is rewritten
on every view change on *global* variables only, and the X plugin caches it once). Medium on
how often real collisions materialize — the window is transition-timing plus stale-session
lifetimes.

## Why this is wildcard territory

Identity generation is nobody's assigned focus: it sits at the intersection of membership
changes (focus 7/8), an implicit uniqueness guarantee the system never states, and a
client-visible API (X protocol). The disabled galera-x tests ("blinking auto-generated
ids") show the vendor hit this and disabled the tests instead of fixing the class.

## Code evidence (verified at commit f9ecb3e)

1. **Per-view rewrite, globals only** — `Wsrep_server_service::log_view`,
   `sql/wsrep_server_service.cc:196-198`: on every view,
   `global_system_variables.auto_increment_offset = view.own_index()+1;
   global_system_variables.auto_increment_increment = view.members().size();`
   under `LOCK_global_system_variables`. **CORRECTION (investigated):** classic SQL
   sessions do NOT keep connect-time copies — `THD::reset_for_next_command`
   (`sql/sql_parse.cc:6440-6456`) re-copies both globals into the session at the start of
   EVERY statement for local wsrep sessions when `wsrep_auto_increment_control=ON`. The
   stale-session window is therefore only the in-flight-statement transition window — BUT
   the per-statement refresh reads the two globals WITHOUT `LOCK_global_system_variables`,
   so a statement racing `log_view` can pick up a torn (old-offset, new-increment) pair,
   which is itself a collision-capable scheme no node legitimately owns.
2. **Cluster-size changes change the modulus**: shrink 3→2 → increment goes 3→2 while
   in-flight statements still stride by 3 from a differently-offset start; the torn-pair
   race above adds mixed schemes even for freshly-started statements.
3. **X-protocol doc IDs cache the scheme once** —
   `plugin/x/src/document_id_aggregator.cc:38-60`: `configue()` reads
   `@@mysqlx_document_id_unique_prefix, @@auto_increment_offset, @@auto_increment_increment`
   one time into `m_variables`; subsequent `generate_id()` calls use the stale snapshot for
   the aggregator's lifetime. `mysqlx_document_id_unique_prefix` defaults identically on
   all nodes, so offset/increment are the only cross-node disambiguators.
4. TOI resets to offset=1/increment=1 for the TOI THD (`sql/wsrep_mysqld.cc:3118-3120`) —
   intentional, but another writer to the same knobs.
5. Consequence chain when IDs collide: both INSERTs carry the same PK cert key → Galera
   first-committer-wins → the loser gets ER_LOCK_DEADLOCK at COMMIT (or is silently retried
   by `wsrep_retry_autocommit`). So collisions are *safe* but manifest as spurious
   certification conflicts between logically non-conflicting transactions — and if the
   colliding column is a non-unique auto-inc key (auto-inc need not be the PK), both rows
   commit and the collision is silent client-visible ID duplication.

## Failure scenario

Continuous single-row inserts (PK omitted) from long-lived connections on all nodes.
Antithesis partitions node C out and back; each membership event renumbers `own_index`.
During and after churn: (a) two nodes generate the same auto-inc PK → cert-conflict storm
on a workload that by construction never conflicts; or (b) with auto-inc as a non-unique
key, duplicated "unique" application IDs committed cluster-wide; or (c) X-protocol
collections get colliding `_id`s from stale cached schemes.

## Testable formulation

Workload: table `ids (id BIGINT AUTO_INCREMENT PRIMARY KEY, node CHAR(1), payload ...)`;
every node inserts continuously from a long-lived connection AND from fresh per-insert
connections (two distinct sub-workloads — the stale-session leg and the clean leg), with
`wsrep_retry_autocommit=0` on these sessions so conflicts are observable.

- `Always` (main): "an INSERT omitting the auto-inc key never returns ER_LOCK_DEADLOCK /
  ER_DUP_ENTRY, and `wsrep_local_cert_failures` attributable to the ids-table workload does
  not increase" — on a workload whose transactions touch disjoint generated keys by
  design, any cert conflict proves an ID collision. `Always` because the guarantee must
  hold on every insert; a single conflict is a real violation of the documented
  auto_increment_control purpose ("avoids auto-increment conflicts across nodes").
- `Always` (steady-state config): "whenever the primary component is stable for > grace
  period, the set of `@@global.auto_increment_offset` across member nodes is pairwise
  distinct and every `@@global.auto_increment_increment` equals `wsrep_cluster_size`."
  Catches lost/misordered view processing even before a collision materializes.
- `Sometimes`: "a view change occurred while at least one ids-table insert was in flight"
  — confirms the interesting interleaving was explored.

## Instrumentation suggestions (all missing)

- Workload checks above (no SUT changes required).
- Optional SUT-side `Sometimes` at `wsrep_server_service.cc:196-198` recording
  (old_offset != new_offset) — marks index reshuffles as exploration checkpoints.
- X-protocol variant: mysqlx collection add loop asserting no duplicate `_id` and no
  duplicate-key error; lower priority (needs mysqlx in the harness).

## Fault requirements

Default-on network partitions/hangs are sufficient — they are exactly what churns
membership. No node termination needed (though it widens the window).

## Open questions

None — all three resolved; see Investigation Log. Net effect — the property's mechanism
scope CHANGES: the wide stale-session leg collapses (per-statement refresh exists); the
primary targets are now (a) the unlocked torn (offset, increment) read racing `log_view`,
(b) in-flight statements crossing a view change, and (c) the X-protocol document-id
aggregator, whose configure-once cache (`document_id_aggregator.cc:38-60`) is NOT touched
by the per-statement refresh and keeps the wide window. The two-sub-workload design
(long-lived + fresh connections) stays, but the long-lived leg now probes the torn-read
race rather than connect-time staleness.

### Investigation Log

#### Is there any code path that refreshes existing sessions' auto_increment vars on a view change?

- Examined: tree-wide grep for `auto_increment_offset =`/`auto_increment_increment =`
  writers; `sql/sql_parse.cc:6421-6460` (`mysql_reset_thd_for_next_command` /
  `THD::reset_for_next_command`, dispatched per statement at `:2752`);
  `sql/wsrep_server_service.cc:196-198` (`log_view` writer, holds
  `LOCK_global_system_variables`); `sql/mysqld.cc:7391-7395` and `sys_vars.cc:1090/8353`
  (saved_* machinery for toggling the control off).
- Found: YES — a per-statement refresh exists: for `WSREP(thd) && wsrep_thd_is_local &&
  !slave_thread && wsrep_auto_increment_control`, both session vars are re-copied from the
  globals at every statement start. So long-lived sessions are stale only while a
  statement is in flight across the view change. However the refresh reads the two globals
  WITHOUT taking `LOCK_global_system_variables`, while `log_view` writes them under it —
  a racing statement can observe a torn (old, new) pair.
- Conclusion: resolved — refresh path exists; property survives with changed scope
  (transition-window + torn-pair + X-protocol cache). Catalog "long-lived sessions keep
  stale copies" wording needs the same correction.

#### Does `wsrep_retry_autocommit` mask duplicate committed rows in the non-unique-key leg?

- Examined: retry site `sql/sql_parse.cc:7977-8034`; key-append logic
  (`ha_innodb.cc:13116-13290`) for the non-unique-auto-inc shape; provider cert-failure
  accounting (replicator stats, incremented at certification, upstream of server retry).
- Found: retry engages only on a certification conflict. In the non-unique-key leg the
  colliding generated values produce NO shared cert key and therefore no conflict — both
  rows commit and there is nothing for retry to mask or retry. Retry masking is confined
  to the unique/PK leg, where it hides the client-visible error but not the
  provider-side `wsrep_local_cert_failures` increment.
- Conclusion: resolved — the default-retry variant is still worth one run for realism, but
  the duplicate-committed-rows check is orthogonal to the retry setting, and the
  counter-based assertion stays sound under `wsrep_retry_autocommit=1`.

#### Do the disabled galera-x "blinking auto-generated ids" tests document a portable collision repro?

- Examined: `mysql-test/collections/disabled.def:276-289` (PXC-4154 block);
  `mysql-test/suite/galera-x/t/udf_mysqlx_generate_document_id.test` (present in-tree).
- Found: they are golden-file tests of the `mysqlx_generate_document_id()` UDF and X CRUD
  inserts. "Blinking" = the generated ids embed the node's (offset, increment), which
  depends on the view's own_index, so the recorded output is nondeterministic in a cluster
  — a flaky-result disablement, not a demonstrated cross-node collision.
- Conclusion: resolved — no portable repro to port; the tests do confirm doc-ids embed the
  view-dependent scheme (supports the X-protocol leg's mechanism) and that the vendor
  disabled rather than stabilized them.
