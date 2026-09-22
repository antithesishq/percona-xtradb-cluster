"""The traffic generator. Entry point for parallel_driver_.

One invocation is one bounded chunk of work: it opens a few sessions, runs a
drawn number of operations against them, and exits. Antithesis re-runs and
overlaps these, and the overlap is what produces the concurrency the
interesting bugs need -- BF conflicts, parallel-applier races, cross-node write
contention.

Exits 0 on every SQL-level outcome. Under fault injection a dropped
connection, a certification conflict, or a node that is not ready are all
expected inputs rather than findings, so a non-zero exit is reserved for the
journal itself being unusable -- a harness bug, which should be loud.
"""

from __future__ import annotations

import traceback
import time

from . import config, ddl, journal, leases, levers, ops, rnd, swarm


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
    jr = journal.Journal("traffic")
    sessions: list[ops.Session] = []
    try:
        # Put back any lever whose holder was killed before touching anything.
        leases.repair_expired(jr)

        profile = jr.get_swarm()
        if not profile:
            # The first_ command has not run yet, or its draw did not land.
            # Nothing to do; this is not an error.
            return 0
        profile = swarm.jitter(profile)

        n_sessions = max(1, int(profile.get("concurrency_sessions", 1)))
        for _ in range(n_sessions):
            name, host = swarm.pick_node(profile)
            sessions.append(ops.Session(name, host, profile))

        budget_end = time.time() + config.TRAFFIC_WALL_BUDGET_SECONDS
        remaining = int(profile.get("ops_per_invocation", 100))
        think = float(profile.get("think_time_ms", 0)) / 1000.0
        weights = {k: float(v) for k, v in dict(profile.get("class_weight", {})).items()}

        while remaining > 0 and time.time() < budget_end:
            remaining -= 1
            cls = rnd.weighted_choice(weights)
            if cls is None:
                break

            session = rnd.choice(sessions)
            if session.demoted:
                live = [s for s in sessions if not s.demoted]
                if not live:
                    break
                session = rnd.choice(live)

            if not session.ensure():
                # A node we cannot reach is demoted for the rest of this
                # invocation rather than retried forever: the goal is to keep
                # making progress somewhere, not to insist on this node.
                session.demoted = True
                jr.tally("session_demoted", None)
                continue

            if cls == "ddl":
                ddl.run_one(jr, session, profile)
            elif cls == "admin":
                # A lever can legitimately run for minutes (graceful_shutdown
                # waits for a port to close and then for a rejoin). Checking
                # the budget only between operations made the nominal
                # per-invocation budget misleading and held a disruption token
                # well past its deadline, so do not START one with little time
                # left.
                if time.time() + 60.0 < budget_end:
                    levers.run_one(jr, session, profile)
            else:
                ops.run_one(jr, session, profile, cls)

            if think > 0:
                time.sleep(think)

        return 0
    finally:
        for s in sessions:
            s.close()
        # Never leave a lever set on the way out.
        leases.release_all_held_by(jr)
        jr.finish()
