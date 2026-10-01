"""Structured events that put the workload's actions in the Antithesis log.

Why this exists: a crash log ends at the crash moment, and before these events
nothing in it said what the workload was doing. The journal records every
operation, but it lives in the workload container, and the only summary that
reached the log (`outcome_tally`) is emitted by the terminal verifier -- after
the crash, without time, order or node. mysqld logs only some levers, so a
crash after `applier_resize` or `ws_size_squeeze` looked like a crash after
nothing.

So every action is announced when it STARTS, as an SDK event. That is the one
placement that puts the in-flight action -- the likeliest trigger -- before the
crash in the log, in order, with its virtual time. A summary at the end of a
driver call would miss exactly that action. Find the events with:

    snouty runs events <run_id> pxc_lever
    snouty runs events <run_id> pxc_op

Event names are inline constants at each call site, not built here, so a grep
for the name finds every emitter.
"""

from __future__ import annotations

import traceback
from typing import Any, Mapping

from antithesis import lifecycle


def emit(name: str, details: Mapping[str, Any]) -> None:
    """Send one event. Never raises: a lost log line must not cost an action."""
    try:
        lifecycle.send_event(name, details)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
