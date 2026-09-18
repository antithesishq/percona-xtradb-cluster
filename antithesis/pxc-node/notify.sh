#!/usr/bin/env bash
#
# wsrep_notify_cmd script, baked into the image but NOT wired up in the
# baseline config.
#
# wsrep_notify_cmd is READ_ONLY — it cannot be set with SET GLOBAL — so the
# notify-cmd properties need a config *variant* that adds one line to my.cnf:
#
#     wsrep_notify_cmd = /opt/antithesis/pxc/notify.sh
#
# Shipping the script now makes that variant a one-line change. Per
# deployment-topology.md it is the cheapest fault-composition surface in the
# catalog: the provider calls this synchronously on every view change, so a
# slow or hung notify script directly probes whether notification handling can
# block commits (notify-cmd-hang-does-not-block-commits).
#
# Galera invokes it as:
#   notify.sh --status <status> --uuid <uuid> --primary <yes|no>
#             --members <list> --index <n>
#
# Behavior knobs, read at call time so a test command can change them between
# view changes by writing the state directory:
#   $STATE_DIR/notify-delay   seconds to sleep before returning (hang probe)
#   $STATE_DIR/notify-fail    exit non-zero if present
#
set -uo pipefail

STATE_DIR="${PXC_STATE_DIR:-/opt/antithesis/state}"
NODE_NAME="${PXC_NODE_NAME:-$(hostname)}"

STATUS=""; UUID=""; PRIMARY=""; MEMBERS=""; INDEX=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)  STATUS="${2:-}";  shift 2 ;;
        --uuid)    UUID="${2:-}";    shift 2 ;;
        --primary) PRIMARY="${2:-}"; shift 2 ;;
        --members) MEMBERS="${2:-}"; shift 2 ;;
        --index)   INDEX="${2:-}";   shift 2 ;;
        *) shift ;;
    esac
done

# Members is a multi-line, comma-separated blob; flatten it so the event stays
# one JSON line.
MEMBERS_FLAT=$(printf '%s' "${MEMBERS}" | tr '\n' ';' | tr -d '"')

emit() {
    local line
    line=$(printf '{"wsrep_notify":{"node":"%s","status":"%s","uuid":"%s","primary":"%s","index":"%s","members":"%s","ts":%s}}' \
        "${NODE_NAME}" "${STATUS}" "${UUID}" "${PRIMARY}" "${INDEX}" "${MEMBERS_FLAT}" "$(date +%s)")
    printf '%s\n' "${line}"
    if [[ -n "${ANTITHESIS_OUTPUT_DIR:-}" ]]; then
        mkdir -p "${ANTITHESIS_OUTPUT_DIR}"
        printf '%s\n' "${line}" >> "${ANTITHESIS_OUTPUT_DIR}/wsrep_notify.jsonl"
    fi
}

emit

if [[ -f "${STATE_DIR}/notify-delay" ]]; then
    DELAY=$(head -c 16 "${STATE_DIR}/notify-delay" 2>/dev/null | tr -dc '0-9.')
    [[ -n "${DELAY}" ]] && sleep "${DELAY}"
fi

if [[ -f "${STATE_DIR}/notify-fail" ]]; then
    exit 1
fi

exit 0
