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

# Declare every assertion in the catalog. Runs once, before the restart loop.
sdk_declare_catalog() {
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
    RECOVERED_POSITION=""
    : > "${recover_log}"

    emit "recover_start"
    "${PXC_PREFIX}/bin/mysqld" \
        --defaults-file="${DEFAULTS_FILE}" \
        --datadir="${DATADIR}" \
        --user=mysql \
        --wsrep-recover \
        --log-error="${recover_log}" >/dev/null 2>&1
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

# Emit the death-class assertions for the boot that just ended.
assert_death_class() {
    local status="$1"
    local slice reached_ready sst_failed inconsistent last_error details

    # A graceful SQL SHUTDOWN is the workload's own lever, not a death.
    [[ "${EXIT_KIND}" == "graceful" ]] && return 0

    slice="$(boot_log_slice)"

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
