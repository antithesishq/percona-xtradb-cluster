"""Detection test for the compare_schemas / compare_gtid_executed fix.

Proves three things at once, which is the point: the transient-error case no
longer reports divergence, a REAL divergence is still reported, and an error
on one node does not blind the comparison of the other two.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs
helper_stubs.install()

from pxcwl import checks

BOOM = RuntimeError("(1205, 'Lock wait timeout exceeded; try restarting transaction')")


class Cur:
    def __init__(self, node): self.node = node; self.rows = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=()):
        if self.node.raises:
            raise BOOM
        if "information_schema.columns" in sql:
            self.rows = [(c, "int") for c in self.node.cols]
        elif "gtid_executed" in sql:
            self.rows = [(self.node.gtid,)]
        else:
            self.rows = []
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass


class Conn:
    def __init__(self, cols, gtid, raises=False):
        self.cols, self.gtid, self.raises = cols, gtid, raises
    def cursor(self, *a, **k): return Cur(self)


OK = ["id", "v"]
GT = "uuid:1-100"


def case(name, conns, tables, want_schema_ok, want_gtid_ok):
    s_ok, s_det = checks.compare_schemas(conns, tables)
    g_ok, g_det = checks.compare_gtid_executed(conns)
    ok = (s_ok == want_schema_ok) and (g_ok == want_gtid_ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"        schema ok={s_ok} (want {want_schema_ok})  gtid ok={g_ok} (want {want_gtid_ok})")
    if not ok:
        print(f"        schema details: {s_det}")
        print(f"        gtid details:   {g_det}")
    return ok


results = []
T = ["t1"]

# 1. Healthy: three identical nodes.
results.append(case("all three identical -> no divergence",
    {"n1": Conn(OK, GT), "n2": Conn(OK, GT), "n3": Conn(OK, GT)}, T, True, True))

# 2. THE FIX: one node errors, the other two agree.
results.append(case("one node 1205, other two agree -> NO false divergence",
    {"n1": Conn(OK, GT, raises=True), "n2": Conn(OK, GT), "n3": Conn(OK, GT)}, T, True, True))

# 3. The fix must not blind the check: one errors, other two really differ.
results.append(case("one node 1205, other two DIFFER -> still detected",
    {"n1": Conn(OK, GT, raises=True), "n2": Conn(OK, GT), "n3": Conn(["id"], "uuid:1-99")},
    T, False, False))

# 4. Only one node readable: nothing to compare against.
results.append(case("two of three unreadable -> no claim",
    {"n1": Conn(OK, GT), "n2": Conn(OK, GT, raises=True), "n3": Conn(OK, GT, raises=True)},
    T, True, True))

# 5. Detection preserved with all three readable.
results.append(case("all readable, one diverges -> detected",
    {"n1": Conn(OK, GT), "n2": Conn(OK, GT), "n3": Conn(["id"], "uuid:1-99")}, T, False, False))

# 6. The error is still reported for triage, just not as a verdict.
_, sd = checks.compare_schemas({"n1": Conn(OK, GT, raises=True), "n2": Conn(OK, GT),
                                "n3": Conn(OK, GT)}, T)
_, gd = checks.compare_gtid_executed({"n1": Conn(OK, GT, raises=True), "n2": Conn(OK, GT),
                                      "n3": Conn(OK, GT)})
vis = "schema_unreadable" in sd and "gtid_unreadable" in gd
print(f"{'PASS' if vis else 'FAIL'}  unreadable nodes still surfaced in details")
print(f"        {sd.get('schema_unreadable')}")
print(f"        {gd.get('gtid_unreadable')}")
results.append(vis)

# 7. No '<error>' or 'error:' sentinel can reach a compared value any more.
leaked = "<error>" in repr(sd) or "error: " in repr(gd.get("gtid_executed", {}))
print(f"{'PASS' if not leaked else 'FAIL'}  no error sentinel inside compared values")
results.append(not leaked)

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
