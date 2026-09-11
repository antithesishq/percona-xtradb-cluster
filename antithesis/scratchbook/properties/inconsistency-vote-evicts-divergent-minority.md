# inconsistency-vote-evicts-divergent-minority — Evidence

**Focus area:** Distributed coordination — inconsistency voting correctness.
**Confidence:** High on mechanics (voting arithmetic, recompute, process_vote all read
directly); the "wrong node evicted" trigger relies on documented residual vote-divergence
(protocol V7 commit text) plus code-confirmed empty-vote collision.

## Claim under test

When applying a writeset fails on some nodes and succeeds on others, the voting protocol
evicts exactly the minority whose result differs from the group's winning vote; the majority
(and in particular nodes that applied successfully, when they are the majority) always
survives. A single divergent node never causes a healthy majority node to leave, and the
cluster never loses Primary status because of one node's apply error.

## Code (validated, commit f9ecb3e; galera @13ff9ed6)

- Vote cast on apply error: `galera/src/replicator_smm.cpp:1432-1470` (`process_apply_error`,
  "must be done IN ORDER" :1439). **Only errors vote** — successful-but-different apply is
  invisible to this machinery (external checksum oracle is a separate property family).
- Vote request handling on success nodes: `replicator_smm.cpp:2379-2437` (`process_vote`):
  vote request → drain monitors to seqno → cast vote 0; return 1 (majority disagrees) →
  "Vote 0 (success) on <gtid> is inconsistent with group. Leaving cluster." →
  `on_inconsistency()` (:2412-2415, :2427-2429). **This is the property's kill shot: a
  majority of *failing* nodes evicts the healthy one.**
- Vote counting: `gcs/src/gcs_group.cpp:939-1080` (`group_recount_votes`): nodes with
  `last_applied >= voting_seqno` implicitly vote 0/success (:972-988); winner selection with
  missing-vote arithmetic (:1031-1057); `gcs.vote_policy` default 0 = plain majority
  (:1034-1046); waits for more votes when undecided (:1053-1056).
- Vote normalization (PXC protocol V7): `gcs_group.cpp:1093-1154`
  (`recompute_vote_based_on_error_code`): regex extracts only 4–5-digit `Error_code: NNNN`
  tokens (:1102); ignore-list = {"1681"} (:1085-1091); sorted, comma-joined, re-hashed
  (:1125-1143). **No regex match → vote computed over the EMPTY string** — two nodes failing
  with completely different unmatchable errors cast IDENTICAL votes and can out-vote the
  correct node. DML apply errors keep locale/privilege-dependent full text upstream of this
  (documented residual divergence, PXC-5286 / sql/wsrep_mysqld.cc:110-140).
- NULL-deref hazard in the same handler: `gcs_group.cpp:1184-1198` — code != 0 with no
  payload → `std::string(data, strlen(data))` with `data = NULL` (:1195-1197). A
  truncated/mixed-version VOTE message crashes the receiver.
- Bypass: empty-error apply failure skips voting entirely — node unilaterally declares itself
  inconsistent and leaves (`replicator_smm.cpp:626-637`, vote only if `err->ptr`
  :1941-1944 region). Transient local failure (e.g. backup-lock acquisition,
  `sql/wsrep_high_priority_service.cc:453-456`) → self-eviction with no group vote.
- Aftermath (verified `saved_state.cpp:301-345` + `replicator_smm.hpp:388-412`): eviction
  runs `on_inconsistency()` → `mark_corrupt_and_close()`. With
  `repl.force_sst_after_inconsistency` default OFF the grastate file is *zeroed in place*
  (UUID UNDEFINED, seqno -1) but NOT deleted; only the non-default ON removes the file
  ("Removed state file ... will request a full SST on the next start",
  `unlink_state_file`). The InnoDB SE checkpoint still holds the (divergent) position, and
  the standard wrapper recovery dance re-presents it via `--wsrep_start_position` → rejoin
  via IST *into known-divergent state* is the shipped behavior (PXC-5208's fix is gated
  behind the same default-OFF).
- Deterministic-error injectors available to the workload (no debug build needed): bug-pattern
  B fixtures — FK violations with differing error variants (PXC-4644 ER_ROW_IS_REFERENCED vs
  _2), privilege-dependent DDL errors (PXC-4709/4765), `wsrep_ignore_apply_errors` knob,
  node-local schema damage on one node via `wsrep_OSU_method=RSU`/`sql_log_bin=0` (documented
  guarantee-void levers) to force one node's apply to fail predictably.

## Failure scenario

1. 3-node (or 5-node) cluster. Workload makes ONE node's apply fail deterministically for a
   chosen writeset (e.g., RSU-applied local schema tweak on node C: drop an index/table that a
   later replicated DML needs; or one-node privilege/locale difference).
2. Expected: C votes nonzero, A+B (last_applied ≥ seqno) count as success → C loses vote →
   "Leaving cluster" on C only; A,B remain Primary size 2; C closes its provider connection
   and its mysqld STAYS ALIVE in Disconnected/non-Primary state (verified: `on_inconsistency`
   → `start_closing()` → `gcs_.close()`; disconnected view → wsrep-lib `go_final`; applier
   threads exit without `unireg_abort`, `sql/wsrep_thd.cc:42-98`). No process death, no
   gu_abort on this path — rejoin requires an external mysqld restart.
3. Bug shapes Antithesis explores: (a) two nodes fail with different unmatchable error texts →
   identical empty votes form a majority → the HEALTHY node evicted (data-preserving nodes
   lose, divergent survive — the inverted outcome); (b) vote concurrent with a view change /
   partition (group_recount_votes on conf change, :962 "can happen on config change") →
   double-eviction or lost vote → cluster non-Primary; (c) corrupt node abstains
   (st_.corrupt() → goto out :2399) shifting the majority; (d) VOTE message truncation →
   NULL-deref crash of a healthy receiver (:1197).
4. Violation observables: more nodes leave than the divergent set; the divergent node stays
   while a healthy one leaves; cluster loses Primary although N-1 healthy nodes were connected;
   any mysqld crash inside the vote handler.

## How to check (workload-side)

- The workload knows which node it sabotaged. `Always`: within T of the poisoned writeset
  committing on the majority, (i) exactly the sabotaged node leaves the cluster view
  (`wsrep_cluster_size` drops by exactly 1 on survivors; `wsrep_evs_state`/error log "Vote"
  lines name it), (ii) survivors remain Primary and writable, (iii) survivors' data matches
  each other.
- `Sometimes`: an inconsistency vote round completed with a nonzero minority (the machinery
  was actually exercised — MTR covers this only with symmetric single-error injections).
- Log-derived check (workload greps error logs, or SDK in SUT): "is inconsistent with group.
  Leaving cluster." appears ONLY on the sabotaged node.

## Assertion type

- `Always`: "vote outcome evicts exactly the known-divergent node set; majority stays
  Primary" — safety of the arbitration.
- `Sometimes(vote_round_with_disagreement_completed)` — exploration guidance; vacuity guard.

## Instrumentation suggestions (missing)

- SUT-side `Always` in `process_vote` (`replicator_smm.cpp:2412`): reaching the
  "success-inconsistent-with-group" branch while this node's apply genuinely succeeded is
  only legal if a true majority failed — emit vote tallies as details for triage.
- SUT-side `Unreachable` at `gcs_group.cpp:1197` guard: `code != 0 && data == NULL` (the
  NULL-deref precondition) — cheap and high-value.
- SUT-side `Sometimes` at `recompute_vote_based_on_error_code` when
  `matchedErrorCodes.empty() && !err_msg.empty()` — the empty-vote collision precondition;
  seeing it fire under two-node failure is the direct precursor of the inverted eviction.
- SUT-side `Reachable` on the no-vote self-eviction path (`replicator_smm.cpp:626-637`).

## Fault requirements

- None strictly (workload-induced divergence suffices); network faults add the vote×view-change
  races; CPU throttle widens vote/commit-cut interleavings. Node restart needed only to
  exercise the post-eviction rejoin (`force_sst_after_inconsistency=OFF` variant).
- Harness note: gu_abort suppresses core dumps (gu_abort.c:29-58) and shipped systemd excludes
  SIGABRT from restart — the harness must supervise/restart the evicted node itself if rejoin
  behavior is in scope.

## Open questions

None — all four resolved; see Investigation Log. Note the resulting detector change: the
workload's "exactly one node left" check must look for provider-Disconnected/non-Primary
state on the evicted node (its mysqld stays alive), not process death.

### Investigation Log

#### Are DML apply-error strings bit-identical across identically-configured 8.4.10 nodes?

- Examined: `sql/wsrep_applier.cc:83-136` (`wsrep_store_error`),
  `sql/wsrep_high_priority_service.cc:418-429` (`apply_events`) and its call sites
  (`:473` TOI, `:844` NBO pass `include_msg = protocol < V7`; `:676` DML applier and
  `:1088` replayer use the default `include_msg=true`).
- Found: not guaranteed. The DML vote payload is the full diagnostics area: the error's
  `message_text()` PLUS every accumulated SQL condition (warnings), each as
  " <text>, Error_code: NNNN;". The in-code "KH" comment at the top of `wsrep_store_error`
  concedes exactly this hazard ("source node issues some warnings because of executing
  user privileges ... On replica side it can fail without preceding warnings"). Message
  texts are format-string+data-derived, so two identically-configured appliers failing on
  identical data produce identical bytes for the common data-dependent errors — but any
  per-node warning, errno/strerror or path-bearing text breaks bit-identity. Only TOI/NBO
  are normalized to code-only under V7.
- Conclusion: resolved — determinism is conditional, not guaranteed; the assertion should
  treat "extra node evicted for text-divergence" as a distinct (also-a-bug) outcome, as
  the property text anticipated.

#### Does anything besides `process_apply_error` cast nonzero votes?

- Examined: tree-wide grep for `gcs_vote`/`gcs_.vote` (only wrapper `galera_gcs.hpp:193`
  and two callers); callers of `handle_apply_error`.
- Found: nonzero votes only from `process_apply_error` (`replicator_smm.cpp:1443`), fed by
  writeset-apply failure (`:1533`) and TOI failure (`:1945`). Vote 0 only from
  `process_vote` (`:2401`). Empty-error failures self-evict without any vote (`:626-637`,
  `:1235`, `:1498`).
- Conclusion: resolved — "only errors vote" is proven; the external checksum oracle is
  mandatory, not optional.

#### Vote vs. simultaneous partition — stale `vote_result` misfiring a later vote on the same seqno?

- Examined: `gcs_group.cpp:939-1080` (`group_recount_votes`), `:1156-1266` (vote message
  handler), `:2298-2308` (conf-change handler).
- Found: on every configuration change in PRIMARY state the group re-runs
  `group_recount_votes` and ships the result in the cchange event (`:2303-2307`), so an
  undecided round ("Waiting for more votes") resolves with the shrunken membership rather
  than going stale. `vote_result.seqno` is monotonic; later votes on an already-decided
  seqno are answered from `vote_history` — which is ERASED on first read (`:1238`) — or
  fall to the default "result = 0 ... this node is the only inconsistent one"
  (`:1241-1246`).
- Not found: any path that re-opens a decided seqno.
- Conclusion: resolved with a narrowed residual — the stale-vote_result misfire shape does
  not exist, but the one-shot history erase plus the lose-by-default fallback means a
  node whose vote reply is delayed across a partition/rejoin can self-evict spuriously.
  That residual is already inside this property's "vote × view-change races" angle.

#### Eviction outcome at the server layer: abort vs close in release build?

- Examined: `replicator_smm.hpp:388-412` (`on_inconsistency` → `mark_corrupt_and_close` →
  `start_closing`), `replicator_smm.cpp:295-303` (`start_closing` = `gcs_.close()` only),
  `wsrep-lib/src/server_state.cpp:1125-1157` (disconnected view → `go_final`),
  `sql/wsrep_thd.cc:42-98` (applier exit path), grep for `unireg_abort`/abort hooks in the
  chain (none), `abort_cb` in `wsrep_provider_v26.cpp:659-666` (only cancels SST).
- Found: the provider closes its gcs connection; the server processes a disconnected view
  and goes `s_disconnected`; applier threads run to completion and exit normally. mysqld
  stays alive, non-Primary, wsrep_ready=OFF. grastate is zeroed (UUID UNDEFINED:-1) via
  `mark_corrupt(false)` (default), file kept.
- Conclusion: resolved — close, not abort. Detector = evicted node reports
  Disconnected/non-Primary while alive; survivors' cluster size drops by one. The earlier
  gu_abort assumption in this file's failure scenario was corrected.
