"""M1-03 step 2: model output schema (R1), citations (D2) and redaction (D21)."""

import json

import pytest

from opspilot.knowledge.generation import (
    OutputInvalid,
    citation_errors,
    parse_model_output,
    redact,
)

CATALOG = [
    {"evidence_id": "ev-fact", "citable_as_fact": True},
    {"evidence_id": "ev-nofact", "citable_as_fact": False},
]


def _reply(**override):
    reply = {
        "narrative_sections": [
            {
                "key": "impact",
                "section": "impact_summary",
                "body": "b",
                "evidence_ids": ["ev-fact"],
            }
        ],
        "conclusions": [],
        "proposals": [],
        "disputes": [],
    }
    reply.update(override)
    return json.dumps(reply)


def test_a_minimal_reply_parses() -> None:
    output = parse_model_output(_reply())
    assert [s.key for s in output.statements] == ["impact"]
    assert citation_errors(output, CATALOG) == {}


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('{"narrative_sections": [], "narrative_sections": []}', "DUPLICATE_KEY"),
        ("[]", "NOT_AN_OBJECT"),
        ("nope", "NOT_JSON"),
        (_reply(extra=[]), "extra.unknown"),
        (json.dumps({"narrative_sections": []}), "conclusions.missing"),
        (
            _reply(
                narrative_sections=[
                    {
                        "key": "timeline",
                        "section": "impact_summary",
                        "body": "b",
                        "evidence_ids": ["ev-fact"],
                    }
                ]
            ),
            "narrative_sections[0].key",
        ),
        (
            _reply(
                narrative_sections=[
                    {
                        "key": "impact",
                        "section": "impact_summary",
                        "body": "b",
                        "evidence_ids": [],
                    }
                ]
            ),
            "narrative_sections[0].evidence_ids",
        ),
        (
            _reply(disputes=[{"conclusion_key": "nope", "reason": "r"}]),
            "disputes[0].conclusion_key",
        ),
        (_reply(narrative_sections=[]), "narrative_sections.impact_summary_missing"),
    ],
)
def test_the_schema_is_strict(text: str, problem: str) -> None:
    with pytest.raises(OutputInvalid) as caught:
        parse_model_output(text)
    assert problem in caught.value.problems


def test_citation_errors_name_unknown_and_non_fact_evidence() -> None:
    output = parse_model_output(
        _reply(
            conclusions=[
                {
                    "key": "f1",
                    "section": "findings",
                    "claim": "fact",
                    "body": "b",
                    "evidence_ids": ["ev-nofact"],
                },
                {
                    "key": "h1",
                    "section": "hypotheses",
                    "claim": "hypothesis",
                    "body": "b",
                    "evidence_ids": ["ev-nofact", "ev-ghost"],
                },
            ]
        )
    )
    assert citation_errors(output, CATALOG) == {
        "f1": ["NOT_CITABLE_AS_FACT"],
        "h1": ["UNKNOWN_EVIDENCE"],
    }


def test_redaction_removes_credential_shapes_and_known_values() -> None:
    text = "token=abc123456 Authorization: Bearer xyzxyzxyzxyz sk-0123456789abcdefghij hunter2hunter2"
    out = redact(text, ["hunter2hunter2"])
    for secret in (
        "abc123456",
        "xyzxyzxyzxyz",
        "sk-0123456789abcdefghij",
        "hunter2hunter2",
    ):
        assert secret not in out
