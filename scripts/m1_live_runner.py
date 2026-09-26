"""Bounded live Flash Run through the real driver: PostgreSQL + runner + events.

Unlike ``m1_live_flash_loop.py`` (in-memory loop only), this drives
``InvestigationRunner`` against the lab PostgreSQL with the workbench event
log and evidence store attached, so one real Run proves ADR-0005 end to end:
a qualified report is published; anything else parks the Run
(``waiting_human``), the incident stays open, ``control()`` still accepts a
follow_up, and the page's event stream ends in ``run_completed`` or
``run_handoff``.

Reads DEEPSEEK_API_KEY from ``M0_ENV_FILE`` (never printed) and writes a
business ledger under docs/evidence/m1-01-handoff-runner/live-runs/<run_id>/
(or ``M1_ACCEPTANCE_OUT``). Fixture tool, no real OTel. Not product intake.

    .venv/bin/python -m scripts.m0.postgres_lab start
    M0_ENV_FILE=/abs/.env .venv/bin/python scripts/m1_live_runner.py \\
        [--model-requests N] [--follow-up]

``--model-requests 1`` forces a budget handoff after the first (tool) round.
``--follow-up`` then applies one follow_up control and resumes once more, so
the parked -> queued -> claimed -> parked cycle is recorded as well.
``--deadline-seconds N --sweep`` gives the Run a tiny deadline, lets the real
attempt run into it (its writes are fenced), waits for the deadline to pass
and polls once more: that poll is where the runner sweeps the overdue Run
into a ``DEADLINE_EXCEEDED`` handoff (ADR-0005 decision 2). Ledgers for
that mode go under docs/evidence/m1-01-deadline-sweep/live-runs/.
``--sweep --timeout-follow-up`` then applies one follow_up to the timed-out
Run: control() records the note and starts a fresh Run (user decision
2026-09-25, ``renew_*``) whose input is the C3 continuation of the timed-out
context, and a final poll investigates it for real (renewed wall
``--renew-seconds``). Ledgers go under docs/evidence/m1-01-timeout-followup/.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.context import InvestigationInput, continuation_context
from opspilot.investigation.limits import M1_FROZEN_LIMITS
from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore
from opspilot.tools import TransportResponse
from opspilot.web import DurableEventLog, DurableEvidenceStore
from opspilot.web.service import LEASE_SECONDS
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN, verify_server
from scripts.m1_live_flash_loop import (
    LIVE_TOOL,
    QUESTION,
    TOOL_SCHEMAS,
    RecordingClient,
    cost_cny,
    live_evidence_context,
    read_key,
    resolve_env_file,
)
from tests.m1_tool_support import WINDOW_START, body, build, registration

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "docs/evidence/m1-01-handoff-runner/live-runs"
SWEEP_OUT_ROOT = ROOT / "docs/evidence/m1-01-deadline-sweep/live-runs"
FOLLOWUP_OUT_ROOT = ROOT / "docs/evidence/m1-01-timeout-followup/live-runs"
# The fixture registration's sole target (tests.m1_tool_support.registration).
LIVE_TOOL_TARGET = "checkout-prod"
VERSIONS = {
    **prompt_revision_versions(DISCIPLINE_VARIANT),
    "tool_schema_revision": "live-runner-1",
}


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()


def resolve_out_dir(
    run_id: str, *, sweep: bool = False, follow_up_after_timeout: bool = False
) -> Path:
    explicit = os.environ.get("M1_ACCEPTANCE_OUT")
    if explicit:
        return Path(explicit).expanduser() / run_id
    if follow_up_after_timeout:
        return FOLLOWUP_OUT_ROOT / run_id
    return (SWEEP_OUT_ROOT if sweep else OUT_ROOT) / run_id


def successor_input(store, incident, run_id):
    """The renewed Run's input: the C3 continuation of the timed-out context."""
    snapshot = store.rebuild(incident)
    previous = InvestigationInput.from_json(snapshot["run"]["input"])
    cont = continuation_context(
        snapshot,
        new_run_id=str(run_id),
        authorized_targets=frozenset({LIVE_TOOL_TARGET}),
    )
    return replace(
        previous,
        question=f"{previous.question}\n\n{cont.handoff_note}",
        evidence_context=cont.evidence_context,
    )


def _db_now(store):
    with store.transaction(snapshot=True) as conn:
        return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def executor_factory_for(evidence, clock, deadline):
    def factory(lease, input):
        # A renewed Run carries its own wall in scope_facts; the tool gateway
        # must not keep refusing on the timed-out Run's deadline.
        recorded = input.scope_facts.get("deadline")
        run_deadline = (
            datetime.fromisoformat(recorded) if isinstance(recorded, str) else deadline
        )
        executor, transport, _sink, _ = build(
            clock=clock,
            sink=evidence,
            registrations=[registration(name=LIVE_TOOL)],
            # The control generation stays the fixture's default: the tool
            # gateway checks it against its own fixed control snapshot, not
            # the incident row (that fence is the store's). Overriding it
            # denies every query with CONTROL_GENERATION_CHANGED (live run
            # f987b4b6, 2026-09-24).
            scope_overrides={
                "deadline": run_deadline,
                "tool_names": frozenset({LIVE_TOOL}),
                "run_id": str(lease.run_id),
                "subject_id": str(lease.incident_id),
            },
        )
        transport.response = TransportResponse(
            body=body([{"metric": "http_errors_rate", "value": 0.042}]),
            data_as_of=WINDOW_START,
        )
        return executor

    return factory


def _rows(store, incident):
    rows = store.rebuild(incident)
    return {
        "incident_state": rows["state"],
        "control_generation": rows["control_generation"],
        "run_state": rows["run"]["state"],
        "owner": None if rows["run"]["owner"] is None else "set",
        "lease_until": None
        if rows["run"]["lease_until"] is None
        else rows["run"]["lease_until"].isoformat(),
        "conclusion_published": rows["conclusion"] is not None,
        "steps": [
            {
                "logical_key": s["logical_key"],
                "status": s["status"],
                "kind": (s["response"] or {}).get("kind"),
                "control_generation": s["control_generation"],
            }
            for s in rows["steps"]
        ],
    }


def _events(log, incident):
    return [
        {"sequence": e.sequence, "kind": e.kind, "payload": dict(e.payload)}
        for e in log.read_after(incident, 0, limit=1000)
    ]


def _outcome(outcome):
    loop = outcome.loop
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "epoch": outcome.epoch,
        "replayed_tools": outcome.replayed_tools,
        "loop": None
        if loop is None
        else {
            "execution": loop.execution,
            "handoff": loop.handoff,
            "handoff_reasons": list(loop.handoff_reasons),
            "report_schema_version": None
            if loop.report is None
            else loop.report.schema_version,
            "report_content_sha256": loop.report_content_sha256,
            "evidence_ids": list(loop.evidence_ids),
            "model_requests_used": loop.model_requests_used,
            "steps_committed": loop.steps_committed,
            "prompt_revision": loop.prompt_revision,
            "question_sha256": loop.question_sha256,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-requests", type=int, default=2)
    parser.add_argument("--follow-up", action="store_true")
    parser.add_argument("--deadline-seconds", type=int, default=12 * 60)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--timeout-follow-up", action="store_true")
    parser.add_argument("--renew-seconds", type=int, default=12 * 60)
    args = parser.parse_args()
    if args.timeout_follow_up and not args.sweep:
        raise SystemExit("--timeout-follow-up requires --sweep")
    if args.deadline_seconds < 1:
        raise SystemExit("deadline-seconds must be positive")
    if not 1 <= args.model_requests <= M1_FROZEN_LIMITS.model_requests:
        raise SystemExit("model-requests outside the frozen ceiling")
    env_file = resolve_env_file()
    key = read_key(env_file)
    if not key:
        print(json.dumps({"status": "failed", "failure": "credential unavailable"}))
        return 2
    verify_server()
    store = DurableStore(DSN)
    store.install()
    log = DurableEventLog(store)
    log.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()

    clock = SystemClock()
    deadline = clock.now() + timedelta(seconds=args.deadline_seconds)
    incident, run_id = uuid4(), uuid4()
    input = InvestigationInput(
        question=QUESTION,
        model_requests=args.model_requests,
        limits=M1_FROZEN_LIMITS,
        tool_schemas=tuple(TOOL_SCHEMAS),
        evidence_context=live_evidence_context(str(run_id)),
        variant_id=DISCIPLINE_VARIANT,
        scope_facts={"deadline": deadline.isoformat()},
    )
    store.accept(
        incident,
        run_id,
        f"m1-live-runner-{run_id}",
        deadline=deadline,
        budget_limit=args.model_requests,
        versions=VERSIONS,
        input=input.as_json(),
    )
    recorder = RecordingClient(DeepSeekClient(key, clock=clock))
    del key
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, dict(VERSIONS)),
        model=recorder,
        executor_factory=executor_factory_for(evidence, clock, deadline),
        clock=clock,
        lease_seconds=LEASE_SECONDS,
        events=log,
        evidence=evidence,
    )
    started = clock.now()
    attempts = [{"label": "first", "outcome": _outcome(runner.resume(incident))}]
    attempts[-1]["rows_after"] = _rows(store, incident)
    control = None
    if args.follow_up and attempts[-1]["outcome"]["status"] == "handed_off":
        generation = store.rebuild(incident)["control_generation"]
        applied = store.control(
            incident,
            generation,
            "follow_up",
            "live-runner-operator",
            {"question": "Also compare against the previous hour."},
        )
        control = {
            "action": "follow_up",
            "expected_generation": generation,
            "resulting_generation": applied,
            "rows_after": _rows(store, incident),
        }
        attempts.append(
            {"label": "after-follow-up", "outcome": _outcome(runner.resume(incident))}
        )
        attempts[-1]["rows_after"] = _rows(store, incident)
    if args.sweep:
        # Wait on the database clock, the one the sweep reads.
        while store.rebuild(incident)["run"]["deadline"] > _db_now(store):
            time.sleep(0.5)
        attempts.append(
            {"label": "sweep", "outcome": _outcome(runner.resume(incident))}
        )
        attempts[-1]["rows_after"] = _rows(store, incident)
    if (
        args.timeout_follow_up
        and attempts[-1]["rows_after"]["run_state"] == "waiting_human"
    ):
        generation = store.rebuild(incident)["control_generation"]
        renewed_run = uuid4()
        renewed_deadline = clock.now() + timedelta(seconds=args.renew_seconds)
        renewed_input = replace(
            successor_input(store, incident, renewed_run),
            scope_facts={"deadline": renewed_deadline.isoformat()},
        )
        applied = store.control(
            incident,
            generation,
            "follow_up",
            "live-runner-operator",
            {"text": "Also compare against the previous hour.", "channel": "web"},
            renew_run_id=renewed_run,
            renew_deadline=renewed_deadline,
            renew_input=renewed_input.as_json(),
        )
        control = {
            "action": "follow_up",
            "expected_generation": generation,
            "resulting_generation": applied,
            "renewed_run_id": str(renewed_run),
            "renewed_deadline": renewed_deadline.isoformat(),
            "rows_after": _rows(store, incident),
        }
        attempts.append(
            {
                "label": "after-timeout-follow-up",
                "outcome": _outcome(runner.resume(incident)),
            }
        )
        attempts[-1]["rows_after"] = _rows(store, incident)
    ended = clock.now()
    usages = [a["usage"] for a in recorder.attempts if isinstance(a.get("usage"), dict)]
    ledger = {
        "experiment": "m1-01-timeout-followup"
        if args.timeout_follow_up
        else ("m1-01-deadline-sweep" if args.sweep else "m1-01-handoff-runner"),
        "deadline_seconds": args.deadline_seconds,
        "driver": "opspilot.investigation.runner.InvestigationRunner",
        "incident_id": str(incident),
        "run_id": str(run_id),
        "ledger_id": str(uuid4()),
        "started": started.isoformat(),
        "ended": ended.isoformat(),
        "model": "deepseek-flash",
        "model_requests_bound": args.model_requests,
        "http_count": len(recorder.attempts),
        "attempts_http": recorder.attempts,
        "known_cost_cny_upper": round(sum(cost_cny(u) for u in usages), 6),
        "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
        "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in usages),
        "run_usage": store.run_usage(run_id),
        "current_run_id": str(store.rebuild(incident)["run"]["run_id"]),
        "current_run_usage": store.run_usage(store.rebuild(incident)["run"]["run_id"]),
        "attempts": attempts,
        "control": control,
        "events": _events(log, incident),
    }
    out = resolve_out_dir(
        str(run_id), sweep=args.sweep, follow_up_after_timeout=args.timeout_follow_up
    )
    out.mkdir(parents=True, exist_ok=False)
    (out / "ledger.json").write_text(
        json.dumps(ledger, indent=2, ensure_ascii=False, default=str)
    )
    first = attempts[0]["outcome"]["loop"]
    report = None if first is None else runner_report(store, incident)
    if report is not None:
        (out / "report.json").write_text(report)
    summary = {
        "status": attempts[0]["outcome"]["status"],
        "reason": attempts[0]["outcome"]["reason"],
        "last_status": attempts[-1]["outcome"]["status"],
        "last_reason": attempts[-1]["outcome"]["reason"],
        "run_state": attempts[-1]["rows_after"]["run_state"],
        "conclusion_published": attempts[-1]["rows_after"]["conclusion_published"],
        "last_event": ledger["events"][-1]["kind"] if ledger["events"] else None,
        "http_count": ledger["http_count"],
        "known_cost_cny_upper": ledger["known_cost_cny_upper"],
        "out_dir": str(out.relative_to(ROOT)) if out.is_relative_to(ROOT) else str(out),
    }
    print(json.dumps(summary))
    return 0


def runner_report(store, incident) -> str | None:
    """The last committed report content (published or handoff), if any."""
    for step in reversed(store.rebuild(incident)["steps"]):
        response = step["response"] or {}
        if response.get("kind") == "conclusion":
            content = response["conclusion"].get("report_content")
            return None if content is None else str(content)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
