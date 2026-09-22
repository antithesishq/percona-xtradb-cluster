"""Per-timeline swarm parameters.

This is the mechanism that replaces one-test-per-property. Rather than writing
a targeted scenario per bug, each timeline draws its own personality -- which
action classes it favours, how hard it pushes, which nodes it talks to -- from
wide ranges that include the extremes. Any single timeline goes deep into one
corner of the state space; the union across many timelines covers the surface,
and finds bugs that uniform mixing never reaches.

Two axes compose here:

  shape  how often each menu item is drawn (this module)
  menu   which values are on the menu at all (rnd.sample_menu and ops)

The weight bag deliberately contains zeros. An entire action class being absent
for a timeline is the limiting case of skew, and it is valuable: it lets other
classes run without interference from, say, a constant stream of TOI DDL.
"""

from __future__ import annotations

from . import config, rnd

# Two zeros in a bag of seven means each class is dropped for roughly 29% of
# timelines.
CLASS_WEIGHT_BAG = [0, 0, 1, 3, 10, 30, 100]

# Most timelines should pull one or two levers, not eight. Three zeros in six.
LEVER_WEIGHT_BAG = [0, 0, 0, 1, 5, 20]

ACTION_CLASSES = ["write", "conflict", "bulk", "sr", "ddl", "admin", "read"]

# Classes that actually generate replicated traffic. If the swarm zeroes all of
# them the timeline would drive nothing at all and the branch would be wasted
# budget, so one is forced back on. This is the only place the swarm is clamped.
TRAFFIC_CLASSES = ["write", "conflict", "bulk", "sr", "ddl"]

LEVERS = [
    "applier_resize",
    "desync_cycle",
    "maint_mode_cycle",
    "pc_weight",
    "gmcast_isolate",
    "cluster_address_reset",
    "graceful_shutdown",
    "ws_size_squeeze",
    "strict_mode_window",
    "backup_lock",
]


def draw() -> dict[str, object]:
    """Draw the timeline's personality. Called once, from the first_ command."""
    weights = {c: float(rnd.choice(CLASS_WEIGHT_BAG)) for c in ACTION_CLASSES}
    if all(weights[c] == 0 for c in TRAFFIC_CLASSES):
        weights[rnd.choice(TRAFFIC_CLASSES)] = 1.0

    lever_weights = {l: float(rnd.choice(LEVER_WEIGHT_BAG)) for l in LEVERS}

    return {
        "class_weight": weights,
        "lever_weight": lever_weights,
        # Bounded work per invocation; Antithesis re-runs the driver for more.
        "ops_per_invocation": rnd.choice([5, 25, 100, 400, 1500]),
        "concurrency_sessions": rnd.choice([1, 2, 4, 8]),
        # 0 is a deliberate flood.
        "think_time_ms": rnd.choice([0, 0, 1, 25, 200]),
        # pin_one leaves two nodes write-idle, so a partition lands on an idle
        # member and a rejoin is observable against a quiet baseline.
        "node_skew": rnd.choice(["uniform", "pin_one", "two_only", "avoid_node1"]),
        "pinned_node": rnd.choice([name for name, _ in config.NODES]),
        "txn_size": rnd.choice([1, 2, 8, 40]),
        # hot_keyspace=1 is maximal contention: every writer fights for one row.
        "hot_keyspace": rnd.choice([1, 4, 32, 512]),
        # Autocommit-vs-explicit posture is not a separate knob: it is already
        # expressed by the class weights, since txn_multi_statement and
        # sr_session use explicit transactions while the rest are autocommit.
        #
        # SERIALIZABLE is drawn deliberately even though Galera documents it as
        # unsupported in multi-master: exercising a configuration the docs warn
        # against is the point, and an unrecognised errno from it is treated as
        # "cannot judge" rather than as a violation.
        "isolation": rnd.choice(
            ["REPEATABLE READ", "READ COMMITTED", "SERIALIZABLE"]
        ),
        "sync_wait_level": rnd.choice([0, 1, 3, 7]),
        "bulk_bytes": rnd.choice([65536, 524288, 2097152, config.BULK_MAX_BYTES]),
        # 0 means streaming replication off for this timeline.
        "sr_fragment_size": rnd.choice([0, 0, 1, 16, 512]),
        "sr_fragment_unit": rnd.choice(["bytes", "rows", "statements"]),
        "pkless_enabled": rnd.choice([False, False, True]),
        # 0 is the sharp arm for retry-exactly-once: no silent retry at all.
        "retry_autocommit": rnd.choice([0, 1, 4]),
        "optimistic_pa": rnd.choice(["yes", "no"]),
        "fc_limit": rnd.choice([16, 100, 500]),
    }


def jitter(profile: dict[str, object]) -> dict[str, object]:
    """Per-invocation variation on top of the timeline profile.

    Keeps a timeline from being perfectly monotone without averaging away the
    skew that makes swarm testing work: the class weights themselves are left
    alone, and only the work size and node policy move.
    """
    out = dict(profile)
    base_ops = int(profile.get("ops_per_invocation", 100))
    out["ops_per_invocation"] = max(1, rnd.randint(base_ops // 2, base_ops * 3 // 2))
    if rnd.chance(0.2):
        out["node_skew"] = rnd.choice(["uniform", "pin_one", "two_only", "avoid_node1"])
    return out


def pick_node(profile: dict[str, object]) -> tuple[str, str]:
    """Choose a node to talk to, honouring the timeline's skew."""
    names = [name for name, _ in config.NODES]
    skew = str(profile.get("node_skew", "uniform"))

    if skew == "pin_one":
        name = str(profile.get("pinned_node", names[0]))
    elif skew == "two_only":
        # A random pair, not always the first two -- otherwise the
        # {node1, node3} pairing is unreachable and one third of the
        # two-node skews never happen.
        pair = rnd.shuffled(names)[:2]
        name = rnd.choice(pair)
    elif skew == "avoid_node1":
        name = rnd.choice(names[1:])
    else:
        name = rnd.choice(names)
    return name, config.NODE_HOSTS[name]
