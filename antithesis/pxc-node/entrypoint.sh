#!/usr/bin/env bash
#
# PXC node supervisor for the Antithesis harness.
#
# Implements the entrypoint supervisor from deployment-topology.md. It replaces
# mysqld_safe / mysql-systemd deliberately, because the harness needs control
# over restart policy, the recovery dance and ungraceful death that the shipped
# wrappers do not expose.
#
# Responsibilities:
#   1. Generate the per-node config fragment from the environment.
#   2. First boot: initialize the datadir. On the bootstrap node only, and only
#      once, start with --wsrep-new-cluster.
#   3. Every other boot: run the --wsrep-recover dance and start at the
#      recovered position.
#   4. Restart mysqld when it exits, unless held down or shutting down.
#   5. Serve the workload -> supervisor kill channel (steerable kill -9).
#   6. Emit JSONL: boot-phase markers, the grastate/recovery probe, and restart
#      accounting including a "would shipped systemd have restarted this?"
#      classification.
#
# Deliberate divergence from the field: shipped systemd uses Restart=on-abort
# with RestartPreventExitStatus=SIGABRT, so inconsistency-aborted nodes stay
# down. This supervisor restarts them anyway, and records what the field would
# have done instead, so triage can still say "the field would be permanently
# down here".
#
# Control files (all under $STATE_DIR, all workload-writable via test commands):
#   bootstrapped   cluster bootstrap already happened; never bootstrap again
#   hold-down      do NOT restart mysqld until this file is removed
#   kill           kill -9 mysqld now, then delete this file
#
set -uo pipefail

PXC_PREFIX="${PXC_PREFIX:-/usr/local/pxc}"
DATADIR="${PXC_DATADIR:-/var/lib/mysql}"
LOG_ERROR="${PXC_LOG_ERROR:-/var/log/mysql/error.log}"
STATE_DIR="${PXC_STATE_DIR:-/opt/antithesis/state}"
DEFAULTS_FILE="${PXC_DEFAULTS_FILE:-/etc/my.cnf}"
# Build-time verdicts on InnoDB assertion sites (pxc-node/assert_tiers.py).
ASSERT_TIERS_FILE="${PXC_ASSERT_TIERS_FILE:-/opt/antithesis/pxc/assert-tiers.tsv}"
NODE_CNF_DIR="${PXC_NODE_CNF_DIR:-/etc/my.cnf.d}"
NODE_CNF="${NODE_CNF_DIR}/node.cnf"

PXC_NODE_NAME="${PXC_NODE_NAME:-$(hostname)}"
PXC_NODE_ADDRESS="${PXC_NODE_ADDRESS:-}"
PXC_BOOTSTRAP="${PXC_BOOTSTRAP:-0}"
PXC_WORKLOAD_USER="${PXC_WORKLOAD_USER:-antithesis}"
PXC_WORKLOAD_PASSWORD="${PXC_WORKLOAD_PASSWORD:-antithesis}"
PXC_RESTART_DELAY="${PXC_RESTART_DELAY:-2}"

BOOTSTRAP_MARKER="${STATE_DIR}/bootstrapped"
HOLDDOWN_MARKER="${STATE_DIR}/hold-down"
KILL_MARKER="${STATE_DIR}/kill"
# How the background kill watcher learns the current mysqld pid; see
# watch_kill_channel for why a variable would not work.
MYSQLD_PID_FILE="${STATE_DIR}/mysqld.pid"

MYSQLD_PID=""
SHUTTING_DOWN=0
BOOT_COUNT=0
# Error log line count at the start of the current boot, so a death is judged
# only on the log this boot produced.
BOOT_LOG_OFFSET=0
# Set by assert_death_class and its helpers: 1 when this boot's death was a
# failed assertion, a fatal signal or an undocumented unireg_abort, i.e. a bug
# rather than a documented death path.
BUG_DEATH=0
EXIT_STATUS=0
EXIT_KIND=""
EXIT_SIGNAL=0
EXIT_FIELD_RESTART=""

mkdir -p "${STATE_DIR}" "${NODE_CNF_DIR}" "$(dirname "${LOG_ERROR}")"

# ---------------------------------------------------------------------------
# JSONL emission
#
# Goes to stdout (Antithesis captures it) and, when running inside Antithesis,
# to a supervisor.jsonl in the output directory. Deliberately NOT sdk.jsonl —
# that file is reserved for SDK events and setup_complete.
# ---------------------------------------------------------------------------
emit() {
    local event="$1"; shift
    local extra="${1:-}"
    local line
    line=$(printf '{"supervisor":{"event":"%s","node":"%s","boot":%d,"ts":%s%s}}' \
        "${event}" "${PXC_NODE_NAME}" "${BOOT_COUNT}" "$(date +%s)" \
        "${extra:+,${extra}}")
    printf '%s\n' "${line}"
    if [[ -n "${ANTITHESIS_OUTPUT_DIR:-}" ]]; then
        mkdir -p "${ANTITHESIS_OUTPUT_DIR}"
        printf '%s\n' "${line}" >> "${ANTITHESIS_OUTPUT_DIR}/supervisor.jsonl"
    fi
}

log() { printf '[supervisor] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Antithesis SDK emission (fallback SDK)
#
# The supervisor is the ONLY component that observes how mysqld died. The
# workload cannot: a dead node answers no queries, and performance_schema.
# error_log is an in-memory ring buffer that is lost on restart. So the death
# classification has to become a property from here.
#
# There is no bash SDK, so this uses the fallback SDK: single-line JSON objects
# written to $ANTITHESIS_OUTPUT_DIR/sdk.jsonl. Every assertion is written twice
# — once at supervisor startup as a CATALOG DECLARATION (hit:false), and again
# when it actually fires (hit:true). The declaration is what makes an unfired
# claim reportable instead of silently absent, so the two must agree on id,
# message and location.
#
# Ids are inline constant strings for the same reason they are in oracles.py:
# a name built at run time is invisible to reporting. begin_line values are
# stable nominal ids, not real line numbers — bash has no useful callsite.
# ---------------------------------------------------------------------------
SDK_FILE=""
if [[ -n "${ANTITHESIS_OUTPUT_DIR:-}" ]]; then
    SDK_FILE="${ANTITHESIS_OUTPUT_DIR}/sdk.jsonl"
fi

A_DIED_UNRESTARTABLE="a node died in a way the shipped systemd unit would not restart"
A_DIED_UNRESTARTABLE_LINE=1

A_DIED_IN_STARTUP="a node died before mysqld reached ready for connections"
A_DIED_IN_STARTUP_LINE=2

A_DIED_AFTER_SST_FAILURE="a node died in a boot whose state transfer had failed"
A_DIED_AFTER_SST_FAILURE_LINE=3

A_DIED_INCONSISTENT="a node died after the cluster declared it inconsistent"
A_DIED_INCONSISTENT_LINE=4

# The server's own invariants, as properties. Until these existed, every
# assert() abort landed in the platform's generic "No unexpected crashes ->
# mysqld" group: one red for 14 distinct crash sites, none of them named in
# the report. Two tiers:
#   - one declared umbrella, so a run with no abort shows it passing rather
#     than absent;
#   - one property per assert SITE (file:line), built at run time. The rule
#     that ids must be inline constants exists for claims that have to be
#     cataloged to be reported unfired. An Unreachable does not: absent and
#     passing mean the same thing, and the platform evaluates an undeclared
#     assertion the first time it sees it. Keying on file:line is what lets a
#     report say gcs_node.cpp:224 x41 instead of "mysqld x76".
#
# Both tiers are split by BUILD TIER, because the findings go to the Percona
# team and every death has to say whether a production build would die the
# same way. This image is a Debug build (mysqld: CMAKE_BUILD_TYPE=Debug, so
# UNIV_DEBUG on and NDEBUG off; Galera: debug=3, NDEBUG off), so it checks
# invariants that release builds compile out:
#   - debug-only: glibc assert() in mysqld, wsrep-lib or Galera (release
#     builds define NDEBUG; Galera's SConstruct:92), InnoDB ut_ad, and InnoDB
#     ut_a inside #ifdef UNIV_DEBUG. The invariant is PXC's own, but a release
#     build has no check there and would carry on, for better or worse.
#   - release: an InnoDB ut_a or ut_error that release builds keep. A
#     production build would die the same way.
#   - unknown: an InnoDB site that the build-time table cannot place.
# The harness forces none of these. It only reads the abort from the log.
A_ASSERT_DEBUG_ANY="mysqld never fails an assertion that only debug builds check"
A_ASSERT_DEBUG_ANY_LINE=5
A_ASSERT_SITE_LINE=6
A_ASSERT_RELEASE_ANY="mysqld never fails an assertion that release builds also check"
A_ASSERT_RELEASE_ANY_LINE=12
A_ASSERT_UNKNOWN_ANY="mysqld never fails an assertion of unknown build tier"
A_ASSERT_UNKNOWN_ANY_LINE=13

# gu_abort() is Galera stopping the process on purpose: it logs
# "<program>: Terminated." (galerautils/src/gu_abort.c:46) and calls abort().
# It is unconditional code, so a release build dies the same way. Until this
# class existed these deaths fell into the undiagnosed catch-all below,
# although the log says exactly what happened (run
# 6a88fb9ba55565a5eed180ad68837c8d-63-3: a joiner whose donor answered
# "State transfer ... failed: No message of desired type", then
# "gcs_group.cpp:1379: Will never receive state. Need to abort."). Same shape
# as unireg_abort below: a declared umbrella, and one property per cause,
# keyed on the last [ERROR] before the Terminated line.
A_GU_ABORT_UNDOCUMENTED="mysqld never calls gu_abort for an undocumented reason"
A_GU_ABORT_UNDOCUMENTED_LINE=14
A_GU_ABORT_CAUSE_LINE=15

A_FATAL_SIGNAL_ANY="mysqld never dies on a fatal signal outside a failed assertion"
A_FATAL_SIGNAL_ANY_LINE=7
A_FATAL_SIGNAL_LINE=8

# Catch-all keyed on HOW mysqld exited, not on what it logged: exit status 2
# (mysqld's own fatal-signal handler, or the GTID out-of-memory _exit), or a
# death by any signal the harness did not send itself. The harness sends
# SIGKILL (the workload kill channel) and SIGTERM (container stop), so those
# two are excluded. This is what makes "every crash fails a property" hold
# even when the log slice has no parsable line.
A_FATAL_EXIT_UNDIAGNOSED="mysqld never dies on a fatal path without a diagnosable log line"
A_FATAL_EXIT_UNDIAGNOSED_LINE=9

# unireg_abort (exit status 1) is mysqld stopping itself after logging an
# [ERROR]. Some of those stops are the documented response to a condition
# fault injection legitimately causes; those stay coverage (the four reach
# claims above). Any other cause is a bug and fails here. Two tiers, as for
# asserts: a declared umbrella, and one property per cause, keyed on the
# MY-code of the last [ERROR] before "Aborting". Galera and WSREP lines all
# carry MY-000000, so for that code the key also carries the normalized
# message, or every wsrep cause would collapse into one property.
A_UNIREG_UNDOCUMENTED="mysqld never stops itself for an undocumented reason"
A_UNIREG_UNDOCUMENTED_LINE=10
A_UNIREG_CAUSE_LINE=11

# Documented unireg_abort causes, as EREs matched against the [ERROR] lines
# just before "Aborting" (see unireg_cause_lines). Add a pattern ONLY with a
# source citation showing the stop is the intended response to a condition
# the fault injector can create. Never add one to make a count go down.
UNIREG_DOCUMENTED_CAUSES=(
    # No primary component within pc.wait_prim_timeout (default PT30S): gcomm
    # throws ETIMEDOUT (galera gcomm/src/pc.cpp:161-177, defaults.cpp:66,
    # doc/source/wsrep-provider-index.rst "pc.wait_prim_timeout"), and
    # wsrep_init_startup() unireg_abort(1)s on the failed connect
    # (sql/wsrep_mysqld.cc:1297). A partition, or peers that are down or
    # non-primary, legitimately cause it.
    '\[Galera\] failed to open gcomm backend connection: [0-9]+: failed to reach primary view \(pc\.wait_prim_timeout\)'
)

# Documented gu_abort causes, same rule as UNIREG_DOCUMENTED_CAUSES: add a
# pattern ONLY with a source citation showing the abort is the intended
# response to a condition the fault injector can create.
# Deliberately empty for now. The one cause seen so far (a joiner whose donor
# could not serve it) happened in a run with no faults, so it cannot be
# excused as a fault response.
GU_ABORT_DOCUMENTED_CAUSES=(
)

# Minimal JSON string escaping. The payload is one line of mysqld error log,
# which routinely contains quotes, backslashes and stray control bytes.
json_escape() {
    printf '%s' "${1:-}" | LC_ALL=C tr -d '\000-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# $1 id/message  $2 nominal line  $3 hit (true|false)  $4 details JSON or ""
sdk_reachable() {
    local id="$1" line="$2" hit="$3" details="${4:-}"
    [[ -n "${SDK_FILE}" ]] || return 0
    mkdir -p "$(dirname "${SDK_FILE}")" 2>/dev/null || true
    printf '{"antithesis_assert":{"hit":%s,"must_hit":true,"assert_type":"reachability","display_type":"Reachable","condition":%s,"id":"%s","message":"%s","location":{"class":"","function":"supervisor","file":"antithesis/pxc-node/entrypoint.sh","begin_line":%s,"begin_column":0}%s}}\n' \
        "${hit}" "${hit}" "${id}" "${id}" "${line}" "${details:+,\"details\":${details}}" \
        >> "${SDK_FILE}"
}

# $1 id/message  $2 nominal line  $3 hit (true|false)  $4 details JSON or ""
# Unreachable: must_hit false, and condition false both declared and on hit.
sdk_unreachable() {
    local id="$1" line="$2" hit="$3" details="${4:-}"
    [[ -n "${SDK_FILE}" ]] || return 0
    mkdir -p "$(dirname "${SDK_FILE}")" 2>/dev/null || true
    printf '{"antithesis_assert":{"hit":%s,"must_hit":false,"assert_type":"reachability","display_type":"Unreachable","condition":false,"id":"%s","message":"%s","location":{"class":"","function":"supervisor","file":"antithesis/pxc-node/entrypoint.sh","begin_line":%s,"begin_column":0}%s}}\n' \
        "${hit}" "${id}" "${id}" "${line}" "${details:+,\"details\":${details}}" \
        >> "${SDK_FILE}"
}

# Declare every assertion in the catalog. Runs once, before the restart loop.
sdk_declare_catalog() {
    sdk_unreachable "${A_ASSERT_DEBUG_ANY}"      "${A_ASSERT_DEBUG_ANY_LINE}"      false
    sdk_unreachable "${A_ASSERT_RELEASE_ANY}"    "${A_ASSERT_RELEASE_ANY_LINE}"    false
    sdk_unreachable "${A_ASSERT_UNKNOWN_ANY}"    "${A_ASSERT_UNKNOWN_ANY_LINE}"    false
    sdk_unreachable "${A_GU_ABORT_UNDOCUMENTED}" "${A_GU_ABORT_UNDOCUMENTED_LINE}" false
    sdk_unreachable "${A_FATAL_SIGNAL_ANY}"      "${A_FATAL_SIGNAL_ANY_LINE}"      false
    sdk_unreachable "${A_FATAL_EXIT_UNDIAGNOSED}" "${A_FATAL_EXIT_UNDIAGNOSED_LINE}" false
    sdk_unreachable "${A_UNIREG_UNDOCUMENTED}"   "${A_UNIREG_UNDOCUMENTED_LINE}"   false
    sdk_reachable "${A_DIED_UNRESTARTABLE}"      "${A_DIED_UNRESTARTABLE_LINE}"      false
    sdk_reachable "${A_DIED_IN_STARTUP}"         "${A_DIED_IN_STARTUP_LINE}"         false
    sdk_reachable "${A_DIED_AFTER_SST_FAILURE}"  "${A_DIED_AFTER_SST_FAILURE_LINE}"  false
    sdk_reachable "${A_DIED_INCONSISTENT}"       "${A_DIED_INCONSISTENT_LINE}"       false
}

# ---------------------------------------------------------------------------
# Per-node config fragment
# ---------------------------------------------------------------------------
write_node_cnf() {
    if [[ -z "${PXC_NODE_ADDRESS}" ]]; then
        log "FATAL: PXC_NODE_ADDRESS is unset. Static addressing is required —"
        log "       gcomm resolves peer addresses exactly once at connect() and"
        log "       retries stale IPs forever, so a node that changes address on"
        log "       restart turns every restart scenario into the same finding."
        exit 1
    fi
    cat > "${NODE_CNF}" <<EOF
# Generated by the Antithesis supervisor. Do not edit by hand.
[mysqld]
wsrep_node_name             = ${PXC_NODE_NAME}
wsrep_node_address          = ${PXC_NODE_ADDRESS}
wsrep_node_incoming_address = ${PXC_NODE_ADDRESS}:3306
wsrep_sst_receive_address   = ${PXC_NODE_ADDRESS}:4444
EOF
    log "wrote ${NODE_CNF} for ${PXC_NODE_NAME} (${PXC_NODE_ADDRESS})"
}

# ---------------------------------------------------------------------------
# Datadir initialization (first boot only)
# ---------------------------------------------------------------------------
datadir_initialized() { [[ -d "${DATADIR}/mysql" ]]; }

initialize_datadir() {
    emit "initialize_start"
    log "initializing datadir at ${DATADIR}"
    # wsrep must be off during --initialize: there is no cluster to join yet.
    if ! "${PXC_PREFIX}/bin/mysqld" \
            --defaults-file="${DEFAULTS_FILE}" \
            --initialize-insecure \
            --datadir="${DATADIR}" \
            --user=mysql \
            --wsrep_provider=none; then
        emit "initialize_failed"
        log "FATAL: datadir initialization failed"
        # my.cnf sets log-error, so the actual reason went to a file rather
        # than to stdout. Echo it, or this failure is undiagnosable from the
        # container log alone.
        if [[ -f "${LOG_ERROR}" ]]; then
            sed -e 's/^/[initialize] /' "${LOG_ERROR}" || true
        fi
        exit 1
    fi
    emit "initialize_done"
    log "datadir initialized"
}

# The workload connects remotely, so it needs an account that does not exist
# in a fresh datadir. Created on the bootstrap node's first start only —
# joiners inherit it through SST/IST, which copies the mysql schema wholesale.
#
# GRANT ALL covers only static privileges in MySQL 8; the workload also needs
# dynamic ones to drive SET GLOBAL wsrep_*, backup locks and connection
# management, so those are granted explicitly.
write_init_file() {
    local f="${STATE_DIR}/init.sql"
    cat > "${f}" <<EOF
CREATE USER IF NOT EXISTS '${PXC_WORKLOAD_USER}'@'%' IDENTIFIED BY '${PXC_WORKLOAD_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO '${PXC_WORKLOAD_USER}'@'%' WITH GRANT OPTION;
GRANT SYSTEM_VARIABLES_ADMIN, CONNECTION_ADMIN, BACKUP_ADMIN,
      SERVICE_CONNECTION_ADMIN, SESSION_VARIABLES_ADMIN, SYSTEM_USER,
      PERSIST_RO_VARIABLES_ADMIN, GROUP_REPLICATION_ADMIN, REPLICATION_SLAVE_ADMIN
  ON *.* TO '${PXC_WORKLOAD_USER}'@'%';
EOF
    printf '%s' "${f}"
}

# ---------------------------------------------------------------------------
# Recovery dance
#
# Reproduces what mysqld_safe / mysql-systemd galera-recovery do: run
# `mysqld --wsrep-recover`, read the recovered position out of the log, and
# start the real server at that position.
#
# The log-grep pattern is an open question in the topology ('WSREP:' vs
# '[WSREP]'), so this parses whatever the binary actually emits rather than
# assuming either form.
# ---------------------------------------------------------------------------
# Sets the global RECOVERED_POSITION. Deliberately NOT via command
# substitution: this function also emits JSONL and echoes the recover log, and
# capturing its stdout would swallow all of that into the position string.
RECOVERED_POSITION=""

recover_position() {
    local recover_log="${STATE_DIR}/wsrep-recover.log"
    local recover_stdio="${STATE_DIR}/wsrep-recover.stdio"
    RECOVERED_POSITION=""
    : > "${recover_log}"
    : > "${recover_stdio}"
    # Hand the log to mysql, for the same reason LOG_ERROR is chowned below.
    # mysqld drops to --user=mysql BEFORE it opens --log-error, and `: >` run
    # as root creates a root-owned 0644 file. Without this chown, every
    # recovery in run 93ec5045...-63-2 exited rc=1 without writing a single
    # line. So every rejoin started without --wsrep_start_position, which is
    # not the path mysqld_safe and galera-recovery take in the field.
    chown mysql:mysql "${recover_log}" 2>/dev/null || true

    emit "recover_start"
    # stdout/stderr are kept, not discarded. They are where mysqld reports a
    # failure that happens before --log-error is open: exactly the failure
    # above, which /dev/null made invisible.
    "${PXC_PREFIX}/bin/mysqld" \
        --defaults-file="${DEFAULTS_FILE}" \
        --datadir="${DATADIR}" \
        --user=mysql \
        --wsrep-recover \
        --log-error="${recover_log}" >"${recover_stdio}" 2>&1
    local rc=$?

    # Accept both the bracketed '[WSREP]' and unbracketed 'WSREP:' forms.
    # deployment-topology.md leaves which one this binary emits as an open
    # question, so match the position itself rather than the prefix.
    local pos
    pos=$(grep -aoE 'Recovered position:?[[:space:]]*[0-9a-fA-F-]{8,}:-?[0-9]+' "${recover_log}" \
          | tail -n1 \
          | grep -aoE '[0-9a-fA-F-]{8,}:-?[0-9]+' \
          | tail -n1)

    # Surface the recover log so an unparsed position is debuggable rather
    # than a silent fallback.
    sed -e 's/^/[wsrep-recover] /' "${recover_log}" || true
    sed -e 's/^/[wsrep-recover:stdio] /' "${recover_stdio}" || true

    if [[ -z "${pos}" ]]; then
        emit "recover_no_position" "\"rc\":${rc}"
        log "no recovered position parsed (rc=${rc}); starting without --wsrep_start_position"
        return 0
    fi

    RECOVERED_POSITION="${pos}"
    emit "recover_done" "\"rc\":${rc},\"position\":\"${pos}\""
    log "recovered position: ${pos}"
}

# grastate.dat is the other half of the recovery picture, and the supervisor is
# the only observer in the harness with file-level access to it. Emitting the
# parsed fields on every boot is near-free here and is what the workload's
# epoch ledger and the grastate/checkpoint-agreement properties consume.
probe_grastate() {
    local f="${DATADIR}/grastate.dat"
    if [[ ! -f "${f}" ]]; then
        emit "grastate_absent"
        return
    fi
    local uuid seqno safe
    uuid=$(awk -F': *' '/^uuid:/    {print $2}' "${f}" | tr -d '[:space:]')
    seqno=$(awk -F': *' '/^seqno:/  {print $2}' "${f}" | tr -d '[:space:]')
    safe=$(awk -F': *' '/^safe_to_bootstrap:/ {print $2}' "${f}" | tr -d '[:space:]')
    emit "grastate" \
        "\"uuid\":\"${uuid:-}\",\"seqno\":\"${seqno:-}\",\"safe_to_bootstrap\":\"${safe:-}\""
}

# ---------------------------------------------------------------------------
# Restart accounting
#
# Classifies each mysqld death and records whether shipped systemd
# (Restart=on-abort, RestartPreventExitStatus=SIGABRT, plus unireg_abort's
# exit 1) would have brought it back. Triage joins these records with liveness
# properties to distinguish "recovered" from "the field would still be down".
# ---------------------------------------------------------------------------
classify_exit() {
    local status="$1"
    local kind signal field_restart
    # Also published as EXIT_KIND / EXIT_FIELD_RESTART so the SDK assertions
    # below can key on the same verdict the JSONL records, rather than
    # re-deriving it and risking the two disagreeing.

    if (( status > 128 )); then
        signal=$(( status - 128 ))
        kind="signal"
    else
        signal=0
        kind="exit"
    fi

    if [[ "${kind}" == "exit" && "${status}" -eq 0 ]]; then
        # Clean exit: SQL SHUTDOWN or mysqladmin shutdown.
        field_restart="false"
        kind="graceful"
    elif [[ "${kind}" == "exit" && "${status}" -eq 1 ]]; then
        # unireg_abort(1) — explicitly excluded from restart in the field.
        field_restart="false"
        kind="unireg_abort"
    elif [[ "${kind}" == "signal" && "${signal}" -eq 6 ]]; then
        # SIGABRT: assert() or gu_abort(). RestartPreventExitStatus=SIGABRT
        # keeps these nodes down in the field.
        field_restart="false"
        kind="abort"
    elif [[ "${kind}" == "signal" ]]; then
        # Any other signal (SIGKILL from the crash channel, SIGSEGV, ...) —
        # Restart=on-abort covers these.
        field_restart="true"
        kind="crash"
    else
        field_restart="false"
        kind="exit"
    fi

    # Published as globals, NOT printed. The caller used to run this inside a
    # command substitution, which is a subshell — anything assigned there is
    # discarded. exit_json() renders the JSONL fragment from these instead, so
    # the JSONL record and the SDK assertions cannot disagree about the verdict.
    EXIT_STATUS="${status}"
    EXIT_KIND="${kind}"
    EXIT_SIGNAL="${signal}"
    EXIT_FIELD_RESTART="${field_restart}"
}

# Render the classification for emit(). Safe in a subshell: reads only.
exit_json() {
    printf '"status":%d,"kind":"%s","signal":%d,"field_would_restart":%s' \
        "${EXIT_STATUS}" "${EXIT_KIND}" "${EXIT_SIGNAL}" "${EXIT_FIELD_RESTART}"
}

# ---------------------------------------------------------------------------
# Boot-scoped error log scan.
#
# Read once per death, from the line the error log was at when this boot
# started, so evidence from earlier boots cannot leak into this verdict.
# Everything fragile (the pattern list) feeds DETAILS; the only thing a pattern
# decides on its own is a reach claim, never a pass/fail verdict.
# ---------------------------------------------------------------------------
boot_log_slice() {
    tail -n +$(( BOOT_LOG_OFFSET + 1 )) "${LOG_ERROR}" 2>/dev/null
}

# Which code base a crash site sits in, by path alone. This is a hint for
# triage, not the ownership verdict: an assert inside an upstream file is
# still Percona's when a wsrep_* frame put it there (scratchbook/
# triage-scope.md classifies by caller, which needs the backtrace).
site_component() {
    case "$1" in
        gcs/*|galera/*|galerautils/*|gcache/*|gcomm/*) printf 'galera' ;;
        wsrep-lib/*)                                   printf 'wsrep-lib' ;;
        sql/wsrep*|storage/innobase/*wsrep*)           printf 'pxc-wsrep' ;;
        *)                                             printf 'server' ;;
    esac
}

# True when the exit itself says mysqld died on a fatal path.
fatal_exit_status() {
    [[ "${EXIT_KIND}" == "exit" && "${EXIT_STATUS}" -eq 2 ]] && return 0
    [[ "${EXIT_KIND}" == "abort" ]] && return 0
    [[ "${EXIT_KIND}" == "crash" && "${EXIT_SIGNAL}" -ne 9 && "${EXIT_SIGNAL}" -ne 15 ]] && return 0
    return 1
}

# Build tier of one assertion site: debug_only, release or unknown.
# $1 form (glibc|innodb)  $2 site as printed (FILE:LINE)
assert_tier() {
    local form="$1" site="$2" tier=""
    # glibc assert() exists only without NDEBUG, which every release build
    # defines. No lookup needed.
    if [[ "${form}" == "glibc" ]]; then
        printf 'debug_only'
        return 0
    fi
    # InnoDB prints the same text for ut_a and ut_ad, so only the source can
    # tell; assert_tiers.py read it at build time.
    if [[ -r "${ASSERT_TIERS_FILE}" ]]; then
        tier="$(awk -F'\t' -v k="${site}" '$1 == k { print $2; exit }' "${ASSERT_TIERS_FILE}")"
    fi
    printf '%s' "${tier:-unknown}"
}

# One failed-assertion property per crash site, for the boot that just ended.
# Recognizes the two forms the build emits:
#   mysqld: FILE:LINE: FUNC: Assertion `EXPR' failed.        (glibc assert)
#   [InnoDB] Assertion failure: FILE:LINE:EXPR               (ut_a / ut_ad)
# Only the FIRST in the boot is the one that killed it; later lines are
# noise from the dying process.
assert_failed_site() {
    local slice="$1" hit file line func expr site component details form tier
    local umbrella umbrella_line per_site has_check
    BUG_DEATH=0
    # Set by a class that explained the death without calling it a bug (a
    # documented gu_abort cause), so the undiagnosed catch-all stays quiet.
    DIAGNOSED=0
    hit="$(grep -m1 -E "^mysqld: [^:]+:[0-9]+: .*: Assertion \`.*' failed\.|\[InnoDB\] Assertion failure: [^:]+:[0-9]+" <<< "${slice}")"
    if [[ -z "${hit}" ]]; then
        # Not an assertion. gu_abort logs its own line, so it is told apart
        # from a bare fatal signal before falling back to the signal.
        gu_abort_death "${slice}" && return 0
        fatal_signal_without_assert "${slice}"
        return 0
    fi

    if [[ "${hit}" =~ ^mysqld:\ ([^:]+):([0-9]+):\ (.*):\ Assertion\ \`(.*)\'\ failed\.$ ]]; then
        file="${BASH_REMATCH[1]}"; line="${BASH_REMATCH[2]}"
        func="${BASH_REMATCH[3]}"; expr="${BASH_REMATCH[4]}"
        form="glibc"
    elif [[ "${hit}" =~ Assertion\ failure:\ ([^:]+):([0-9]+):?(.*)$ ]]; then
        file="${BASH_REMATCH[1]}"; line="${BASH_REMATCH[2]}"
        func=""; expr="${BASH_REMATCH[3]}"
        expr="${expr%% thread [0-9]*}"
        form="innodb"
    else
        return 0
    fi
    # Server-tree asserts print an absolute build path (/src/...), Galera's a
    # relative one. Normalize, or the same site splits into two properties.
    file="${file#/src/}"
    site="${file}:${line}"
    component="$(site_component "${file}")"
    tier="$(assert_tier "${form}" "${site}")"

    # The tier goes into the property NAME, not only the details, so the
    # report itself says whether a release build has this check.
    case "${tier}" in
        debug_only)
            umbrella="${A_ASSERT_DEBUG_ANY}"; umbrella_line="${A_ASSERT_DEBUG_ANY_LINE}"
            per_site="mysqld debug-only assertion failed at ${site}"; has_check=false ;;
        release)
            umbrella="${A_ASSERT_RELEASE_ANY}"; umbrella_line="${A_ASSERT_RELEASE_ANY_LINE}"
            per_site="mysqld release-build assertion failed at ${site}"; has_check=true ;;
        *)
            tier="unknown"
            umbrella="${A_ASSERT_UNKNOWN_ANY}"; umbrella_line="${A_ASSERT_UNKNOWN_ANY_LINE}"
            per_site="mysqld assertion of unknown build tier failed at ${site}"; has_check='"unknown"' ;;
    esac

    details="$(printf '{"node":"%s","boot":%d,"site":"%s","component":"%s","function":"%s","expression":"%s","exit_status":%d,"kind":"%s","exit_cause":"%s","build_tier":"%s","release_build_has_this_check":%s,"log_line":"%s"}' \
        "${PXC_NODE_NAME}" "${BOOT_COUNT}" "$(json_escape "${site}")" "${component}" \
        "$(json_escape "${func}")" "$(json_escape "${expr}")" "${EXIT_STATUS}" "${EXIT_KIND}" \
        "${tier}_assert" "${tier}" "${has_check}" \
        "$(json_escape "$(cut -c1-400 <<< "${hit}")")")"

    BUG_DEATH=1
    sdk_unreachable "${umbrella}" "${umbrella_line}" true "${details}"
    sdk_unreachable "$(json_escape "${per_site}")" "${A_ASSERT_SITE_LINE}" true "${details}"
    emit "assertion_failed" "\"site\":\"$(json_escape "${site}")\",\"component\":\"${component}\",\"build_tier\":\"${tier}\""
}

# A gu_abort death: Galera logged "<program>: Terminated." and aborted.
# Returns 1 when the slice has no such line, so the caller tries the next
# class. Sets BUG_DEATH=1 unless the cause is on GU_ABORT_DOCUMENTED_CAUSES.
gu_abort_death() {
    local slice="$1" cause window pat key details
    grep -qE '\[Galera\] .*: Terminated\.$' <<< "${slice}" || return 1
    # The [ERROR] lines before the Terminated line are the cause; the lines
    # after it ("Terminating SST process", SST script cleanup) are teardown.
    cause="$(awk '/\[Galera\] .*: Terminated\.$/ { exit } /\[ERROR\]/ { print }' <<< "${slice}")"
    window="$(tail -n 8 <<< "${cause}")"
    for pat in "${GU_ABORT_DOCUMENTED_CAUSES[@]}"; do
        if [[ -n "${window}" ]] && grep -qE -- "${pat}" <<< "${window}"; then
            DIAGNOSED=1
            return 0
        fi
    done

    key="$(unireg_cause_key "$(tail -n 1 <<< "${cause}")")"
    details="$(printf '{"node":"%s","boot":%d,"status":%d,"kind":"%s","exit_cause":"gu_abort","release_build_has_this_check":true,"cause":"%s","last_errors":"%s"}' \
        "${PXC_NODE_NAME}" "${BOOT_COUNT}" "${EXIT_STATUS}" "${EXIT_KIND}" \
        "$(json_escape "${key}")" \
        "$(json_escape "$(tail -n 5 <<< "${cause}" | cut -c1-240 | tr '\n' '|')")")"
    BUG_DEATH=1
    sdk_unreachable "${A_GU_ABORT_UNDOCUMENTED}" "${A_GU_ABORT_UNDOCUMENTED_LINE}" true "${details}"
    sdk_unreachable "mysqld called gu_abort after $(json_escape "${key}")" "${A_GU_ABORT_CAUSE_LINE}" true "${details}"
    emit "gu_abort" "\"cause\":\"$(json_escape "${key}")\""
    return 0
}

# A fatal signal with no assert line: SIGSEGV, or an abort() that printed no
# expression (gu_abort, my_abort). Run c89f2f7a...-63-2 had 219
# "got signal 6" against 179 assert lines, plus two SIGSEGVs. mysqld's own
# handler turns every one of these into _exit(2) (signal_handler.cc), so the
# platform's crash detector, which counts deaths BY signal, never sees them.
# The status-2 exits outnumbered the SIGABRT ones 213 to 96. The id is keyed
# on the signal, because there is no site to key on.
fatal_signal_without_assert() {
    local slice="$1" hit sig details
    hit="$(grep -m1 -E 'mysqld got (signal|exception) [0-9]+' <<< "${slice}")"
    [[ -n "${hit}" ]] || return 0
    [[ "${hit}" =~ got\ (signal|exception)\ ([0-9]+) ]] || return 0
    sig="${BASH_REMATCH[2]}"
    # release_build_has_this_check is "unknown": a SIGSEGV or a bare abort in
    # a Debug -O2 build may or may not happen the same way in a release build.
    details="$(printf '{"node":"%s","boot":%d,"signal":%d,"exit_status":%d,"kind":"%s","exit_cause":"fatal_signal","release_build_has_this_check":"unknown","log_line":"%s","last_error":"%s"}' \
        "${PXC_NODE_NAME}" "${BOOT_COUNT}" "${sig}" "${EXIT_STATUS}" "${EXIT_KIND}" \
        "$(json_escape "$(cut -c1-300 <<< "${hit}")")" \
        "$(json_escape "$(grep -F '[ERROR]' <<< "${slice}" | tail -n 1 | cut -c1-300)")")"
    BUG_DEATH=1
    sdk_unreachable "${A_FATAL_SIGNAL_ANY}" "${A_FATAL_SIGNAL_ANY_LINE}" true "${details}"
    sdk_unreachable "mysqld died on fatal signal ${sig} without a failed assertion" "${A_FATAL_SIGNAL_LINE}" true "${details}"
    emit "fatal_signal" "\"signal\":${sig}"
}

# The [ERROR] lines logged before the LAST "[MY-010119] [Server] Aborting" of
# the slice: the cause of a unireg_abort. The lines after it
# ("Failed to shutdown components infrastructure", ...) are the teardown, not
# the cause. With no Aborting line at all, every [ERROR] line of the slice.
unireg_cause_lines() {
    awk '
        /\[MY-010119\] \[Server\] Aborting/ { kn = n; for (i = 0; i < n; i++) k[i] = b[i]; seen = 1; next }
        /\[ERROR\]/ { b[n++] = $0 }
        END {
            if (!seen) { kn = n; for (i = 0; i < n; i++) k[i] = b[i] }
            for (i = 0; i < kn; i++) print k[i]
        }' <<< "$1"
}

# Property key for one unireg_abort cause, from the last [ERROR] before
# Aborting: "MY-013183 [InnoDB]", or for MY-000000 the code plus the message
# with addresses, UUIDs and long numbers normalized away.
unireg_cause_key() {
    local line="$1" code sub msg
    if [[ ! "${line}" =~ \[ERROR\]\ \[(MY-[0-9]+)\]\ \[([^]]+)\]\ ?(.*)$ ]]; then
        printf 'no [ERROR] line before exit'
        return 0
    fi
    code="${BASH_REMATCH[1]}"; sub="${BASH_REMATCH[2]}"; msg="${BASH_REMATCH[3]}"
    if [[ "${code}" != "MY-000000" ]]; then
        printf '%s [%s]' "${code}" "${sub}"
        return 0
    fi
    msg="$(sed -E -e 's#gcomm://[^ )]*#gcomm://...#g' \
                  -e 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/<uuid>/g' \
                  -e 's/[0-9]+(\.[0-9]+){3}(:[0-9]+)?/<ip>/g' \
                  -e 's/[0-9]{3,}/N/g' <<< "${msg}" | cut -c1-120)"
    printf '%s [%s] %s' "${code}" "${sub}" "${msg}"
}

# A unireg_abort whose cause is not on UNIREG_DOCUMENTED_CAUSES is a bug.
# Sets BUG_DEATH=1 when it fires, so the coverage claims skip the boot.
unireg_abort_undocumented() {
    local slice="$1" cause window pat key details
    [[ "${EXIT_KIND}" == "unireg_abort" ]] || return 0
    cause="$(unireg_cause_lines "${slice}")"
    # The cause chain is short and contiguous (5 lines for a gcomm connect
    # failure); matching only the tail keeps an earlier, survived error
    # (an SST that fell back to IST) from excusing a different stop.
    window="$(tail -n 8 <<< "${cause}")"
    for pat in "${UNIREG_DOCUMENTED_CAUSES[@]}"; do
        [[ -n "${window}" ]] && grep -qE -- "${pat}" <<< "${window}" && return 0
    done

    key="$(unireg_cause_key "$(tail -n 1 <<< "${cause}")")"
    # unireg_abort is unconditional server code: a release build stops too.
    details="$(printf '{"node":"%s","boot":%d,"status":%d,"kind":"%s","exit_cause":"unireg_abort","release_build_has_this_check":true,"cause":"%s","last_errors":"%s"}' \
        "${PXC_NODE_NAME}" "${BOOT_COUNT}" "${EXIT_STATUS}" "${EXIT_KIND}" \
        "$(json_escape "${key}")" \
        "$(json_escape "$(tail -n 5 <<< "${cause}" | cut -c1-240 | tr '\n' '|')")")"
    BUG_DEATH=1
    sdk_unreachable "${A_UNIREG_UNDOCUMENTED}" "${A_UNIREG_UNDOCUMENTED_LINE}" true "${details}"
    sdk_unreachable "mysqld stopped itself after $(json_escape "${key}")" "${A_UNIREG_CAUSE_LINE}" true "${details}"
    emit "unireg_abort_undocumented" "\"cause\":\"$(json_escape "${key}")\""
}

# Emit the death-class assertions for the boot that just ended.
assert_death_class() {
    local status="$1"
    local slice reached_ready sst_failed inconsistent last_error details

    # A graceful SQL SHUTDOWN is the workload's own lever, not a death.
    [[ "${EXIT_KIND}" == "graceful" ]] && return 0

    slice="$(boot_log_slice)"

    assert_failed_site "${slice}"

    if (( BUG_DEATH == 0 && DIAGNOSED == 0 )) && fatal_exit_status; then
        BUG_DEATH=1
        details="$(printf '{"node":"%s","boot":%d,"kind":"%s","status":%d,"signal":%d,"exit_cause":"undiagnosed","release_build_has_this_check":"unknown","last_lines":"%s"}' \
            "${PXC_NODE_NAME}" "${BOOT_COUNT}" "${EXIT_KIND}" "${status}" "${EXIT_SIGNAL}" \
            "$(json_escape "$(tail -n 8 <<< "${slice}" | cut -c1-200 | tr '\n' '|')")")"
        sdk_unreachable "${A_FATAL_EXIT_UNDIAGNOSED}" "${A_FATAL_EXIT_UNDIAGNOSED_LINE}" true "${details}"
    fi

    if (( BUG_DEATH == 0 )); then
        unireg_abort_undocumented "${slice}"
    fi

    # The four claims below are COVERAGE signals: they pass when seen, and
    # they say the workload drove a node through a documented death path (a
    # failed SST, an inconsistency eviction, a unireg_abort on the documented
    # list). A death from a failed assertion, a fatal signal or an
    # undocumented unireg_abort is a BUG. It is reported only by the Unreachables above, which fail when
    # seen. Before this gate, every assert abort also turned "a node died in
    # a way the shipped systemd unit would not restart" greener. That claim
    # passed with 460 examples in run c89f2f7a...-63-2, while 179 asserts
    # showed nowhere else in the report.
    if (( BUG_DEATH == 1 )); then
        return 0
    fi

    reached_ready=false
    grep -qF 'ready for connections' <<< "${slice}" && reached_ready=true

    sst_failed=false
    grep -qE 'SST failed|Process completed with error: wsrep_sst' <<< "${slice}" && sst_failed=true

    # Both verdict forms: "Inconsistent by consensus" (the cluster voted against
    # this node) and "Could not reach consensus" (the vote itself failed and the
    # node assumed the worst about itself).
    inconsistent=false
    grep -qF 'Inconsistency detected' <<< "${slice}" && inconsistent=true

    # The last ERROR-level line of the boot: whatever diagnostic an operator
    # would actually have to work with. Empty means mysqld died silently.
    last_error="$(grep -F '[ERROR]' <<< "${slice}" | tail -n 1 | cut -c1-300)"

    details="$(printf '{"node":"%s","boot":%d,"kind":"%s","status":%d,"field_would_restart":%s,"reached_ready":%s,"sst_failed":%s,"inconsistent":%s,"last_error":"%s"}' \
        "${PXC_NODE_NAME}" "${BOOT_COUNT}" "${EXIT_KIND}" "${status}" \
        "${EXIT_FIELD_RESTART}" "${reached_ready}" "${sst_failed}" "${inconsistent}" \
        "$(json_escape "${last_error}")")"

    if [[ "${EXIT_FIELD_RESTART}" == "false" ]]; then
        sdk_reachable "${A_DIED_UNRESTARTABLE}" "${A_DIED_UNRESTARTABLE_LINE}" true "${details}"
    fi
    if [[ "${reached_ready}" == "false" ]]; then
        sdk_reachable "${A_DIED_IN_STARTUP}" "${A_DIED_IN_STARTUP_LINE}" true "${details}"
    fi
    if [[ "${sst_failed}" == "true" ]]; then
        sdk_reachable "${A_DIED_AFTER_SST_FAILURE}" "${A_DIED_AFTER_SST_FAILURE_LINE}" true "${details}"
    fi
    if [[ "${inconsistent}" == "true" ]]; then
        sdk_reachable "${A_DIED_INCONSISTENT}" "${A_DIED_INCONSISTENT_LINE}" true "${details}"
    fi
}

# ---------------------------------------------------------------------------
# Workload -> supervisor kill channel
#
# The v1 path to ungraceful death that does not depend on tenant-side node
# termination faults being enabled, and which the workload can aim at a precise
# moment (mid-IST, mid-SST, mid-DDL) in a way platform faults cannot.
# ---------------------------------------------------------------------------
# The watcher runs as a background subshell, so it gets a COPY of the parent's
# variables at fork time and never sees later updates. Both the current pid and
# the current boot number therefore travel through a file, re-read on every
# poll — otherwise the kill would target nothing and every event would be
# misattributed to boot 0.
watch_kill_channel() {
    while :; do
        if [[ -f "${KILL_MARKER}" ]]; then
            local pid="" boot=0
            if [[ -f "${MYSQLD_PID_FILE}" ]]; then
                read -r pid boot < "${MYSQLD_PID_FILE}" 2>/dev/null || true
            fi
            if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
                local reason
                reason=$(head -c 200 "${KILL_MARKER}" 2>/dev/null | tr -d '"\n' || true)
                rm -f "${KILL_MARKER}"
                BOOT_COUNT="${boot:-0}"
                emit "kill_channel_fired" "\"reason\":\"${reason:-unspecified}\",\"pid\":${pid}"
                log "kill channel: SIGKILL to mysqld pid ${pid} (${reason:-unspecified})"
                kill -9 "${pid}" 2>/dev/null || true
            fi
        fi
        sleep 0.25
    done
}

# ---------------------------------------------------------------------------
# Signal handling
#
# On container stop, forward SIGTERM to mysqld and let it shut down cleanly.
# Critically, set SHUTTING_DOWN first so the supervisor loop does not treat
# that exit as a crash and restart mysqld mid-shutdown.
#
# compose sets stop_grace_period: 90s because mysqld's SIGTERM handler first
# sleeps pxc_maint_transition_period (10s by default) before it even begins
# shutting down. Docker's default 10s grace would SIGKILL at the start of every
# graceful stop, guaranteeing an unclean shutdown, grastate seqno -1 and a
# forced SST on the way back.
# ---------------------------------------------------------------------------
on_term() {
    SHUTTING_DOWN=1
    emit "container_stop_requested"
    log "SIGTERM received; forwarding to mysqld and waiting for clean shutdown"
    if [[ -n "${MYSQLD_PID}" ]]; then
        kill -TERM "${MYSQLD_PID}" 2>/dev/null || true
    fi
}
trap on_term TERM INT

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
write_node_cnf

INIT_FILE_ARG=()
if ! datadir_initialized; then
    initialize_datadir
    if [[ "${PXC_BOOTSTRAP}" == "1" ]]; then
        INIT_FILE_ARG=(--init-file="$(write_init_file)")
    fi
fi

watch_kill_channel &
KILL_WATCHER_PID=$!

# Stream the error log to container stdout for the whole life of the
# supervisor. log_output=FILE is a startup-fatal PXC requirement, so without
# this Antithesis sees a service that never says anything.
#
# Started ONCE, outside the restart loop, on purpose: `tail -F` follows the
# path across mysqld restarts and log truncation, whereas starting a tail per
# boot would multiply every line by the number of restarts.
# Create the file up front so `tail -F` has something to follow immediately,
# and hand it to the mysql user — mysqld drops privileges to mysql and could
# not write to a root-owned error log.
touch "${LOG_ERROR}" 2>/dev/null || true
chown mysql:mysql "${LOG_ERROR}" 2>/dev/null || true
#
# A bare `tail`, deliberately NOT `( tail | sed 's/^/[mysqld] /' ) &`. For a
# backgrounded pipeline $! is the subshell, and killing it orphans tail and sed
# rather than stopping them — cleaning that up correctly needs `pkill -P` plus
# an assumption about which process group bash chose. One process means $! is
# exactly the thing to kill, with no such assumptions.
#
# No prefix is lost that matters: supervisor lines carry [supervisor], JSONL
# lines are objects, and mysqld's own timestamped [Note]/[ERROR] format is
# unmistakable.
tail -n +1 -F "${LOG_ERROR}" 2>/dev/null &
LOG_TAIL_PID=$!

sdk_declare_catalog

while :; do
    BOOT_COUNT=$(( BOOT_COUNT + 1 ))

    # Hold-down: lets the workload construct a genuinely all-nodes-down state
    # instead of racing an automatic restart.
    if [[ -f "${HOLDDOWN_MARKER}" ]]; then
        emit "hold_down_active"
        log "hold-down marker present; not starting mysqld"
        while [[ -f "${HOLDDOWN_MARKER}" && "${SHUTTING_DOWN}" -eq 0 ]]; do
            sleep 1
        done
        (( SHUTTING_DOWN == 1 )) && break
        emit "hold_down_cleared"
    fi

    BOOT_LOG_OFFSET=$(wc -l < "${LOG_ERROR}" 2>/dev/null || echo 0)
    emit "boot_start"
    probe_grastate

    ARGS=(--defaults-file="${DEFAULTS_FILE}" --datadir="${DATADIR}" --user=mysql)

    if [[ "${PXC_BOOTSTRAP}" == "1" && ! -f "${BOOTSTRAP_MARKER}" ]]; then
        # Bootstrap exactly once, ever. Re-bootstrapping an already-formed
        # cluster is the split-brain bug this harness exists to FIND; the
        # harness must never cause it by accident. The marker is written
        # before the start attempt so even a crashed bootstrap is not retried.
        touch "${BOOTSTRAP_MARKER}"
        ARGS+=(--wsrep-new-cluster)
        if (( ${#INIT_FILE_ARG[@]} > 0 )); then
            ARGS+=("${INIT_FILE_ARG[@]}")
        fi
        emit "boot_mode" "\"mode\":\"bootstrap\""
        log "bootstrapping new cluster (--wsrep-new-cluster)"
    else
        recover_position
        if [[ -n "${RECOVERED_POSITION}" ]]; then
            ARGS+=(--wsrep_start_position="${RECOVERED_POSITION}")
        fi
        emit "boot_mode" "\"mode\":\"join\",\"position\":\"${RECOVERED_POSITION}\""
        log "joining cluster${RECOVERED_POSITION:+ at position ${RECOVERED_POSITION}}"
    fi

    emit "mysqld_exec"
    "${PXC_PREFIX}/bin/mysqld" "${ARGS[@]}" &
    MYSQLD_PID=$!
    printf '%s %s\n' "${MYSQLD_PID}" "${BOOT_COUNT}" > "${MYSQLD_PID_FILE}"
    log "mysqld started (pid ${MYSQLD_PID})"

    # A trap firing while `wait` is blocked makes wait return 128+signal rather
    # than mysqld's exit status. Re-wait while mysqld is genuinely still alive,
    # so the restart accounting records how mysqld actually died instead of
    # recording the signal the supervisor itself received.
    wait "${MYSQLD_PID}"
    STATUS=$?
    while (( STATUS > 128 )) && kill -0 "${MYSQLD_PID}" 2>/dev/null; do
        wait "${MYSQLD_PID}"
        STATUS=$?
    done

    rm -f "${MYSQLD_PID_FILE}"
    MYSQLD_PID=""

    classify_exit "${STATUS}"
    emit "mysqld_exited" "$(exit_json)"
    assert_death_class "${STATUS}"
    log "mysqld exited with status ${STATUS}"

    probe_grastate

    if (( SHUTTING_DOWN == 1 )); then
        emit "container_stopping"
        log "shutting down; not restarting mysqld"
        break
    fi

    emit "restart_scheduled" "\"delay_seconds\":${PXC_RESTART_DELAY}"
    sleep "${PXC_RESTART_DELAY}"
done

kill "${KILL_WATCHER_PID}" 2>/dev/null || true
kill "${LOG_TAIL_PID}" 2>/dev/null || true
emit "supervisor_exit"
log "supervisor exiting"
