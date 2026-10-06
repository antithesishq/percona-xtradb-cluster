This directory contains files relevant to running tests in Antithesis.

Use the `antithesis-setup` skill to scaffold and manage this directory. Use the `antithesis-research` skill to analyze the system and build a property catalog. Use the `antithesis-workload` skill to implement assertions and test commands. Use the `antithesis-launch` skill to build, validate, and submit Antithesis runs — do not run `snouty launch` directly.

**Validate before every launch: `antithesis/local-validate.sh`**
This is the single pre-launch validation entry point. Run it from the repo root
(`percona-xtradb-cluster/`) before `snouty validate` / the `antithesis-launch`
skill, and after any harness change.

- `bash antithesis/local-validate.sh --offline` runs every check that needs no
  container runtime: shell syntax, Python compile, test-template structure,
  Dockerfile `COPY` sources, and the oracle tests. It takes seconds. Use it on
  a machine with no runtime (like the harness development sandbox).
- `bash antithesis/local-validate.sh [--build]` runs the same offline checks
  first, stops if any fails, then brings the cluster up and runs every test
  command.

It writes one log (`local-validate-<stamp>.log`) and exits non-zero on any
failure. New pre-launch checks go INTO this script (a step in
`offline_checks`, or a section in `main`), not into a separate script an agent
would have to know about.

**snouty launch**
Use the `percona` webhook for pxc runs. Before a launch, check whether the images are current. Rebuild stale ones with `./antithesis/build-images.sh [service...]`. The default launch is:

```sh
snouty launch --json --webhook percona --config antithesis/config \
  --test-name "<name>" --description "<what this run tests>" --duration <minutes> \
  --param custom.include_for_node_termination="pxc-node[123]" \
  --param custom.include_for_node_hang="pxc-node[123]" \
  --param custom.include_for_node_throttle="pxc-node[123]" \
  --param custom.vm_memory_gb=16
```

Network faults hit every container, including `pxc-workload`. Kill, hang and
throttle hit `pxc-node1..3` only, because killing the workload tests the
harness, not PXC. `custom.cpu_modulation` cannot be scoped and also throttles
the workload, so it stays off unless a run asks for it. The other `custom.*`
params (`disable_faults`, `disable_network_faults`, `clock_jitter`,
`smoke_test_seconds`, the `include_all_*` bools) keep the webhook defaults.
`custom.vm_memory_gb` sets the VM memory in GB. The webhook default is 10;
pxc runs pass 16.

**config-variant.sh**
Makes a copy of `config/` with workload switches changed, for one launch:
`./antithesis/config-variant.sh no-levers PXC_LEVERS=off` writes
`antithesis/config-no-levers/`. Launch parameters cannot reach a container, so
this is how a workload switch gets into a run. See `workload/README.md`.

**snouty validate**
Use this command to quickly validate changes to the Antithesis scaffolding. See `snouty validate --help` for details.

**setup-complete.sh**
Inject this script into a Dockerfile to notify Antithesis that setup is complete. This script should only run once the system under test is ready for testing. Antithesis will not run any test commands until it receives this event.

**config**
This directory contains the `docker-compose.yaml` file used to bring up this system within the Antithesis environment, along with any closely related config files. Snouty will push tagged images, consume this config directory, and launch the run.

**scratchbook**
This directory is the Antithesis scratchbook for the codebase. It contains documents such as system analysis, property catalogs, topology plans, per-property evidence files (in `scratchbook/properties/`), property relationship maps, and other persistent integration notes. Keep it up to date as Antithesis-related decisions change. Read `scratchbook/triage-scope.md` before triaging any run: it is the Percona-owned vs upstream-MySQL ownership map, and the customer only wants findings in code Percona controls. Then group every finding by theme with `scratchbook/property-themes.md` (T1 to T9), so triage reports present the same way every time. A new assertion gets a row in that file's triage tables.

**test**
This directory contains test templates. A test template is a directory containing test command executable files. Each test command must have a valid prefix: `parallel_driver_, singleton_driver_, serial_driver_, first_, eventually_, finally_, anytime_`. Prefixes constrain when and how commands are composed in a single timeline. Files or subdirectories prefixed with `helper_` are ignored by Antithesis and can be used for helper scripts kept alongside the commands.

---

## This project (Percona XtraDB Cluster 8.4)

Topology: three `mysqld` nodes plus one Python workload client. See
`scratchbook/deployment-topology.md` for the reasoning behind every choice, and
`scratchbook/instrumentation-inventory.md` for the per-service instrumentation
decision record.

**Dockerfile**
Multi-stage, built from the repo root. Stages: `pxc-build` (compiles PXC from
source — galera via scons, server via cmake), `pxc-base` (runtime OS plus the
compiled install tree), `assert-tiers` (debug-only InnoDB assertion table),
`pxc-node` (`pxc-base` plus the harness, for pxc-node1/2/3), `workload`
(Python test driver). Build with `./antithesis/build-images.sh [service...]`.
It hashes the `pxc-base` inputs and pulls `<repository>/pxc-base:<hash>` from
the registry when it exists, so only a change to PXC source, the submodules,
`antithesis/build/` or the Dockerfile above `# === END OF PXC-BASE INPUTS ===`
compiles. A plain `<engine> compose ... build` still works, but compiles
locally.

`ARG PXC_INSTRUMENT=0` is the flip point for C/C++ coverage instrumentation,
which is **deliberately deferred in v1**. Read
`scratchbook/instrumentation-inventory.md` before turning it on.

**build/**
`patch-sources.sh` — source patches applied inside the build stage. Removes
Galera's in-process core-dump suppression (a deliberate divergence from field
behavior, test image only) and, when instrumenting, adds the instrumentation
header to `sql/main.cc`. Every patch fails the build loudly if the upstream code
moved, rather than silently no-op'ing.

**pxc-node/**
`my.cnf` — shared server config. `entrypoint.sh` — the supervisor: bootstrap
guard, `--wsrep-recover` dance, restart loop, the workload→supervisor kill
channel (served, but unused in v1: the workload container cannot reach
`/opt/antithesis/state`), hold-down knob, and JSONL boot/restart accounting. `notify.sh` — a
`wsrep_notify_cmd` script baked in but not wired up; enabling it needs a config
variant because the variable is READ_ONLY.

**workload/**
Start with `workload/README.md`. It explains the test commands, the swarm
profile, and the **levers**: the SQL administrator actions that the workload
does to the nodes. Levers run in every run, also when the launch turns off
Antithesis faults, so read it before you triage a crash.
`entrypoint.py` — readiness gate, schema seeding, the bootstrap property, and
`setup_complete`. `/opt/antithesis/catalog/` symlinks here for assertion
cataloging, so **assertion names must be inline constant string literals**.

**oracle-tests/**
Runtime-free detection tests for the assertion logic, run with
`bash antithesis/oracle-tests/run.sh`, and run automatically by
`local-validate.sh`. Run them after any change to
`workload/pxcwl/checks.py`, `oracles.py`, `levers.py`, `probe.py`,
`ddl.py` or `pxc-node/entrypoint.sh`. They are detection tests: each one also feeds the *old, broken*
behaviour to the same checker and requires it to be caught, so a green result
distinguishes a working oracle from a blind one — which a smoke test does not.
See `oracle-tests/README.md`.

**VALIDATION.md**
How to build and validate, what was and was not verified, and the open questions
the first runs should answer. Read this before the first build — the local build
and `snouty validate` have not been run yet.
