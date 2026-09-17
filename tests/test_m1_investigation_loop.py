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
from opspilot.investigation.reports import (
    FINAL_REPORT_INSTRUCTION,
    eligible_time_policies,
    parse_report,
)
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
from tests.m1_tool_support import NOW, WINDOW_END, WINDOW_START, FakeClock


def test_reservation_ids_are_scoped_to_the_run():
    first = reservation_id_for("run-a", "round-1")
    assert first == reservation_id_for("run-a", "round-1")
    assert first != reservation_id_for("run-b", "round-1")


def test_mismatched_store_run_id_is_rejected():
    loop, request, _, _, store, _ = assemble(
        replies=[reply(content="unused", finish="stop")],
        model_requests=1,
    )
    loop.store = MemoryStepStore(
        budget_limit=store.budget_limit,
        deadline=store.deadline,
        clock=loop.clock,
        run_id="some-other-run",
    )
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        loop.run(request)


def test_canonical_catalog_target_maps_onto_the_sole_authorized_target():
    opaque = "target:deadbeef"

    def cite_opaque(call):
        evidence_id = "missing"
        for message in reversed(call.messages):
            if message.get("role") == "tool":
                evidence_id = json.loads(message["content"])["evidence_id"]
                break
        return reply(
            content=report_json(evidence_id=evidence_id, target_ref=opaque),
            finish="stop",
        )

    loop, request, _, transport, _, _ = assemble(
        replies=[reply(tool_calls=[tool_call()], finish="tool_calls"), cite_opaque],
        model_requests=2,
    )
    request = replace(
        request,
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "time_policies": [
                {
                    "id": "policy-window-1",
                    "mode": "historical_window",
                    "all_authorized_targets": True,
                    "window": {
                        "start": WINDOW_START.isoformat(),
                        "end": WINDOW_END.isoformat(),
                    },
                }
            ],
            "target_catalog": {
                opaque: {
                    "namespace": "checkout",
                    "resource_uid": "svc-checkout",
                    "cluster_uid": "cluster-a",
                }
            },
        },
    )
    outcome = loop.run(request)
    assert transport.called is True
    assert outcome.execution == "completed"
    assert outcome.handoff is False


def test_stale_view_is_not_bound_to_a_current_policy():
    loop, request, _, _, _, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=2,
    )
    request = replace(
        request,
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "time_policies": [
                {
                    "id": "policy-window-1",
                    "mode": "current",
                    "max_source_age_seconds": 60,
                }
            ],
        },
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)


def test_mismatched_request_run_id_is_rejected():
    loop, request, _, _, _, _ = assemble(
        replies=[reply(content="unused", finish="stop")],
        model_requests=1,
    )
    request = replace(request, run_id="not-the-authorized-run")
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        loop.run(request)


def test_v4_opaque_target_refs_are_validated_via_catalog():
    evidence_id = "ev-v4"
    opaque = "target:deadbeef"
    loop, request, _, transport, _, _ = assemble(
        replies=[
            reply(
                content=report_json(evidence_id=evidence_id, target_ref=opaque),
                finish="stop",
            )
        ],
        model_requests=1,
    )
    request = replace(
        request,
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "run_id": request.run_id,
            "time_policies": [{"id": "policy-window-1"}],
            "target_catalog": {opaque: {"target_id": "checkout-prod"}},
            "view_bindings": {
                evidence_id: {
                    "status": "ok",
                    "target_refs": [opaque],
                    "time_scope_refs": ["policy-window-1"],
                },
            },
        },
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert outcome.handoff is False
    assert transport.called is False


def test_registry_id_is_rejected_when_a_v4_catalog_is_present():
    evidence_id = "ev-v4"
    opaque = "target:deadbeef"
    loop, request, _, _, _, _ = assemble(
        replies=[
            reply(
                content=report_json(
                    evidence_id=evidence_id, target_ref="checkout-prod"
                ),
                finish="stop",
            )
        ],
        model_requests=1,
    )
    request = replace(
        request,
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "run_id": request.run_id,
            "time_policies": [{"id": "policy-window-1"}],
            "target_catalog": {opaque: {"target_id": "checkout-prod"}},
            "view_bindings": {
                evidence_id: {
                    "status": "ok",
                    "target_refs": [opaque],
                    "time_scope_refs": ["policy-window-1"],
                },
            },
        },
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)


def test_fact_time_scope_must_match_the_cited_view():
    evidence_id = "ev-time"
    loop, request, _, _, _, _ = assemble(
        replies=[
            reply(
                content=report_json(
                    evidence_id=evidence_id, time_scope="policy-window-1"
                ),
                finish="stop",
            )
        ],
        model_requests=1,
    )
    request = replace(
        request,
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "run_id": request.run_id,
            "time_policies": [{"id": "policy-window-1"}, {"id": "policy-other"}],
            "view_bindings": {
                evidence_id: {
                    "status": "ok",
                    "target_refs": ["checkout-prod"],
                    "time_scope_refs": ["policy-other"],
                },
            },
        },
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)


def test_current_policy_with_empty_target_refs_covers_no_target():
    """Bot review finding #2: a valid v4 policy with ``target_refs: []`` and
    ``all_authorized_targets: false`` covers no targets. The old check only
    rejected a *nonempty* disjoint list -- ``named and named.isdisjoint(...)``
    is vacuously false for an empty ``named`` -- so an explicitly-scoped-to-
    nothing policy was silently attached to every target instead."""
    eligible = eligible_time_policies(
        [
            {
                "id": "policy-current",
                "mode": "current",
                "target_refs": [],
                "all_authorized_targets": False,
                "max_source_age_seconds": 60,
            }
        ],
        source="prometheus",
        tool="metrics.range_query",
        target_ids=frozenset({"checkout-prod"}),
        window=None,
        freshness_seconds=1,
    )
    assert eligible == frozenset()


def test_historical_policy_with_no_target_refs_key_covers_no_target():
    """Same gap, ``target_refs`` absent entirely rather than an empty list."""
    eligible = eligible_time_policies(
        [
            {
                "id": "policy-hist",
                "mode": "historical_window",
                "all_authorized_targets": False,
                "window": {
                    "start": WINDOW_START.isoformat(),
                    "end": WINDOW_END.isoformat(),
                },
            }
        ],
        source="prometheus",
        tool="metrics.range_query",
        target_ids=frozenset({"checkout-prod"}),
        window={"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        freshness_seconds=None,
    )
    assert eligible == frozenset()


def test_historical_policy_with_disjoint_target_refs_is_still_rejected():
    """Regression: a nonempty but disjoint ``target_refs`` was already
    rejected before the fix and must stay rejected."""
    eligible = eligible_time_policies(
        [
            {
                "id": "policy-hist",
                "mode": "historical_window",
                "target_refs": ["other-service"],
                "window": {
                    "start": WINDOW_START.isoformat(),
                    "end": WINDOW_END.isoformat(),
                },
            }
        ],
        source="prometheus",
        tool="metrics.range_query",
        target_ids=frozenset({"checkout-prod"}),
        window={"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        freshness_seconds=None,
    )
    assert eligible == frozenset()


def test_policy_with_all_authorized_targets_still_covers_everything():
    """Regression: an explicit ``all_authorized_targets: true`` is unaffected
    by the empty-``target_refs`` fix."""
    eligible = eligible_time_policies(
        [
            {
                "id": "policy-any",
                "mode": "current",
                "all_authorized_targets": True,
                "max_source_age_seconds": 60,
            }
        ],
        source="prometheus",
        tool="metrics.range_query",
        target_ids=frozenset({"checkout-prod"}),
        window=None,
        freshness_seconds=1,
    )
    assert eligible == frozenset({"policy-any"})


def test_policy_with_an_intersecting_target_ref_is_still_eligible():
    """Regression: an explicit, intersecting ``target_refs`` entry is
    unaffected by the fix."""
    eligible = eligible_time_policies(
        [
            {
                "id": "policy-named",
                "mode": "current",
                "target_refs": ["checkout-prod"],
                "max_source_age_seconds": 60,
            }
        ],
        source="prometheus",
        tool="metrics.range_query",
        target_ids=frozenset({"checkout-prod"}),
        window=None,
        freshness_seconds=1,
    )
    assert eligible == frozenset({"policy-named"})


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
            "run_id": request.run_id,
            "time_policies": [{"id": "policy-window-1"}],
            "view_bindings": {
                evidence_id: {
                    "status": "ok",
                    "target_refs": ["checkout-prod"],
                    "time_scope_refs": ["policy-window-1"],
                },
            },
        },
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert outcome.handoff is False
    assert transport.called is False
    assert evidence_id in outcome.evidence_ids


def test_evidence_context_from_a_different_run_is_not_trusted():
    """Bot review finding #1: ``delivered_from_context`` must bind the
    supplied context to this Run's own identity. Without it, a context
    copied from another Run -- or a fabricated mapping with no run identity
    at all -- would seed delivered citations as if this Run had produced
    them, letting a report claim ``supported`` from unbound provenance."""
    evidence_id = "ev-foreign"
    loop, request, _, _, _, _ = assemble(
        replies=[reply(content=report_json(evidence_id=evidence_id), finish="stop")],
        model_requests=1,
    )
    foreign = replace(
        request,
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "run_id": "some-other-run",
            "time_policies": [{"id": "policy-window-1"}],
            "view_bindings": {
                evidence_id: {
                    "status": "ok",
                    "target_refs": ["checkout-prod"],
                    "time_scope_refs": ["policy-window-1"],
                },
            },
        },
    )
    outcome = loop.run(foreign)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)


def test_evidence_context_missing_run_id_entirely_is_not_trusted():
    """The same gap for a fabricated mapping with no run identity at all."""
    evidence_id = "ev-no-identity"
    loop, request, _, _, _, _ = assemble(
        replies=[reply(content=report_json(evidence_id=evidence_id), finish="stop")],
        model_requests=1,
    )
    no_identity = replace(
        request,
        evidence_context={
            "time_policies": [{"id": "policy-window-1"}],
            "view_bindings": {
                evidence_id: {
                    "status": "ok",
                    "target_refs": ["checkout-prod"],
                    "time_scope_refs": ["policy-window-1"],
                },
            },
        },
    )
    outcome = loop.run(no_identity)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)


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
        run_id=store.authorized_run_id,
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


def test_every_physical_request_is_settled_as_spent_or_unknown():
    """C3 §13: reservations are settled in the store; unknown cost stays occupied."""
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
    loop, request, model, _, store, _ = assemble(
        replies=[
            ModelError("MODEL_UNAVAILABLE"),
            reply(content=payload, finish="stop"),
        ],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed" and len(model.calls) == 2
    assert (store.budget_reserved, store.budget_spent, store.budget_unknown) == (
        0,
        1,
        1,
    )
    assert sorted(store.settled.values()) == ["spent", "unknown"]


def test_a_fenced_settlement_leaves_the_reservation_occupied_and_records_history():
    class FenceAfterAnswer(MemoryStepStore):
        def settle_budget(self, reservation_id, outcome):
            self.deny_control()
            super().settle_budget(reservation_id, outcome)

    loop, request, model, _, store, _ = assemble(
        replies=[reply(content="late", finish="stop")], model_requests=1
    )
    loop.store = FenceAfterAnswer(
        budget_limit=store.budget_limit,
        deadline=store.deadline,
        clock=loop.clock,
        run_id=store.authorized_run_id,
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("CONTROL_DENIED",)
    # Not settled, not released: still counted against the limit.
    assert (loop.store.budget_reserved, loop.store.budget_spent) == (1, 0)


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


def test_provider_response_without_id_is_rejected():
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "{}"},
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


def test_loop_outcome_prompt_revision_ignores_the_run_instance_budget():
    """Two real loop runs that differ only in ``model_requests`` (an L1b
    instance value) must report the same ``ModelProfile.prompt_revision`` --
    the exact value ``prompt_revision_versions`` would put in ``versions`` --
    even though their rendered L1 face genuinely differs (C3 §5)."""
    from opspilot.investigation.loop import prompt_revision_versions

    def with_supplied_view(request, evidence_id):
        return replace(
            request,
            evidence_context={
                **request.evidence_context,
                "run_id": request.run_id,
                "view_bindings": {
                    evidence_id: {
                        "status": "ok",
                        "target_refs": ["checkout-prod"],
                        "time_scope_refs": ["policy-window-1"],
                    },
                },
            },
        )

    loop_one, request_one, _, _, _, _ = assemble(
        replies=[reply(content=report_json(evidence_id="ev-a"), finish="stop")],
        model_requests=1,
    )
    outcome_one = loop_one.run(with_supplied_view(request_one, "ev-a"))
    loop_four, request_four, _, _, _, _ = assemble(
        replies=[reply(content=report_json(evidence_id="ev-b"), finish="stop")],
        model_requests=4,
    )
    outcome_four = loop_four.run(with_supplied_view(request_four, "ev-b"))
    assert outcome_one.execution == "completed"
    assert outcome_four.execution == "completed"
    expected = prompt_revision_versions()["prompt_revision"]
    assert outcome_one.prompt_revision == expected
    assert outcome_four.prompt_revision == expected
    assert outcome_one.prompt_face_sha256 != outcome_four.prompt_face_sha256


def test_prompt_revision_versions_moves_with_the_l2_report_contract_text():
    """A real L2 content edit (not a hand-typed stand-in) must move the value
    a caller would compare in ``versions`` (C3 §5 revision rule 1)."""
    from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
    from opspilot.investigation.reports import REPORT_CONTRACT

    original = prompt_revision_versions(DISCIPLINE_VARIANT)
    edited = prompt_revision_versions(
        DISCIPLINE_VARIANT,
        report_contract=REPORT_CONTRACT + " New required field: severity.",
    )
    assert original != edited


def test_evidence_context_projection_strips_nested_unlisted_keys():
    """Redline P3-4: the old top-level ``password``/``secret``/``token``
    blocklist misses nested values entirely. An allowlist projection must
    drop an unexpected key at any depth -- inside ``view_bindings``,
    ``target_catalog`` and a ``time_policies`` entry, including its own
    nested ``window`` -- before the context reaches the model prompt."""
    from opspilot.investigation.reports import evidence_context_projection

    evidence_id = "ev-nested"
    opaque = "target:deadbeef"
    context = {
        "type": "opspilot-evidence-context-v4",
        "followup_text": {"password": "leak-top-level"},
        "time_policies": [
            {
                "id": "policy-window-1",
                "mode": "historical_window",
                "window": {
                    "start": WINDOW_START.isoformat(),
                    "end": WINDOW_END.isoformat(),
                    "authorization": "leak-window",
                },
                "secret": "leak-policy",
            }
        ],
        "target_catalog": {
            opaque: {
                "namespace": "checkout",
                "resource_uid": "svc-checkout",
                "cluster_uid": "cluster-a",
                "token": "leak-catalog",
            }
        },
        "view_bindings": {
            evidence_id: {
                "status": "ok",
                "target_refs": [opaque],
                "time_scope_refs": ["policy-window-1"],
                "authorization": "leak-view",
            },
        },
    }
    projected = evidence_context_projection(context)
    serialized = json.dumps(projected)
    for leaked in (
        "leak-top-level",
        "leak-window",
        "leak-policy",
        "leak-catalog",
        "leak-view",
    ):
        assert leaked not in serialized
    assert "followup_text" not in projected
    assert "secret" not in projected["time_policies"][0]
    assert "authorization" not in projected["time_policies"][0]["window"]
    assert "token" not in projected["target_catalog"][opaque]
    assert "authorization" not in projected["view_bindings"][evidence_id]
    # The legitimate fields the loop and the citation checks actually use
    # must survive the projection unchanged.
    assert projected["time_policies"][0]["window"]["start"] == WINDOW_START.isoformat()
    assert projected["target_catalog"][opaque]["namespace"] == "checkout"
    assert projected["view_bindings"][evidence_id]["status"] == "ok"


def test_evidence_context_projection_preserves_every_schema_required_field():
    """An allowlist that is narrower than the frozen v4 contract silently
    truncates a fully-compliant caller's context instead of only stripping
    what should not be there (an independent review of this change caught
    exactly that: ``run_id``, ``ViewBinding.view_hash``/``timing`` and
    ``TimePolicy.revision``/``integration_id``/``reference_rule`` were
    missing, and ``target_catalog`` only matched
    ``opspilot.domain.intake.Target``'s Kubernetes shape, not the schema's
    Compose/Integration variants). This test is a full round-trip against
    the actual frozen schema
    (``docs/evidence/m0-real-investigation/IncidentScenario.v4.schema.json``,
    ``$defs.EvidenceContext``), every required field of every referenced
    ``$def``, populated once per discriminated ``target_catalog`` kind."""
    from opspilot.investigation.reports import evidence_context_projection

    context = {
        "type": "opspilot-evidence-context-v4",
        "run_id": "run-schema-check",
        "target_catalog": {
            "target:kube": {
                "kind": "kubernetes",
                "integration_id": "int-1",
                "cluster_uid": "cluster-a",
                "namespace": "checkout",
                "resource_uid": "svc-checkout",
                "revision": "rev-1",
            },
            "target:compose": {
                "kind": "compose",
                "integration_id": "int-2",
                "deployment_instance": "host-1",
                "service": "checkout",
                "container_id": "c-1",
                "image_digest": "sha256:deadbeef",
                "telemetry_instance": "otel-1",
                "mapping_revision": "map-1",
                "config_revision": "cfg-1",
            },
            "target:integration": {
                "kind": "integration",
                "integration_id": "int-3",
                "deployment_instance": "host-2",
                "mapping_revision": "map-2",
                "service_identity": "unknown",
                "observed_services": ["checkout"],
            },
        },
        "view_bindings": {
            "ev-schema": {
                "view_hash": "a" * 64,
                "target_refs": ["target:kube"],
                "time_scope_refs": ["policy-1"],
                "timing": {
                    "operation_started_at": WINDOW_START.isoformat(),
                    "collection_completed_at": WINDOW_END.isoformat(),
                    "source_start_at": WINDOW_START.isoformat(),
                    "source_end_at": WINDOW_END.isoformat(),
                    "source_time_basis": "event_time",
                },
                "status": "ok",
            },
        },
        "time_policies": [
            {
                "id": "policy-1",
                "revision": "policy-rev-1",
                "integration_id": "int-1",
                "interfaces": ["metrics.range_query"],
                "mode": "historical_window",
                "reference_rule": "dispatch_started_at",
                "window": {
                    "start": WINDOW_START.isoformat(),
                    "end": WINDOW_END.isoformat(),
                },
                "max_source_age_seconds": None,
                "target_refs": [],
                "all_authorized_targets": False,
                "scope_revision": None,
            }
        ],
    }
    assert evidence_context_projection(context) == context


def test_loop_never_sends_nested_secret_bearing_keys_to_the_model():
    """End-to-end version of the projection test above: run the real loop
    with a poisoned ``evidence_context`` and inspect every byte actually
    handed to the ``ModelClient`` double."""
    evidence_id = "ev-nested-loop"
    loop, request, model, _, _, _ = assemble(
        replies=[reply(content=report_json(evidence_id=evidence_id), finish="stop")],
        model_requests=1,
    )
    context = {
        "type": "opspilot-evidence-context-v4",
        "run_id": request.run_id,
        "time_policies": [
            {
                "id": "policy-window-1",
                "mode": "historical_window",
                "window": {
                    "start": WINDOW_START.isoformat(),
                    "end": WINDOW_END.isoformat(),
                },
                "authorization": "leak-policy",
            }
        ],
        "view_bindings": {
            evidence_id: {
                "status": "ok",
                "target_refs": ["checkout-prod"],
                "time_scope_refs": ["policy-window-1"],
                "token": "leak-view",
            },
        },
    }
    request = replace(request, evidence_context=context)
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    sent = json.dumps([dict(message) for message in model.calls[0].messages])
    assert "leak-policy" not in sent
    assert "leak-view" not in sent
