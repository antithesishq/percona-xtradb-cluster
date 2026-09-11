# cert-interval-reject-symmetry — Evidence

**Property (catalog one-liner):** With cluster-uniform provider certification parameters,
the certification-interval rejection path (`cert_interval > cert.max_length`, or
`last_seen < initial_position`) produces the same verdict on every node for every writeset —
no writeset is silently dummied on some nodes and applied on others.

**Type:** Safety. **Assertion:** `Always` (workload-side cross-node checksum equality at
sync barriers), with a companion `Sometimes` (the interval-rejection branch fired at least
once) to confirm the path is actually exercised. `Always` is the right type because verdict
symmetry must hold on *every* writeset; a single asymmetric verdict is permanent silent
divergence. **Confidence:** high on the mechanism (all code paths below read at commit
f9ecb3e); medium on how hot the rejection branch gets under a uniformly-low `cert.max_length`
(depends on replicate→delivery in-flight depth — but Antithesis network throttling directly
inflates it, see below).

## Mechanism (all paths in percona-xtradb-cluster-galera submodule @13ff9ed6)

- `galera/src/certification.cpp:28-40` — `cert.max_length` / `cert.length_check` param names
  and the in-code warning: *"It is EXTREMELY important that these constants are the same on
  all nodes. Don't change them ever!!!"* (:37-39). Defaults 16384 / 127 (:39-40).
- `register_params` (:43-53): both registered `gu::Config::Flag::hidden` — *"people should
  not know about these dangerous setting unless they read RTFM"*. They are settable per node
  via `wsrep_provider_options='cert.max_length=N;cert.length_check=M'` in my.cnf.
- Read exactly once at construction (:996-997 → `max_length(conf)` / `length_check(conf)`
  helpers :55-75). **Not runtime-mutable:** `Certification::param_set` (:1403-1421) handles
  only `cert.log_conflicts` and `cert.optimistic_pa`, else throws `gu::NotFound`;
  `ReplicatorSMM::param_set` (replicator_smm_params.cpp:225-295) propagates NotFound out, so
  `SET GLOBAL wsrep_provider_options='cert.max_length=...'` errors. Mismatch is therefore a
  *startup* config skew — exactly the shape of version skew inside a single-version cluster.
- **No cross-node agreement:** the GCS state-exchange message carries gcs/repl/appl protocol
  versions (`gcs/src/gcs_state_msg.cpp:154-155,:205`) but no certification parameters —
  confirmed by direct read (this was flagged "reasoned inference; confirm on second pass" in
  sut-analysis §6.2 SG-2.1; now confirmed). `sql/wsrep_check_opts.cc:36-44` validates only 7
  mysqld options, all locally. Nothing detects or refuses a mismatched peer.
- Rejection site: `do_test` (certification.cpp:414-445). After the version check, at
  :430-445: `cert_interval = global_seqno - last_seen_seqno`; TEST_FAILED when
  `last_seen < initial_position_ || cert_interval > max_length_`, with only a `log_warn`
  ("certification interval ... exceeds the limit of ..."). Carve-out :430-431: preloaded
  (`certified()==true`) writesets from IST index rebuild never fail here.
- Consequence of TEST_FAILED: `Certification::test` (:1160-1173) calls `trx->mark_dummy()`
  (:1172). On remote nodes the dummy is "applied" as a no-op in total order
  (`replicator_smm.cpp:2256-2292`); the seqno is consumed. **No inconsistency vote** — votes
  are cast only on apply *errors* (`process_apply_error`, replicator_smm.cpp:1432-1470), and
  a dummy never applies, so it never errors. Divergence is silent.
- Second, subtler divergence channel from the same params: `max_length_` /
  `max_length_check_` also drive certification-index trimming in `append_trx`
  (:1261-1281) — `purge_trxs_upto_(position_ - max_length_)` when the map exceeds
  `max_length_` at a `length_check`-masked position. Different values per node ⇒ different
  index contents ⇒ potentially different *conflict* verdicts even for writesets whose
  interval is below every node's threshold. (Trim is clamped to the group
  safe-to-discard seqno :1267-1275, which bounds but does not eliminate the skew.)

## Failure scenario

Misconfig calibration variant (negative control, run once to validate the oracle): node 3
starts with `cert.max_length=64`, nodes 1-2 default. Concurrent multi-writer load; a lagging
originator replicates trx T whose delivery is delayed (network throttle) so
`cert_interval(T) > 64`. Node 3 certifies TEST_FAILED → dummy → row never written locally;
nodes 1-2 certify TEST_OK → apply; the originating client gets success (its node used the
default). Node 3 now permanently disagrees, keeps voting "success" on everything (it applied
a dummy — nothing failed), and is never ejected. The external checksum property is the ONLY
thing that can catch it — this run doubles as proof the checksum oracle works.

Supported-config property runs: all nodes at identical params. Variant A: defaults. Variant
B: uniformly low (`cert.max_length=512;cert.length_check=15` on every node) so the rejection
and trim paths run hot. In both variants verdicts must be symmetric because
`global_seqno`/`last_seen_seqno` are writeset fields (identical everywhere) and the
thresholds are identical — *except* for `initial_position_`, which is node-local (see open
question).

## Antithesis angle

`last_seen_seqno` is stamped at local replication time (`replicate()` finalize,
replicator_smm.cpp:813); `global_seqno` at total-order delivery. Network
throttling/partition of the originator while other nodes keep committing directly inflates
`cert_interval` — the fault injector *is* the workload amplifier for this branch. Membership
churn (join under load) exercises the `initial_position_` edge. No node termination
required: joins can be driven with `gmcast.isolate` / `wsrep_node_isolation_mode_set_v1`
plus SST/IST re-join, though process-kill restarts widen coverage if enabled.

## Instrumentation suggestions (all missing — no SDK instrumentation exists)

- SUT-side `Reachable`/details event at certification.cpp:437-444 carrying
  `(global_seqno, cert_interval, max_length_)` — enables per-writeset cross-node symmetry
  checking and gives Antithesis an exploration anchor. Interim substitute: scrape the
  release-build `log_warn` "certification interval ... exceeds the limit".
- Workload-side `Always`: after quiescing writes and a `wsrep_sync_wait`-guarded barrier
  (or equality of `wsrep_last_committed` across nodes), per-table `CHECKSUM TABLE` /
  content digests identical on all nodes. This is the ensemble-wide checksum oracle; this
  property is one of its named consumers.
- Workload-side `Sometimes`: the rejection warning observed at least once in variant B.

## Config / fault requirements

- Per-node `wsrep_provider_options` control in the harness config (startup-only knob).
- Network faults (default-on) suffice; multi-writer workload with ≥3 concurrent clients.
- Negative-control variant intentionally violates the uniformity requirement — expect the
  checksum `Always` to fail there; do not ship that variant as a passing property.

## Related negative finding recorded here

`wsrep_certification_rules` (STRICT/OPTIMIZED, sql/sys_vars.cc:8445-8455, dynamic GLOBAL)
has **zero readers** anywhere in this tree — only the definition
(sql/wsrep_mysqld.{h:53-55,97, cc:108}) exists. In MariaDB/upstream this knob changes FK
certification-key conflict verdicts; in PXC 8.4.10 it is an inert variable, so the
"per-node STRICT vs OPTIMIZED changes FK verdicts" skew scenario does NOT exist in this
codebase and no property is built on it.

## Open Questions

- Does Percona's public 8.4 documentation state the uniformity requirement for
  `cert.max_length`/`cert.length_check` anywhere users can see it (the params are hidden by
  design)? If not, the misconfig scenario is field-plausible, raising the value of asking
  Percona whether these should be gossiped/validated at state exchange. `(needs human
  input)` — code and vendored docs exhausted: params are `Flag::hidden`, absent from
  doc/source/wsrep-provider-index.rst, and nothing in gcs_state_msg.cpp exchanges them.

(The `initial_position_` join-asymmetry question is resolved — no membership-quiescent
qualifier needed; see Investigation Log. The invariant stands as stated for uniform-param
configs, joins under load included.)

### Investigation Log

#### Is the "no cross-node gossip of cert params" claim (sut-analysis SG-2.1) real?

- Examined: gcs/src/gcs_state_msg.cpp (full field layout :120-205), grep for `max_length`
  across gcs/ and gcomm/, certification.cpp register_params/param_set,
  replicator_smm_params.cpp param dispatch, sql/wsrep_check_opts.cc.
- Found: state message carries gcs/repl/appl proto versions and flags only; cert params are
  node-local, hidden, ctor-read, runtime-immutable (NotFound from param_set).
- Not found: any validation, gossip, or refusal path for mismatched cert params.
- Conclusion: confirmed — mismatch is undetectable by the SUT; only an external oracle can
  catch the resulting divergence.

#### Can initial_position_ make verdicts asymmetric on a fresh joiner even with uniform params?

(2026-09-10, open-questions pass)

- Examined: `certification.cpp` `do_test` :414-449, `assign_initial_position` :1045-1107,
  `append_trx` trim :1261-1281, `get_safe_to_discard_seqno_` :1178-1191 (min over
  `deps_set_` of in-flight-certified last_seens), `set_trx_committed` :1333-1370;
  `replicator_str.cpp` donor preload-range selection (:673 `preload_start =
  cc_lowest_trx_seqno_`), joiner-side `assign_initial_position` at first preload event
  (:1859, `GTID(uuid, ts->global_seqno()-1)`) and IST CC (:1937); `record_cc_seqnos`
  (replicator_smm.cpp:2550-2563, lowest = `cert_.lowest_trx_seqno()` at the join CC);
  `process_commit_cut` (:2313-2337, totally ordered); `replicate()` last_seen stamping
  (:813 `trx.finalize(last_committed())` inside the gcs schedule/replv loop);
  `gcs_set_last_applied` (gcs/src/gcs.cpp:2563-2597) and `gcs_sm_enter/schedule`
  (gcs/src/gcs_sm.hpp:293-385).
- Found — three mechanisms jointly make joiner/old-member verdicts symmetric:
  1. The joiner's `initial_position_` = donor's `lowest_trx_seqno()` at the join CC − 1.
     The trx map is dense (every certified trx, incl. dummies, is inserted at
     append_trx:1286; dummy preloads at :1318-1330), so after a commit-cut purge to C the
     lowest retained entry is C+1 and `initial_position_ = C`.
  2. Commit-cut purges are delivered and processed in total order
     (`process_commit_cut` under LocalOrder), and the cut is the min over ALL members'
     reported safe-to-discard — including the originator's. The originator's last-applied
     reports travel through the SAME FIFO send monitor as its writesets
     (`gcs_set_last_applied` → `gcs_sm_enter(conn->sm, ..., block=false)`,
     gcs.cpp:2571 — it queues behind an already-scheduled writeset or fails fast with
     "Unable to report last applied ... Will try later"; it never jumps the queue). So a
     cut C > last_seen(W) cannot be delivered before W itself: within a view, any writeset
     delivered after the join CC has `last_seen >= C = initial_position_(joiner)`.
  3. Index-size trims (append_trx:1261-1281) purge to `position_ - max_length_` (clamped
     down by local stds). By arithmetic, any W whose last_seen is below a trim horizon has
     `cert_interval = global(W) - last_seen > max_length_` at delivery (position only
     grows), so the uniform interval check rejects W on EVERY node — including the joiner —
     symmetrically. Per-node trim differences therefore cannot split verdicts under
     uniform params.
  Also noted: upstream comment at append_trx:1250-1256 (codership #733) acknowledges
  "last_seen_seqno is below certification index" occurrences as false-positive warnings —
  consistent with the analysis that sub-horizon last_seens are handled, not divergent.
- Not found / assumptions: per-sender FIFO from send-monitor entry through EVS delivery is
  relied on as gcomm's standard guarantee (not re-proven from gcomm source); the case of a
  writeset surviving its originator's transit through non-primary and being resent with a
  stale last_seen was not traced (local trx are interrupted/aborted when the originator
  drops out of primary, making that path moot in the supported flow).
- Conclusion: resolved — verdicts are symmetric under uniform params even for joins under
  load; the property needs no membership-quiescent qualifier. A checksum failure anchored
  at a join boundary would be a genuine finding, not a known false positive.

#### Does wsrep_certification_rules affect certification in PXC 8.4.10?

- Examined: repo-wide grep (all .cc/.h/.cpp/.hpp/.ic) for `wsrep_certification_rules` /
  `CERTIFICATION_RULES`; ha_innodb.cc FK key-append paths.
- Found: only the sysvar definition and the enum; no consumer.
- Conclusion: inert knob; scenario dropped (recorded above).

## Synthesis refinement (2026-09-10)

REFRAMED as a one-shot oracle calibration: run the uniform cert.max_length=512 exercise once as the negative-control calibration for the cross-node checksum oracle; not a standing property or maintained variant.
