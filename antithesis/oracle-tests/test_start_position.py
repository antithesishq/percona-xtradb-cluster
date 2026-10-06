"""Start position and interrupted-SST handling in the supervisor.

Drives the real recover_position, datadir_initialized and clear_interrupted_sst
(pulled out of pxc-node/entrypoint.sh with awk, not copied) against a scratch
datadir. A fake mysqld stands in for `mysqld --wsrep-recover`: it writes the
recovery line that node1 wrote in run fdb9d32c...-63-5 and records that it ran.

Start position. In that run node1 stopped cleanly with grastate.dat seqno 604,
and --wsrep-recover reported 603. The shipped wrappers start at 604
(scripts/mysqld_safe.sh:270-276). The supervisor used 603, IST re-applied 604,
and node1 crash-looped. A clean seqno must win; seqno -1 or no file must still
fall through to recovery.

Interrupted SST. An xtrabackup joiner wipes mysql/ but keeps sst_in_progress,
grastate.dat and the logs. The RPM wrapper empties such a datadir before it
initializes again (build-ps/rpm/mysql-systemd:57-64). A datadir without the
marker must be left alone.

Detection: both cases are also fed to the pre-change code (the grastate block
cut out of recover_position; clear_interrupted_sst stubbed out), which must be
caught starting at 603 and leaving the SST leftovers in place.
"""
import pathlib, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
ENTRY = HERE.parent / "pxc-node" / "entrypoint.sh"

UUID = "4e7ca4a7-c127-11f1-804c-2e28d37e9dfc"
RECOVER_LINE = f"2026-10-06T01:44:30.673582Z 0 [Note] [MY-000000] [WSREP] Recovered position: {UUID}:603\n"

FUNCS = ["datadir_initialized", "clear_interrupted_sst", "recover_position"]


def extract(old=False):
    parts = []
    for f in FUNCS:
        body = subprocess.run(["awk", f"/^{f}\\(\\) \\{{/,/^\\}}/"], stdin=open(ENTRY),
                              capture_output=True, text=True, check=True).stdout
        assert body.strip(), f"function {f} not found in entrypoint.sh"
        if old and f == "recover_position":
            start = body.index("    local gs_uuid gs_seqno\n")
            end = body.index("        fi\n    fi\n", start) + len("        fi\n    fi\n")
            body = body[:start] + body[end:]
        if old and f == "clear_interrupted_sst":
            body = "clear_interrupted_sst() { :; }\n"
        parts.append(body)
    return "\n".join(parts)


def run(setup, old=False):
    """Build a datadir with `setup`, run the boot steps, return what happened."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        datadir, state, prefix = d / "data", d / "state", d / "pxc"
        for p in (datadir, state, prefix / "bin"):
            p.mkdir(parents=True)
        ran = d / "mysqld-ran"
        mysqld = prefix / "bin" / "mysqld"
        # Writes the recovery line to the --log-error file, like the real one.
        mysqld.write_text(f"""#!/usr/bin/env bash
touch {ran}
for a in "$@"; do case "$a" in --log-error=*) printf '%s' '{RECOVER_LINE}' >> "${{a#--log-error=}}";; esac; done
""")
        mysqld.chmod(0o755)
        setup(datadir)
        events = d / "events"
        script = extract(old) + f"""
DATADIR={datadir}; STATE_DIR={state}; PXC_PREFIX={prefix}; DEFAULTS_FILE=/dev/null
emit() {{ echo "$1" >> {events}; }}
log() {{ :; }}
chown() {{ :; }}
if ! datadir_initialized; then clear_interrupted_sst; fi
recover_position
echo "POS=${{RECOVERED_POSITION}}"
"""
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        pos = [l[4:] for l in out.stdout.splitlines() if l.startswith("POS=")]
        return {
            "pos": pos[0] if pos else None,
            "recover_ran": ran.exists(),
            "left": sorted(p.name for p in datadir.iterdir()),
            "events": events.read_text().split() if events.exists() else [],
            "stderr": out.stderr,
        }


def grastate(seqno):
    def setup(dd):
        (dd / "mysql").mkdir()
        (dd / "grastate.dat").write_text(
            f"# GALERA saved state\nversion: 2.1\nuuid:    {UUID}\nseqno:   {seqno}\nsafe_to_bootstrap: 0\n")
    return setup


def no_grastate(dd):
    (dd / "mysql").mkdir()


def interrupted_sst(dd):
    # The files the xtrabackup-v2 keep-list leaves behind (wsrep_sst_xtrabackup-v2.sh:836).
    for f in ("sst_in_progress", "grastate.dat", "gvwstate.dat", "innobackup.prepare.log"):
        (dd / f).write_text("x\n")


def foreign_files(dd):
    # No mysql/ and no SST marker: not ours to delete.
    (dd / "keepme").write_text("x\n")


FAILED = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {info}"))
    if not cond:
        FAILED.append(name)


r = run(grastate(604))
check("clean grastate seqno is the start position", r["pos"] == f"{UUID}:604", r)
check("clean grastate seqno skips --wsrep-recover", not r["recover_ran"], r)
check("grastate path still emits recover_done", "recover_done" in r["events"], r)

r = run(grastate(-1))
check("seqno -1 falls through to --wsrep-recover", r["recover_ran"] and r["pos"] == f"{UUID}:603", r)

r = run(no_grastate)
check("missing grastate falls through to --wsrep-recover", r["recover_ran"] and r["pos"] == f"{UUID}:603", r)

r = run(interrupted_sst)
check("interrupted SST leftovers are cleared", r["left"] == [], r)
check("clearing is recorded", "sst_leftover_cleared" in r["events"], r)

r = run(foreign_files)
check("a datadir without the SST marker is left alone", r["left"] == ["keepme"], r)

r = run(grastate(604), old=True)
check("detection: old behaviour starts at the recovered 603", r["pos"] == f"{UUID}:603", r)

r = run(interrupted_sst, old=True)
check("detection: old behaviour leaves the SST leftovers for --initialize to refuse",
      "sst_in_progress" in r["left"], r)

print()
print("FAILED: " + (", ".join(FAILED) if FAILED else "none"))
sys.exit(1 if FAILED else 0)
