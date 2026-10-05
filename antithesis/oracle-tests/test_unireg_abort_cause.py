"""unireg_abort deaths: documented causes stay coverage, undocumented ones fail.

Drives the supervisor's real death classification (functions pulled out of
pxc-node/entrypoint.sh with awk, not copied) over boot log slices, then reads
back the sdk.jsonl it wrote.

BOOT below is the part of a real boot that the classification reads, copied
verbatim from run c89f2f7a...-63-2, node3 (the 20:13:42 boot): its start line,
the whole [ERROR] cause chain with its continuation line, and Aborting. The
Note/Warning lines in between are left out; the check ignores them. The
teardown line after Aborting is the same text from the previous boot of that
node. Every one of the 994 unireg_abort deaths in c89f2f7a...-63-2 and
93ec5045...-63-2 has this chain; the two runs differ only in timestamps.

The undocumented cases are built from the same real lines: the documented line
removed, or real InnoDB [ERROR] lines from the run's recovery log placed before
the Aborting line. No undocumented unireg_abort occurred in either run.

Detection: the undocumented case is also fed to the pre-change classification
(the new check stubbed out), which must be caught emitting only the coverage
Reachables. Otherwise a green result would not tell a working check from a
missing one.
"""
import json, pathlib, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
ENTRY = HERE.parent / "pxc-node" / "entrypoint.sh"

BOOT = """\
2026-09-28T20:13:42.189415Z 0 [System] [MY-015015] [Server] MySQL Server - start.
2026-09-28T20:13:42.320387Z 0 [Note] [MY-000000] [WSREP] Starting replication
2026-09-28T20:14:13.320969Z 0 [ERROR] [MY-000000] [Galera] failed to open gcomm backend connection: 110: failed to reach primary view (pc.wait_prim_timeout)
\t at gcomm/src/pc.cpp:connect():177
2026-09-28T20:14:13.320970Z 0 [ERROR] [MY-000000] [Galera] gcs/src/gcs_core.cpp:gcs_core_open():256: Failed to open backend connection: -110 (Connection timed out)
2026-09-28T20:14:13.320975Z 0 [ERROR] [MY-000000] [Galera] gcs/src/gcs.cpp:gcs_open():1987: Failed to open channel 'antithesis-pxc' at 'gcomm://10.20.20.11,10.20.20.12,10.20.20.13': -110 (Connection timed out)
2026-09-28T20:14:13.320976Z 0 [ERROR] [MY-000000] [Galera] gcs connect failed: Operation timed out
2026-09-28T20:14:13.320977Z 0 [ERROR] [MY-000000] [WSREP] Provider/Node (gcomm://10.20.20.11,10.20.20.12,10.20.20.13) failed to establish connection with cluster (reason: 7)
2026-09-28T20:14:13.320978Z 0 [ERROR] [MY-010119] [Server] Aborting
2026-09-28T20:14:13.320984Z 0 [ERROR] [MY-010065] [Server] Failed to shutdown components infrastructure.
"""

FUNCS = ["json_escape", "sdk_reachable", "sdk_unreachable", "sdk_declare_catalog",
         "site_component", "fatal_exit_status", "assert_tier", "assert_failed_site",
         "gu_abort_death",
         "fatal_signal_without_assert", "unireg_cause_lines", "unireg_cause_key",
         "unireg_abort_undocumented", "assert_death_class"]

UMBRELLA = "[prod] mysqld never stops itself for an undocumented reason"
COVERAGE = {"[coverage] a node died in a way the shipped systemd unit would not restart",
            "[coverage] a node died before mysqld reached ready for connections",
            "[coverage] a node died in a boot whose state transfer had failed",
            "[coverage] a node died after the cluster declared it inconsistent"}


def extract():
    parts = [
        subprocess.run(["awk", "/^A_[A-Z_]+=/"], stdin=open(ENTRY), capture_output=True, text=True, check=True).stdout,
        subprocess.run(["awk", "/^UNIREG_DOCUMENTED_CAUSES=\\(/,/^\\)/"], stdin=open(ENTRY), capture_output=True, text=True, check=True).stdout,
        subprocess.run(["awk", "/^GU_ABORT_DOCUMENTED_CAUSES=\\(/,/^\\)/"], stdin=open(ENTRY), capture_output=True, text=True, check=True).stdout,
    ]
    for f in FUNCS:
        body = subprocess.run(["awk", f"/^{f}\\(\\) \\{{/,/^\\}}/"], stdin=open(ENTRY),
                              capture_output=True, text=True, check=True).stdout
        assert body.strip(), f"function {f} not found in entrypoint.sh"
        parts.append(body)
    return "\n".join(parts)


LIB = extract()


def run(slice_text, kind="unireg_abort", status=1, signal=0, field="false", stub_new=False):
    with tempfile.TemporaryDirectory() as d:
        sdk = pathlib.Path(d) / "sdk.jsonl"
        sl = pathlib.Path(d) / "slice.log"
        sl.write_text(slice_text)
        script = LIB + f"""
emit() {{ :; }}
boot_log_slice() {{ cat {sl}; }}
{"unireg_abort_undocumented() { :; }" if stub_new else ""}
SDK_FILE={sdk}; PXC_NODE_NAME=node3; BOOT_COUNT=6; BUG_DEATH=0
EXIT_KIND={kind}; EXIT_STATUS={status}; EXIT_SIGNAL={signal}; EXIT_FIELD_RESTART={field}
assert_death_class {status}
"""
        subprocess.run(["bash", "-c", script], check=True)
        raw = sdk.read_text() if sdk.exists() else ""
    # every line must parse with jq, the platform's parser aside
    if raw:
        subprocess.run(["jq", "-e", "."], input=raw, capture_output=True, text=True, check=True)
    return [json.loads(l)["antithesis_assert"] for l in raw.splitlines()]


fails = []
def check(name, cond, got=None):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)
        if got is not None:
            for a in got: print("       ", a["display_type"], a["id"])

def unreach(ev): return [a for a in ev if a["display_type"] == "Unreachable"]
def reach(ev): return {a["id"] for a in ev if a["display_type"] == "Reachable"}

WAIT_PRIM = "failed to reach primary view (pc.wait_prim_timeout)"

base = BOOT

# 1. Documented: only the coverage Reachables.
ev = run(base)
check("pc.wait_prim_timeout stop emits no Unreachable", not unreach(ev), ev)
check("pc.wait_prim_timeout stop emits the coverage Reachables",
      reach(ev) == {"[coverage] a node died in a way the shipped systemd unit would not restart",
                    "[coverage] a node died before mysqld reached ready for connections"}, ev)

# 2. Undocumented: the same real chain without its documented line. The last
#    ERROR before Aborting is the generic WSREP connect failure.
no_prim = "\n".join(l for l in base.splitlines() if WAIT_PRIM not in l
                    and not l.startswith("\t at gcomm/src/pc.cpp")) + "\n"
ev = run(no_prim)
u = unreach(ev)
ids = [a["id"] for a in u]
check("gcomm connect failure without pc.wait_prim_timeout fails the umbrella", UMBRELLA in ids, ev)
key = "[prod] mysqld stopped itself after MY-000000 [WSREP] Provider/Node (gcomm://...) failed to establish connection with cluster (reason: 7)"
check("per-cause property is keyed on the last ERROR before Aborting, normalized", key in ids, ev)
check("coverage Reachables are skipped for an undocumented unireg_abort", not (reach(ev) & COVERAGE), ev)
d = next(a["details"] for a in u if a["id"] == UMBRELLA)
check("details carry node and boot", d["node"] == "node3" and d["boot"] == 6)
check("details carry the last ERROR lines, not the teardown",
      "failed to establish connection with cluster" in d["last_errors"]
      and "MY-010065" not in d["last_errors"]
      and d["last_errors"].rstrip("|").endswith("(reason: 7)")
      and d["last_errors"].count("|") == 4)  # the 4 ERROR lines left before Aborting

# 3. Undocumented with a real MY-code: InnoDB errors from the run's recover log.
innodb = [
    "2026-09-28T20:25:57.660402Z 1 [ERROR] [MY-012592] [InnoDB] Operating system error number 2 in a file operation.",
    "2026-09-28T20:25:57.660403Z 1 [ERROR] [MY-012593] [InnoDB] The error means the system cannot find the path specified.",
    "2026-09-28T20:25:57.660405Z 1 [ERROR] [MY-012646] [InnoDB] File ./ibdata1: 'open' returned OS error 71. Cannot continue operation",
    "2026-09-28T20:25:57.660406Z 1 [ERROR] [MY-012981] [InnoDB] Cannot continue operation.",
    "2026-09-28T20:25:57.660407Z 0 [ERROR] [MY-010119] [Server] Aborting",
    "2026-09-28T20:25:57.660408Z 0 [ERROR] [MY-010065] [Server] Failed to shutdown components infrastructure.",
]
head = "\n".join(base.splitlines()[:2])
ev = run(head + "\n" + "\n".join(innodb) + "\n")
ids = [a["id"] for a in unreach(ev)]
check("InnoDB stop is undocumented and keyed on MY-012981 [InnoDB]",
      UMBRELLA in ids and "[prod] mysqld stopped itself after MY-012981 [InnoDB]" in ids, ev)

# 4. The documented line appearing only AFTER Aborting (teardown) excuses nothing.
ev = run(head + "\n" + "\n".join(innodb) + "\n"
         + "2026-09-28T20:25:57.660409Z 0 [ERROR] [MY-000000] [Galera] failed to open gcomm backend connection: 110: " + WAIT_PRIM + "\n")
check("a documented line after Aborting does not excuse the stop", UMBRELLA in [a["id"] for a in unreach(ev)], ev)

# 5. Exit status 1 with no ERROR at all.
ev = run(head + "\n")
ids = [a["id"] for a in unreach(ev)]
check("silent exit 1 fails, keyed as having no ERROR line",
      UMBRELLA in ids and "[prod] mysqld stopped itself after no [ERROR] line before exit" in ids, ev)

# 6. Other exit kinds never reach the new check.
ev = run(no_prim, kind="crash", status=137, signal=9, field="true")
check("kill-channel SIGKILL does not trigger the unireg property",
      UMBRELLA not in [a["id"] for a in ev], ev)

# 7. Catalog declares the umbrella, unfired.
with tempfile.TemporaryDirectory() as dd:
    sdk = pathlib.Path(dd) / "sdk.jsonl"
    subprocess.run(["bash", "-c", LIB + f"\nSDK_FILE={sdk}\nsdk_declare_catalog\n"], check=True)
    raw = sdk.read_text()
    subprocess.run(["jq", "-e", "."], input=raw, capture_output=True, text=True, check=True)
    decl = [json.loads(l)["antithesis_assert"] for l in raw.splitlines()]
check("umbrella declared in the catalog as an unfired Unreachable",
      any(a["id"] == UMBRELLA and a["hit"] is False and a["display_type"] == "Unreachable" for a in decl))

# 8. Detection: the old classification is blind to the undocumented stop.
ev = run(no_prim, stub_new=True)
check("detection: old behaviour emits no Unreachable for the undocumented stop", not unreach(ev), ev)
check("detection: old behaviour turned the coverage claims green instead", bool(reach(ev) & COVERAGE), ev)

print()
print("FAILED:", len(fails) if fails else "none")
sys.exit(1 if fails else 0)
