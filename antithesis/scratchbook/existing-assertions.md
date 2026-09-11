---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
external_references:
  - path: https://docs.percona.com/percona-xtradb-cluster/8.4/
    why: Upstream product documentation (user-approved scope: repo + upstream docs)
  - path: https://galeracluster.com/library/documentation/
    why: Galera replication library documentation (user-approved scope: repo + upstream docs)
  - path: https://dev.mysql.com/doc/refman/8.4/en/
    why: MySQL 8.4 reference documentation (user-approved scope: repo + upstream docs)
---

# Existing Antithesis SDK Assertions

**Result: none found.** The codebase has no existing Antithesis instrumentation.

## Scan performed

Searched the full repo (commit `f9ecb3e`, excluding `.git/`) across C/C++/Go/Python/Rust
sources and CMake build files for:

- Imports/includes of any Antithesis SDK (`antithesis_sdk`, `antithesis/instrumentation`,
  any path or symbol containing `antithesis`, case-insensitive)
- Assertion calls: `assert_always`, `assert_sometimes`, `assert_reachable`,
  `assert_unreachable` and non-macro equivalents

No matches. The only near-miss hits were MySQL's own `MY_ASSERT_UNREACHABLE()` macro
(`include/my_compiler.h:78`), which maps to `__builtin_unreachable()`/`assert(0)` and is
unrelated to the Antithesis SDK.

## Implication

All Antithesis properties for this SUT must be introduced from scratch — either as
workload-side checks (test commands querying cluster state) or as new SUT-side SDK
instrumentation added to the wsrep/Galera code paths. The codebase does, however, have
extensive native assertion density (`assert`, `DBUG_ASSERT`, `ut_a`/`ut_ad` in InnoDB,
`gu_trace`/assertions in Galera) that Antithesis picks up as crash-detection signal even
without SDK instrumentation.

## Assumptions

- Scan covered source and build files only; no generated build artifacts exist in-tree.

## Open Questions

- None.
