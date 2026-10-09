"""M1-03 D24: one bounded real postmortem generation in the lab.

Runs ``PostmortemWorker.generate`` once, with the real ``deepseek-flash``
client, against a lab database copy whose recovery observation has ended
(the F6 archive copy, migrated to head). In lab mode (``OPSPILOT_TRACE=lab``
and ``--lab-round``) the attempt is exported to the round's LangSmith
project after the same fail-closed lab check the worker uses; the frozen
``summary.json`` goes to ``docs/evidence/m1-03-draft-generation/live-runs``
and the raw ledger stays outside the repository (ADR-0006, scripts/
lab_evidence.py). Cost: the provider balance before and after, plus the
token-priced upper bound.

    M0_ENV_FILE=/abs/.env OPSPILOT_TRACE=lab .venv/bin/python \\
        scripts/m1_03_draft_live.py --dsn "<lab copy DSN>" \\
        --incident <uuid> --lab-round m1-03-draft-<date>

Development script, not product code; credentials are read from
``M0_ENV_FILE`` and never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opspilot import tracing  # noqa: E402
from opspilot.investigation.client import DeepSeekClient  # noqa: E402
from opspilot.knowledge import KnowledgeStore  # noqa: E402
from opspilot.knowledge.jobs import GenerationStore  # noqa: E402
from opspilot.knowledge.worker import PostmortemWorker  # noqa: E402
from opspilot.web.events import MemoryEventLog  # noqa: E402
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

OUT_ROOT = ROOT / "docs/evidence/m1-03-draft-generation/live-runs"
EXPERIMENT = "m1-03-draft-generation"


def _balance(key: str) -> dict | str:
    try:
        return balance(key)
    except Exception as exc:  # noqa: BLE001 - recorded, not fatal
        return f"error:{type(exc).__name__}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--incident", required=True, type=UUID)
    parser.add_argument("--lab-round", default=os.environ.get(LAB_ROUND_ENV))
    args = parser.parse_args()

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
    recorder = RecordingClient(DeepSeekClient(key))
    balance_before = _balance(key)
    events = MemoryEventLog()
    worker = PostmortemWorker(
        knowledge=knowledge, jobs=jobs, model=recorder, events=events
    )
    started = datetime.now(timezone.utc)
    outcome = worker.generate(args.incident)
    ended = datetime.now(timezone.utc)
    tracing.shutdown()
    balance_after = _balance(key)
    del key
    if outcome is None:
        raise SystemExit("incident is not a generation candidate")
    view = knowledge.incident_postmortem(args.incident)
    knowledge.close()
    jobs.close()

    latest = (view["postmortem"] or {"versions": [None]})["versions"][-1]
    document = None if latest is None else json.loads(latest["content"])
    usages = [a["usage"] for a in recorder.attempts if isinstance(a.get("usage"), dict)]
    trace_record = {
        "mode": trace.mode,
        "trace_id": trace.last_trace_id,
        "project": os.environ.get("LANGSMITH_PROJECT") if trace.mode == "lab" else None,
        "dropped_spans": getattr(trace, "dropped_spans", 0),
    }
    conclusions = (
        []
        if latest is None
        else [
            {
                "key": c["conclusion_key"],
                "section": c["section"],
                "author": c["author"],
                "certainty": c["certainty"],
                "citations_valid": c["citations_valid"],
                "evidence_ids": [r["evidence_id"] for r in c["evidence_refs"]],
                "dispute_state": c["dispute_state"],
                "body": c["body"] if c["author"] == "model" else None,
            }
            for c in latest["conclusions"]
        ]
    )
    ledger = {
        "experiment": EXPERIMENT,
        "incident_id": str(args.incident),
        "attempt_id": str(outcome.attempt_id),
        "started": started.isoformat(),
        "ended": ended.isoformat(),
        "outcome": outcome.__dict__,
        "http": recorder.attempts,
        "view": view,
        "events": [e.__dict__ for e in events.read_after(args.incident, 0)],
        "balance_before": balance_before,
        "balance_after": balance_after,
        "trace": trace_record,
    }
    frozen, summary_path = freeze(
        ledger,
        experiment=EXPERIMENT,
        run_id=str(outcome.attempt_id),
        evidence_dir=OUT_ROOT / str(outcome.attempt_id),
        summary={
            "incident_id": str(args.incident),
            "environment": "lab: F6 archive database copy (synthetic OTel Demo lab data), not production",
            "started": started.isoformat(),
            "ended": ended.isoformat(),
            "verdicts": {
                "attempt_status": outcome.status,
                "error_code": outcome.error_code,
                "generation_status": view["status"],
                "version": outcome.version,
                "version_state": outcome.state,
                "event_kinds": [e.kind for e in events.read_after(args.incident, 0)],
            },
            "counts": {
                "model_requests": outcome.model_requests,
                "max_model_requests": None
                if document is None
                else document["generation"]["max_model_requests"],
                "http_count": len(recorder.attempts),
                "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
                "completion_tokens": sum(
                    int(u.get("completion_tokens") or 0) for u in usages
                ),
                "evidence_catalog": None
                if document is None
                else len(document["evidence_catalog"]),
            },
            "generation": None if document is None else document["generation"],
            "validation": None if document is None else document["validation"],
            "conclusions": conclusions,
            "content_sha256": None if latest is None else latest["content_sha256"],
            "cost": {
                "known_cost_cny_upper": round(sum(cost_cny(u) for u in usages), 6),
                "balance_before": balance_before,
                "balance_after": balance_after,
            },
            "trace": trace_evidence(
                trace_record,
                run_id=str(outcome.attempt_id),
                project=project,
                client_factory=langsmith_client,
            ),
        },
    )
    print(
        json.dumps(
            {
                **frozen["verdicts"],
                "trace": frozen["trace"],
                "cost": frozen["cost"],
                "summary": str(summary_path.relative_to(ROOT)),
            },
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
