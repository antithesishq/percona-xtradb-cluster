"""TOI DDL under concurrent replicated writes.

Constrained by two configured settings, both deliberate:

  pxc_strict_mode=ENFORCING  blocks non-InnoDB DML and PK-less DML.
  enforce_gtid_consistency=ON forbids CREATE TABLE ... SELECT and
                              CREATE TEMPORARY TABLE inside a transaction.

So the generator never emits either of those shapes. A bug here would present
as thousands of identical errors in the outcome tally rather than as a finding,
which is why the tally is worth reading in the first triage.

DDL targets a fixed pool of scratch tables, and otherwise only touches the
INDEX structure of the FK pair -- index changes do not alter row content, so
the checksum set stays comparable while DDL runs against it.
"""

from __future__ import annotations

from . import config, db, oracles, rnd, schema

SCHEMA = config.SCHEMA

# Menu axis (interesting values) is largely not applicable here: the action
# vocabulary is a fixed set of statement shapes rather than a bounded numeric
# input, and the only parameterised dimensions -- which scratch table, which
# index name, which ALGORITHM -- are drawn from small closed sets already
# enumerated below. The bounded-input menus live in ops.py, where row counts,
# transaction sizes and writeset bytes are drawn from configured-limit families.


def _run_ddl(jr, s, stmt: str, shape: str) -> None:
    """Execute one DDL statement as a tracked episode.

    The episode ledger is what makes "no DDL left unresolved after
    reconvergence" a decidable question: an episode still in ATTEMPTED after
    the cluster has settled means a TOI operation never reached a terminal
    outcome.
    """
    ddl_id = jr.ddl_start(s.name, stmt)
    try:
        with s.conn.cursor() as cur:
            cur.execute(stmt)
        jr.ddl_end(ddl_id, "DONE")
        jr.tally(shape, None)
        # The claim is "DDL completed under concurrent replicated writes", so
        # it needs evidence that another driver was actually running -- a
        # successful DDL on an otherwise idle cluster does not exercise the
        # TOI-versus-DML interleaving the divergence family needs.
        concurrent = jr.concurrent_drivers()
        if concurrent > 0:
            oracles.saw_ddl_under_load(
                {
                    "statement": stmt[:200],
                    "node": s.name,
                    "concurrent_driver_invocations": concurrent,
                }
            )
    except Exception as e:  # noqa: BLE001
        errno = db.errno_of(e)
        # A clean DDL rejection is a terminal outcome, not a residue. Only a
        # lost connection leaves the episode genuinely unresolved.
        if errno is not None and db.is_alive(s.conn):
            jr.ddl_end(ddl_id, "FAILED", errno)
        else:
            jr.ddl_end(ddl_id, "UNKNOWN", errno)
        jr.tally(shape, errno)


def ddl_index(jr, s, profile: dict) -> None:
    table = rnd.choice(config.SCRATCH_TABLES + ["wl_fk_parent", "wl_fk_child"])
    idx = f"ix_swarm_{rnd.randint(0, 3)}"
    col = "v" if table in config.SCRATCH_TABLES else ("pv" if table == "wl_fk_parent" else "cv")
    if rnd.chance(0.5):
        stmt = f"CREATE INDEX `{idx}` ON `{SCHEMA}`.`{table}` (`{col}`)"
    else:
        stmt = f"DROP INDEX `{idx}` ON `{SCHEMA}`.`{table}`"
    _run_ddl(jr, s, stmt, "ddl_index")


def ddl_column(jr, s, profile: dict) -> None:
    table = rnd.choice(config.SCRATCH_TABLES)
    col = f"c_swarm_{rnd.randint(0, 3)}"
    algo = rnd.choice(["INPLACE", "COPY", "DEFAULT"])
    if rnd.chance(0.5):
        stmt = (
            f"ALTER TABLE `{SCHEMA}`.`{table}` ADD COLUMN `{col}` INT NULL, "
            f"ALGORITHM={algo}"
        )
    else:
        stmt = f"ALTER TABLE `{SCHEMA}`.`{table}` DROP COLUMN `{col}`, ALGORITHM={algo}"
    _run_ddl(jr, s, stmt, "ddl_column")


def ddl_online_fk(jr, s, profile: dict) -> None:
    """Online FK add/drop with foreign_key_checks disabled.

    This is a named repro family in the research: an FK constraint added
    INPLACE while cascading DML runs against the same tables.
    """
    child = rnd.choice(config.SCRATCH_TABLES)
    name = f"fk_swarm_{rnd.randint(0, 3)}"
    try:
        with s.conn.cursor() as cur:
            cur.execute("SET SESSION foreign_key_checks = 0")
    except Exception:  # noqa: BLE001
        return

    if rnd.chance(0.5):
        # `pref`, not `sid`. sid is BIGINT UNSIGNED and wl_fk_parent.pid is
        # INT; MySQL requires the two ends of a foreign key to match in width
        # and signedness, so the sid form was rejected errno 3780 every time
        # it was drawn and this whole repro family never once executed.
        stmt = (
            f"ALTER TABLE `{SCHEMA}`.`{child}` ADD CONSTRAINT `{name}` "
            f"FOREIGN KEY (pref) REFERENCES `{SCHEMA}`.`wl_fk_parent` (pid) "
            "ON DELETE CASCADE ON UPDATE CASCADE, ALGORITHM=INPLACE"
        )
    else:
        stmt = f"ALTER TABLE `{SCHEMA}`.`{child}` DROP FOREIGN KEY `{name}`"
    _run_ddl(jr, s, stmt, "ddl_online_fk")

    try:
        with s.conn.cursor() as cur:
            cur.execute("SET SESSION foreign_key_checks = 1")
    except Exception:  # noqa: BLE001
        pass


def ddl_truncate(jr, s, profile: dict) -> None:
    table = rnd.choice(config.SCRATCH_TABLES)
    _run_ddl(jr, s, f"TRUNCATE TABLE `{SCHEMA}`.`{table}`", "ddl_truncate")


def ddl_rename_swap(jr, s, profile: dict) -> None:
    """A three-way rename: atomic from the client's view, TOI on the cluster."""
    # A random pair rather than always the first two, so the swap is not
    # confined to one corner of the scratch pool.
    pair = rnd.shuffled(config.SCRATCH_TABLES)[:2]
    a, b = pair[0], pair[1]
    tmp = "wl_scratch_swap_tmp"
    stmt = (
        f"RENAME TABLE `{SCHEMA}`.`{a}` TO `{SCHEMA}`.`{tmp}`, "
        f"`{SCHEMA}`.`{b}` TO `{SCHEMA}`.`{a}`, "
        f"`{SCHEMA}`.`{tmp}` TO `{SCHEMA}`.`{b}`"
    )
    _run_ddl(jr, s, stmt, "ddl_rename_swap")


def ddl_create_drop(jr, s, profile: dict) -> None:
    """Create and drop ONE dedicated table, never the shared pool.

    The shape exists to put CREATE TABLE and DROP TABLE through TOI, and one
    table does that as well as four. Pointing it at config.SCRATCH_TABLES did
    something else as a side effect: the coin flip random-walked the pool, so
    roughly half the time a given scratch table did not exist, and the five
    other DDL shapes -- which all target that pool -- failed ER_NO_SUCH_TABLE
    instead of exercising what they were written for. Neither branch here can
    report the damage, because IF EXISTS and IF NOT EXISTS both always succeed.

    See config.EPHEMERAL_TABLE for why the resulting inconsistency votes were
    worse than merely wasteful.

    Note there is no CREATE TABLE ... SELECT: enforce_gtid_consistency=ON
    forbids it.
    """
    table = config.EPHEMERAL_TABLE
    if rnd.chance(0.5):
        # One definition, shared with the seeded pool, so the two cannot drift.
        stmt = schema.scratch_ddl(table, qualify=True)
    else:
        stmt = f"DROP TABLE IF EXISTS `{SCHEMA}`.`{table}`"
    _run_ddl(jr, s, stmt, "ddl_create_drop")


DDL_OPS = [
    ddl_index,
    ddl_column,
    ddl_online_fk,
    ddl_truncate,
    ddl_rename_swap,
    ddl_create_drop,
]


def run_one(jr, s, profile: dict) -> None:
    op = rnd.choice(DDL_OPS)
    try:
        op(jr, s, profile)
    except Exception as e:  # noqa: BLE001
        jr.tally("ddl:uncaught", db.errno_of(e))
