"""Budget, deadline and handoff contracts of the Flash investigation loop.

A budget or connection failure must not look like a completed investigation
(v4 acceptance packet). An incomplete but well-formed report is completed
execution with an explicit handoff, not a rewritten success.
"""

import hashlib
import json
from dataclasses import replace

import pytest

from opspilot.investigation.client import _parse_reply
from opspilot.investigation.limits import (
    MAX_HTTP_REQUEST_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_MODEL_REQUESTS_PER_RUN,
    MAX_OUTPUT_TOKENS,
    MAX_TOOL_OPERATIONS_PER_RUN,
    MAX_TOOL_SECONDS_PER_RUN,
    MODEL_REQUEST_TIMEOUT_SECONDS,
    RUN_WALL_SECONDS,
)
from opspilot.investigation.loop import (
    ACCEPTED_RESPONSE_MODEL,
    ModelCall,
    ModelError,
    serialized_request,
)
from opspilot.investigation.reports import FINAL_REPORT_INSTRUCTION, parse_report
from opspilot.investigation.store import (
    MemoryStepStore,
    StepStoreError,
    reservation_id_for,
)
from tests.m1_investigation_support import (
    assemble,
    reply,
    report_from_transcript,
    report_json,
    tool_call,
)
from tests.m1_tool_support import NOW, FakeClock


def test_reservation_ids_are_scoped_to_the_run():
    first = reservation_id_for("run-a", "round-1")
    assert first == reservation_id_for("run-a", "round-1")
    assert first != reservation_id_for("run-b", "round-1")


def test_supplied_context_views_can_be_cited_without_new_tools():
    evidence_id = "ev-supplied"
    loop, request, _, transport, _, _ = assemble(
        replies=[reply(content=report_json(evidence_id=evidence_id), finish="stop")],
        model_requests=1,
    )
    request = replace(
        request,
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "time_policies": [{"id": "policy-window-1"}],
            "view_bindings": {
                evidence_id: {"status": "ok", "target_refs": ["checkout-prod"]},
            },
        },
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert outcome.handoff is False
    assert transport.called is False
    assert evidence_id in outcome.evidence_ids


def test_retry_re_reserves_and_rechecks_control():
    class DenyOnSecondReserve(MemoryStepStore):
        def reserve_budget(self, reservation_id, amount):
            if self.reservations:
                raise StepStoreError("CONTROL_DENIED")
            super().reserve_budget(reservation_id, amount)

    loop, request, model, _, store, _ = assemble(
        replies=[
            ModelError("MODEL_UNAVAILABLE"),
            reply(content="should-not-run", finish="stop"),
        ],
        model_requests=1,
    )
    loop.store = DenyOnSecondReserve(
        budget_limit=store.budget_limit,
        deadline=store.deadline,
        clock=loop.clock,
    )
    outcome = loop.run(request)
    assert len(model.calls) == 1
    assert outcome.handoff_reasons == ("CONTROL_DENIED",)
    assert outcome.execution == "failed"


def test_exploration_retry_does_not_consume_the_final_slot():
    payload = json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "incomplete",
            "conclusion": "inconclusive",
            "summary": "Visible evidence is insufficient to support a cause.",
            "claims": [],
            "gaps": ["The reserved final request still ran."],
            "next_steps": ["Have a human inspect the remaining gaps."],
        }
    )
    loop, request, model, _, _, _ = assemble(
        replies=[
            ModelError("MODEL_UNAVAILABLE"),
            reply(content=payload, finish="stop"),
        ],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert len(model.calls) == 2
    assert model.calls[0].json_mode is False
    assert model.calls[1].json_mode is True
    assert model.calls[1].messages[-1]["content"] == FINAL_REPORT_INSTRUCTION
    assert outcome.execution == "completed"


def test_committed_step_records_request_hash_and_response_id():
    payload = json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "incomplete",
            "conclusion": "inconclusive",
            "summary": "Visible evidence is insufficient to support a cause.",
            "claims": [],
            "gaps": ["No further evidence was required."],
            "next_steps": ["Have a human close the incident."],
        }
    )
    loop, request, model, _, store, _ = assemble(
        replies=[reply(content=payload, finish="stop", raw={"id": "chatcmpl-live-1"})],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    recorded = store.steps["round-1"]["response"]
    assert recorded["response_id"] == "chatcmpl-live-1"
    assert (
        recorded["request_sha256"]
        == hashlib.sha256(serialized_request(model.calls[0])).hexdigest()
    )
    assert b'"thinking"' in serialized_request(model.calls[0])


def test_serialized_request_includes_vendor_fields_counted_in_the_size_cap():
    call = ModelCall(
        messages=({"role": "user", "content": "q"},),
        tools=None,
        json_mode=True,
        max_tokens=16,
        timeout_seconds=1,
    )
    body = serialized_request(call)
    assert b'"thinking"' in body
    assert b'"reasoning_effort"' in body
    assert b'"response_format"' in body
    assert b'"stream"' in body


def test_unavailable_retry_is_a_second_physical_request():
    payload = json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "incomplete",
            "conclusion": "inconclusive",
            "summary": "Visible evidence is insufficient to support a cause.",
            "claims": [],
            "gaps": ["Retry recovered enough to close the run."],
            "next_steps": ["Have a human inspect the remaining gaps."],
        }
    )
    loop, request, model, _, _, _ = assemble(
        replies=[
            ModelError("MODEL_UNAVAILABLE"),
            reply(content=payload, finish="stop"),
        ],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert len(model.calls) == 2
    assert outcome.model_requests_used == 2
    assert outcome.execution == "completed"


def test_frozen_ceilings_match_the_v4_b2_values():
    assert MAX_MODEL_REQUESTS_PER_RUN == 4
    assert MAX_TOOL_OPERATIONS_PER_RUN == 20
    assert MAX_OUTPUT_TOKENS == 16_384
    assert MAX_HTTP_REQUEST_BYTES == 512 * 1024
    assert MAX_HTTP_RESPONSE_BYTES == 2 * 1024 * 1024
    assert MODEL_REQUEST_TIMEOUT_SECONDS == 360.0
    assert RUN_WALL_SECONDS == 1800.0
    assert MAX_TOOL_SECONDS_PER_RUN == 240.0
    assert ACCEPTED_RESPONSE_MODEL == "deepseek-flash"


def test_budget_exhaustion_handoffs_and_is_not_completed():
    loop, request, model, transport, _, _ = assemble(
        replies=[reply(content=report_json(evidence_id="never"))],
        budget_limit=0,
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "budget_exhausted"
    assert outcome.handoff is True
    assert outcome.handoff_reasons == ("BUDGET_EXHAUSTED",)
    assert outcome.report is None
    assert model.calls == []
    assert transport.called is False


def test_deadline_before_dispatch_handoffs_without_calling_the_model():
    clock = FakeClock(start=NOW)
    loop, request, model, _, _, _ = assemble(
        replies=[reply(content=report_json(evidence_id="never"))],
        model_requests=1,
        clock=clock,
    )
    request = replace(request, scope=replace(request.scope, deadline=NOW))
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("DEADLINE_EXCEEDED",)
    assert outcome.report is None
    assert model.calls == []


def test_store_deadline_during_commit_is_control_denied():
    loop, request, model, _, store, _ = assemble(
        replies=[reply(content=report_json(evidence_id="x"), finish="stop")],
        model_requests=1,
    )
    store.deadline = NOW
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert "CONTROL_DENIED" in outcome.handoff_reasons
    assert outcome.report is None


def test_malformed_report_handoffs_and_keeps_the_raw_bytes():
    loop, request, _, _, _, _ = assemble(
        replies=[reply(content="not-json", finish="stop")],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff is True
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert outcome.report is None
    assert outcome.report_content == "not-json"
    assert outcome.report_content_sha256 == hashlib.sha256(b"not-json").hexdigest()


def test_empty_report_is_not_completed():
    loop, request, _, _, _, _ = assemble(
        replies=[reply(content="", finish="stop")],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("EMPTY_REPORT",)
    assert outcome.report is None


def test_length_finish_with_tool_calls_does_not_execute():
    loop, request, _, transport, _, _ = assemble(
        replies=[reply(tool_calls=[tool_call()], finish="length")],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("OUTPUT_LENGTH",)
    assert transport.called is False
    assert outcome.report is None


def test_invented_evidence_id_is_not_published():
    loop, request, _, _, _, _ = assemble(
        replies=[reply(content=report_json(evidence_id="invented"), finish="stop")],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert outcome.report is None


def test_invented_target_ref_is_not_published():
    def bad_target(call):
        evidence_id = "missing"
        for message in reversed(call.messages):
            if message.get("role") == "tool":
                evidence_id = json.loads(message["content"])["evidence_id"]
                break
        return reply(
            content=report_json(evidence_id=evidence_id, target_ref="other-svc")
        )

    loop, request, _, transport, _, _ = assemble(
        replies=[reply(tool_calls=[tool_call()], finish="tool_calls"), bad_target],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert transport.called is True
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert outcome.report is None


def test_invented_time_scope_is_not_published():
    def bad_scope(call):
        evidence_id = "missing"
        for message in reversed(call.messages):
            if message.get("role") == "tool":
                evidence_id = json.loads(message["content"])["evidence_id"]
                break
        return reply(
            content=report_json(evidence_id=evidence_id, time_scope="not-a-policy")
        )

    loop, request, _, _, _, _ = assemble(
        replies=[reply(tool_calls=[tool_call()], finish="tool_calls"), bad_scope],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert outcome.report is None


@pytest.mark.parametrize("container", [{}, "", 0, False])
def test_provider_non_list_tool_calls_are_rejected(container):
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "{}",
                    "tool_calls": container,
                },
            }
        ],
        "model": "deepseek-flash",
        "usage": {},
    }
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        _parse_reply(payload)


def test_provider_mixed_tool_calls_are_rejected():
    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [tool_call(), "not-a-call"],
                },
            }
        ],
        "model": "deepseek-flash",
        "usage": {},
    }
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        _parse_reply(payload)


def test_length_finish_is_output_length_handoff():
    loop, request, _, _, _, _ = assemble(
        replies=[reply(content="", finish="length")],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.handoff_reasons == ("OUTPUT_LENGTH",)
    assert outcome.execution == "failed"


def test_incomplete_valid_report_is_completed_execution_with_handoff():
    payload = json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "incomplete",
            "conclusion": "inconclusive",
            "summary": "Visible evidence is insufficient to support a cause.",
            "claims": [],
            "gaps": ["No query returned usable evidence."],
            "next_steps": ["Have a human supply the missing HealthProfile."],
        }
    )
    loop, request, _, _, _, _ = assemble(
        replies=[reply(content=payload, finish="stop")],
        model_requests=1,
    )
    # parse_report itself must accept the payload before the loop does.
    parsed, reason = parse_report(payload, finish_reason="stop")
    assert parsed is not None and reason == ""
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert outcome.handoff is True
    assert outcome.handoff_reasons == ("INCOMPLETE_INVESTIGATION",)
    assert outcome.report is not None
    assert outcome.report.assessment_status == "incomplete"
    assert outcome.report.conclusion == "inconclusive"
    assert outcome.report.gaps


def test_model_identity_mismatch_is_rejected():
    loop, request, _, _, _, _ = assemble(
        replies=[reply(content="{}", model="deepseek-v4-flash")],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.handoff_reasons == ("MODEL_IDENTITY_MISMATCH",)
    assert outcome.execution == "failed"
    assert outcome.report is None


def test_model_error_does_not_look_like_completion():
    loop, request, _, _, _, _ = assemble(
        replies=[ModelError("MODEL_UNAVAILABLE")],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("MODEL_UNAVAILABLE",)


def test_last_request_is_reserved_for_the_report_and_sends_no_tools():
    loop, request, model, _, store, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert model.calls[0].tools is not None
    assert model.calls[0].json_mode is False
    assert model.calls[1].tools is None
    assert model.calls[1].json_mode is True
    assert model.calls[1].messages[-1]["content"] == FINAL_REPORT_INSTRUCTION
    assert store.steps["round-1"]["status"] == "tool_result_committed"
    assert store.steps["round-2"]["status"] == "response_committed"
    assert outcome.evidence_ids
    assert (
        outcome.question_sha256 == hashlib.sha256(request.question.encode()).hexdigest()
    )
    assert model.calls[0].messages[1]["content"] == request.question


def test_invalid_early_text_uses_the_reserved_final_request():
    payload = json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "incomplete",
            "conclusion": "inconclusive",
            "summary": "Visible evidence is insufficient to support a cause.",
            "claims": [],
            "gaps": ["The first turn did not deliver a usable report."],
            "next_steps": ["Have a human retry with a wider evidence window."],
        }
    )
    loop, request, model, _, _, _ = assemble(
        replies=[reply(content="not-json-yet"), reply(content=payload)],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert len(model.calls) == 2
    assert model.calls[1].json_mode is True
    assert outcome.execution == "completed"
    assert outcome.handoff_reasons == ("INCOMPLETE_INVESTIGATION",)


def test_tool_plan_on_the_final_request_is_a_handoff():
    loop, request, _, _, _, _ = assemble(
        replies=[reply(tool_calls=[tool_call()], finish="tool_calls")],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.handoff_reasons == ("TOOL_PLAN_ON_FINAL",)
    assert outcome.execution == "failed"


def test_prompt_revision_is_stable_across_instance_budgets():
    from opspilot.instructions.discipline import prompt_revision, render
    from opspilot.investigation.reports import REPORT_CONTRACT

    rev_one = prompt_revision("replay-candidate", report_contract=REPORT_CONTRACT)
    rev_two = prompt_revision("replay-candidate", report_contract=REPORT_CONTRACT)
    assert rev_one == rev_two
    face_one = hashlib.sha256(
        render(
            "replay-candidate", model_requests=1, report_contract=REPORT_CONTRACT
        ).encode()
    ).hexdigest()
    face_four = hashlib.sha256(
        render(
            "replay-candidate", model_requests=4, report_contract=REPORT_CONTRACT
        ).encode()
    ).hexdigest()
    assert face_one != face_four
