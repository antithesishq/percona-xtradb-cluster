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

Planned commands for this SUT are described in `../scratchbook/deployment-topology.md`
under "workload (role: client — test driver)".
