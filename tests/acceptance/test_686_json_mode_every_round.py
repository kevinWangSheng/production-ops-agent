"""Independent #686 acceptance, from the r1 rulings (no implementation branch).

Drive the public loop with ScriptedModel and project IncidentScenario to
IncidentOutcome. Only provider requests, committed business records, public
rebuild and observable outcomes are inspected. No PostgreSQL or model HTTP.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from opspilot.acceptance import IncidentScenario, outcome_from_loop
from opspilot.investigation.context import (
    COMPACTION_INSTRUCTION,
    CONTEXT_POLICY,
    estimate_tokens,
    rebuild_transcript,
)
from opspilot.investigation.limits import RunLimits
from opspilot.investigation.loop import serialized_request
from opspilot.tools.fixture import fixture_versions
from opspilot.tools.otel_demo import otel_demo_versions
from opspilot.tools.otel_demo_ext import otel_demo_ext_versions
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    assemble,
    reply,
    report_from_transcript,
    report_json,
    tool_call,
)


def _scenario(name):
    return IncidentScenario(
        scenario_id=f"686-{name}",
        feature_id="F3",
        acceptance_step="PR-R r1 JSON-mode contract",
        kind="scripted",
        subject_id="incident-acceptance",
    )


def _assemble(replies, *, mode=None, model_requests=6):
    loop, request, model, transport, store, sink = assemble(
        replies=replies, budget_limit=16, model_requests=model_requests
    )
    if mode is not None:
        store.append_input("message", {"text": "Please explain the evidence."})
        request = replace(request, conversation={"floor": 0, "mode": mode})
    return loop, request, model, transport, store, sink


def _tool(content=None, *, number=1, finish="tool_calls"):
    return reply(
        content=content,
        tool_calls=[
            tool_call(
                call_id=f"read-{number}",
                arguments=f'{{"expr":"rate(http_errors[{number}m])"}}',
            )
        ],
        finish=finish,
    )


def _answer():
    return {"to_input_seq": 1, "text": "The evidence is recorded.", "evidence_ids": []}


def _wrapped(report=None):
    return reply(
        content=json.dumps(
            {
                "schema_version": "m0-response-v1",
                "replies": [_answer()],
                "report": report,
            }
        )
    )


def _feedback(model):
    return [
        message["content"]
        for call in model.calls
        for message in call.messages
        if message.get("role") == "user"
        and message.get("content", "").startswith("The previous reply was not accepted")
    ]


@pytest.mark.parametrize(
    "mode", [None, "report_required", "report_optional", "reply_only"]
)
def test_every_investigation_request_serializes_json_mode(mode):
    """U7: tools and JSON coexist; final/continuation/reply requests also use JSON."""

    def final(call):
        if mode == "reply_only":
            return _wrapped()
        report = report_from_transcript(call)
        return report if mode is None else _wrapped(json.loads(report.content))

    script = [final] if mode == "reply_only" else [_tool(), _tool(number=2), final]
    loop, request, model, transport, _, _ = _assemble(
        script, mode=mode, model_requests=len(script)
    )
    outcome = outcome_from_loop(_scenario(f"requests-{mode}"), loop.run(request))
    assert outcome.final_state == ("answered" if mode == "reply_only" else "completed")
    bodies = [json.loads(serialized_request(call)) for call in model.calls]
    assert len(bodies) == len(script)
    assert "tools" not in bodies[-1]
    if mode != "reply_only":
        assert len(transport.requests) == 2
        assert all(body.get("tools") for body in bodies[:-1])
    for body in bodies:
        assert body.get("response_format") == {"type": "json_object"}


def test_forced_final_retry_also_serializes_json_mode():
    loop, request, model, _, _, _ = _assemble(
        [reply(content="   "), reply(content="   ")]
    )
    outcome = outcome_from_loop(_scenario("forced-final-json"), loop.run(request))
    assert outcome.handoff_reasons == ("EMPTY_REPORT",)
    assert len(model.calls) == 2
    bodies = [json.loads(serialized_request(call)) for call in model.calls]
    assert bodies[0].get("tools") and "tools" not in bodies[1]
    assert all(
        body.get("response_format") == {"type": "json_object"} for body in bodies
    )


@pytest.mark.parametrize("mode", [None, "report_optional", "reply_only"])
def test_recorded_revision_sets_remain_the_main_baseline(mode):
    """R3/D8: freeze main aa86233d values, rather than comparing to current constants."""
    script = [_wrapped()] if mode else [_tool(), report_from_transcript]
    loop, request, _, _, _, _ = _assemble(script, mode=mode)
    result = loop.run(request)
    assert result.execution == ("answered" if mode else "completed")
    assert result.prompt_revision == "prompt-replay-candidate-8688f6e52275"
    assert fixture_versions() == {
        "prompt_revision": "prompt-replay-candidate-8688f6e52275",
        "context_policy_revision": "ctx-ctx-policy-v1-a30e65ea52f4",
        "tool_schema_revision": "fixture-5",
    }
    assert otel_demo_versions() == {
        "prompt_revision": "prompt-replay-candidate-8688f6e52275",
        "context_policy_revision": "ctx-ctx-policy-v1-a30e65ea52f4",
        "tool_schema_revision": "otel-demo-a3a95a0f888e",
    }
    assert otel_demo_ext_versions() == {
        "prompt_revision": "prompt-replay-candidate-k8s-0b066c7cd6d1",
        "context_policy_revision": "ctx-ctx-policy-v1-a30e65ea52f4",
        "tool_schema_revision": "otel-demo-ext-c63f058afb18",
    }


@pytest.mark.parametrize(
    "blank", ["", " \t\r\n  ", " " * 232], ids=["empty", "whitespace", "232-spaces"]
)
@pytest.mark.parametrize("mode", [None, "report_optional"])
def test_blank_tool_content_executes_and_replays_byte_identically(blank, mode):
    def final(call):
        report = report_from_transcript(call)
        return _wrapped(json.loads(report.content)) if mode else report

    loop, request, model, transport, store, _ = _assemble(
        [_tool(blank), final], mode=mode
    )
    outcome = outcome_from_loop(_scenario("blank-tool"), loop.run(request))
    assert outcome.final_state == "completed" and outcome.report_available
    assert outcome.actions == ("read_only_query",)
    assert outcome.permissions == ("read_only",)
    assert len(transport.requests) == 1 and outcome.evidence_ids
    messages = json.loads(serialized_request(model.calls[-1]))["messages"]
    assistant = next(m for m in messages if m.get("tool_calls"))
    assert assistant["content"].encode("utf-8") == blank.encode("utf-8")
    # Rebuild rechecks each recorded input_snapshot_hash, including the request
    # which replays the whitespace. Do not normalize it to an empty message.
    rebuilt = rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
        input=request.as_input(),
    )
    replayed = next(m for m in rebuilt.messages if m.get("tool_calls"))
    assert replayed["content"].encode("utf-8") == blank.encode("utf-8")


@pytest.mark.parametrize("blank", [None, "", "   "], ids=["null", "empty", "spaces"])
def test_no_message_blank_answer_gets_one_retry_then_empty_report(blank):
    loop, request, model, transport, _, _ = _assemble(
        [reply(content=blank), reply(content=blank)]
    )
    outcome = outcome_from_loop(_scenario("twice-blank"), loop.run(request))
    assert outcome.final_state == "failed" and outcome.decision == "handoff"
    assert outcome.handoff_reasons == ("EMPTY_REPORT",)
    assert not outcome.report_available and not transport.called
    assert len(model.calls) == 2
    assert model.calls[0].tools and model.calls[-1].tools is None
    feedback = _feedback(model)
    assert len(feedback) == 1 and "(EMPTY_REPORT)" in feedback[0]


def test_blank_in_the_reserved_final_round_has_no_retry():
    loop, request, model, _, _, _ = _assemble([reply(content="   ")], model_requests=1)
    outcome = outcome_from_loop(_scenario("reserved-final-blank"), loop.run(request))
    assert outcome.final_state == "failed"
    assert outcome.handoff_reasons == ("EMPTY_REPORT",)
    assert len(model.calls) == 1 and model.calls[0].tools is None
    assert _feedback(model) == []


@pytest.mark.parametrize("mode", ["report_optional", "reply_only"])
@pytest.mark.parametrize("blank", [None, "", "   "], ids=["null", "empty", "spaces"])
def test_continuation_without_a_valid_reply_fails_without_retry(mode, blank):
    loop, request, model, transport, _, _ = _assemble([reply(content=blank)], mode=mode)
    outcome = outcome_from_loop(_scenario(f"blank-{mode}"), loop.run(request))
    assert outcome.final_state == "failed" and outcome.decision == "handoff"
    assert outcome.handoff_reasons == ("RESPONSE_INVALID",)
    assert not outcome.report_available and not transport.called
    assert len(model.calls) == 1 and _feedback(model) == []


def test_blank_tool_content_does_not_count_as_a_valid_continuation_reply():
    loop, request, model, transport, _, _ = _assemble(
        [_tool(" " * 232), reply(content="   ")], mode="report_optional"
    )
    outcome = outcome_from_loop(_scenario("no-earlier-reply"), loop.run(request))
    assert outcome.final_state == "failed"
    assert outcome.handoff_reasons == ("RESPONSE_INVALID",)
    assert len(transport.requests) == 1
    assert len(model.calls) == 2 and _feedback(model) == []


@pytest.mark.parametrize("shape", ["note", "wrapper"])
def test_valid_json_tool_reply_survives_blank_continuation_finish(shape):
    visible = {"replies": [_answer()]}
    if shape == "note":
        visible["note"] = "Checking the existing evidence."
    else:
        visible.update(schema_version="m0-response-v1", report=None)
    loop, request, model, transport, store, _ = _assemble(
        [_tool(json.dumps(visible)), reply(content="   ")], mode="report_optional"
    )
    outcome = outcome_from_loop(_scenario(f"earlier-{shape}-reply"), loop.run(request))
    assert outcome.final_state == "answered" and outcome.decision == "answered"
    assert outcome.handoff_reasons == () and not outcome.report_available
    assert len(transport.requests) == 1
    assert len(model.calls) == 2 and _feedback(model) == []
    business_replies = [
        item
        for step in store.snapshot()["steps"]
        for item in step["response"].get("replies", [])
    ]
    assert business_replies == [_answer()]
    if shape == "note":
        assert any(
            step["response"].get("note") == visible["note"]
            for step in store.snapshot()["steps"]
        )


@pytest.mark.parametrize("mode", [None, "report_optional"])
def test_length_limited_tool_round_fails_output_length_without_retry(mode):
    loop, request, model, transport, _, _ = _assemble(
        [_tool(" " * 232, finish="length")], mode=mode
    )
    outcome = outcome_from_loop(_scenario("length-tool"), loop.run(request))
    assert outcome.final_state == "failed"
    assert outcome.handoff_reasons == ("OUTPUT_LENGTH",)
    assert not outcome.report_available and not transport.called
    assert len(model.calls) == 1 and _feedback(model) == []


def _compacted_run():
    """Force actual compaction using a threshold between two tool views."""
    dry_loop, dry_request, dry_model, _, _, _ = _assemble(
        [_tool(), _tool(number=2), report_from_transcript]
    )
    dry_loop.run(dry_request)
    before = estimate_tokens(dry_model.calls[1].messages, TOOL_SCHEMAS)
    after = estimate_tokens(dry_model.calls[2].messages, TOOL_SCHEMAS)
    assert after > before
    budget = int((before + after) / 2 / CONTEXT_POLICY.compaction_pct)

    # View ids are newly generated, so capture this Run's first delivered id.
    seen = {}

    def summary(call):
        seen["id"] = next(
            json.loads(m["content"])["evidence_id"]
            for m in call.messages
            if m.get("role") == "tool"
        )
        return reply(
            content="Facts: errors observed. Unknown: cause. Next: final report."
        )

    def finish(call):
        return reply(content=report_json(evidence_id=seen["id"]))

    loop, request, model, _, _, _ = _assemble(
        [_tool(), _tool(number=2), summary, finish]
    )
    request = replace(
        request,
        limits=RunLimits(
            model_requests=8, context_tokens=budget + 64, output_tokens=64
        ),
    )
    outcome = outcome_from_loop(_scenario("compaction"), loop.run(request))
    assert outcome.final_state == "completed" and outcome.report_available
    return model


def test_compaction_request_does_not_serialize_response_format():
    """R1: the summary request remains plain text, without tools or JSON mode."""
    model = _compacted_run()
    compact = [
        call
        for call in model.calls
        if call.messages[-1].get("content") == COMPACTION_INSTRUCTION
    ]
    assert len(compact) == 1
    body = json.loads(serialized_request(compact[0]))
    assert "response_format" not in body and "tools" not in body


def test_investigation_requests_around_compaction_still_serialize_json_mode():
    model = _compacted_run()
    investigation = [
        call
        for call in model.calls
        if call.messages[-1].get("content") != COMPACTION_INSTRUCTION
    ]
    assert len(investigation) == 3
    assert all(
        json.loads(serialized_request(call)).get("response_format")
        == {"type": "json_object"}
        for call in investigation
    )


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "p1-report"])
@pytest.mark.parametrize(
    "omitted", ["hypothesis-evidence", "recommendation-evidence", "next_steps"]
)
def test_relaxed_optional_report_fields_publish_through_the_run(wrapped, omitted):
    def final(call):
        payload = json.loads(report_from_transcript(call).content)
        if omitted == "next_steps":
            del payload["next_steps"]
        else:
            payload["claims"].append(
                {
                    "kind": omitted.split("-")[0],
                    "text": "A possibility for human follow-up.",
                }
            )
        return _wrapped(payload) if wrapped else reply(content=json.dumps(payload))

    loop, request, model, _, _, _ = _assemble(
        [_tool(), final], mode="report_optional" if wrapped else None
    )
    result = loop.run(request)
    outcome = outcome_from_loop(_scenario(f"defaults-{wrapped}-{omitted}"), result)
    assert outcome.final_state == "completed" and outcome.report_available
    assert result.report is not None and result.report_rejected_reason is None
    if omitted == "next_steps":
        assert result.report.next_steps == []
    else:
        assert result.report.claims[-1].evidence_ids == []
    assert len(model.calls) == 2 and _feedback(model) == []


@pytest.mark.parametrize(
    "invalid", ["fact-evidence", "gaps", "root-evidence-missing", "root-evidence-empty"]
)
def test_p1_wrapper_keeps_valid_reply_but_rejects_invalid_report(invalid):
    def final(call):
        payload = json.loads(report_from_transcript(call).content)
        if invalid == "fact-evidence":
            del payload["claims"][0]["evidence_ids"]
        elif invalid == "gaps":
            del payload["gaps"]
        else:
            payload["schema_version"] = "m0-report-v3"
            payload["root_cause"] = {"kind": "service", "name": "checkout"}
            if invalid == "root-evidence-empty":
                payload["root_cause"]["evidence_ids"] = []
        return _wrapped(payload)

    loop, request, model, _, _, _ = _assemble([_tool(), final], mode="report_optional")
    result = loop.run(request)
    outcome = outcome_from_loop(_scenario(f"invalid-wrapper-{invalid}"), result)
    assert outcome.final_state == "answered" and outcome.decision == "answered"
    assert not outcome.report_available and result.report is None
    assert result.report_rejected_reason == "REPORT_INVALID"
    assert result.replies == (_answer(),)
    assert len(model.calls) == 2 and _feedback(model) == []


def test_nearest_id_hints_reach_the_one_forced_final_request():
    ids = []
    wrong_ids = []

    def bad_report(call):
        ids.extend(
            json.loads(m["content"])["evidence_id"]
            for m in call.messages
            if m.get("role") == "tool"
        )
        for evidence_id in ids:
            # Change one hex digit, preserving the id length and the rest of
            # its distinctive operation/UUID prefix. No guessed distance rule.
            wrong_ids.append(
                evidence_id[:-1] + ("0" if evidence_id[-1] != "0" else "1")
            )
        payload = json.loads(report_json(evidence_id=ids[0]))
        claim = payload["claims"][0]
        payload["claims"] = [{**claim, "evidence_ids": [wrong]} for wrong in wrong_ids]
        return reply(content=json.dumps(payload))

    loop, request, model, _, _, _ = _assemble(
        [_tool(), _tool(number=2), bad_report, report_from_transcript]
    )
    outcome = outcome_from_loop(_scenario("citation-hint-request"), loop.run(request))
    assert outcome.final_state == "completed" and outcome.report_available
    assert len(model.calls) == 4
    feedback = _feedback(model)
    assert len(feedback) == 1
    assert "unknown_evidence_id" in feedback[0]
    for wrong, nearest in zip(wrong_ids, ids, strict=True):
        assert wrong in feedback[0] and nearest in feedback[0]
    assert model.calls[-1].tools is None
