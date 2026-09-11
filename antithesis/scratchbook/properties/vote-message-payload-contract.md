# vote-message-payload-contract

**Focus:** Protocol contracts — inconsistency-vote message schema validation and vote
semantics.
**Confidence:** High (both defects read directly in gcs_group.cpp at commit f9ecb3e).

## Claimed contract

Inconsistency voting (GCS_MSG_VOTE) is PXC's only divergence-*detection* mechanism: nodes
whose apply of writeset S failed broadcast a vote derived from the error; the minority
verdict is ejected ("Leaving cluster", replicator_smm.cpp:2411-2413). Implicit schema
contract: a vote with non-zero code carries an error-message payload; distinct failures
produce distinct votes; a node that applied successfully (vote 0) is only ejected when a
true majority observed a *different* outcome on the same seqno.

## Two verified contract violations in the handler

`gcs_group_handle_vote_msg` (`gcs/src/gcs_group.cpp:1156-1210`):

1. **NULL-payload crash.** `data` is set to NULL when `msg->size <=
   gcs::core::CodeMsg::serial_size()` (:1184-1187). The log line handles NULL (:1193
   prints "(null)"), but the very next statement does not:
   ```
   if (code != 0) {
       std::string err_msg(data, strlen(data));   // :1195-1197 — strlen(NULL)
   ```
   A payload-less vote with non-zero code → SIGSEGV in the GCS recv path of **every node
   that processes it** — a single malformed message crashes the whole cluster
   simultaneously (correlated failure, worse than the inconsistency it reports).

2. **Empty-string vote collision.** `recompute_vote_based_on_error_code` (:1094-1154)
   extracts only `Error_code: \d{4,5}` matches (:1102), drops the ignore-list ({"1681"},
   :1085-1091), and computes the vote over the joined string. **No match → vote over the
   empty string** (:1139-1143). Consequences:
   - Two nodes failing for *different* reasons whose messages contain no matching
     `Error_code:` token cast **identical** votes → counted as agreeing → they can outvote
     the node that applied successfully → **the consistent node is ejected and the two
     broken nodes continue as the cluster** — the vote protocol inverts its own guarantee.
   - Error codes <1000 (e.g. OS-level codes) and 3-digit codes never match the regex.

## Failure scenario

Antithesis provokes apply failures that differ per node (disk faults, privilege/timing
differences — bug pattern B: PXC-4709/4765/4644/4887...) or message truncation/connection
churn during a vote exchange. Either the NULL-deref fires (correlated crash) or an
empty-vote tie mis-ejects the consistent node (silent wrong-majority).

## Suggested assertions (all missing — SUT-side SDK instrumentation)

- **Unreachable (SUT-side, gcs_group.cpp:1195 branch):** "vote with non-zero code and no
  payload processed" — placed just before the `std::string err_msg(...)` construction.
  Guards the schema contract; converts a latent SIGSEGV into a property signal (and the fix
  is a one-line NULL check).
- **AlwaysOrUnreachable (SUT-side, in recompute_vote_based_on_error_code):** whenever a
  vote is recomputed from a non-empty source error message, the resulting error-code list is
  non-empty. Message: "vote recomputation preserved at least one error code".
  Rationale for AlwaysOrUnreachable: the recompute path only runs when apply errors occur
  (optional path), but every execution must satisfy the non-degeneracy invariant.
- **Sometimes (SUT-side or log-based, workload oracle):** "inconsistency vote completed and
  ejected the minority" — voting is a rare, high-value semantic state worth a replay anchor;
  the workload should confirm post-vote that the *surviving* nodes agree with each other on
  data content (external checksum — voting itself cannot see silent divergence).
- **Always (workload-side, post-vote):** after any vote-driven eviction, the evicted node's
  data disagreed with the survivors OR the survivors agree among themselves (detects the
  wrong-majority ejection: survivors that disagree with each other after ejecting the
  consistent node).

## Assertion-type rationale

`Unreachable` for the NULL-payload branch: it is an impossible state per the message
schema; any hit is a wire-format or truncation bug. `AlwaysOrUnreachable` for empty-vote:
the path is workload-dependent, but degeneration to the empty string must never happen when
a real error message existed.

## Fault requirements

Needs apply errors that differ per node. Network faults alone rarely cause apply errors;
**disk faults or a workload lever (e.g. divergent grants/schema via RSU, FK games) are the
practical trigger — flag for workload planning**. Message truncation angle benefits from
connection resets (default-on).

## Open questions

None — all three resolved (see Investigation Log). Net effect on the property: the
`Unreachable` at the NULL-payload branch is precisely right (any firing = transport truncation
or foreign sender); the empty-vote collision defect stands exactly as described (it can never
collide with the success vote, only with another empty vote — which is the defect); a
homogeneous 8.4.10 harness negotiates wsrep protocol V7 + GCS protocol 6, so TOI/NBO votes are
code-only while DML votes keep full locale-dependent text (making DML apply errors the
interesting regex-input generator).

### Investigation Log

#### Is the NULL-payload vote reachable from a well-behaved same-version peer?

- Examined: `gcs/src/gcs_core.cpp:1564-1600` (`gcs_core_send_vote`), `gcs/src/gcs.cpp:2627-2700`
  (`gcs_vote`), receiver check at `gcs/src/gcs_group.cpp:1184-1197`.
- Found: the same-version sender always transmits `CodeMsg + payload + trailing NUL` —
  `vmsg_size = cmsg_size + copy_size + 1` even when `data_len == 0` — so `msg->size` is always
  strictly greater than `CodeMsg::serial_size()` and the receiver's `data` pointer is non-NULL
  (worst case an empty string, on which `strlen` is safe). The payload-less encoding exists
  only in the `#if 0`'d "simple code message" alternative (`:1568-1569`, compiled out).
- Conclusion: resolved — a well-behaved 8.4.10 peer can never produce the NULL-payload shape;
  the `Unreachable` assertion is exactly right, and any firing indicates transport-layer
  truncation or a foreign/mixed-version sender.

#### Can the empty-string vote hash collide with code 0 or a legitimate vote?

- Examined: `gcs/src/gcs.cpp:2605-2625` (`compute_vote`), `:2679-2692` (success vote = literal
  0), `gcs/src/gcs_group.cpp:1094-1154` (`recompute_vote_based_on_error_code`).
- Found: every computed vote sets bit 63 (`hash.gather8() | (1ULL << 63)`, comment "never 0
  and always negative"); the success vote is the literal constant 0 and is never hashed. So an
  empty-string recomputed vote structurally cannot equal Success. Collision with a legitimate
  non-empty vote would require a 63-bit MMH3 collision — not constructible as a test target.
  The defect is empty↔empty: two *different* failures whose messages carry no
  `Error_code: \d{4,5}` token recompute to the identical `compute_vote(gtid, -1, "", 0)`.
  Supporting detail: server-side apply errors are formatted by `wsrep_store_error`
  (`sql/wsrep_applier.cc:89-130`) which always embeds `Error_code: %d;` with mysql_errno
  (≥1000, i.e. 4-5 digits), so no-match messages come from non-server-error vote text
  (galera-internal error strings) rather than ordinary DML failures.
- Conclusion: resolved — the workload oracle needs no special-casing for Success; the
  empty↔empty collision remains the target and requires failure modes whose error text lacks
  Error_code tokens on at least two nodes.

#### Which vote protocol version does the harness cluster negotiate?

- Examined: `sql/wsrep_mysqld.cc:137-139` (`wsrep_max_protocol_version = V7`),
  `sql/wsrep_high_priority_service.cc:473-474` (TOI) and `:844-845` (NBO) passing
  `include_msg = wsrep_protocol_version < V7`, `:676`/`:1088` (DML/unordered default
  include_msg=true); `galera/src/replicator_smm_params.cpp:32` (`MAX_PROTO_VER = 11`),
  `gcs/src/gcs_act_proto.hpp:34` (`GCS_PROTO_MAX 6`), vote gating in `gcs/src/gcs.cpp:2631`
  (needs gcs ≥1) and `gcs/src/gcs_group.cpp:1177-1180` (gcs ≥4 min_seqno branch).
- Found: a homogeneous 8.4.10 cluster negotiates wsrep application protocol V7 and repl
  protocol 11 → TOI/NBO error votes are normalized to bare ` Error_code: NNNN;` tokens; DML
  apply errors keep full message text. GCS protocol negotiates 6 → voting enabled, the
  `last_applied`-aware min_seqno branch active. The receive-side recompute
  (`gcs_group.cpp:1198`) is unconditional — not version-gated.
- Conclusion: resolved — harness gets V7/gcs-6 semantics; DML apply errors are the message
  class that exercises the full-text→regex path, TOI/NBO exercise the pre-normalized path.

## Synthesis refinement (2026-09-10)

The strlen(NULL) SIGSEGV Unreachable (gcs_group.cpp:1197) is DROPPED from the catalog invariant — the precondition is undrivable by any planned fault (same-version senders always append >=1 NUL; only transport truncation or a foreign sender reaches it). The empty-vote-recompute and survivor-consistency legs are kept (v2-instrumented + release tier). The NULL-deref remains a candidate upstream report to Percona (see evaluation/synthesis.md, Bias 4).
