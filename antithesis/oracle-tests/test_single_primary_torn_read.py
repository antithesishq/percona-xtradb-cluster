"""single_primary_component re-reads a disjoint pair before it fails.

Run fdb9d32c...-63-5, input_hash 7856742902588311048, vtime 146.11: node2
left the cluster after it failed to apply a writeset. Galera set node2's
wsrep_incoming_addresses to {node2} about 20 ms before it set
wsrep_cluster_status to non-Primary. The probe read node2 in that gap, and
read node3 as the real one-member Primary at the same time. Galera never had
two Primary components (node2's log goes from PRIM {node2,node3} straight to
NON_PRIM {node2}).

The probe now reads a disjoint pair again after PRIMARY_CONFIRM_DELAY_SECONDS:
- still disjoint            -> fails (a real split brain must still be caught)
- now overlapping           -> passes, coverage claim fires
- a node is now non-Primary -> no verdict, coverage claim fires
- a node does not answer    -> no verdict, never a pass

Detection: the pre-change single read (_split_brain alone) is applied to the
vtime 146.11 reads and must be caught calling them split brain.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs
helper_stubs.install()

from antithesis import assertions as A
from pxcwl import config, db, probe

config.PRIMARY_CONFIRM_DELAY_SECONDS = 0.0
W = config.PRIMARY_SAMPLE_WINDOW_SECONDS
TORN = "[coverage] a disjoint Primary pair was not seen again on a re-read"


def prim(*members):
    return {"wsrep_cluster_status": "Primary",
            "wsrep_incoming_addresses": ",".join(f"{m}:3306" for m in members)}


def nonprim(*members):
    return {"wsrep_cluster_status": "non-Primary",
            "wsrep_incoming_addresses": ",".join(f"{m}:3306" for m in members)}


# The reads at vtime 146.11: node1 isolated and down to the probe, node2 torn,
# node3 the real one-member Primary.
FIRST = {"node1": None, "node2": prim("10.20.20.12"), "node3": prim("10.20.20.13")}
FIRST_AT = {"node2": 100.000, "node3": 100.004}

FAILED = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {info}"))
    if not cond:
        FAILED.append(name)


def run(re_read):
    """Drive _confirmed_split_brain with a fixed re-read result."""
    A.FIRED.clear()
    asked = []

    def fake(nodes=None):
        asked.append([n for n, _ in nodes])
        return re_read, {n: 100.6 for n, st in re_read.items() if st is not None}

    db.cluster_status_sampled = fake
    v = probe._confirmed_split_brain(FIRST, FIRST_AT)
    torn = [r for r in A.FIRED if r["message"] == TORN]
    return v, torn, asked


v, torn, asked = run({"node2": nonprim("10.20.20.12"), "node3": prim("10.20.20.13")})
check("vtime 146.11: the leaving node is non-Primary on the re-read, so no verdict",
      v["compared_pairs"] == 0 and v["disjoint_pair"] is None, v)
check("only the disjoint pair is read again", asked == [["node2", "node3"]], asked)
check("the torn read is counted as left_primary",
      len(torn) == 1 and torn[0]["details"]["outcome"] == "left_primary", torn)
check("the first read stays in the details", v["first_read"]["disjoint_pair"] == ("node2", "node3"), v)

v, torn, _ = run({"node2": prim("10.20.20.12"), "node3": prim("10.20.20.13")})
check("a split brain that is still there on the re-read fails",
      v["compared_pairs"] >= 1 and v["disjoint_pair"] == ("node2", "node3"), v)
check("and is not counted as a torn read", not torn, torn)

v, torn, _ = run({"node2": prim("10.20.20.12", "10.20.20.13"), "node3": prim("10.20.20.12", "10.20.20.13")})
check("a pair that overlaps on the re-read passes",
      v["compared_pairs"] >= 1 and v["disjoint_pair"] is None, v)
check("and is counted as overlapping", len(torn) == 1 and torn[0]["details"]["outcome"] == "overlapping", torn)

v, torn, _ = run({"node2": None, "node3": prim("10.20.20.13")})
check("a node that does not answer the re-read gives no verdict, not a pass",
      v["compared_pairs"] == 0 and v.get("re_read_outcome") == "unreachable", v)
check("and is not counted as a torn read", not torn, torn)

healthy = {n: prim("a", "b", "c") for n in ("node1", "node2", "node3")}
A.FIRED.clear()
called = []
db.cluster_status_sampled = lambda nodes=None: called.append(1) or ({}, {})
v = probe._confirmed_split_brain(healthy, {"node1": 1.0, "node2": 1.0, "node3": 1.0})
check("a healthy cluster is not read again", not called and v["disjoint_pair"] is None, v)

old = probe._split_brain(FIRST, FIRST_AT, W)
check("detection: the old single read calls vtime 146.11 split brain",
      old["disjoint_pair"] == ("node2", "node3"), old)

print()
print("FAILED: " + (", ".join(FAILED) if FAILED else "none"))
sys.exit(1 if FAILED else 0)
