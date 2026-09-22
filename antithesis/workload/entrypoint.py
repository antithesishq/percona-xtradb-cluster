#!/usr/bin/env python3
"""Workload container entrypoint for the Antithesis PXC harness.

Responsibilities, in order:

1. Wait until all three PXC nodes are genuinely Synced in one Primary component
   sharing one state UUID.
2. Seed the workload schema and the checksum-oracle bookkeeping tables.
3. Prove the apply path end to end: write on node1, read it back from node2 and
   node3 with wsrep_sync_wait=1.
4. Emit the bootstrap property.
5. Emit setup_complete, then idle.

setup_complete is emitted HERE, from the entrypoint, rather than from a
``first_`` test command. Test commands do not start until after Antithesis has
already observed setup_complete, so emitting it from one would deadlock.

This skill (antithesis-setup) intentionally defines no test commands. They
belong to antithesis-workload and will land in /opt/antithesis/test/v1/.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from typing import Any

import pymysql
from antithesis.assertions import reachable
from antithesis.lifecycle import setup_complete

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

NODES: list[tuple[str, str]] = [
    ("node1", os.environ.get("PXC_NODE1_HOST", "10.20.20.11")),
    ("node2", os.environ.get("PXC_NODE2_HOST", "10.20.20.12")),
    ("node3", os.environ.get("PXC_NODE3_HOST", "10.20.20.13")),
]

MYSQL_PORT = int(os.environ.get("PXC_PORT", "3306"))
MYSQL_USER = os.environ.get("PXC_WORKLOAD_USER", "antithesis")
MYSQL_PASSWORD = os.environ.get("PXC_WORKLOAD_PASSWORD", "antithesis")

SCHEMA = os.environ.get("PXC_WORKLOAD_SCHEMA", "antithesis")

# The readiness gate has to outlast a full SST of the two joiners plus datadir
# initialization on a cold start. Generous on purpose: a timeout here means the
# run never starts, which is a far worse outcome than a slow start.
READY_TIMEOUT_SECONDS = int(os.environ.get("PXC_READY_TIMEOUT", "900"))
READY_POLL_SECONDS = float(os.environ.get("PXC_READY_POLL", "2"))
CONNECT_TIMEOUT_SECONDS = int(os.environ.get("PXC_CONNECT_TIMEOUT", "5"))

EXPECTED_CLUSTER_SIZE = int(os.environ.get("PXC_CLUSTER_SIZE", "3"))


def log(message: str) -> None:
    print(f"[workload] {message}", flush=True)


def connect(host: str, database: str | None = None) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=host,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=database,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=CONNECT_TIMEOUT_SECONDS * 4,
        write_timeout=CONNECT_TIMEOUT_SECONDS * 4,
        autocommit=True,
        charset="utf8mb4",
    )


def status_vars(conn: pymysql.connections.Connection, names: list[str]) -> dict[str, str]:
    """Read wsrep status variables by exact name."""
    placeholders = ",".join(["%s"] * len(names))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT VARIABLE_NAME, VARIABLE_VALUE "
            "FROM performance_schema.global_status "
            f"WHERE VARIABLE_NAME IN ({placeholders})",
            names,
        )
        return {row[0].lower(): row[1] for row in cur.fetchall()}


# --------------------------------------------------------------------------
# Readiness gate
# --------------------------------------------------------------------------

WSREP_STATUS_NAMES = [
    "wsrep_ready",
    "wsrep_local_state",
    "wsrep_cluster_status",
    "wsrep_cluster_size",
    "wsrep_local_state_uuid",
]


# Why each host last failed, keyed by host. Without this, the bare
# `except Exception` below reports every failure as the single word
# "unreachable", which reads like a network fault. It once hid a
# RuntimeError raised by PyMySQL's caching_sha2_password path (the
# `cryptography` package was missing from requirements.txt) and sent
# debugging toward the network for a full validate cycle. Keep the reason.
LAST_ERROR: dict[str, str] = {}


def node_state(host: str) -> dict[str, str] | None:
    """Return the wsrep status of one node, or None if it is unreachable.

    On failure the reason is recorded in LAST_ERROR[host] so the readiness
    report can name it rather than just saying "unreachable".
    """
    try:
        conn = connect(host)
    except Exception as exc:
        LAST_ERROR[host] = f"{type(exc).__name__}: {exc}"
        return None
    try:
        state = status_vars(conn, WSREP_STATUS_NAMES)
        LAST_ERROR.pop(host, None)
        return state
    except Exception as exc:
        LAST_ERROR[host] = f"{type(exc).__name__}: {exc}"
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def node_is_synced(state: dict[str, str] | None) -> bool:
    """A node is ready only when every one of these holds.

    wsrep_local_state == 4 (Synced) is the load-bearing check: the
    JOINED -> SYNCED transition additionally requires the receive queue to
    drain, which makes it a real end-to-end signal rather than a membership
    formality.
    """
    if not state:
        return False
    return (
        state.get("wsrep_ready", "").upper() == "ON"
        and state.get("wsrep_local_state", "") == "4"
        and state.get("wsrep_cluster_status", "").lower() == "primary"
        and state.get("wsrep_cluster_size", "") == str(EXPECTED_CLUSTER_SIZE)
    )


def wait_for_cluster() -> dict[str, dict[str, str]]:
    """Block until every node is Synced in one Primary component on one UUID.

    Deliberately NOT reused here: scripts/clustercheck.sh. It returns 200
    without validating the ability to commit. Its accuracy is a property this
    harness tests, so it must never become a harness dependency.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_report = 0.0

    while time.monotonic() < deadline:
        states = {name: node_state(host) for name, host in NODES}
        synced = {name: node_is_synced(s) for name, s in states.items()}

        uuids = {
            s.get("wsrep_local_state_uuid")
            for s in states.values()
            if s and s.get("wsrep_local_state_uuid")
        }
        one_lineage = len(uuids) == 1

        if all(synced.values()) and one_lineage:
            log(f"all {len(NODES)} nodes Synced on state UUID {next(iter(uuids))}")
            return {name: s for name, s in states.items() if s}

        now = time.monotonic()
        if now - last_report > 10:
            last_report = now
            summary = ", ".join(
                f"{name}="
                + (
                    "unreachable (" + LAST_ERROR.get(host, "no detail") + ")"
                    if states[name] is None
                    else f"state{states[name].get('wsrep_local_state', '?')}"
                    f"/{states[name].get('wsrep_cluster_status', '?')}"
                    f"/size{states[name].get('wsrep_cluster_size', '?')}"
                )
                for name, host in NODES
            )
            log(f"waiting for cluster: {summary}; distinct state UUIDs={len(uuids)}")

        time.sleep(READY_POLL_SECONDS)

    raise TimeoutError(
        f"cluster did not reach {EXPECTED_CLUSTER_SIZE} Synced nodes on a single "
        f"state UUID within {READY_TIMEOUT_SECONDS}s"
    )


# --------------------------------------------------------------------------
# Schema seeding
# --------------------------------------------------------------------------

# Bookkeeping tables the cross-node consistency oracle will need. The workload
# skill owns the real workload tables; these are the shared substrate.
#
# Every table has an explicit primary key: pxc_strict_mode defaults to
# ENFORCING, which blocks DML on PK-less tables outright.
SEED_STATEMENTS = [
    f"CREATE DATABASE IF NOT EXISTS `{SCHEMA}`",
    f"""CREATE TABLE IF NOT EXISTS `{SCHEMA}`.`harness_heartbeat` (
            id          BIGINT       NOT NULL AUTO_INCREMENT,
            node        VARCHAR(64)  NOT NULL,
            written_at  TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY (id)
        ) ENGINE=InnoDB""",
    f"""CREATE TABLE IF NOT EXISTS `{SCHEMA}`.`harness_epoch` (
            id          BIGINT       NOT NULL AUTO_INCREMENT,
            node        VARCHAR(64)  NOT NULL,
            state_uuid  VARCHAR(64)  NOT NULL,
            seqno       VARCHAR(64)  NOT NULL,
            observed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY (id)
        ) ENGINE=InnoDB""",
]


def seed_schema(host: str) -> None:
    log(f"seeding workload schema on {host}")
    conn = connect(host)
    try:
        with conn.cursor() as cur:
            for statement in SEED_STATEMENTS:
                cur.execute(statement)
    finally:
        conn.close()


def prove_apply_path(write_host: str, read_hosts: list[tuple[str, str]]) -> None:
    """Write on one node, read it back from the others under wsrep_sync_wait=1.

    This is stronger than the status-variable gate: it proves writesets are
    certified AND applied cluster-wide, not merely that membership looks right.
    """
    marker = f"bootstrap-{int(time.time() * 1000)}"

    conn = connect(write_host, database=SCHEMA)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `harness_heartbeat` (node) VALUES (%s)", (marker,)
            )
            cur.execute("SELECT LAST_INSERT_ID()")
            row_id = cur.fetchone()[0]
    finally:
        conn.close()

    log(f"wrote heartbeat row id={row_id} on {write_host}")

    for name, host in read_hosts:
        conn = connect(host, database=SCHEMA)
        try:
            with conn.cursor() as cur:
                # wsrep_sync_wait=1 makes this read wait for the node to catch
                # up to the last seen writeset. Without it, the server default
                # of 0 permits a stale read and this check would be vacuous.
                cur.execute("SET SESSION wsrep_sync_wait = 1")
                cur.execute(
                    "SELECT node FROM `harness_heartbeat` WHERE id = %s", (row_id,)
                )
                found = cur.fetchone()
        finally:
            conn.close()

        if not found or found[0] != marker:
            raise RuntimeError(
                f"apply path check failed: row {row_id} written on {write_host} "
                f"was not readable on {name} ({host}) under wsrep_sync_wait=1"
            )
        log(f"heartbeat row id={row_id} confirmed on {name} ({host})")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    log(f"targeting nodes: {', '.join(f'{n}={h}' for n, h in NODES)}")

    states = wait_for_cluster()
    seed_schema(NODES[0][1])
    prove_apply_path(NODES[0][1], NODES[1:])

    # Bootstrap property.
    #
    # Purpose is integration verification, not business validation: it proves
    # the Python SDK is installed, that /opt/antithesis/catalog/ cataloging
    # found this file, and that assertions reach the triage report. Business
    # invariants belong to antithesis-workload.
    #
    # The name is an inline constant string literal on purpose. Cataloging
    # statically scans this source before any run, so a name built at run time
    # (concatenated, interpolated, or passed through a variable) would silently
    # fail to catalog.
    details: dict[str, Any] = {
        "cluster_size": EXPECTED_CLUSTER_SIZE,
        "state_uuid": next(
            (s.get("wsrep_local_state_uuid") for s in states.values() if s), None
        ),
        "nodes": [name for name, _ in NODES],
    }
    reachable("workload startup: 3-node cluster reached Synced", details)
    log("bootstrap property emitted")

    # Only now is the system genuinely ready for test commands.
    setup_complete(details)
    log("setup_complete emitted; idling")

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail loudly and stay dead. Exiting non-zero without setup_complete is
        # the correct signal: Antithesis will not start test commands, and the
        # traceback is in the container log for triage.
        traceback.print_exc()
        sys.stdout.flush()
        sys.exit(1)
