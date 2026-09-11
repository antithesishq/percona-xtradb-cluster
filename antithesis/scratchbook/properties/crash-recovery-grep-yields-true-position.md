# crash-recovery-grep-yields-true-position

**Property:** After an unclean node stop, the packaged recovery flow (the
`--wsrep-recover` → grep error log → `--wsrep_start_position` dance performed by
mysqld_safe / the systemd pre-start scripts) always starts the node with exactly the
position mysqld printed — it never (a) refuses to start solely because log parsing failed,
(b) starts with no recovered position when one was printed, or (c) starts with a stale
position from an earlier boot.

**Confidence:** High — the pattern mismatch is verified in code, including the
detail that one fallback message is unreachable (`#if 0`). **Investigated 2026-09-10: the
broken-grep scripts are NOT field-reachable** — both shipped RPM and Debian units route
recovery exclusively through `/usr/bin/mysql-systemd galera-recovery` (working bracketed
grep). `mysqld_pre_systemd.in` is never built (`WITH_SYSTEMD=OFF` in all PXC spec cmake
invocations and defaulted off by debian rules); `mysql-helpers:get_recover_pos` ships on
Debian but has no invoker in any shipped unit. The property therefore targets the
*shipped* flow (mysql-systemd galera-recovery, plus mysqld_safe for harnesses that use
it), not a deterministically-broken path — see Investigation Log.

## Why this is wildcard territory

The recovery *machinery* is shell-greps-log — a text protocol between mysqld and its
supervisor. Focus 3 (failure recovery) will test the SUT's recovery semantics; nobody owns
"the log-scraping layer between mysqld and its own startup scripts is correct." This is
also the "operator following documented procedure never loses availability/data" class.

## Code evidence (verified at commit f9ecb3e)

1. **What mysqld actually emits**: `sql/wsrep_mysqld.cc:1364`
   `WSREP_INFO("Recovered position: %s", ...)`; `WSREP_LOG` macro
   (`sql/wsrep_mysqld.h:290-302`) routes through LogEvent with `.subsys("WSREP")`; the
   traditional sink renders `<ts> <tid> [<label>] [MY-xxxxxx] [WSREP] <msg>`
   (`sql/server_component/log_sink_trad.cc:333-337`). So the on-disk line contains
   **`[WSREP] Recovered position:`** — never `WSREP: Recovered position:`.
2. **Two incompatible grep families ship in the same tree**:
   - Bracketed (matches): `scripts/mysqld_safe.sh:295`,
     `build-ps/rpm/mysql-systemd:306`, `build-ps/debian/extra/mysql-systemd:323`,
     `scripts/wsrep_sst_clone.sh:1202`.
   - Unbracketed (**never matches**): `scripts/systemd/mysqld_pre_systemd.in:116`,
     `build-ps/debian/extra/mysql-helpers:83`.
3. **The fallback is unreachable code**: on no match, the scripts accept a
   `'skipping position recovery'` line instead (`mysqld_pre_systemd.in:118`,
   `mysqld_safe.sh:297`, both mysql-systemd variants). But the only emitter of that message
   is inside `#if 0` (`sql/wsrep_mysqld.cc:1350-1360`) — it can never be printed by 8.4.10.
   Consequence on the mysqld_pre_systemd/mysql-helpers path: grep fails → fallback fails →
   `exit 1` → **the unit refuses to start after any crash**; and since
   `mysqld_pre_systemd.in` greps a *persistent, append-only* `$log` (`2>> $log`, `:113-116`),
   any old-format or hand-injected line from a previous boot would satisfy `tail -n 1` with
   a stale position (mechanism (c)).
4. **Environment poisoning**: `mysqld_pre_systemd.in:133-135` persists
   `MYSQLD_RECOVER_START` via `systemctl set-environment`; it is cleared only by the
   `--post` action (`:138-141`) — a failed start between pre and post replays a stale
   `--wsrep_start_position` on the next boot.
5. **Verbatim-grastate shortcut**: `mysqld_safe.sh:270-278` — when grastate seqno != -1,
   recovery is skipped and the value passed verbatim; a torn/garbage grastate seqno (the
   file is rewritten in place, non-atomically — `galera/src/saved_state.cpp`) becomes the
   start position with no sanity check. Combined with the healthy-node invariant "grastate
   seqno is -1 while running", any non-(-1) seqno after a crash is itself suspicious.
6. Related invariant from analysis (focus 2 §2.5): at process start,
   `grastate.seqno == -1 || grastate.seqno == SE_checkpoint.seqno` — PXC-4845 is the
   realized failure.

## Failure scenario

Node killed mid-load (unclean). Supervisor runs the recovery dance. On the broken-grep
path the node never rejoins (permanent capacity loss, no error pointing at the real cause);
on the stale-log path it rejoins at an old seqno → joins with a wrong position → IST from
the wrong point or forced SST — at scale, a crash storm converts to an SST storm.

## Testable formulation

Precondition: the harness must actually run a packaged recovery flow (mysqld_safe or a
faithful copy of the systemd scripts) rather than bare `mysqld` restart — this property is
*about* that layer, and per sut-analysis §12 the harness must choose one deliberately.
Primary variant: the shipped `mysql-systemd galera-recovery` flow (field path on both RPM
and Debian — see Investigation Log); secondary: mysqld_safe. A mysqld_pre_systemd-style
variant exercises only unshipped upstream-leftover code and is optional.

- `Always`: "whenever the recovery flow runs after an unclean stop, the restarted mysqld's
  `wsrep_start_position` (SELECT @@wsrep_start_position / error-log echo) equals the
  `[WSREP] Recovered position:` value printed by the `--wsrep_recover` pass of the same
  boot" — comparable entirely from the workload/entrypoint by capturing both values.
  `Always` because every recovery must satisfy it; one mismatch = real position corruption.
- `Always`: "a node that was running before the kill returns to `wsrep_ready=ON` within T
  after the supervisor starts the recovery flow, or the recovery flow emits an explicit
  error" — catches the silent `exit 1` refuse-to-start mode.
- `Sometimes`: "recovery ran with grastate seqno == -1 (true crash path)" and "recovery ran
  with grastate seqno != -1 (graceful path)" — both branches explored.

## Instrumentation suggestions (all missing)

- Entrypoint wrapper that captures the `--wsrep_recover` output and exports the parsed
  position for the workload to compare (this doubles as the harness's recovery
  implementation).
- Workload asserts the equality and the rejoin bound; also asserts
  `grastate.seqno ∈ {-1, SE_checkpoint}` at each restart by reading grastate.dat before
  start (cheap file read).

## Fault requirements

**Requires unclean node stops.** Node-termination faults are often disabled by default —
flag: either enable them, or have the test composer `kill -9` mysqld inside the container
and let the supervisor restart it (workload-driven, no platform fault needed). Network
faults alone won't exercise this.

## Open questions

None.

### Investigation Log

#### Which recovery script does the shipped unit actually invoke on RPM vs deb?

Investigated 2026-09-10.

- Examined: `build-ps/percona-xtradb-cluster.spec` (`%install` :1103-1105, `%files`
  :1902-1909, cmake invocations :843/:898/:961), `build-ps/rpm/{mysql.service,
  mysql@.service,mysql-systemd}`, `build-ps/debian/rules` (:301-304, cmake at :82/:135/
  :213), `build-ps/debian/extra/{mysql.service,mysql@.service,mysql-systemd,mysql-helpers,
  mysql-systemd-start}`, `build-ps/debian/control`, `percona-xtradb-cluster-server.install`,
  `scripts/CMakeLists.txt:574-602`, top `CMakeLists.txt:1625`.
- Found: both shipped units (RPM `build-ps/rpm/mysql.service:75` and Debian
  `build-ps/debian/extra/mysql.service:78`, plus both `mysql@.service` variants) run
  `ExecStartPre=/bin/sh -c "VAR=\`bash /usr/bin/mysql-systemd galera-recovery\`; [ $? -eq 0 ]
  && systemctl set-environment _WSREP_START_POSITION=$VAR || exit 1"` →
  `mysql-systemd:306` (rpm) / `:323` (deb), the WORKING bracketed grep
  `'\[WSREP\] Recovered position:'`, over a fresh `mktemp` file, then
  `ExecStart=/usr/sbin/mysqld $_WSREP_START_POSITION`. The units unset
  `_WSREP_START_POSITION` at the start of every ExecStartPre chain, so stale manager-env
  replay is mitigated in the shipped units.
- Found (vestigiality): `mysqld_pre_systemd.in` is generated/installed only under
  `IF(WITH_SYSTEMD)` (`scripts/CMakeLists.txt:574,584-585,598-602`); `WITH_SYSTEMD`
  defaults OFF (`CMakeLists.txt:1625`) and the PXC spec forces `-DWITH_SYSTEMD=OFF` at all
  three cmake invocations; debian rules never passes it. Its consumer unit
  `scripts/systemd/mysqld.service.in` is never installed. `mysql-helpers` IS installed on
  Debian (`rules:338`) and its `get_recover_pos` (broken unbracketed grep at `:83`) is
  called from `verify_database` → `mysql-systemd-start sanity()`, but no shipped PXC unit
  invokes `mysql-systemd-start` (only the never-packaged
  `percona-server-server.mysql@.service` references it; `percona-server-server` is not in
  `build-ps/debian/control`). Dead-but-installed code.
- Conclusion: RESOLVED — the broken-grep path is vestigial, not field-reachable. Shipped
  RPM and Debian both use the correct-grep `mysql-systemd galera-recovery`. The harness
  should reproduce the `mysql-systemd galera-recovery` flow (field path) and optionally
  mysqld_safe; a variant reproducing `mysqld_pre_systemd` tests upstream-leftover code
  only and should be deprioritized. The property statement stands; the expectation shifts
  from "likely finds a packaging defect immediately" to "guards the shipped scrape
  protocol against position corruption/staleness".

#### Could mysqld_safe's error-log append later satisfy another script's grep (stale-line hazard)?

Investigated 2026-09-10.

- Examined: `scripts/mysqld_safe.sh:282-320` (mktemp `wsrep_recovery.XXXXXX`, grep at
  :295, append to `$err_log` at :311-315, `rm $wr_logfile` :320),
  `build-ps/rpm/mysql-systemd:281-345`, `build-ps/debian/extra/mysql-systemd:281-345`,
  `scripts/systemd/mysqld_pre_systemd.in:59-135`, `build-ps/debian/extra/mysql-helpers:
  76-103`, `scripts/wsrep_sst_clone.sh:849-1230`.
- Found: every shipped script greps a fresh `mktemp` file (mysqld_safe:
  `wsrep_recovery.XXXXXX`; both mysql-systemd variants: `wsrep_recovery_verbose.XXXXXX`;
  mysql-helpers: mktemp in /var/lib/mysql-files). The ONLY script that greps the
  persistent append-only error log is the unshipped `mysqld_pre_systemd.in:113-116`
  (`$log` = configured `log-error`, `tail -n 1`), and its pattern (unbracketed) is
  disjoint from the bracketed lines mysqld_safe appends — doubly defused.
- Found (minor residual): `wsrep_sst_clone.sh:1200-1213` appends recovery output to a
  datadir-resident `$CLONE_ERR` with `>>` and takes the FIRST (oldest) match if multiple
  lines exist; bounded by truncation at `:1055` and `cleanup_joiner` removal at `:226`.
- Conclusion: RESOLVED — no shipped flow can be poisoned by mysqld_safe's appended lines;
  the stale-line hazard exists only in the unshipped `mysqld_pre_systemd.in` design.
  Entrypoint builders: keep the fresh-mktemp pattern and the hazard cannot re-enter.

## Synthesis refinement (2026-09-10)

DEFERRED: the v1 entrypoint supervisor replaces the shipped wrapper layer, so in v1 this property tests harness code, not the SUT packaging. Revive under a future field-faithful supervisor variant that runs the shipped mysql-systemd galera-recovery flow.
