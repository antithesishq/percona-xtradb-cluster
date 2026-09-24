# Oracle tests

Runtime-free tests for the assertion logic in `workload/pxcwl/`. Run them after
any change to `checks.py`, `oracles.py`, `levers.py`, `probe.py` or `ddl.py`:

```
bash antithesis/oracle-tests/run.sh
```

They exist because there is no container runtime on the harness development
machine, so `snouty validate` and `local-validate.sh` cannot run here — and
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
| `test_assertion_catalog.py` | Replicates the platform's static scan: every assertion name is an inline literal and unique. An assertion whose name is built at runtime is invisible to the catalog rather than failing loudly. |
