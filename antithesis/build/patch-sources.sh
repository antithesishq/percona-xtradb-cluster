#!/usr/bin/env bash
#
# Source patches applied inside the pxc-build stage, before either build runs.
#
# Kept as a script rather than inline Dockerfile RUN steps so the patching logic
# is readable, testable outside Docker, and free of heredoc-in-RUN quoting
# hazards.
#
# Usage: patch-sources.sh <source-dir>
# Env:   PXC_INSTRUMENT=0|1   whether the instrumentation header is added
#
# Every patch verifies its own effect and exits non-zero on a miss, so an
# upstream rename surfaces as a failed build rather than a silent no-op that
# quietly removes an oracle.
#
set -euo pipefail

SRC="${1:?usage: patch-sources.sh <source-dir>}"
PXC_INSTRUMENT="${PXC_INSTRUMENT:-0}"

cd "${SRC}"

# ---------------------------------------------------------------------------
# Patch 1: let Galera produce core dumps.
#
# gu_abort() disables cores in-process (setrlimit(RLIMIT_CORE, 0) plus
# prctl(PR_SET_DUMPABLE, 0)) before calling abort(), so every Galera
# self-destruct — assert failure, inconsistency abort, fatal provider error —
# dies core-less. Because the suppression is in-process, a container-level
# `ulimit -c unlimited` cannot undo it; the source has to change.
#
# DELIBERATE DIVERGENCE FROM FIELD BEHAVIOR. Applied to the test image only.
# Do not read "a core exists" as a claim about shipped behavior.
# ---------------------------------------------------------------------------
GU_ABORT="percona-xtradb-cluster-galera/galerautils/src/gu_abort.c"

if [[ ! -f "${GU_ABORT}" ]]; then
    echo "ANTITHESIS PATCH FAILED: ${GU_ABORT} not found under ${SRC}" >&2
    exit 1
fi

python3 - "${GU_ABORT}" <<'PY'
import sys

MARKER = "ANTITHESIS: core-dump suppression removed"

path = sys.argv[1]
with open(path) as fh:
    original = fh.read()

# Idempotent: a Docker build patches a fresh COPY each time, but running this
# twice by hand should be a no-op rather than a confusing "pattern not found".
if MARKER in original:
    print("ANTITHESIS: gu_abort.c already patched; nothing to do")
    sys.exit(0)

patched = original.replace(
    "    struct rlimit core_limits = { 0, 0 };\n"
    "    setrlimit (RLIMIT_CORE, &core_limits);",
    "    /* ANTITHESIS: core-dump suppression removed for the test image. */",
)
patched = patched.replace(
    "    prctl(PR_SET_DUMPABLE, 0, 0, 0, 0);",
    "    /* ANTITHESIS: PR_SET_DUMPABLE suppression removed for the test image. */",
)

if patched == original:
    sys.exit(
        "ANTITHESIS PATCH FAILED: gu_abort.c core-dump suppression not found. "
        "Upstream changed shape; re-derive the patch."
    )

if "setrlimit (RLIMIT_CORE" in patched or "PR_SET_DUMPABLE, 0" in patched:
    sys.exit(
        "ANTITHESIS PATCH FAILED: gu_abort.c suppression only partially removed."
    )

with open(path, "w") as fh:
    fh.write(patched)

print("ANTITHESIS: patched gu_abort.c to allow core dumps")
PY

# ---------------------------------------------------------------------------
# Patch 2: instrumentation header (only when PXC_INSTRUMENT=1).
#
# antithesis_instrumentation.h must be included in exactly ONE translation
# unit at link time. sql/main.cc is a one-line file holding mysqld's main(),
# which is exactly the placement the Antithesis C/C++ docs recommend.
# ---------------------------------------------------------------------------
if [[ "${PXC_INSTRUMENT}" == "1" ]]; then
    MAIN_CC="sql/main.cc"

    if [[ ! -f "${MAIN_CC}" ]]; then
        echo "ANTITHESIS PATCH FAILED: ${MAIN_CC} not found; mysqld's main()" \
             "has moved. Re-derive the instrumentation include site." >&2
        exit 1
    fi

    if grep -q 'antithesis_instrumentation.h' "${MAIN_CC}"; then
        echo "ANTITHESIS: instrumentation header already present in ${MAIN_CC}"
    else
        printf '#include "antithesis_instrumentation.h"\n' > "${MAIN_CC}.new"
        cat "${MAIN_CC}" >> "${MAIN_CC}.new"
        mv "${MAIN_CC}.new" "${MAIN_CC}"
        grep -q 'antithesis_instrumentation.h' "${MAIN_CC}"
        echo "ANTITHESIS: instrumentation header added to ${MAIN_CC}"
    fi
else
    echo "ANTITHESIS: PXC_INSTRUMENT=0 — skipping instrumentation header"
fi

# ---------------------------------------------------------------------------
# Patch 3: stop the server's cmake build from also building galera.
#
# WITH_WSREP=ON adds two subdirectories:
#
#     IF(WITH_WSREP)
#        ADD_SUBDIRECTORY(percona-xtradb-cluster-galera)
#        ADD_SUBDIRECTORY(wsrep-lib)
#     ENDIF()
#
# wsrep-lib is required — it is statically linked into mysqld. The galera
# subdirectory is not: the provider is a runtime dlopen() target, nothing in
# sql/ links it, and no cmake target outside percona-xtradb-cluster-galera/
# references galera_smm / galerautilsxx / friends.
#
# Building it here is worse than redundant. This harness builds the provider
# with scons (assert-enabled via debug=3) and installs that over whatever cmake
# produced, so the cmake copy is dead weight that also drags in galera's unit
# tests (galera/tests -> galera_check). Dropping it removes several minutes of
# build time and a whole failure surface that can never affect the artifact we
# actually ship.
#
# wsrep-lib is deliberately left in place.
# ---------------------------------------------------------------------------
TOP_CMAKE="CMakeLists.txt"

if [[ ! -f "${TOP_CMAKE}" ]]; then
    echo "ANTITHESIS PATCH FAILED: ${TOP_CMAKE} not found under ${SRC}" >&2
    exit 1
fi

python3 - "${TOP_CMAKE}" <<'PY'
import sys

MARKER = "ANTITHESIS: galera subdirectory skipped"
TARGET = "   ADD_SUBDIRECTORY(percona-xtradb-cluster-galera)\n"

path = sys.argv[1]
with open(path) as fh:
    original = fh.read()

if MARKER in original:
    print("ANTITHESIS: CMakeLists.txt already patched; nothing to do")
    sys.exit(0)

if original.count(TARGET) != 1:
    sys.exit(
        "ANTITHESIS PATCH FAILED: expected exactly one "
        f"ADD_SUBDIRECTORY(percona-xtradb-cluster-galera) line, found "
        f"{original.count(TARGET)}. Re-derive the patch."
    )

patched = original.replace(
    TARGET,
    "   # " + MARKER + " — built separately with scons and installed over\n"
    "   # this build's output. wsrep-lib below is still required.\n"
    "   # ADD_SUBDIRECTORY(percona-xtradb-cluster-galera)\n",
)

# wsrep-lib must survive: mysqld statically links it.
if "ADD_SUBDIRECTORY(wsrep-lib)" not in patched:
    sys.exit("ANTITHESIS PATCH FAILED: wsrep-lib subdirectory went missing.")

with open(path, "w") as fh:
    fh.write(patched)

print("ANTITHESIS: patched CMakeLists.txt to skip the galera subdirectory")
PY

echo "ANTITHESIS: source patching complete"
