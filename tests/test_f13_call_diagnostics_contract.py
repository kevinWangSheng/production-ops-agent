"""Independent #202 r11 D54/D55/D57/D59; public worker/trace interfaces.

Frozen baseline 02d896f and tmp/f13-65k/interface-r11.md. No real model,
network exporter, database, or assumptions about new private helpers.
"""

import json
from types import SimpleNamespace
from typing import get_args
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from opspilot import tracing
from opspilot.investigation.loop import ModelCall, ModelError, ModelReply
from opspilot.knowledge import contract
from opspilot.knowledge.generation import (
    MAX_MODEL_REQUESTS_PER_GENERATION,
    MAX_OUTPUT_TOKENS,
    GenerationInput,
)
from opspilot.knowledge.jobs import MAX_ATTEMPTS_PER_WATERMARK, Claim
from opspilot.knowledge.store import Watermark
from opspilot.knowledge.worker import PostmortemWorker
from tests.test_tracing import lab_env

REASONING = "SENTINEL_PRIVATE_REASONING_202"
BODY = "SENTINEL_OUTPUT_BODY_202"
PROMPT = "SENTINEL_INPUT_PROMPT_202"
CALL_FIELDS = {
    "seq",
    "purpose",
    "finish_reason",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "visible_completion_tokens",
    "outcome",
    "problems",
}
FULL_USAGE = {
    "prompt_tokens": 101,
    "completion_tokens": 83,
    "completion_tokens_details": {"reasoning_tokens": 59},
}


def valid_output(evidence_id="ev"):
    return {
        "narrative_sections": [
            {
                "key": "impact",
                "section": "impact_summary",
                "body": BODY,
                "evidence_ids": [evidence_id],
            }
        ],
        "conclusions": [],
        "proposals": [],
        "disputes": [],
    }


def reply(content=None, *, finish_reason="stop", usage=None):
    return ModelReply(
        json.dumps(valid_output()) if content is None else content,
        REASONING,
        (),
        finish_reason,
        "deepseek-flash",
        FULL_USAGE if usage is None else usage,
        {"reasoning_content": REASONING, "content": BODY, "prompt": PROMPT},
    )


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, call):
        self.calls.append(call)
        assert len(self.calls) <= 2, "D22: no third request"
        assert call.max_tokens == 65_536, "D57: each request uses the new output limit"
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RecordingJobs:
    """In-memory public GenerationStore collaborator; capture finish payload."""

    def __init__(self, built):
        self.built = built
        self.used = 0
        self.finished = []

    def claim(self, incident_id, *, owner, versions, max_model_requests):
        assert max_model_requests == 2
        return Claim(uuid4(), incident_id, owner, 1, max_model_requests, None)

    def read_input(self, incident_id):
        return {"watermark": self.built.watermark}

    def reserve_model_request(self, claim):
        self.used += 1
        assert self.used <= claim.max_model_requests
        return self.used

    def finish(self, claim, **values):
        self.finished.append(values)


class RecordingKnowledge:
    def __init__(self):
        self.drafts = []

    def record_draft(self, incident_id, **values):
        self.drafts.append(values)
        return SimpleNamespace(
            version=1, state="under_review", generation=1, object_id=uuid4()
        )


def generate(replies):
    incident = uuid4()
    built = GenerationInput(
        incident,
        Watermark(0, 1, uuid4(), uuid4(), 0, None, 0, "a" * 64),
        0,
        None,
        None,
        {section: {} for section in contract.CODE_SECTIONS},
        (
            {
                "evidence_id": "ev",
                "scope": "checkout",
                "window_start": "a",
                "window_end": "b",
                "citable_as_fact": True,
            },
        ),
        {"operator_note": PROMPT},
    )
    jobs, knowledge, model = (
        RecordingJobs(built),
        RecordingKnowledge(),
        ScriptedModel(replies),
    )
    outcome = PostmortemWorker(
        knowledge=knowledge,
        jobs=jobs,
        model=model,
        input_builder=lambda raw: built,
    ).generate(incident)
    assert outcome is not None
    assert len(jobs.finished) == 1
    assert outcome.model_requests == len(model.calls)
    return outcome, jobs.finished[0], knowledge, model


def diagnostics(finished):
    assert "model_calls" in finished, "D54: finish must persist per-request diagnostics"
    calls = finished["model_calls"]
    assert isinstance(calls, list)
    for seq, call in enumerate(calls, 1):
        assert set(call) == CALL_FIELDS
        assert call["seq"] == seq
        assert call["purpose"] == ("generate" if seq == 1 else "repair")
    encoded = json.dumps(calls)
    for private in (REASONING, BODY, PROMPT, "reasoning_content"):
        assert private not in encoded
    return calls


def test_d54_success_records_one_accepted_call_with_token_split():
    outcome, finished, _, _ = generate([reply()])
    assert outcome.status == "succeeded"
    assert diagnostics(finished) == [
        {
            "seq": 1,
            "purpose": "generate",
            "finish_reason": "stop",
            "prompt_tokens": 101,
            "completion_tokens": 83,
            "reasoning_tokens": 59,
            "visible_completion_tokens": 24,
            "outcome": "accepted",
            "problems": [],
        }
    ]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_d54_length_invalid_then_one_repair(repair_succeeds):
    outcome, finished, _, model = generate(
        [
            reply("{", finish_reason="length"),
            reply() if repair_succeeds else reply("not json " + BODY),
        ]
    )
    assert len(model.calls) == 2
    assert [call.max_tokens for call in model.calls] == [65_536, 65_536]
    assert outcome.status == ("succeeded" if repair_succeeds else "failed")
    assert outcome.error_code == (None if repair_succeeds else "OUTPUT_INVALID")
    calls = diagnostics(finished)
    assert [c["outcome"] for c in calls] == [
        "repair_requested",
        "accepted" if repair_succeeds else "rejected",
    ]
    assert calls[0]["finish_reason"] == "length"
    assert calls[0]["problems"]
    assert bool(calls[1]["problems"]) is (not repair_succeeds)
    assert calls[1]["finish_reason"] == "stop"


@pytest.mark.parametrize("visible_chars", [64_000, 64_001])
def test_d59_repair_echo_keeps_64000_character_boundary(visible_chars):
    # Multibyte text distinguishes a character limit from a byte limit.
    start, end = "ECHO_BOUNDARY_START_", "_ECHO_BOUNDARY_END"
    visible = start + "回" * (visible_chars - len(start) - len(end)) + end
    outcome, finished, _, model = generate(
        [reply(visible, finish_reason="length"), reply()]
    )
    assert outcome.status == "succeeded"
    assert len(model.calls) == 2
    assert [call.max_tokens for call in model.calls] == [65_536, 65_536]
    sent = "\n".join(
        message.get("content") or "" for message in model.calls[1].messages
    )
    assert visible[:64_000] in sent
    if visible_chars > 64_000:
        assert visible not in sent
    assert [call["purpose"] for call in diagnostics(finished)] == ["generate", "repair"]


@pytest.mark.parametrize("code", ["MODEL_REJECTED", "MODEL_UNAVAILABLE"])
def test_d54_provider_error_has_no_inferred_tokens(code):
    outcome, finished, _, model = generate([ModelError(code)])
    assert outcome.status == "failed" and outcome.error_code == code
    assert len(model.calls) == 1
    assert diagnostics(finished) == [
        {
            "seq": 1,
            "purpose": "generate",
            "finish_reason": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "reasoning_tokens": None,
            "visible_completion_tokens": None,
            "outcome": "provider_error",
            "problems": [],
        }
    ]


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"prompt_tokens": 101, "completion_tokens": 83}, (101, 83, None, None)),
        ({}, (None, None, None, None)),
        (
            {"completion_tokens_details": {"reasoning_tokens": 59}},
            (None, None, 59, None),
        ),
    ],
)
def test_d54_missing_usage_is_null_not_zero_or_estimated(usage, expected):
    outcome, finished, _, _ = generate([reply(usage=usage)])
    assert outcome.status == "succeeded"
    (call,) = diagnostics(finished)
    assert (
        tuple(
            call[k]
            for k in (
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "visible_completion_tokens",
            )
        )
        == expected
    )


def test_d54_repair_provider_error_preserves_first_request():
    outcome, finished, _, _ = generate([reply("{"), ModelError("MODEL_UNAVAILABLE")])
    assert outcome.error_code == "MODEL_UNAVAILABLE"
    calls = diagnostics(finished)
    assert [c["outcome"] for c in calls] == ["repair_requested", "provider_error"]
    assert calls[0]["problems"]
    assert all(
        calls[1][k] is None
        for k in (
            "finish_reason",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "visible_completion_tokens",
        )
    )


@pytest.mark.parametrize("parent", ["generation", "investigation"])
def test_d55_model_span_exports_finish_and_reasoning_count_only(parent):
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    call = ModelCall(
        messages=({"role": "user", "content": PROMPT},),
        tools=None,
        json_mode=True,
        max_tokens=16384,
        timeout_seconds=30,
    )
    context = (
        tracer.generation(incident_id=uuid4(), attempt_id=uuid4(), versions={})
        if parent == "generation"
        else tracer.run(incident_id=uuid4(), run_id=uuid4(), versions={})
    )
    try:
        with context:
            with tracer.model_call(call) as span:
                span.reply(reply(finish_reason="length"))
        tracer.shutdown()
        spans = exporter.get_finished_spans()
        models = [s for s in spans if "opspilot.model.tool_call_count" in s.attributes]
        assert len(models) == 1
        attrs = models[0].attributes
        assert attrs.get("opspilot.model.finish_reason") == "length"
        assert attrs.get("opspilot.model.reasoning_tokens") == 59
        assert type(attrs["opspilot.model.reasoning_tokens"]) is int
        assert REASONING not in str([(dict(s.attributes), s.events) for s in spans])
        assert "reasoning_content" not in str([dict(s.attributes) for s in spans])
    finally:
        tracing.reset()


def test_d57_output_limit_preserves_other_limits_errors_and_generation_record():
    assert MAX_OUTPUT_TOKENS == 65_536
    assert MAX_MODEL_REQUESTS_PER_GENERATION == 2
    assert MAX_ATTEMPTS_PER_WATERMARK == 3
    assert set(get_args(contract.AttemptErrorCode)) == {
        "MODEL_REJECTED",
        "MODEL_UNAVAILABLE",
        "INPUT_TOO_LARGE",
        "OUTPUT_INVALID",
        "WATERMARK_MOVED",
        "GENERATION_CONFLICT",
        "LEASE_LOST",
    }
    assert contract.RETRYABLE_ATTEMPT_ERRORS == {
        "MODEL_REJECTED",
        "MODEL_UNAVAILABLE",
        "OUTPUT_INVALID",
    }
    assert contract.POSTMORTEM_PROMPT_VERSION == "f13-postmortem-prompt-v5"
    assert contract.POSTMORTEM_INPUT_POLICY_VERSION == "f13-postmortem-input-v4"
    _, _, knowledge, model = generate([reply()])
    assert len(model.calls) == 1 and model.calls[0].max_tokens == 65_536
    record = knowledge.drafts[0]["content"]["generation"]
    assert set(record) == {
        "attempt_id",
        "model",
        "model_profile",
        "prompt_version",
        "output_schema_version",
        "input_policy_version",
        "response_model",
        "input_sha256",
        "input_bytes",
        "model_requests",
        "max_model_requests",
        "usage",
        "repaired",
        "revises_version",
        "return_reason",
    }
