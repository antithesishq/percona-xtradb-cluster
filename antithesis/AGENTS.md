This directory contains files relevant to running tests in Antithesis.

Use the `antithesis-setup` skill to scaffold and manage this directory. Use the `antithesis-research` skill to analyze the system and build a property catalog. Use the `antithesis-workload` skill to implement assertions and test commands. Use the `antithesis-launch` skill to build, validate, and submit Antithesis runs — do not run `snouty launch` directly.

**snouty launch**
Use `snouty launch --json --webhook basic_test --config antithesis/config` to start an Antithesis run. Always run `compose build` first to ensure images are up to date.

**snouty validate**
Use this command to quickly validate changes to the Antithesis scaffolding. See `snouty validate --help` for details.

**setup-complete.sh**
Inject this script into a Dockerfile to notify Antithesis that setup is complete. This script should only run once the system under test is ready for testing. Antithesis will not run any test commands until it receives this event.

**config**
This directory contains the `docker-compose.yaml` file used to bring up this system within the Antithesis environment, along with any closely related config files. Snouty will push tagged images, consume this config directory, and launch the run.

**scratchbook**
This directory is the Antithesis scratchbook for the codebase. It contains documents such as system analysis, property catalogs, topology plans, per-property evidence files (in `scratchbook/properties/`), property relationship maps, and other persistent integration notes. Keep it up to date as Antithesis-related decisions change. Read `scratchbook/triage-scope.md` before triaging any run: it is the Percona-owned vs upstream-MySQL ownership map, and the customer only wants findings in code Percona controls.

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
source — galera via scons, server via cmake), `pxc-node` (runtime for
pxc-node1/2/3), `workload` (Python test driver). Build with
`<engine> compose -f antithesis/config/docker-compose.yaml build`.

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
channel, hold-down knob, and JSONL boot/restart accounting. `notify.sh` — a
`wsrep_notify_cmd` script baked in but not wired up; enabling it needs a config
variant because the variable is READ_ONLY.

**workload/**
`entrypoint.py` — readiness gate, schema seeding, the bootstrap property, and
`setup_complete`. `/opt/antithesis/catalog/` symlinks here for assertion
cataloging, so **assertion names must be inline constant string literals**.

**VALIDATION.md**
How to build and validate, what was and was not verified, and the open questions
the first runs should answer. Read this before the first build — the local build
and `snouty validate` have not been run yet.
