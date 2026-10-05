"""Every property the harness emits carries the right label.

Why: the findings go to the Percona team, and each property name must say
whether a production (release) build would fail the same way (see "Property
labels" in workload/README.md):

    [prod]        a release build fails the same way, or the check is on what
                  a client sees
    [debug-only]  the check exists only in this Debug build
    [prod?]       the log cannot tell
    [coverage]    a reach claim, not a bug signal

Checked here, without running anything:
- every workload assertion: a literal message that starts with the label its
  assertion type implies (always / always_or_unreachable / unreachable ->
  [prod], reachable / sometimes -> [coverage]);
- every supervisor message constant (A_*="..." in pxc-node/entrypoint.sh) and
  every per-site name the supervisor builds at run time.

Detection: an unlabeled name and a wrongly labeled one are fed to the same
check, which must reject both. Otherwise a green result would not tell a
working check from one that never looks.
"""
import ast, pathlib, re, sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
LABELS = ("[prod] ", "[debug-only] ", "[prod?] ", "[coverage] ")
BY_TYPE = {"always": "[prod] ", "always_or_unreachable": "[prod] ", "unreachable": "[prod] ",
           "reachable": "[coverage] ", "sometimes": "[coverage] "}

fails = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def workload_problems(source: str, filename: str) -> list[str]:
    """Messages whose label does not match their assertion type."""
    problems = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        if fn not in BY_TYPE:
            continue
        msgs = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if not msgs:
            continue
        if not msgs[0].startswith(BY_TYPE[fn]):
            problems.append(f"{filename}:{node.lineno} {fn}({msgs[0]!r})")
    return problems


# 1. Workload assertions.
files = sorted((ROOT / "workload" / "pxcwl").glob("*.py")) + [ROOT / "workload" / "entrypoint.py"]
problems = []
for f in files:
    src = f.read_text()
    problems += workload_problems(src, f.name)
for p in problems:
    print("       ", p)
check("every workload assertion carries the label of its type", not problems)

# 2. Supervisor message constants and run-time names.
entry = (ROOT / "pxc-node" / "entrypoint.sh").read_text()
consts = re.findall(r'^A_[A-Z_]+="([^"]*)"', entry, re.M)
bad = [c for c in consts if not c.startswith(LABELS)]
for b in bad:
    print("       ", b)
check(f"every supervisor A_* message is labeled ({len(consts)} found)", consts and not bad)
for built in ("[debug-only] mysqld assertion failed at ", "[prod] mysqld assertion failed at ",
              "[prod?] mysqld assertion failed at ", "[prod] mysqld called gu_abort after ",
              "[prod?] mysqld died on fatal signal ", "[prod] mysqld stopped itself after "):
    check(f"supervisor builds per-site names as {built.strip()!r}", built in entry)

# 3. Detection: the check rejects an unlabeled and a mislabeled message.
check("detection: an unlabeled always() is rejected",
      bool(workload_problems('always(ok, "every row is present", d)', "x.py")))
check("detection: a reachable() labeled [prod] is rejected",
      bool(workload_problems('reachable("[prod] a state transfer ran", d)', "x.py")))
check("detection: a correctly labeled pair passes",
      not workload_problems('always(ok, "[prod] x", d)\nreachable("[coverage] y", d)', "x.py"))

print()
print("FAILED: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
