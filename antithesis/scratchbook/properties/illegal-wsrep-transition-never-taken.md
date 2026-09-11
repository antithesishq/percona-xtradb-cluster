# illegal-wsrep-transition-never-taken

**Property:** The wsrep-lib transaction and server state machines never take a transition
their own legality matrices forbid. In release (NDEBUG) builds these matrices are checked
by `assert(0)` that compiles out — the illegal transition is logged and then **applied
anyway** — so the SUT's strongest internal spec exists but is unenforced exactly where the
field runs it.

**Confidence:** High that the enforcement gap exists (verified both sites). Unknown whether
illegal transitions actually occur under faults — that unknown is the point: this converts
the SUT's own dormant spec into an oracle.

## Why this is wildcard territory

This is the assertion-erosion class ("assertions-as-oracle erosion at the wsrep seam"):
~200+ internal invariants at the mysqld↔wsrep-lib↔provider seam vanish under the forced
`-DNDEBUG` (galera `cmake/compiler.cmake:57-59`, top `CMakeLists.txt:1551-1554`). Rather
than one focus-area bug, it is a *whole class* of ready-made properties: every
"log + assert(0) + proceed" branch is a free `Unreachable`. The two state-machine matrices
are the highest-value members because everything else (BF abort, replay, commit ordering,
SST) is built on top of their correctness.

## Code evidence (verified at commit f9ecb3e)

1. **Transaction state machine** — `wsrep-lib/src/transaction.cpp:1392-1416`: 13×13
   `allowed` matrix; on violation `wsrep::log_debug() << "unallowed state transition for
   transaction ..."` then `assert(0)`; execution continues and `state_ = next_state` is
   applied (`:1418-1423`). Note the violation log is *debug level* — likely invisible in a
   default release log, so the field has effectively **no** signal for this.
2. **Server state machine** — `wsrep-lib/src/server_state.cpp:1456-1481`: 9×9 matrix; on
   violation `wsrep::log_warning() << "... unallowed state transition: ..."` then
   `assert(0)`; transition applied (`:1488-1491`). One legal-ish escape is coded: while
   `s_disconnecting`, illegal events are ignored (`:1474-1475`).
3. **Same pattern deeper down** (members of the same class, cited from sut-analysis focus
   11 §1 — spot-checked previously by that focus): 
   `replicator_smm.cpp:3893-3910` (`enter_apply_monitor_for_local_not_committing` default
   branch returns *without entering* the apply monitor; the paired leave then corrupts
   monitor bookkeeping), `server_state.cpp:1176-1200` (forced donor→synced), and the
   monitor-internal asserts (`galerautils/.../monitor.hpp`).
4. Known trigger candidates from history: MDEV-38843 (apply+rollback error), PXC-4844
   (dirty Diagnostics_area), replay-path races (`transaction.cpp:1959-1961` "Galera may
   return CONN_FAIL if trx BF aborted O_o") — all involve unusual state sequences under
   faults, i.e., exactly what would traverse a forbidden matrix cell first.

## Failure scenario

Under a BF-abort/replay/SST-cancel race, a transaction moves e.g. s_committing →
s_certifying (forbidden). Release build logs at debug level and continues; downstream code
assumes matrix-legal history (e.g., monitor enter/leave pairing, replay bookkeeping) and
corrupts ordering state → node stall, or worse, an out-of-order commit — with zero
diagnostic trail.

## Testable formulation

Two complementary implementations; do both:

- **SUT-side (missing instrumentation), release build**: add Antithesis `Unreachable`
  assertions in the two violation branches:
  `transaction.cpp:1410-1416` → `unreachable("wsrep transaction illegal state transition")`
  and `server_state.cpp:1476-1481` → `unreachable("wsrep server illegal state transition")`
  (distinct messages per site, per catalog rules; optionally include from/to states as
  details). `Unreachable` is the exact semantic match: the SUT's own spec says this branch
  must never execute; any hit is a bug regardless of downstream symptoms, and it gives
  Antithesis a replay anchor at the earliest corruption point rather than at the eventual
  stall.
- **Workload-side (no code change) fallback**: `Always`-style log scan — no error log ever
  contains "unallowed state transition". Weaker: the transaction-side message is
  debug-level (see above), so raise wsrep_debug/verbosity in one variant or rely on the
  server_state warning only.
- Companion build variant: run one image with asserts enabled (-UNDEBUG / Debug) so the
  native `assert(0)` becomes a crash oracle for the same class (sut-analysis §12 already
  recommends dual images; this property is the concrete consumer).

## Instrumentation suggestions (all missing)

- The two `Unreachable` calls above (wsrep-lib is a submodule — needs a patch carried by
  the harness build).
- Same treatment for `replicator_smm.cpp:3893-3910` default branch (third distinct
  message: "apply monitor entered via impossible client mode").
- Optional `Sometimes` on rare-but-legal transitions (s_must_replay entry, replay success)
  to steer exploration toward the state-space corners where illegal transitions live.

## Fault requirements

None special — this is a passive oracle. Value scales with whatever faults the other
properties inject (network faults, BF-abort-heavy workloads, SST churn). Works with
default-on faults only.

## Open questions

- Does the transaction-matrix violation branch ever fire in practice, or is it genuinely
  unreachable? Either answer is valuable: a hit is a real bug at the seam; sustained
  silence upgrades confidence in every property that assumes matrix-legal histories.
  `(partial: statically unresolvable by design — the run answers; both violation branches
  re-verified at commit f9ecb3e)`

Resolved (see Investigation Log):

- The `s_disconnecting` ignore-escape is confirmed at `server_state.cpp:1474` and sits
  *before* the warn/assert — the planned assertion placement (after the early return) is
  correct and will not false-positive on shutdown races.
- The harness build **can** carry the wsrep-lib patch: the submodule's sources are
  vendored in the build tree and compiled directly via `ADD_SUBDIRECTORY(wsrep-lib)`
  (top `CMakeLists.txt:2432`); both `Unreachable` sites are implementable.

### Investigation Log

#### Does the transaction-matrix violation branch ever fire in practice?

- Examined: `wsrep-lib/src/transaction.cpp:1392-1424` (13×13 `allowed` matrix; on
  violation `wsrep::log_debug()` "unallowed state transition for transaction" at `:1411`,
  `assert(0)` at `:1415`, then `state_ = next_state` applied at `:1423`);
  `wsrep-lib/src/server_state.cpp:1455-1491` (9×9 matrix, `log_warning` + `assert(0)`
  at `:1476-1480`, transition applied after).
- Found: both warn-and-proceed branches exist exactly as cataloged; the transaction-side
  message is debug-level (invisible in default release logs), confirming the SUT-side
  `Unreachable` is the only reliable oracle for that matrix.
- Conclusion: the reachability question is empirical by nature — no static analysis can
  prove or refute it; tagged `(partial)` and left for the run.

#### The s_disconnecting ignore-escape — assertion placement

- Examined: `wsrep-lib/src/server_state.cpp:1470-1480`.
- Found: `if (s_disconnecting == state_) return;` at `:1474` executes before the
  warning/assert — illegal events during disconnect are silently swallowed by design.
- Conclusion: resolved — placing the `Unreachable` after that early return (as planned)
  cannot false-positive on shutdown; no property change needed.

#### Can the harness build carry a wsrep-lib patch?

- Examined: `.gitmodules` (wsrep-lib listed as submodule of
  github.com/percona/wsrep-lib), working tree (`wsrep-lib/` sources present — the tree
  is a full export), top `CMakeLists.txt` (`INCLUDE(wsrep-lib)` at `:797`, include dirs
  at `:1678-1679`, `ADD_SUBDIRECTORY(wsrep-lib)` at `:2432`).
- Found: wsrep-lib is compiled from the in-tree sources by the main build; any harness
  that builds this tree compiles a patched `transaction.cpp`/`server_state.cpp`
  automatically. No separate prebuilt artifact is fetched.
- Conclusion: resolved — the patch is trivially carriable; the SUT-side `Unreachable`
  variant is implementable, log-scan fallback demoted to backup.
