"""Every mysqld death is labeled by whether a release build would die the same way.

Why: the findings go to the Percona team. A debug-only assert (a check that
exists only because this image is a Debug build), a release-build check, and a
deliberate gu_abort() are different claims, and the report has to say which
one each death is.

Drives the supervisor's real death classification (functions pulled out of
pxc-node/entrypoint.sh with awk, not copied) over boot log slices, then reads
back the sdk.jsonl it wrote. The tier table is the real one: this test runs
pxc-node/assert_tiers.py over the repo, as the image build does.

The log lines are copied from run 6a88fb9ba55565a5eed180ad68837c8d-63-3:
- client_state.cpp:536, a glibc assert (node3, boot 1);
- lock0lock.cc:5282, an InnoDB ut_a inside #ifdef UNIV_DEBUG;
- a gu_abort chain from node3 boot 5: the donor's "State transfer ... failed",
  gcs_group.cpp:1379 "Will never receive state. Need to abort.", Galera's
  "Terminated." line, and the SST script teardown after it.
The release-tier InnoDB line uses the same format at lock0lock.cc:336, a plain
ut_a outside any UNIV_DEBUG block.

Detection: the gu_abort slice is also fed to the pre-change classification
(gu_abort_death stubbed out), which must be caught reporting it as
undiagnosed. Otherwise a green result would not tell a working class from a
missing one.
"""
import json, pathlib, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
ENTRY = HERE.parent / "pxc-node" / "entrypoint.sh"
SCANNER = HERE.parent / "pxc-node" / "assert_tiers.py"
REPO = HERE.parent.parent

FUNCS = ["json_escape", "sdk_reachable", "sdk_unreachable", "sdk_declare_catalog",
         "site_component", "fatal_exit_status", "assert_tier", "assert_failed_site",
         "gu_abort_death", "fatal_signal_without_assert", "unireg_cause_lines",
         "unireg_cause_key", "unireg_abort_undocumented", "assert_death_class",
         "drop_session_errors"]

START = "2026-10-02T13:09:40.673440Z 0 [System] [MY-015015] [Server] MySQL Server - start.\n"

GLIBC = START + """\
mysqld: /src/wsrep-lib/src/client_state.cpp:536: int wsrep::client_state::bf_abort(wsrep::unique_lock<wsrep::mutex>&, wsrep::seqno): Assertion `mode_ == m_local || transaction_.is_streaming()' failed.
2026-10-02T13:07:56Z UTC - mysqld got signal 6 ;
"""

INNODB_DEBUG = START + """\
2026-10-02T13:11:19.098856Z 52501 [ERROR] [MY-013183] [InnoDB] Assertion failure: lock0lock.cc:5282:lock_get_wait(other_lock) thread 140389401978560
"""
INNODB_RELEASE = INNODB_DEBUG.replace("lock0lock.cc:5282", "lock0lock.cc:336")
INNODB_UNKNOWN = INNODB_DEBUG.replace("lock0lock.cc:5282", "nosuchfile0.cc:1")

GU_ABORT = START + """\
2026-10-02T13:09:56.297873Z 0 [Warning] [MY-000000] [Galera] 0.0 (node1): State transfer to 2.0 (node3) failed: No message of desired type
2026-10-02T13:09:56.297874Z 0 [ERROR] [MY-000000] [Galera] gcs/src/gcs_group.cpp:gcs_group_handle_join_msg():1379: Will never receive state. Need to abort.
2026-10-02T13:09:56.330588Z 0 [Note] [MY-000000] [Galera] /usr/local/pxc/bin/mysqld: Terminated.
2026-10-02T13:09:56.330590Z 0 [Note] [MY-000000] [WSREP] Terminating SST process
2026-10-02T13:09:56.330591Z 0 [ERROR] [MY-000000] [WSREP-SST] Removing /var/lib/mysql//xtrabackup_galera_info file due to signal
2026-10-02T13:09:56.330593Z 0 [ERROR] [MY-000000] [WSREP-SST] SST script interrupted
2026-10-02T13:09:56.330595Z 0 [ERROR] [MY-000000] [WSREP-SST] Cleanup after exit with status:11
"""

SEGV = START + "2026-10-02T13:10:00Z UTC - mysqld got signal 11 ;\n"

DEBUG_ANY = "[debug-only] mysqld never fails an assertion"
RELEASE_ANY = "[prod] mysqld never fails an assertion"
UNKNOWN_ANY = "[prod?] mysqld never fails an assertion of unknown build tier"
GU_ANY = "[prod] mysqld never calls gu_abort for an undocumented reason"
UNDIAGNOSED = "[prod?] mysqld never dies on a fatal path without a diagnosable log line"


def extract():
    parts = [
        subprocess.run(["awk", "/^A_[A-Z_]+=/"], stdin=open(ENTRY), capture_output=True, text=True, check=True).stdout,
    ]
    for arr in ("UNIREG_DOCUMENTED_CAUSES", "GU_ABORT_DOCUMENTED_CAUSES"):
        parts.append(subprocess.run(["awk", f"/^{arr}=\\(/,/^\\)/"], stdin=open(ENTRY),
                                    capture_output=True, text=True, check=True).stdout)
    for f in FUNCS:
        body = subprocess.run(["awk", f"/^{f}\\(\\) \\{{/,/^\\}}/"], stdin=open(ENTRY),
                              capture_output=True, text=True, check=True).stdout
        assert body.strip(), f"function {f} not found in entrypoint.sh"
        parts.append(body)
    return "\n".join(parts)


LIB = extract()
TIERS = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
TIERS.write(subprocess.run([sys.executable, str(SCANNER), str(REPO)], capture_output=True,
                           text=True, check=True).stdout)
TIERS.close()


def run(slice_text, kind="abort", status=134, signal=6, stub=""):
    with tempfile.TemporaryDirectory() as d:
        sdk = pathlib.Path(d) / "sdk.jsonl"
        sl = pathlib.Path(d) / "slice.log"
        sl.write_text(slice_text)
        script = LIB + f"""
emit() {{ :; }}
boot_log_slice() {{ cat {sl}; }}
{stub}
SDK_FILE={sdk}; PXC_NODE_NAME=node3; BOOT_COUNT=5; BUG_DEATH=0
ASSERT_TIERS_FILE={TIERS.name}
EXIT_KIND={kind}; EXIT_STATUS={status}; EXIT_SIGNAL={signal}; EXIT_FIELD_RESTART=false
assert_death_class {status}
"""
        subprocess.run(["bash", "-c", script], check=True)
        raw = sdk.read_text() if sdk.exists() else ""
    if raw:
        subprocess.run(["jq", "-e", "."], input=raw, capture_output=True, text=True, check=True)
    return [json.loads(l)["antithesis_assert"] for l in raw.splitlines()]


fails = []
def check(name, cond, got=None):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)
        for a in got or []:
            print("       ", a["display_type"], a["id"])

def ids(ev): return {a["id"] for a in ev if a["display_type"] == "Unreachable"}
def det(ev, i): return next(a["details"] for a in ev if a["id"] == i)

# 1. glibc assert: debug-only, by rule.
ev = run(GLIBC)
site = "[debug-only] mysqld assertion failed at wsrep-lib/src/client_state.cpp:536"
check("glibc assert names the debug-only tier", ids(ev) == {DEBUG_ANY, site}, ev)
d = det(ev, site)
check("glibc assert details: no release check", d["build_tier"] == "debug_only"
      and d["release_build_has_this_check"] is False and d["exit_cause"] == "debug_only_assert")

# 2. InnoDB ut_a inside #ifdef UNIV_DEBUG: debug-only, from the real table.
ev = run(INNODB_DEBUG)
site = "[debug-only] mysqld assertion failed at lock0lock.cc:5282"
check("InnoDB ut_a inside UNIV_DEBUG is debug-only", ids(ev) == {DEBUG_ANY, site}, ev)
check("expression survives without the thread id",
      det(ev, site)["expression"] == "lock_get_wait(other_lock)")

# 3. InnoDB ut_a outside UNIV_DEBUG: release.
ev = run(INNODB_RELEASE)
site = "[prod] mysqld assertion failed at lock0lock.cc:336"
check("InnoDB ut_a outside UNIV_DEBUG is release", ids(ev) == {RELEASE_ANY, site}, ev)
check("release details say a release build has the check",
      det(ev, site)["release_build_has_this_check"] is True)

# 4. Site not in the table: unknown, never guessed.
ev = run(INNODB_UNKNOWN)
site = "[prod?] mysqld assertion failed at nosuchfile0.cc:1"
check("site missing from the table is unknown", ids(ev) == {UNKNOWN_ANY, site}, ev)

# 5. No table at all (image built without it): unknown, not release.
def run_no_table(slice_text):
    global TIERS
    saved = TIERS.name
    TIERS.name = "/nonexistent/assert-tiers.tsv"
    try:
        return run(slice_text)
    finally:
        TIERS.name = saved
ev = run_no_table(INNODB_DEBUG)
check("missing table makes InnoDB sites unknown",
      ids(ev) == {UNKNOWN_ANY, "[prod?] mysqld assertion failed at lock0lock.cc:5282"}, ev)

# 6. gu_abort: its own class, keyed on the last ERROR before Terminated.
ev = run(GU_ABORT)
key = ("[prod] mysqld called gu_abort after MY-000000 [Galera] gcs/src/gcs_group.cpp:"
       "gcs_group_handle_join_msg():N: Will never receive state. Need to abort.")
check("gu_abort death fails the gu_abort umbrella and its cause", ids(ev) == {GU_ANY, key}, ev)
d = det(ev, GU_ANY)
check("gu_abort details: release code path", d["exit_cause"] == "gu_abort"
      and d["release_build_has_this_check"] is True)
check("gu_abort cause excludes the SST teardown after Terminated",
      "Will never receive state" in d["last_errors"] and "WSREP-SST" not in d["last_errors"])
check("gu_abort is not reported as undiagnosed", UNDIAGNOSED not in ids(ev), ev)

# 7. Detection: the pre-change classification called it undiagnosed.
ev = run(GU_ABORT, stub="gu_abort_death() { return 1; }")
check("detection: without the class, gu_abort lands in undiagnosed", ids(ev) == {UNDIAGNOSED}, ev)

# 7b. A client session's pxc_strict_mode verdict logged between the real
#     cause and Terminated must not become the key. Lines from run
#     8fd9e28bf16da1132b1c9fe934a8219f-63-5, node3 boot 1 (vtime 130-131).
SERIAL = ("2026-10-05T20:21:44.982553Z 4198 [ERROR] [MY-000000] [WSREP] Percona-XtraDB-Cluster "
          "doesn't recommend using SERIALIZABLE isolation with pxc_strict_mode = ENFORCING\n")
gl = GU_ABORT.splitlines(keepends=True)
term = next(i for i, l in enumerate(gl) if l.rstrip().endswith("Terminated."))
GU_ABORT_SERIAL = "".join(gl[:term]) + SERIAL + "".join(gl[term:])
ev = run(GU_ABORT_SERIAL)
check("gu_abort key ignores a session's strict-mode ERROR before Terminated",
      ids(ev) == {GU_ANY, key}, ev)
check("gu_abort last_errors leave out the strict-mode line",
      "SERIALIZABLE" not in det(ev, GU_ANY)["last_errors"])
ev = run(GU_ABORT_SERIAL, stub="drop_session_errors() { cat; }")
check("detection: without the filter, the strict-mode line becomes the key",
      any("SERIALIZABLE" in i for i in ids(ev)), ev)

# 8. Bare SIGSEGV: unchanged names, labeled as unknown for release.
ev = run(SEGV, kind="exit", status=2, signal=0)
sig = "[prod?] mysqld died on fatal signal 11 without a failed assertion"
check("SIGSEGV keeps the fatal-signal names",
      ids(ev) == {"[prod?] mysqld never dies on a fatal signal outside a failed assertion", sig}, ev)
check("SIGSEGV details label the cause", det(ev, sig)["exit_cause"] == "fatal_signal")

# 9. Catalog: every umbrella declared as an unfired Unreachable.
with tempfile.TemporaryDirectory() as d:
    sdk = pathlib.Path(d) / "sdk.jsonl"
    subprocess.run(["bash", "-c", LIB + f"\nSDK_FILE={sdk}\nsdk_declare_catalog\n"], check=True)
    cat = [json.loads(l)["antithesis_assert"] for l in sdk.read_text().splitlines()]
declared = {a["id"] for a in cat if a["display_type"] == "Unreachable" and not a["hit"]}
check("catalog declares the tier and gu_abort umbrellas",
      {DEBUG_ANY, RELEASE_ANY, UNKNOWN_ANY, GU_ANY} <= declared)
check("catalog no longer declares the old untiered umbrella",
      "mysqld never aborts on a failed assertion" not in declared)

print()
print("FAILED: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
