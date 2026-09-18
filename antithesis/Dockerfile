# syntax=docker/dockerfile:1
#
# Antithesis harness images for Percona XtraDB Cluster 8.4.
#
# Build context is the repo root (percona-xtradb-cluster/). Stages:
#
#   pxc-build   build mysqld (cmake) + libgalera_smm.so/garbd (scons) from source
#   pxc-node    runtime image for pxc-node1/2/3 — mysqld under a supervisor
#   workload    Python test driver, Antithesis Python SDK, bootstrap assertion
#
# See antithesis/scratchbook/instrumentation-inventory.md for the instrumentation
# decision record and antithesis/VALIDATION.md for how to build and validate.

# ---------------------------------------------------------------------------
# Stage: pxc-build
# ---------------------------------------------------------------------------
FROM debian:bookworm AS pxc-build

ENV DEBIAN_FRONTEND=noninteractive \
    NO_COLOR=1 \
    GCC_COLORS=

# Build toolchain + every -dev package the PXC 8.4 server and the galera
# provider need. Every name here was checked against the bookworm main index.
#
# Note `check` (the C unit-test framework), not libcheck-dev — Debian ships the
# headers in the `check` package itself, with no separate -dev. It is required
# even though we never build galera's unit tests: SConstruct's Configure block
# calls Exit(1) when check.h or libcheck is missing, and that block runs
# unconditionally, whatever targets you ask for.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        bison \
        flex \
        git \
        pkg-config \
        scons \
        python3 \
        perl \
        curl \
        patch \
        file \
        libssl-dev \
        libncurses-dev \
        libedit-dev \
        libaio-dev \
        libnuma-dev \
        libtirpc-dev \
        libudev-dev \
        zlib1g-dev \
        liblz4-dev \
        libzstd-dev \
        libbz2-dev \
        liblzma-dev \
        libcurl4-openssl-dev \
        libldap2-dev \
        libsasl2-dev \
        libkrb5-dev \
        libpam0g-dev \
        libreadline-dev \
        libboost-dev \
        libboost-program-options-dev \
        check \
        libev-dev \
    && rm -rf /var/lib/apt/lists/*

# PXC_INSTRUMENT=1 switches the build to clang with Antithesis coverage
# instrumentation. DEFERRED and UNVALIDATED in v1 — see
# scratchbook/instrumentation-inventory.md "Decision 1". Flipping this on is
# its own task; expect to iterate on the clang build.
ARG PXC_INSTRUMENT=0
ARG ANTITHESIS_CPP_SDK_VERSION=v0.5.0

RUN if [ "$PXC_INSTRUMENT" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends clang lld && \
        rm -rf /var/lib/apt/lists/* && \
        mkdir -p /opt/antithesis-sdk-cpp && \
        curl -fsSL "https://raw.githubusercontent.com/antithesishq/antithesis-sdk-cpp/${ANTITHESIS_CPP_SDK_VERSION}/antithesis_instrumentation.h" \
             -o /opt/antithesis-sdk-cpp/antithesis_instrumentation.h && \
        curl -fsSL "https://raw.githubusercontent.com/antithesishq/antithesis-sdk-cpp/${ANTITHESIS_CPP_SDK_VERSION}/antithesis_sdk.h" \
             -o /opt/antithesis-sdk-cpp/antithesis_sdk.h ; \
    fi

WORKDIR /src
COPY . /src

# ---------------------------------------------------------------------------
# Source patches, applied before either build.
#
# 1. gu_abort() disables core dumps in-process, so every Galera self-destruct
#    dies core-less and no container-level `ulimit -c` can undo it. Strip the
#    suppression. DELIBERATE DIVERGENCE FROM FIELD BEHAVIOR — test image only.
#
# 2. When instrumenting, include antithesis_instrumentation.h in exactly one
#    translation unit. sql/main.cc is a 1-line file holding mysqld's main().
#
# Both patches verify their own effect and fail the build loudly on a miss, so
# an upstream rename surfaces here rather than as a silent no-op that quietly
# removes an oracle. See antithesis/build/patch-sources.sh.
# ---------------------------------------------------------------------------
RUN PXC_INSTRUMENT="$PXC_INSTRUMENT" bash /src/antithesis/build/patch-sources.sh /src

# ---------------------------------------------------------------------------
# Build 1 of 2: the galera provider, via scons.
#
# This mirrors build-ps/build-binary.sh's invocation (psi=1 for the .so is
# required — PXC's galera fork carries extra vtable entries that mysqld
# expects) with one addition: debug=3.
#
#   debug=3  ->  '-g -O3 -fno-omit-frame-pointer'  with NO -DNDEBUG
#
# That is the assert-enabled tier from deployment-topology.md: optimized code
# with live assert(). SConstruct supports it as a first-class option, unlike
# galera's cmake path which hard-codes add_definitions(-DNDEBUG).
#
#   dbug=1   ->  -DGU_DBUG_ON, which makes GU_DBUG_SYNC provider sync points
#                available via wsrep_provider_options. They are inert unless a
#                test sets them, and topology Open Question "GU_DBUG_SYNC
#                availability is UNVERIFIED" is answered by building them in.
#
# NOTE: this does NOT enable MySQL's DBUG/DEBUG_SYNC. DBUG_OFF stays defined
# in every non-Debug server build — that is a separate image tier.
#
# GALERA_LOG_H_ENABLE_CXX is required and is NOT optional tuning. In C++
# translation units, gu_log.h guards the gu_fatal/gu_error/gu_warn/gu_info
# macros behind:
#     #if !defined(__cplusplus) || defined(GALERA_LOG_H_ENABLE_CXX)
# galera's cmake build defines it globally (cmake/common.cmake), but the scons
# build only defines it for gcs/ (gcs/src/SConscript). Several C++ files under
# gcache/ and gcomm/ call gu_error() anyway — e.g. GCache::free()'s catch block
# and FairSendQueue::front()/back() — so the scons build of those modules fails
# with "'gu_error' was not declared in this scope" without it. Verified by
# compile test: the define alone decides it, and NDEBUG is irrelevant.
#
# Environment-variable plumbing here is unusually sharp-edged (SConstruct:229-235):
#   CPPFLAGS  <- $CPPFLAGS   (default '')            safe to set
#   CXXFLAGS  <- $CXXFLAGS   (default '')            safe to set
#   CFLAGS    <- $CFLAGS     (default '')            safe to set
#   LINKFLAGS <- $LDFLAGS    (NOT $LINKFLAGS)        must use LDFLAGS
#   CCFLAGS   <- $CCFLAGS    (default: opt_flags!)   NEVER SET — see below
# Setting CCFLAGS would Replace the default, silently discarding opt_flags and
# with it the whole debug=/NDEBUG decision plus the arch flags. Leave it alone.
# ---------------------------------------------------------------------------
ARG GALERA_DEBUG_LEVEL=3
ARG GALERA_DBUG=1
ARG BUILD_JOBS=

# Optional provenance. GALERA_VERSION is a key=value file, NOT a revision
# string, so it must be parsed rather than cat'd — scons bakes revno straight
# into a preprocessor define (`-DGALERA_REV="..."`, galera/src/SConscript:61),
# and a multi-line value there is an unterminated string literal.
#
# The exact galera commit lives in the submodule gitlink, which the build
# cannot read: .dockerignore excludes the top-level .git, so the submodule's
# .git pointer dangles inside the image. Rather than hardcode a SHA that would
# silently drift when the submodule moves, this is left empty by default —
# scons then reports revision "XXXX". Pass it explicitly when you want the real
# revision stamped into wsrep_provider_version:
#
#   --build-arg GALERA_REVISION=$(git -C percona-xtradb-cluster-galera rev-parse --short HEAD)
ARG GALERA_REVISION=

RUN set -eux; \
    JOBS="${BUILD_JOBS:-$(nproc)}"; \
    cd percona-xtradb-cluster-galera; \
    GALERA_VER="$(sed -n 's/^GALERA_VERSION_MAJOR=//p' GALERA_VERSION)"."$(sed -n 's/^GALERA_VERSION_MINOR=//p' GALERA_VERSION)"; \
    GALERA_EXTRA="$(sed -n 's/^GALERA_VERSION_EXTRA=//p' GALERA_VERSION)"; \
    if [ -n "$GALERA_EXTRA" ]; then GALERA_VER="${GALERA_VER}${GALERA_EXTRA}"; fi; \
    REVNO_ARG=""; \
    if [ -n "$GALERA_REVISION" ]; then REVNO_ARG="revno=${GALERA_REVISION}"; fi; \
    echo "ANTITHESIS: galera version=${GALERA_VER} revno=${GALERA_REVISION:-XXXX}"; \
    BPO=""; \
    if [ -r /usr/lib/x86_64-linux-gnu/libboost_program_options.a ]; then \
        BPO="bpostatic=/usr/lib/x86_64-linux-gnu/libboost_program_options.a"; \
    fi; \
    export CPPFLAGS="-DGALERA_LOG_H_ENABLE_CXX ${CPPFLAGS:-}"; \
    export LDFLAGS="-Wl,--build-id ${LDFLAGS:-}"; \
    if [ "$PXC_INSTRUMENT" = "1" ]; then \
        export CC=clang CXX=clang++; \
        export CPPFLAGS="-I/opt/antithesis-sdk-cpp ${CPPFLAGS}"; \
        export CFLAGS="-fsanitize-coverage=trace-pc-guard -g ${CFLAGS:-}"; \
        export CXXFLAGS="-fsanitize-coverage=trace-pc-guard -g ${CXXFLAGS:-}"; \
    else \
        export CC=gcc CXX=g++; \
    fi; \
    scons -j"$JOBS" --config=force \
        psi=1 debug="$GALERA_DEBUG_LEVEL" dbug="$GALERA_DBUG" \
        boost_pool=0 version="$GALERA_VER" $REVNO_ARG $BPO \
        libgalera_smm.so; \
    scons -j"$JOBS" --config=force \
        debug="$GALERA_DEBUG_LEVEL" dbug="$GALERA_DBUG" \
        boost_pool=0 version="$GALERA_VER" $REVNO_ARG $BPO \
        garb/garbd; \
    test -f libgalera_smm.so; \
    test -f garb/garbd

# Fail loudly if the provider came out with asserts compiled out — the whole
# point of this tier is that assert() is live.
RUN set -eux; \
    cd percona-xtradb-cluster-galera; \
    if ! nm -C libgalera_smm.so | grep -qi '__assert_fail'; then \
        echo "ANTITHESIS: libgalera_smm.so has no __assert_fail reference —" \
             "asserts appear to be compiled out. Expected an assert-enabled build." >&2; \
        exit 1; \
    fi; \
    echo "ANTITHESIS: libgalera_smm.so is assert-enabled"

# ---------------------------------------------------------------------------
# Build 2 of 2: the PXC server, via cmake.
#
# INSTALL_MYSQLTESTDIR is set empty so `make install` skips the mysql-test
# suite — it is hundreds of megabytes that the runtime image has no use for.
# Run MTR from a build tree, not from this image.
#
# WITH_NDB=OFF drops MySQL Cluster (NDB), which is a large, wholly separate
# storage engine with no relationship to galera replication. PXC uses InnoDB;
# nothing in the property catalog touches NDB. Turning it off removes a big
# chunk of build time and, incidentally, one of the two files that failed on
# the ENABLED_DEBUG_SYNC issue above (storage/ndb/plugin/ha_ndbcluster_binlog.cc
# calls debug_sync_set_action from inside DBUG_EXECUTE_IF).
#
# WITH_AUTHENTICATION_LDAP is a strictness flag, not a feature switch — cmake
# documents it as "Report error if the LDAP authentication plugin cannot be
# built", and it defaults ON. On Debian it hard-fails configure because
# WARN_MISSING_SYSTEM_SASL only passes when the SASL *SCRAM plugin* is present
# (from libsasl2-modules-gssapi-mit); libsasl2-dev alone is not enough.
#
# Turned OFF rather than installing that package, because sasl.cmake itself
# says the scram plugin "is not needed for build, but it is needed for
# testing" — i.e. it gates MTR's LDAP auth tests, which this image never runs.
# Nothing in the property catalog touches LDAP/SASL authentication; the
# workload connects with an ordinary MySQL account. If you ever do need the
# LDAP auth plugins, drop this flag and add libsasl2-modules-gssapi-mit to the
# build stage instead.
#
# The sibling strictness options are fine and deliberately left alone:
# WITH_AUTHENTICATION_KERBEROS only warns (CMakeLists.txt:2053-2060), and
# WITH_AUTHENTICATION_WEBAUTHN is satisfied by bundled libfido2 plus the
# libudev-dev installed above.
#
# Deviations from build-ps/build-binary.sh, all deliberate:
#   - WITH_PGO is not used. build-binary.sh defaults to a 3-pass PGO build,
#     which triples build time and is incompatible with instrumentation.
#   - WITH_LTO=OFF. cmake/fprofile.cmake auto-promotes LTO whenever FPROFILE_USE
#     is set, and LTO is incompatible with coverage instrumentation.
#   - NDEBUG is undefined (see below).
#   - No RPATH rewriting / tarball packaging; we install straight into the image.
#
# Boost is NOT downloaded: cmake/boost.cmake pins boost_1_84_0 and says
# "Always use the bundled version", pointing at extra/boost/ in this tree. So
# DOWNLOAD_BOOST/WITH_BOOST are not passed — they would be ignored, and the
# build needs no network access for boost.
#
# BUILD TYPE: Debug, with -O2. This is the single most important choice in
# this file, and it was arrived at the hard way — three separate link failures.
#
# The goal is an assert-enabled *optimized* build. The obvious route —
# RelWithDebInfo with NDEBUG stripped — CANNOT WORK, because this codebase
# decides what to *compile* from the build type and from flag substrings, not
# from the effective macro state. Every one of these is a separate desync:
#
#   mysys/CMakeLists.txt:148      compiles dbug.cc only if the flags string
#                                 MATCHES "DNDEBUG"
#   sql/CMakeLists.txt:653        compiles debug_sync.cc only if the flags
#                                 string MATCHES "DENABLED_DEBUG_SYNC"
#   storage/heap/CMakeLists.txt:54  compiles _check.cc only if
#                                 CMAKE_BUILD_TYPE_UPPER STREQUAL "DEBUG"
#   storage/innobase/innodb.cmake:88  adds -DUNIV_DEBUG to CMAKE_CXX_FLAGS_DEBUG
#                                 (so ut0test.cc's body, and ib_interpreter_*,
#                                 exist only in a Debug-type build)
#
# The heap one is decisive: it tests the literal build type, so NO combination
# of compiler flags can satisfy it. Meanwhile the headers emit calls to all of
# these symbols as soon as NDEBUG is absent. "RelWithDebInfo minus NDEBUG" is a
# configuration the vendor never builds and that cannot be assembled by hand.
#
# So: use the build type the vendor DOES build (Debug, which PXC supports —
# build-binary.sh has --debug, and CMakeLists.txt:1566 deliberately enables
# ENABLED_DEBUG_SYNC while leaving SAFE_MUTEX off for PXC), and change only the
# optimization level. Passing CMAKE_{C,CXX}_FLAGS_DEBUG="-g -O2" keeps every
# debug define the build system adds for us — the final flags come out as
# "-DENABLED_DEBUG_SYNC -g -O2 -DUNIV_DEBUG" — while replacing cmake's implicit
# -O0. The one thing we are guessing is the optimization level, instead of
# guessing an entire configuration.
#
# Caveat, stated honestly: Debug-at-O2 is still not a vendor-tested
# combination. If it misbehaves, dropping to the vendor-exact -O0 is a one-word
# change to PXC_OPT_FLAGS below.
#
# RelWithDebInfo flags are also passed, so PXC_BUILD_TYPE=RelWithDebInfo gives
# a clean NDEBUG release tier — that is the "later: release image (NDEBUG)"
# tier from deployment-topology.md, and it is coherent precisely because we no
# longer try to strip NDEBUG out of it.
#
# WITH_WSREP=ON is required — it is what links wsrep-lib into mysqld. It would
# normally also add the galera subdirectory, making cmake build a second,
# NDEBUG copy of the provider plus galera's unit tests. patch-sources.sh
# comments that one ADD_SUBDIRECTORY out (keeping wsrep-lib), because the
# provider is a runtime dlopen() target that nothing links, and this image
# ships the scons-built assert-enabled one instead.
# ---------------------------------------------------------------------------
ARG PXC_BUILD_TYPE=Debug
ARG PXC_OPT_FLAGS="-g -O2"
ARG PXC_PREFIX=/usr/local/pxc

RUN set -eux; \
    JOBS="${BUILD_JOBS:-$(nproc)}"; \
    mkdir -p /build && cd /build; \
    OPT_FLAGS="$PXC_OPT_FLAGS"; \
    if [ "$PXC_INSTRUMENT" = "1" ]; then \
        export CC=clang CXX=clang++; \
        EXTRA_C="-fsanitize-coverage=trace-pc-guard -g -I/opt/antithesis-sdk-cpp"; \
        EXTRA_LD="-Wl,--build-id"; \
    else \
        export CC=gcc CXX=g++; \
        EXTRA_C=""; \
        EXTRA_LD="-Wl,--build-id"; \
    fi; \
    cmake /src \
        -DCMAKE_BUILD_TYPE="$PXC_BUILD_TYPE" \
        -DCMAKE_C_FLAGS_DEBUG="$OPT_FLAGS $EXTRA_C" \
        -DCMAKE_CXX_FLAGS_DEBUG="$OPT_FLAGS $EXTRA_C" \
        -DCMAKE_C_FLAGS_RELWITHDEBINFO="$OPT_FLAGS $EXTRA_C" \
        -DCMAKE_CXX_FLAGS_RELWITHDEBINFO="$OPT_FLAGS $EXTRA_C" \
        -DCMAKE_EXE_LINKER_FLAGS="$EXTRA_LD" \
        -DCMAKE_SHARED_LINKER_FLAGS="$EXTRA_LD" \
        -DCMAKE_INSTALL_PREFIX="$PXC_PREFIX" \
        -DWITH_WSREP=ON \
        -DWITH_LTO=OFF \
        -DWITH_SSL=system \
        -DWITH_ZLIB=system \
        -DWITH_UNIT_TESTS=OFF \
        -DWITH_ROUTER=OFF \
        -DWITH_NDB=OFF \
        -DWITH_PAM=ON \
        -DWITH_AUTHENTICATION_LDAP=OFF \
        -DINSTALL_MYSQLTESTDIR= \
        -DFORCE_INSOURCE_BUILD=0 ; \
    if ! make -j"$JOBS" --output-sync=target; then \
        echo "=================================================================="; \
        echo "ANTITHESIS: parallel build failed. Re-running serially to surface"; \
        echo "the actual failing target — with -j${JOBS} the real error is"; \
        echo "interleaved with other targets and the top-level 'Error 2' says"; \
        echo "nothing. This is incremental, so it stops at the true failure."; \
        echo "=================================================================="; \
        make -j1 --output-sync=target; \
        exit 1; \
    fi; \
    make install

# Verify the server really came out debug-configured before anything
# downstream depends on it. A silently-NDEBUG mysqld would make this whole tier
# pointless while still appearing to work.
#
# Two checks, because they can fail independently: __assert_fail proves assert()
# is live, and _db_enter_ proves the DBUG machinery was actually linked in —
# the exact desync that broke three earlier builds.
RUN set -eux; \
    test -x "${PXC_PREFIX}/bin/mysqld"; \
    if ! nm -C "${PXC_PREFIX}/bin/mysqld" | grep -q '_db_enter_'; then \
        echo "ANTITHESIS: mysqld has no _db_enter_ — the DBUG library was not" \
             "linked in, so this is not a coherent debug-configured build." >&2; \
        exit 1; \
    fi; \
    echo "ANTITHESIS: mysqld has DBUG linked in"; \
    if ! nm -C "${PXC_PREFIX}/bin/mysqld" | grep -qi '__assert_fail'; then \
        echo "ANTITHESIS: mysqld has no __assert_fail reference — NDEBUG is" \
             "defined. Expected an assert-enabled build." >&2; \
        exit 1; \
    fi; \
    echo "ANTITHESIS: mysqld is assert-enabled"

# Install the scons-built assert-enabled provider. cmake no longer builds a
# competing copy (see patch-sources.sh patch 3), so this is the only
# libgalera_smm.so in the image. garbd goes alongside it for a future
# 2-node+arbitrator variant.
RUN set -eux; \
    install -m 0755 /src/percona-xtradb-cluster-galera/libgalera_smm.so \
        "${PXC_PREFIX}/lib/libgalera_smm.so"; \
    install -m 0755 /src/percona-xtradb-cluster-galera/garb/garbd \
        "${PXC_PREFIX}/bin/garbd"

# Verify the artifacts are actually symbolizable. /symbols in the runtime image
# is only useful if these are unstripped and carry a GNU build id — and LLVM
# symbolization specifically requires the build id. Nothing in this tree strips
# on install, but a missing build id would otherwise surface as unreadable
# stack traces in triage rather than as a build failure.
RUN set -eux; \
    for artifact in "${PXC_PREFIX}/bin/mysqld" "${PXC_PREFIX}/lib/libgalera_smm.so"; do \
        file -L "$artifact" | grep -q 'not stripped' \
            || { echo "ANTITHESIS: $artifact is stripped — /symbols would be useless" >&2; exit 1; }; \
        readelf -n "$artifact" | grep -qi 'Build ID' \
            || { echo "ANTITHESIS: $artifact has no GNU build id — symbolization will fail" >&2; exit 1; }; \
        echo "ANTITHESIS: $artifact is unstripped and has a build id"; \
    done

# Verify instrumentation when it was requested. Without this the image would
# build clean and silently produce no coverage at run time.
RUN set -eux; \
    if [ "$PXC_INSTRUMENT" = "1" ]; then \
        nm "${PXC_PREFIX}/bin/mysqld" | grep -q antithesis_load_libvoidstar \
            || { echo "ANTITHESIS: mysqld is NOT instrumented (no antithesis_load_libvoidstar)" >&2; exit 1; }; \
        echo "ANTITHESIS: mysqld instrumentation verified"; \
    else \
        echo "ANTITHESIS: PXC_INSTRUMENT=0 — pxc-node is intentionally uninstrumented"; \
    fi

# ---------------------------------------------------------------------------
# Percona XtraBackup 8.4, required by wsrep_sst_method=xtrabackup-v2.
#
# wsrep_sst_xtrabackup-v2.sh resolves PXB relative to its own location:
# "$(dirname $0)/pxc_extra/pxb-8.4/bin/xtrabackup", and enforces a minimum
# version of 8.4.0-5. The 8.3 / 8.0 sibling paths are only consulted for
# cross-version donors, which a homogeneous 8.4 cluster never has, so only
# pxb-8.4 is installed. Add the others if a rolling-upgrade variant lands.
# ---------------------------------------------------------------------------
ARG PXB_VERSION=8.4.0-5
ARG PXB_GLIBC=2.35

RUN set -eux; \
    PXB_TARBALL="percona-xtrabackup-${PXB_VERSION}-Linux-x86_64.glibc${PXB_GLIBC}.tar.gz"; \
    curl -fsSL -o /tmp/pxb.tar.gz \
        "https://downloads.percona.com/downloads/Percona-XtraBackup-8.4/Percona-XtraBackup-${PXB_VERSION}/binary/tarball/${PXB_TARBALL}"; \
    mkdir -p /tmp/pxb "${PXC_PREFIX}/bin/pxc_extra/pxb-8.4"; \
    tar -xzf /tmp/pxb.tar.gz -C /tmp/pxb --strip-components=1; \
    cp -r /tmp/pxb/bin "${PXC_PREFIX}/bin/pxc_extra/pxb-8.4/"; \
    cp -r /tmp/pxb/lib "${PXC_PREFIX}/bin/pxc_extra/pxb-8.4/"; \
    rm -rf /tmp/pxb /tmp/pxb.tar.gz; \
    test -x "${PXC_PREFIX}/bin/pxc_extra/pxb-8.4/bin/xtrabackup"

# ---------------------------------------------------------------------------
# Stage: pxc-node — runtime image for pxc-node1/2/3
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS pxc-node

ARG PXC_PREFIX=/usr/local/pxc

ENV DEBIAN_FRONTEND=noninteractive \
    NO_COLOR=1 \
    FORCE_COLOR=0 \
    TERM=dumb \
    PXC_PREFIX=/usr/local/pxc \
    PATH=/usr/local/pxc/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Runtime dependencies. socat is FATAL if missing — wsrep_sst_common.sh aborts
# the transfer without it. diffutils/pv/lsof/procps are likewise probed by the
# SST scripts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        socat \
        openssl \
        diffutils \
        pv \
        lsof \
        procps \
        psmisc \
        netcat-openbsd \
        gzip \
        tar \
        bash \
        coreutils \
        gawk \
        sed \
        grep \
        libstdc++6 \
        libgcc-s1 \
        libaio1 \
        libnuma1 \
        libssl3 \
        libncurses6 \
        libedit2 \
        liblz4-1 \
        libzstd1 \
        libtirpc3 \
        libsasl2-2 \
        libldap-2.5-0 \
        libkrb5-3 \
        libcurl4 \
        libev4 \
        zlib1g \
        libatomic1 \
        libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r mysql && useradd -r -g mysql -d /var/lib/mysql -s /sbin/nologin mysql

COPY --from=pxc-build ${PXC_PREFIX} ${PXC_PREFIX}

# Symbolization. The binaries are built with -g and --build-id and are NOT
# stripped, so a one-hop symlink into /symbols is all Antithesis needs.
# Populated even though v1 is uninstrumented: it costs nothing, makes core
# dumps and multiverse-debugger stack traces readable now, and removes a
# moving part from the PXC_INSTRUMENT=1 flip later.
#
# One hop only — Antithesis follows a single symlink per entry, so these must
# point at real files, never at another symlink.
RUN set -eux; \
    mkdir -p /symbols; \
    ln -sfn "${PXC_PREFIX}/bin/mysqld" /symbols/mysqld; \
    ln -sfn "${PXC_PREFIX}/lib/libgalera_smm.so" /symbols/libgalera_smm.so; \
    test -e /symbols/mysqld; \
    test -e /symbols/libgalera_smm.so

RUN set -eux; \
    mkdir -p /var/lib/mysql /var/log/mysql /var/run/mysqld \
             /opt/antithesis/pxc /opt/antithesis/state; \
    chown -R mysql:mysql /var/lib/mysql /var/log/mysql /var/run/mysqld /opt/antithesis/state

COPY antithesis/pxc-node/my.cnf /etc/my.cnf
COPY antithesis/pxc-node/entrypoint.sh /opt/antithesis/pxc/entrypoint.sh
COPY antithesis/pxc-node/notify.sh /opt/antithesis/pxc/notify.sh
COPY antithesis/setup-complete.sh /opt/antithesis/setup-complete.sh
RUN chmod 0755 /opt/antithesis/pxc/entrypoint.sh \
               /opt/antithesis/pxc/notify.sh \
               /opt/antithesis/setup-complete.sh

EXPOSE 3306 4444 4567 4568

ENTRYPOINT ["/opt/antithesis/pxc/entrypoint.sh"]

# ---------------------------------------------------------------------------
# Stage: workload — Python test driver
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS workload

ENV DEBIAN_FRONTEND=noninteractive \
    NO_COLOR=1 \
    FORCE_COLOR=0 \
    TERM=dumb \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/antithesis/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        python3-venv \
        python3-pip \
        bash \
        coreutils \
        procps \
    && rm -rf /var/lib/apt/lists/*

# A venv rather than --break-system-packages: bookworm's python3 is
# PEP 668-managed, and the venv keeps the SDK out of the system site-packages.
RUN python3 -m venv /opt/antithesis/venv

COPY antithesis/workload/requirements.txt /opt/antithesis/workload/requirements.txt
RUN /opt/antithesis/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/antithesis/venv/bin/pip install --no-cache-dir \
         -r /opt/antithesis/workload/requirements.txt

COPY antithesis/workload/ /opt/antithesis/workload/
COPY antithesis/setup-complete.sh /opt/antithesis/setup-complete.sh
RUN chmod 0755 /opt/antithesis/setup-complete.sh /opt/antithesis/workload/entrypoint.py

# Assertion cataloging. Antithesis recursively catalogs every .py under
# /opt/antithesis/catalog/, following ONE symlink per entry — so this points
# straight at the real workload directory, never at another symlink.
RUN set -eux; \
    mkdir -p /opt/antithesis; \
    ln -sfn /opt/antithesis/workload /opt/antithesis/catalog; \
    test -f /opt/antithesis/catalog/entrypoint.py

# Where antithesis-workload will drop test templates. Created here so the
# path and ownership already exist; this skill defines no test commands.
RUN mkdir -p /opt/antithesis/test/v1

ENTRYPOINT ["/opt/antithesis/venv/bin/python3", "/opt/antithesis/workload/entrypoint.py"]
