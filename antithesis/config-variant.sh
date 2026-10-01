#!/usr/bin/env bash
#
# Make a copy of antithesis/config/ with workload switches changed, for one
# launch. Launch parameters (custom.*) configure the Antithesis platform and
# never reach a container, so a workload switch has to travel in the config
# image itself.
#
# Usage:
#   ./antithesis/config-variant.sh <name> PXC_LEVERS=off [VAR=value ...]
#
# Writes antithesis/config-<name>/ and prints its path. Then:
#   snouty validate antithesis/config-<name>
#   snouty launch --config antithesis/config-<name> ...
#
# Why a sibling of config/ and not a temp directory: the compose file builds
# from `context: ../..`. At the same depth, that still points at the repo root,
# so `snouty validate` and `docker compose` see the same file layout as for
# config/ itself.
#
# Only variables that config/docker-compose.yaml already sets on pxc-workload
# can be changed. That keeps the base file the single list of switches, and
# makes a typo fail here instead of producing a variant that silently differs
# in nothing.
#
# The copies are generated, so .gitignore excludes them. Make a fresh one for
# every launch; a stale copy misses later edits to config/.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${HERE}/config"

if [[ $# -lt 2 ]]; then
    sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
fi

NAME="$1"
shift
if [[ ! "${NAME}" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "config-variant: name must be lowercase letters, digits and dashes: ${NAME}" >&2
    exit 2
fi

OUT="${HERE}/config-${NAME}"
rm -rf "${OUT}"
cp -a "${BASE}" "${OUT}"
COMPOSE="${OUT}/docker-compose.yaml"

for assignment in "$@"; do
    var="${assignment%%=*}"
    value="${assignment#*=}"
    if [[ "${var}" == "${assignment}" || ! "${var}" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
        echo "config-variant: expected VAR=value, got: ${assignment}" >&2
        exit 2
    fi
    # Match the exact `      VAR: "..."` line of the environment block. The
    # count check is what turns "variable not in the base file" into an error.
    pattern="^( +)${var}: \".*\"$"
    count=$(grep -cE "${pattern}" "${COMPOSE}" || true)
    if [[ "${count}" != "1" ]]; then
        echo "config-variant: ${var} must appear exactly once in config/docker-compose.yaml (found ${count})" >&2
        rm -rf "${OUT}"
        exit 1
    fi
    sed -E -i "s|${pattern}|\\1${var}: \"${value}\"|" "${COMPOSE}"
    echo "config-variant: ${var}=${value}" >&2
done

echo "${OUT}"
