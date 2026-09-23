"""MySQL connectivity and error classification.

Deliberately mirrors the conventions already established in
``workload/entrypoint.py`` -- same timeouts, same autocommit default, same
charset, and the same "record why a host failed" discipline. That last one is
not decoration: a bare failure reason once presented as "every node
unreachable" and sent debugging toward the network for a full validate cycle,
when the real cause was a missing Python package (entrypoint.py:105-111).
"""

from __future__ import annotations

import time

import pymysql

from . import config

# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------

# Clean, expected, node-still-alive rejections. Every one of these leaves the
# connection usable and leaves NO trace of the transaction in the cluster:
# certification failure (deterministic -- galera turns the writeset into a
# dummy on every node), duplicate key, lock wait timeout, node-not-ready,
# and the query-interrupted / kill family.
ER_LOCK_DEADLOCK = 1213          # also: BF abort surfaced as deadlock
ER_DUP_ENTRY = 1062
ER_LOCK_WAIT_TIMEOUT = 1205
ER_UNKNOWN_COM_ERROR = 1047      # "WSREP has not yet prepared node"
ER_LOCK_NOWAIT = 3572
ER_QUERY_INTERRUPTED = 1317
ER_UNKNOWN_ERROR = 1105
ER_OPTION_PREVENTS_STATEMENT = 1290
ER_CANT_EXECUTE_IN_READ_ONLY = 1836

CLEAN_REJECTIONS: frozenset[int] = frozenset(
    {
        ER_LOCK_DEADLOCK,
        ER_DUP_ENTRY,
        ER_LOCK_WAIT_TIMEOUT,
        ER_UNKNOWN_COM_ERROR,
        ER_LOCK_NOWAIT,
        ER_QUERY_INTERRUPTED,
        ER_UNKNOWN_ERROR,
        ER_OPTION_PREVENTS_STATEMENT,
        ER_CANT_EXECUTE_IN_READ_ONLY,
    }
)

# Connection-level failures: the outcome of an in-flight COMMIT is unknowable.
CR_SERVER_GONE_ERROR = 2006
CR_SERVER_LOST = 2013

# Documented legal outcomes of a locking read. Anything else is a finding.
LOCKING_READ_LEGAL: frozenset[int] = frozenset(
    {ER_LOCK_DEADLOCK, ER_LOCK_NOWAIT, ER_LOCK_WAIT_TIMEOUT, ER_QUERY_INTERRUPTED}
)

# Errnos that mean a locking read went genuinely wrong, as opposed to hitting a
# documented lock outcome or an injected fault. Kept as a small explicit set
# because the safe default for an UNRECOGNISED errno is "cannot judge", not
# "violation" -- otherwise a schema that was never seeded (ER_NO_SUCH_TABLE)
# would fire the property on every statement.
ER_PARSE_ERROR = 1064
ER_WRONG_ARGUMENTS = 1210
ER_CRASHED_ON_USAGE = 1194
ER_NOT_SUPPORTED_YET = 1235
FATAL_STATEMENT_ERRNOS: frozenset[int] = frozenset(
    {ER_PARSE_ERROR, ER_WRONG_ARGUMENTS, ER_CRASHED_ON_USAGE, ER_NOT_SUPPORTED_YET}
)

# Errnos a SCHEMA DDL statement may legitimately hit because of the
# environment. Deliberately NOT CLEAN_REJECTIONS: that set exists for the
# statement path and includes ER_UNKNOWN_ERROR (1105), which is how PXC reports
# a strict-mode rejection. Treating 1105 as environment here would put us right
# back where run a359f1f8-63-0 was -- a seed that fails, exits 0, and produces a
# green report over a workload that never ran.
SCHEMA_DDL_ENVIRONMENT_ERRNOS: frozenset[int] = frozenset(
    {
        CR_SERVER_GONE_ERROR,
        CR_SERVER_LOST,
        ER_UNKNOWN_COM_ERROR,          # node not ready; it will be later
        ER_LOCK_WAIT_TIMEOUT,          # TOI waiting behind something
        ER_LOCK_DEADLOCK,
        ER_QUERY_INTERRUPTED,          # killed mid-DDL
        ER_OPTION_PREVENTS_STATEMENT,
        ER_CANT_EXECUTE_IN_READ_ONLY,
    }
)

LAST_ERROR: dict[str, str] = {}


def errno_of(exc: BaseException) -> int | None:
    """Extract a MySQL errno from a PyMySQL exception, if it carries one."""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def connect(host: str, database: str | None = None, *, timeout: int | None = None):
    t = config.CONNECT_TIMEOUT_SECONDS if timeout is None else timeout
    return pymysql.connect(
        host=host,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=database,
        connect_timeout=t,
        read_timeout=t * 4,
        write_timeout=t * 4,
        autocommit=True,
        charset="utf8mb4",
    )


def connect_with_retry(host: str, database: str | None = None, *, attempts: int = 3):
    """Connect, tolerating the transient failures fault injection produces.

    Returns None rather than raising: a driver command must keep making
    progress against other nodes instead of bailing, and an unreachable node is
    an expected condition here, not an exceptional one.
    """
    delay = 0.5
    for attempt in range(attempts):
        try:
            conn = connect(host, database)
            LAST_ERROR.pop(host, None)
            return conn
        except Exception as exc:  # noqa: BLE001 - every failure mode is expected
            LAST_ERROR[host] = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay *= 2
    return None


def close_quietly(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - closing a dead connection is not news
        pass


def is_alive(conn) -> bool:
    """Whether the connection still works.

    This is the discriminator between a clean rejection and an unknown-outcome
    write. Every clean rejection leaves the session usable; a node that died
    mid-COMMIT does not. See journal.classify.
    """
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchall()
        return True
    except Exception:  # noqa: BLE001
        return False


def schema_ddl_failure_is_environmental(conn, exc: BaseException) -> bool:
    """Whether a failed schema DDL says something about PXC or about us.

    A server that answered, is still answering, and rejected our DDL for a
    reason outside SCHEMA_DDL_ENVIRONMENT_ERRNOS has told us the DDL is wrong.
    That is a harness bug and the seed must exit non-zero for it. Everything
    else -- a dropped connection, a node that is not ready, a killed
    statement -- is fault injection doing its job.
    """
    errno = errno_of(exc)
    if errno is None:
        # No errno at all means it never got as far as a server verdict.
        return True
    if errno in SCHEMA_DDL_ENVIRONMENT_ERRNOS:
        return True
    return not is_alive(conn)


# --------------------------------------------------------------------------
# Status variables
# --------------------------------------------------------------------------

WSREP_STATUS_NAMES = [
    "wsrep_ready",
    "wsrep_local_state",
    "wsrep_local_state_comment",
    "wsrep_cluster_status",
    "wsrep_cluster_size",
    "wsrep_local_state_uuid",
    "wsrep_last_committed",
    "wsrep_local_recv_queue",
    "wsrep_flow_control_paused_ns",
    "wsrep_flow_control_sent",
    "wsrep_flow_control_active",
    "wsrep_desync_count",
    "wsrep_incoming_addresses",
    "wsrep_thread_count",
    "wsrep_cert_deps_distance",
]


def status_vars(conn, names: list[str]) -> dict[str, str]:
    placeholders = ",".join(["%s"] * len(names))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT VARIABLE_NAME, VARIABLE_VALUE "
            "FROM performance_schema.global_status "
            f"WHERE VARIABLE_NAME IN ({placeholders})",
            names,
        )
        return {row[0].lower(): row[1] for row in cur.fetchall()}


def global_vars(conn, names: list[str]) -> dict[str, str]:
    placeholders = ",".join(["%s"] * len(names))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT VARIABLE_NAME, VARIABLE_VALUE "
            "FROM performance_schema.global_variables "
            f"WHERE VARIABLE_NAME IN ({placeholders})",
            names,
        )
        return {row[0].lower(): row[1] for row in cur.fetchall()}


def node_status(host: str) -> dict[str, str] | None:
    """wsrep status of one node, or None if unreachable (reason in LAST_ERROR)."""
    conn = connect_with_retry(host, attempts=1)
    if conn is None:
        return None
    try:
        return status_vars(conn, WSREP_STATUS_NAMES)
    except Exception as exc:  # noqa: BLE001
        LAST_ERROR[host] = f"{type(exc).__name__}: {exc}"
        return None
    finally:
        close_quietly(conn)


def is_synced(state: dict[str, str] | None) -> bool:
    """The same readiness definition the entrypoint's gate uses.

    wsrep_local_state == 4 (Synced) is load-bearing: the JOINED -> SYNCED
    transition additionally requires the receive queue to drain, which makes it
    an end-to-end signal rather than a membership formality.
    """
    if not state:
        return False
    return (
        state.get("wsrep_ready", "").upper() == "ON"
        and state.get("wsrep_local_state", "") == "4"
        and state.get("wsrep_cluster_status", "").lower() == "primary"
        and state.get("wsrep_cluster_size", "") == str(config.EXPECTED_CLUSTER_SIZE)
    )


def cluster_status() -> dict[str, dict[str, str] | None]:
    return {name: node_status(host) for name, host in config.NODES}


def clustercheck_green(state: dict[str, str] | None, maint_mode: str | None) -> bool:
    """Recompute the shipped health check's verdict from its own inputs.

    Deliberately NOT by invoking scripts/clustercheck.sh: the accuracy of that
    script is itself a property under test, so it must never become a harness
    dependency. The formula mirrors clustercheck.sh -- Primary, local state 4
    (or 2 when donors count as available), maintenance disabled -- and notably
    never consults wsrep_ready, flow control, or queue depth, which is exactly
    the gap the property attacks.
    """
    if not state:
        return False
    if state.get("wsrep_cluster_status", "").lower() != "primary":
        return False
    if state.get("wsrep_local_state", "") != "4":
        return False
    if maint_mode is not None and maint_mode.upper() != "DISABLED":
        return False
    return True
