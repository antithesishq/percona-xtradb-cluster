"""Timeline initialization. Entry point for the first_ command.

Runs once per timeline, before any driver and before faults are injected, with
nothing else running alongside. Three things need exactly those conditions:

1. Drawing the swarm parameters. Drawing them once is the whole point -- if
   each driver invocation drew its own, the skew would average out across the
   timeline and swarm testing would degrade into uniform mixing.
2. Creating the schema, without a TOI thundering herd from several concurrent
   drivers at timeline start.
3. Applying per-timeline server posture, while the cluster is known healthy.

It does NOT emit setup_complete. The workload container's entrypoint owns that,
and emitting it from a test command would deadlock startup: commands only run
after Antithesis has already observed it.
"""

from __future__ import annotations

import traceback
import time

from . import config, db, journal, leases, schema, swarm


class SeedSchemaError(RuntimeError):
    """A live server rejected one of our own schema statements.

    The one condition this command exits non-zero for. It is not an
    environment condition -- the node answered and told us the DDL was wrong --
    and it is not survivable either: without the schema no profile is
    published, so every driver idles and the whole timeline reports green over
    a workload that never ran. That is exactly what happened in run
    a359f1f86be19b71e7d1f94a1768f976-63-0, where `wl_nopk` was rejected with
    errno 3750 and nothing in the report said so.
    """


def _apply_posture(profile: dict) -> dict[str, object]:
    """Set the per-timeline server posture on every node.

    Recorded as well as applied, because one of these values -- fc_limit --
    parameterizes an assertion threshold later, and a threshold derived from a
    guess rather than from what was actually set would be meaningless.
    """
    applied: dict[str, object] = {}
    retry = int(profile.get("retry_autocommit", 1))
    opa = str(profile.get("optimistic_pa", "no"))
    fc_limit = int(profile.get("fc_limit", config.FC_LIMIT_DEFAULT))

    for name, host in config.NODES:
        conn = db.connect_with_retry(host, attempts=3)
        if conn is None:
            applied[name] = "unreachable"
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("SET GLOBAL wsrep_retry_autocommit = %s", (retry,))
                cur.execute(
                    "SET GLOBAL wsrep_provider_options = %s",
                    (f"cert.optimistic_pa={opa}",),
                )
                cur.execute(
                    "SET GLOBAL wsrep_provider_options = %s", (f"gcs.fc_limit={fc_limit}",)
                )
                # Container-local disk with no volumes, sync_binlog=1 and
                # log_replica_updates=ON: binlogs would otherwise grow for the
                # whole run and could fill the filesystem.
                cur.execute("SET GLOBAL binlog_expire_logs_seconds = 600")
            applied[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - posture is best-effort
            applied[name] = f"partial: {str(exc)[:120]}"
        finally:
            db.close_quietly(conn)
    return applied


def _seed_schema(conn) -> None:
    """Create every PK'd table and its fixed rows, or say why we could not.

    Raises SeedSchemaError when a live server rejected the statement, and lets
    the original exception through when the failure was environmental -- the
    caller turns that into a quiet exit 0 with no profile published.
    """
    try:
        schema.create_all(conn)
        schema.seed_rows(conn)
    except Exception as exc:  # noqa: BLE001 - classified, then re-raised
        if db.schema_ddl_failure_is_environmental(conn, exc):
            raise
        raise SeedSchemaError(
            f"schema seeding rejected by a live server: "
            f"errno={db.errno_of(exc)} {str(exc)[:300]}"
        ) from exc


def _create_nopk(jr, conn) -> str:
    """Create `wl_nopk` under a lease, and report what happened.

    Leased rather than merely fenced: schema.create_nopk lowers a GLOBAL on
    node1, and a seed killed between the two halves of that window would leave
    pxc_strict_mode PERMISSIVE for the rest of the run, quietly invalidating
    the premise of every property that assumes the ENFORCING default. The
    lease means the next command's repair pass puts it back. It reuses the
    strict_mode_window lever name on purpose -- it is the same lever, and
    sharing the name means the two can never be open at once.

    Failing to take the lease is not fatal: the rest of the workload does not
    need this table, and the return value records that the PK-less leg is
    inert for this timeline so a green nopk property is not read as evidence.
    """
    if not leases.acquire(
        jr,
        "strict_mode_window",
        config.NODES[0][0],
        intent="PERMISSIVE",
        restore="ENFORCING",
        hold_seconds=30,
    ):
        return "skipped: strict_mode_window lease unavailable"
    try:
        schema.create_nopk(conn)
        return "created"
    except Exception as exc:  # noqa: BLE001 - classified, then re-raised
        if db.schema_ddl_failure_is_environmental(conn, exc):
            return f"skipped: errno={db.errno_of(exc)} {str(exc)[:200]}"
        raise SeedSchemaError(
            f"`{config.NOPK_TABLE}` rejected by a live server inside the "
            f"PK-less fence: errno={db.errno_of(exc)} {str(exc)[:300]}"
        ) from exc
    finally:
        leases.release(jr, "strict_mode_window", config.NODES[0][0], "ENFORCING")


def run() -> int:
    """Entry point. Never exits non-zero for an environment condition.

    Antithesis attaches a built-in property to each command's exit code, so a
    non-zero exit must mean a genuine bug -- not a locked journal, an
    unreachable node, or a command killed mid-flight.

    SeedSchemaError is the single exception, and it is a harness bug rather
    than a PXC finding: see the class docstring.
    """
    try:
        return _run()
    except SeedSchemaError:
        traceback.print_exc()
        return 1
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 0


def _run() -> int:
    jr = journal.Journal("first")
    try:
        leases.repair_expired(jr)

        existing = jr.get_swarm()
        if existing:
            # A timeline gets exactly one personality. If a profile is already
            # recorded, this is a re-run and redrawing would corrupt the skew.
            return 0

        profile = swarm.draw()

        # Create the schema BEFORE publishing the profile. The drivers treat a
        # published profile as "the timeline is ready"; publishing it first and
        # then failing to seed would set every driver running against tables
        # that do not exist, which shows up as thousands of ER_NO_SUCH_TABLE
        # and can fire statement-level properties for a harness reason.
        host = config.NODES[0][1]
        conn = db.connect_with_retry(host, attempts=5)
        if conn is None:
            # Not a bug and not this command's business to report: the
            # entrypoint's readiness gate already passed, so a node being
            # unreachable here is fault injection doing its job. No profile is
            # published, so the drivers stay idle until a later timeline.
            return 0
        try:
            _seed_schema(conn)
            nopk = _create_nopk(jr, conn)
        finally:
            db.close_quietly(conn)

        jr.put_swarm(profile)
        jr.put_observed("fc_limit", profile["fc_limit"])
        jr.put_observed("nopk_table", nopk)
        jr.put_observed("timeline_started_at", time.time())

        applied = _apply_posture(profile)
        jr.put_observed("posture", applied)
        return 0
    finally:
        jr.finish()
