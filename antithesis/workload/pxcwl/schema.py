"""Workload schema.

Every table carries an explicit PRIMARY KEY, with exactly one deliberate
exception (``wl_nopk``), which is created separately by ``create_nopk`` inside
a fenced window. What the fence is for, verified against this tree's own
sources rather than assumed:

* ``pxc_strict_mode`` is left at its ENFORCING default, and raising it to
  ENFORCING force-sets ``sql_require_primary_key=ON`` globally
  (``sql/wsrep_var.cc`` ``pxc_strict_mode_update``).
* ``sql_require_primary_key=ON`` rejects a PK-less CREATE with errno 3750, and
  setting it OFF is itself rejected while ``pxc_strict_mode`` is ENFORCING
  (``sql/sys_vars.cc``
  ``check_session_admin_and_sql_require_primary_key_on_check``). So the strict
  mode has to come down first, and go back up last.
* ``pxc_strict_mode`` is GLOBAL-only (``sql/sys_vars.cc`` ``Sys_pxc_strict_mode``
  is a ``GLOBAL_VAR``), so the fence cannot be session-scoped.
* The session value is enough for the OTHER nodes. A CREATE TABLE writeset
  carries ``Q_SQL_REQUIRE_PRIMARY_KEY`` (``sql/log_event.cc``
  ``is_sql_require_primary_key_needed``), and every applier adopts it because
  the default channel policy is ``PK_CHECK_STREAM`` (``sql/rpl_rli.cc``
  constructor, consumed at ``sql/log_event.cc:4829``). Without that the CREATE
  would succeed on the donor and fail on the appliers, which is a harness-made
  schema divergence -- strictly worse than the CREATE failing outright.

Note what the fence is NOT for: nothing in this tree blocks plain DML on an
existing PK-less table. The only ``pxc_strict_mode`` PK check on the write path
is for INSERT through a VIEW whose base table has no PK
(``sql/sql_insert.cc:263``), which this workload never does.

None of the new tables use TIMESTAMP DEFAULT CURRENT_TIMESTAMP. Row-based
replication ships the evaluated value so it would in fact be safe, but these
tables are compared byte-for-byte across nodes and there is no reason to leave
even a question about server-evaluated defaults in the checksum set.
"""

from __future__ import annotations

from . import config

SCHEMA = config.SCHEMA

# Public and parameterised by name, so ddl.py's create/drop shape builds its
# ephemeral table from this one definition instead of carrying its own copy.
# Two hand-synchronised copies of the same CREATE is a drift waiting to happen.
#
# `pref` exists solely as an FK-legal reference to wl_fk_parent.pid. It cannot
# be `sid`: that is BIGINT UNSIGNED and pid is INT, and MySQL requires the
# referencing and referenced columns to match in both width and signedness --
# so ddl_online_fk's ADD CONSTRAINT failed errno 3780 on every single attempt,
# and its DROP branch then failed 1091 against a constraint that had never been
# created. Nullable on purpose: existing rows predate the column, and a NULL
# child value satisfies a foreign key.
SCRATCH_DDL = """CREATE TABLE IF NOT EXISTS {ident} (
  sid  BIGINT UNSIGNED NOT NULL,
  v    BIGINT NOT NULL DEFAULT 0,
  pref INT NULL,
  PRIMARY KEY (sid)
) ENGINE=InnoDB"""

WORKLOAD_TABLES: list[str] = [
    # The ack journal's backbone: written once, never updated or deleted.
    # Write-once matters -- touching traffic converts a divergence into an
    # eviction before any checksum can observe it, so the primary witness of
    # cross-node equality must be immutable.
    """CREATE TABLE IF NOT EXISTS `wl_witness` (
         wid     BIGINT UNSIGNED NOT NULL,
         writer  VARCHAR(64)     NOT NULL,
         node    VARCHAR(16)     NOT NULL,
         seq     INT             NOT NULL,
         payload VARBINARY(255)  NOT NULL,
         PRIMARY KEY (wid)
       ) ENGINE=InnoDB""",
    # Certification / BF-abort target, and the interval-accounted counter.
    """CREATE TABLE IF NOT EXISTS `wl_hot` (
         hid INT         NOT NULL,
         v   BIGINT      NOT NULL DEFAULT 0,
         tag VARCHAR(64) NOT NULL DEFAULT '',
         PRIMARY KEY (hid)
       ) ENGINE=InnoDB""",
    # Certification keys on a UNIQUE secondary index, not just the PK.
    """CREATE TABLE IF NOT EXISTS `wl_uk` (
         id BIGINT UNSIGNED NOT NULL,
         u  INT NOT NULL,
         PRIMARY KEY (id),
         UNIQUE KEY uk_u (u)
       ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS `wl_fk_parent` (
         pid INT    NOT NULL,
         pv  BIGINT NOT NULL DEFAULT 0,
         PRIMARY KEY (pid)
       ) ENGINE=InnoDB""",
    # Cascading FK, so wsrep_append_foreign_key runs and parent keys land in
    # the writeset. Missed FK key extraction is a silent-divergence path.
    """CREATE TABLE IF NOT EXISTS `wl_fk_child` (
         cid BIGINT UNSIGNED NOT NULL,
         pid INT    NOT NULL,
         cv  BIGINT NOT NULL DEFAULT 0,
         PRIMARY KEY (cid),
         KEY k_pid (pid),
         CONSTRAINT fk_child_parent FOREIGN KEY (pid)
           REFERENCES `wl_fk_parent` (pid) ON DELETE CASCADE ON UPDATE CASCADE
       ) ENGINE=InnoDB""",
    # gcache page-store pressure. A bounded ring: there are no compose volumes,
    # so container disk is finite and an unbounded blob table would fill it.
    """CREATE TABLE IF NOT EXISTS `wl_bulk` (
         bid  BIGINT UNSIGNED NOT NULL,
         gen  BIGINT NOT NULL DEFAULT 0,
         body LONGBLOB NOT NULL,
         PRIMARY KEY (bid)
       ) ENGINE=InnoDB""",
    # Server-assigned identity, written from all three nodes at once. The
    # UNIQUE wid is what links a server-chosen aid back to a journal record,
    # which is what makes an autoinc collision detectable at all.
    """CREATE TABLE IF NOT EXISTS `wl_autoinc` (
         aid  BIGINT NOT NULL AUTO_INCREMENT,
         node VARCHAR(16) NOT NULL,
         wid  BIGINT UNSIGNED NOT NULL,
         PRIMARY KEY (aid),
         UNIQUE KEY uk_wid (wid)
       ) ENGINE=InnoDB""",
    # Health probe. `at` is client-supplied epoch ms, not NOW().
    """CREATE TABLE IF NOT EXISTS `wl_probe` (
         node VARCHAR(16) NOT NULL,
         n    BIGINT NOT NULL DEFAULT 0,
         at   BIGINT NOT NULL DEFAULT 0,
         PRIMARY KEY (node)
       ) ENGINE=InnoDB""",
]

# The one PK-less table. Deliberately NOT in WORKLOAD_TABLES: it is the only
# table whose CREATE needs the fence, and having it in the main list made one
# rejected statement abort the whole seed. Compared under its own property name.
NOPK_DDL = """CREATE TABLE IF NOT EXISTS `wl_nopk` (
         a INT NOT NULL,
         b VARCHAR(64) NOT NULL
       ) ENGINE=InnoDB"""


def scratch_ddl(name: str, *, qualify: bool = False) -> str:
    """The CREATE for one scratch-shaped table.

    Qualify when the caller has no USE in effect -- ddl.py fully qualifies
    every statement it emits, create_all does not.
    """
    ident = f"`{SCHEMA}`.`{name}`" if qualify else f"`{name}`"
    return SCRATCH_DDL.format(ident=ident)


def create_all(conn) -> None:
    """Every table except `wl_nopk`. See create_nopk for that one."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{SCHEMA}`")
        cur.execute(f"USE `{SCHEMA}`")
        for ddl in WORKLOAD_TABLES:
            cur.execute(ddl)
        for name in config.SCRATCH_TABLES:
            cur.execute(scratch_ddl(name))


def create_nopk(conn) -> None:
    """Create `wl_nopk` inside a PK-less fence. See the module docstring.

    The caller owns the lease that guarantees the fence is closed even if this
    command is killed between the two halves; the `finally` here only covers
    the ordinary path. Restoring pxc_strict_mode to ENFORCING also puts the
    GLOBAL sql_require_primary_key back on its own
    (sql/wsrep_var.cc pxc_strict_mode_update), but it is restored explicitly
    anyway rather than resting on a side effect.
    """
    with conn.cursor() as cur:
        cur.execute(f"USE `{SCHEMA}`")
        cur.execute("SET GLOBAL pxc_strict_mode = PERMISSIVE")
        try:
            cur.execute("SET SESSION sql_require_primary_key = OFF")
            cur.execute(NOPK_DDL)
        finally:
            # Best-effort, and deliberately so: if the node died mid-CREATE
            # these will fail too, and re-raising from the finally would
            # replace the real error with a connection error. The lease is
            # what actually guarantees the fence closes.
            for stmt in (
                "SET SESSION sql_require_primary_key = ON",
                "SET GLOBAL sql_require_primary_key = ON",
                "SET GLOBAL pxc_strict_mode = ENFORCING",
            ):
                try:
                    cur.execute(stmt)
                except Exception:  # noqa: BLE001 - lease repair is the backstop
                    pass


def seed_rows(conn) -> None:
    """Pre-create the fixed-key rows the conflict generators update.

    Seeding up front means an UPDATE on a hot row is a real update rather than
    a no-op, so certification conflicts actually happen.
    """
    with conn.cursor() as cur:
        cur.execute(f"USE `{SCHEMA}`")
        cur.executemany(
            "INSERT IGNORE INTO `wl_hot` (hid, v) VALUES (%s, 0)",
            [(i,) for i in range(config.HOT_KEYSPACE_MAX)],
        )
        cur.executemany(
            "INSERT IGNORE INTO `wl_fk_parent` (pid, pv) VALUES (%s, 0)",
            [(i,) for i in range(config.FK_PARENT_ROWS)],
        )
        cur.executemany(
            "INSERT IGNORE INTO `wl_probe` (node, n, at) VALUES (%s, 0, 0)",
            [(name,) for name, _ in config.NODES],
        )


def payload_for(wid: int) -> bytes:
    """Deterministic content for a witness row.

    Derived from the key so the checksum pass can tell a silently CORRUPTED
    row from a MISSING one. Without a content component, a divergence that
    rewrites a row's data while preserving its key would be invisible to any
    key-set comparison.
    """
    block = wid.to_bytes(8, "big")
    return block * 8


# --------------------------------------------------------------------------
# Checksum support
# --------------------------------------------------------------------------


def column_signature(conn, table: str) -> list[tuple[str, str]]:
    """(column, type) in ordinal order, read from the server's own catalog.

    Compared across nodes before any row is hashed: if the definitions differ,
    the row comparison is meaningless and the real finding is the schema
    divergence.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (SCHEMA, table),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# Live schema introspection for the DDL generator
#
# Read fresh immediately before each statement rather than cached in the
# journal. A cache cannot survive ddl_rename_swap: renaming wl_scratch_0 to
# wl_scratch_2 moves every index, column and constraint on it to the other
# name, so any ledger keyed on (table, object) is wrong the instant a swap
# lands -- and the swap is issued by a different driver invocation, so no
# in-process bookkeeping can see it either. The server's own catalog is the
# only view that is right by construction.
# --------------------------------------------------------------------------


def column_names(conn, table: str) -> set[str]:
    """Columns of one table. Empty means the table is not there right now.

    Every table has at least one column, so the empty set is an unambiguous
    "absent" signal, which is what the DDL generator gates on.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (SCHEMA, table),
        )
        return {r[0] for r in cur.fetchall()}


def index_names(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT INDEX_NAME FROM information_schema.statistics "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (SCHEMA, table),
        )
        return {r[0] for r in cur.fetchall()}


def foreign_key_owners(conn) -> dict[str, str]:
    """constraint name -> the table it is on, for the WHOLE schema.

    Schema-wide, not per-table, and that is the whole point. MySQL 8 requires a
    foreign key constraint name to be unique per SCHEMA, so a per-table lookup
    reports `fk_swarm_2` absent from wl_scratch_2 while it sits on
    wl_scratch_0, and an ADD against it fails ER_FK_DUP_NAME 1826. That pair is
    in local-validate-20260923T211617Z.log verbatim -- id=32 DONE on
    wl_scratch_0, id=44 FAILED 1826 on wl_scratch_2 -- and a per-table lookup
    would be WORSE than the coin flip it replaced, because once a name is taken
    anywhere three of the four table draws for it are doomed rather than half.

    Indexes and columns really are per-table, so those two lookups are keyed
    correctly; this is the one that is not.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT CONSTRAINT_NAME, TABLE_NAME FROM information_schema.table_constraints "
            "WHERE TABLE_SCHEMA = %s AND CONSTRAINT_TYPE = 'FOREIGN KEY'",
            (SCHEMA,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def checksum_expr(table: str, columns: list[str]) -> str:
    """An order-independent, multiset-sensitive checksum over a table.

    Three components, because each alone is blind to something:

      COUNT(*)  catches bulk loss.
      BIT_XOR   catches content divergence but is blind to a DUPLICATED pair
                (x XOR x == 0), which is exactly what a double-apply looks
                like.
      SUM       is sensitive to duplication, covering BIT_XOR's blind spot.

    The NULL bitmap is not optional: CONCAT_WS skips NULLs, so without it the
    rows ('a', NULL) and (NULL, 'a') would hash identically.
    """
    cols = ", ".join(f"`{c}`" for c in columns)
    nulls = ", ".join(f"ISNULL(`{c}`)" for c in columns)
    rowstr = f"CONCAT_WS(0x1f, {cols}, CONCAT({nulls}))"

    if table == "wl_bulk":
        # Deliberate weakening: hashing multi-megabyte blobs on three nodes is
        # too slow for the terminal budget. Length plus CRC32 still detects
        # divergence in practice, and the blob ring is a pressure generator
        # rather than a correctness witness.
        rowstr = "CONCAT_WS(0x1f, `bid`, `gen`, LENGTH(`body`), CRC32(`body`))"

    return (
        "SELECT COUNT(*) AS n, "
        f"BIT_XOR(CAST(CONV(SUBSTRING(SHA2({rowstr}, 256), 1, 16), 16, 10) AS UNSIGNED)) AS x, "
        f"SUM(CAST(CONV(SUBSTRING(SHA2({rowstr}, 256), 17, 16), 16, 10) AS UNSIGNED)) AS s "
        f"FROM `{SCHEMA}`.`{table}`"
    )
