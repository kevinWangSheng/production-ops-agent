"""Bounded live Flash investigation: product loop + fixture tools + usage ledger.

Reads DEEPSEEK_API_KEY from a private env file, never prints it. Evidence
follows ADR-0006 decision 3: one frozen ``summary.json`` per Run under
docs/evidence/m1-01-acceptance/live-runs/<run_id>/ (or M1_ACCEPTANCE_OUT),
carrying the report, the acceptance verdict, counts, cost, trace link and the
sha256 of the raw ledger, which goes to the ignored ``tmp/lab-ledgers/`` (or
``OPSPILOT_LEDGER_DIR``). Each Run gets its own directory so earlier evidence
is never overwritten. This is not product intake wiring.

``OPSPILOT_TRACE=lab`` with ``--lab-round <name>`` (or ``OPSPILOT_LAB_ROUND``)
exports the Run to the LangSmith project ``opspilot-lab-<name>`` (set to the
longest retention first) under one root span, as the runner does.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from opspilot import tracing
from opspilot.acceptance import IncidentScenario, outcome_from_loop
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import (
    DISCIPLINE_VARIANT,
    InvestigationLoop,
    InvestigationRequest,
    ModelCall,
    ModelError,
    ModelReply,
    prompt_revision_versions,
)
from opspilot.investigation.store import MemoryStepStore
from opspilot.tools import TransportResponse
from scripts.lab_evidence import (
    LAB_ROUND_ENV,
    freeze,
    langsmith_client,
    parse_report,
    prepare_lab_project,
    trace_evidence,
)
from tests.m1_tool_support import (
    WINDOW_START,
    body,
    build,
    historical_window_context,
    registration,
)

LIVE_TOOL = "metrics_range_query"
TOOL_SCHEMAS = (
    {
        "type": "function",
        "function": {
            "name": LIVE_TOOL,
            "description": (
                "Return a projected Prometheus range query for one registered "
                "target inside the authorized window. The view is sampled: a "
                "missing series is unknown, not zero. Listing a series does "
                "not prove health. Available expressions: "
                '["rate(http_errors[5m])"]; any other value returns an error. '
                "At most 512 view bytes; truncated views set truncated true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expr": {
                        "type": "string",
                        "description": "Exact PromQL string from the available list.",
                    }
                },
                "required": ["expr"],
            },
        },
    },
)

QUESTION = (
    "Checkout appears to show elevated HTTP errors. Query the authorized "
    "metrics and return a json investigation report for the authorized window."
)

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "docs/evidence/m1-01-acceptance/live-runs"
CNY_PER_USD = 7.3
INPUT_USD_PER_M = 0.3
OUTPUT_USD_PER_M = 1.2


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()


class RecordingClient:
    def __init__(self, inner: DeepSeekClient):
        self.inner = inner
        self.attempts: list[dict] = []

    def complete(self, call: ModelCall) -> ModelReply:
        started = time.monotonic()
        try:
            reply = self.inner.complete(call)
        except ModelError as exc:
            self.attempts.append(
                {
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": exc.code,
                    "usage": None,
                    "json_mode": call.json_mode,
                }
            )
            raise
        self.attempts.append(
            {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "finish_reason": reply.finish_reason,
                "response_model": reply.response_model,
                "usage": dict(reply.usage),
                "tool_call_count": len(reply.tool_calls),
                "json_mode": call.json_mode,
            }
        )
        return reply


def read_key(path: Path) -> str:
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


def resolve_env_file() -> Path:
    """Only M0_ENV_FILE is trusted; no cross-worktree fallback paths."""
    explicit = os.environ.get("M0_ENV_FILE")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path.resolve()
    raise SystemExit("credential file unavailable: set M0_ENV_FILE")


def resolve_out_dir(run_id: str) -> Path:
    explicit = os.environ.get("M1_ACCEPTANCE_OUT")
    if explicit:
        return Path(explicit).expanduser() / run_id
    return OUT_ROOT / run_id


def cost_cny(usage: dict) -> float:
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    usd = (
        prompt * INPUT_USD_PER_M / 1_000_000 + completion * OUTPUT_USD_PER_M / 1_000_000
    )
    return round(usd * CNY_PER_USD, 6)


def live_evidence_context(run_id: str) -> dict:
    """The context a live Run cites, keyed to that Run's own ``run_id``.

    The loop binds each delivered view to the time policies it is eligible
    for (``eligible_time_policies``) and fails closed: a policy without
    ``mode`` and ``window`` is never worn by a view, so every fact citing it
    is REPORT_INVALID even when the model followed the report contract. The
    context must carry this Run's own ``run_id`` or the loop's projection
    discards it wholesale (bot review finding, PR #29), and the fixture view
    is judged against the response-received instant. Share the fixture
    builder with the loop doubles instead of hand-writing the context here.
    """
    return historical_window_context(run_id, reference_rule="response_received_at")


def build_run(model, *, clock, deadline, run_id, evidence_context=None):
    """The product loop over the fixture Prometheus tool, exactly as a live Run.

    Shared with the offline replay tests so the evidence context the model is
    asked to cite is the same one the loop binds reports against.
    """
    executor, transport, _sink, _ = build(
        clock=clock,
        registrations=[registration(name=LIVE_TOOL)],
        scope_overrides={
            "deadline": deadline,
            "tool_names": frozenset({LIVE_TOOL}),
            "run_id": run_id,
        },
    )
    transport.response = TransportResponse(
        body=body([{"metric": "http_errors_rate", "value": 0.042}]),
        data_as_of=WINDOW_START,
    )
    store = MemoryStepStore(
        budget_limit=4, deadline=deadline, clock=clock, run_id=run_id
    )
    loop = InvestigationLoop(model=model, executor=executor, store=store, clock=clock)
    request = InvestigationRequest(
        run_id=executor.scope.run_id,
        question=QUESTION,
        scope=executor.scope,
        tool_schemas=TOOL_SCHEMAS,
        model_requests=2,
        evidence_context=(
            live_evidence_context(run_id)
            if evidence_context is None
            else evidence_context
        ),
    )
    return loop, request


LANGSMITH_KEYS = (
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_WORKSPACE_ID",
)


def read_named(path: Path, name: str) -> str:
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


def load_langsmith_env(path: Path) -> list[str]:
    """Copy the ``LANGSMITH_*`` entries the shell left unset; return the names."""
    loaded = []
    for name in LANGSMITH_KEYS:
        if os.environ.get(name):
            continue
        value = read_named(path, name)
        if value:
            os.environ[name] = value
            loaded.append(name)
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab-round", default=os.environ.get(LAB_ROUND_ENV) or None)
    args = parser.parse_args()
    env_file = resolve_env_file()
    key = read_key(env_file)
    if not key:
        print(
            json.dumps(
                {"status": "failed", "failure": "trusted credential unavailable"}
            )
        )
        return 2
    load_langsmith_env(env_file)
    # Lab mode: prove the lab target first (zero LangSmith calls otherwise),
    # then create the round's project and set retention before the Run.
    project = prepare_lab_project(
        os.environ, args.lab_round, client_factory=langsmith_client
    )
    trace = tracing.configure(os.environ)
    clock = SystemClock()
    deadline = clock.now() + timedelta(minutes=12)
    run_id = str(uuid4())
    recorder = RecordingClient(DeepSeekClient(key, clock=clock))
    del key
    loop, request = build_run(recorder, clock=clock, deadline=deadline, run_id=run_id)
    scenario = IncidentScenario(
        scenario_id=f"m1-01-real-{run_id}",
        feature_id="F3",
        acceptance_step="external IncidentScenario -> IncidentOutcome",
        kind="real-deepseek",
        subject_id="incident-acceptance",
    )
    started = clock.now()
    # One root span for the Run, as ``InvestigationRunner.resume`` opens one
    # per attempt; the loop's model and tool spans attach to it in lab mode.
    with tracing.tracer().run(
        incident_id=scenario.subject_id,
        run_id=run_id,
        versions=prompt_revision_versions(DISCIPLINE_VARIANT),
    ) as span:
        outcome = loop.run(request)
        span.settle(
            outcome.execution,
            outcome.handoff_reasons[0] if outcome.handoff_reasons else None,
        )
    acceptance_outcome = outcome_from_loop(scenario, outcome)
    ended = clock.now()
    tracing.shutdown()
    trace_record = {
        "mode": trace.mode,
        "trace_id": trace.last_trace_id,
        "project": os.environ.get("LANGSMITH_PROJECT") if trace.mode == "lab" else None,
        "dropped_spans": getattr(trace, "dropped_spans", 0),
    }
    usages = [
        item["usage"]
        for item in recorder.attempts
        if isinstance(item.get("usage"), dict)
    ]
    ledger = {
        "experiment": "m1-01-flash-loop",
        "run_id": request.run_id,
        "ledger_id": str(uuid4()),
        "started": started.isoformat(),
        "ended": ended.isoformat(),
        "model": "deepseek-flash",
        "http_count": len(recorder.attempts),
        "attempts": recorder.attempts,
        "known_cost_cny_upper": round(sum(cost_cny(u) for u in usages), 6),
        "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
        "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in usages),
        "execution": outcome.execution,
        "handoff": outcome.handoff,
        "handoff_reasons": list(outcome.handoff_reasons),
        "report_schema_version": (
            outcome.report.schema_version if outcome.report is not None else None
        ),
        "report_content_sha256": outcome.report_content_sha256,
        "evidence_ids": list(outcome.evidence_ids),
        "steps_committed": outcome.steps_committed,
        "prompt_revision": outcome.prompt_revision,
        "question_sha256": outcome.question_sha256,
        "trace": trace_record,
    }
    OUT = resolve_out_dir(run_id)
    # Frozen summary (ADR-0006 decision 3); the raw ledger with every HTTP
    # attempt stays out of the repository, identified by its sha256.
    frozen, summary_path = freeze(
        ledger,
        experiment=ledger["experiment"],
        run_id=run_id,
        evidence_dir=OUT,
        summary={
            "model": ledger["model"],
            "started": ledger["started"],
            "ended": ledger["ended"],
            "verdicts": {
                "status": outcome.execution,
                "handoff": outcome.handoff,
                "handoff_reasons": list(outcome.handoff_reasons),
                "acceptance_outcome": {
                    "scenario_id": acceptance_outcome.scenario_id,
                    "final_state": acceptance_outcome.final_state,
                    "evidence_ids": list(acceptance_outcome.evidence_ids),
                    "decision": acceptance_outcome.decision,
                    "actions": list(acceptance_outcome.actions),
                    "permissions": list(acceptance_outcome.permissions),
                    "human_interaction": acceptance_outcome.human_interaction,
                    "handoff_reasons": list(acceptance_outcome.handoff_reasons),
                    "report_available": acceptance_outcome.report_available,
                },
            },
            "counts": {
                "http_count": ledger["http_count"],
                "prompt_tokens": ledger["prompt_tokens"],
                "completion_tokens": ledger["completion_tokens"],
                "steps_committed": outcome.steps_committed,
                "evidence_ids": list(outcome.evidence_ids),
            },
            "cost": {"known_cost_cny_upper": ledger["known_cost_cny_upper"]},
            "report": parse_report(outcome.report_content),
            "report_schema_version": ledger["report_schema_version"],
            "report_content_sha256": outcome.report_content_sha256,
            "prompt_revision": outcome.prompt_revision,
            "trace": trace_evidence(
                trace_record,
                run_id=run_id,
                project=project,
                client_factory=langsmith_client,
            ),
        },
    )
    summary = {
        "status": outcome.execution,
        "handoff": outcome.handoff,
        "http_count": ledger["http_count"],
        "known_cost_cny_upper": ledger["known_cost_cny_upper"],
        "report_schema_version": ledger["report_schema_version"],
        "handoff_reasons": ledger["handoff_reasons"],
        "trace": frozen["trace"],
        "ledger_sha256": frozen["raw_ledger"]["sha256"],
        "summary": str(summary_path.relative_to(ROOT))
        if summary_path.is_relative_to(ROOT)
        else str(summary_path),
    }
    print(json.dumps(summary, default=str))
    return 0 if outcome.execution == "completed" and outcome.report is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
