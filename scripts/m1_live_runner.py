"""Bounded live Flash Run through the real driver: PostgreSQL + runner + events.

Unlike ``m1_live_flash_loop.py`` (in-memory loop only), this drives
``InvestigationRunner`` against the lab PostgreSQL with the workbench event
log and evidence store attached, so one real Run proves ADR-0005 end to end:
a qualified report is published; anything else parks the Run
(``waiting_human``), the incident stays open, ``control()`` still accepts a
follow_up, and the page's event stream ends in ``run_completed`` or
``run_handoff``.

Reads DEEPSEEK_API_KEY from ``M0_ENV_FILE`` (never printed). Evidence follows
ADR-0006 decision 3: the repository gets one frozen ``summary.json`` per Run
under docs/evidence/m1-01-handoff-runner/live-runs/<run_id>/ (or
``M1_ACCEPTANCE_OUT``) -- report, verdicts, counts, cost, trace id and link,
sha256 of the raw ledger -- while the raw ledger itself goes to the ignored
``tmp/lab-ledgers/`` (or ``OPSPILOT_LEDGER_DIR``). Fixture tool, no real OTel.
Not product intake.

``OPSPILOT_TRACE=lab`` exports the Run's spans to LangSmith through
``opspilot.tracing`` (fail-closed: project must be ``opspilot-lab-*``). In
that mode ``--lab-round <name>`` (or ``OPSPILOT_LAB_ROUND``) is required: the
project becomes ``opspilot-lab-<name>`` and is set to the longest retention
before the Run; afterwards the root run is read back and its link frozen. The
``LANGSMITH_*`` entries of ``M0_ENV_FILE`` are loaded into the environment
only where the shell did not already set them; values are never printed.
``OPSPILOT_LAB_DSN`` points at a throwaway PostgreSQL instead of the 55431
lab (``verify_server`` is then skipped: it checks the lab's own pid file).

    .venv/bin/python -m scripts.m0.postgres_lab start
    M0_ENV_FILE=/abs/.env .venv/bin/python scripts/m1_live_runner.py \\
        [--model-requests N] [--follow-up] [--lab-round NAME]

``--model-requests 1`` forces a budget handoff after the first (tool) round.
``--follow-up`` then applies one follow_up control and resumes once more, so
the parked -> queued -> claimed -> parked cycle is recorded as well.
``--deadline-seconds N --sweep`` gives the Run a tiny deadline, lets the real
attempt run into it (its writes are fenced), waits for the deadline to pass
and polls once more: that poll is where the runner sweeps the overdue Run
into a ``DEADLINE_EXCEEDED`` handoff (ADR-0005 decision 2). Ledgers for
that mode go under docs/evidence/m1-01-deadline-sweep/live-runs/.
``--f3-coverage`` (F3 PR-2 R-13) adds an ``f3_coverage`` block to the frozen
summary, read after the Run with the same DSN: the acceptance projection's
coverage and ``collection_truncated`` for the Run, and which of
``#coverage-gaps`` / ``#collection-truncated`` the workbench page rendered
for the incident. ``--f3-answer evals/f3/missing-source/answer.json`` also
computes that scenario's deterministic U6-a verdict.
``--sweep --timeout-follow-up`` then applies one follow_up to the timed-out
Run: control() records the note and starts a fresh Run (user decision
2026-09-25, ``renew_*``) whose input is the C3 continuation of the timed-out
context, and a final poll investigates it for real (renewed wall
``--renew-seconds``). Ledgers go under docs/evidence/m1-01-timeout-followup/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg

from opspilot import tracing
from opspilot.acceptance_investigation import investigation_evidence
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.inputs import continuation_input
from opspilot.investigation.limits import M1_FROZEN_LIMITS
from opspilot.investigation.loop import (
    DISCIPLINE_VARIANT,
    MAX_TOOL_PARALLELISM,
    prompt_revision_versions,
)
from opspilot.investigation.reports import parse_report as parse_report_v2
from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore
from opspilot.tools import TransportResponse
from opspilot.tools.fixture import FIXTURE_TARGET, FIXTURE_TOOL, fixture_face
from opspilot.tools.otel_demo import TARGET_ID as OTEL_TARGET_ID
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    hash_password,
)
from opspilot.web.service import LEASE_SECONDS
from opspilot.worker import Worker
from scripts.lab_evidence import (
    LAB_ROUND_ENV,
    freeze,
    langsmith_client,
    parse_report,
    prepare_lab_project,
    trace_evidence,
)
from scripts.m0.postgres_lab import DSN, verify_server
from scripts.m1_live_flash_loop import (
    QUESTION,
    RecordingClient,
    cost_cny,
    load_langsmith_env,
    read_key,
    resolve_env_file,
)
from tests.m1_tool_support import WINDOW_START, body, build, registration
from tests.m1_web_support import call

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "docs/evidence/m1-01-handoff-runner/live-runs"
SWEEP_OUT_ROOT = ROOT / "docs/evidence/m1-01-deadline-sweep/live-runs"
FOLLOWUP_OUT_ROOT = ROOT / "docs/evidence/m1-01-timeout-followup/live-runs"
# The product fixture profile's tool and sole target: the input recorded
# here is built by the same ``ToolFace`` the workbench records on intake.
LIVE_TOOL = FIXTURE_TOOL
LIVE_TOOL_TARGET = FIXTURE_TARGET
OTEL_QUESTION = (
    "Checkout appears to show elevated errors. Investigate with the authorized "
    "metrics and traces and return a json investigation report for the "
    "authorized window."
)
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


def successor_input(store, incident, run_id, deadline, target_id=LIVE_TOOL_TARGET):
    """The renewed Run's input: the C3 continuation of the timed-out context."""
    return continuation_input(
        store.rebuild(incident),
        new_run_id=str(run_id),
        deadline=deadline,
        authorized_targets=frozenset({target_id}),
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


USAGE_KEYS = ("run_usage", "current_run_id", "current_run_usage")


def run_usages(store, incident, first_run_id) -> dict:
    """Per-Run usage for the evidence: the first Run (``run_usage``) and the
    Run the report and trace belong to (``current_run_*``; the same Run unless
    ``--timeout-follow-up`` renewed it). The raw ledger is not kept, so the
    frozen summary carries both to stay reconcilable (#116)."""
    current = store.rebuild(incident)["run"]["run_id"]
    return {
        "run_usage": store.run_usage(first_run_id),
        "current_run_id": str(current),
        "current_run_usage": store.run_usage(current),
    }


def summary_counts(ledger: dict, model_requests_bound: int, first) -> dict:
    """The ``counts`` block of the frozen summary."""
    return {
        "model_requests_bound": model_requests_bound,
        "http_count": ledger["http_count"],
        "prompt_tokens": ledger["prompt_tokens"],
        "completion_tokens": ledger["completion_tokens"],
        **{key: ledger[key] for key in USAGE_KEYS},
        "evidence_ids": None if first is None else first["evidence_ids"],
    }


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
    parser.add_argument("--lab-round", default=os.environ.get(LAB_ROUND_ENV) or None)
    # #309 evidence: tool calls of one turn in flight (1 = serial, the
    # earlier behaviour of this script), and a narrowed context window so a
    # bounded Run reaches compaction (``RunLimits.within`` admits narrowing).
    parser.add_argument("--tool-parallelism", type=int, default=1)
    parser.add_argument("--context-tokens", type=int, default=None)
    # ``otel-demo`` reads Prometheus/Jaeger at the OPSPILOT_OTEL_* URLs through
    # the product profile (ledger, control, transport); the default keeps
    # this script's canned fixture tool.
    parser.add_argument(
        "--tool-profile", choices=("fixture", "otel-demo"), default="fixture"
    )
    parser.add_argument("--f3-coverage", action="store_true")
    parser.add_argument("--f3-answer", type=Path, default=None)
    args = parser.parse_args()
    if args.f3_answer is not None and not args.f3_coverage:
        raise SystemExit("--f3-answer requires --f3-coverage")
    answer = None
    if args.f3_answer is not None:
        answer_bytes = args.f3_answer.read_bytes()
        answer = {
            "path": str(args.f3_answer),
            "sha256": hashlib.sha256(answer_bytes).hexdigest(),
            "content": json.loads(answer_bytes),
        }
    if not 1 <= args.tool_parallelism <= MAX_TOOL_PARALLELISM:
        raise SystemExit(f"tool-parallelism must be 1-{MAX_TOOL_PARALLELISM}")
    limits = M1_FROZEN_LIMITS
    if args.context_tokens is not None:
        if not (
            M1_FROZEN_LIMITS.output_tokens
            < args.context_tokens
            <= M1_FROZEN_LIMITS.context_tokens
        ):
            raise SystemExit("context-tokens outside (output_tokens, frozen ceiling]")
        limits = replace(M1_FROZEN_LIMITS, context_tokens=args.context_tokens)
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
    if not os.environ.get("OPSPILOT_LAB_DSN"):
        verify_server()
    load_langsmith_env(env_file)
    # Lab mode: prove the lab target first (zero LangSmith calls otherwise),
    # then create the round's project and set retention before the Run.
    project = prepare_lab_project(
        os.environ, args.lab_round, client_factory=langsmith_client
    )
    trace = tracing.configure(os.environ)
    store = DurableStore(DSN)
    store.install()
    log = DurableEventLog(store)
    log.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()

    clock = SystemClock()
    deadline = clock.now() + timedelta(seconds=args.deadline_seconds)
    incident, run_id = uuid4(), uuid4()
    if args.tool_profile == "otel-demo":
        profile = select_profile({**os.environ, PROFILE_ENV: "otel-demo"})
        versions = profile.versions()
        face, target_id, question = profile.face(clock), OTEL_TARGET_ID, OTEL_QUESTION
        factory = profile.executor_factory(store, evidence, clock)
    else:
        versions = dict(VERSIONS)
        face, target_id, question = fixture_face(), LIVE_TOOL_TARGET, QUESTION
        factory = executor_factory_for(evidence, clock, deadline)
    input = face.input_for(
        run_id=str(run_id),
        question=question,
        target_id=target_id,
        deadline=deadline,
        model_requests=args.model_requests,
        limits=limits,
    )
    store.accept(
        incident,
        run_id,
        f"m1-live-runner-{run_id}",
        deadline=deadline,
        budget_limit=args.model_requests,
        versions=versions,
        input=input.as_json(),
    )
    recorder = RecordingClient(DeepSeekClient(key, clock=clock))
    del key
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, dict(versions)),
        model=recorder,
        executor_factory=factory,
        clock=clock,
        lease_seconds=LEASE_SECONDS,
        events=log,
        evidence=evidence,
        tool_parallelism=args.tool_parallelism,
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
        renewed_input = successor_input(
            store, incident, renewed_run, renewed_deadline, target_id
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
    tracing.shutdown()
    f3 = (
        f3_coverage(store, log, evidence, incident, face, versions, answer)
        if args.f3_coverage
        else None
    )
    trace_record = {
        "mode": trace.mode,
        "trace_id": trace.last_trace_id,
        "project": os.environ.get("LANGSMITH_PROJECT") if trace.mode == "lab" else None,
        "dropped_spans": getattr(trace, "dropped_spans", 0),
    }
    usages = [a["usage"] for a in recorder.attempts if isinstance(a.get("usage"), dict)]
    experiment = (
        "m1-01-timeout-followup"
        if args.timeout_follow_up
        else ("m1-01-deadline-sweep" if args.sweep else "m1-01-handoff-runner")
    )
    ledger = {
        "experiment": experiment,
        "deadline_seconds": args.deadline_seconds,
        "tool_parallelism": args.tool_parallelism,
        "context_tokens": limits.context_tokens,
        "tool_profile": args.tool_profile,
        "question": question,
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
        **run_usages(store, incident, run_id),
        "attempts": attempts,
        "control": control,
        "events": _events(log, incident),
        "trace": trace_record,
    }
    out = resolve_out_dir(
        str(run_id), sweep=args.sweep, follow_up_after_timeout=args.timeout_follow_up
    )
    # The report is the last committed conclusion; its digest and evidence
    # ids come from the attempt that produced it, not from the first attempt
    # (Codex review, PR #110 B1).
    first = final_loop(attempts)
    report = None if first is None else runner_report(store, incident)
    # Frozen summary (ADR-0006 decision 3): report, verdicts, counts, cost,
    # trace link; the raw ledger (HTTP attempts, row snapshots, event stream)
    # stays out of the repository and is identified by its sha256.
    frozen, summary_path = freeze(
        ledger,
        experiment=experiment,
        run_id=str(run_id),
        evidence_dir=out,
        summary={
            "incident_id": str(incident),
            "driver": ledger["driver"],
            "model": ledger["model"],
            "started": ledger["started"],
            "ended": ledger["ended"],
            "tool_parallelism": ledger["tool_parallelism"],
            "context_tokens": ledger["context_tokens"],
            "tool_profile": ledger["tool_profile"],
            "verdicts": {
                "status": attempts[0]["outcome"]["status"],
                "reason": attempts[0]["outcome"]["reason"],
                "last_status": attempts[-1]["outcome"]["status"],
                "last_reason": attempts[-1]["outcome"]["reason"],
                "run_state": attempts[-1]["rows_after"]["run_state"],
                "conclusion_published": attempts[-1]["rows_after"][
                    "conclusion_published"
                ],
                "attempts": [
                    {
                        "label": a["label"],
                        "status": a["outcome"]["status"],
                        "reason": a["outcome"]["reason"],
                        "handoff_reasons": (a["outcome"]["loop"] or {}).get(
                            "handoff_reasons"
                        ),
                    }
                    for a in attempts
                ],
                "control": None
                if control is None
                else {k: v for k, v in control.items() if k != "rows_after"},
                "event_kinds": [e["kind"] for e in ledger["events"]],
            },
            "counts": summary_counts(ledger, args.model_requests, first),
            "cost": {"known_cost_cny_upper": ledger["known_cost_cny_upper"]},
            "report": parse_report(report),
            "report_content_sha256": None
            if first is None
            else first["report_content_sha256"],
            "trace": trace_evidence(
                trace_record,
                run_id=str(run_id),
                project=project,
                client_factory=langsmith_client,
            ),
            **({} if f3 is None else {"f3_coverage": f3}),
        },
    )
    summary = {
        **frozen["verdicts"],
        "http_count": ledger["http_count"],
        "known_cost_cny_upper": ledger["known_cost_cny_upper"],
        "trace": frozen["trace"],
        "f3_coverage": frozen.get("f3_coverage"),
        "ledger_sha256": frozen["raw_ledger"]["sha256"],
        "summary": str(summary_path.relative_to(ROOT))
        if summary_path.is_relative_to(ROOT)
        else str(summary_path),
    }
    print(json.dumps(summary, default=str))
    return 0


_ROW = re.compile(r'data-source="([^"]*)" data-state="([^"]*)"')


def page_elements(app, incident) -> dict:
    """Render the incident page once through the workbench route and record
    which F3 step-3 elements it carries (R-13). A throwaway UI password is
    generated and hashed in process; it is never printed or stored."""
    html = call(app["app"], "GET", f"/incidents/{incident}", headers=app["headers"])
    text = html.text
    truncated = re.search(r'<p id="collection-truncated">(.*?)</p>', text, re.S)
    return {
        "http_status": html.status,
        "coverage_gaps": 'id="coverage-gaps"' in text,
        "coverage_rows": [
            {"source": source, "state": state} for source, state in _ROW.findall(text)
        ],
        "collection_truncated": truncated is not None,
        "collection_truncated_text": None
        if truncated is None
        else re.sub(r"<[^>]+>", "", truncated.group(1)).strip(),
        "not_integrated": 'id="not-integrated"' in text,
    }


def _workbench_app(store, log, evidence, face, versions) -> dict:
    import base64

    ledger = DurableWebLedger(store)
    ledger.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=log,
        evidence=evidence,
        ledger=ledger,
        run_versions=versions,
        run_seconds=600,
        tool_face=face,
    )
    password = secrets.token_urlsafe(24)
    auth = Authenticator(
        AuthConfig(
            ui_users={"f3-reader": hash_password(password)},
            event_tokens={},
            auth_revision="f3-pr2-page-check",
            allowed_origins=frozenset(),
        )
    )
    token = base64.b64encode(f"f3-reader:{password}".encode()).decode()
    return {
        "app": create_app(workbench, auth, DurableClock(store)),
        "headers": {"authorization": f"Basic {token}"},
    }


def _signature_match(view, signature) -> bool:
    """D7-a: the cited view is an ok result of the signature's tool with an
    error span on one of the named services."""
    if (
        view.get("tool") != signature["tool"]
        or view.get("status") != signature["status"]
    ):
        return False
    services = set(signature["error_span_services_any_of"])
    found = 0
    stack = [view.get("content")]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("service") in services and item.get("error_by_visible_tags"):
                found += 1
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found >= signature["min_matches"]


def u6_verdict(conn, run_id, report_content, coverage, answer) -> dict:
    """F3 U6-a over the frozen answer: (not supported, or no fact cites an
    evidence matching the signature) and the required source is a gap."""
    expected = answer["content"]
    report, reason = parse_report_v2(report_content, finish_reason="stop")
    required = expected["required_source"]
    row = next((r for r in coverage["sources"] if r["name"] == required), None)
    fact_ids = (
        []
        if report is None
        else [eid for c in report.claims if c.kind == "fact" for eid in c.evidence_ids]
    )
    views = {}
    if fact_ids:
        with conn.cursor() as cursor:
            for evidence_id, view in cursor.execute(
                "SELECT evidence_id,view FROM opspilot_evidence"
                " WHERE evidence_id=ANY(%s) AND run_id=%s",
                (fact_ids, str(run_id)),
            ).fetchall():
                views[evidence_id] = view
    matching = sorted(
        eid
        for eid, view in views.items()
        if _signature_match(view, expected["signature"])
    )
    supported = report is not None and report.conclusion == "supported"
    gap = row is not None and row["gap"]
    gaps_text = [] if report is None else list(report.gaps)
    return {
        "answer_path": answer["path"],
        "answer_sha256": answer["sha256"],
        "report_parsed": report is not None,
        "report_parse_reason": reason,
        "conclusion": None if report is None else report.conclusion,
        "assessment_status": None if report is None else report.assessment_status,
        "fact_evidence_matching_signature": matching,
        "required_source": required,
        "required_source_state": None if row is None else row["state"],
        "required_source_gap": gap,
        "passed": report is not None and (not supported or not matching) and gap,
        # Recorded as observed, not a gate (contract section 8).
        "model_gaps_name_traces": any(
            word in text.lower() for text in gaps_text for word in ("trace", "jaeger")
        ),
    }


def _coverage_json(entry) -> dict:
    return {
        "run_id": str(entry.run_id),
        "sources_recorded": entry.sources_recorded,
        "duplicates_skipped": entry.duplicates_skipped,
        "sources": [
            {
                "name": row.name,
                "ok": row.ok,
                "no_data": row.no_data,
                "non_ok": row.non_ok,
                "reasons": dict(row.reasons),
                "state": row.state,
                "gap": row.gap,
            }
            for row in entry.sources
        ],
    }


def f3_coverage(store, log, evidence, incident, face, versions, answer) -> dict:
    """R-13: the Run's coverage and truncation as the acceptance projection
    and the workbench page show them, read in one READ ONLY transaction."""
    current = store.rebuild(incident)["run"]["run_id"]
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        projected = investigation_evidence(conn, incident)
        coverage = next(
            (_coverage_json(c) for c in projected.coverage if c.run_id == current),
            None,
        )
        reports = [r for r in projected.reports if r.run_id == current]
        verdict = None
        if answer is not None and coverage is not None:
            verdict = u6_verdict(
                conn, current, runner_report(store, incident), coverage, answer
            )
        conn.rollback()
    app = _workbench_app(store, log, evidence, face, versions)
    return {
        "run_id": str(current),
        "coverage": coverage,
        "report_collection_truncated": [r.collection_truncated for r in reports],
        "page": page_elements(app, incident),
        "u6": verdict,
    }


def final_loop(attempts) -> dict | None:
    """The loop outcome of the last attempt that ran the loop, if any."""
    for attempt in reversed(attempts):
        loop = attempt["outcome"]["loop"]
        if loop is not None:
            return loop
    return None


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
