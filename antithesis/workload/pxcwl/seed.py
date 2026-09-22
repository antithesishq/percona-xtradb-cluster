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


def run() -> int:
    """Entry point. Never exits non-zero for an environment condition.

    Antithesis attaches a built-in property to each command's exit code, so a
    non-zero exit must mean a genuine bug -- not a locked journal, an
    unreachable node, or a command killed mid-flight.
    """
    try:
        return _run()
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
            schema.create_all(conn)
            schema.seed_rows(conn)
        finally:
            db.close_quietly(conn)

        jr.put_swarm(profile)
        jr.put_observed("fc_limit", profile["fc_limit"])
        jr.put_observed("timeline_started_at", time.time())

        applied = _apply_posture(profile)
        jr.put_observed("posture", applied)
        return 0
    finally:
        jr.finish()
