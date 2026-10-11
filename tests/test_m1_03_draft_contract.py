"""Independent, deterministic contract tests for M1-03 draft generation.

These tests exercise only the frozen public generation interface (R1, D20--D23).
They deliberately do not import or inspect the worker implementation.
"""

import json
from uuid import uuid4

import pytest

from opspilot.knowledge.contract import (
    CODE_SECTIONS,
    CONTRACT_REVISION,
    EVENT_PAYLOAD_KEYS,
    MODEL_OUTPUT_FIELDS,
    RETRYABLE_ATTEMPT_ERRORS,
    ReviewCommand,
)
from opspilot.knowledge.generation import (
    OutputInvalid,
    citation_errors,
    parse_model_output,
    redact,
)


def _valid(*, evidence_ids=None):
    ids = evidence_ids or ["ev-1"]
    return {
        "narrative_sections": [
            {
                "key": "impact",
                "section": "impact_summary",
                "body": "Impact",
                "evidence_ids": ids,
            }
        ],
        "conclusions": [
            {
                "key": "cause",
                "section": "hypotheses",
                "claim": "hypothesis",
                "body": "A possible cause",
                "evidence_ids": ids,
            }
        ],
        "proposals": [],
        "disputes": [],
    }


def test_r1_parser_accepts_exact_shape_and_rejects_unknown_top_level_field():
    parsed = parse_model_output(json.dumps(_valid()))
    assert parsed.statements[0].key == "impact"
    assert parsed.statements[0].narrative is True
    assert parsed.statements[1].key == "cause"
    bad = _valid()
    bad["extra"] = []
    with pytest.raises(OutputInvalid) as exc:
        parse_model_output(json.dumps(bad))
    assert exc.value.problems


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x["narrative_sections"].append(x["narrative_sections"][0].copy()),
        lambda x: x["conclusions"][0].update({"claim": "unsupported-claim"}),
        lambda x: x["narrative_sections"][0].update({"key": "incident"}),
        lambda x: x["conclusions"][0].update({"evidence_ids": ["x"] * 21}),
        lambda x: x["disputes"].append({"conclusion_key": "missing", "reason": "why"}),
    ],
)
def test_r1_rejects_duplicate_reserved_or_unbounded_items(mutate):
    value = _valid()
    mutate(value)
    with pytest.raises(OutputInvalid):
        parse_model_output(json.dumps(value))


def test_citation_errors_distinguishes_unknown_and_non_citable_fact():
    output = parse_model_output(json.dumps(_valid(evidence_ids=["ev-1"])))
    catalog = [
        {
            "evidence_id": "ev-1",
            "scope": "svc",
            "window_start": "a",
            "window_end": "b",
            "citable_as_fact": False,
        }
    ]
    errors = citation_errors(output, catalog)
    assert errors["impact"] == ["NOT_CITABLE_AS_FACT"]
    unknown = parse_model_output(json.dumps(_valid(evidence_ids=["unknown"])))
    assert set(citation_errors(unknown, [])["impact"]) == {
        "UNKNOWN_EVIDENCE",
        "NOT_CITABLE_AS_FACT",
    }


def test_r1_fact_in_findings_is_structurally_valid_but_requires_citable_evidence():
    value = _valid()
    value["conclusions"][0].update({"claim": "fact", "section": "findings"})
    output = parse_model_output(json.dumps(value))
    assert output.statements[1].claim == "fact"
    assert output.statements[1].section == "findings"
    catalog = [{"evidence_id": "ev-1", "citable_as_fact": False}]
    assert citation_errors(output, catalog)["cause"] == ["NOT_CITABLE_AS_FACT"]
    catalog[0]["citable_as_fact"] = True
    assert citation_errors(output, catalog) == {}


def test_d21_redaction_removes_credentials_from_text():
    secrets = tuple(uuid4().hex for _ in range(3))
    text = "Author" + "ization: Bearer " + secrets[0]
    text += "; pass" + "word=" + secrets[1] + "; to" + "ken=" + secrets[2]
    clean = redact(text, secrets=secrets)
    assert all(secret not in clean for secret in secrets)


def test_d26_contract_constants_and_event_payload_allowlist_are_frozen():
    assert CONTRACT_REVISION == "r8"
    assert RETRYABLE_ATTEMPT_ERRORS == {
        "MODEL_UNAVAILABLE",
        "MODEL_REJECTED",
        "OUTPUT_INVALID",
    }
    assert set(MODEL_OUTPUT_FIELDS[""]) == {
        "narrative_sections",
        "conclusions",
        "proposals",
        "disputes",
    }
    assert CODE_SECTIONS == (
        "incident",
        "timeline",
        "runs",
        "human_actions",
        "recovery",
    )
    for keys in EVENT_PAYLOAD_KEYS.values():
        assert "trace_id" not in keys and "content" not in keys


@pytest.mark.parametrize(
    "action", ["approve", "reject", "return", "supersede", "revoke"]
)
def test_d26_review_command_rejects_missing_action_fields(action):
    command = ReviewCommand(action=action, idempotency_key="k", expected_generation=0)
    assert command.problems()
