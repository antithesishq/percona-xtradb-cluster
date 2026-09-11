# sst-ready-message-implies-listener

**Focus:** Protocol contracts — the SST line-oriented control-pipe protocol between mysqld
and the SST shell scripts.
**Confidence:** High (script and server sides both read directly at commit f9ecb3e).

## Claimed contract

The SST control channel is a line-oriented pipe protocol over the script's stdout:

- **Joiner side** (`sql/wsrep_sst.cc` sst_joiner_thread :599): line 1 = `ready <addr>` —
  the promise that a transfer listener is bound and the address can be sent to the donor
  in the state-transfer request; line 2 = `<uuid>:<seqno>` on completion.
- **Donor side** (`sql/wsrep_sst.cc:1471-1500`): control words `flush tables` (take FTWRL +
  disallow InnoDB writes), `continue` (release), `done <uuid>:<seqno>` (SST finished).

## Verified contract violations

1. **False "ready".** `wait_for_listen` in `scripts/wsrep_sst_xtrabackup-v2.sh:1242-1318`:
   the detection loop runs `for i in {1..300}` with `sleep 0.2` (60s); if no bound
   socat/nc listener is ever found, the loop simply exhausts and **falls through to
   `echo "ready ${host}:${port}/..."` unconditionally at :1317, `return 0`**. mysqld
   forwards the address to the donor; the donor connects to nothing and enters its 30×
   retry loop; the joiner's mysqld waits unbounded (`while(!sst_received_)`
   replicator_str.cpp:1240; `my_fgets` wsrep_sst.cc:634/:694 has no timeout). The
   detection loop is additionally backgrounded *before* socat launches (:2207 vs :2224),
   so the race is structural.
2. **Unauthenticated, unframed control words.** The donor reader (`wsrep_sst.cc:1476-1499`)
   matches raw lines case-insensitively; `done` is a prefix match (:1495).
   **RESCOPED (investigation):** the torn-snapshot arm requires `locked == true`, i.e. a
   prior "flush tables" line — and "flush tables" is emitted ONLY by `wsrep_sst_rsync.sh:248`.
   The shipped default `wsrep_sst_allowed_methods = "xtrabackup-v2,clone"`
   (`wsrep_sst.cc:88`) makes the donor reject rsync requests, so in the default config
   `locked` can never become true and a spurious `continue` is a no-op
   (`if (locked)`, :1487-1493). The freeze-release/torn-snapshot scenario is reachable only
   when an operator adds rsync to the allowlist. The unframed-protocol weakness itself is
   real regardless (violation 3).
3. **Unknown line terminates the protocol.** The `else` branch (:1498-1500) only logs
   "Received unknown signal" and then falls out of the read loop entirely (no
   `goto wait_signal`) — a single stray stdout line ends the donor control session; the
   code then reports SST over with whatever uuid/seqno state it has. The "SST script died"
   diagnostic is `#if 0`'d (:1504-1508).

## Failure scenario

Antithesis delays/blocks the joiner's port-4444 bind (network faults, slow container) past
60s → false "ready" → donor streams to nothing / retries → both sides in unbounded waits →
joiner never reaches Synced, donor pinned in DONOR state; no error surfaces anywhere.
Alternatively (rsync-enabled configs only — see rescope note under violation 2) a
fault-injected subprocess message on the donor pipe releases the write freeze mid-snapshot →
the joiner receives an inconsistent backup and joins with divergent data (silent — voting
cannot see it).

## Suggested assertions (all missing)

- **Always (SUT-side, joiner script or a thin wrapper):** the `ready` line is emitted only
  after the listener socket is verified bound (the loop's `break 2` path). Practical form:
  patch the script to `safe_exit 32` on loop exhaustion, or assert via SDK in
  sst_joiner_thread: "ready address is connectable" (mysqld can attempt a probe connect
  before sending the state-transfer request). Message: "SST ready message implies bound
  listener".
- **Always (workload-side, liveness bound):** every SST attempt terminates within T
  (joiner reaches `wsrep_local_state=4`/Synced, or the joiner process exits with an error)
  — no silent >T both-sides-waiting state. T must exceed the scripted timeouts (100s
  joiner-initial + 120s idle watchdog); suggest T = 10 minutes of wall progress.
- **Sometimes (workload-side):** "SST completed successfully under active network faults" —
  ensures the property isn't vacuously green because SST never ran.
- **Unreachable (SUT-side, donor reader wsrep_sst.cc:1499):** "unknown control word on SST
  pipe" — the protocol's alphabet is closed; any other line means a subprocess is leaking
  into the control channel (precondition of violation 2).

## Assertion-type rationale

The ready→listener implication is a per-occurrence contract (`Always`). Termination-within-
bound is the externally checkable liveness consequence (`Always` over a timer in the
workload). The unknown-word check is `Unreachable` because the alphabet is fixed and any
hit is a protocol-integrity failure regardless of workload.

## Fault requirements

SST is most naturally triggered by node restart with a gcache-exceeding gap or a wiped
datadir — **node termination/restart faults materially strengthen this property; flag as
often-disabled**. Without termination faults, force SSTs via the workload (e.g.
`wsrep_provider_options='gcache.size=...'` small + long partitions, or deliberate
`grastate.dat` removal by a harness helper between restarts if available).

## Open questions

None — both resolved (see Investigation Log). Property scope CHANGED: violation 2 (torn
snapshot via spurious `continue`) is config-gated — unreachable under the default
`wsrep_sst_allowed_methods = "xtrabackup-v2,clone"` because only the rsync script ever sends
"flush tables". The false-ready arm (violation 1) and the unknown-word protocol closure
(violation 3) are unchanged and remain the core of the property. The liveness bound is now
concrete: false-ready hangs are bounded at ~100s (stage 1) / ~120s (bulk stall) script-side;
the suggested workload T = 10 min stays comfortably above both. Caveat kept: an orphaned SST
child holding the script's stdout pipe open (no PDEATHSIG) can still defeat the EOF that
bounds mysqld's `my_fgets` wait — the orphan-tree arm of
`failed-state-transfer-node-rejoins`.

### Investigation Log

#### After a false "ready", which scripted timeout bounds the hang?

- Examined: `scripts/wsrep_sst_common.sh:1188-1193` (WSREP_SST_DONOR_TIMEOUT 10 /
  WSREP_SST_JOINER_TIMEOUT = joiner-timeout|sst-initial-timeout default 100 /
  WSREP_SST_IDLE_TIMEOUT 120), `scripts/wsrep_sst_xtrabackup-v2.sh:793` (stimeout), `:2224`
  (stage-1 `recv_data_from_donor_to_joiner $STATDIR ... $stimeout -2` runs under
  `interruptable_timeout` → RC 124), `:1376-1395` (RC 124 → "Possible timeout in receving
  first data from donor" → `safe_exit 32`), `:2385` (bulk stage timeout 0), `:2426` +
  `:340-391` (`monitor_sst_progress` — du-based no-progress watchdog, sst-idle-timeout 120s,
  SIGKILL → exit 137 → `safe_exit 32`).
- Found: the false-ready hang is bounded twice: stage 1 (metadata/sst-info receive, which
  begins right after "ready" is forwarded) aborts at 100s; the bulk stage aborts after 120s
  without byte progress. Either way the script exits, mysqld's `my_fgets` gets EOF, and the
  joiner takes the sst_received error path (abort/restart). Stage-1 failures occur BEFORE the
  no-way-back point (SAFE_EXIT_CODE_OVERRIDE still armed), so the datadir/grastate are
  untouched.
- Not found: any unbounded joiner-script wait, given the script's own children die with it.
- Conclusion: RESOLVED — violation degrades from permanent hang to a bounded stall + abort;
  T set accordingly (bullet above), orphan caveat noted.

#### Is there a legitimate stdout producer between "flush tables" and "done" in the xtrabackup-v2 flow?

- Examined: grep for control words across `scripts/wsrep_sst_*.sh` ("flush tables" only in
  `wsrep_sst_rsync.sh:248`); `wsrep_sst_xtrabackup-v2.sh:1990-2160` (donor flow: xtrabackup
  output → `innobackup.backup.log`, script logging → stderr via `wsrep_log()`
  `wsrep_sst_common.sh:286-289`; stdout lines are exactly `continue` (IST-bypass branch,
  :2145) and `done <gtid>` (:2159)); `sql/wsrep_sst.cc:88` (default allowlist
  `xtrabackup-v2,clone`), `:1643-1683` (donor method gate), `:1471-1500` (donor reader —
  spurious `continue` is a no-op while `locked == false`).
- Found: no legitimate stdout producer between the metadata phase and "done"; the
  xtrabackup-v2 flow never emits "flush tables" at all, so the server-side freeze state is
  never entered under default config.
- Conclusion: RESOLVED — the `Unreachable` at the unknown-word branch (:1499) is safe to
  add; additionally an `Unreachable` on the "flush tables" match itself is valid under the
  default method allowlist. Torn-snapshot arm rescoped to rsync-enabled configs.

## Synthesis refinement (2026-09-10)

Keep-as-message: the v1 remainder duplicates restarted-node-rejoins-synced's bounded-completion timeout — fold the check into that watchdog; this file stays as documentation of the control-channel hazard. Bound re-anchored to ~220s (script-side ~100s initial + ~120s idle-stall), pinned after the calibration run; the 10-min figure is superseded.
