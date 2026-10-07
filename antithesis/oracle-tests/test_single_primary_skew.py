"""single_primary_component compares only Primary claims read at the same time.

Run 69449aa5...-63-5, vtime 212.50: the probe read the nodes one after
another. A 5 s connect timeout to a down node put the next read 5 s later, and
two one-member Primary views that never existed at the same moment looked like
split brain. The probe now reads every node at once and compares a pair only
when both reads returned within PRIMARY_SAMPLE_WINDOW_SECONDS.

A real split brain must still be caught: two disjoint Primary views read
together, also when a third node is down or slow, and also when one side is a
Donor (a split brain often starts a state transfer).

Detection: the pre-change pairing (no time check) is applied to the skewed
reads and must be caught calling them split brain; the pre-change sequential
read must be caught putting a slow node's 5 s delay into the next node's time.
"""
import sys, pathlib, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs
helper_stubs.install()

from pxcwl import config, db, probe

W = config.PRIMARY_SAMPLE_WINDOW_SECONDS


def prim(*members, state="4"):
    return {"wsrep_cluster_status": "Primary", "wsrep_local_state": state,
            "wsrep_incoming_addresses": ",".join(f"{m}:3306" for m in members)}


def old_disjoint(states):
    sets = {}
    for n, st in states.items():
        if st and st.get("wsrep_cluster_status", "").lower() == "primary":
            sets[n] = set(st["wsrep_incoming_addresses"].split(","))
    names = sorted(sets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if not (sets[names[i]] & sets[names[j]]):
                return (names[i], names[j])
    return None


FAILED = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {info}"))
    if not cond:
        FAILED.append(name)


# Run 69449aa5 shape: node1 alone, node2 down (5 s timeout), node3 alone 5 s later.
skewed = {"node1": prim("pxc-node1"), "node2": None, "node3": prim("pxc-node3")}
v = probe._split_brain(skewed, {"node1": 100.0, "node3": 105.1}, W)
check("reads 5 s apart give no verdict", v["compared_pairs"] == 0 and v["disjoint_pair"] is None, v)
check("the skipped pair is in the details", v["skipped_for_skew"][0]["pair"] == ["node1", "node3"], v)

v = probe._split_brain(skewed, {"node1": 100.0, "node3": 100.05}, W)
check("a real split brain read together is caught, node2 down", v["disjoint_pair"] == ("node1", "node3"), v)

donor = {"node1": prim("pxc-node1"), "node2": prim("pxc-node2", "pxc-node3", state="2"),
         "node3": prim("pxc-node2", "pxc-node3")}
v = probe._split_brain(donor, {"node1": 100.0, "node2": 100.02, "node3": 100.04}, W)
check("a split brain whose other side is a Donor is caught", v["disjoint_pair"] is not None, v)

healthy = {n: prim("pxc-node1", "pxc-node2", "pxc-node3") for n in ("node1", "node2", "node3")}
v = probe._split_brain(healthy, {"node1": 100.0, "node2": 100.01, "node3": 100.02}, W)
check("one shared Primary view passes", v["compared_pairs"] == 3 and v["disjoint_pair"] is None, v)

# The parallel read: node2 is slow (1.5 s here, more than the window), and
# node1 and node3 must still be read together.
real_status = db.node_status
def fake_status(host):
    if host == dict(config.NODES)["node2"]:
        time.sleep(1.5)
        return None
    return prim(host)
db.node_status = fake_status
try:
    states, at = db.cluster_status_sampled()
    gap = abs(at["node1"] - at["node3"])
    check("a slow node does not delay the other nodes' reads", gap < W and "node2" not in at, at)
    v = probe._split_brain(states, at, W)
    check("so the split brain around a slow node is compared", v["disjoint_pair"] == ("node1", "node3"), v)

    seq = {}
    for name, host in config.NODES:
        fake_status(host)
        seq[name] = time.time()
    check("detection: a sequential read puts node2's delay into node3's time",
          seq["node3"] - seq["node1"] > W, seq)
finally:
    db.node_status = real_status

check("detection: the old pairing calls the skewed reads split brain",
      old_disjoint(skewed) == ("node1", "node3"))

print()
print("FAILED: " + (", ".join(FAILED) if FAILED else "none"))
sys.exit(1 if FAILED else 0)
