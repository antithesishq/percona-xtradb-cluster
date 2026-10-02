#!/usr/bin/env bash
#
# Build the harness images without compiling PXC again unless PXC changed.
#
# Why this exists: the pxc-node image holds a PXC compile that takes 75-100
# minutes. BuildKit's local cache made harness-only rebuilds fast, but on this
# VM's 25 GB disk the cache did not survive: a build that filled the disk and
# a purge to free space each cost a full compile (2026-10-02). So the compiled
# part, the pxc-base stage of antithesis/Dockerfile, now lives in the
# Antithesis registry, tagged by a hash of everything that goes into it:
#
#   1. Compute the hash of the pxc-base inputs.
#   2. If <repository>/pxc-base:<hash> is in the registry, use it. Otherwise
#      build the pxc-base stage once and push it.
#   3. Build the requested compose services with PXC_BASE_IMAGE set to that
#      image, so pxc-node is only the harness files on top of it.
#
# A plain `docker compose build` still works: PXC_BASE_IMAGE then defaults to
# the local pxc-base stage, which compiles from source.
#
# Usage:
#   ./antithesis/build-images.sh [SERVICE...]   build the services (default: all)
#   ./antithesis/build-images.sh --hash         print the pxc-base hash only
#   ./antithesis/build-images.sh --base-only    make sure pxc-base exists, then stop
#
# Needs: git, docker with buildx, jq, and snouty (for the repository name and
# the registry login that `snouty launch` already uses).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKERFILE="${ROOT}/antithesis/Dockerfile"
COMPOSE_FILE="${ROOT}/antithesis/config/docker-compose.yaml"
END_MARKER='# === END OF PXC-BASE INPUTS ==='

# ==============================================================================
# 1. The pxc-base input hash
# ==============================================================================
#
# What can change the compiled binaries:
#   - the PXC source tree, without antithesis/ (the pxc-src stage removes it);
#   - the submodules (Galera, wsrep-lib, ...), each its own git repository;
#   - antithesis/build/, which patches the source before the compile;
#   - the Dockerfile from the top down to END_MARKER: base images, packages,
#     build arguments and their defaults, and every build step.
#
# The source trees are hashed as git trees of the WORKING tree, not of HEAD,
# so an uncommitted edit to a PXC file changes the hash too. A throwaway copy
# of the index takes `git add -A`, so the real index is never touched, and the
# stat cache in that copy keeps it fast.
#
# Intentionally not hashed:
#   - files that .gitignore hides but the build context still contains. The
#     PXC tree has none that the compile reads; a build that relied on one
#     would reuse a stale base.
#   - the content behind a base image tag (debian:bookworm moving under the
#     same tag). A base built from an older Debian point release stays in use
#     until an input above changes.
#   TODO: a --rebuild flag that ignores the stored base, for those two cases.

# Tree hash of the working tree of the repository at $1, with the paths in
# the remaining arguments left out.
worktree_tree() {
    local dir="$1"; shift
    local index
    index="$(mktemp)"
    cp "$(git -C "${dir}" rev-parse --path-format=absolute --git-path index)" "${index}"
    GIT_INDEX_FILE="${index}" git -C "${dir}" add -A
    # `git add -A` stages untracked files too, so leaving paths out means
    # removing them from the copy, committed and untracked alike.
    local path
    for path in "$@"; do
        GIT_INDEX_FILE="${index}" git -C "${dir}" rm -r -q --cached --ignore-unmatch -- "${path}"
    done
    GIT_INDEX_FILE="${index}" git -C "${dir}" write-tree
    rm -f "${index}"
}

base_hash() {
    {
        # The superproject records each submodule only as a commit id, so
        # each submodule's working tree is hashed on its own below.
        printf 'pxc-tree %s\n' "$(worktree_tree "${ROOT}" antithesis)"
        local sub
        while read -r sub; do
            printf 'submodule %s %s\n' "${sub}" "$(worktree_tree "${ROOT}/${sub}")"
        done < <(git -C "${ROOT}" submodule foreach --quiet --recursive 'echo "$displaypath"')
        # antithesis/build/ in full, untracked files included.
        (cd "${ROOT}" && find antithesis/build -type f -print0 | sort -z | xargs -0 sha256sum)
        # The Dockerfile up to the marker. A missing marker must fail, or the
        # whole file would be hashed and every harness edit would rebuild.
        grep -qxF "${END_MARKER}" "${DOCKERFILE}" \
            || { echo "build-images: '${END_MARKER}' not found in ${DOCKERFILE}" >&2; exit 1; }
        printf 'dockerfile '
        awk -v m="${END_MARKER}" '$0 == m { exit } { print }' "${DOCKERFILE}" | sha256sum
    } | sha256sum | cut -c1-16
}

if [[ "${1:-}" == "--hash" ]]; then
    base_hash
    exit 0
fi

# ==============================================================================
# 2. Use the stored pxc-base, or build and push it once
# ==============================================================================

# The same repository that `snouty launch` pushes to, so the login it already
# set up covers this push.
REPOSITORY="$(snouty doctor --json | jq -r '.settings.repository // empty')"
if [[ -z "${REPOSITORY}" ]]; then
    echo "build-images: snouty reports no image repository (snouty doctor --json)" >&2
    exit 1
fi

HASH="$(base_hash)"
BASE_IMAGE="${REPOSITORY}/pxc-base:${HASH}"

if docker buildx imagetools inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
    echo "build-images: using stored ${BASE_IMAGE}"
else
    echo "build-images: ${BASE_IMAGE} is not in the registry; building it (this compiles PXC)"
    # type=registry pushes straight from the build result. A plain --push on
    # the containerd image store first unpacks the image locally, about 5 GB
    # that this VM's disk may not have.
    docker buildx build \
        --file "${DOCKERFILE}" \
        --target pxc-base \
        --output "type=registry,name=${BASE_IMAGE}" \
        "${ROOT}"
    echo "build-images: pushed ${BASE_IMAGE}"
fi

if [[ "${1:-}" == "--base-only" ]]; then
    exit 0
fi

# ==============================================================================
# 3. Build the compose services on top of it
# ==============================================================================

PXC_BASE_IMAGE="${BASE_IMAGE}" docker compose -f "${COMPOSE_FILE}" build "$@"
