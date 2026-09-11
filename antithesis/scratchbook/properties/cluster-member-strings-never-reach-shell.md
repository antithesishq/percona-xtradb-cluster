---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
---

# cluster-member-strings-never-reach-shell — Peer-supplied strings never reach a shell unvalidated

**Type:** Safety (with a Sometimes companion on the rejection path)
**Confidence:** High — both fix commits read in full; validators, gate function, and MTR
injection test all confirmed in the tree at commit f9ecb3e.

## What led here

PXC-5240/CVE-2026-49261 and PXC-5241/CVE-2026-48165 (both fixed 2026-06-15, i.e. very
recent) closed shell-interpolation paths where a *remote cluster member's* self-reported
strings, or SUPER-settable sysvars, were interpolated into a command string executed via
`/bin/sh -c`. Both fixes are **allowlist (character-class) based**, which makes them classic
regression targets: any future loosening of the allowlist (there is real product pressure to
support richer node names/addresses — IPv6 brackets and `/` were already added to the addr
class) or any *new* interpolation site added to these hot files reopens the hole. The
sink is generic: `wsp::process` runs every command through `sh -c`
(sql/wsrep_utils.cc:187 `_PATH_BSHELL "/bin/sh"`, :392 `{"sh","-c",str_}`).

## Code paths (validated at f9ecb3e)

Three independent channels feed peer/operator strings toward `sh -c`:

1. **View-change notify (PXC-5240, commit 9d4e3d5f953).** On every cluster view change,
   `wsrep_notify_status()` (sql/wsrep_notify.cc:46-115) builds
   `<wsrep_notify_cmd> --status ... --members <id>/<name>/<incoming>,...` and runs it via
   `wsp::process` (:106). `name` and `incoming` are the **remote joiner's**
   `wsrep_node_name` / `wsrep_node_incoming_address` — attacker-controlled cluster-internal
   input reaching every node's shell. The fix adds `is_valid_node_name()` (:25-32, alnum +
   `-_.`) and `is_valid_node_addr()` (:35-44, additionally `:[]/`); any member failing the
   check aborts the whole notification with `WSREP_ERROR("Unsafe characters in cluster
   member %u ...")` (:83-90) **before** the process is spawned.
   The member `id` (:91-95) and view uuid (:65-68) are *not* validated, but
   `wsrep::operator<<(ostream&, const wsrep::id&)` (wsrep-lib/src/id.cpp:81-103) emits either
   a purely alphanumeric string or a hex UUID rendering — safe by construction *today*; a
   change to id stringification would silently reopen this channel.

2. **Joiner-side SST sysvars (PXC-5241, commit 7a6af91e72a).** `wsrep_sst_donor` and
   `wsrep_sst_receive_address` are interpolated into the SST script CLI. The fix validates
   at SET-time: `check_request_str()` + `filename_char`/`address_char`/`names_list`
   (sql/wsrep_sst.cc:94-121), enforced in the sysvar check functions (:164, :194).

3. **Donor-side incoming SST request (pre-existing gate, explicitly relied on by the
   PXC-5241 commit message).** `wsrep_sst_donate()` (sql/wsrep_sst.cc:1690) gatekeeps every
   peer-supplied donate request through `is_sst_request_valid()` (:1643-1683): method must
   be in `allowed_sst_methods` (default `xtrabackup-v2,clone` —
   `WSREP_SST_ALLOWED_METHODS_DEFAULT`, wsrep_sst.cc:88; CORRECTED from the earlier
   "literally only xtrabackup-v2"), exactly
   `method\0data\0` framing (piggyback rejected :1663-1666), and data must match regex
   `[\w:/.[\]@-]+` (:1677). Rejection → `WSREP_ERROR("Invalid sst_request: ...")` +
   `WSREP_CB_FAILURE` (:1696-1707). The joiner-supplied `auth` part is split off and passed
   to the script via stdin, never on the CLI (:1726-1744 + commit message).

Regression-test artifact: mysql-test/suite/galera/t/galera_wsrep_notify_cmd_injection.test —
node_2 joins with `wsrep_node_name=';touch PWN;#'` (see .cnf), asserts node_1 logs the
rejection and no `PWN` file appears in the datadir.

## Failure scenario

A "malicious/compromised member" is modeled in the harness by giving one node a
`wsrep_node_name` / `wsrep_node_incoming_address` / SST-request payload containing shell
metacharacters plus a sentinel command (`;touch /tmp/pwned;#`). Under membership churn
(partitions healing → repeated view changes → repeated notify invocations; SST retries →
repeated donate requests) every view change and every SST re-exercises the validation gates.
A violation = the sentinel side effect appears on any node, meaning peer-controlled bytes
executed as shell on a machine the peer does not own. Pre-fix this was CVSS 10.0
(cluster-internal channel, no SQL auth needed — only Galera-channel membership, which has
no peer-identity verification beyond CA chaining, gu_asio.cpp:416-421).

## Suggested assertions (all missing — no SDK instrumentation exists)

- **Unreachable (workload/sidecar check acting as the assertion):** "shell-injection
  sentinel executed" — a watcher on every node asserts the sentinel file/marker never
  appears. This is the property proper; Unreachable matches "this state must never be
  observed".
- **Sometimes (SUT-side, sql/wsrep_notify.cc:89 and sql/wsrep_sst.cc:1706):** "unsafe
  member string rejected before shell" / "invalid SST request rejected" — proves the
  malicious config actually drove the validators (otherwise the Unreachable is vacuously
  green). Can be approximated workload-side by grepping error logs for
  `Unsafe characters in cluster member` / `Invalid sst_request`.
- **Always (SUT-side, at `wsp::process::process`, sql/wsrep_utils.cc ctor):** every command
  string passed to `sh -c` from the notify/SST call sites is composed only of validated or
  compile-time-constant segments. Harder to phrase mechanically; the two assertions above
  are the practical form.

## Fault-availability notes

Needs only view changes (network partitions — default-on) and SST triggers. No node
termination or clock faults required, though node kills increase SST/view frequency.

## Observations

- The notify path still has the pre-existing snprintf-accumulation hazard: `cmd_off` sums
  `snprintf` *would-be* lengths; the `cmd_off == cmd_len` guard (:100-104) runs AFTER all
  writes, and an intermediate truncation makes `cmd_len - cmd_off` negative → cast to
  size_t in later calls → OOB pointer/size. Member count and name lengths are
  remote-influenced. Distinct bug from injection; same function; worth a separate
  crash-oracle note (native crash detection covers it if it fires).
- Graceful degradation choice in the fix: a cluster containing one bad-named member gets NO
  notifications at all (whole notification aborted, not just that member skipped) —
  operational monitoring silently goes dark. Not a security violation but a testable
  side-effect (liveness of notify under a hostile member).
- `wsrep_notify_cmd` runs synchronously with no timeout from the view path
  (wsrep_notify.cc:106-108) — a hung notify script stalls view processing (separate
  liveness property, out of scope here).

## Open Questions

None — all three resolved (see Investigation Log). Net effect on the property: no change to
the invariant; the sink set is confirmed closed (two live peer-facing sites, both gated); a
garbd node is optional (it adds a donor-gate exerciser, not a new sentinel surface); and the
"allowlist-loosening pressure" narrative should cite the real facts (garbd's own validation
is deliberately permissive printable-ASCII) rather than a nonexistent liberalization commit.

### Investigation Log

#### Does garbd apply equivalent validation on its notify/SST-request paths?

- Examined: galera tree `garb/` in full relevant parts — `garb/process.cc:94`
  (`{"bash","-c",str_}` — garbd's only shell sink), `garb/garb_recv_loop.cpp:91`
  (process constructed from `config_.recv_script()` — fixed at startup), `:100-175`
  (recv loop: no shell composition from peer data; the SST request it SENDS is built from
  its own `--sst`/`--donor` config and lands on the donor's `is_sst_request_valid` gate),
  `garb/garb_config.cpp:216-252` (PXC-3921 validation: regex `^[ -~]+$` per option),
  galera submodule commit `681e5247` (PXC-3921) and top-level `65fb25eb58e` (submodule bump
  + MTR test `galera_garbd_invalid_param_value.test`).
- Found: PXC-3921 validates only garbd's OWN CLI/config options, and only against
  non-printable ASCII — shell metacharacters are explicitly allowed (the MTR test injects
  `\015` control chars, not `;`). But no *peer-supplied* string ever reaches garbd's
  `bash -c`: the recv-script command is operator config executed verbatim; garbd runs no
  notify command.
- Not found: any garbd path interpolating remote member strings into a shell command.
- Conclusion: RESOLVED — garbd is not a sentinel surface for this property (its injection
  exposure is operator-config-only, not a trust-boundary crossing). A garbd node in the
  harness is optional; if included, hostile `--sst`/`--donor` strings exercise the donor
  gate (`Sometimes` on `Invalid sst_request` rejection).

#### Does an 8.4.5 node-name liberalization commit exist?

- Examined: `git log --grep PXC-3921` in both trees (top-level d2598ea/1481bbe/65fb25e;
  galera 0adb3539/681e5247 — all "Add user input validation to garbd"); history search for
  node-name commits (`52b34516037` 64-byte limit, `ac6ad6a757b` hostname default,
  PXC-5240 validation ports — no liberalization anywhere);
  `galera_garbd_invalid_param_value.test` content.
- Found: no liberalization commit exists in either tree. The sut-analysis claim is a
  misreading of PXC-3921 — whose permissive printable-ASCII validation *accepts* special
  characters in garbd options (the likely source of the "added special-char support"
  confusion).
- Conclusion: RESOLVED — the regression-target framing stands on the two CVE fixes alone;
  the loosening pressure is hypothetical, not documented by a commit. Partial tag cleared.

#### Are there other runtime-composed `wsp::process` call sites?

- Examined: tree-wide grep for `wsp::process` constructions; `sql/wsrep_notify.cc:106`;
  `sql/wsrep_sst.cc:618` (joiner script — command composed by sst_prepare_other from
  SET-time-validated sysvars + constants), `:1424` (donor script — composed in
  sst_donate_other after `is_sst_request_valid`), `:1008` (sst_run_shell — inside `#if 0`,
  dead code); `sql/wsrep_mysqld.cc:1117-1163` (`override_galera_option` — the suspected
  "encryption option rewriting" manipulates the wsrep_provider_options STRING passed to the
  galera option parser; no shell involvement).
- Found: the complete live sink set is exactly three: notify (validated per PXC-5240),
  joiner SST launch (sysvars validated per PXC-5241), donor SST launch (request gated).
- Not found: any ungated runtime-composed shell execution.
- Conclusion: RESOLVED — sink set closed; the sentinel property covers the full surface.

#### Are the claimed fixes real and allowlist-based?

- Examined: `git show 9d4e3d5f953` (full message + diff), `git show 7a6af91e72a` (full
  message + diff), sql/wsrep_notify.cc (whole file at HEAD), sql/wsrep_sst.cc:94-200 and
  :1643-1775, sql/wsrep_utils.cc shell spawn (:187,:392),
  galera_wsrep_notify_cmd_injection.test, wsrep-lib/src/id.cpp (whole file).
- Found: both fixes present at f9ecb3e; character-class allowlists exactly as described;
  donor gate `is_sst_request_valid` with regex + framing check; id stringification safe by
  construction; MTR test uses `;touch PWN;#` node name.
- Not found: garbd-side equivalent (submodule not examined).
- Conclusion: property grounded in the mechanism, not the report text. Regression-target
  claim validated against actual fix commits.

## Synthesis refinement (2026-09-10)

DEMOTED to CI/deterministic-variant-only: the check has no timing component, and the metacharacter-laden sentinel node name is allowlist-REJECTED AT STARTUP — it breaks the 3-node baseline and would pollute shared runs (poison-budget convention). Needs its own fenced variant environment; priority lowered to Low in the catalog.
