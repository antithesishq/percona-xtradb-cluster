# Test templates

Owned by the `antithesis-workload` skill — `antithesis-setup` deliberately
defines no test commands.

A test template is a directory of executable test commands, mounted into the
workload container at `/opt/antithesis/test/v1/<template-name>/`. The
`pxc-workload` image already creates that path.

Each command's filename must start with one of: `parallel_driver_`,
`singleton_driver_`, `serial_driver_`, `first_`, `eventually_`, `finally_`,
`anytime_`. The prefix controls when and how Antithesis composes the command
into a timeline. Files and subdirectories prefixed with `helper_` are ignored by
Antithesis and are the right place for shared code.

Do **not** emit `setup_complete` from a test command, including a `first_` one.
Test commands only start after Antithesis has already observed `setup_complete`,
so doing that would deadlock. The workload entrypoint emits it — see
`../workload/entrypoint.py`.

## The `pxc` template

One template, deliberately. Catalog properties are expressed as assertions layered over a
single stream of general-purpose traffic, not as one template or one test case per
property: the workload's job is to exercise the system broadly and pull the levers, and
fault injection's job is to force the interesting states.

| Command | Role |
| --- | --- |
| `first_seed_workload_schema` | Draws the timeline's swarm profile **once**, creates the schema, applies server posture |
| `parallel_driver_traffic` | The traffic generator: 7 action classes plus 10 admin levers |
| `anytime_cluster_probe` | Continuous checks of what each node *claims* against what it *does* |
| `eventually_verify_convergence` | Terminal oracle, faults stopped, drivers killed mid-flight |
| `finally_verify_convergence` | Terminal oracle on timelines where every command completed cleanly |

### Where the code lives, and why it is not here

Every command in this directory is a thin shim. All logic, and **every SDK assertion**,
lives in `../workload/pxcwl/`.

That split is not organisational taste. Assertion cataloging statically scans
`/opt/antithesis/catalog`, which is a symlink to `/opt/antithesis/workload` — it does not
look inside `/opt/antithesis/test/`. An assertion written in a command file here would
never be cataloged, and an uncataloged reach claim that is never hit is *invisible* rather
than failing. So the one signal the workload has for whether it reaches its targets would
be silently lost.

If you add a command, add its logic to `pxcwl/` and keep the command a shim.

### Two things that will bite

**Leases.** An `eventually_` command kills every running command. A driver killed while
holding `gmcast.isolate=1`, `wsrep_desync=ON` or a non-default `pc.weight` would leave the
cluster permanently impaired, and then the convergence assertions fail for a reason that
has nothing to do with PXC. So no lever is ever simply set: it is leased with a deadline
and a restore value (`pxcwl/leases.py`), and **every command repairs expired leases before
doing anything else**. Preserve that property in anything you add.

**Bounds, not exact values.** A write under fault injection can end ACKED, FAILED (clean
rejection, node still alive, provably no trace) or UNKNOWN (connection died mid-COMMIT).
The journal records all three and the oracle checks a band. Asserting an exact expected
count would fire every time the environment dropped a request.

The original research notes for this SUT are in `../scratchbook/deployment-topology.md`
under "workload (role: client — test driver)"; current coverage and known gaps are in
`../scratchbook/property-catalog.md` under "Implementation status".
