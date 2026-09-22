"""Cross-Run continuation: a successor Run starts from committed evidence.

C3 §7/§13: an exhausted Run hands off; a new Run continues from business
facts, never by extending the old execution right. Creating the successor is
a human/Controller action -- this only builds what it receives.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from opspilot.investigation.context import ContextError, continuation_context
from opspilot.investigation.limits import RunLimits
from opspilot.investigation.loop import InvestigationLoop, InvestigationRequest
from opspilot.investigation.reports import delivered_from_context
from opspilot.investigation.store import MemoryStepStore
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    ScriptedModel,
    assemble,
    reply,
    report_json,
    tool_call,
)
from tests.m1_tool_support import build


def _rounds(count):
    return [
        reply(tool_calls=[tool_call(call_id=f"call-{i + 1}")], finish="tool_calls")
        for i in range(count)
    ]


def _exhausted_run():
    """Two tool rounds, then the budget runs out: a handoff with evidence."""
    loop, request, model, _, store, _ = assemble(
        replies=[*_rounds(2), reply(content="", finish="stop")],
        budget_limit=3,
        model_requests=3,
    )
    request = replace(request, limits=RunLimits(model_requests=3))
    outcome = loop.run(request)
    assert outcome.execution == "failed" and outcome.handoff is True
    assert len(outcome.evidence_ids) == 2
    # The product path records the input snapshot at accept(); the memory
    # store needs it set explicitly.
    store.input = request.as_input().as_json()
    return loop, request, store, outcome


def test_a_successor_run_can_cite_the_previous_runs_adopted_evidence():
    loop, request, store, outcome = _exhausted_run()
    cont = continuation_context(
        store.snapshot(),
        new_run_id="run-next",
        authorized_targets=request.scope.target_ids,
    )
    assert cont.previous_run_id == request.run_id
    assert cont.evidence_ids == outcome.evidence_ids
    assert cont.evidence_context["run_id"] == "run-next"
    assert set(cont.evidence_context["view_bindings"]) == set(outcome.evidence_ids)
    assert (
        cont.evidence_context["time_policies"]
        == request.evidence_context["time_policies"]
    )
    views = delivered_from_context(cont.evidence_context, run_id="run-next")
    assert sorted(v.evidence_id for v in views) == sorted(outcome.evidence_ids)
    assert all(v.time_scope_refs == frozenset({"policy-window-1"}) for v in views)
    assert "reasoning" not in cont.handoff_note
    assert request.run_id in cont.handoff_note and "EMPTY_REPORT" in cont.handoff_note

    # The successor Run is a new authorization: new run id, fresh budget,
    # and it may conclude from the carried evidence without any tool call.
    executor, _, _, clock = build(
        clock=loop.clock, scope_overrides={"run_id": "run-next"}
    )
    successor_store = MemoryStepStore(
        budget_limit=4, deadline=store.deadline, clock=clock, run_id="run-next"
    )
    cited = outcome.evidence_ids[0]
    successor = InvestigationLoop(
        model=ScriptedModel([reply(content=report_json(evidence_id=cited))]),
        executor=executor,
        store=successor_store,
        clock=clock,
    )
    result = successor.run(
        InvestigationRequest(
            run_id="run-next",
            question=f"{request.question}\n\n{cont.handoff_note}",
            scope=executor.scope,
            tool_schemas=TOOL_SCHEMAS,
            model_requests=2,
            evidence_context=cont.evidence_context,
        )
    )
    assert result.execution == "completed", result.handoff_reasons
    assert result.model_requests_used == 1
    assert cited in result.evidence_ids


def test_evidence_outside_the_successors_authorization_is_not_carried():
    _, request, store, _ = _exhausted_run()
    cont = continuation_context(
        store.snapshot(), new_run_id="run-next", authorized_targets=frozenset({"other"})
    )
    assert cont.evidence_ids == () and cont.evidence_context["view_bindings"] == {}
    assert "none" in cont.handoff_note


def test_evidence_inherited_through_one_handoff_survives_the_next():
    """Bot review (PR #29, comment 4067626641): a successor that itself
    started from carried evidence hands that evidence on again, under the
    same authorization filter as views it collected itself."""
    loop, request, store, outcome = _exhausted_run()
    first = continuation_context(
        store.snapshot(),
        new_run_id="run-next",
        authorized_targets=request.scope.target_ids,
    )
    executor, _, _, clock = build(
        clock=loop.clock, scope_overrides={"run_id": "run-next"}
    )
    successor_store = MemoryStepStore(
        budget_limit=4, deadline=store.deadline, clock=clock, run_id="run-next"
    )
    successor = InvestigationLoop(
        model=ScriptedModel([reply(content="", finish="stop")]),
        executor=executor,
        store=successor_store,
        clock=clock,
    )
    successor_request = InvestigationRequest(
        run_id="run-next",
        question=f"{request.question}\n\n{first.handoff_note}",
        scope=executor.scope,
        tool_schemas=TOOL_SCHEMAS,
        model_requests=1,
        evidence_context=first.evidence_context,
    )
    result = successor.run(successor_request)
    assert result.execution == "failed" and result.handoff is True
    assert set(result.evidence_ids) == set(outcome.evidence_ids)
    successor_store.input = successor_request.as_input().as_json()

    second = continuation_context(
        successor_store.snapshot(),
        new_run_id="run-third",
        authorized_targets=executor.scope.target_ids,
    )
    assert second.previous_run_id == "run-next"
    assert set(second.evidence_ids) == set(outcome.evidence_ids)
    assert (
        second.evidence_context["view_bindings"]
        == first.evidence_context["view_bindings"]
    )
    assert second.evidence_context["run_id"] == "run-third"

    narrowed = continuation_context(
        successor_store.snapshot(),
        new_run_id="run-third",
        authorized_targets=frozenset({"other"}),
    )
    assert narrowed.evidence_ids == ()
    assert narrowed.evidence_context["view_bindings"] == {}


def test_inherited_bindings_with_opaque_target_refs_are_authorized_via_the_catalog():
    """Bot review (PR #29, comment 4068683865): a schema-valid v4 binding names
    opaque ``target_refs`` and no registry id; the catalog, not a raw
    registry-id subset test, decides whether it is still authorized."""
    loop, request, _, _, store, _ = assemble(
        replies=[reply(content="", finish="stop")], model_requests=1
    )
    context = dict(request.evidence_context)
    context["target_catalog"] = {"checkout-ref": {"target_id": "checkout-prod"}}
    context["view_bindings"] = {
        "ev-prior": {
            "status": "ok",
            "target_refs": ["checkout-ref"],
            "time_scope_refs": ["policy-window-1"],
            "view_hash": "sha256:prior",
        }
    }
    request = replace(request, evidence_context=context)
    outcome = loop.run(request)
    assert outcome.execution == "failed" and "ev-prior" in outcome.evidence_ids
    store.input = request.as_input().as_json()

    cont = continuation_context(
        store.snapshot(),
        new_run_id="run-next",
        authorized_targets=request.scope.target_ids,
    )
    assert cont.evidence_ids == ("ev-prior",)
    binding = cont.evidence_context["view_bindings"]["ev-prior"]
    assert binding["target_refs"] == ["checkout-ref"] and "target_id" not in binding
    assert cont.evidence_context["target_catalog"] == context["target_catalog"]
    views = delivered_from_context(cont.evidence_context, run_id="run-next")
    assert [v.evidence_id for v in views] == ["ev-prior"]

    narrowed = continuation_context(
        store.snapshot(), new_run_id="run-next", authorized_targets=frozenset({"other"})
    )
    assert narrowed.evidence_ids == ()


def test_continuation_fails_closed_without_an_input_snapshot_or_with_the_same_run():
    _, request, store, _ = _exhausted_run()
    with pytest.raises(ContextError, match="INVALID_INPUT"):
        continuation_context(
            store.snapshot(), new_run_id=request.run_id, authorized_targets=frozenset()
        )
    snapshot = store.snapshot()
    snapshot["run"]["input"] = None
    with pytest.raises(ContextError, match="INPUT_MISSING"):
        continuation_context(
            snapshot, new_run_id="run-next", authorized_targets=frozenset()
        )
