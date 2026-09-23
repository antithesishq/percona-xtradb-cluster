#!/usr/bin/env bash
# Run every oracle test. No container runtime needed: the workload is driven
# against stubbed antithesis/pymysql packages (see helper_stubs.py).
#
# Run this after ANY change to workload/pxcwl/checks.py, oracles.py or
# levers.py. The comparison tests are detection tests -- they assert that a
# real divergence is still caught, not merely that a clean cluster passes.
set -uo pipefail
cd "$(dirname "$0")"
rc=0
for t in test_*.py; do
  echo "=== $t"
  python3 "$t" || rc=1
  echo
done
[ $rc -eq 0 ] && echo "ALL ORACLE TESTS PASSED" || echo "ORACLE TESTS FAILED"
exit $rc
