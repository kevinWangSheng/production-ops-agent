"""#304: the rebuild re-derives a forced final round's retry feedback.

An L1a/B3 forced final round (the previous non-final reply proposed no tool
calls and failed report validation) sends the report instruction, the retry
feedback, then the run coverage summary, and records the hash of exactly
those bytes. The rebuild's hash check must re-insert the same feedback --
recomputed from the committed non-final reply with the same
``validated_report`` the live round used -- or every rebuild of such a Run
(follow-up after the handoff, restart before the conclusion row) blocks on
``INCOMPATIBLE_STATE``. Written by the implementer as a self-test; the
independent acceptance tests live elsewhere.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from opspilot.investigation import context as ctx
from opspilot.investigation.context import ContextError, estimate_tokens, messages_hash
from opspilot.investigation.limits import RunLimits
from opspilot.investigation.reports import (
    FINAL_REPORT_INSTRUCTION,
    run_coverage_message,
    validated_report,
)
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    ScriptedModel,
    assemble,
    reply,
    report_from_transcript,
)
from tests.test_m1_investigation_context import (
    Crash,
    _die_on_conclusion,
    _restart,
    _tool_rounds,
    _transcript,
    _wide,
)


def _bad(call):
    return reply(content="not a report")


def _forced_final_handoff():
    loop, request, model, _, store, _ = _wide(
        replies=[*_tool_rounds(1), _bad, _bad], model_requests=8
    )
    outcome = loop.run(request)
    return loop, request, model, store, outcome


def test_a_forced_final_round_sends_feedback_and_its_row_rebuilds():
    loop, request, model, store, outcome = _forced_final_handoff()
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    final_call = model.calls[-1]
    instruction, feedback, coverage = final_call.messages[-3:]
    assert instruction["content"] == FINAL_REPORT_INSTRUCTION
    assert feedback["content"].startswith("The previous reply was not accepted")
    assert coverage["content"] == run_coverage_message(
        _transcript_views(store, request)
    )
    row = store.steps["ctx0:round-3"]["response"]["context"]
    assert row["final"] is True
    assert row["input_snapshot_hash"] == messages_hash(final_call.messages)

    transcript = _transcript(store, request)  # raised INCOMPATIBLE_STATE before

    assert transcript.next_round == 4
    assert transcript.last_round is not None and transcript.last_round.final


def _transcript_views(store, request):
    # Rebuild only up to the tool round: a snapshot without the two report
    # rows never reaches the forced final row's hash check.
    snapshot = store.snapshot()
    snapshot = {
        **snapshot,
        "steps": [s for s in snapshot["steps"] if s["logical_key"] == "ctx0:round-1"],
    }
    return ctx.rebuild_transcript(
        snapshot,
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
        input=request.as_input(),
    ).delivered


def test_the_rebuild_feedback_is_the_live_validated_report_feedback():
    loop, request, model, store, _ = _forced_final_handoff()
    _, reason, feedback = validated_report(
        "not a report",
        finish_reason="stop",
        delivered=_transcript_views(store, request),
        evidence_context=request.as_input().evidence_context,
        authorized_targets=request.scope.target_ids,
    )
    assert reason == "REPORT_INVALID"
    assert model.calls[-1].messages[-2]["content"] == feedback


def test_a_follow_up_after_the_forced_final_handoff_resumes_the_same_run():
    loop, request, _, store, _ = _forced_final_handoff()
    store.advance_generation()
    store.append_input("follow_up", {"question": "look at payments again"})

    transcript = _transcript(store, request)

    assert transcript.last_round is not None and transcript.last_round.concluded
    second, model = _restart(loop, store, [report_from_transcript])
    outcome = second.resume(transcript)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert len(model.calls) == 1
    assert "look at payments again" in model.calls[0].messages[-1]["content"]
    assert outcome.model_requests_used == 4


def test_a_valid_forced_final_report_survives_a_crash_before_its_conclusion():
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), _bad, report_from_transcript], model_requests=8
    )
    dying = _die_on_conclusion(loop, store, request)
    with pytest.raises(Crash):
        loop.run(request)
    assert dying.steps["ctx0:round-3"]["response"]["context"]["final"] is True

    transcript = _transcript(dying, request)

    third, model = _restart(loop, dying, [])  # any model call would raise
    outcome = third.resume(transcript)
    assert outcome.execution == "completed" and model.calls == []
    assert outcome.model_requests_used == 3 and outcome.final_step_id is not None


def test_a_different_recomputed_feedback_still_fails_closed():
    """Changing only what the feedback is derived from (the reply's recorded
    ``finish_reason``; the transcript bytes are unchanged) must still block:
    the fix re-derives the feedback, it does not stop checking it."""
    _, request, _, store, _ = _forced_final_handoff()
    row = store.steps["ctx0:round-2"]["response"]
    assert row["finish_reason"] == "stop"
    row["finish_reason"] = "length"
    with pytest.raises(ContextError) as exc:
        _transcript(store, request)
    assert exc.value.code == "INCOMPATIBLE_STATE"


def test_a_feedback_from_an_older_generation_is_not_inserted():
    """The forced final row's generation must match the failed reply's; a
    rebuild that sees them under different generations reproduces bytes the
    live loop never sent (the worker fences commits on its lease generation,
    so this only arises from tampered rows) and fails closed."""
    _, request, _, store, _ = _forced_final_handoff()
    store.steps["ctx0:round-2"]["control_generation"] = 7
    with pytest.raises(ContextError) as exc:
        _transcript(store, request)
    assert exc.value.code == "INCOMPATIBLE_STATE"


def test_a_budget_final_round_without_a_failed_reply_has_no_feedback():
    loop, request, model, _, store, _ = _wide(
        replies=[*_tool_rounds(1), _bad], model_requests=2
    )
    outcome = loop.run(request)
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert model.calls[-1].messages[-2]["content"] == FINAL_REPORT_INSTRUCTION
    transcript = _transcript(store, request)
    store.advance_generation()
    store.append_input("follow_up", {"question": "again"})
    assert _transcript(store, request).next_round == transcript.next_round


def test_the_feedback_survives_a_compaction_between_the_failed_reply_and_the_final():
    """Live ``state.retry_feedback`` survives ``_manage_context``; when the
    forced final round compacts first, the rebuild must carry the feedback
    across the accepted compaction row to the final row."""
    padded = "not a report " + "x" * 4000

    def bad_padded(call):
        return reply(content=padded)

    def summary(call):
        return reply(content="Facts: errors. Unknown: cause. Next: report.")

    # Dry run with wide limits to size the threshold between the round-2
    # call (one tool group) and the forced final call (plus the padded reply).
    dry_loop = _wide(replies=[*_tool_rounds(1), bad_padded, _bad], model_requests=8)
    dry_loop[0].run(dry_loop[1])
    calls = dry_loop[2].calls
    tools = tuple(TOOL_SCHEMAS)
    before = estimate_tokens(calls[1].messages, tools)
    after = estimate_tokens(calls[2].messages, tools)
    assert after > before
    output = 64
    budget = int((before + after) / 2 / ctx.CONTEXT_POLICY.compaction_pct)
    limits = RunLimits(
        model_requests=16, context_tokens=budget + output, output_tokens=output
    )

    loop, request, _, _, store, _ = assemble(
        replies=[], budget_limit=16, model_requests=8
    )
    request = replace(request, limits=limits)
    loop.model = model = ScriptedModel([*_tool_rounds(1), bad_padded, summary, _bad])
    outcome = loop.run(request)
    assert outcome.handoff_reasons == ("REPORT_INVALID",), outcome.handoff_reasons
    kinds = [s["response"].get("kind") for s in store.snapshot()["steps"]]
    assert "compaction" in kinds
    final_call = model.calls[-1]
    assert final_call.messages[-3]["content"] == FINAL_REPORT_INSTRUCTION
    assert final_call.messages[-2]["content"].startswith(
        "The previous reply was not accepted"
    )

    _transcript(store, request)  # no INCOMPATIBLE_STATE across the compaction
