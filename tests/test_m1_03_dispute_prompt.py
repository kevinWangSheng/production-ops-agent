"""Independent r7 contract tests for the narrowed dispute prompt."""

import json
from uuid import uuid4

import pytest

from opspilot.knowledge.contract import (
    CONTRACT_REVISION,
    POSTMORTEM_CONTENT_SCHEMA_VERSION,
    POSTMORTEM_INPUT_POLICY_VERSION,
    POSTMORTEM_OUTPUT_SCHEMA_VERSION,
    POSTMORTEM_PROMPT_VERSION,
)
from opspilot.knowledge.generation import (
    SYSTEM_PROMPT,
    GenerationInput,
    OutputInvalid,
    assemble,
    messages,
    parse_model_output,
)
from opspilot.knowledge.store import Watermark


def _built() -> GenerationInput:
    return GenerationInput(
        incident_id=uuid4(),
        watermark=Watermark(
            incident_control_generation=1,
            observation_generation=1,
            observation_session_id=uuid4(),
            observation_ending_id=uuid4(),
            run_count=1,
            last_run_id=uuid4(),
            input_watermark=1,
            evidence_snapshot_sha256="hash",
        ),
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
        catalog=[
            {
                "evidence_id": "ev-1",
                "scope": "service:api",
                "window_start": "2026-01-01T00:00:00Z",
                "window_end": "2026-01-01T01:00:00Z",
                "citable_as_fact": True,
            }
        ],
        payload={"evidence_catalog": []},
    )


def _reply(*, claim="hypothesis", extra=None):
    value = {
        "narrative_sections": [
            {
                "key": "impact",
                "section": "impact_summary",
                "body": "Observed impact",
                "evidence_ids": ["ev-1"],
            }
        ],
        "conclusions": [
            {
                "key": "cause",
                "section": "hypotheses",
                "claim": claim,
                "body": "A possible cause",
                "evidence_ids": ["ev-1"],
            }
        ],
        "proposals": [],
        "disputes": [],
    }
    if extra:
        value["conclusions"][0].update(extra)
    return json.dumps(value)


def test_r7_versions_keep_schema_and_input_v1_values() -> None:
    assert POSTMORTEM_PROMPT_VERSION == "f13-postmortem-prompt-v2"
    assert POSTMORTEM_OUTPUT_SCHEMA_VERSION == "f13-postmortem-output-v1"
    assert POSTMORTEM_CONTENT_SCHEMA_VERSION == "f13-postmortem-content-v1"
    assert POSTMORTEM_INPUT_POLICY_VERSION == "f13-postmortem-input-v1"
    assert CONTRACT_REVISION == "r7"


def test_system_prompt_contains_narrow_dispute_and_hypothesis_rules() -> None:
    normalized = " ".join(SYSTEM_PROMPT.lower().split())
    assert "at least two" in normalized
    assert "both evidence ids" in normalized
    disputes = normalized[normalized.index("disputes") :]
    assert "hypothes" in disputes
    assert "unresolved" in disputes or "not proven" in disputes
    assert "counter_evidence" in disputes
    assert "leaves open" not in normalized
    assert "leaves\n  open" not in SYSTEM_PROMPT.lower()


def test_first_and_repair_messages_use_the_same_system_prompt() -> None:
    built = _built()
    assert messages(built)[0] == {"role": "system", "content": SYSTEM_PROMPT}
    repaired = messages(built, repair=["bad shape"], previous="{}")
    assert repaired[0] == {"role": "system", "content": SYSTEM_PROMPT}


def test_hypothesis_claim_derives_uncertain_certainty() -> None:
    output = parse_model_output(_reply())
    draft = assemble(
        _built(), output, {}, record={"prompt_version": POSTMORTEM_PROMPT_VERSION}
    )
    conclusion = next(item for item in draft.conclusions if item.key == "cause")
    assert conclusion.certainty == "uncertain"
    assert conclusion.certainty != "supported"


def test_unknown_certainty_field_is_still_rejected() -> None:
    with pytest.raises(OutputInvalid) as caught:
        parse_model_output(_reply(extra={"certainty": "supported"}))
    assert "conclusions[0].certainty.unknown" in caught.value.problems
