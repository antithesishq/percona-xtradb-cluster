"""The quiesce protocol, the checksum pass, and ack reconciliation.

All of this runs only where fault injection has stopped. There are deliberately
no mid-run quiesced checkpoints: under active faults a quiesce cannot be made
reliable, it over-promises, and waiting for one drains exactly the interleavings
the faults were creating. The guaranteed full-strength check belongs at the end.
"""

from __future__ import annotations

import concurrent.futures
import time

import pymysql.cursors

from . import config, db, schema

SCHEMA = config.SCHEMA


# ==========================================================================
# Quiesce
# ==========================================================================


def wait_all_synced(deadline: float, poll: float = 3.0) -> dict[str, dict[str, str] | None]:
    """Wait until every node is Synced in one Primary component on one UUID.

    Same definition the entrypoint's readiness gate uses, so "ready" means the
    same thing at both ends of the run.
    """
    last: dict[str, dict[str, str] | None] = {}
    while time.time() < deadline:
        last = db.cluster_status()
        if all(db.is_synced(s) for s in last.values()):
            uuids = {
                s.get("wsrep_local_state_uuid")
                for s in last.values()
                if s and s.get("wsrep_local_state_uuid")
            }
            if len(uuids) == 1:
                return last
        time.sleep(poll)
    return last


def _down(host: str) -> bool:
    """Whether the last connect to ``host`` found nothing to talk to.

    A refused connection means nothing is listening: mysqld is down or still
    booting (a boot that cannot reach Primary aborts before it ever listens).
    A name-resolution failure means the container itself is gone. A timeout
    is NOT either -- a hung node may still be Primary -- so it never counts.
    """
    err = db.LAST_ERROR.get(host, "")
    return any(
        m in err
        for m in (
            "Connection refused",
            "Errno 111",
            "Name or service not known",
            "Temporary failure in name resolution",
            "Errno -2",
            "Errno -3",
        )
    )


def _last_committed(st: dict[str, str]) -> int | None:
    try:
        return int(st.get("wsrep_last_committed", "") or "")
    except ValueError:
        return None


def _no_primary_anywhere(states: dict[str, dict[str, str] | None]) -> bool:
    """Every node is provably outside a Primary Component.

    Reachable nodes must report a non-Primary cluster status. A node that is
    not reachable must be down, AND every reachable node must count it in its
    own current (non-Primary) component -- ``wsrep_cluster_size`` equal to the
    full cluster. That second half is what makes skipping it safe: a node
    inside a non-Primary component cannot simultaneously be a Primary of its
    own, whereas a down node outside it might be about to restore one from
    gvwstate.dat.
    """
    reachable = {n: st for n, st in states.items() if st}
    if not reachable:
        return False
    if any(st.get("wsrep_cluster_status", "").lower() == "primary" for st in reachable.values()):
        return False
    hosts = dict(config.NODES)
    missing = [n for n, st in states.items() if not st]
    if not all(_down(hosts[n]) for n in missing):
        return False
    if missing and not all(
        st.get("wsrep_cluster_size", "") == str(config.EXPECTED_CLUSTER_SIZE)
        for st in reachable.values()
    ):
        return False
    return True


def _ledger_high_water(jr) -> tuple[int | None, dict[str, int]]:
    """Highest wsrep_last_committed the anytime_ probe ever saw, per node."""
    try:
        rows = jr.conn.execute("SELECT node, last_committed FROM progress").fetchall()
    except Exception:  # noqa: BLE001
        return None, {}
    seen = {r["node"]: int(r["last_committed"]) for r in rows}
    return (max(seen.values()) if seen else -1), seen


def bootstrap_if_no_primary(jr, stable_for: float = 15.0) -> dict | None:
    """Recover a cluster that has lost its Primary Component, as an operator would.

    Galera does not recover from this on its own unless every member of the
    last Primary view comes back -- which a gracefully restarted node, with a
    new gcomm identity and no gvwstate.dat, never does. The documented
    recovery is ``pc.bootstrap=YES`` on the most advanced node. Without this,
    the reconvergence assertion measured "did the fault schedule happen to
    avoid a total split" rather than anything about PXC: run 5aa4afb5-63-0
    went red with node2 and node3 non-Primary for 600 quiet seconds.

    Bootstrapping the wrong node is itself a manufactured finding -- a second
    Primary, or a more advanced node SST'd back over its acknowledged writes
    -- so every gate below errs toward doing nothing:

      * no node may be Primary, and a down node is only skipped when the
        reachable nodes count it inside their own non-Primary component
        (``_no_primary_anywhere``), held across two samples ``stable_for``
        apart so a remerge in progress is left alone, and re-checked
        immediately before the SET;
      * the chosen node must be at or beyond the highest commit the anytime_
        probe ever observed on ANY node. Seqnos are one sequence within one
        lineage, so a down node that was once seen further ahead blocks the
        bootstrap outright. With no ledger at all, only a fully reachable
        cluster -- where the comparison below is the whole truth -- qualifies.

    Runs only after fault injection has stopped. Returns None when not
    applicable, else what was decided and done.
    """
    first = db.cluster_status()
    if not _no_primary_anywhere(first):
        return None
    time.sleep(stable_for)
    states = db.cluster_status()
    if not _no_primary_anywhere(states):
        return None

    candidates = {n: _last_committed(st) for n, st in states.items() if st}
    candidates = {n: c for n, c in candidates.items() if c is not None}
    high_water, seen = _ledger_high_water(jr)
    all_reachable = all(states.get(n) for n, _ in config.NODES)
    decision = {
        "bootstrapped": False,
        "node": None,
        "last_committed": candidates,
        "ledger_high_water": seen,
        "cluster_status": {
            n: (st or {}).get("wsrep_cluster_status") for n, st in states.items()
        },
        "local_state": {
            n: (st or {}).get("wsrep_local_state_comment") for n, st in states.items()
        },
        "cluster_sizes": {n: (st or {}).get("wsrep_cluster_size") for n, st in states.items()},
        "unreachable_reasons": {
            n: db.LAST_ERROR.get(h) for n, h in config.NODES if not states.get(n)
        },
        "error": None,
    }
    if not candidates:
        decision["error"] = "no reachable node reported wsrep_last_committed"
        return decision

    # Most advanced node; ties go to the first in config order, deterministically.
    order = [n for n, _ in config.NODES]
    chosen = max(candidates, key=lambda n: (candidates[n], -order.index(n)))
    decision["node"] = chosen
    if high_water is None and not all_reachable:
        decision["error"] = "journal ledger unreadable and a node is down: cannot rule it ahead"
        return decision
    if high_water is not None and candidates[chosen] < high_water:
        decision["error"] = "a node was once observed ahead of every reachable node"
        return decision

    host = dict(config.NODES)[chosen]
    conn = db.connect_with_retry(host, attempts=2)
    if conn is None:
        decision["error"] = db.LAST_ERROR.get(host)
        return decision
    try:
        # Last look: the sampling above took seconds, and a booting node can
        # restore a Primary in that gap.
        if not _no_primary_anywhere(db.cluster_status()):
            decision["error"] = "a Primary appeared before the bootstrap was sent"
            return decision
        with conn.cursor() as cur:
            cur.execute("SET GLOBAL wsrep_provider_options = 'pc.bootstrap=YES'")
        decision["bootstrapped"] = True
    except Exception as exc:  # noqa: BLE001
        decision["error"] = str(exc)[:200]
    finally:
        db.close_quietly(conn)
    return decision


def wait_commit_cut_equal(deadline: float, poll: float = 2.0) -> tuple[bool, dict[str, int]]:
    """Wait for wsrep_last_committed to agree across nodes, twice running.

    Two consecutive equal readings, because a single one can catch the cluster
    mid-flight. Cross-node comparability needs this: a per-node sync_wait only
    drains what that node has already seen, so without an equal commit cut the
    three nodes could be compared at different points in the total order.
    """
    previous: dict[str, int] | None = None
    while time.time() < deadline:
        cut: dict[str, int] = {}
        states = db.cluster_status()
        for name, st in states.items():
            if st is None:
                cut = {}
                break
            try:
                cut[name] = int(st.get("wsrep_last_committed", "-1") or -1)
            except ValueError:
                cut = {}
                break
        if cut and len(set(cut.values())) == 1:
            if previous == cut:
                return True, cut
            previous = cut
        else:
            previous = None
        time.sleep(poll)
    return False, previous or {}


def open_barrier_connections() -> dict[str, object]:
    """One connection per node with the strongest causality barrier set.

    wsrep_sync_wait=7 waits on the apply monitor, which is released only after
    a commit is fully over on both the applier and local paths -- unlike the
    commit monitor, which can be released early under group commit. That makes
    it the stronger barrier and a sound commit-visibility gate.
    """
    conns: dict[str, object] = {}
    for name, host in config.NODES:
        conn = db.connect_with_retry(host, SCHEMA, attempts=3)
        if conn is None:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION wsrep_sync_wait = 7")
            conns[name] = conn
        except Exception:  # noqa: BLE001
            db.close_quietly(conn)
    return conns


# ==========================================================================
# Schema comparison
# ==========================================================================


def compare_schemas(conns: dict[str, object], tables: list[str]) -> tuple[bool, dict]:
    """Compare column definitions across nodes before hashing any rows.

    If the definitions differ, a row comparison is meaningless and the real
    finding is the schema divergence -- so this runs first and is its own
    property.

    A node that cannot answer is EXCLUDED, never compared. The previous form
    folded the exception into the signature, and an exception never equals a
    real signature, so one transient 1205 on one node reported every table as
    divergent. This is the same exclusion `compare_table_set` already makes.
    """
    mismatches: dict[str, dict[str, object]] = {}
    unreadable: dict[str, dict[str, str]] = {}
    for table in tables:
        sigs: dict[str, list] = {}
        errors: dict[str, str] = {}
        for name, conn in conns.items():
            try:
                sigs[name] = schema.column_signature(conn, table)
            except Exception as exc:  # noqa: BLE001
                errors[name] = str(exc)[:120]
        if errors:
            unreadable[table] = errors
        # One readable node compares against nothing and proves nothing.
        if len(sigs) < 2:
            continue
        distinct = {repr(v) for v in sigs.values()}
        if len(distinct) > 1:
            mismatches[table] = {n: [c for c, _ in v] for n, v in sigs.items()}
    details: dict[str, object] = {"schema_mismatches": mismatches}
    if unreadable:
        details["schema_unreadable"] = unreadable
    return (not mismatches), details


def compare_table_set(conns: dict[str, object]) -> tuple[bool, dict]:
    """Compare which tables and indexes exist, across nodes.

    The checksum set covers only the stable workload tables. Every DDL target is
    a scratch table outside that set, so without this check nothing at all
    compares the RESULT of TOI DDL across nodes -- and a table present on one
    node and missing on another would score as identical, because a missing
    table simply yields an empty column signature.
    """
    per_node: dict[str, object] = {}
    for name, conn in conns.items():
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT TABLE_NAME FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
                    (SCHEMA,),
                )
                tables = tuple(r[0] for r in cur.fetchall())
                cur.execute(
                    "SELECT TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME "
                    "FROM information_schema.statistics "
                    "WHERE TABLE_SCHEMA = %s "
                    "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
                    (SCHEMA,),
                )
                indexes = tuple(tuple(r) for r in cur.fetchall())
            per_node[name] = (tables, indexes)
        except Exception as exc:  # noqa: BLE001
            per_node[name] = f"error: {str(exc)[:160]}"

    readable = {k: v for k, v in per_node.items() if isinstance(v, tuple)}
    if len(readable) < 2:
        return True, {"table_set": "fewer than two nodes readable"}

    distinct = {v for v in readable.values()}
    if len(distinct) <= 1:
        return True, {"table_count": len(next(iter(readable.values()))[0])}

    # Report the symmetric difference rather than two long lists.
    sets = {k: set(v[0]) for k, v in readable.items()}
    common = set.intersection(*sets.values())
    return False, {
        "tables_per_node": {k: sorted(v) for k, v in sets.items()},
        "tables_not_on_every_node": sorted(set.union(*sets.values()) - common),
        "index_rows_per_node": {k: len(v[1]) for k, v in readable.items()},
    }


# ==========================================================================
# Checksums
# ==========================================================================


def _checksum_one(conn, table: str, columns: list[str]) -> tuple:
    with conn.cursor() as cur:
        cur.execute(schema.checksum_expr(table, columns))
        row = cur.fetchone()
    return (int(row[0]), str(row[1]), str(row[2]))


def checksum_table(conns: dict[str, object], table: str) -> dict[str, object]:
    """Checksum one table on every node, querying the nodes concurrently."""
    columns: list[str] = []
    for conn in conns.values():
        try:
            columns = [c for c, _ in schema.column_signature(conn, table)]
            if columns:
                break
        except Exception:  # noqa: BLE001
            continue
    if not columns:
        return {"table": table, "skipped": "no column signature"}

    results: dict[str, object] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(conns) or 1) as pool:
        futures = {
            pool.submit(_checksum_one, conn, table, columns): name
            for name, conn in conns.items()
        }
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as exc:  # noqa: BLE001
                results[name] = f"error: {str(exc)[:160]}"
    return {"table": table, "per_node": results}


def compare_checksums(
    conns: dict[str, object], tables: list[str]
) -> tuple[bool, dict]:
    """Compare every table across nodes. Short-circuits on the first mismatch.

    Short-circuiting is not only about speed: the first differing table is the
    most useful thing a divergence finding can carry.
    """
    details: dict[str, object] = {}
    all_equal = True
    for table in tables:
        res = checksum_table(conns, table)
        per_node = res.get("per_node")
        if not isinstance(per_node, dict) or not per_node:
            details[table] = res
            continue
        values = {v for v in per_node.values() if isinstance(v, tuple)}
        errors = {k: v for k, v in per_node.items() if not isinstance(v, tuple)}
        if errors:
            details.setdefault("unreadable", {})[table] = errors  # type: ignore[index]
        if len(values) > 1:
            all_equal = False
            details["first_mismatch"] = {
                "table": table,
                "per_node": {k: list(v) if isinstance(v, tuple) else v for k, v in per_node.items()},
            }
            break
        if values:
            (n, _, _) = next(iter(values))
            details.setdefault("row_counts", {})[table] = n  # type: ignore[index]
    return all_equal, details


def compare_gtid_executed(conns: dict[str, object]) -> tuple[bool, dict]:
    """Compare gtid_executed across nodes.

    An independent plane from row content: rows and GTID sets can diverge
    separately, so neither check subsumes the other. Free here because
    gtid_mode=ON is already configured.

    A node whose read fails is EXCLUDED, never compared: an exception string
    differs from every real GTID set, so folding it in turned a transient 1205
    into a reported divergence.
    """
    sets: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name, conn in conns.items():
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT @@GLOBAL.gtid_executed")
                raw = cur.fetchone()[0] or ""
            # Normalise: the same set can be printed with different interval
            # ordering and with embedded newlines.
            sets[name] = ",".join(
                sorted(p.strip() for p in str(raw).replace("\n", "").split(",") if p.strip())
            )
        except Exception as exc:  # noqa: BLE001
            errors[name] = str(exc)[:120]
    details: dict[str, object] = {"gtid_executed": sets}
    if errors:
        details["gtid_unreadable"] = errors
    # One readable node compares against nothing and proves nothing.
    if len(sets) < 2:
        return True, details
    return len(set(sets.values())) <= 1, details


# ==========================================================================
# Ack reconciliation
# ==========================================================================


def reconcile(jr, conns: dict[str, object]) -> tuple[bool, bool, bool, dict]:
    """Check the ack journal against what each node actually holds.

    Returns (acked_present, failed_absent, no_unminted, details).

    The three states give a band rather than a point value:

      ACKED    must be present on every node -- exact, in that direction
      FAILED   must be absent from every node -- exact, in that direction
      UNKNOWN  gets no per-key verdict at all; it may be present or absent,
               and demanding either would be asserting something the protocol
               cannot know

    Unknown keys still have to be *consistently* present or absent across
    nodes, but that is already proved more strongly by the checksum pass, so it
    is reported here rather than asserted again.

    Implemented as a streaming merge join: the witness table can hold millions
    of rows, so both sides are read in key order and compared in one pass with
    bounded memory. The first discrepancy yields a tight key for triage.
    """
    missing_acked: dict[str, list[int]] = {}
    failed_present: dict[str, list[int]] = {}
    unminted: dict[str, list[int]] = {}
    unknown_presence: dict[str, int] = {}
    scan_errors: dict[str, str] = {}

    for name, conn in conns.items():
        sql_rows = jr.conn.execute(
            "SELECT wid, state FROM ack WHERE target = 'witness' ORDER BY wid"
        )
        cur = conn.cursor(pymysql.cursors.SSCursor)
        try:
            cur.execute(f"SELECT wid FROM `{SCHEMA}`.`wl_witness` ORDER BY wid")

            a = sql_rows.fetchone()
            m = cur.fetchone()
            unknown_here = 0

            while a is not None and m is not None:
                awid, astate = int(a["wid"]), a["state"]
                mwid = int(m[0])
                if awid < mwid:
                    if astate == "ACKED":
                        missing_acked.setdefault(name, [])
                        if len(missing_acked[name]) < 10:
                            missing_acked[name].append(awid)
                    a = sql_rows.fetchone()
                elif awid == mwid:
                    if astate == "FAILED":
                        failed_present.setdefault(name, [])
                        if len(failed_present[name]) < 10:
                            failed_present[name].append(awid)
                    elif astate in ("UNKNOWN", "ATTEMPTED"):
                        unknown_here += 1
                    a = sql_rows.fetchone()
                    m = cur.fetchone()
                else:
                    unminted.setdefault(name, [])
                    if len(unminted[name]) < 10:
                        unminted[name].append(mwid)
                    m = cur.fetchone()

            while a is not None:
                if a["state"] == "ACKED":
                    missing_acked.setdefault(name, [])
                    if len(missing_acked[name]) < 10:
                        missing_acked[name].append(int(a["wid"]))
                a = sql_rows.fetchone()

            while m is not None:
                unminted.setdefault(name, [])
                if len(unminted[name]) < 10:
                    unminted[name].append(int(m[0]))
                m = cur.fetchone()

            unknown_presence[name] = unknown_here
        except Exception as exc:  # noqa: BLE001
            # Reading this node failed part-way. That is an environment
            # condition, not evidence about the data, and a partial scan proves
            # nothing either way -- so it is recorded and excluded rather than
            # counted as a discrepancy.
            scan_errors[name] = str(exc)[:200]
            missing_acked.pop(name, None)
            failed_present.pop(name, None)
            unminted.pop(name, None)
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass

    counts = jr.ack_counts()
    details = {
        "ack_counts": counts,
        "missing_acked_sample": missing_acked,
        "failed_present_sample": failed_present,
        # What the journal recorded for each sampled discrepancy. Without it a
        # red here cannot be classified at all: run 5aa4afb5-63-0 had 8 FAILED
        # rows on every node and no way to tell which operation, errno or
        # phase produced them.
        "failed_present_evidence": _journal_evidence(jr, failed_present),
        "missing_acked_evidence": _journal_evidence(jr, missing_acked),
        "unminted_sample": unminted,
        "unknown_rows_present_per_node": unknown_presence,
        "scan_errors": scan_errors,
        "nodes_fully_scanned": sorted(set(conns) - set(scan_errors)),
    }
    return (not missing_acked), (not failed_present), (not unminted), details


def _journal_evidence(jr, samples: dict[str, list[int]]) -> dict[str, dict]:
    """The journal rows behind a set of sampled wids, keyed by wid.

    Best-effort: this only decorates details, so a journal read that fails
    leaves the verdict alone and says so.
    """
    wids = sorted({w for ws in samples.values() for w in ws})
    if not wids:
        return {}
    try:
        placeholders = ",".join("?" * len(wids))
        rows = jr.conn.execute(
            "SELECT wid, inv_id, node, shape, state, errno, errmsg FROM ack "
            f"WHERE wid IN ({placeholders})",
            wids,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        return {"journal_read_error": {"error": str(exc)[:200]}}
    return {
        str(r["wid"]): {
            "inv_id": r["inv_id"],
            "node": r["node"],
            "shape": r["shape"],
            "state": r["state"],
            "errno": r["errno"],
            "errmsg": (r["errmsg"] or "")[:200],
        }
        for r in rows
    }


def counter_bounds(jr, conns: dict[str, object]) -> tuple[bool, bool, dict]:
    """Check the hot-counter total against its journal band."""
    floor, ceiling = jr.incr_bounds()
    totals: dict[str, int] = {}
    for name, conn in conns.items():
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COALESCE(SUM(v), 0) FROM `{SCHEMA}`.`wl_hot`")
                totals[name] = int(cur.fetchone()[0])
        except Exception:  # noqa: BLE001
            continue
    if not totals:
        return True, True, {"counter_totals": {}, "note": "no node readable"}

    observed = max(totals.values())
    details = {
        "counter_totals": totals,
        "acked_floor": floor,
        "attempted_ceiling": ceiling,
        "observed": observed,
    }
    return observed >= floor, observed <= ceiling, details
