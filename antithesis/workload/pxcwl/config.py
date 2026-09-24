"""Static configuration: nodes, paths, tunables, and the table registry."""

from __future__ import annotations

import os

# --------------------------------------------------------------------------
# Topology. Same env-var contract as workload/entrypoint.py.
# --------------------------------------------------------------------------

NODES: list[tuple[str, str]] = [
    ("node1", os.environ.get("PXC_NODE1_HOST", "10.20.20.11")),
    ("node2", os.environ.get("PXC_NODE2_HOST", "10.20.20.12")),
    ("node3", os.environ.get("PXC_NODE3_HOST", "10.20.20.13")),
]
NODE_HOSTS: dict[str, str] = {name: host for name, host in NODES}

MYSQL_PORT = int(os.environ.get("PXC_PORT", "3306"))
MYSQL_USER = os.environ.get("PXC_WORKLOAD_USER", "antithesis")
MYSQL_PASSWORD = os.environ.get("PXC_WORKLOAD_PASSWORD", "antithesis")
SCHEMA = os.environ.get("PXC_WORKLOAD_SCHEMA", "antithesis")
EXPECTED_CLUSTER_SIZE = int(os.environ.get("PXC_CLUSTER_SIZE", "3"))

CONNECT_TIMEOUT_SECONDS = int(os.environ.get("PXC_CONNECT_TIMEOUT", "5"))

# --------------------------------------------------------------------------
# Cross-invocation state. Container-local on purpose: every test command runs
# in the single pxc-workload container, so no compose volume is required.
# --------------------------------------------------------------------------

JOURNAL_DIR = os.environ.get("PXC_JOURNAL_DIR", "/opt/antithesis/journal")
JOURNAL_PATH = os.path.join(JOURNAL_DIR, "pxc.sqlite3")

# --------------------------------------------------------------------------
# Per-invocation budgets. A test command must eventually exit; each driver
# invocation is one bounded chunk of work and Antithesis re-runs it for more.
# --------------------------------------------------------------------------

TRAFFIC_WALL_BUDGET_SECONDS = float(os.environ.get("PXC_TRAFFIC_BUDGET", "100"))
PROBE_WALL_BUDGET_SECONDS = float(os.environ.get("PXC_PROBE_BUDGET", "90"))
PROBE_INTERVAL_SECONDS = float(os.environ.get("PXC_PROBE_INTERVAL", "3"))
# Scan a live node's error log every Nth probe iteration, not every one: five
# LIKE queries per node per pass is real load, and performance_schema.error_log
# is a ring buffer that holds an event for far longer than one interval.
ERROR_LOG_SCAN_EVERY = int(os.environ.get("PXC_ERROR_LOG_SCAN_EVERY", "5"))
VERIFY_BUDGET_SECONDS = float(os.environ.get("PXC_VERIFY_BUDGET", "600"))

# --------------------------------------------------------------------------
# Bounds that assertions compare against. Every one of these is a calibration
# value: the first triage round is the measurement that sets it. They are
# deliberately generous, because a tight bound that fires on legitimate slow
# recovery is worse than a loose bound that still catches a true wedge.
# --------------------------------------------------------------------------

# Commit-progress freeze: all nodes claim Synced/Primary, nobody is a donor or
# joiner, and our own probe write is failing, for this long.
WEDGE_WINDOW_SECONDS = float(os.environ.get("PXC_WEDGE_WINDOW", "300"))
# Sustained clustercheck-green window that must contain a committed write.
GREEN_WINDOW_SECONDS = float(os.environ.get("PXC_GREEN_WINDOW", "120"))
# SQL SHUTDOWN to port-closed. pxc_maint_transition_period is 10s by default
# (my.cnf:78) and applier drain rides on top of it.
SHUTDOWN_BOUND_SECONDS = float(os.environ.get("PXC_SHUTDOWN_BOUND", "120"))
# Applier resize settle.
RESIZE_SETTLE_SECONDS = float(os.environ.get("PXC_RESIZE_SETTLE", "60"))
# Galera's gcs.fc_limit default is 16 (not set in my.cnf). The recv-queue bound
# is a large multiple of it: flow control is supposed to be the thing that keeps
# the queue bounded, so this catches runaway, not normal backpressure.
FC_LIMIT_DEFAULT = int(os.environ.get("PXC_FC_LIMIT", "16"))
RECV_QUEUE_SLACK = int(os.environ.get("PXC_RECV_QUEUE_SLACK", "100"))
# An absolute ceiling alongside the multiple of fc_limit. With a swarmed
# fc_limit of 500 the multiple alone allows 50,000 queued writesets, and at
# multi-megabyte writesets the node would be out of memory long before that --
# leaving the assertion unfirable in the timelines that need it most.
RECV_QUEUE_ABS_MAX = int(os.environ.get("PXC_RECV_QUEUE_ABS_MAX", "20000"))

# gcache.size is 16M (my.cnf:95). A writeset larger than the gcache forces a
# full SST on every subsequent rejoin, so the bulk generator stays under half.
GCACHE_BYTES = 16 * 1024 * 1024
BULK_MAX_BYTES = GCACHE_BYTES // 2

# --------------------------------------------------------------------------
# Table registry.
#
# CHECKSUM_TABLES are compared across nodes by the terminal oracle. Membership
# is deliberate: a table only belongs here if divergence in it is a genuine
# consistency bug rather than an artifact of how the workload drives it.
# --------------------------------------------------------------------------

CHECKSUM_TABLES: list[str] = [
    "wl_witness",
    "wl_hot",
    "wl_uk",
    "wl_fk_parent",
    "wl_fk_child",
    "wl_bulk",
    "wl_autoinc",
    "wl_probe",
    "harness_heartbeat",
    "harness_epoch",
]

# Compared under its own property name so that a red on a documented PXC
# limitation cannot mask a red on the real invariant.
NOPK_TABLE = "wl_nopk"

# DDL targets. A fixed pool keeps the schema namespace finite and enumerable,
# so "no DDL residue after reconvergence" is a decidable question. Seeded once
# and never dropped: every other DDL shape needs these to be there.
#
# The indexes, columns and constraints ON these tables come and go, and which
# direction is legal at any moment is read from the server's catalog rather
# than guessed -- see ddl._toggle for why a coin flip there was the same bug
# EPHEMERAL_TABLE describes below, one level down.
SCRATCH_TABLES: list[str] = [f"wl_scratch_{i}" for i in range(4)]

# The ONLY table ddl_create_drop touches. Held apart from SCRATCH_TABLES
# deliberately. When create/drop random-walked the shared pool, the pool spent
# roughly half its time half-missing and every other DDL shape failed
# ER_NO_SUCH_TABLE against it -- measured at 632 of 1428 DDL episodes in one
# four-minute local run, with an inconsistency vote burned on each.
#
# That is not harmless noise. property-catalog.md's "un-injected-vote rule"
# makes any inconsistency vote not attributable to injected sabotage count as
# evidence of divergence, so ambient harness-made votes destroy the signal the
# terminal checksum oracle depends on. Real vote coverage comes from the
# sabotage-fenced variant, where the disagreement is deliberate.
#
# Nothing seeds this table: create_drop's own IF NOT EXISTS / IF EXISTS pair
# owns its whole lifecycle, and compare_table_set enumerates the schema, so it
# still gets cross-node agreement checked for free.
EPHEMERAL_TABLE = "wl_scratch_ephemeral"

# wl_bulk holds a bounded ring of large blobs; without the bound, container
# disk (there are no volumes) fills up.
BULK_RING_SIZE = 32

# Upper bound on witness rows per timeline. The table is write-once and nothing
# prunes it, the containers have no volumes, and the terminal oracle hashes
# every row on three nodes -- so an unbounded witness table costs node disk and
# can push the checksum pass past the verification budget.
WITNESS_ROW_CAP = int(os.environ.get("PXC_WITNESS_ROW_CAP", "2000000"))

# Hot-row keyspace ceiling; the timeline's actual keyspace is drawn from a menu.
HOT_KEYSPACE_MAX = 512
FK_PARENT_ROWS = 64
