---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
updated: 2026-09-23
source: customer directive, relayed 2026-09-23 — "Percona is not interested in
  property failures related to bugs in mysql, they can't control the upstream,
  they want to focus on property failures related to bugs in percona products."
---

# Triage Scope — what counts as a reportable finding

Percona owns the wsrep/Galera stack and the PXC patches on top of MySQL. It does
**not** own stock Oracle MySQL code. A finding that bottoms out in unpatched
upstream MySQL is not actionable for this customer and must be labelled as such
in triage rather than reported as a PXC bug.

This file is the ownership map. Apply it to every crash site, every stack frame,
and every oracle failure before ranking findings.

## Percona-owned (report these)

| Path in the build (`/src/...`) | Repo location | What it is |
| --- | --- | --- |
| `gcs/`, `galera/`, `galerautils/`, `gcache/` | `percona-xtradb-cluster-galera/` | The Galera provider (`libgalera_smm.so`) as Percona ships and forks it |
| `/src/wsrep-lib/` | `wsrep-lib/` | Percona's fork of the wsrep client-state/transaction library |
| `sql/wsrep*.{cc,h}`, `sql/service_wsrep.cc` | `sql/` | PXC's wsrep integration layer |
| any `wsrep_*` symbol in `storage/innobase/` | `storage/innobase/` | PXC's wsrep patch inside InnoDB (cert-key generation, BF aborts) |
| `pxc_*` variables and their handlers (`pxc_maint_mode`, `pxc_strict_mode`) | `sql/` | Percona-only features |
| SST/recovery scripts, packaging wrappers | `scripts/`, `build-ps/` | Percona-authored plumbing |

## Upstream Oracle MySQL (note, do not report as a PXC bug)

Everything else under `sql/`, `storage/innobase/`, `strings/`, `mysys/`,
`libbinlogevents/`, `client/` that carries no `wsrep`/`pxc` marker. PXC 8.4.10-10
rebases on MySQL 8.4.10; a defect reachable from a plain, non-replicated code
path is Oracle's.

## The rule that actually decides it: follow the caller, not the file

A crash inside an upstream file is a **Percona** finding when a `wsrep_*` frame
put it there. This is not hypothetical — it is the shape of the single most
interesting crash in run `2af893bc390d2feb09cad48b26c98bbf-63-0`:

```
#9  my_strnxfrm_uca_900_tmpl      strings/ctype-uca.cc:5111   <- upstream file
#10 wsrep_innobase_mysql_sort     storage/innobase/handler/ha_innodb.cc:8718
#11 wsrep_store_key_val_for_row   storage/innobase/handler/ha_innodb.cc:8863
#12 ha_innobase::wsrep_append_keys storage/innobase/handler/ha_innodb.cc:13209
```

The assert `(dstlen % 2) == 0` is upstream MySQL's; the odd `dstlen` is handed to
it by PXC's certification-key builder. Percona owns the bug.

So, when classifying:

1. Read the whole backtrace, never just the assert line. Log slices stop at the
   failing moment, so grab the moment of the `Trying to get some variables` line
   that follows a crash — that history contains the full trace.
2. Walk the frames outward until the first `wsrep_*` / `pxc_*` / galera frame.
   If one exists below the upstream frame, it is a Percona finding.
3. Only if the entire stack is upstream-to-upstream, and no replication path is
   involved, file it as "upstream MySQL — out of scope" and move on.

### Which crash-dump lines carry information

`print_fatal_signal` (`sql/signal_handler.cc:328`) prints a fixed skeleton. Only
two parts of it are evidence:

| Line | Worth |
| --- | --- |
| ``mysqld: <file>:<line>: <func>: Assertion `...' failed.`` | **the finding** — identifies the site on its own |
| `#0 … #N` frames | **the ownership call** — only needed when the assert file is upstream |
| `Trying to get some variables.` | **a landmark**: first line *after* the last frame, so it is the moment to hand `snouty runs logs` when you need a complete dump |
| `Attempting backtrace…` / `terribly wrong...` / `stack_bottom` / `Thread pointer` | boilerplate, printed unconditionally — never a symptom, never a count |

In run `2af893bc...-63-0`: 538 `terribly wrong`, 538 `mysqld got signal`, 525
frame `#0`, but only 270 `Trying to get some variables` — many dumps end
mid-backtrace. That costs nothing for classification (544 assert lines vs 538
signal lines; essentially every crash carries its own assert text) and matters
only for the upstream-file case, where the frames are the whole argument.

### The death class no crash property counts

The `mysqld` / "No unexpected crashes" property counts **processes terminated by
a signal**. PXC also dies by `unireg_abort(1)` — a controlled exit, status 1, no
signal, no crash dump, no "terribly wrong". The supervisor records these as
`{"event":"mysqld_exited","kind":"unireg_abort","field_would_restart":false}`;
run `2af893bc...-63-0` has 79 such records (one history reached `boot: 9`) on top
of its 119 signal deaths. The shipped systemd unit does not restart status 1, so
in the field these nodes stay down permanently — the worse outcome of the two.

**Covered as of 2026-09-23.** The supervisor now emits fallback-SDK assertions
to `$ANTITHESIS_OUTPUT_DIR/sdk.jsonl` (declared in the catalog at startup, so an
unfired claim still reports):

| Claim | Fires when |
| --- | --- |
| `a node died in a way the shipped systemd unit would not restart` | any non-graceful exit with `field_would_restart:false` |
| `a node died before mysqld reached ready for connections` | the boot never logged "ready for connections" |
| `a node died in a boot whose state transfer had failed` | the boot's log slice shows an SST failure |
| `a node died after the cluster declared it inconsistent` | the boot's log slice shows `Inconsistency detected` |

All four carry `node`, `boot`, `kind`, `status`, `field_would_restart`,
`reached_ready`, `sst_failed`, `inconsistent` and `last_error` — the last being
the final `[ERROR]` line of that boot, which is how you tell a diagnosed death
from a silent one without a pattern list deciding the verdict.

The surviving-node half is in the workload (`probe.py` `_claim_error_log`, via
`performance_schema.error_log`): `a state transfer failed on a node that kept
serving`, `a failed state transfer fell back to IST instead of killing the node`,
and `a node was declared inconsistent and was still serving`. The two halves
cover each other's blind spot — the supervisor cannot see a node that survives,
and the workload cannot query one that died.

**Still uncovered:** the cross-node case, where all three nodes declare
themselves inconsistent at once. No single component observes it: the supervisor
sees only its own node, and the workload usually cannot poll a node between its
verdict and its death. It needs the per-node verdicts accumulated in the journal
and asserted over a window. Until then it shows up as the terminal reconvergence
reds plus three `a node died after the cluster declared it inconsistent` claims
in the same history.

## Corollary for oracle (SDK property) failures

Every property in `workload/pxcwl/oracles.py` is about replication, membership,
or a `pxc_*` feature, so a *genuine* failure of any of them is in scope by
construction. The scope question for oracles is therefore the other one:
**is it a real SUT failure at all, or workload noise?** Rank each counterexample
as one of:

- **SUT** — the cluster violated the invariant.
- **Harness** — the oracle compared something it should not have (e.g. a query
  error string folded into a value), or two workload levers interfered.
- **Undecidable** — the details do not distinguish the two. Fix the details
  first; an undecidable oracle is worth less than no oracle.

Harness-class counterexamples must be fixed before the next run, because they
inflate counterexample counts and can mask the SUT class behind them.
