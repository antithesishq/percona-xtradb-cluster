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

Every shape that has two directions READS THE CATALOG FIRST and emits the one
that is legal, instead of flipping a coin and hoping. See _toggle.
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


def _run_ddl(jr, s, stmt: str, shape: str) -> bool:
    """Execute one DDL statement as a tracked episode. True if it completed.

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
        return True
    except Exception as e:  # noqa: BLE001
        errno = db.errno_of(e)
        # A clean DDL rejection is a terminal outcome, not a residue. Only a
        # lost connection leaves the episode genuinely unresolved.
        if errno is not None and db.is_alive(s.conn):
            jr.ddl_end(ddl_id, "FAILED", errno)
        else:
            jr.ddl_end(ddl_id, "UNKNOWN", errno)
        jr.tally(shape, errno)
        return False


# Lookups this module makes before choosing a direction. A failure here is an
# environment condition -- the node is mid-restart, the connection just died,
# the table is being renamed past us -- and the only safe response is to emit
# nothing. Guessing is what produced the errors this function exists to stop.
def _catalog(jr, s, fn, shape: str, *args) -> object | None:
    try:
        return fn(s.conn, *args)
    except Exception as e:  # noqa: BLE001
        jr.tally(f"{shape}:lookup_failed", db.errno_of(e))
        return None


def _table_is_there(jr, s, table: str, shape: str) -> bool:
    """Is this table in the catalog right now?

    A rename_swap that died between its own renames, or a table dropped by a
    shape that should not have, would otherwise silently zero out three of the
    six DDL shapes for the rest of the run. The tally is what makes that
    visible instead of looking like a quiet timeline.
    """
    cols = _catalog(jr, s, schema.column_names, shape, table)
    if cols is None:
        return False
    if not cols:
        jr.tally(f"{shape}:table_absent", None)
        return False
    return True


def _toggle(jr, s, shape: str, present: bool, create_stmt: str, drop_stmt: str) -> None:
    """Emit whichever of the two directions the catalog says is legal.

    The coin flip this replaces is the same defect config.EPHEMERAL_TABLE
    documents for tables, one level down: a CREATE against an object that is
    already there fails ER_DUP_KEYNAME / ER_DUP_FIELDNAME / ER_FK_DUP_NAME, a
    DROP against one that is not fails ER_CANT_DROP_FIELD_OR_KEY, and PXC
    replicates the failing statement anyway. Each one then costs a cluster-wide
    inconsistency vote, which property-catalog.md's un-injected-vote rule reads
    as divergence evidence -- so the noise does not merely waste the run, it
    destroys the signal the terminal checksum oracle depends on. Run
    5a7b1d9f...-63-0 showed the sharp end of it: one such statement failed to
    apply on node1 at seqno 606 during a partition, node2 could not vote,
    declared itself inconsistent and demanded a full SST, and node1 then served
    a green health check for 164 seconds without committing anything.

    A race against a concurrent driver can still land a stale direction -- the
    catalog read and the statement are not one atomic unit -- but that is a
    rare loser rather than every second statement, and the shape x errno tally
    measures whatever is left.
    """
    if present:
        if _run_ddl(jr, s, drop_stmt, shape):
            oracles.saw_ddl_drop_of_live_object({"statement": drop_stmt[:200], "node": s.name})
    else:
        _run_ddl(jr, s, create_stmt, shape)


def ddl_index(jr, s, profile: dict) -> None:
    # Which slot is still the random draw; only the direction is decided by
    # what is actually there, so the menu axis is unchanged.
    table = rnd.choice(config.SCRATCH_TABLES + ["wl_fk_parent", "wl_fk_child"])
    idx = f"ix_swarm_{rnd.randint(0, 3)}"
    col = "v" if table in config.SCRATCH_TABLES else ("pv" if table == "wl_fk_parent" else "cv")
    # An index lookup on a missing table returns the empty set, which is
    # indistinguishable from "table is there, index is not" -- and would send
    # us into a CREATE that fails ER_NO_SUCH_TABLE. Columns disambiguate it:
    # a table that exists always has some.
    if not _table_is_there(jr, s, table, "ddl_index"):
        return
    have = _catalog(jr, s, schema.index_names, "ddl_index", table)
    if have is None:
        return
    _toggle(
        jr, s, "ddl_index", idx in have,
        f"CREATE INDEX `{idx}` ON `{SCHEMA}`.`{table}` (`{col}`)",
        f"DROP INDEX `{idx}` ON `{SCHEMA}`.`{table}`",
    )


def ddl_column(jr, s, profile: dict) -> None:
    table = rnd.choice(config.SCRATCH_TABLES)
    col = f"c_swarm_{rnd.randint(0, 3)}"
    algo = rnd.choice(["INPLACE", "COPY", "DEFAULT"])
    have = _catalog(jr, s, schema.column_names, "ddl_column", table)
    if have is None:
        return
    if not have:
        jr.tally("ddl_column:table_absent", None)
        return
    _toggle(
        jr, s, "ddl_column", col in have,
        f"ALTER TABLE `{SCHEMA}`.`{table}` ADD COLUMN `{col}` INT NULL, ALGORITHM={algo}",
        f"ALTER TABLE `{SCHEMA}`.`{table}` DROP COLUMN `{col}`, ALGORITHM={algo}",
    )


def ddl_online_fk(jr, s, profile: dict) -> None:
    """Online FK add/drop with foreign_key_checks disabled.

    This is a named repro family in the research: an FK constraint added
    INPLACE while cascading DML runs against the same tables.
    """
    child = rnd.choice(config.SCRATCH_TABLES)
    name = f"fk_swarm_{rnd.randint(0, 3)}"
    if not _table_is_there(jr, s, child, "ddl_online_fk"):
        return
    # Schema-wide: the name may already be live on a DIFFERENT scratch table,
    # and adding it here would fail ER_FK_DUP_NAME. When it is taken elsewhere
    # the useful move is to drop it from its actual owner rather than skip the
    # draw, which keeps both directions exercised at full rate.
    owners = _catalog(jr, s, schema.foreign_key_owners, "ddl_online_fk")
    if owners is None:
        return
    owner = owners.get(name)

    try:
        with s.conn.cursor() as cur:
            cur.execute("SET SESSION foreign_key_checks = 0")
    except Exception:  # noqa: BLE001
        return

    # `pref`, not `sid`. sid is BIGINT UNSIGNED and wl_fk_parent.pid is INT;
    # MySQL requires the two ends of a foreign key to match in width and
    # signedness, so the sid form was rejected errno 3780 every time it was
    # drawn and this whole repro family never once executed.
    _toggle(
        jr, s, "ddl_online_fk", owner is not None,
        f"ALTER TABLE `{SCHEMA}`.`{child}` ADD CONSTRAINT `{name}` "
        f"FOREIGN KEY (pref) REFERENCES `{SCHEMA}`.`wl_fk_parent` (pid) "
        "ON DELETE CASCADE ON UPDATE CASCADE, ALGORITHM=INPLACE",
        f"ALTER TABLE `{SCHEMA}`.`{owner}` DROP FOREIGN KEY `{name}`",
    )

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
