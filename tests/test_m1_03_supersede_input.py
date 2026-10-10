"""Independent r8 contract tests for the F13 supersede input boundary.

These tests exercise only the public generation DTOs and parser.  The
PostgreSQL tests cover the worker's real payload and review transaction.
"""

import json
from uuid import UUID, uuid4

import pytest

from opspilot.knowledge.generation import (
    GenerationInput,
    InputTooLarge,
    assemble,
    build_input,
    parse_model_output,
)
from opspilot.knowledge.store import Watermark


def _built(published=None) -> GenerationInput:
    return GenerationInput(
        incident_id=uuid4(),
        watermark=Watermark(1, 1, uuid4(), uuid4(), 1, uuid4(), 1, "h"),
        expected_generation=0,
        revises_version=None,
        return_reason=None,
        facts={
            "incident": {},
            "timeline": {},
            "runs": {},
            "human_actions": {},
            "recovery": {},
        },
        catalog=(),
        payload={
            "evidence_catalog": [],
            "published_knowledge_entries": published or [],
        },
        published_entries={
            "checkout-errors": UUID("11111111-1111-1111-1111-111111111111")
        },
    )


def _output(*proposals):
    return json.dumps(
        {
            "narrative_sections": [
                {
                    "key": "impact",
                    "section": "impact_summary",
                    "body": "b",
                    "evidence_ids": ["e"],
                }
            ],
            "conclusions": [],
            "proposals": list(proposals),
            "disputes": [],
        }
    )


def _proposal(key, name="same name"):
    return {
        "key": key,
        "name": name,
        "tags": [],
        "symptoms": ["s"],
        "checks": ["c"],
        "evidence_ids": ["e"],
    }


def test_r21_r22_r23_payload_projection_is_stable_and_uuid_free():
    entries = [
        {"key": "z-key", "name": "z [REDACTED]", "revision": 2},
        {"key": "a-key", "name": "a", "revision": 4},
    ]
    first = _built(entries)
    second = _built(entries)
    assert first.payload_text == second.payload_text
    assert "entry_id" not in first.payload_text
    assert [e["key"] for e in entries] == ["z-key", "a-key"]


def test_r24_empty_list_is_preserved():
    assert _built().payload["published_knowledge_entries"] == []


def test_r25_r43_exact_key_maps_and_name_does_not():
    output = parse_model_output(_output(_proposal("checkout-errors", "different name")))
    draft = assemble(_built(), output, {}, record={})
    assert draft.proposals[0].supersedes_entry_id == UUID(
        "11111111-1111-1111-1111-111111111111"
    )
    unknown = parse_model_output(_output(_proposal("unknown-key", "same name")))
    assert (
        assemble(_built(), unknown, {}, record={}).proposals[0].supersedes_entry_id
        is None
    )


def test_r25_duplicate_targets_are_rejected_by_output_schema():
    with pytest.raises(Exception):
        parse_model_output(
            _output(_proposal("checkout-errors"), _proposal("checkout-errors"))
        )


def test_d21_total_input_limit_refuses_before_generation(monkeypatch):
    import opspilot.knowledge.generation as generation

    raw = {
        "incident": {
            "incident_id": uuid4(),
            "control_generation": 0,
            "observation_generation": 1,
            "intake_key": "i",
            "lifecycle": "resolved",
            "state": "completed",
            "mode": "automatic",
            "target_id": uuid4(),
            "resource_uid": "r",
            "current_run_id": None,
            "created_at": None,
            "conclusion": {},
        },
        "watermark": _built().watermark,
        "observation": {
            "session": {
                "state": "completed",
                "mode": "automatic",
                "session_id": uuid4(),
                "target": {},
                "authorized_by": "x",
                "authorized_at": None,
                "deadline_at": None,
                "max_samples": 1,
                "sample_interval_seconds": 1,
                "sustained_window_seconds": 1,
                "ended_reason": "x",
                "health_profile_revision": None,
                "adopted_count": 0,
                "healthy_since": None,
            },
            "ending": {
                "ending_id": uuid4(),
                "ended_reason": "x",
                "recorded_at": None,
                "lifecycle_before": "x",
                "lifecycle_after": "x",
                "transition": "x",
            },
            "samples": [],
            "readings": [],
        },
        "evidence": [],
        "controls": [],
        "inputs": [],
        "runs": [],
        "postmortem": None,
        "pending_regeneration_version": None,
        "return_reason": None,
        "published_entries": {},
    }
    monkeypatch.setattr(generation, "MAX_INPUT_BYTES", 1)
    with pytest.raises(InputTooLarge, match="MAX_INPUT_BYTES"):
        build_input(raw, secrets=[])
