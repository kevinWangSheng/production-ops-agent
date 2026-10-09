"""M1-03 step 4 real acceptance (D32, D33, D24, R9, R12, R15): one bounded
lab run of postmortem generation and workbench review.

Against a lab copy of the F6 archive database (migrated to head) whose
recovery observation has ended, with a real ``python -m opspilot.web
serve`` already running on the same copy:

1. generate a draft with ``PostmortemWorker.generate`` and the real
   ``deepseek-flash`` client (the postmortem path only; no investigation
   worker is started, so the archive's queued Runs stay queued, R12);
2. approve it through the workbench HTTP route as a dedicated lab Basic Auth
   account (test-driven simulated review, D33) and check the published
   knowledge revision;
3. move the watermark with a legal human input (``follow_up`` through the
   workbench control route), generate, return the version for revision,
   regenerate, reject;
4. move the watermark again, generate, and supersede the published entry
   only when the version actually proposes ``supersedes_entry_id``;
   otherwise record that the replacement could not be exercised (R15);
5. revoke the active revision through the workbench.

Every state is read back through ``opspilot.acceptance.postmortem_outcome``
and the pages it must agree with; model requests are counted on the client
and compared with the attempts the store recorded (R9). In lab mode
(``OPSPILOT_TRACE=lab``, ``--lab-round``) each generation is exported to the
round's LangSmith project after the fail-closed lab check; the frozen
``summary.json`` goes to ``docs/evidence/m1-03-review-acceptance/live-runs``
and the raw ledger stays outside the repository (ADR-0006).

    M0_ENV_FILE=/abs/.env OPSPILOT_TRACE=lab F13_LAB_UI_PASSWORD=... \\
        .venv/bin/python scripts/m1_03_review_live.py --dsn "<lab copy DSN>" \\
        --incident <uuid> --web http://127.0.0.1:<port> --user <lab account> \\
        --lab-round m1-03-review-<date>

Development script, not product code; credentials come from ``M0_ENV_FILE``
and the environment and are never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opspilot import tracing  # noqa: E402
from opspilot.acceptance import (  # noqa: E402
    PostmortemOutcome,
    postmortem_outcome,
    postmortem_records,
)
from opspilot.investigation.client import DeepSeekClient  # noqa: E402
from opspilot.knowledge import KnowledgeStore  # noqa: E402
from opspilot.knowledge.jobs import GenerationStore  # noqa: E402
from opspilot.knowledge.worker import PostmortemWorker  # noqa: E402
from opspilot.persistence import DurableStore  # noqa: E402
from opspilot.web.events import DurableEventLog  # noqa: E402
from scripts.lab_evidence import (  # noqa: E402
    LAB_ROUND_ENV,
    freeze,
    langsmith_client,
    prepare_lab_project,
    trace_evidence,
)
from scripts.m1_context_latency import balance  # noqa: E402
from scripts.m1_live_flash_loop import (  # noqa: E402
    RecordingClient,
    cost_cny,
    load_langsmith_env,
    read_key,
    resolve_env_file,
)

OUT_ROOT = ROOT / "docs/evidence/m1-03-review-acceptance/live-runs"
EXPERIMENT = "m1-03-review-acceptance"
PASSWORD_ENV = "F13_LAB_UI_PASSWORD"
# R7: at most three failed attempts per watermark; the backoff starts at 60 s
MAX_GENERATIONS_PER_STEP = 3
MAX_BACKOFF_WAIT_S = 300.0
# D16: a disputed version can only be returned; regenerate at most this often
MAX_DISPUTE_RETURNS = 2


def _balance(key: str) -> dict | str:
    try:
        return balance(key)
    except Exception as exc:  # noqa: BLE001 - recorded, not fatal
        return f"error:{type(exc).__name__}"


class Web:
    """The workbench as the lab reviewer's browser would reach it."""

    def __init__(self, base: str, user: str, password: str) -> None:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.client = httpx2.Client(
            base_url=base,
            headers={"authorization": f"Basic {token}", "origin": base},
            timeout=30.0,
        )

    def page(self, path: str) -> tuple[int, str]:
        response = self.client.get(path, headers={"accept": "text/html"})
        return response.status_code, response.text

    def post(self, path: str, fields: dict[str, str]) -> tuple[int, Any]:
        response = self.client.post(
            path, data=fields, headers={"accept": "application/json"}
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body


def _control_generation(dsn: str, incident_id: UUID) -> int:
    """Engineer read of the incident's control generation for the form."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _brief(outcome: PostmortemOutcome) -> dict[str, Any]:
    """The projection, reduced to what the summary freezes."""
    return {
        "generation_status": outcome.generation_status,
        "unknown_reasons": list(outcome.unknown_reasons),
        "postmortem_generation": outcome.postmortem_generation,
        "versions": [
            {
                "version": v.version,
                "revises_version": v.revises_version,
                "state": v.state,
                "stale_reason": v.stale_reason,
                "content_sha256": v.content_sha256,
                "conclusions": len(v.conclusions),
                "uncertain": sum(c.certainty == "uncertain" for c in v.conclusions),
                "citations_failed": sum(not c.citations_valid for c in v.conclusions),
                "disputed": sum(c.dispute_state == "disputed" for c in v.conclusions),
                "sections_present": sorted(
                    name for name, value in v.sections.items() if value
                ),
                "model_sections": sorted(
                    {c.section for c in v.conclusions if c.author == "model"}
                ),
                "proposals": [
                    {
                        "key": p["proposal_key"],
                        "supersedes_entry_id": None
                        if p["supersedes_entry_id"] is None
                        else str(p["supersedes_entry_id"]),
                    }
                    for p in v.proposals
                ],
                "review_actions": [
                    [a.action, a.actor_id, a.principal_kind] for a in v.review_actions
                ],
            }
            for v in outcome.versions
        ],
        "knowledge": [
            {
                "entry_id": str(k.entry_id),
                "revision": k.revision,
                "state": k.state,
                "retrievable": k.retrievable,
                "source_version": k.source_version,
                "source_proposal_key": k.source_proposal_key,
                "supersedes_revision": k.supersedes_revision,
                "approved_by": k.approved_by,
                "approved_by_kind": k.approved_by_kind,
                "content_sha256": k.content_sha256,
                "revoked_by": k.revoked_by,
                "revoked_reason": k.revoked_reason,
            }
            for k in outcome.knowledge
        ],
        "knowledge_actions": [
            [a.action, a.revision, a.actor_id, a.principal_kind]
            for a in outcome.knowledge_actions
        ],
        "recent_attempts": [
            {
                "status": a["status"],
                "error_code": a["error_code"],
                "version": a["version"],
                "model_requests": a["model_requests"],
            }
            for a in outcome.recent_attempts
        ],
    }


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _page_agrees(web: Web, outcome: PostmortemOutcome) -> dict[str, Any]:
    """D34/R14, coarse: the pages show the projection's states. The full
    field-by-field comparison is the acceptance suite's; this records that
    the live pages carried the same status, version states and revision
    states at a stable generation."""
    checks: dict[str, Any] = {}
    status, html = web.page(f"/incidents/{outcome.subject_id}")
    text = _text(html)
    checks["incident_page"] = status
    checks["incident_status_shown"] = (
        f"generation {outcome.postmortem_generation}" in text
        if outcome.postmortem_generation is not None
        else True
    )
    for v in outcome.versions:
        status, html = web.page(
            f"/postmortems/{outcome.postmortem_id}/versions/{v.version}"
        )
        checks[f"version_{v.version}"] = [status, v.state in _text(html)]
    for k in outcome.knowledge:
        status, html = web.page(f"/knowledge/{k.entry_id}")
        text = _text(html)
        checks[f"knowledge_{k.entry_id}_{k.revision}"] = [
            status,
            k.state in text,
            k.approved_by in text,
        ]
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--incident", required=True, type=UUID)
    parser.add_argument("--web", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--lab-round", default=os.environ.get(LAB_ROUND_ENV))
    args = parser.parse_args()

    password = os.environ.get(PASSWORD_ENV)
    if not password:
        raise SystemExit(f"{PASSWORD_ENV} missing")
    env_file = resolve_env_file()
    key = read_key(env_file)
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY missing in M0_ENV_FILE")
    load_langsmith_env(env_file)
    project = prepare_lab_project(
        os.environ, args.lab_round, client_factory=langsmith_client
    )
    trace = tracing.configure(os.environ)

    knowledge = KnowledgeStore(args.dsn)
    knowledge.install()
    jobs = GenerationStore(args.dsn)
    jobs.install()
    durable = DurableStore(args.dsn)
    events = DurableEventLog(durable)
    events.install()
    recorder = RecordingClient(DeepSeekClient(key))
    worker = PostmortemWorker(
        knowledge=knowledge, jobs=jobs, model=recorder, events=events
    )
    web = Web(args.web.rstrip("/"), args.user, password)
    del password
    run_id = str(uuid4())
    scenario = SimpleNamespace(
        scenario_id=f"f13-live-{run_id}", subject_id=str(args.incident)
    )

    steps: list[dict[str, Any]] = []
    generations: list[dict[str, Any]] = []

    def project_now() -> PostmortemOutcome:
        return postmortem_outcome(
            scenario, postmortem_records(knowledge, args.incident)
        )

    def record(name: str, **extra: Any) -> PostmortemOutcome:
        outcome = project_now()
        steps.append({"step": name, **extra, "projection": _brief(outcome)})
        return outcome

    def generate(name: str) -> PostmortemOutcome:
        """Generate until a reviewable version or R7's three failures."""
        for _ in range(MAX_GENERATIONS_PER_STEP):
            calls_before = len(recorder.attempts)
            result = worker.generate(args.incident)
            if result is None:
                view = knowledge.incident_postmortem(args.incident)
                due = view["schedule"]["next_attempt_at"]
                wait = (
                    0.0
                    if due is None
                    else (due - datetime.now(timezone.utc)).total_seconds()
                )
                if due is None or wait > MAX_BACKOFF_WAIT_S:
                    generations.append({"step": name, "claimed": False})
                    break
                time.sleep(max(wait, 0.0) + 1.0)
                continue
            generations.append(
                {
                    "step": name,
                    "claimed": True,
                    "attempt_id": str(result.attempt_id),
                    "status": result.status,
                    "error_code": result.error_code,
                    "version": result.version,
                    "state": result.state,
                    "model_requests_recorded": result.model_requests,
                    "model_requests_counted": len(recorder.attempts) - calls_before,
                    "trace": trace_evidence(
                        {
                            "mode": trace.mode,
                            "trace_id": result.trace_id,
                            "dropped_spans": getattr(trace, "dropped_spans", 0),
                        },
                        run_id=str(result.attempt_id),
                        project=project,
                        client_factory=langsmith_client,
                    ),
                }
            )
            if result.state == "under_review":
                break
        return record(name)

    def latest(outcome: PostmortemOutcome):
        return outcome.versions[-1] if outcome.versions else None

    def review(name: str, outcome: PostmortemOutcome, action: str, **fields: str):
        version = latest(outcome)
        assert version is not None
        status, body = web.post(
            f"/postmortems/{outcome.postmortem_id}/versions/{version.version}/review",
            {
                "action": action,
                "idempotency_key": f"lab-{uuid4().hex}",
                "expected_generation": str(outcome.postmortem_generation),
                **fields,
            },
        )
        return record(name, http_status=status, http_body=body, version=version.version)

    def follow_up(name: str, text: str) -> PostmortemOutcome:
        status, body = web.post(
            f"/incidents/{args.incident}/control",
            {
                "action": "follow_up",
                "idempotency_key": f"lab-{uuid4().hex}",
                "expected_generation": str(
                    _control_generation(args.dsn, args.incident)
                ),
                "text": text,
            },
        )
        return record(name, http_status=status, http_body=body)

    balance_before = _balance(key)
    started = datetime.now(timezone.utc)
    failures: list[str] = []

    before = record("before")
    if before.generation_status != "not_generated":
        failures.append(f"BEFORE_NOT_EMPTY:{before.generation_status}")

    def disputed(version: Any) -> bool:
        return any(c.dispute_state == "disputed" for c in version.conclusions)

    def settle(name: str, current: PostmortemOutcome) -> PostmortemOutcome:
        """A disputed version cannot be approved (D3, D16): show the refusal
        once, return it for revision and let the worker regenerate, at most
        ``MAX_DISPUTE_RETURNS`` times."""
        for round_ in range(1, MAX_DISPUTE_RETURNS + 1):
            version = latest(current)
            if version is None or version.state != "under_review":
                return current
            if not disputed(version):
                return current
            refused = review(f"{name}_approve_disputed_{round_}", current, "approve")
            if refused.postmortem_generation != current.postmortem_generation:
                failures.append("DISPUTED_APPROVAL_CHANGED_GENERATION")
            current = review(
                f"{name}_return_disputed_{round_}",
                refused,
                "return",
                reason="Lab review: a statement is disputed; re-check it "
                "against the cited evidence.",
            )
            current = generate(f"{name}_regenerate_{round_}")
        return current

    # 1. generate, approve, publish
    current = settle("v1", generate("generate_v1"))
    v1 = latest(current)
    if v1 is None or v1.state != "under_review":
        failures.append("V1_NOT_REVIEWABLE")
    else:
        if any(p["supersedes_entry_id"] for p in v1.proposals):
            failures.append("V1_PROPOSES_SUPERSEDE")
        current = review("approve_v1", current, "approve")
        if latest(current).state != "approved":
            failures.append("V1_NOT_APPROVED")
        if not any(k.retrievable for k in current.knowledge):
            failures.append("APPROVAL_PUBLISHED_NOTHING_RETRIEVABLE")

    # 2. move the watermark, generate, return; regenerate, reject
    current = follow_up(
        "follow_up_1",
        "Lab acceptance follow-up: confirm the impact window against the recovery samples.",
    )
    current = generate("generate_v2")
    v2 = latest(current)
    if (
        v2 is None
        or (v1 is not None and v2.version <= v1.version)
        or v2.state != "under_review"
    ):
        failures.append("V2_NOT_REVIEWABLE")
    else:
        current = review(
            "return_v2", current, "return", reason="Lab review: tighten the timeline."
        )
        current = generate("generate_v3")
        v3 = latest(current)
        if v3 is None or v3.state != "under_review" or v3.revises_version is None:
            failures.append("V3_NOT_REVIEWABLE_REVISION")
        else:
            current = review(
                "reject_v3",
                current,
                "reject",
                reason="Lab review: rejected to exercise the rejection path.",
            )

    # 3. move the watermark again; supersede only when actually proposed
    current = follow_up(
        "follow_up_2",
        "Lab acceptance follow-up: re-check the dependency signals after recovery.",
    )
    current = settle("v4", generate("generate_v4"))
    v4 = latest(current)
    supersedes = (
        []
        if v4 is None or v4.state != "under_review"
        else [
            p["supersedes_entry_id"] for p in v4.proposals if p["supersedes_entry_id"]
        ]
    )
    if v4 is None or v4.state != "under_review":
        failures.append("V4_NOT_REVIEWABLE")
    elif disputed(v4):
        failures.append("V4_STILL_DISPUTED")
    elif not supersedes:
        failures.append("SUPERSEDE_NOT_PROPOSED")
        steps.append({"step": "supersede_v4", "skipped": "no supersedes_entry_id"})
    else:
        entry_fields = {
            f"entry_generation:{entry_id}": str(
                knowledge.knowledge_history(entry_id)["generation"]
            )
            for entry_id in supersedes
        }
        current = review("supersede_v4", current, "supersede", **entry_fields)

    # 4. revoke the active revision
    active = [k for k in current.knowledge if k.state == "active"]
    if not active:
        failures.append("NOTHING_ACTIVE_TO_REVOKE")
    else:
        target = active[-1]
        status, body = web.post(
            f"/knowledge/{target.entry_id}/revoke",
            {
                "revision": str(target.revision),
                "idempotency_key": f"lab-{uuid4().hex}",
                "expected_generation": str(
                    knowledge.knowledge_history(target.entry_id)["generation"]
                ),
                "reason": "Lab review: revoked to exercise reversibility.",
            },
        )
        current = record("revoke", http_status=status, http_body=body)
        if knowledge.active_revision(target.entry_id) is not None:
            failures.append("REVOKED_STILL_RETRIEVABLE")

    ended = datetime.now(timezone.utc)
    final = project_now()
    pages = _page_agrees(web, final)
    tracing.shutdown()
    balance_after = _balance(key)
    del key
    knowledge.close()
    jobs.close()
    durable.close()

    counted = len(recorder.attempts)
    recorded = sum(int(g.get("model_requests_recorded") or 0) for g in generations)
    if counted != recorded:
        failures.append(f"MODEL_REQUEST_COUNT_MISMATCH:{counted}!={recorded}")
    usages = [a["usage"] for a in recorder.attempts if isinstance(a.get("usage"), dict)]
    ledger = {
        "experiment": EXPERIMENT,
        "incident_id": str(args.incident),
        "steps": steps,
        "generations": generations,
        "http": recorder.attempts,
        "pages": pages,
        "balance_before": balance_before,
        "balance_after": balance_after,
    }
    frozen, summary_path = freeze(
        ledger,
        experiment=EXPERIMENT,
        run_id=run_id,
        evidence_dir=OUT_ROOT / run_id,
        summary={
            "incident_id": str(args.incident),
            "environment": "lab: F6 archive database copy (synthetic OTel Demo lab data), not production",
            "review_driver": "test-driven simulated review: a dedicated lab Basic Auth account posting the workbench forms over HTTP (D33); not a human review",
            "started": started.isoformat(),
            "ended": ended.isoformat(),
            "failures": failures,
            "steps": [{k: v for k, v in s.items() if k != "http_body"} for s in steps],
            "generations": generations,
            "pages": pages,
            "final": _brief(final),
            "counts": {
                "model_requests_counted": counted,
                "model_requests_recorded": recorded,
                "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
                "completion_tokens": sum(
                    int(u.get("completion_tokens") or 0) for u in usages
                ),
            },
            "cost": {
                "known_cost_cny_upper": round(sum(cost_cny(u) for u in usages), 6),
                "balance_before": balance_before,
                "balance_after": balance_after,
            },
        },
    )
    print(
        json.dumps(
            {
                "failures": frozen["failures"],
                "final_status": frozen["final"]["generation_status"],
                "cost": frozen["cost"],
                "summary": str(summary_path.relative_to(ROOT)),
            },
            default=str,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
