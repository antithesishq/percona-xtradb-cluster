# Workload design

This directory is the PXC test client. It runs in the `pxc-workload`
container. It sends SQL to the three `mysqld` nodes, changes their settings,
and checks what they report. Every SDK assertion lives here, in `pxcwl/`.

Read this file first if you are new to the harness. It explains what the
workload does to the cluster, and what that means when you read a triage
report.

## The test commands

Antithesis runs five commands from `../test/pxc/`. Each command is a thin
shim that calls into `pxcwl/`. See `../test/README.md` for why the code is not
in the command files.

| Command | Module | Job |
| --- | --- | --- |
| `first_seed_workload_schema` | `seed.py` | Draws the timeline's swarm profile one time, creates the schema |
| `parallel_driver_traffic` | `traffic.py` | Sends traffic and pulls levers. Antithesis runs many copies at the same time |
| `anytime_cluster_probe` | `probe.py` | Compares what each node reports with what it does |
| `eventually_verify_convergence` | `verify.py` | Final check after Antithesis stops faults and kills the drivers |
| `finally_verify_convergence` | `verify.py` | Final check on timelines where every command completed |

## Three things that can disrupt the cluster

A run can disrupt the cluster in three ways. Only the first one is
controlled by launch parameters.

| Source | Who controls it | On in this harness? |
| --- | --- | --- |
| Antithesis faults: network partitions, node pause, node kill, clock skips | The launch parameters, for example `custom.disable_faults` on the `intermediate_test` webhook | Yes, unless the launch turns them off |
| Workload levers: SQL administrator actions, described below | The swarm profile of each timeline (`pxcwl/swarm.py`), and the `PXC_LEVERS` switch | Yes, unless the config sets `PXC_LEVERS: "off"`. No launch parameter turns them off |
| Supervisor kill channel: `kill -9` of `mysqld` through a file in `/opt/antithesis/state` | `../pxc-node/entrypoint.sh` | No. The supervisor serves it, but the workload container cannot write to that directory, so nothing uses it in v1 |

**Important:** `custom.disable_faults=true` turns off only the first row. The
levers still run. A "no fault" run is a "levers only" run, unless you also
turn the levers off.

## The lever switch

`PXC_LEVERS` on the `pxc-workload` service turns all levers on or off. The
base config, `../config/docker-compose.yaml`, sets `PXC_LEVERS: "on"`. With
`"off"`:

- `swarm.draw` sets the `admin` class weight and every lever weight to 0, so
  every timeline is traffic-only.
- `levers.run_one` refuses to run, as a second fence.

A launch parameter cannot reach a container, so the switch travels in the
config image. Make a levers-off copy of the config directory for one launch:

```sh
./antithesis/config-variant.sh no-levers PXC_LEVERS=off
snouty validate antithesis/config-no-levers
snouty launch --config antithesis/config-no-levers ...
```

The copies (`antithesis/config-*/`) are generated and git ignores them. Make a
new copy before every launch, so it has the latest `config/` edits.

Properties that only a lever can reach (for example "applier thread count
reaches the configured setpoint after a resize") are not hit in a levers-off
run. That is expected, not a regression.

## What a lever is

A lever is an administrator action that the workload does to a node through
SQL. Examples: `SET GLOBAL wsrep_desync = ON`, `SHUTDOWN`. A lever is not a
fault. An operator can do every lever on a production cluster, and PXC must
handle each one correctly.

### Why the workload has levers

Plain DML never reaches many PXC code paths: donor state, quorum weights,
maintenance mode, the rejoin path, the writeset size limit. Fault injection
reaches some of them, but only when a fault lands at the correct time. A lever
reaches them directly, from the first timeline. Some properties can only be
reached with a lever. For example, only `maint_mode_cycle` sets
`pxc_maint_mode = MAINTENANCE`.

So when a lever triggers a crash, the crash is a real finding. The trigger is
a supported operator action, not a harness artifact. The report must name
the lever as the trigger, so the reader knows that no fault was needed.

### The ten levers

All levers are in `pxcwl/levers.py`. "Disruptive" means that the lever can
change cluster availability. The table below explains the rules for
disruptive levers.

| Lever | SQL it sends | What it exercises | Disruptive |
| --- | --- | --- | --- |
| `applier_resize` | `SET GLOBAL wsrep_applier_threads = 1..16` | The applier pool resizes and keeps applying | No |
| `backup_lock` | `LOCK INSTANCE FOR BACKUP`, then `UNLOCK INSTANCE` | Replication with a backup lock held | No |
| `maint_mode_cycle` | `SET GLOBAL pxc_maint_mode = MAINTENANCE` or `DISABLED` | PXC keeps the mode that the operator set | Yes. It shares the token with `graceful_shutdown`, because shutdown also writes `pxc_maint_mode` |
| `desync_cycle` | `SET GLOBAL wsrep_desync = ON` for 1 to 15 s | A desynced node that still reports itself available | Yes |
| `pc_weight` | `SET GLOBAL wsrep_provider_options = 'pc.weight=1..3'` for 5 to 45 s | Quorum with unequal node weights | Yes |
| `gmcast_isolate` | `SET GLOBAL wsrep_provider_options = 'gmcast.isolate=1'` for 5 to 30 s | The node leaves the group and goes non-Primary. This is the closest lever to a network fault | Yes |
| `cluster_address_reset` | `SET GLOBAL wsrep_cluster_address = 'gcomm://<all three nodes>'` | The node leaves and rejoins through IST or SST, with no process restart | Yes |
| `graceful_shutdown` | `SHUTDOWN`, then wait for the supervisor to restart the node | The node stops cleanly, restarts, and rejoins | Yes |
| `ws_size_squeeze` | `SET GLOBAL wsrep_max_ws_size = 1 MiB`, then a 4 MiB write | The writeset size limit rejects the write | Yes |
| `strict_mode_window` | `pxc_strict_mode = PERMISSIVE` and `sql_require_primary_key = OFF` on all three nodes | Writes to a table with no primary key | Yes |

### Actions that are deliberately not levers

Each of these actions causes a finding by itself, so the workload does not do
them.

- `pc.weight = 0`: This removes the node from quorum.
- `pc.bootstrap`: This causes split brain. Final verification uses it only
  for recovery when no node is Primary.
- NBO DDL: This always aborts a joining node.

## Safety rules for levers

The levers must not leave the cluster damaged. If they did, the final
checks would fail for a harness reason. `pxcwl/leases.py` applies these
rules:

1. **Every lever has a lease.** The lease records the restore value and a
   deadline. An `eventually_` command kills running drivers, so a driver can
   stop while it holds a lever. Every command first restores all levers whose
   lease has expired (`leases.repair_expired`).
2. **One disruptive lever at a time.** All disruptive levers share one token
   for the whole cluster. Two drivers can never isolate two nodes at the same
   time.
3. **Disruptive levers start only from full health.** A disruptive lever
   starts only when all three nodes are Synced. The workload never adds a
   disruption to a cluster that is already degraded.
4. **No lever starts near the end of a driver's time budget.** A lever starts
   only if at least 60 s remain of the driver's 100 s budget
   (`PXC_TRAFFIC_BUDGET`).

## How a timeline chooses its levers

The `first_` command draws a swarm profile one time per timeline
(`pxcwl/swarm.py`). The profile sets two weights for levers:

- The `admin` action class weight comes from `[0, 0, 1, 3, 10, 30, 100]`. In
  about 29% of timelines it is 0, so the timeline pulls no levers.
- Each lever's weight comes from `[0, 0, 0, 1, 5, 20]`. Each lever is off in
  about 50% of the remaining timelines.

So about 29% of timelines in every run have traffic and no levers. Those
timelines are a baseline without levers.

The `first_` command logs the profile as the SDK event `pxc_swarm_profile`
(`seed._log_profile`). Find it with
`snouty runs events <run_id> pxc_swarm_profile`. The event has these fields:

- `levers_switch`: The `PXC_LEVERS` value, `on` or `off`.
- `levers_enabled`: The levers that this timeline can pull. A lever needs a
  non-zero `admin` weight and a non-zero lever weight.
- `traffic_only`: `true` when `levers_enabled` is empty.
- `classes_enabled`: The action classes with a non-zero weight.
- `profile`: The full profile.

## Levers are not the only trigger

Traffic alone can also trigger bugs. The action classes and the session
settings in the profile push PXC into hard states too. These are the main
ones:

| Class or setting | Operations (`pxcwl/ops.py`, `pxcwl/ddl.py`) | PXC code it stresses |
| --- | --- | --- |
| `conflict` | `update_hot_row`, `delete_reinsert`, `uk_churn`, `locking_read`, `fk_cascade_dml` | Certification conflicts and BF (brute-force) aborts, where a replicated writeset kills a local transaction |
| `sr` | `sr_session` | Streaming replication fragments, rollback, and replay |
| `ddl` | Create/drop, column, index, rename, truncate, online FK | TOI DDL under concurrent replicated writes |
| `bulk` | `bulk_write` | Large writesets, flow control, gcache pages |
| `write` | Single-row, multi-row, multi-statement, auto-increment inserts | The base replication path, certification keys |
| `hot_keyspace = 1` | All writers use one row | Maximum contention for the conflict paths |
| `isolation = SERIALIZABLE` | All sessions | A mode that Galera does not support in multi-master |
| `sr_fragment_size`, `optimistic_pa`, `fc_limit` | Server and session posture | Streaming replication, parallel apply, flow control |

Example: in run `ae37e54b593856b8df2b2ac3b581479c-63-2`, history
`-4358306101398655125`, node3 asserted at `lock0lock.cc:5282` on its first
boot (vtime 92.45). Before the crash, no node logged a membership change, a
shutdown, a weight change, or "Stop replication by". An `fk_cascade_dml` write
was in flight. A lever that does not log (for example `applier_resize`)
cannot be excluded, because that run had no profile or lever events yet.

## Action events in the log

The workload announces each action when it starts, as an SDK event
(`pxcwl/events.py`). An event at the start puts the action that is in flight
during a crash before the crash in the log. A summary at the end of a driver
call would arrive after the crash, and a crash log ends at the crash.

| Event | Emitted by | When | Fields |
| --- | --- | --- | --- |
| `pxc_swarm_profile` | `seed._log_profile` | One time per timeline | See above |
| `pxc_lever` | `leases.acquire`, `leases.release`, `leases.repair_expired` | `phase: acquire` just before the lever's SQL. `phase: release` when the lever ends. `phase: repair` when another command restores a lever whose holder was killed | `lever`, `node`, `intent`, `restore`, `hold_seconds`, `disruptive`, `restored`, `inv_id`, `by` |
| `pxc_op` | `ops.run_one`, `ddl.run_one` | Just before each traffic or DDL operation | `class`, `op`, `node`, `inv_id` |

`inv_id` is the driver call's id in the journal. It links an event to the
journal rows of that call.

Notes:

- `by` is the command that emitted the event: `first`, `traffic`, `probe`, or
  `verify`. Swarm levers come from `traffic`. The `first` command also takes
  the `strict_mode_window` lease one time, to create the table without a
  primary key (`seed._create_nopk`). That is a setup step. It also occurs
  with `PXC_LEVERS: "off"`.
- `acquire` means "about to apply". The SQL can still fail. Then `release`
  follows at once.
- `restored: false` means the restore SQL could not reach the node. The
  lease stays, and a later command retries the restore.
- Find the events with `snouty runs events <run_id> pxc_lever` or
  `... pxc_op`. In a downloaded log, select them with
  `jq 'select(.pxc_lever or .pxc_op)'`.
- `pxc_op` adds volume, one event per operation. Check the "Customer output
  volume" property after a run. If it goes above 200 MB per core-hour, keep
  `pxc_op` only for the `conflict`, `sr` and `ddl` classes.

## Reading a triage report with levers in mind

- **Find the lever before a crash.** Look for the last `pxc_lever` events
  before the crash. In logs from runs before these events existed, check the
  node's log just before the crash for these lines:
  - `cluster_address_reset` logs `[WSREP] Stop replication by <thread id>`
    (`sql/wsrep_var.cc:566` calls `wsrep_stop_replication`, which logs at
    `sql/wsrep_mysqld.cc:1368`).
  - `pc_weight` logs `[Galera] <uuid> changing node <uuid> weight (reg) 1 -> N`.
  - TODO: Find the log lines for the other levers in the source code and add
    them here.
- **Do not use `outcome_tally` to explain a crash.** The assertion
  "terminal verification completed a quiesced three-node comparison" carries
  `outcome_tally` in its details: counts of `<operation>/<errno>` for the
  whole timeline, for example `graceful_shutdown/-1: 3`. It is only a coverage
  check. It has no time, no order, and no node, it keeps only the 40 largest
  entries, and only the final check emits it, after the crash. A crash log
  ends at the crash, so it never contains it.
- **Classify the trigger.** In the report, name the trigger as one of: an
  Antithesis fault, a lever, or neither. A crash after a lever with no fault is
  still a PXC finding. Use `../scratchbook/triage-scope.md` to decide if
  Percona owns the code.

## Known gaps

- The kill channel is not used. To test ungraceful death, either enable
  Antithesis node termination faults, or give the workload a shared volume to
  `/opt/antithesis/state` on each node.
