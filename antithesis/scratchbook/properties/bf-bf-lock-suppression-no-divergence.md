# bf-bf-lock-suppression-no-divergence

**Type:** Safety. **Focus:** concurrency — InnoDB delegates BF-BF record-lock correctness to certification.

## What led to this property

InnoDB deliberately **ignores record-lock conflicts between two high-priority (applier/BF)
transactions**. Verified in this tree:

- `storage/innobase/lock/lock0lock.cc:581-599` — in the conflict predicate, if both the
  requesting trx and the lock holder are high-priority and wsrep is on, the function returns
  `Conflict::NO_CONFLICT` with the in-code comment: *"conflicting high priority locks are
  supposed to be false positives i.e. locks not on actual certified pay load data or GAP
  locks. these should not block applier execution, hence returning false here."*
- Second suppression site in the wait-queue scan `storage/innobase/lock/lock0lock.cc:2126-2149`
  (`/* don't wait for another BF lock */ res = false;`) using `wsrep_thd_is_BF(..., false)` —
  which reads `client_state::mode_` **without the client-state mutex**
  (`sql/service_wsrep.cc:121-132`), so a thread mid-transition can be misclassified.

The consequence: two appliers can concurrently modify the *same row* without either blocking
or erroring. The only thing that prevents that is Galera certification's dependency tracking
(`depends_seqno`): two writesets touching the same certification key must not be scheduled
for parallel apply. If certification's key coverage or dependency computation is wrong for
any statement class, InnoDB will not save you — the nodes silently diverge (each node's
appliers interleave differently).

Two amplifiers make the window wider:

1. `cert.optimistic_pa=yes` seeds `depends_seqno` optimistically
   (`percona-xtradb-cluster-galera/galera/src/certification.cpp:449-462`), allowing more
   parallelism than the writeset's declared dependencies strictly require.
2. **`Certification::param_set` mutates `optimistic_pa_` with no lock**
   (`certification.cpp:1403-1422` — verified: the function takes no mutex) while `do_test`
   reads it under the certification mutex. `SET GLOBAL
   wsrep_provider_options='cert.optimistic_pa=...'` under load is a plain data race on the
   variable that decides parallel-apply dependencies.

This is bug pattern A in the SUT analysis (missed cert keys → applier BF-BF): the realized
family is PXC-4789 (FK-child cert keys; the fix itself regressed multi-table DROP),
PXC-4657/4684 (no-op UPDATE/trigger emits table_map without a cert key), and the disabled
test `galera-index-online-fk` ("fk_40 triggers inconsistency voting" — a real reproducer of
cluster inconsistency, disabled rather than fixed, per `mysql-test/collections/disabled.def`).

## Failure scenario

1. Node A executes two local commits T1, T2 touching overlapping rows through a statement
   class whose cert-key footprint under-covers its row footprint (FK cascade, no-op update
   with trigger, multi-table DDL+DML mix).
2. Certification assigns `depends_seqno` that permits parallel apply on node B
   (`wsrep_applier_threads > 1`, worse with `cert.optimistic_pa=yes`).
3. On node B two appliers race on the shared rows; InnoDB suppresses the lock conflict
   (lock0lock.cc:581-599); the apply order of the conflicting row operations on B differs
   from A's commit order.
4. Both appliers succeed → **no apply error → inconsistency voting never triggers** (voting
   only compares apply *errors*, not state). Nodes are silently divergent until some later
   statement fails on one node only, or forever.

## Invariant / assertion plan

- **Primary (workload-side, `Always`):** cross-node logical checksum equality. After the
  workload quiesces (or at synchronized checkpoints using `wsrep_sync_wait=1` +
  `wsrep_last_committed` equality), every table's checksum (e.g. `CHECKSUM TABLE` or
  ordered-row digest) is identical on all nodes. Message:
  `"cross-node data identical after parallel apply"`. `Always` because divergence is never
  acceptable — this is the core product guarantee.
- **Coverage companion (SUT-side, `Sometimes`, missing):** instrument the suppression branch
  at lock0lock.cc:581-599 — `Sometimes("BF-BF record-lock conflict suppressed")`. Without
  this we cannot tell whether the workload ever exercised the delegated-correctness window;
  runs that never hit the branch say nothing about the property.
- **Optional sharper SUT-side check (missing, needs design):** at the suppression site,
  record `(space, page, heap_no)` + both trx seqnos; assert the two writesets are
  certification-ordered (holder's `global_seqno` <= requester's `depends_seqno`). If that
  relation does not hold, this specific suppression was NOT a false positive — a direct,
  local detector for missed cert keys that fires before any divergence is visible. This
  beats the workload checksum on localization but requires plumbing seqnos into the lock
  layer check; the checksum stays as the ground-truth oracle.

## Config / timing dependencies

- `wsrep_applier_threads > 1` (required; default is 1 — the config must set it).
- `cert.optimistic_pa=yes` via `wsrep_provider_options` (amplifier; also toggle it under
  load to exercise the unlocked `param_set` race).
- Workload needs FK parent/child updates, unique-key churn, no-op updates with triggers,
  and mixed DDL — the statement classes with historic key-coverage bugs.
- **Online-FK shapes (gap-fill, from the disabled `galera-index-online-fk` repro)**:
  online `ALTER ... ADD CONSTRAINT ... FOREIGN KEY ... CASCADE, ALGORITHM=INPLACE` under
  `foreign_key_checks=0`, `CREATE INDEX` on FK parents/children under concurrent
  cascading DML, multi-FK single ALTER, and FK on a non-unique secondary index
  (`restrict_fk_on_non_standard_key=OFF`) — full statement list in
  `no-mdl-bf-bf-abort.md` (shared DDL generator; see catalog Shared conventions,
  "Shared workload requirements").
- Faults: none strictly required (pure interleaving), but network jitter between nodes
  varies applier scheduling. Available-by-default faults suffice.

## Open questions

None — all three resolved; see Investigation Log. Net workload consequences: (a) the
workload MUST include PK-less FK-child tables under cascading DML (confirmed
under-coverage), (b) `Sometimes` probes belong at BOTH suppression sites with distinct
messages (second site independently reachable), (c) the `optimistic_pa` toggle stays in as
an amplifier, not as a standalone divergence generator.

### Investigation Log

#### Does `wsrep_certify_nonPK=ON` fully close the no-PK case, or do row-hash keys under-cover FK cascades?

- Examined: `ha_innodb.cc:13116-13290` (`wsrep_append_keys`; row-hash branch
  `:13256-13273`), `wsrep_append_foreign_key` definition (`ha_innodb.cc:12923-13056`), its
  call sites `row0ins.cc:1363` (cascade path, `referenced=false`, EXCLUSIVE key on the
  child table) and `:1713` (FK existence check, SHARED/REFERENCE key on the parent).
- Found: the full-row MD5 hash is computed ONLY in the handler-layer `wsrep_append_keys`
  (write_row/update_row/delete_row). FK cascades execute inside InnoDB
  (`row_update_cascade_for_mysql`) and never pass through the handler — the only cert key
  a cascaded child-row modification gets is the FK-index-value key from `row0ins.cc:1363`.
  For a PK-less child, a concurrent direct DML on the same row certifies under the
  full-row-hash key, which shares no key with the cascade's FK-value key.
- Conclusion: resolved — under-covered. `wsrep_certify_nonPK` does NOT protect
  cascade-vs-direct-DML on PK-less children; the workload must include PK-less FK-child
  tables with ON DELETE/UPDATE CASCADE.

#### Is the second suppression site (lock0lock.cc:2126-2149) independently reachable?

- Examined: `rec_lock_check_conflict` (`lock0lock.cc:563-610`, site 1) and the wait-queue
  scan with the BF-BF override (`:2100-2160`, site 2) including its 20-line in-code
  comment.
- Found: yes. Site 2 fires only when `rec_lock_has_to_wait` (which embeds site 1) says
  conflict — i.e., precisely when site 1 did NOT suppress. The predicates differ: site 1
  uses `trx_is_high_priority` on both trxs, site 2 uses `wsrep_thd_is_BF(thd,false/true)`
  (unlocked `client_state::mode_` reads), so a thread mid-transition (BF-aborted trx being
  replayed) is classified differently. The in-code comment documents a concrete reachable
  scenario (galera_FK_duplicate_client_insert: replayed insert enqueues behind an
  applier's S-lock, then the scan returns "no conflict").
- Conclusion: resolved — independently reachable; place `Sometimes` probes at both sites
  with distinct messages (already in the suggestions below).

#### Can toggling `cert.optimistic_pa` under load produce per-node `depends_seqno` divergence?

- Examined: `certification.cpp:449-462` (`do_test` parent-seqno init reading
  `optimistic_pa_`), `:1403-1422` (`param_set` — confirmed no mutex), `:1000` (ctor).
- Found: `optimistic_pa_` is a node-local provider option (never negotiated), read per
  writeset during each node's local certification. Per-node `depends_seqno` differences
  are therefore possible by design whenever nodes hold different values — including
  transiently during a toggle, since the unlocked write races in-flight `do_test` calls
  (a plain bool, so "torn" values are not the issue; which value is observed per writeset
  per node is nondeterministic). However, the flag only gates the conservative raise of
  `depends_seqno` to `last_seen_seqno`; the key-derived dependencies computed later are
  unaffected, and certification PASS/FAIL outcomes do not depend on it.
- Conclusion: resolved — the toggle alone cannot cause divergence when cert keys are
  complete; it changes apply parallelism only. It remains exactly what the property
  already calls it: an amplifier that widens the window for missed-key divergence.

## SUT-side instrumentation suggestions (all missing)

- `Sometimes("BF-BF record-lock conflict suppressed")` at lock0lock.cc:581-599 branch.
- `Sometimes("BF-BF wait-queue conflict suppressed")` at lock0lock.cc:2126-2149 branch.
- `Always("suppressed BF-BF conflict is certification-ordered", holder_seqno <= requester_depends_seqno)`
  at the same branch (needs seqno plumbing).
- `Sometimes("cert.optimistic_pa toggled while certifications in flight")` in
  `Certification::param_set`.
