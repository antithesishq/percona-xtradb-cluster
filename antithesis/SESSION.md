# SESSION

Deferred issues found during triage. Fix later unless they block work.

## From run 1ef6812708256c2f81e103ba93c0d048-63-5 (2026-10-06)

- **`wl_witness` mismatch details cannot show which rows are missing.** The table-content
  oracle gives only row count and hash per node. Add the `wid` set difference to the details.
- **`[coverage] a node died in a boot whose state transfer had failed` never fires**, though
  the run has many SST-failure gu_aborts. The 2026-09-28 gate skips any boot that ends in
  gu_abort, and SST failures end in gu_abort. Check if this claim can ever pass.
- `[prod] cluster commit progress never freezes while every node reports Synced` emits only for node1 in run 17eb4031…-63-5. `healthy_claim` (`antithesis/workload/pxcwl/probe.py:438-443`) seems false for node2/node3 even when their probes succeed. Check which term fails (wsrep_ready, cluster_size, desync_count).
- `antithesis/scratchbook/properties/first-committer-wins-loser-leaves-no-trace.md:92-93,117` says CONN_FAIL is returned only before ordering. Run 17eb4031…-63-5 disproves it: a node that goes non-Primary after send gets CONN_FAIL for a writeset the rest of the cluster orders.
- The "write outcome unknown" coverage events carry no node, so triage cannot assign them to a node. Add `node` to their details.
- `snouty runs events` for the gtid property returned only passing examples in run 17eb4031…-63-5, so other counterexamples cannot be sampled without query-logs.
