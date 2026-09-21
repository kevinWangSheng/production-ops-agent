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
