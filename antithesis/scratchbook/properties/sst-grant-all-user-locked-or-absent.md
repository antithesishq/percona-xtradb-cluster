---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# sst-grant-all-user-locked-or-absent — The SST post-processing GRANT-ALL account is never live outside the post-processing window

**Type:** Safety
**Confidence:** High for the mechanism (script read directly; all kill-window error paths
located); Medium for real-world exploitability (password is random and discarded — the
violation is primarily a policy/attack-surface invariant plus a propagation defect, see
Observations).

## What led here

Every SST joiner runs a hidden second mysqld on the freshly-transferred datadir for
post-processing (upgrade / RESET REPLICA), driven by
`run_post_processing_steps` (scripts/wsrep_sst_common.sh:655-998). To operate that instance
it creates a **superuser**: `'mysql.pxc.sst.user'@localhost` with `GRANT ALL ON *.*`
(:876-880, via `--init-file`, with a random password :865). The account is **never dropped**
— at the end it is only `ACCOUNT LOCK`ed in the same statement as `SHUTDOWN` (:963-972,
comment at :959-961: "LOCK the account to ensure that the user can't be used pass this
point if the server fails to shutdown properly (or is killed)"). The vendor's own comment
states the intended invariant; the implementation leaves multiple windows where it fails.

## Code paths and violation windows (validated at f9ecb3e)

Account lifecycle inside one SST:
1. `DROP USER IF EXISTS` + `CREATE USER` + `GRANT ALL ON *.*` via init-file at temp-mysqld
   start (:875-881). Account is now LIVE (unlocked, all privileges, localhost).
2. Post-processing SQL runs authenticated as that account (:886-955).
3. `ALTER USER ... ACCOUNT LOCK; SHUTDOWN;` (:967).

Windows that end the script between (1) and (3) leaving the account live in mysql.user of
the node's permanent datadir:
- `wait_for_mysqld_startup` failure → `return $errcode` (:886-890) — account already created
  by init-file, never locked.
- `SHOW REPLICA STATUS` client failure → `kill -9 $mysql_pid; return 3` (:912-921) — kill -9
  precedes any lock.
- `RESET REPLICA ALL` failure → `kill -9 $mysql_pid; return 3` (:943-952) — same.
- Shutdown-statement failure → `sleep 3; kill -9 $mysql_pid; return 3` (:973-985) — whether
  the ALTER inside the failed multi-statement took effect is indeterminate.
- The whole script killed externally: mysqld SIGKILL/OOM orphans the SST process tree — the
  posix_spawn path sets no PDEATHSIG (sql/wsrep_utils.cc:580-650) — and the joiner trap
  `sig_joiner_cleanup` does not exit on SIGTERM (xtrabackup-v2.sh:953-957). Node/container
  termination during post-processing is the most Antithesis-natural trigger.

Propagation: mysql.user is part of the datadir. A node carrying the live account that later
becomes a **donor** streams it to every future joiner (SST = full datadir copy). The next
SST *into* a node does run `DROP USER IF EXISTS` first (:877), so the account is refreshed
per-joiner-SST — but between SSTs the live account persists indefinitely, survives restarts,
and (because `SET sql_log_bin=OFF` :876,:967 and wsrep_provider=none :850) is invisible to
Galera replication and to binlog-based audit: nodes legitimately *differ* in mysql.user
content, so the cross-node checksum oracle must exclude it, and nothing else checks it.

## Failure scenario

SST triggered (joiner restart after unclean stop — trivially common under Antithesis
faults). During the post-processing phase (bounded by `post-processing-timeout`, default
300s — a wide window), the joiner mysqld/script is killed, or the temp-mysqld interaction
fails (disk pressure, FC on the client calls, socket-path issues). Result: a permanent,
unlocked, GRANT-ALL superuser account in that node's mysql.user. Anyone who can
authenticate as it (localhost only; password random — see Open Questions on residual
credential material) has full control; independent of exploitability, the node now violates
its own hardening contract and the state silently propagates to future SST joiners if the
node donates.

## Suggested assertions (all missing)

- **Always (workload-side — the property proper):** on every node, at any time when no SST
  post-processing is in flight on that node:
  `SELECT COUNT(*) FROM mysql.user WHERE user='mysql.pxc.sst.user' AND account_locked='N'`
  == 0. Always matches: the invariant must hold on every check outside the explicit window.
  (Gate on `wsrep_local_state_comment != 'Joining'`/absence of sst_in_progress AND
  `wsrep_local_state != 2` (not Donor) — the donor-side lifecycle keeps the account
  legitimately unlocked for the whole donation; see Open Questions/Investigation Log.)
- **Sometimes (workload-side):** "an SST post-processing phase was interrupted (script or
  node killed between temp-mysqld start and shutdown)" — the triggering window is narrow
  relative to a run; without this marker a green Always is weak evidence.
- **AlwaysOrUnreachable (SUT-side alternative if script instrumentation is added):** at
  cleanup_joiner / safe_exit, assert the account is locked-or-absent before the script
  exits with failure. Shell-side, so practically this becomes a wrapper check in the
  harness image rather than SDK instrumentation.

## Fault-availability notes

The cleanest trigger is killing the joiner **node/container** during post-processing — node
termination is often disabled by default; FLAG: this property wants it enabled, or the
harness must induce the script-internal error paths instead (e.g. disk-full in the upgrade
tmpdir, killing only the temp mysqld PID from a sidecar — both reachable without
node-termination faults). Network faults alone are unlikely to hit the window because the
temp mysqld is `--skip-networking` (unix socket only).

## Observations

- Exploitability nuance (honest assessment): the password is 36 chars of /dev/urandom
  (:865) and held only in script memory and `--defaults-file=/dev/stdin` heredocs — not
  obviously recoverable post-mortem. The security value of the invariant is (a)
  defense-in-depth the vendor itself asserts in a comment, (b) `ALTER USER ...` on that
  account requires no password to *reset* by anyone who gains a different foothold with
  CREATE USER privileges — a persistence vehicle, (c) the differing-mysql.user propagation
  breaks auditability. If the team wants a purely exploit-graded property this is weaker
  than the shell-injection one; as a testable state-hygiene invariant it is crisp and cheap.
- The temp mysqld itself runs with `--read_only=OFF --super_read_only=OFF` (:847) and logs
  into the datadir (`mysqld.post.processing.log` :792 — propagates cluster-wide via future
  SSTs, per sut-analysis focus 12 §4).
- Related-but-separate hazards in the same function kept out of this property's scope: the
  `wait_for_mysqld_startup` kills the wrong variable by scoping accident (:566 vs :611 per
  sut-analysis), and upgrade DDL may mint GTIDs (init-file sets sql_log_bin=OFF per-session
  but upgrade runs are separate sessions).

## Open Questions

None — all three resolved (see Investigation Log). **Property CHANGED (scope extended) by a
finding from Q3:** there is a SECOND, donor-side lifecycle of the same account, in server
code, not the script. `wsrep_create_sst_user` (sql/wsrep_sst.cc:1272-1335) creates
`'mysql.pxc.sst.user'@localhost` UNLOCKED on the donor at the start of every donation —
granted the `mysql.pxc.sst.role` role (LOCK TABLES, PROCESS, RELOAD, REPLICATION CLIENT,
SUPER, BACKUP_ADMIN, ... — scripts/mysql_system_users.sql:97-135; scoped, not GRANT ALL) —
and `wsrep_remove_sst_user` DROPs it only after the donor script exits
(sql/wsrep_sst.cc:1551, sole call site). Consequences:

- A donor kill mid-donation leaves an unlocked, role-scoped near-superuser account on the
  DONOR — a violation window on the donor side, cleaned only by that node's next donation.
- Every SST stream ships the unlocked donor account inside mysql.user (created before the
  backup starts); joiner post-processing is what replaces it with the GRANT-ALL variant and
  locks it — so a joiner killed before post-processing retains the DONOR's unlocked copy.
- The workload probe is unchanged (`account_locked='N'` count == 0), but the gate must
  exclude BOTH windows: no SST post-processing in flight on the node AND the node is not
  currently DONOR (`wsrep_local_state != 2`).

### Investigation Log

#### Can the random password persist anywhere on disk?

- Examined: `scripts/wsrep_sst_common.sh:860-1000` (generation :865; delivery paths:
  init-file heredoc via `--init-file=/dev/stdin` :876-881, client credentials via
  `--defaults-file=/dev/stdin` heredocs :902-971, `exec_sql`'s
  `--defaults-file=<(echo ...)` process substitution :1014-1022), temp-mysqld flags
  (`--general-log=0 --slow-query-log=0 --log_output=NONE` :841-846), capture files
  (`show_replica_status.out`, `reset_replica.out`, `upgrade_shutdown.out` — client
  stdout/stderr only), grep for `set -x`/xtrace (none; WSREP_LOG_DEBUG only gates
  wsrep_log_debug lines), server-side donor password path (fprintf to the script's stdin
  pipe, wsrep_sst.cc:1433-1437; query buffer memset after use :1327).
- Found: the plaintext never reaches a CLI argument or a persistent file; only the
  caching_sha2 hash lands in mysql.user (normal). Client batch mode writes no history file.
- Not found: any temp file, log, or capture containing the plaintext.
- Conclusion: RESOLVED — no on-disk persistence; severity assessment stands
  (policy/persistence-vehicle, not directly usable credentials).

#### Does the ALTER inside the failed multi-statement shutdown take effect?

- Examined: `scripts/wsrep_sst_common.sh:958-990` (single client session executing
  `SET sql_log_bin=OFF; ALTER USER ... ACCOUNT LOCK; SHUTDOWN;` with one exit code).
- Found: statements execute sequentially in one session, so the answer depends on where the
  failure hit: connect failure or ALTER failure → account left UNLOCKED (real violation);
  failure at/after SHUTDOWN → ALTER already committed (grant tables are InnoDB; the commit
  is durable and survives the follow-up `kill -9`). The script's exit code cannot
  distinguish the sub-cases.
- Conclusion: RESOLVED as far as code determines — the :973 path IS a real violation window
  in its pre-ALTER sub-cases; since errcode doesn't disambiguate, the workload should treat
  ANY failed post-processing as potentially-unlocked and probe the account state, rather
  than trying to classify sub-windows in the Sometimes marker.

#### Is 'mysql.pxc.sst.user' detected by any vendor tooling?

- Examined: tree-wide grep for `mysql.pxc.sst`; `sql/wsrep_sst.cc:1272-1360`
  (wsrep_create_sst_user / wsrep_remove_sst_user), call-site search (`:1404` create at
  donor-thread start, `:1551` drop after donor script exit — the only remove call),
  `scripts/mysql_system_users.sql:88-135` (role definition), pxc_strict_mode/upgrade-checker
  greps.
- Found: NO shipped check, startup cleanup, or tooling detects a leftover unlocked account.
  Cleanup is purely lifecycle-driven: donor-side DROP after each donation, next SST's
  DROP-and-recreate (:877), and the post-processing ALTER...LOCK. Bonus finding: the
  donor-side unlocked account lifecycle described above (property scope extended).
- Conclusion: RESOLVED — no shipped detection to piggyback on; the bespoke workload probe is
  required, now gated on both windows.

#### Is the locked-not-dropped mechanism and its kill window real?

- Examined: scripts/wsrep_sst_common.sh:655-998 in full (creation :875-881, all error
  paths :886-890, :912-921, :943-952, :973-985, lock+shutdown :963-972, vendor comment
  :959-961); spawn-path PDEATHSIG divergence sql/wsrep_utils.cc:461-470 vs :580-650
  (from sut-analysis, spot-confirmed); next-SST refresh via DROP USER IF EXISTS :877.
- Found: account is created unlocked with GRANT ALL before any post-processing step; three
  explicit `kill -9`-before-lock error paths plus external-kill windows; account never
  dropped by design (comment documents lock-as-mitigation).
- Not found: any startup-time or runtime check on any node asserting the account is
  locked/absent; any replication of the account state (deliberately sql_log_bin=OFF,
  provider=none).
- Conclusion: invariant is the vendor's own stated intent; violation windows concrete and
  line-numbered; property is workload-checkable with one SQL probe.
