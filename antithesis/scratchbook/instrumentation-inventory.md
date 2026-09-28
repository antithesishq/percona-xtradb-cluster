---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-11
external_references:
  - path: https://antithesis.com/docs/reference/sdk/cpp/instrumentation.md
    why: C/C++ coverage instrumentation requirements (clang 13+, trace-pc-guard, /symbols)
  - path: https://antithesis.com/docs/reference/sdk/python.md
    why: Python SDK cataloging requirements (/opt/antithesis/catalog/)
  - path: https://antithesis.com/docs/reference/sdk/assertion_cataloging.md
    why: Why assertion names must be inline constant literals
---

# Instrumentation Inventory — PXC Antithesis Harness (v1)

Produced by `antithesis-setup`. This is the per-service instrumentation decision record
required by the skill's `references/instrumentation.md`. It records what v1 actually
ships, what is deliberately deferred, and the exact flip needed to un-defer it.

## Per-service table

| Service | Language / build | Instrumented? | SDK in dep graph | Catalog / symbols | Bootstrap property |
|---|---|---|---|---|---|
| `pxc-node1/2/3` (`mysqld` + `libgalera_smm.so`) | C++ / cmake (server) + scons (galera), GCC | **No** (deferred — see below) | No | `/symbols` **created and populated** with unstripped `mysqld` + `libgalera_smm.so` (harmless without libvoidstar; ready for the flip) | n/a — asserts are the oracle on this tier |
| `pxc-workload` | Python 3 (Debian python3) | **Cataloging-only** (the only mode Python supports today) | **Yes** — `antithesis==0.3.1` | `/opt/antithesis/catalog/` → one-hop symlink to `/opt/antithesis/workload` | **Yes** — `reachable("workload startup: 3-node cluster reached Synced")` |

## Decision 1 — C/C++ coverage instrumentation is DEFERRED in v1

Confirmed with the user on 2026-09-11; consistent with `deployment-topology.md`
("C/C++ SDK instrumentation of mysqld/libgalera is a later phase").

Why deferred rather than done now:

1. **Toolchain mismatch.** Antithesis C/C++ coverage instrumentation requires
   **Clang 13+** (`-fsanitize-coverage=trace-pc-guard`). `build-ps/build-binary.sh`
   and the PXC/MySQL cmake tree are GCC-oriented; galera builds through **scons**
   with its own flag plumbing. Switching both halves of the build to Clang is a
   nonstandard toolchain change for this codebase and a distinct piece of work from
   standing the harness up.
2. **Build-shape conflicts.** `build-binary.sh` defaults to a **3-pass PGO** build
   (`WITH_PGO=1`) and auto-promotes LTO (`cmake/fprofile.cmake` turns
   `WITH_LTO_DEFAULT=ON` whenever `FPROFILE_USE` is set). Both must be off for
   coverage instrumentation. The harness Dockerfile already disables them.
3. **Oracle density is already high without it.** The v1 tier is *assert-enabled*
   (NDEBUG stripped), which re-arms the entire transaction / monitor / certification
   invariant surface — per `sut-analysis.md` §11 that surface is `assert()`-only and
   is compiled out of every shipped build. That is the densest free oracle in this SUT.

**Cost of the deferral, stated plainly:** no coverage-guided search feedback on the
SUT itself, and **no thread-pausing faults** (they require libvoidstar instrumentation).
Antithesis still drives the system through network/timing/process faults and the
workload, and still reports on the Python-side assertions. Search will be less
directed than it will be once instrumentation lands.

**How to un-defer (single flag).** `antithesis/Dockerfile` takes
`ARG PXC_INSTRUMENT=0`. Setting it to `1` is the intended flip point; the stage
already carries the clang install, the flag plumbing for both cmake and scons, the
`antithesis_instrumentation.h` include hook, and the `/symbols` population. It is
**not validated** — treat turning it on as its own task, expect to iterate on the
clang build, and verify with
`nm /usr/local/pxc/bin/mysqld | grep antithesis_load_libvoidstar`.

### `/symbols` is populated in v1 anyway

Even though v1 is uninstrumented, the runtime image builds with `-g` and
`-Wl,--build-id` and symlinks the unstripped `mysqld` and `libgalera_smm.so` into
`/symbols/`. Reasons: it costs nothing, it makes core dumps and MVD stack traces
readable now, and it removes a moving part from the instrumentation flip later.
Symlinks are **one hop** (`/symbols/mysqld` → the real binary, never a symlink chain),
per the cataloging docs' one-symlink-deep rule.

### Core-dump suppression is patched out

`gu_abort()` calls `setrlimit(RLIMIT_CORE, 0)` + `prctl(PR_SET_DUMPABLE, 0)`
(`galerautils/src/gu_abort.c`), so every Galera self-destruct would otherwise die
core-less — and a container-level `ulimit -c unlimited` cannot undo it, because the
suppression is in-process. The build stage patches `gu_abort.c` to skip the
suppression. **This is a deliberate divergence from field behavior**, applied only to
the test image; do not read a core dump's existence as a field-behavior claim.

## Decision 2 — Python SDK on the workload carries the bootstrap property

The workload container is where v1's SDK integration lives:

- `antithesis==0.3.1` is installed into the image's Python environment.
- `/opt/antithesis/catalog/` is a **one-hop symlink** to `/opt/antithesis/workload`,
  so Antithesis recursively catalogs every `.py` under it.
- The workload entrypoint emits `setup_complete` via `antithesis.lifecycle`, **not**
  via `antithesis/setup-complete.sh` and **not** from a `first_` test command —
  test commands do not start until after `setup_complete` is observed.

### The bootstrap property

In `antithesis/workload/entrypoint.py`, immediately after the readiness gate proves
all three nodes are `Synced` / `Primary` / `cluster_size=3` on a single state UUID:

```python
reachable("workload startup: 3-node cluster reached Synced")
```

It satisfies the skill's requirements: it is a `reachable` (not a business invariant),
it sits in a guaranteed-to-run startup path rather than behind rare behavior, and its
name is an **inline constant string literal** — never concatenated, never passed
through a variable — because cataloging statically scans the source before any run
and a computed name silently breaks it.

## Deferred to `antithesis-workload` (not setup scope)

The supervisor extensions in `deployment-topology.md` that depend on workload-side
data structures are intentionally **not** implemented here, because they have no
meaning until the workload defines the structures they join against:

- post-graceful-shutdown grastate evaluation against the **DDL episode ledger**
  (needs the ledger)
- **gcache page metrics** emission for `gcache-page-files-bounded` (needs the bulk
  transaction driver to make the page store non-vacuous)
- the **log-string scan layer** feeding `fatal-node-terminates-no-zombie`

What the supervisor *does* implement in v1 is the infrastructure those build on:
boot-phase markers, the `--wsrep-recover` / grastate probe, restart accounting with
the field-restart classifier, the hold-down knob, and the workload→supervisor kill
channel. See `antithesis/pxc-node/entrypoint.sh`.

## Validation checklist status

| Check | v1 status |
|---|---|
| Build produced instrumented artifacts | **n/a — deferred by decision** |
| SDK present where assertions are emitted | ✅ workload image (`antithesis==0.3.1`) |
| Bootstrap assertion in a simple path | ✅ workload entrypoint readiness gate |
| `/opt/antithesis/catalog/` populated | ✅ workload image, one-hop symlink |
| `/symbols/` populated | ✅ pxc-node image (unstripped + build-id) |
| `nm ... \| grep antithesis_load_libvoidstar` | **will not match in v1** — expected |
| `Software was instrumented` in first triage report | **will NOT appear for pxc-node** — expected, not a regression |
| Local `compose build` + `snouty validate` | ⚠️ **NOT RUN** — no container runtime in the authoring sandbox; see `antithesis/VALIDATION.md` |

## Addendum 2026-09-28: server asserts as named properties

Until now every `assert()` abort surfaced only in the platform's generic
"No unexpected crashes → `mysqld`" group. Run `c89f2f7a…-63-2` had 76 of them
across 16 sites, with no site named in the report. The supervisor now parses
each non-graceful boot's error log slice for the fatal assert, in both forms:
glibc's `mysqld: FILE:LINE: FUNC: Assertion `EXPR' failed.` and InnoDB's
`Assertion failure: FILE:LINE:EXPR`. It emits two fallback-SDK `Unreachable`
properties:

- `mysqld never aborts on a failed assertion`: declared at startup, so it
  shows passing when nothing aborts.
- `mysqld assertion failed at <file>:<line>`: one per site, built at run time.
  This is the one deliberate exception to the inline-constant id rule. An
  undeclared Unreachable loses nothing, because absent and passing mean the
  same, and the platform evaluates undeclared assertions on first sight.

`/src/` is stripped so server and Galera paths key the same way. The details
carry node, boot, function, expression, and a path-only `component` hint
(galera / wsrep-lib / pxc-wsrep / server). The hint does not replace the
by-caller rule in `triage-scope.md`.

**Why the platform's crash count was low (76 against 179 assert lines).**
mysqld's `handle_fatal_signal` (`sql/signal_handler.cc`) catches the SIGABRT
that `assert()` raises, prints a backtrace, and calls `_exit(2)`. The kernel
sees a normal exit with status 2, so a crash detector that counts deaths by
signal misses most of them. In `c89f2f7a…-63-2` the supervisor recorded 213
status-2 exits and 96 SIGABRT (134) exits, plus 150 `unireg_abort` (1). The
supervisor therefore also emits
`mysqld died on fatal signal <N> without a failed assertion` (plus a declared
umbrella) when a boot's log has `mysqld got signal N` but no assert line. That
case covers SIGSEGV and expression-less aborts. The run had 219 `got signal 6`
lines, 179 assert lines, and 2 `got signal 11`.

**Catch-all, and what still is not caught.** `mysqld never dies on a fatal
path without a diagnosable log line` fails on any boot that exited with status
2 (mysqld's fatal-signal handler, or the GTID out-of-memory `_exit`), on
SIGABRT, or on any signal the harness did not send, i.e. anything but SIGKILL
(kill channel) or SIGTERM (container stop), when neither parser found an assert
or `got signal` line. Still not caught as bugs:
- **`unireg_abort` (exit 1):** 150 in `c89f2f7a`. Mostly documented, such as a
  failed SST as joiner, an inconsistency eviction, or being unable to reach the
  cluster. It still needs a cause allowlist before an undocumented one can fail.
- **SIGKILL from the kernel OOM killer:** indistinguishable from the kill
  channel by exit status alone.
- **Crashes in the side mysqld runs** (`--wsrep-recover`, `--initialize`): their
  logs are not scanned.
- **Hangs:** these are not crashes, and are left to the liveness oracles.
