#!/usr/bin/env bash
#
# Bring up the PXC harness locally, run every test command the way Antithesis
# would, and write ONE self-contained log for an agent to triage.
#
# Why one file: the previous local run lost the two things that mattered most.
# `docker compose exec` writes to your terminal and NOT to the attached compose
# log, so neither the test commands' stdout nor their exit codes survived --
# and the exit code of first_seed_workload_schema is the entire signal for
# whether the schema actually seeded. Everything here lands in one place with
# fixed section markers and an explicit rc for every command.
#
# Container output is FOLLOWED into the same file as it happens rather than
# dumped in a batch at the end, so a node error appears next to the command
# that provoked it -- including inside that command's block. See "HOW TO READ
# THIS FILE" at the top of the log for the one filter that separates them.
#
# Usage:
#   ./antithesis/local-validate.sh [--build] [--keep] [--rounds N] [--out FILE]
#
#   --build      rebuild images first (the pxc-node stage compiles PXC and
#                galera from source and is very slow; omit to reuse what you
#                already have)
#   --keep       skip the initial `down -v` and reuse the running cluster
#   --rounds N   driver/probe rounds to run (default 3)
#   --out FILE   log path (default ./local-validate-<utc stamp>.log)
#
# Exits non-zero if any test command did.

set -uo pipefail

HELP_LINES='2,28p'

# --------------------------------------------------------------------------
# Arguments. Parsed before anything is printed, because --out decides where
# the output goes.
# --------------------------------------------------------------------------

DO_BUILD=0
DO_KEEP=0
ROUNDS=3
OUT=""
INVOKED_FROM="$PWD"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build)  DO_BUILD=1; shift ;;
        --keep)   DO_KEEP=1; shift ;;
        --rounds) ROUNDS="${2:?--rounds needs a number}"; shift 2 ;;
        --out)    OUT="${2:?--out needs a path}"; shift 2 ;;
        -h|--help) sed -n "$HELP_LINES" "$0" | sed 's/^#\( \|$\)//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ "$ROUNDS" =~ ^[0-9]+$ ]] || { echo "--rounds must be a number" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"          # percona-xtradb-cluster/ -- the build context
COMPOSE_FILE="antithesis/config/docker-compose.yaml"

[[ -n "$OUT" ]] || OUT="$INVOKED_FROM/local-validate-$(date -u +%Y%m%dT%H%M%SZ).log"
mkdir -p "$(dirname "$OUT")" 2>/dev/null
: > "$OUT" || { echo "cannot write $OUT" >&2; exit 1; }
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"

cd "$ROOT" || exit 1

# Output goes through process substitution rather than `main | tee`. With a
# pipeline, main() runs in a subshell and the top-level shell blocks waiting on
# it -- and bash defers trap handling until a foreground job finishes, so a
# `kill <script pid>` was measured to orphan the log follower permanently.
# Running main() in this shell keeps the traps live. The cost is that the
# process-substitution child is not waited for automatically; finish() handles
# that by closing the fds so tee sees EOF and flushes.
exec > >(tee -a "$OUT") 2>&1

# Pick a compose implementation, and the engine binary that goes with it.
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose -f "$COMPOSE_FILE"); ENGINE=docker
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose -f "$COMPOSE_FILE"); ENGINE=docker
elif podman compose version >/dev/null 2>&1; then
    COMPOSE=(podman compose -f "$COMPOSE_FILE"); ENGINE=podman
else
    echo "no docker compose / docker-compose / podman compose on PATH" >&2
    exit 1
fi

TESTDIR=/opt/antithesis/test/v1/pxc
SDKDIR=/tmp/sdk                    # one assertion stream per invocation
JOURNAL=/opt/antithesis/journal/pxc.sqlite3
PY=/opt/antithesis/venv/bin/python3

declare -a RESULTS=()
FAILED=0

# Reaping the follower on every exit path. Ctrl-C at a terminal signals the
# whole foreground process group and reaches the follower directly (it is a
# background job of a non-interactive shell, so it shares that group). A
# targeted `kill <script pid>` -- a timeout wrapper, CI, another shell -- does
# not, and that is what these traps are for. An untrapped SIGINT kills bash
# without running EXIT traps, so INT and TERM are named explicitly.
LOGGER_PID=""

reap_follower() {
    [[ -n "$LOGGER_PID" ]] && kill "$LOGGER_PID" 2>/dev/null
    LOGGER_PID=""
    return 0
}

finish() {
    reap_follower
    # Closing the fds makes tee see EOF and flush. Without it the tail of the
    # summary can be lost when the shell exits out from under it.
    exec 1>&- 2>&-
    sleep 1
}
trap 'finish' EXIT
trap 'finish; exit 130' INT TERM

# --------------------------------------------------------------------------
# Output helpers. The section marker format is fixed on purpose: an agent
# should be able to slice this file with `grep '^===== '` and nothing else.
# --------------------------------------------------------------------------

section() { printf '\n\n===== [%s] %s =====\n' "$(date -u +%H:%M:%SZ)" "$*"; }
note()    { printf -- '--- %s\n' "$*"; }

# Run a command, log it, record its rc. Never aborts the script: a non-zero
# test command is a result to capture, not a reason to stop collecting.
#
# `step` counts toward the overall exit status; `diag` does not. The split
# matters -- an empty assertion stream or an absent journal file makes a
# diagnostic exit non-zero, and without the split the whole run would report
# failure for something that is not one.
_run() {
    local label="$1" counts="$2"; shift 2
    # Markers carry a timestamp because container output streams into this file
    # concurrently: when a node line lands inside a block, the timestamps are
    # what let you tell whether it really happened during the command.
    printf '\n----- BEGIN %s [%s] -----\n' "$label" "$(date -u +%H:%M:%SZ)"
    # Echoed as one truncated line: several of these carry an embedded Python
    # program, and printing it here as well as running it doubles the noise in
    # a file whose whole purpose is to be read. Newlines are folded first --
    # truncating to N characters is useless when the first N span ten lines.
    local shown="${*//$'\n'/ }"
    if [[ ${#shown} -gt 180 ]]; then
        printf '$ %.180s [...]\n' "$shown"
    else
        printf '$ %s\n' "$shown"
    fi
    "$@"
    local rc=$?
    printf -- '----- END %s rc=%d [%s] -----\n' "$label" "$rc" "$(date -u +%H:%M:%SZ)"
    RESULTS+=("$(printf '%-40s rc=%-3d%s' "$label" "$rc" \
                 "$( [[ $counts -eq 0 ]] && echo '  (diagnostic)' )")")
    if [[ $rc -ne 0 && $counts -eq 1 ]]; then FAILED=1; fi
    return $rc
}
step() { _run "$1" 1 "${@:2}"; }
diag() { _run "$1" 0 "${@:2}"; }

wl()  { "${COMPOSE[@]}" exec -T pxc-workload "$@"; }
node(){ "${COMPOSE[@]}" exec -T "$1" sh -c "$2"; }

# --------------------------------------------------------------------------
# Container log follower.
#
# Rather than dumping every container's output in one batch at the end, follow
# it into the same file the command blocks go to, so a node error lands next to
# the command that provoked it. Both writers hold the file O_APPEND, and on
# Linux the seek-and-write is atomic per write() for a regular file, so lines
# interleave without ever tearing into each other.
#
# The cost is that a command block is no longer only that command's output.
# That is recoverable in one filter, because every container line carries a
# `<service> |` prefix the workload's own output never produces:
#
#   sed -n '/^----- BEGIN cmd:seed /,/^----- END cmd:seed /p' LOG \
#     | grep -vE "$CONTAINER_RE"
#
# --tail=all is explicit rather than relied upon: the follower starts just
# after `up -d`, and anything already emitted has to be replayed or the boot
# sequence is lost.
# --------------------------------------------------------------------------

CONTAINER_RE='^[A-Za-z0-9_.-]+[[:space:]]*\|[[:space:]]'

start_follower() {
    "${COMPOSE[@]}" logs --no-color --timestamps --tail=all -f >> "$OUT" 2>&1 &
    LOGGER_PID=$!
}

stop_follower() {
    [[ -n "$LOGGER_PID" ]] || return 0
    kill "$LOGGER_PID" 2>/dev/null
    wait "$LOGGER_PID" 2>/dev/null
    LOGGER_PID=""
    # Give the last forwarded lines a moment to land before anything reads the
    # file, so the census does not miss the tail of the run.
    sleep 1
}

test_cmd() {
    local name="$1" tag="${2:-$1}"
    step "cmd:$tag" "${COMPOSE[@]}" exec -T \
        -e "ANTITHESIS_SDK_LOCAL_OUTPUT=$SDKDIR/$tag.jsonl" \
        pxc-workload "$TESTDIR/$name"
}

MYSQL="/usr/local/pxc/bin/mysql -uroot --protocol=socket --socket=/var/lib/mysql/mysql.sock"

# --------------------------------------------------------------------------

main() {

section "run metadata"

echo "started:   $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "host:      $(uname -srm)"
echo "compose:   ${COMPOSE[*]}"
echo "engine:    $ENGINE"
echo "root:      $ROOT"
echo "log:       $OUT"
echo "rounds:    $ROUNDS   build: $DO_BUILD   keep: $DO_KEEP"
"${COMPOSE[@]}" version 2>&1 | head -3
note "source revision"
git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo "(not a git checkout)"
git -C "$ROOT" status --porcelain 2>/dev/null | head -40

note "HOW TO READ THIS FILE"
cat <<'LAYOUT'
  Sections    lines matching  ^===== [hh:mm:ssZ] name =====
  Commands    ^----- BEGIN <label> [ts] -----  ..  ^----- END <label> rc=N [ts] -----
              Labels prefixed "cmd:" are test commands and set this script's
              exit status. The rest are diagnostics and do not.
  Rounds      ^----- BEGIN round:N/M [ts] -----  ..  ^----- END round:N/M ...
              The driver and probe run concurrently inside a round. Their own
              stdout is buffered and replayed at the END of the round; the
              container output in between is live.
  Container   lines matching  ^<service> | <rfc3339> ...
  output      Streamed in as it happens, so it DOES appear inside command
              blocks. That is deliberate -- it is how you see which command
              provoked a node error.

  To recover one command's own output, drop the container lines. Note the
  ^----- anchors: without them the range below also matches these very
  instructions, because this text contains the marker words too.

      sed -n '/^----- BEGIN cmd:seed /,/^----- END cmd:seed /p' LOG \
        | grep -vE '^[A-Za-z0-9_.-]+[[:space:]]*\|[[:space:]]'
LAYOUT

section "bring up the cluster"

if [[ $DO_KEEP -eq 0 ]]; then
    diag "compose:down" "${COMPOSE[@]}" down -v --remove-orphans
else
    note "--keep: reusing whatever is already running"
fi

if [[ $DO_BUILD -eq 1 ]]; then
    note "building; the pxc-node stage compiles PXC + galera from source"
    if ! step "compose:build" "${COMPOSE[@]}" build; then
        section "ABORT: build failed"
        return 1
    fi
fi

if ! step "compose:up" "${COMPOSE[@]}" up -d; then
    section "ABORT: up failed"
    "${COMPOSE[@]}" logs --no-color --timestamps
    return 1
fi

# From here on every container line streams into this file as it happens.
# Kill the follower on any exit path, including an interrupt, so it cannot
# outlive the script and keep appending to a file someone is already reading.
start_follower
note "following container output into this file from here on"
note "container lines match: $CONTAINER_RE"

section "wait for readiness"

# The workload entrypoint emits setup_complete and then idles forever, so that
# line -- not container health alone -- is the real "cluster is usable" signal.
# A cold start initializes node1's datadir, then SSTs node2, then node3.
local deadline=$(( $(date +%s) + 1800 ))
local ready=0
while [[ $(date +%s) -lt $deadline ]]; do
    if "${COMPOSE[@]}" logs --no-color pxc-workload 2>/dev/null \
         | grep -q 'setup_complete emitted'; then
        ready=1
        break
    fi
    # Bail out rather than burn thirty minutes if something died on the way up.
    local svc cid
    for svc in pxc-node1 pxc-node2 pxc-node3 pxc-workload; do
        cid="$("${COMPOSE[@]}" ps -q "$svc" 2>/dev/null | head -1)"
        if [[ -n "$cid" ]]; then
            if [[ "$("$ENGINE" inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)" == "exited" ]]; then
                section "ABORT: $svc exited during startup"
                stop_follower
                "${COMPOSE[@]}" ps
                return 1
            fi
        fi
    done
    sleep 5
done

"${COMPOSE[@]}" ps

if [[ $ready -ne 1 ]]; then
    section "ABORT: setup_complete never appeared within 30 minutes"
    stop_follower
    return 1
fi
note "setup_complete observed"

section "reset per-timeline state"

# Without this a second run tests nothing: first_seed_workload_schema returns 0
# immediately once a swarm profile exists (a timeline gets exactly one
# personality), so the seed path would be skipped entirely.
diag "journal:reset" wl rm -f "$JOURNAL"
diag "sdkdir:reset"  wl sh -c "rm -rf $SDKDIR && mkdir -p $SDKDIR"

section "test commands"

note "rc=1 from the seed means a live server rejected one of our own schema"
note "statements -- a harness bug. rc=0 means the schema really seeded."
test_cmd first_seed_workload_schema seed

note "second run: a timeline gets one swarm profile, so this must return 0"
note "without touching the schema or reopening the PK-less fence"
test_cmd first_seed_workload_schema seed-rerun

local i t_out p_out tpid ppid pair lbl f rc b_ts
for i in $(seq 1 "$ROUNDS"); do
    # A round marker, not just per-command markers. These two commands run
    # concurrently, so their output has to be buffered and replayed -- which
    # means their own blocks land at the END of the window they describe.
    # Wrapping the window keeps the container output that streamed in during
    # those 90-odd seconds inside the thing it belongs to. Without this the
    # first real run put ~1290 container lines ABOVE a three-line traffic
    # block that had been running the whole time.
    printf '\n----- BEGIN round:%s/%s [%s] -----\n' "$i" "$ROUNDS" "$(date -u +%H:%M:%SZ)"
    note "driver and probe running concurrently; container output below is live"
    note "their own stdout is buffered and replayed at the end of this round"
    t_out="$(mktemp)"; p_out="$(mktemp)"; b_ts="$(date -u +%H:%M:%SZ)"
    ( "${COMPOSE[@]}" exec -T -e "ANTITHESIS_SDK_LOCAL_OUTPUT=$SDKDIR/traffic-$i.jsonl" \
        pxc-workload "$TESTDIR/parallel_driver_traffic" >"$t_out" 2>&1
      echo $? >"$t_out.rc" ) &
    tpid=$!
    ( "${COMPOSE[@]}" exec -T -e "ANTITHESIS_SDK_LOCAL_OUTPUT=$SDKDIR/probe-$i.jsonl" \
        pxc-workload "$TESTDIR/anytime_cluster_probe" >"$p_out" 2>&1
      echo $? >"$p_out.rc" ) &
    ppid=$!
    wait $tpid $ppid

    # Replayed sequentially: two concurrent writers into one log is unreadable.
    # Same marker shape as _run so one filter works on every command block.
    for pair in "traffic-$i:$t_out" "probe-$i:$p_out"; do
        lbl="${pair%%:*}"; f="${pair#*:}"
        rc="$(cat "$f.rc" 2>/dev/null || echo '?')"
        printf '\n----- BEGIN cmd:%s [%s] -----\n' "$lbl" "$b_ts"
        cat "$f"
        printf -- '----- END cmd:%s rc=%s [%s] -----\n' "$lbl" "$rc" "$(date -u +%H:%M:%SZ)"
        RESULTS+=("$(printf '%-40s rc=%s' "cmd:$lbl" "$rc")")
        if [[ "$rc" != "0" ]]; then FAILED=1; fi
        rm -f "$f" "$f.rc"
    done
    printf -- '----- END round:%s/%s [%s] -----\n' "$i" "$ROUNDS" "$(date -u +%H:%M:%SZ)"
done

note "terminal oracles: finally_ assumes the drivers finished on their own,"
note "eventually_ assumes they were killed. Both legs are exercised."
test_cmd finally_verify_convergence    finally
test_cmd eventually_verify_convergence eventually

section "assertions that fired"

# The vacuity check, and the most useful thing in this file. Run
# a359f1f8-63-0 was green precisely because these were absent: not one
# ops/ddl/levers assertion fired anywhere, and the terminal oracles passed
# over empty tables and an empty journal.
diag "sdk:raw" wl sh -c \
    "set -- $SDKDIR/*.jsonl; [ -e \"\$1\" ] || { echo '(no assertion streams were written)'; exit 0; }; for f in \"\$@\"; do echo \"### \$f\"; cat \"\$f\"; done"

note "assertions that were evaluated, and any that came out false"
note "Coverage is NOT the point here: an assertion missing from this list"
note "almost always means this timeline's swarm draw never reached it."
note "Antithesis is what exercises the rest. What matters locally is that"
note "nothing FAILED and that the terminal vacuity guard appears."
diag "sdk:summary" wl "$PY" -c "
import glob, json, collections
hits, fails = collections.Counter(), collections.Counter()
def walk(o):
    if isinstance(o, dict):
        if isinstance(o.get('message'), str):
            yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)
files = sorted(glob.glob('$SDKDIR/*.jsonl'))
for path in files:
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            for a in walk(ev):
                # hit=false records are catalog pre-registration, not an
                # evaluation. Counting them as failures reads every unreached
                # reach claim as a red.
                if a.get('hit') is False:
                    continue
                hits[a['message']] += 1
                if a.get('condition') is False:
                    fails[a['message']] += 1
print('streams: %d   assertions evaluated: %d' % (len(files), len(hits)))
if not hits:
    print()
    print('(nothing parsed -- see sdk:raw above. If the streams are empty this')
    print(' SDK build may not honour ANTITHESIS_SDK_LOCAL_OUTPUT; the journal')
    print(' section below is then the fallback evidence that traffic ran.)')
print()
print('## FAILED (evaluated false at least once)')
if not fails:
    print('  (none)')
for msg, n in sorted(fails.items()):
    print('  %6d/%-6d %s' % (n, hits[msg], msg))
print()
print('## EVALUATED')
for msg, n in sorted(hits.items()):
    print('  %6d  %s' % (n, msg))
"

section "journal state"

# ddl.py says it plainly: a broken generator \"would present as thousands of
# identical errors in the outcome tally rather than as a finding\". So the
# outcome table is the first thing to read, every time.
diag "journal:dump" wl "$PY" -c "
import os, sqlite3
if not os.path.exists('$JOURNAL'):
    # Meaningful, not an error: no command ever opened a journal, so no
    # driver ran. That is the shape run a359f1f8-63-0 had.
    raise SystemExit('NO JOURNAL AT $JOURNAL -- no test command ever wrote one')
c = sqlite3.connect('$JOURNAL')
c.row_factory = sqlite3.Row
def show(title, sql):
    print('### ' + title)
    try:
        rows = c.execute(sql).fetchall()
    except Exception as e:
        print('  (' + str(e) + ')')
        print()
        return
    if not rows:
        print('  (empty)')
    for r in rows:
        print('  ' + ' | '.join('%s=%s' % (k, r[k]) for k in r.keys()))
    print()
show('swarm profile',     'SELECT k, v FROM swarm ORDER BY k')
show('observed',          'SELECT k, v FROM observed ORDER BY k')
show('outcome tally',     'SELECT shape, errno, n FROM outcome ORDER BY n DESC, shape')
show('ddl by state',      'SELECT state, errno, COUNT(*) n FROM ddl GROUP BY state, errno ORDER BY n DESC')
show('ddl statements',    'SELECT id, state, errno, stmt FROM ddl ORDER BY id')
show('ack by state',      'SELECT target, shape, state, errno, COUNT(*) n FROM ack GROUP BY target, shape, state, errno ORDER BY n DESC')
show('incr by state',     'SELECT state, COUNT(*) n, SUM(delta) total FROM incr GROUP BY state')
show('invocations',       'SELECT kind, COUNT(*) n FROM invocation GROUP BY kind')
show('progress',          'SELECT * FROM progress')
show('LEAKED leases',     'SELECT * FROM lease')
show('LEAKED disruption', 'SELECT * FROM disruption_token')
"

section "cluster state after the run"

local n
for n in pxc-node1 pxc-node2 pxc-node3; do
    note "$n"
    node "$n" "$MYSQL -N -B -e \"
      SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status
       WHERE VARIABLE_NAME IN ('wsrep_ready','wsrep_local_state_comment',
             'wsrep_cluster_status','wsrep_cluster_size','wsrep_local_state_uuid',
             'wsrep_last_committed','wsrep_local_recv_queue');
      SELECT 'pxc_strict_mode', @@global.pxc_strict_mode;
      SELECT 'sql_require_primary_key', @@global.sql_require_primary_key;
      SELECT 'tables', GROUP_CONCAT(TABLE_NAME ORDER BY TABLE_NAME)
        FROM information_schema.tables WHERE TABLE_SCHEMA='antithesis';\"" 2>&1
done

note "pxc_strict_mode must read ENFORCING and sql_require_primary_key ON above."
note "Anything else means a fence leaked and the rest of the run is suspect."

note "wl_nopk must exist on all three: creating it is the fence this harness"
note "got wrong in run a359f1f8-63-0, and nothing else proves the fence works"
for n in pxc-node1 pxc-node2 pxc-node3; do
    printf '%s wl_nopk: ' "$n"
    node "$n" "$MYSQL -N -B -e \"SHOW TABLES FROM antithesis LIKE 'wl_nopk'\"" 2>&1 \
        | tr '\n' ' '
    echo
done

section "server error census"

# The follower stops first: everything below reads container output, and it has
# to stop growing before it can be counted.
stop_follower
note "container log follower stopped; all container output above is inline"

# Read a SNAPSHOT, never $OUT directly. This section's own output is appended
# to $OUT through tee while these greps run, so grepping the live file would
# let the census match its own results and count them again.
local snap
snap="$(mktemp)"
cp "$OUT" "$snap"

# Scope every count to container lines. Script output in this same file quotes
# error codes too, and counting those would inflate every number here.
local clog
clog="$(mktemp)"
grep -E "$CONTAINER_RE" "$snap" > "$clog"
note "container lines captured: $(wc -l < "$clog")"

note "CRASHES: asserts, fatal signals, and non-zero mysqld exits"
note "anything here means a node died unexpectedly -- the headline finding"
grep -cE 'Assertion .* failed|got signal [0-9]|"signal":[1-9]|"status":[1-9][0-9]*,"kind"' "$clog"
grep -E 'Assertion .* failed|got signal [0-9]|"signal":[1-9]|"status":[1-9][0-9]*,"kind"' "$clog" \
    | head -40 | sed 's/^/    /'

# Split out on purpose. The first version of this census counted every
# mysqld_exited as a crash and reported six of them as the headline, when all
# six were the graceful_shutdown lever doing its job: status 0, signal 0,
# kind "graceful". A census that cries wolf on a working lever is worse than
# no census.
note "graceful shutdowns (the graceful_shutdown lever working; NOT crashes)"
grep -cE '"status":0,"kind":"graceful"' "$clog"

note "replica apply error codes. A repeating code is usually a generator bug,"
note "not a PXC bug -- and each one also costs a cluster-wide inconsistency vote."
grep -oE "Error_code: MY-[0-9]+" "$clog" | sort | uniq -c | sort -rn

note "one sample statement per distinct error code"
grep -oE "Error_code: MY-[0-9]+" "$clog" | sort -u | while read -r _ code; do
    echo "## $code"
    # Indented on purpose: unindented this would match CONTAINER_RE and a
    # second pass over the finished file would count the census's own
    # samples as container output.
    grep -m1 "Error_code: $code" "$clog" | cut -c1-500 | sed 's/^/    /'
done

note "inconsistency voting rounds"
grep -c "initiates vote on" "$clog"

# Also split out. "Received NON-PRIMARY" was in this pattern and produced six
# false evictions -- it is what every node logs on the way out of the primary
# component during an ordinary graceful shutdown.
note "evictions / forced SST after inconsistency (should be 0)"
grep -cE "inconsistent with group|Evicting|force_sst_after_inconsistency = yes" "$clog"
note "primary-component departures (normal during shutdown/rejoin, informational)"
grep -c "Received NON-PRIMARY" "$clog"

rm -f "$snap" "$clog"


section "summary"

if [[ ${#RESULTS[@]} -gt 0 ]]; then printf '%s\n' "${RESULTS[@]}"; fi
echo
echo "finished:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "log:       $OUT"
if [[ $FAILED -eq 0 ]]; then
    echo "overall:   every command exited 0"
else
    echo "overall:   AT LEAST ONE COMMAND EXITED NON-ZERO"
fi
echo
echo "The cluster is still up. Tear it down with:"
echo "  ${COMPOSE[*]} down -v"

return $FAILED
}

main "$@"
rc=$?

echo
echo "wrote $OUT"
exit "$rc"
