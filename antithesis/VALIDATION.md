# Validating the PXC Antithesis harness

**Status: the build and `snouty validate` steps below have NOT been run.**

The harness was authored in a sandbox with no container runtime — `snouty
doctor` reports *"Container runtime not detected: neither podman nor docker is
installed"*, and podman could not be bootstrapped there (no `/etc/subuid`, no
`newuidmap`, no `/run/user`). `snouty validate` runs docker-compose for real, so
it cannot run without an engine either.

Everything that could be checked without a runtime was:

| Check | Result |
|---|---|
| `docker-compose config` parses `antithesis/config/docker-compose.yaml` | ✅ pass (compose v2.36.0) |
| Every service: `container_name == hostname`, no underscores, `platform: linux/amd64`, `init: true`, `NO_COLOR=1`, `image:` set | ✅ pass |
| No `logging:` driver, no `pull_policy:`, no `internal: true` network | ✅ pass |
| Every `depends_on` uses `condition: service_healthy` | ✅ pass |
| No `${VAR}` interpolation anywhere in the compose file (so shell-vs-hermetic resolution cannot diverge) | ✅ pass |
| `bash -n` + `shellcheck -S warning` on all shell scripts | ✅ clean |
| `antithesis/build/patch-sources.sh` against the real `gu_abort.c` and `sql/main.cc` | ✅ patches apply, are idempotent, and fail loudly on a missing/changed pattern |
| **`pxc-node/entrypoint.sh` run end to end against a fake `mysqld`** | ✅ see below |
| `pxc-node/notify.sh` including the `notify-delay` and `notify-fail` knobs | ✅ valid JSONL, multi-line member list flattened, knobs honored |
| `workload/entrypoint.py` imports with the real `antithesis` + `PyMySQL` installed | ✅ clean |
| Antithesis Python SDK signatures match the calls | ✅ `reachable(message, details)` and `setup_complete(details)` verified against installed `antithesis==0.3.1`; both calls execute without error |
| cmake options passed actually exist in this tree | ✅ checked against `CMakeLists.txt` / `cmake/*.cmake`; `WITH_MYSQLX`, `DOWNLOAD_BOOST` and `WITH_BOOST` were removed after confirming this tree does not use them |
| PXB 8.4.0-5 tarball URL resolves | ✅ HTTP 200 |
| Antithesis C++ SDK header URLs at `v0.5.0` resolve | ✅ HTTP 200 |
| Image build | ❌ **not run** |
| `snouty validate` | ❌ **not run** |
| amd64 architecture verification | ❌ **not run** |
| Assertion cataloging actually picking up the bootstrap property | ❌ **not verifiable offline** — the SDK's `python -m antithesis` is a runtime launcher, not an offline cataloger; cataloging is done by the platform scanning `/opt/antithesis/catalog/`. Confirm from the first triage report. |

### What the supervisor test covered

`pxc-node/entrypoint.sh` was run against a stub `mysqld` that emulates
`--initialize-insecure`, `--wsrep-recover` and a long-running server. Verified:

- datadir initialization on first boot;
- bootstrap happens on boot 1 only — boots 2 and 3 took the join path, so the
  marker guard against accidental re-bootstrap works;
- the `--wsrep-recover` parser extracts the position from the **bracketed
  `[WSREP]`** form (topology open question 2) and passes it as
  `--wsrep_start_position`;
- the workload→supervisor kill channel delivers `SIGKILL` to the live mysqld and
  is attributed to the correct boot number;
- restart accounting classifies correctly: `SIGKILL` → `kind=crash`,
  `field_would_restart=true`; clean exit → `kind=graceful`,
  `field_would_restart=false`;
- the hold-down marker suppresses the restart and releases on removal;
- `SIGTERM` forwards to mysqld, does **not** trigger a restart, and the
  supervisor exits 0;
- a `SIGTERM` sent to mysqld alone (not to the supervisor) is classified
  `kind=graceful` and *does* trigger a restart into the next boot;
- mysqld's error-log lines reach container stdout, exactly once per boot — the
  single persistent `tail -F` follows the file across restarts without
  duplicating output;
- the supervisor leaves **no orphaned processes** behind after exit;
- all emitted JSONL lines parsed as valid JSON, in the container log and in
  `$ANTITHESIS_OUTPUT_DIR/supervisor.jsonl`.

This exercises the supervisor's control flow, not its interaction with a real
mysqld — timing around SST, IST and the `pxc_maint_transition_period` shutdown
sleep is still unverified.

Run the rest on a machine with a container engine.

## 1. Check the engine

```sh
snouty doctor
```

Note which engine and which compose CLI it reports; use those below. snouty
prefers podman when both are installed, and the two keep **separate image
stores** — export `SNOUTY_CONTAINER_ENGINE=docker` if you build with docker on a
machine that also has podman.

With podman, confirm the compose provider is the real Compose v2 binary and not
the `podman-compose` Python tool, which is incompatible with features this
harness needs:

```sh
podman compose version   # must print "Docker Compose version ..."
```

## 2. Build

From the repo root (`percona-xtradb-cluster/`):

```sh
docker compose -f antithesis/config/docker-compose.yaml build
```

**Expect this to take a long time.** It builds PXC 8.4 from source: galera via
scons, then the full server via cmake. Budget an hour or more on a cold cache
even on a large machine. Run it in the background rather than in a foreground
shell that can time out.

Things worth knowing before the first attempt:

- The build context is the whole repo (~840 MB minus `.git`, which
  `.dockerignore` excludes). The first context upload is slow.
- Boost is **not** downloaded — `cmake/boost.cmake` pins boost 1.84.0 and always
  uses the copy bundled at `extra/boost/` in this tree.
- Percona XtraBackup 8.4.0-5 **is** downloaded from `downloads.percona.com`, and
  the Debian packages come from `deb.debian.org`, so the **build** needs
  internet access. The Antithesis **runtime** does not — nothing is fetched at
  container start.
- PGO and LTO are deliberately off. `build-ps/build-binary.sh` defaults to a
  3-pass PGO build; this Dockerfile does a single pass on purpose.
- All 72 package names in the Dockerfile were checked against the real
  `bookworm/main` binary index and resolve. (The first build attempt failed on
  `libcheck-dev`, which does not exist on Debian — the C unit-test framework
  ships as plain `check`, headers included, with no separate `-dev`. It is
  required even though galera's unit tests are never built, because
  SConstruct's Configure block `Exit(1)`s on a missing `check.h` regardless of
  which targets you request.) Names resolving is not the same as the set being
  *sufficient* — if cmake or scons still reports a missing header, add the
  matching `-dev` package to the `pxc-build` stage.

The build has self-checks that fail loudly rather than silently producing a
useless image:

- the `gu_abort.c` core-dump patch fails the build if the upstream code moved;
- `libgalera_smm.so` and `mysqld` are each checked for an `__assert_fail`
  reference, so a silently-NDEBUG build cannot pass;
- `mysqld` is additionally checked for `_db_enter_`, proving the DBUG library
  was actually linked — the desync that broke three earlier builds;
- with `PXC_INSTRUMENT=1`, `mysqld` is checked for `antithesis_load_libvoidstar`.

### Build findings so far

Failures hit and fixed during the first real build attempts. Several are
recorded here because they contradict things the research scratchbook assumed;
finding 6 in particular changes the v1 image tier.

**1. `libcheck-dev` does not exist on Debian.** The C unit-test framework is
packaged as plain `check`. Required even though galera's unit tests are never
built, because `SConstruct`'s `Configure` block `Exit(1)`s on a missing
`check.h` regardless of the requested targets.

**2. The scons galera build needs `-DGALERA_LOG_H_ENABLE_CXX`.** In C++
translation units, `gu_log.h` guards the `gu_fatal`/`gu_error`/`gu_warn`/
`gu_info` macros behind
`#if !defined(__cplusplus) || defined(GALERA_LOG_H_ENABLE_CXX)`. galera's
**cmake** build defines it globally (`cmake/common.cmake:10`); the **scons**
build defines it only for `gcs/` (`gcs/src/SConscript:19`). But C++ code under
`gcache/` and `gcomm/` calls `gu_error()` anyway — `GCache::free()`'s catch
block, `FairSendQueue::front()`/`back()` — so those modules fail to compile.

Verified by compile test against the real sources with the actual build flags:
`gcomm/src/defaults.cpp` and `gcache/src/GCache_memops.cpp` both fail without
the define and compile with it, and **`NDEBUG` is irrelevant** — they fail with
and without it.

That last point matters: this was *not* caused by the assert-enabled
(`debug=3`) choice, and `deployment-topology.md` Assumption 1 —
"`build-ps/build-binary.sh` works against this checkout" — **is false for the
galera half**. `build-binary.sh` does not set this define anywhere, so its
scons path cannot compile this galera revision as-is. The likely explanation is
that Percona now builds galera through cmake (PXC's top-level `CMakeLists.txt`
does `ADD_SUBDIRECTORY(percona-xtradb-cluster-galera)`), leaving the scons path
stale. Treat `build-binary.sh` as a reference, not a working build.

**3. `revno=` was being fed a key=value file.** `GALERA_VERSION` holds
`GALERA_VERSION_MAJOR=4` style lines, not a revision string, and scons bakes
`revno` straight into `-DGALERA_REV="..."` (`galera/src/SConscript:61`) — a
multi-line value there is an unterminated string literal. Now parsed into
`version=4.27`, with `revno` opt-in via
`--build-arg GALERA_REVISION=$(git -C percona-xtradb-cluster-galera rev-parse --short HEAD)`.
Left empty by default rather than hardcoding a SHA that would drift when the
submodule moves; the provider then reports revision `XXXX`.

**4. `WITH_AUTHENTICATION_LDAP=OFF` is required on Debian.** It is a strictness
flag ("Report error if the LDAP authentication plugin cannot be built"), not a
feature switch, and it defaults ON. Configure hard-fails because
`WARN_MISSING_SYSTEM_SASL` only passes when the SASL **SCRAM plugin** is
installed (`libsasl2-modules-gssapi-mit`) — `libsasl2-dev` alone is not enough.
Disabled rather than installing that package, because `cmake/sasl.cmake` itself
says the scram plugin "is not needed for build, but it is needed for testing",
i.e. it gates MTR's LDAP auth tests that this image never runs, and nothing in
the property catalog touches LDAP/SASL auth. Reversible: drop the flag and add
the package if LDAP auth plugins are ever wanted.

**5. cmake was building galera a second time, redundantly.** `WITH_WSREP=ON`
adds *two* subdirectories — `wsrep-lib` (required; statically linked into
mysqld) and `percona-xtradb-cluster-galera` (not required). The provider is a
runtime `dlopen()` target: `sql/CMakeLists.txt` has no galera reference, and no
cmake target outside `percona-xtradb-cluster-galera/` references
`galera_smm`/`galerautilsxx`. So cmake was building a second, NDEBUG copy of
the provider — plus galera's unit tests (`galera/tests` → `galera_check`) —
that this image then overwrites with the scons-built assert-enabled one.
`patch-sources.sh` now comments out that single `ADD_SUBDIRECTORY` line,
keeping `wsrep-lib`. Saves several minutes and removes a failure surface that
could never affect the shipped artifact.

**6. The assert-enabled tier must use `CMAKE_BUILD_TYPE=Debug`, not
RelWithDebInfo-minus-NDEBUG.** This took three link failures to establish, and
it is the most important build finding here.

The goal was an assert-enabled *optimized* build. The obvious route — keep
RelWithDebInfo and strip `NDEBUG` — **cannot work**, because this codebase
decides what to *compile* from the build type and from flag substrings, never
from the effective macro state. Each of these is an independent desync, and
each surfaced only after ~20 minutes of building:

| Gate | Condition | Symptom when it desyncs |
|---|---|---|
| `mysys/CMakeLists.txt:148` | flags string `MATCHES "DNDEBUG"` → drop `dbug.cc` | hundreds of undefined `_db_*` refs |
| `sql/CMakeLists.txt:653` | flags string `MATCHES "DENABLED_DEBUG_SYNC"` → compile `debug_sync.cc` | `'debug_sync_set_action' was not declared` |
| `storage/heap/CMakeLists.txt:54` | `CMAKE_BUILD_TYPE_UPPER STREQUAL "DEBUG"` → compile `_check.cc` | undefined `heap_check_heap` |
| `storage/innobase/innodb.cmake:88` | appends `-DUNIV_DEBUG` to `CMAKE_CXX_FLAGS_DEBUG` | undefined `ib_interpreter_check/update` (`ut0test.cc` body is `#ifdef UNIV_DEBUG`) |

**The heap one is decisive: it tests the literal build type, so no combination
of compiler flags can ever satisfy it.** Meanwhile the headers emit calls to
all of these symbols the moment `NDEBUG` is absent. "RelWithDebInfo minus
NDEBUG" is a configuration the vendor never builds and that cannot be assembled
by hand — chasing it is unbounded whack-a-mole.

**Resolution:** use the build type the vendor *does* build, and change only the
optimization level. `PXC_BUILD_TYPE=Debug` with
`CMAKE_{C,CXX}_FLAGS_DEBUG="-g -O2"` keeps every debug define the build system
adds for us — final flags come out as
`-DENABLED_DEBUG_SYNC -g -O2 -DUNIV_DEBUG` — while replacing cmake's implicit
`-O0`. PXC supports Debug builds (`build-binary.sh --debug`; `CMakeLists.txt:1566`
deliberately enables `ENABLED_DEBUG_SYNC` while leaving `SAFE_MUTEX` off
specifically for PXC). We now guess one thing (the optimization level) instead
of an entire configuration.

Honest caveat: **Debug-at-`-O2` is still not a vendor-tested combination.** If
it misbehaves, `--build-arg PXC_OPT_FLAGS="-g -O0"` gives the vendor-exact
build. The image verifies the outcome rather than assuming it: it checks
`mysqld` for both `__assert_fail` (assert live) and `_db_enter_` (DBUG actually
linked — the precise thing that desynced three times).

**What this changes for the tier plan.** `deployment-topology.md` described
three tiers, of which the v1 one no longer exists as described:

- **DBUG is on.** In 8.4 there is no separate `DBUG_OFF`; `my_dbug.h` gates
  DBUG on `NDEBUG` directly (lines 40, 57, 298). Assert and DBUG are the same
  switch. Runtime `--debug` keywords are available, at the cost of a
  per-traced-function branch on `_db_enabled_()`.
- **DEBUG_SYNC is on**, at `-O2`. The topology listed it as Debug-tier-only for
  manufacturing hard IST/donor preconditions; those are now reachable on the
  fast tier. Sync points stay inert until set, and `opt_debug_sync_timeout`
  defaults to 0.
- **`UNIV_DEBUG` is on**, so InnoDB's own debug instrumentation is live too.
- **Throughput must be measured, not inherited.** This is a far heavier binary
  than the topology's "optimized code with live assert()" assumed. Treat the
  first run of this tier as the calibration run and pin no numeric bound before
  it.
- **The release tier got easier.** `PXC_BUILD_TYPE=RelWithDebInfo` now yields a
  clean NDEBUG release build — the "later: release image" tier — precisely
  because we stopped trying to strip NDEBUG out of it.

**7. `WITH_NDB=OFF`.** MySQL Cluster (NDB) is a large, wholly separate storage
engine with no relationship to galera replication; PXC uses InnoDB and nothing
in the property catalog touches NDB. It was building by default, costing
substantial time.

**8. `MYSQL_MAINTAINER_MODE=OFF` — the other half of the Debug-at-`-O2`
caveat.** `CMakeLists.txt:660-666` turns maintainer mode ON automatically for
`CMAKE_BUILD_TYPE=Debug` + GCC, and `cmake/maintainer.cmake:236` makes that
mean exactly one thing: `-Werror`. Nothing else is gated on it.

GCC's interprocedural warnings depend on optimization, so the three
configurations diverge:

| Config | Flags | Result |
|---|---|---|
| vendor Debug | `-O0` + `-Werror` | warnings never fire — clean |
| vendor RelWithDebInfo | `-O2`, no `-Werror` | warnings fire, not fatal |
| **this tier** | `-O2` + `-Werror` | **worst of both — build dies** |

Two files died, both known-benign GCC false positives that only appear once
inlining happens:

- `utilities/innochecksum.cc` — `-Werror=array-bounds` on
  `Prealloced_array<byte, 1>`, via `mach_read_from_2` inlined into `main`.
- `sql/mysqld.cc:8022` — `-Werror=stringop-truncation` on
  `strncpy(server_uuid, uuid.c_ptr(), sizeof(server_uuid))`. `server_uuid` is
  `char[37]` and a UUID is 36 chars, so it fits and *is* NUL-terminated; GCC
  simply cannot prove the source length.

Disabling `-Werror` fixes the whole class at once instead of chasing
`-Wno-error=` flags one ~50-minute build at a time. Warnings still print. We
are not MySQL maintainers, and we deliberately chose a non-vendor optimization
level, so treating their maintainer warnings as build-fatal is not ours to
inherit.

**A reporting fix, not a build fix:** `make -j20` interleaves output, so a
failing target's error scrolls away and the top-level `make: *** [Makefile:166:
all] Error 2` says nothing about what broke. The build now runs with
`--output-sync=target` and, on failure, re-runs `make -j1 --output-sync=target`
to surface the real failing target. The retry is incremental, so it stops at
the true failure rather than rebuilding everything.

The sibling strictness options were checked and left alone:
`WITH_AUTHENTICATION_KERBEROS` only warns (`CMakeLists.txt:2053-2060`), and
`WITH_AUTHENTICATION_WEBAUTHN` is satisfied by bundled libfido2 plus
`libudev-dev`.

**If the scons path turns out to be broken in further ways**, the fallback is to
build galera via its cmake path instead, which is known-good. Cost: galera's
`cmake/compiler.cmake` calls `add_definitions(-DNDEBUG)` unconditionally for
non-Debug builds, and `add_definitions` lands *after* `CXXFLAGS`, so `-UNDEBUG`
cannot override it — keeping asserts would require patching that file, which is
exactly why scons with its first-class `debug=3` knob was preferred.

**A sharp edge to know about `SConstruct` env plumbing** (lines 229-235), since
it is easy to break the build silently:

| scons var | comes from | default | safe to set? |
|---|---|---|---|
| `CPPFLAGS` | `$CPPFLAGS` | `''` | yes |
| `CXXFLAGS` | `$CXXFLAGS` | `''` | yes |
| `CFLAGS` | `$CFLAGS` | `''` | yes |
| `LINKFLAGS` | **`$LDFLAGS`** | `link_arch` | yes — note the name |
| `CCFLAGS` | `$CCFLAGS` | **`opt_flags + compile_arch`** | **never** |

Setting `CCFLAGS` would `Replace` the default and silently discard `opt_flags`,
taking the whole `debug=`/NDEBUG decision and the arch flags with it. And
because link flags come from `LDFLAGS`, an exported `LINKFLAGS` is ignored —
the Dockerfile originally made that mistake, so `-Wl,--build-id` never reached
the galera link. Fixed.

## 3. Verify architecture

Antithesis runs on x86-64. `platform: linux/amd64` is set on every service, but
verify the result — on an ARM host a missing or ineffective platform directive
produces arm64 images that fail only at launch:

```sh
for img in pxc-node:latest pxc-workload:latest; do
  printf '%s: ' "$img"
  docker image inspect "$img" --format '{{.Architecture}}'
done
```

Both must print `amd64`.

## 4. Verify image contents

```sh
# Symbols (one hop, pointing at real unstripped binaries)
docker run --rm --entrypoint ls pxc-node:latest -lL /symbols

# The provider really is assert-enabled
docker run --rm --entrypoint bash pxc-node:latest -c \
  "nm -C /usr/local/pxc/lib/libgalera_smm.so | grep -c __assert_fail"

# XtraBackup is where the SST script looks for it
docker run --rm --entrypoint bash pxc-node:latest -c \
  "/usr/local/pxc/bin/pxc_extra/pxb-8.4/bin/xtrabackup --version"

# Cataloging directory resolves to the workload source
docker run --rm --entrypoint ls pxc-workload:latest -lL /opt/antithesis/catalog
```

In v1 `nm /usr/local/pxc/bin/mysqld | grep antithesis_load_libvoidstar` will
find **nothing**. That is expected — C/C++ coverage instrumentation is
deliberately deferred; see `scratchbook/instrumentation-inventory.md`.

## 5. Validate

```sh
snouty validate antithesis/config
```

This brings the system up locally and watches for `setup_complete`. It should
show the three nodes reaching Synced and then the workload emitting
`setup_complete`.

`snouty validate` also discovers test commands under `/opt/antithesis/test/v1`.
This skill defines none — the directory exists but is empty, which is expected
until `antithesis-workload` runs.

If `setup_complete` is never observed:

```sh
docker compose -f antithesis/config/docker-compose.yaml logs pxc-node1
docker compose -f antithesis/config/docker-compose.yaml logs pxc-workload
```

The supervisor emits JSONL boot-phase markers (`boot_start`, `boot_mode`,
`mysqld_exec`, `mysqld_exited`, `grastate`) on stdout, so a stall is
attributable to a phase rather than showing up as an unexplained timeout. The
workload logs the per-node wsrep state every 10 seconds while it waits.

If your engine runs in a VM (Docker Desktop, podman machine), `snouty validate`
watches for `setup_complete` through a bind-mounted temp directory — set
`SNOUTY_TEMP_DIR` to a path the VM shares, or it will never see the event even
though the system came up fine.

## 6. Launch

Use the `antithesis-launch` skill rather than calling `snouty launch` directly.
`snouty doctor` currently reports `repository is not set`; run `snouty login` or
export `ANTITHESIS_REPOSITORY` before launching. Rebuild images first if
anything changed.

Start short — 15-30 minutes — to confirm the system comes up, then read the
triage report for:

- the bootstrap property `workload startup: 3-node cluster reached Synced`;
- session errors under `No Antithesis session errors` (symbolization problems
  show up here);
- `Software was instrumented` under `Setup` — this will **not** appear for
  `pxc-node` in v1, by design.

## Open items carried from research

These are known unknowns from `deployment-topology.md` that the first runs
should answer, not defects in the harness:

1. Does the assert-enabled (non-NDEBUG) build boot and stay up? If it proves
   unstable, fall back to `PXC_BUILD_TYPE=Debug`.
2. Do Antithesis-restarted containers keep their compose-assigned static IP? If
   not, gcomm's one-shot address resolution becomes ambient noise in every
   restart scenario and needs a mitigation decision.
3. Does a graceful container stop under `stop_grace_period: 90s` actually yield
   a clean shutdown (grastate seqno != -1)?
4. Is `gcache.size=16M` giving a healthy IST/SST mix, or all-SST? Tune after the
   first runs.
5. Is `GU_DBUG_SYNC` actually available? The galera build sets `dbug=1`
   (`-DGU_DBUG_ON`), so one runtime
   `SET GLOBAL wsrep_provider_options='dbug=...'` answers it.

---

# Workload validation (antithesis-workload, 2026-09-22)

## What was verified, and how

All of this ran without a container runtime — see the gap below for why.

1. **Assertion cataloging, by replicating the platform scanner.** An AST pass over
   `workload/` looking for SDK assertion calls with literal messages, the same shape the
   platform's pre-run scan uses: **32 assertions, 0 duplicate property names, 0 dynamically
   built messages**. This is a replication of the scanner, not the scanner itself — run
   `python -m antithesis.catalog /opt/antithesis/workload -o catalog.json` inside the built
   image to confirm against the real one.
2. **SDK API surface confirmed against the published source**, rather than assumed:
   `always(condition, message, details)`, `always_or_unreachable(...)`,
   `sometimes(...)`, `reachable(message, details)`, `unreachable(message, details)`;
   `antithesis.random` exposes `get_random()` and `random_choice(list)`.
   **There are no rich numeric assertion forms** in the Python SDK, so the two counter
   bounds are written as plain `always` with the operands in the details.
3. **Logic tests** (stubbed SDK + driver): randomness helpers including the all-zero-weight
   case, swarm draw and its traffic-class clamp, node skew, the three-state ack protocol,
   counter bounds as a band, swarm round-trip through SQLite, lease exclusion, lease repair,
   and `force_all` reclamation.
4. **Swarm distribution measured**, not eyeballed: each action class is zeroed in ~28% of
   timelines and each lever in ~50%, matching the weight bags.
5. **Reconcile merge-join tests**, including the false-positive guards: an unknown outcome
   present *or* absent is not a violation; an unresolved ATTEMPTED row is treated as
   unknown; and **a node whose scan dies mid-way is excluded rather than reported as data
   loss**.
6. **Every operation, DDL shape and lever forced individually** against a fake MySQL that
   validates parameter counts — all 29 execute cleanly. This is what catches a bad
   placeholder count in a rarely-drawn shape.
7. **All five commands exit 0 against a totally unreachable cluster**, firing only the one
   honest assertion (the cluster did not reconverge). Fault injection produces this state
   constantly, and a command that crashed or exited non-zero here would report a fault as a
   bug.
8. **No false positives on a healthy cluster**: all 12 terminal assertions green in both
   `eventually` and `finally` modes.
9. **The oracle demonstrably detects.** Six divergences injected per-node, each caught by
   exactly the right property: a silently lost acknowledged write, a content checksum
   mismatch, a `gtid_executed` mismatch, a schema mismatch, a counter below the acked floor,
   and a counter above the attempted ceiling (double apply).

## Gap: the image build and `snouty validate` did NOT run

Podman is installed and `snouty doctor` reports "Container runtime: podman detected", but
podman **cannot execute in this sandbox**: there is no `/etc/subuid`, no passwd entry for
uid 1000, and no container storage. `snouty doctor` only detects the binary. The successful
build recorded in `pxc-build.log` (2026-09-22 04:37) came from a different environment.

So these remain to be run where podman works:

```
docker compose -f antithesis/config/docker-compose.yaml build
# the real cataloging scanner
docker run --rm --entrypoint /opt/antithesis/venv/bin/python3 pxc-workload:latest \
  -m antithesis.catalog /opt/antithesis/workload -o /tmp/catalog.json
# the commands are present, executable, and their shebang resolves
docker run --rm --entrypoint /bin/bash pxc-workload:latest -c \
  'ls -l /opt/antithesis/test/v1/pxc/ && head -1 /opt/antithesis/test/v1/pxc/*'
snouty validate ./config
```

Structural checks that *were* possible without a runtime: every `COPY` source exists
relative to the build context, all five commands carry a recognized prefix (2 are
driver-or-anytime, satisfying snouty's requirement), all are mode 0755, and every shebang
points at the venv interpreter the image creates at `Dockerfile:657`.

One behavioural check is worth running early because it is the failure mode most likely to
waste a whole run: start a driver, `SIGKILL` it while it holds `gmcast.isolate`, then run
`eventually_verify_convergence` and confirm it restores `gmcast.isolate=0` and reaches a
quiesced comparison.

## Expect the four known Galera asserts to dominate

Run `b36ed60620f2dc4f939738f3f44d0906-63-0` crashed mysqld in 21 histories from network
faults against an *idle* cluster. With real traffic they will fire more, and a crashed node
makes the reconvergence assertion fail secondarily. The reconvergence and lineage details
payloads carry per-node state, cluster size and unreachable reasons so triage can attribute
it at a glance. Consider a first run with all ten `admin_lever_weight`s forced to 0, which
separates workload-induced from fault-induced aborts before the levers add variables.

## Fresh-context review, and what it found (2026-09-22)

A reviewer with no prior context was given the skill definition and the code and asked to
find assertions that can fire when PXC is *correct*. It found one severe bug and a set of
real false-positive risks. All are fixed; the counts above reflect the pre-review state, and
the catalog now holds **35 assertions** (16 `always`, 5 `always_or_unreachable`, 13
`reachable`, 1 `unreachable`).

**The severe one — missing `ROLLBACK`.** Four operations called `conn.begin()` but issued no
rollback on error. MySQL rolls back only the failing *statement* on a lock-wait timeout or
certification conflict; the transaction stays open, `SELECT 1` still succeeds so the session
is reused, and the next `BEGIN` or any DDL **implicitly commits whatever had already
landed**. Writes the journal recorded as FAILED would really be there, firing "a cleanly
failed write is absent" and the counter ceiling on a correct cluster. Fixed centrally in
`ops._rollback_quietly`, called from `_finish` so no future operation can forget, with a
regression test that also runs the control case: without the rollback, two "failed" writes
commit anyway.

Also fixed:

| Finding | Fix |
| --- | --- |
| `no_unresolved_ddl` fired whenever `eventually_` killed a driver mid-DDL | A DDL episode left open by a *killed* invocation is unknowable, not unresolved — the same treatment the ack journal already gave a killed write. `invocation.ended_at` distinguishes them; the abandoned count is reported, never asserted. |
| `leases.release` deleted by lever name only | A driver whose lease had already been reclaimed could delete the **new** holder's lease and token, after which that holder's lever survived the whole terminal verification. Both deletes now carry an `inv_id` guard. `graceful_shutdown` made this near-certain: its body ran ~310s against a 240s deadline, so its hold is now derived from its actual budget. |
| `acquire` wrote the token and the lease in two transactions | Between them "token implies lease" was false, and a repair pass read that as a leak and freed the token — letting two disruptive levers run at once, the one thing the module exists to prevent. Now one transaction. |
| `classify` mapped an unrecognised errno to FAILED | FAILED asserts no-trace, which an unknown errno cannot support. UNKNOWN is the sound default. |
| `locking_read` failed the property on any unrecognised errno | Inverted: only an explicitly fatal set fails. This also closed a live path — `ER_NO_SUCH_TABLE` on a schema that was never seeded. |
| `seed` published the swarm profile before creating the schema | Drivers treat a published profile as "timeline ready", so a failed seed set every driver running against tables that did not exist. Schema first now. |
| `applier_resize_converged` could fire for unrelated reasons | It holds no disruption token, so a concurrent `graceful_shutdown` on the same node invalidated the window. Now requires the last poll to have succeeded and the setpoint to still be ours, and asserts `>=` rather than `==` on the thread count. |
| `saw_large_writeset` served two callsites, falsely at one | A hundred small witness inserts is ~7KB, not 4MB. Split into `saw_long_transaction`. |
| `strict_mode_window` changed three nodes under a one-node lease | Levers in `leases.CLUSTER_WIDE` restore every node, so a killed driver no longer leaves two nodes PERMISSIVE for the rest of the run. |
| A SQLite lock timeout exited a command non-zero | Every entry point now has a top-level guard returning 0. Exit codes carry a built-in property, so non-zero must mean a real bug. |
| `recv_queue_bounded` was unfirable at high `fc_limit` | Absolute cap alongside the multiple: 500 × 100 allowed 50,000 queued writesets. |
| A traffic-free timeline read as a clean run | The vacuity guard is now gated on the journal holding at least one write. |
| `wl_witness` was unbounded on volume-less containers | Capped per timeline; `wl_bulk` and binlogs were already bounded. |

Two gaps the review closed that were genuine holes in coverage, not just risk:

- **Nothing compared the results of DDL across nodes.** Every DDL target is a scratch table
  outside the checksum set, and a missing table yields an empty column signature — so a
  table present on one node and absent on another scored as *identical*. Added
  `checks.compare_table_set`, comparing `information_schema.tables` and `.statistics` for
  the whole schema. It is now the detector for a TOI divergence, and it caught an injected
  one in test.
- **`sync_wait_read` computed the cross-node causality claim and discarded it**, keeping only
  a tally. It is now asserted. This is the only *continuous* cross-node check in the
  workload; the terminal oracle runs once, at the end, and cannot substitute for it.

One review finding was incorrect: it reported no `.dockerignore` guarding against
`__pycache__` reaching the catalog tree. `percona-xtradb-cluster/.dockerignore` (the build
context root) already excludes `**/__pycache__` and `**/*.pyc`, with a comment explaining
exactly that risk.

Detection is now demonstrated for seven injected divergences: a silently lost acknowledged
write, a content checksum mismatch, a `gtid_executed` mismatch, a column-definition
mismatch, a table-set mismatch, and the counter breaching its floor and its ceiling.

