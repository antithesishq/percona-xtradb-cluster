"""Workload schema.

Every table carries an explicit PRIMARY KEY, with exactly one deliberate
exception (``wl_nopk``). This is not a style choice: pxc_strict_mode is left at
the ENFORCING default, which blocks DML on primary-key-less tables outright, so
a PK-less table can only be written inside a fenced PERMISSIVE window.

None of the new tables use TIMESTAMP DEFAULT CURRENT_TIMESTAMP. Row-based
replication ships the evaluated value so it would in fact be safe, but these
tables are compared byte-for-byte across nodes and there is no reason to leave
even a question about server-evaluated defaults in the checksum set.
"""

from __future__ import annotations

from . import config

SCHEMA = config.SCHEMA

_SCRATCH_DDL = """CREATE TABLE IF NOT EXISTS `{name}` (
  sid BIGINT UNSIGNED NOT NULL,
  v   BIGINT NOT NULL DEFAULT 0,
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
    # The one PK-less table. Only ever written inside a fenced PERMISSIVE
    # window, and compared under its own property name.
    """CREATE TABLE IF NOT EXISTS `wl_nopk` (
         a INT NOT NULL,
         b VARCHAR(64) NOT NULL
       ) ENGINE=InnoDB""",
]


def create_all(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{SCHEMA}`")
        cur.execute(f"USE `{SCHEMA}`")
        for ddl in WORKLOAD_TABLES:
            cur.execute(ddl)
        for name in config.SCRATCH_TABLES:
            cur.execute(_SCRATCH_DDL.format(name=name))


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
