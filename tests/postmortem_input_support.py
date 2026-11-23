"""A ``GenerationStore.read_input`` row set for driving the postmortem
drafting input (``build_input`` -> ``messages``) without PostgreSQL (#807).

The incident's ``conclusion`` column holds the terminal step in the shape the
investigation loop commits (``opspilot/investigation/loop.py``): the report
lives as a raw JSON string under ``conclusion.report_content`` next to the
Run's bookkeeping. Every field outside the published report's summary,
claims and gaps carries a sentinel string (B1-U5, B2-U2).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from opspilot.knowledge import Watermark

INCIDENT = UUID("00000000-0000-4000-8000-000000000807")
RUN = UUID("00000000-0000-4000-8000-000000000808")
SESSION = UUID("00000000-0000-4000-8000-000000000809")
ENDING = UUID("00000000-0000-4000-8000-000000000810")
AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

SUMMARY = "summary-marker-807: checkout errors followed the cart rollout."
CLAIMS = (
    {
        "kind": "fact",
        "text": "claim-marker-807-a: cart pods restarted at 12:00.",
        "evidence_ids": ["ev-807-a"],
        "target_refs": ["target:cart"],
        "time_scope_ref": "window:807",
    },
    {
        "kind": "hypothesis",
        "text": "claim-marker-807-b: the new cart image is the trigger.",
        "evidence_ids": [],
        "target_refs": [],
        "time_scope_ref": None,
    },
)
GAPS = ("gap-marker-807: no database metrics were available.",)

#: field -> sentinel; none of them may reach the drafting model.
SENTINELS = {
    "root_cause": "SENTINEL-807-root-cause-object",
    "next_steps": "SENTINEL-807-next-step",
    "replies": "SENTINEL-807-operator-reply",
    "prompt_revision": "SENTINEL-807-prompt-revision",
    "prompt_face_sha256": "SENTINEL-807-prompt-face",
    "question_sha256": "SENTINEL-807-question-sha",
    "calibration": "SENTINEL-807-calibration",
    "report_rejected_reason": "SENTINEL-807-rejected-reason",
    "handoff_reasons": "SENTINEL-807-handoff-reason",
    "evidence_ids": "SENTINEL-807-step-evidence-id",
    "source_step_id": "SENTINEL-807-source-step",
    "segment": "SENTINEL-807-segment",
}


def report_content() -> str:
    """The published report as the model returned it (m0-report-v3)."""
    return json.dumps(
        {
            "schema_version": "m0-report-v3",
            "assessment_status": "completed",
            "conclusion": "partial",
            "summary": SUMMARY,
            "claims": [dict(c) for c in CLAIMS],
            "gaps": list(GAPS),
            "next_steps": [SENTINELS["next_steps"]],
            "root_cause": {
                "kind": "Deployment",
                "namespace": "shop",
                "name": SENTINELS["root_cause"],
                "evidence_ids": ["ev-807-a"],
            },
        }
    )


def terminal_step(content: str | None) -> dict[str, Any]:
    """The incident's ``conclusion`` column: the loop's terminal step."""
    return {
        "kind": "conclusion",
        "assistant": {"role": "assistant", "content": None},
        "conclusion": {
            "execution": "completed",
            "handoff": True,
            "handoff_reasons": [SENTINELS["handoff_reasons"]],
            "report_schema_version": None if content is None else "m0-report-v3",
            "report_content": content,
            "report_content_sha256": None,
            "evidence_ids": [SENTINELS["evidence_ids"]],
            "model_requests_used": 3,
            "model_seconds_used": 12.5,
            "prompt_revision": SENTINELS["prompt_revision"],
            "prompt_face_sha256": SENTINELS["prompt_face_sha256"],
            "question_sha256": SENTINELS["question_sha256"],
            "source_step_id": SENTINELS["source_step_id"],
            "segment": SENTINELS["segment"],
            "rounds": 2,
            "compactions": 0,
            "calibration": {"note": SENTINELS["calibration"]},
            "replies": [{"sequence": 1, "text": SENTINELS["replies"]}],
            "report_rejected_reason": SENTINELS["report_rejected_reason"],
        },
    }


def raw_input(conclusion: Any) -> dict[str, Any]:
    """A minimal ``read_input`` result with ``conclusion`` as the column."""
    return {
        "incident": {
            "incident_id": INCIDENT,
            "intake_key": "intake-807",
            "target_id": None,
            "resource_uid": "cart",
            "state": "completed",
            "lifecycle": "resolved",
            "mode": "observe",
            "created_at": AT,
            "control_generation": 1,
            "observation_generation": 1,
            "current_run_id": RUN,
            "conclusion": conclusion,
        },
        "watermark": Watermark(
            incident_control_generation=1,
            observation_generation=1,
            observation_session_id=SESSION,
            observation_ending_id=ENDING,
            run_count=1,
            last_run_id=RUN,
            input_watermark=0,
            evidence_snapshot_sha256="0" * 64,
        ),
        "observation": {
            "session": {
                "health_profile_revision": "profile-1",
                "authorized_by": "system",
                "authorized_at": AT,
                "authorization_source": "automatic",
                "state": "ended",
                "adopted_count": 0,
                "max_samples": 3,
                "sample_interval_seconds": 60,
                "sustained_window_seconds": 180,
                "healthy_since": None,
            },
            "ending": {
                "ended_reason": "sustained_healthy",
                "transition": None,
                "lifecycle_before": "mitigating",
                "lifecycle_after": "resolved",
                "recorded_at": AT,
            },
            "samples": [],
            "readings": [],
        },
        "evidence": [],
        "controls": [],
        "inputs": [],
        "runs": [
            {
                "run_id": RUN,
                "state": "completed",
                "control_generation": 1,
                "budget_limit": 8,
                "budget_spent": 3,
                "deadline": AT,
            }
        ],
        "return_reason": None,
        "published_entries": {},
        "postmortem": None,
        "pending_regeneration_version": None,
    }


def model_visible(conclusion: Any) -> tuple[dict[str, Any], str]:
    """(payload["investigation_report"], every message text) for one draft."""
    from opspilot.knowledge.generation import build_input, messages

    built = build_input(raw_input(conclusion), secrets=[])
    sent = json.dumps(messages(built, secrets=[]), ensure_ascii=False)
    user = json.loads(messages(built, secrets=[])[1]["content"])
    return user["investigation_report"], sent
