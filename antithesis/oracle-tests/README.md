# Oracle tests

Runtime-free tests for the assertion logic in `workload/pxcwl/` and the
supervisor's death classification. Run them after any change to `checks.py`,
`oracles.py`, `levers.py`, `probe.py`, `ddl.py` or `pxc-node/entrypoint.sh`:

```
bash antithesis/oracle-tests/run.sh
```

`antithesis/local-validate.sh` runs them as its `offline:oracle-tests` step
(with `--offline`, or before the cluster phase), so the normal pre-launch
check covers them. A new test needs no wiring beyond the `test_*.py` name.

They exist because there is no container runtime on the harness development
machine, so `snouty validate` and the cluster phase of `local-validate.sh`
cannot run here — and
because a green run proves much less than it looks like it does. These are
**detection** tests: each one injects the real failure — a divergence, a wedged
node, or a statement the server would reject — and asserts it is caught,
alongside the benign case where the checker must stay silent. Several also feed
the old, broken behaviour to the same checker and require it to be rejected,
because "found nothing" otherwise does not distinguish a fixed subject from a
blind checker. A checker that never fires and one that always fires both pass a
smoke test.

`helper_stubs.py` fakes the `antithesis` and `pymysql` packages and puts them
ahead of the real ones on `sys.path` (`helper_` so Antithesis ignores it, by the
same convention used under `test/`). This directory is not copied into any
image, and `antithesis/` is stripped from the `pxc-src` stage, so nothing here
touches the PXC compile cache.

| Test | What it pins |
| --- | --- |
| `test_compare_excludes_unreadable.py` | A node that errors is excluded from the schema/GTID comparison, never folded into the compared value — while a real divergence among the readable nodes is still caught, including when a third node is erroring. |
| `test_maint_mode_claim.py` | The maint-mode claim fires only on the operator-set-MAINTENANCE-reverted-to-DISABLED arm; SHUTDOWN and the forced-flip direction are carved out; a view change is *not*. Plus: `maint_mode_cycle` and `graceful_shutdown` are mutually exclusive through the real lease token. |
| `test_availability_evidence.py` | The green-node availability claim needs positive evidence — a probe that landed in the trailing window, or an unbroken run of refused writes covering it — and stays silent on missing data, an unreadable `pxc_maint_mode`, or an unusable schema, while still catching a green node that genuinely refuses writes. Also pins the concurrency fold: several `anytime_` probe processes share one journal row, and a failing one must not erase another's recorded success. |
| `test_ddl_direction.py` | The DDL generator reads the catalog and emits only the direction that is legal, exercises both, and emits nothing at all when the lookup fails or the table is momentarily absent. The last case feeds the old coin-flip generator to the same model, which must reject it — otherwise "no invalid statements" would not distinguish a fixed generator from a blind model. |
| `test_ddl_concurrent_drivers.py` | Four concurrent drivers on three nodes whose catalog views lag an authoritative, totally ordered one emit no statement TOI would reject. That rests on `ddl._scratch_lock` and on the causal (`wsrep_sync_wait` READ bit) lookup, whose session level is restored afterwards. Detection: without both fixes the model catches 100+ invalid statements. The race and the stale node are each shown to need their own fix: the lock-less run still races, and a node kept nine statements behind emits invalid DDL without the causal read. |
| `test_triage_false_positives.py` | The four false positives from run `5aa4afb5-63-0`, each replayed and required to stay silent beside the real failure, which must still be caught: `applier_resize` gives no verdict on a node that is disconnected or out of Synced, but a Synced pool stuck below its setpoint is still caught; a COMMIT failing 1105/1205/1317 is UNKNOWN, while 1213/1062 and statement-time errors stay FAILED; a FAILED-but-present row carries its journal shape/errno/phase; terminal `pc.bootstrap` happens only when no node is Primary, every unreachable node is down (not timed out) and inside the reachable nodes' component, the chosen node is not behind the probe ledger's high-water mark, and the condition holds across two samples and a final re-check; `Session.ensure` applies every SET after a rejected one. |
| `test_unireg_abort_cause.py` | Supervisor, not workload: the `unireg_abort` classification in `pxc-node/entrypoint.sh`, with its functions extracted by awk and fed the cause chain of a real boot from run `c89f2f7a…-63-2`, inlined verbatim in the test. The documented `pc.wait_prim_timeout` stop emits only the coverage Reachables. A stop without it, an InnoDB stop, a documented line appearing only after `Aborting`, and a silent exit 1 each fail the umbrella and a per-cause Unreachable, and skip the coverage claims. Every emitted line parses with `jq`. Detection: with the check stubbed out, the undocumented stop turns coverage green instead. |
| `test_start_position.py` | Supervisor, not workload: `recover_position` and `clear_interrupted_sst` in `pxc-node/entrypoint.sh`, extracted by awk and run on a scratch datadir with a fake `mysqld --wsrep-recover`. A clean `grastate.dat` seqno is the start position and skips recovery, as the shipped wrappers do; seqno -1 or no file still recovers. A datadir left by an interrupted xtrabackup SST (`sst_in_progress`, no `mysql/`) is emptied before `--initialize`, as the RPM wrapper does; a datadir without the marker is left alone. Detection: the pre-change code starts at the recovered 603 of run `fdb9d32c…-63-5` and leaves the SST leftovers in place. |
| `test_single_lineage_zero_uuid.py` | The single-lineage claim ignores the all-zero UUID that a node with no state reports (an Inconsistent node in run `fdb9d32c…-63-5`), and such a node does not count toward the two-reporter gate. Two different non-zero UUIDs are still a fork, also on a node that is not Synced. Detection: the pre-change lineage set counts the zero UUID as a second lineage. |
| `test_assertion_catalog.py` | Replicates the platform's static scan: every assertion name is an inline literal and unique. An assertion whose name is built at runtime is invisible to the catalog rather than failing loudly. |
