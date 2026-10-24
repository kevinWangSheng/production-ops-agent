#!/usr/bin/env python3
"""Dev-only F2 D harness: real ``kill -9`` of ``worker_main`` at one breakpoint.

F2 contract r1 U7 (docs/tasks/2026-10-17-f2-contract.md): one real Run per
breakpoint, killed once, resumed by a fresh ``worker_main``:

* ``model_in_flight``     a model request is reserved and not yet answered;
* ``response_committed``  the model step (with tool calls) is committed and
  none of its tools has been dispatched;
* ``tool_in_flight``      one Prometheus query is held by the host stall
  proxy (``scripts/f2_stall_proxy.py``, R7), the only unsettled dispatch,
  and every sibling of its step settled in this epoch is already in
  ``tool_results`` -- held unchanged for 1 s before the kill (#332 U-3).

Each breakpoint is located from PostgreSQL rows (owner connection), never by
sleeping: unsettled ``opspilot_budget_reservations``, committed
``opspilot_steps``, ``opspilot_tool_charges`` rows, the proxy's own stall
counter. ``response_committed`` holds the gap open with ``LOCK TABLE
opspilot_tool_charges IN EXCLUSIVE MODE`` taken while the model request is
in flight: ``commit_step`` never touches that table and the executor's
pre-dispatch charge (``DurableStore.charge_tool``) does, so the worker parks
between the two; the harness then terminates the killed worker's backends
before it releases the lock. That lock is a harness action on a throwaway
database, not a product permission.

Only the worker process this harness started is killed, after its PID's
parent and command line are checked; nothing is selected by name. The web
process is started by the operator (``python -m opspilot.web serve``);
this script submits one incident through ``POST /intake/events``.

Writes the raw ledger (snapshots, proxy log, worker logs) under the
ignored ``tmp/`` path given by ``--ledger-dir`` and only the frozen
``summary.json`` under ``--summary``. Credentials are read from the private
env files into process environments and never printed or written.

    .venv/bin/python scripts/f2_kill_resume.py run --breakpoint model_in_flight \\
        --owner-dsn 'host=127.0.0.1 port=55893 dbname=m0_budget user=m0_lab' \\
        --worker-dsn 'host=127.0.0.1 port=55893 dbname=m0_budget user=opspilot_worker_lab' \\
        --web-url http://127.0.0.1:8096 --event-token-file tmp/f2d/event-token \\
        --secrets-env-file /abs/.env --prometheus-env-file /abs/prometheus-investigator.env \\
        --lab-round f2-kill-resume-live-20261022 \\
        --ledger-dir tmp/lab-ledgers/f2-kill-resume-live \\
        --summary docs/evidence/f2-kill-resume-live/model_in_flight/summary.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BREAKPOINTS = ("model_in_flight", "response_committed", "tool_in_flight")
OPEN_STATES = frozenset({"queued", "running"})
STALLED_TOOL = "metrics_range_query"
TARGET_ID = "m0-otel-20260909"
WORKER_MODULE = "opspilot.worker_main"
# Seconds a candidate ``model_in_flight`` state must persist, unchanged,
# before the kill: the reservation row is written just before the request
# leaves, so the dwell makes "on the wire" the overwhelmingly likely state.
# It only delays the kill; the held check after the kill is what decides.
MODEL_DWELL_SECONDS = 1.0
# Epoch start is observed by polling; this is the polling resolution added
# to "lease expiry + one worker poll" when judging the restart latency.
EPOCH_POLL_SECONDS = 0.2
STALL_KILL_BEFORE_SECONDS = 25.0  # the stalled query times out at 30 s
# Seconds the ``tool_in_flight`` state -- the stalled dispatch plus every
# settled sibling of its step already in ``tool_results`` -- must persist,
# unchanged, before the kill (#332 U-3, same dwell as ``model_in_flight``).
# A sibling settled but not yet committed in the millisecond window before
# its commit is outside what the breakpoint covers.
SIBLING_DWELL_SECONDS = 1.0

QUESTION = (
    "Checkout requests are failing for users. Investigate the checkout and "
    "payment services in the OTel Demo lab: which service errors, since "
    "when, and what evidence supports the cause?"
)


# -- snapshot (rows -> plain dict) --------------------------------------------


def snapshot(conn: Any, run_id: str) -> dict[str, Any]:
    """Every row the verdicts read, in one read-only pass."""
    run = conn.execute(
        "SELECT state,epoch,owner::text AS owner,lease_until,budget_reserved,"
        "budget_spent,budget_unknown,tool_operations_used,tool_seconds_used,"
        "clock_timestamp() AS db_now FROM opspilot_runs WHERE run_id=%s",
        (run_id,),
    ).fetchone()
    steps = conn.execute(
        "SELECT step_id::text AS step_id,sequence,logical_key,status,response,"
        "jsonb_array_length(tool_results) AS tool_results,"
        "ARRAY(SELECT (r->>'ordinal')::int FROM jsonb_array_elements(tool_results) r "
        "ORDER BY 1) AS tool_ordinals,observed_at "
        "FROM opspilot_steps WHERE run_id=%s ORDER BY sequence,step_id",
        (run_id,),
    ).fetchall()
    reservations = conn.execute(
        "SELECT reservation_id::text AS reservation_id,state,reserved_seconds,seconds "
        "FROM opspilot_budget_reservations WHERE run_id=%s ORDER BY reservation_id",
        (run_id,),
    ).fetchall()
    charges = conn.execute(
        "SELECT dispatch_id::text AS dispatch_id,epoch,operation_id,reserved,seconds "
        "FROM opspilot_tool_charges WHERE run_id=%s ORDER BY epoch,operation_id",
        (run_id,),
    ).fetchall()
    evidence = conn.execute(
        "SELECT evidence_id,committed,adopted,raw_sha256 FROM opspilot_evidence "
        "WHERE run_id=%s ORDER BY evidence_id",
        (run_id,),
    ).fetchall()
    incident = conn.execute(
        "SELECT i.state,i.conclusion FROM opspilot_incidents i "
        "JOIN opspilot_runs r ON r.incident_id=i.incident_id WHERE r.run_id=%s",
        (run_id,),
    ).fetchone()
    return {
        "db_now": _iso(run["db_now"]),
        "run": {
            key: _iso(value) if isinstance(value, datetime) else value
            for key, value in run.items()
            if key != "db_now"
        },
        "steps": [
            {
                "step_id": s["step_id"],
                "sequence": s["sequence"],
                "logical_key": s["logical_key"],
                "status": s["status"],
                "kind": (s["response"] or {}).get("kind")
                if isinstance(s["response"], dict)
                else None,
                "tool_calls": tool_names(s["response"]),
                "tool_results": s["tool_results"],
                "tool_ordinals": list(s["tool_ordinals"]),
                "observed_at": _iso(s["observed_at"]),
            }
            for s in steps
        ],
        "reservations": [dict(r) for r in reservations],
        "charges": [dict(c) for c in charges],
        "evidence": [dict(e) for e in evidence],
        "incident_state": None if incident is None else incident["state"],
        "conclusion_evidence_ids": conclusion_evidence_ids(
            None if incident is None else incident["conclusion"]
        ),
    }


def _iso(value: Any) -> Any:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def tool_names(response: Any) -> list[str]:
    """Tool names of a committed model step, in ordinal order (both shapes)."""
    if not isinstance(response, dict):
        return []
    container = response.get("assistant", response)
    calls = container.get("tool_calls") if isinstance(container, dict) else None
    if not isinstance(calls, list):
        return []
    return [str((c.get("function") or {}).get("name")) for c in calls]


def conclusion_evidence_ids(conclusion: Any) -> list[str] | None:
    if not isinstance(conclusion, dict):
        return None
    inner = conclusion.get("conclusion", conclusion)
    ids = inner.get("evidence_ids") if isinstance(inner, dict) else None
    return list(ids) if isinstance(ids, list) else None


# -- pure readings of a snapshot ----------------------------------------------


def live_steps(snap: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [s for s in snap["steps"] if s["status"] != "late_result"]


def model_steps(snap: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Steps that answer one model request: rounds and compactions. The
    published ``conclusion`` step is a record of the report round, not a
    request of its own."""
    return [s for s in live_steps(snap) if s["kind"] != "conclusion"]


def unsettled_reservations(snap: Mapping[str, Any]) -> list[str]:
    return [
        r["reservation_id"] for r in snap["reservations"] if r["state"] == "reserved"
    ]


def committed_evidence(snap: Mapping[str, Any]) -> dict[str, str]:
    return {
        e["evidence_id"]: e["raw_sha256"] for e in snap["evidence"] if e["committed"]
    }


def operation_tool(snap: Mapping[str, Any], operation_id: str) -> str | None:
    """``<step_id>-t<ordinal>`` -> that step's tool name (C3 §7 op id)."""
    step_id, sep, ordinal = operation_id.rpartition("-t")
    if not sep or not ordinal.isdigit():
        return None
    for step in snap["steps"]:
        if step["step_id"] == step_id:
            names = step["tool_calls"]
            index = int(ordinal)
            return names[index] if index < len(names) else None
    return None


def unsettled_charges(snap: Mapping[str, Any], epoch: int) -> list[Mapping[str, Any]]:
    return [c for c in snap["charges"] if c["epoch"] == epoch and c["seconds"] is None]


def pending_step(snap: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The last live step when it planned tools and none of them ran yet."""
    steps = model_steps(snap)
    if not steps:
        return None
    last = steps[-1]
    if not last["tool_calls"] or last["tool_results"]:
        return None
    prefix = f"{last['step_id']}-t"
    if any(c["operation_id"].startswith(prefix) for c in snap["charges"]):
        return None
    return last


def armed(snap: Mapping[str, Any], *, arm_after_evidence: int) -> bool:
    """Prior evidence exists, so the kill can show it is kept."""
    return len(committed_evidence(snap)) >= arm_after_evidence


def model_in_flight(snap: Mapping[str, Any]) -> str | None:
    """The single unsettled model reservation, if exactly one."""
    pending = unsettled_reservations(snap)
    return pending[0] if len(pending) == 1 else None


def stalled_dispatch(snap: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The only unsettled dispatch of this epoch, when it is a stalled-tool call."""
    pending = unsettled_charges(snap, snap["run"]["epoch"])
    if len(pending) != 1:
        return None
    charge = pending[0]
    return (
        charge if operation_tool(snap, charge["operation_id"]) == STALLED_TOOL else None
    )


def committed_ops(snap: Mapping[str, Any]) -> set[str]:
    """Operation ids whose result is in their step's ``tool_results``.

    Parallel calls commit in completion order (#332), so the committed
    ordinals need not be a prefix. A ledger frozen before ``tool_ordinals``
    existed only has the count; those runs committed in index order.
    """
    ops: set[str] = set()
    for s in snap["steps"]:
        ordinals = s.get("tool_ordinals")
        if ordinals is None:
            ordinals = range(s["tool_results"])
        ops.update(f"{s['step_id']}-t{i}" for i in ordinals)
    return ops


def settled_uncommitted(
    snap: Mapping[str, Any], epoch: int, step_id: str | None = None
) -> list[str]:
    """Charges of ``epoch`` that settled but whose result is not committed,
    optionally only those of one step."""
    committed = committed_ops(snap)
    prefix = None if step_id is None else f"{step_id}-t"
    return sorted(
        c["operation_id"]
        for c in snap["charges"]
        if c["epoch"] == epoch
        and c["seconds"] is not None
        and c["operation_id"] not in committed
        and (prefix is None or c["operation_id"].startswith(prefix))
    )


def stalled_step(snap: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The step of the stalled dispatch, when there is one."""
    charge = stalled_dispatch(snap)
    if charge is None:
        return None
    step_id = charge["operation_id"].rpartition("-t")[0]
    return next((s for s in snap["steps"] if s["step_id"] == step_id), None)


def stall_without_sibling(snap: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The stalled dispatch when its step planned a single call: such a stall
    can never form breakpoint 3, so the harness lets it time out and arms
    the proxy again instead of killing."""
    step = stalled_step(snap)
    if step is None or len(step["tool_calls"]) >= 2:
        return None
    return stalled_dispatch(snap)


def committed_siblings(snap: Mapping[str, Any]) -> list[str]:
    """Calls of the stalled dispatch's step that settled in this epoch and
    whose result is committed (the stalled call itself excluded)."""
    charge = stalled_dispatch(snap)
    if charge is None:
        return []
    step_id = charge["operation_id"].rpartition("-t")[0]
    committed = committed_ops(snap)
    return sorted(
        c["operation_id"]
        for c in snap["charges"]
        if c["epoch"] == snap["run"]["epoch"]
        and c["seconds"] is not None
        and c["operation_id"].startswith(f"{step_id}-t")
        and c["operation_id"] in committed
    )


def tool_breakpoint_state(snap: Mapping[str, Any]) -> str | None:
    """A key for the ``tool_in_flight`` kill state, or None when it is not
    reached: the stalled dispatch is the epoch's only unsettled one, its
    step planned at least two calls, at least one sibling settled and is
    committed, and every sibling settled in this epoch is committed (#332
    U-3). The key changes whenever a sibling commits or settles."""
    charge = stalled_dispatch(snap)
    step = stalled_step(snap)
    if charge is None or step is None or len(step["tool_calls"]) < 2:
        return None
    step_id = step["step_id"]
    epoch = snap["run"]["epoch"]
    if settled_uncommitted(snap, epoch, step_id) or not committed_siblings(snap):
        return None
    ordinals = sorted(op for op in committed_ops(snap) if op.startswith(f"{step_id}-t"))
    settled = sorted(
        c["dispatch_id"]
        for c in snap["charges"]
        if c["epoch"] == epoch and c["seconds"] is not None
    )
    return json.dumps([charge["dispatch_id"], ordinals, settled])


def dwell(
    candidate: tuple[str, float] | None,
    state: str | None,
    now: float,
    seconds: float,
) -> tuple[tuple[str, float] | None, bool]:
    """Track a candidate kill state: ready once ``state`` has stayed the same
    for ``seconds``; any change restarts the clock, None clears it."""
    if state is None:
        return None, False
    if candidate is None or candidate[0] != state:
        return (state, now), False
    return candidate, now - candidate[1] >= seconds


# -- breakpoint held: pre (last snapshot before kill) vs post (after kill) -----


def breakpoint_held(
    breakpoint: str, pre: Mapping[str, Any], post: Mapping[str, Any]
) -> tuple[bool, dict[str, Any]]:
    detail: dict[str, Any] = {}
    if breakpoint == "model_in_flight":
        reservation = model_in_flight(pre)
        detail = {
            "reservation_id": reservation,
            "post_unsettled": unsettled_reservations(post),
            "live_steps_pre": len(live_steps(pre)),
            "live_steps_post": len(live_steps(post)),
        }
        held = (
            reservation is not None
            and unsettled_reservations(post) == [reservation]
            and len(live_steps(post)) == len(live_steps(pre))
        )
    elif breakpoint == "response_committed":
        step = pending_step(post)
        detail = {
            "step_id": None if step is None else step["step_id"],
            "logical_key": None if step is None else step["logical_key"],
            "tool_calls": None if step is None else step["tool_calls"],
            "post_unsettled_reservations": unsettled_reservations(post),
        }
        held = (
            step is not None
            and pending_step(pre) is not None
            and pending_step(pre)["step_id"] == step["step_id"]
            and not unsettled_reservations(post)
        )
    elif breakpoint == "tool_in_flight":
        charge = stalled_dispatch(post)
        detail = {
            "dispatch_id": None if charge is None else charge["dispatch_id"],
            "operation_id": None if charge is None else charge["operation_id"],
            "post_unsettled_reservations": unsettled_reservations(post),
        }
        held = (
            charge is not None
            and stalled_dispatch(pre) is not None
            and stalled_dispatch(pre)["dispatch_id"] == charge["dispatch_id"]
            and not unsettled_reservations(post)
        )
    else:
        raise ValueError(breakpoint)
    return held, detail


# -- verdicts (U7) ------------------------------------------------------------


def verdicts(
    breakpoint: str,
    *,
    pre: Mapping[str, Any],
    post: Mapping[str, Any],
    final: Mapping[str, Any],
    epoch_started_at: str | None,
    poll_seconds: float,
) -> dict[str, Any]:
    """Each U7 criterion with its numbers; ``passed`` is their conjunction."""
    kill_epoch = int(post["run"]["epoch"])
    held, held_detail = breakpoint_held(breakpoint, pre, post)

    # 1. model requests re-sent. Every physical request holds one
    # reservation; each answered one is settled and committed as a step,
    # each provider failure is settled ``unknown``. What is left over is the
    # killed attempt's in-flight request, whose round the next epoch sends
    # again -- the only re-send a kill may cost.
    killed_unsettled = unsettled_reservations(post)
    unknown = sum(1 for r in final["reservations"] if r["state"] == "unknown")
    lost = len(final["reservations"]) - unknown - len(model_steps(final))
    expected_resent = 1 if breakpoint == "model_in_flight" else 0
    model = {
        "in_flight_at_kill": killed_unsettled,
        "resent": lost,
        "expected": expected_resent,
        "passed": lost <= 1
        and lost == expected_resent
        and len(killed_unsettled) == expected_resent,
        "reservations_total": len(final["reservations"]),
        "reservations_unknown": unknown,
        "model_steps": len(model_steps(final)),
    }

    # 2. tool queries re-dispatched: operations charged in the killed epoch
    # and again in a later one.
    before = {c["operation_id"] for c in final["charges"] if c["epoch"] == kill_epoch}
    after_rows = [c for c in final["charges"] if c["epoch"] > kill_epoch]
    redispatched = sorted(before & {c["operation_id"] for c in after_rows})
    in_flight_ops = [c["operation_id"] for c in unsettled_charges(post, kill_epoch)]
    if breakpoint == "tool_in_flight":
        tool_ok = len(redispatched) == 1 and redispatched == in_flight_ops
    else:
        tool_ok = not redispatched
    # Settled in the killed epoch but never committed to its step: before
    # #332 a parallel sibling whose result waited behind the in-flight call;
    # after it, only one killed in the window between settling and commit.
    uncommitted = settled_uncommitted(post, kill_epoch)
    tool = {
        "redispatched": redispatched,
        "in_flight_at_kill": in_flight_ops,
        "settled_uncommitted_at_kill": uncommitted,
        "passed": len(redispatched) <= 1 and tool_ok,
    }
    if breakpoint == "response_committed" and held_detail.get("step_id"):
        prefix = f"{held_detail['step_id']}-t"
        planned = len(held_detail["tool_calls"] or [])
        counts: dict[str, int] = {}
        for c in final["charges"]:
            if c["operation_id"].startswith(prefix):
                counts[c["operation_id"]] = counts.get(c["operation_id"], 0) + 1
        tool["pending_dispatches"] = counts
        tool["pending_planned"] = planned
        tool["passed"] = tool["passed"] and all(n == 1 for n in counts.values())

    # 3. evidence committed before the kill is kept unchanged, and the
    # adopted part of it is citable by the conclusion. A non-adopted record
    # (a tool error) is kept as history and is never citable, killed or not.
    kept_before = committed_evidence(post)
    kept_after = committed_evidence(final)
    adopted = {e["evidence_id"] for e in post["evidence"] if e.get("adopted", True)}
    citable = final.get("conclusion_evidence_ids")
    missing = sorted(k for k, v in kept_before.items() if kept_after.get(k) != v)
    should_cite = set(kept_before) & adopted
    not_citable = (
        sorted(should_cite) if citable is None else sorted(should_cite - set(citable))
    )
    evidence = {
        "before_kill": len(kept_before),
        "adopted_before_kill": len(should_cite),
        "not_adopted_before_kill": sorted(set(kept_before) - adopted),
        "missing_or_changed": missing,
        "not_in_conclusion": not_citable,
        "conclusion_present": citable is not None,
        "passed": bool(should_cite) and not missing and not not_citable,
    }

    # 4. unsettled cost stays occupied.
    if breakpoint == "model_in_flight":
        still = [r for r in killed_unsettled if r in unsettled_reservations(final)]
        cost = {
            "kind": "model_reservation",
            "unsettled_at_kill": killed_unsettled,
            "still_reserved_at_end": still,
            "budget_reserved_at_end": final["run"]["budget_reserved"],
            "passed": bool(still)
            and still == killed_unsettled
            and final["run"]["budget_reserved"] >= len(still),
        }
    elif breakpoint == "tool_in_flight":
        at_kill = {c["dispatch_id"] for c in unsettled_charges(post, kill_epoch)}
        at_end = {
            c["dispatch_id"]
            for c in final["charges"]
            if c["dispatch_id"] in at_kill and c["seconds"] is None
        }
        reserved = sum(
            float(c["reserved"]) for c in final["charges"] if c["dispatch_id"] in at_end
        )
        cost = {
            "kind": "tool_dispatch",
            "unsettled_at_kill": sorted(at_kill),
            "still_unsettled_at_end": sorted(at_end),
            "reserved_seconds_occupied": reserved,
            "tool_operations_used": final["run"]["tool_operations_used"],
            "charge_rows": len(final["charges"]),
            "passed": bool(at_kill)
            and at_end == at_kill
            and final["run"]["tool_operations_used"] == len(final["charges"]),
        }
    else:
        cost = {
            "kind": "none_at_kill",
            "unsettled_reservations_at_kill": killed_unsettled,
            "unsettled_charges_at_kill": in_flight_ops,
            "passed": not killed_unsettled and not in_flight_ops,
        }

    # 5. the new epoch starts within lease expiry + one poll.
    lease_until = post["run"]["lease_until"]
    bound = None
    latency = None
    if lease_until is not None and epoch_started_at is not None:
        expiry = datetime.fromisoformat(lease_until)
        started = datetime.fromisoformat(epoch_started_at)
        bound = (
            expiry + timedelta(seconds=poll_seconds + EPOCH_POLL_SECONDS)
        ).isoformat()
        latency = (started - expiry).total_seconds()
    epoch = {
        "kill_epoch": kill_epoch,
        "final_epoch": final["run"]["epoch"],
        "lease_until_at_kill": lease_until,
        "observed_at": epoch_started_at,
        "seconds_after_lease_expiry": latency,
        "bound": bound,
        "passed": bool(
            bound is not None
            and final["run"]["epoch"] >= kill_epoch + 1
            and datetime.fromisoformat(epoch_started_at or "")
            <= datetime.fromisoformat(bound)
        ),
    }

    run = {
        "final_state": final["run"]["state"],
        "incident_state": final.get("incident_state"),
        "terminal": final["run"]["state"] not in OPEN_STATES,
    }
    criteria = {
        "breakpoint_held": {"passed": held, **held_detail},
        "model_resend": model,
        "tool_redispatch": tool,
        "evidence_kept": evidence,
        "unsettled_cost_kept": cost,
        "new_epoch_timely": epoch,
    }
    return {
        "criteria": criteria,
        "run": run,
        "passed": all(c["passed"] for c in criteria.values()),
    }


# -- processes ----------------------------------------------------------------


def minimal_env(extra: Mapping[str, str]) -> dict[str, str]:
    """PATH and HOME only, plus ``extra`` (secrets stay in env files)."""
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env.update(extra)
    return env


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines (``export`` and quotes allowed); values never logged."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def check_owned_worker(ps_line: str, *, pid: int, parent: int) -> None:
    """Refuse unless ``ps -o pid=,ppid=,command=`` shows our own worker."""
    parts = ps_line.split(None, 2)
    if len(parts) != 3:
        raise RuntimeError(f"PID_UNVERIFIED: {pid}")
    seen_pid, seen_parent, command = int(parts[0]), int(parts[1]), parts[2]
    if seen_pid != pid or pid <= 1:
        raise RuntimeError(f"PID_MISMATCH: {pid}")
    if seen_parent != parent:
        raise RuntimeError(f"PID_NOT_OURS: {pid} parent {seen_parent}")
    if f"-m {WORKER_MODULE}" not in command:
        raise RuntimeError(f"PID_NOT_WORKER: {pid}")
    lowered = command.lower()
    if any(name in lowered for name in ("clash", "mihomo", "verge")):
        raise RuntimeError(f"PID_PROTECTED: {pid}")


def signal_worker(proc: subprocess.Popen[bytes], signum: int) -> None:
    if proc.poll() is not None:
        raise RuntimeError("WORKER_ALREADY_EXITED")
    line = subprocess.run(
        ["ps", "-o", "pid=,ppid=,command=", "-p", str(proc.pid)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    check_owned_worker(line, pid=proc.pid, parent=os.getpid())
    os.kill(proc.pid, signum)


class Ledger:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []

    def event(self, name: str, **detail: Any) -> None:
        record = {"at": datetime.now(timezone.utc).isoformat(), "event": name, **detail}
        self.events.append(record)
        print(json.dumps(record, default=str), flush=True)

    def write(self, name: str, value: Any) -> Path:
        path = self.directory / name
        path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
        return path


def start_worker(
    args: argparse.Namespace, ledger: Ledger, label: str, proxy_url: str
) -> subprocess.Popen[bytes]:
    extra = {
        "OPSPILOT_WORKER_DSN": args.worker_dsn,
        "OPSPILOT_ENV_FILE": str(Path(args.secrets_env_file).resolve()),
        "OPSPILOT_TOOL_PROFILE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": proxy_url,
        "OPSPILOT_WORKER_POLL_SECONDS": str(args.poll_seconds),
        **(
            {"OPSPILOT_TOOL_PARALLELISM": str(args.tool_parallelism)}
            if args.tool_parallelism
            else {}
        ),
        "OPSPILOT_TRACE": "lab" if args.lab_round else "off",
        **read_env_file(Path(args.prometheus_env_file)),
    }
    if args.lab_round:
        from scripts.lab_evidence import lab_project

        extra["LANGSMITH_PROJECT"] = lab_project(args.lab_round)
    log = (ledger.directory / f"worker-{label}.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", WORKER_MODULE],
        cwd=ROOT,
        env=minimal_env(extra),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    ledger.event("worker_started", label=label, pid=proc.pid, env_names=sorted(extra))
    return proc


def submit(args: argparse.Namespace, label: str) -> dict[str, Any]:
    token = Path(args.event_token_file).read_text().strip()
    body = {
        "source": "f2-kill-resume",
        "external_event_id": label,
        "target_id": TARGET_ID,
        "question": args.question,
    }
    request = urllib.request.Request(
        args.web_url.rstrip("/") + "/intake/events",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read())
        result["http_status"] = response.status
    return result


def lock_waiters(conn: Any, relation: str = "opspilot_tool_charges") -> list[int]:
    rows = conn.execute(
        "SELECT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
        "WHERE NOT l.granted AND l.relation=%s::regclass AND a.usename<>current_user",
        (relation,),
    ).fetchall()
    return [r["pid"] for r in rows]


def terminate_role_backends(conn: Any, role: str) -> list[int]:
    rows = conn.execute(
        "SELECT pid, pg_terminate_backend(pid) AS ok FROM pg_stat_activity "
        "WHERE usename=%s AND pid<>pg_backend_pid()",
        (role,),
    ).fetchall()
    return [r["pid"] for r in rows if r["ok"]]


def deepseek_balance(key: str) -> str | None:
    from scripts.m1_context_latency import balance

    try:
        infos = balance(key)["balance_infos"]
        return next(i["total_balance"] for i in infos if i.get("currency") == "CNY")
    except Exception:  # noqa: BLE001 - recorded as unknown
        return None


def trace_roots(
    project: str, run_id: str, secrets: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Root runs of every attempt of ``run_id`` (one trace per process)."""
    from scripts.lab_evidence import langsmith_client, run_url

    env = {**os.environ, "LANGSMITH_API_KEY": secrets.get("LANGSMITH_API_KEY", "")}
    client = langsmith_client(env)
    found: list[dict[str, Any]] = []
    for _ in range(30):
        runs = list(
            client.list_runs(
                project_name=project,
                is_root=True,
                filter=f'and(eq(metadata_key, "run_id"), eq(metadata_value, "{run_id}"))',
            )
        )
        if len(runs) >= 2:
            break
        time.sleep(2)
    for root in sorted(runs, key=lambda r: r.start_time):
        found.append(
            {
                "langsmith_run_id": str(root.id),
                "start_time": _iso(root.start_time),
                "url": run_url(
                    client,
                    project_id=str(root.session_id),
                    langsmith_run_id=str(root.id),
                ),
                "status": root.status,
            }
        )
    return found


# -- the run ------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    import psycopg
    from psycopg.rows import dict_row

    from scripts.f2_stall_proxy import StallProxy

    breakpoint: str = args.breakpoint
    label = f"{breakpoint}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    ledger = Ledger(Path(args.ledger_dir) / label)
    secrets = read_env_file(Path(args.secrets_env_file))
    balance_before = deepseek_balance(secrets.get("DEEPSEEK_API_KEY", ""))
    ledger.event("balance", cny=balance_before)

    project_info = None
    if args.lab_round:
        from scripts.lab_evidence import langsmith_client, prepare_lab_project

        env = {
            "OPSPILOT_TRACE": "lab",
            "LANGSMITH_API_KEY": secrets.get("LANGSMITH_API_KEY", ""),
            **{
                k: v
                for k, v in secrets.items()
                if k.startswith("LANGSMITH_") and k != "LANGSMITH_API_KEY"
            },
        }
        project_info = prepare_lab_project(
            env, args.lab_round, client_factory=lambda: langsmith_client(env)
        )
        ledger.event("lab_project", **(project_info or {}))

    proxy_log = (ledger.directory / "proxy.jsonl").open("w")
    host, port = args.proxy_listen.rsplit(":", 1)
    proxy = StallProxy(
        (host, int(port)), args.upstream_prometheus, count=0, log=proxy_log
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    proxy_url = f"http://{args.proxy_listen}"

    owner = psycopg.connect(args.owner_dsn, autocommit=True, row_factory=dict_row)
    locker = None
    worker_a = start_worker(args, ledger, "a", proxy_url)
    worker_b = None
    timeline: dict[str, Any] = {}
    try:
        intake = submit(args, label)
        ledger.event("submitted", **intake)
        run_id = intake["run_id"]
        deadline = time.monotonic() + args.breakpoint_timeout
        pre: dict[str, Any] | None = None
        candidate: tuple[str, float] | None = None
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError("BREAKPOINT_TIMEOUT")
            if worker_a.poll() is not None:
                raise RuntimeError("WORKER_A_EXITED")
            snap = snapshot(owner, run_id)
            if snap["run"]["state"] not in OPEN_STATES:
                raise RuntimeError(
                    f"RUN_ENDED_BEFORE_BREAKPOINT:{snap['run']['state']}"
                )
            ready = False
            if armed(snap, arm_after_evidence=args.arm_after_evidence):
                if breakpoint == "model_in_flight":
                    reservation = model_in_flight(snap)
                    steps = len(live_steps(snap))
                    if reservation is None:
                        candidate = None
                    elif candidate is None or candidate[0] != f"{reservation}:{steps}":
                        candidate = (f"{reservation}:{steps}", time.monotonic())
                    elif time.monotonic() - candidate[1] >= MODEL_DWELL_SECONDS:
                        ready = True
                elif breakpoint == "response_committed":
                    if locker is None and model_in_flight(snap) is not None:
                        locker = psycopg.connect(args.owner_dsn, row_factory=dict_row)
                        locker.execute(
                            "LOCK TABLE opspilot_tool_charges IN EXCLUSIVE MODE"
                        )
                        timeline["lock_taken"] = snap["db_now"]
                        ledger.event("lock_taken", reservation=model_in_flight(snap))
                    elif locker is not None:
                        if pending_step(snap) is not None and lock_waiters(owner):
                            ready = True
                        elif (
                            model_in_flight(snap) is None and pending_step(snap) is None
                        ):
                            # The answer carried no tools: let it go, arm again.
                            locker.rollback()
                            locker.close()
                            locker = None
                            ledger.event("lock_released_no_tools")
                elif breakpoint == "tool_in_flight":
                    with proxy.lock:
                        if "proxy_armed" not in timeline:
                            proxy.remaining = 1
                            timeline["proxy_armed"] = snap["db_now"]
                            ledger.event("proxy_armed")
                        engaged = proxy.remaining == 0
                    held_op = stall_without_sibling(snap) if engaged else None
                    if held_op is not None:
                        # This stall cannot form breakpoint 3 (no sibling):
                        # never kill on it; re-arm once it has settled.
                        if timeline.get("stall_single") != held_op["dispatch_id"]:
                            timeline["stall_single"] = held_op["dispatch_id"]
                            timeline["stalls_without_sibling"] = (
                                timeline.get("stalls_without_sibling", 0) + 1
                            )
                            ledger.event(
                                "stall_without_sibling",
                                operation_id=held_op["operation_id"],
                            )
                        timeline.pop("stall_seen", None)
                        candidate = None
                        engaged = False
                    elif engaged and "stall_single" in timeline:
                        pending = unsettled_charges(snap, snap["run"]["epoch"])
                        if not any(
                            c["dispatch_id"] == timeline["stall_single"]
                            for c in pending
                        ):
                            with proxy.lock:
                                proxy.remaining = 1
                            del timeline["stall_single"]
                            ledger.event("proxy_rearmed")
                        engaged = False
                    if engaged:
                        timeline.setdefault("stall_seen", time.monotonic())
                        candidate, ready = dwell(
                            candidate,
                            tool_breakpoint_state(snap),
                            time.monotonic(),
                            SIBLING_DWELL_SECONDS,
                        )
                        if (
                            not ready
                            and time.monotonic() - timeline["stall_seen"]
                            > STALL_KILL_BEFORE_SECONDS
                        ):
                            # 未形成断点③: no kill; the summary is not written.
                            raise RuntimeError("BREAKPOINT_3_NOT_FORMED")
            if ready:
                pre = snap
                break
            time.sleep(0.1)

        ledger.write("pre.json", pre)
        signal_worker(worker_a, signal.SIGKILL)
        worker_a.wait(timeout=30)
        timeline["killed"] = datetime.now(timezone.utc).isoformat()
        ledger.event("killed", pid=worker_a.pid, returncode=worker_a.returncode)
        timeline["terminated_backends"] = terminate_role_backends(
            owner, args.worker_role
        )
        if locker is not None:
            locker.rollback()
            locker.close()
            locker = None
            ledger.event("lock_released")
        post = snapshot(owner, run_id)
        ledger.write("post.json", post)
        held, detail = breakpoint_held(breakpoint, pre, post)
        ledger.event("breakpoint_held", held=held, **detail)

        worker_b = start_worker(args, ledger, "b", proxy_url)
        epoch_started_at = None
        last_seen = None
        wait_until = time.monotonic() + args.resume_timeout
        while time.monotonic() < wait_until:
            row = owner.execute(
                "SELECT epoch,state,clock_timestamp() AS now FROM opspilot_runs WHERE run_id=%s",
                (run_id,),
            ).fetchone()
            if row["epoch"] > post["run"]["epoch"]:
                epoch_started_at = _iso(row["now"])
                timeline["epoch_previous_poll"] = last_seen
                break
            last_seen = _iso(row["now"])
            time.sleep(EPOCH_POLL_SECONDS)
        ledger.event("epoch_observed", observed_at=epoch_started_at)
        final = snapshot(owner, run_id)
        while time.monotonic() < wait_until and final["run"]["state"] in OPEN_STATES:
            time.sleep(2)
            final = snapshot(owner, run_id)
        ledger.event("run_ended", state=final["run"]["state"])
        signal_worker(worker_b, signal.SIGTERM)
        worker_b.wait(timeout=90)
        ledger.event("worker_b_stopped", returncode=worker_b.returncode)
        final = snapshot(owner, run_id)
        ledger.write("final.json", final)

        result = verdicts(
            breakpoint,
            pre=pre,
            post=post,
            final=final,
            epoch_started_at=epoch_started_at,
            poll_seconds=args.poll_seconds,
        )
    finally:
        if locker is not None:
            locker.rollback()
            locker.close()
        for proc in (worker_a, worker_b):
            if proc is not None and proc.poll() is None:
                signal_worker(proc, signal.SIGTERM)
                proc.wait(timeout=90)
        proxy.shutdown()
        proxy.server_close()
        proxy_log.close()

    proxy_records = [
        json.loads(line)
        for line in (ledger.directory / "proxy.jsonl").read_text().splitlines()
    ]
    traces: list[dict[str, Any]] = []
    if project_info is not None:
        try:
            traces = trace_roots(project_info["project"], run_id, secrets)
        except Exception as exc:  # noqa: BLE001 - read-back is best effort
            traces = [{"read_back": f"error:{type(exc).__name__}"}]
    balance_after = deepseek_balance(secrets.get("DEEPSEEK_API_KEY", ""))
    ledger.write("events.json", ledger.events)
    raw = json.dumps(
        {
            "pre": pre,
            "post": post,
            "final": final,
            "events": ledger.events,
            "proxy": proxy_records,
        },
        sort_keys=True,
        default=str,
    ).encode()
    summary = {
        "schema": "opspilot-lab-summary-1",
        "experiment": "f2-kill-resume-live",
        "breakpoint": breakpoint,
        "question": args.question,
        "incident_id": intake["incident_id"],
        "run_id": run_id,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "category": "bounded real run (real DeepSeek, real kind OTel Demo lab, real kill -9 of worker_main); not production proof",
        "versions": {
            "commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
            ).stdout.strip(),
            "worker_versions": owner.execute(
                "SELECT versions FROM opspilot_runs WHERE run_id=%s", (run_id,)
            ).fetchone()["versions"],
        },
        "timeline": {**timeline, "submitted": intake},
        "kill": {
            "signal": "SIGKILL",
            "pid": worker_a.pid,
            "returncode": worker_a.returncode,
            "lease_until_at_kill": post["run"]["lease_until"],
            "epoch_at_kill": post["run"]["epoch"],
        },
        "verdicts": result,
        "final_usage": final["run"],
        "proxy": {
            "requests": len(proxy_records),
            "stalled": [r for r in proxy_records if r["stalled"]],
        },
        "trace": {
            "project": None if project_info is None else project_info,
            "roots": traces,
        },
        "cost": {
            "deepseek_balance_cny_before": balance_before,
            "deepseek_balance_cny_after": balance_after,
            "note": "upper bound; the account may have other callers",
        },
        "raw_ledger": {
            "path": str(ledger.directory.relative_to(ROOT))
            if ledger.directory.is_relative_to(ROOT)
            else str(ledger.directory),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "in_repository": False,
        },
    }
    out = Path(args.summary)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    ledger.write("summary.json", summary)
    ledger.event("verdict", passed=result["passed"])
    return 0 if result["passed"] else 1


def rejudge(args: argparse.Namespace) -> int:
    """Recompute the verdicts of a finished run from its ledger snapshots.

    For a harness-side reading fix only: the rows are the ones frozen at
    run time; the summary records that, and when, they were re-read.
    """
    directory = Path(args.ledger)
    pre, post, final = (
        json.loads((directory / f"{name}.json").read_text())
        for name in ("pre", "post", "final")
    )
    backfilled = False
    if args.owner_dsn:
        import psycopg

        with psycopg.connect(args.owner_dsn) as conn:
            for snap in (pre, post, final):
                for e in snap["evidence"]:
                    if "adopted" not in e:
                        e["adopted"] = conn.execute(
                            "SELECT adopted FROM opspilot_evidence WHERE evidence_id=%s",
                            (e["evidence_id"],),
                        ).fetchone()[0]
                        backfilled = True
    summary_path = Path(args.summary)
    summary = json.loads(summary_path.read_text())
    epoch_at = next(
        e.get("observed_at", e.get("at"))
        for e in json.loads((directory / "events.json").read_text())
        if e["event"] == "epoch_observed"
    )
    previous = summary["verdicts"]["passed"]
    summary["verdicts"] = verdicts(
        summary["breakpoint"],
        pre=pre,
        post=post,
        final=final,
        epoch_started_at=epoch_at,
        poll_seconds=args.poll_seconds,
    )
    summary.setdefault("rejudged", []).append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
            ).stdout.strip(),
            "reason": args.reason,
            "passed_before": previous,
            "adopted_backfilled_from_db": backfilled,
        }
    )
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n"
    )
    print(json.dumps({"passed": summary["verdicts"]["passed"]}))
    return 0 if summary["verdicts"]["passed"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--breakpoint", choices=BREAKPOINTS, required=True)
    p.add_argument("--owner-dsn", required=True)
    p.add_argument("--worker-dsn", required=True)
    p.add_argument("--worker-role", default="opspilot_worker_lab")
    p.add_argument("--web-url", required=True)
    p.add_argument("--event-token-file", required=True)
    p.add_argument("--secrets-env-file", required=True)
    p.add_argument("--prometheus-env-file", required=True)
    p.add_argument("--upstream-prometheus", default="http://127.0.0.1:19090")
    p.add_argument("--proxy-listen", default="127.0.0.1:19092")
    p.add_argument("--poll-seconds", type=float, default=2.0)
    p.add_argument(
        "--tool-parallelism",
        type=int,
        default=0,
        help="OPSPILOT_TOOL_PARALLELISM for both workers; 0 keeps the product default",
    )
    p.add_argument("--arm-after-evidence", type=int, default=1)
    p.add_argument("--breakpoint-timeout", type=float, default=900.0)
    p.add_argument("--resume-timeout", type=float, default=2400.0)
    p.add_argument("--lab-round", default="")
    p.add_argument(
        "--question",
        default=QUESTION,
        help="incident question (input only; the product prompt is unchanged)",
    )
    p.add_argument("--ledger-dir", required=True)
    p.add_argument("--summary", required=True)
    r = sub.add_parser("rejudge")
    r.add_argument("--ledger", required=True)
    r.add_argument("--summary", required=True)
    r.add_argument("--reason", required=True)
    r.add_argument(
        "--owner-dsn",
        default="",
        help="fill evidence.adopted for ledgers frozen before it was captured",
    )
    r.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    return run(args) if args.command == "run" else rejudge(args)


if __name__ == "__main__":
    sys.exit(main())
