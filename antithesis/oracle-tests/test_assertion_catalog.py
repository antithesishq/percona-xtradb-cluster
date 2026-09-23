"""Replicate the platform's static assertion scan: literal names, unique."""
import ast, pathlib, sys, collections
root = pathlib.Path(__file__).resolve().parent.parent / "workload"
FORMS = {"always", "always_or_unreachable", "sometimes", "reachable", "unreachable"}
names = collections.defaultdict(list)
dynamic = []
for f in sorted(root.rglob("*.py")):
    tree = ast.parse(f.read_text(), str(f))
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call): continue
        fn = n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
        if fn not in FORMS: continue
        idx = 0 if fn in ("reachable", "unreachable") else 1
        if len(n.args) <= idx: continue
        a = n.args[idx]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            names[a.value].append(f"{f.relative_to(root)}:{n.lineno}")
        else:
            dynamic.append(f"{f.relative_to(root)}:{n.lineno} ({fn})")
dups = {k: v for k, v in names.items() if len(v) > 1}
print(f"assertion callsites with literal names: {sum(len(v) for v in names.values())}")
print(f"distinct property names:                {len(names)}")
print(f"non-literal (invisible to the scan):    {len(dynamic)}")
for d in dynamic: print("   DYNAMIC", d)
for k, v in dups.items(): print(f"   DUPLICATE {k!r} -> {v}")
sys.exit(1 if dynamic or dups else 0)
