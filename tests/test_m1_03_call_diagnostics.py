"""#202 (M1-03 r10): per-request diagnostics without PostgreSQL.

Implementer tests: the problem-code vocabulary (D54: model-chosen names and
list indexes never stored), one ``model_calls`` entry from a ``ModelReply``
(D54: missing values are ``None``, never inferred), and the two model-reply
span attributes (D55) on the real client path with an in-memory exporter.
"""

import json
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from opspilot import tracing
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import ModelReply
from opspilot.knowledge.generation import (
    PROBLEM_CODES,
    OutputInvalid,
    parse_model_output,
    problem_codes,
)
from opspilot.knowledge.worker import model_call
from tests.test_tracing import FAKE_DEEPSEEK_KEY, VERSIONS, _Opener, lab_env
from tests.test_tracing import model_call as traced_call

REASONING_TEXT = "private-chain-of-thought-must-not-be-exported"


@pytest.fixture(autouse=True)
def _reset_tracer():
    yield
    tracing.reset()


def _reply(usage, *, finish="stop", content="{}") -> ModelReply:
    return ModelReply(
        content=content,
        reasoning_content=REASONING_TEXT,
        tool_calls=(),
        finish_reason=finish,
        response_model="deepseek-flash",
        usage=usage,
        raw={},
    )


# --- problem codes (D54)


def _parser_problems(text: str) -> list[str]:
    with pytest.raises(OutputInvalid) as refused:
        parse_model_output(text)
    return list(refused.value.problems)


def test_parser_problems_map_to_the_fixed_vocabulary_without_model_names():
    item = {"key": "k", "section": "findings", "claim": "fact", "body": "b"}
    reply = {
        "narrative_sections": [],
        "conclusions": [
            {**item, "evidence_ids": [], "secret_field_name": 1},
            {**item, "section": "nowhere", "evidence_ids": ["e"]},
        ],
        "proposals": "not a list",
        "disputes": [],
        "model_invented_top_level": True,
    }
    raw = _parser_problems(json.dumps(reply))
    assert "model_invented_top_level.unknown" in raw
    assert problem_codes(raw) == ["reply.unknown_field"]

    del reply["model_invented_top_level"]
    raw = _parser_problems(json.dumps(reply))
    codes = problem_codes(raw)
    assert codes == [
        "conclusions.unknown_field",
        "conclusions.section",
        "narrative_sections.impact_summary_missing",
        "proposals.not_a_list",
    ]
    assert set(codes) <= PROBLEM_CODES
    assert "secret_field_name" not in json.dumps(codes)


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        ("", ["EMPTY_REPLY"]),
        ('{"narrative_sections": [', ["NOT_JSON"]),
        ('{"a": 1, "a": 2}', ["DUPLICATE_KEY"]),
        ("[]", ["NOT_AN_OBJECT"]),
        (
            '{"conclusions": [], "proposals": [], "disputes": []}',
            ["narrative_sections.missing"],
        ),
    ],
)
def test_whole_reply_problems_keep_their_codes(text, codes):
    assert problem_codes(_parser_problems(text)) == codes


def test_proposal_and_citation_codes_drop_the_keys_and_repeat_once():
    proposal = [
        "proposals.my-key.UNKNOWN_EVIDENCE",
        "proposals.other.UNKNOWN_EVIDENCE",
        "proposals.other.SUPERSEDE_REVISION_MISMATCH",
    ]
    assert problem_codes(proposal) == [
        "proposals.UNKNOWN_EVIDENCE",
        "proposals.SUPERSEDE_REVISION_MISMATCH",
    ]
    errors = {
        "impact": ["UNKNOWN_EVIDENCE", "NOT_CITABLE_AS_FACT"],
        "cause": ["UNKNOWN_EVIDENCE"],
    }
    assert problem_codes([], errors) == [
        "citations.UNKNOWN_EVIDENCE",
        "citations.NOT_CITABLE_AS_FACT",
    ]
    assert problem_codes([]) == []


def test_anything_outside_the_vocabulary_is_unrecognized():
    assert problem_codes(["free text from somewhere", "conclusions[0].nope"]) == [
        "UNRECOGNIZED_PROBLEM"
    ]


# --- one entry (D54)


def test_an_entry_carries_the_provider_values_and_the_split():
    entry = model_call(
        1,
        _reply(
            {
                "prompt_tokens": 9000,
                "completion_tokens": 16384,
                "completion_tokens_details": {"reasoning_tokens": 15000},
            },
            finish="length",
        ),
        ["NOT_JSON"],
    )
    assert entry == {
        "seq": 1,
        "purpose": "generate",
        "finish_reason": "length",
        "prompt_tokens": 9000,
        "completion_tokens": 16384,
        "reasoning_tokens": 15000,
        "visible_completion_tokens": 1384,
        "outcome": "rejected",
        "problems": ["NOT_JSON"],
    }
    assert REASONING_TEXT not in json.dumps(entry)


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": True, "completion_tokens": "20"},
        {"completion_tokens": 20, "completion_tokens_details": None},
        {
            "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 1.5},
        },
    ],
)
def test_missing_counts_are_none_never_inferred(usage):
    entry = model_call(2, _reply(usage), [])
    assert entry["purpose"] == "repair"
    assert entry["reasoning_tokens"] is None
    assert entry["visible_completion_tokens"] is None
    if "prompt_tokens" not in usage or usage["prompt_tokens"] is True:
        assert entry["prompt_tokens"] is None


def test_a_failed_request_has_no_reply_values():
    assert model_call(2, None, []) == {
        "seq": 2,
        "purpose": "repair",
        "finish_reason": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
        "visible_completion_tokens": None,
        "outcome": "provider_error",
        "problems": [],
    }


# --- span attributes (D55)


def _payload(usage, finish):
    return json.dumps(
        {
            "id": "resp-202",
            "model": "deepseek-flash",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"report": "done"}',
                        "reasoning_content": REASONING_TEXT,
                    },
                    "finish_reason": finish,
                }
            ],
            "usage": usage,
        }
    ).encode()


def _llm_attributes(usage, finish):
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    client = DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Opener(_payload(usage, finish)))
    with tracer.run(incident_id=uuid4(), run_id=uuid4(), versions=VERSIONS) as span:
        client.complete(traced_call())
        span.settle("published", None)
    tracer.shutdown()
    [llm] = [
        s
        for s in exporter.get_finished_spans()
        if s.name == "deepseek.chat.completions"
    ]
    return dict(llm.attributes)


def test_reply_span_carries_finish_reason_and_reasoning_tokens():
    attributes = _llm_attributes(
        {
            "prompt_tokens": 100,
            "completion_tokens": 16384,
            "total_tokens": 16484,
            "completion_tokens_details": {"reasoning_tokens": 16000},
        },
        "length",
    )
    assert attributes["opspilot.model.finish_reason"] == "length"
    assert attributes["opspilot.model.reasoning_tokens"] == 16000
    assert tuple(attributes["gen_ai.response.finish_reasons"]) == ("length",)
    assert REASONING_TEXT not in json.dumps(attributes, default=str)


def test_reasoning_tokens_are_left_out_when_not_reported():
    attributes = _llm_attributes(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}, "stop"
    )
    assert attributes["opspilot.model.finish_reason"] == "stop"
    assert "opspilot.model.reasoning_tokens" not in attributes
