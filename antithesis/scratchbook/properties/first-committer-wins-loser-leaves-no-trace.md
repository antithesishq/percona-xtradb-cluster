# first-committer-wins-loser-leaves-no-trace

**Focus:** Protocol contracts — the first-committer-wins conflict contract as seen by
clients.
**Confidence:** High on the documented contract and code sites; the property is the
canonical cross-node check that no existing test runs continuously.

## Claimed contract

doc/source/manual/certification.rst:139 ("FIRST WRITE WINS"), limitation.rst:32-38: when
two concurrent transactions on different nodes conflict, the one certified first commits
everywhere; the other is rolled back and its client receives ER 1213 / SQLSTATE 40001 at
COMMIT. Complementary halves of the contract:

1. **Loser leaves no trace**: a transaction whose client got ER 1213 is not committed on
   *any* node (certification failure produces a dummy writeset so seqnos stay symmetric —
   `sql/wsrep_high_priority_service.cc:569-572`, gcache seqno_skip
   `replicator_smm.cpp:1466-1468`).
2. **Winner is universal**: a transaction whose client got OK at COMMIT is (eventually,
   and immediately under sync_wait) visible on every node and never later disappears.
3. **Exactly one of a conflicting pair wins** — never both, never a lost update.

## Why this can break (mechanism, from discovery)

- The BF-abort / replay machinery between certification and commit is the densest cluster
  of timing-dependent transitions in the SUT (sut-analysis §5.2-5.3): the client mutex is
  unlocked across the provider certify call (wsrep-lib transaction.cpp:1823), 12-branch
  status switch (:1897-2010) including "Galera may return CONN_FAIL if trx BF aborted O_o";
  success-but-must_abort → replay (:1916). A wrong branch under a fault converts a
  certified (won) transaction into a client error — a **phantom commit**: client told
  "deadlock", data committed cluster-wide. The inverse (client OK, quietly aborted locally)
  exists too: commit_order_leave's non-assert failure path "quietly aborts the ordered txn
  locally while peers commit" (wsrep-lib transaction.cpp:598-628; sut-analysis §6.7).
- `wsrep_retry_autocommit` (default 1) silently re-executes autocommit statements after BF
  abort (`sql_parse.cc:7977-8034`) — a retry racing a replay is a double-apply candidate.
- InnoDB deliberately ignores lock conflicts between two applier transactions
  (lock0lock.cc:581-599), so a missed certification dependency turns "exactly one wins"
  into both-apply-in-parallel divergence with no error anywhere.

## Failure scenario

Workload runs deliberately conflicting writes (same keys from multiple nodes) while
Antithesis injects partitions/latency into the certify→commit window. Violations:
(a) 1213-loser's row version visible on any node afterwards; (b) OK-winner's version
missing on some node after convergence; (c) both "winners" for one conflict round (lost
update: final value reflects neither serial order).

## Suggested assertions (all missing — workload-side; this is the flagship workload oracle)

- **Always:** for every transaction that returned ER 1213/40001, its unique write-marker is
  absent from all nodes after convergence (bounded wait + sync_wait read). Message:
  "first-committer-wins loser left no trace".
- **Always:** for every transaction that returned OK at COMMIT, its write-marker is present
  on all nodes after convergence. Message: "committed transaction visible cluster-wide".
  (Ambiguous outcomes — connection dropped mid-COMMIT — are recorded as *unknown* and
  asserted only to converge to one of the two states consistently on all nodes.)
- **Always:** per conflict round on a designated counter row, the final value equals the
  serial application of the observed winner set (no lost updates). Message: "conflicting
  commits serialize".
- **Sometimes:** "a certification conflict produced an ER 1213 loser while its rival
  committed" — confirms the contract's interesting case is actually exercised (BF aborts
  are timing-dependent; without this the Always checks can be vacuous).

## Assertion-type rationale

`Always` for all three correctness halves — each is a per-transaction invariant. The
`Sometimes` companion is essential because on an unloaded cluster certification conflicts
are rare; it doubles as the exploration hint toward the certify-window races.

## Fault requirements

Default-on network faults suffice (they widen the certify→commit windows and force
replays). Node termination adds the crash-during-commit shape (stronger, often disabled —
flag). No clock faults needed.

## Outcome classification (resolved — drives the workload's buckets)

Enumerated from wsrep-lib `transaction.cpp:1897-2010` plus the galera glue
(`wsrep_provider.cpp:588-660`: certify = `replicate()` then `certify()`, with the
invariant that OK/BF_ABORT ⇒ global seqno assigned, `:635-636`) and galera's
post-ordering interrupt handlers (`replicator_smm.cpp:1289-1305` handle_commit_interrupt,
`:3865-3884` handle_apply_monitor_interrupted — both return BF_ABORT → replay for
COMMIT-flagged transactions; per the #528 comment at `:1091-1093` a replicated trx is
never abandoned):

- **Legitimately committed:** client OK — either directly (`success` + s_certifying) or
  after replay (`error_bf_abort`, or `success` + s_must_abort → s_must_replay → replay
  success → s_committed, transaction.cpp:2129-2141).
- **Guaranteed no-trace (assert loser-absent):** any clean error with the node still
  alive: ER 1213 via `error_certification_failed` (deterministic cert failure → dummy
  writeset on every node); ER 1213 after replay cert-failure (same dummy mechanism,
  transaction.cpp:2142-2152); ER 1213 via `error_connection_failed` + must_abort
  (CONN_FAIL is returned only pre-ordering — no seqno, nothing delivered anywhere);
  the e_error_during_commit family (`error_warning`/`transaction_missing`/
  `size_exceeded`/`provider_failed`/`not_implemented` — all pre-ordering failures).
- **Unknown bucket (assert only consistent convergence):** (a) no client reply —
  connection dropped mid-COMMIT; (b) any error accompanied by node death — `error_fatal`
  and replay-failure both call `emergency_shutdown()` (transaction.cpp:1993-1997,
  2153-2155): the writeset may have been ordered and applied by peers; (c) XA left in
  s_prepared (`error_connection_failed` + !must_abort + is_xa, transaction.cpp:1976-1979)
  — outcome deferred to XA COMMIT/ROLLBACK resolution.

## Open questions

None — both resolved (see Investigation Log). The unique-key markers stay in the
workload design regardless: they are the general double-apply oracle.

### Investigation Log

#### Which client-visible outcomes legitimately leave the transaction committed (12-branch switch enumeration)?

- Examined: wsrep-lib `src/transaction.cpp:1804-2013` (certify_commit switch),
  `:2110-2162` (replay), `galera/src/wsrep_provider.cpp:556-685` (replicate+certify
  glue and status funnel), `galera/src/replicator_smm.cpp:748-880` (replicate),
  `:1069-1144` (certify), `:1289-1305`/`:3831-3884` (post-ordering interrupt handlers),
  `:2092-2139` (confirmed the other CONN_FAIL sites are sst_sent/NBO, not certify).
- Found: CONN_FAIL reaches the client only from pre-ordering `replicate()` failures
  (non-prim, -ERESTART, gcs errors) — never after a seqno was assigned; every
  post-ordering interrupt of a COMMIT-flagged trx maps to WSREP_BF_ABORT → s_must_replay;
  replay ends s_committed (OK), s_aborted+1213 (deterministic cert failure → dummy
  everywhere), or `emergency_shutdown()`. Full classification recorded above.
- Not found: any branch that returns a clean client error while leaving the writeset
  ordered-and-applied with the node alive (that shape would be the property violation
  itself — e.g. via `commit_order_leave`'s non-assert error path, which remains the
  mechanism under test).
- Conclusion: RESOLVED — "unknown" bucket = {no reply, error+node-death, XA s_prepared
  limbo}; all clean live-node errors assert loser-no-trace; OK asserts winner-universal.

#### Does wsrep_retry_autocommit re-execution race its own replayed first attempt?

- Examined: `sql/sql_parse.cc:7972-8059` (`wsrep_dispatch_sql_command` retry loop),
  wsrep-lib `src/transaction.cpp:850-959` (after_statement), `:2110-2162` (replay),
  `src/client_state.cpp:258-301`.
- Found: replay executes synchronously inside `wsrep_after_statement` — in the same
  client thread — before the retry decision at sql_parse.cc:8005-8021. The retry loop
  runs only when after_statement returned nonzero, which requires final state s_aborted
  (`assert(ret == 0 || state() == s_aborted)`, transaction.cpp:957); replay success
  returns 0 (no retry), replay cert-failure returns 1 with the first attempt provably
  dummied on all nodes. Strictly serialized; no concurrent retry-vs-replay window.
- Conclusion: RESOLVED — no race by construction; concern dropped. Unique-key markers
  retained as the generic double-apply detector.
