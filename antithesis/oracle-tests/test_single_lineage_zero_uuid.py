"""single_lineage ignores the all-zero UUID of a node with no state.

Run fdb9d32c...-63-5, vtime 687: node2 was Inconsistent and reported
00000000-0000-0000-0000-000000000000, node1 and node3 shared the cluster UUID.
The oracle counted two lineages and failed. A zero UUID is "no state", not a
fork. A real fork (two different non-zero UUIDs) must still be caught, also
when the forked node is not Synced.

Detection: the pre-change lineage set (every non-empty UUID) is applied to the
same states and must be caught counting the zero UUID as a second lineage.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs
helper_stubs.install()

from pxcwl import verify

CLUSTER = "4e7ca4a7-c127-11f1-804c-2e28d37e9dfc"
OTHER = "b1835b58-3ed8-ee0e-7fb3-d1d72c816203"
ZERO = "00000000-0000-0000-0000-000000000000"


def st(uuid, comment="Synced"):
    return {"wsrep_local_state_uuid": uuid, "wsrep_local_state_comment": comment}


def old_lineages(states):
    return sorted({s.get("wsrep_local_state_uuid", "") for s in states.values()
                   if s and s.get("wsrep_local_state_uuid")})


FAILED = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {info}"))
    if not cond:
        FAILED.append(name)


run_687 = {"node1": st(CLUSTER), "node2": st(ZERO, "Inconsistent"), "node3": st(CLUSTER)}
got = verify._lineages(run_687)
check("an Inconsistent node's zero UUID is not a second lineage", got == [CLUSTER], got)

fork = {"node1": st(CLUSTER), "node2": st(OTHER, "Joining"), "node3": st(CLUSTER)}
got = verify._lineages(fork)
check("a real fork is still caught, even on a node that is not Synced", len(got) == 2, got)

only_one = {"node1": st(CLUSTER), "node2": st(ZERO, "Inconsistent"), "node3": None}
reporting = [s for s in only_one.values() if verify._lineage_of(s)]
check("a zero-UUID node does not count toward the two-reporter gate", len(reporting) == 1, reporting)

got = old_lineages(run_687)
check("detection: old behaviour counts the zero UUID as a second lineage", len(got) == 2, got)

print()
print("FAILED: " + (", ".join(FAILED) if FAILED else "none"))
sys.exit(1 if FAILED else 0)
