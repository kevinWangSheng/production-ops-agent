"""Durable transcript rebuild and loop resume (C3 §5/§7), without PostgreSQL.

``MemoryStepStore`` keeps the committed rows across loop instances, so a
"worker restart" is a second ``InvestigationLoop`` over the same store. The
model is scripted; a ``RuntimeError`` from it stands in for the process
dying mid-request.
"""

from __future__ import annotations

import json

import pytest

from opspilot.investigation.context import (
    INITIAL_SEGMENT,
    ContextError,
    InvestigationInput,
    parse_step_key,
    rebuild_transcript,
    step_key,
)
from opspilot.investigation.limits import M1_FROZEN_LIMITS, RunLimits
from opspilot.investigation.loop import InvestigationLoop, tool_request_for
from opspilot.investigation.store import MemoryStepStore
from tests.m1_investigation_support import (
    ScriptedModel,
    assemble,
    reply,
    report_from_transcript,
    tool_call,
)

WIDE = RunLimits(model_requests=12)


class Crash(RuntimeError):
    """The process died while this model request was in flight."""


def _wide(*, replies, model_requests=8, budget_limit=12):
    loop, request, model, transport, store, sink = assemble(
        replies=replies, budget_limit=budget_limit, model_requests=model_requests
    )
    from dataclasses import replace

    request = replace(request, limits=WIDE)
    return loop, request, model, transport, store, sink


def _tool_rounds(count, *, start=1):
    return [
        reply(
            tool_calls=[tool_call(call_id=f"call-{start + index}")], finish="tool_calls"
        )
        for index in range(count)
    ]


def _restart(loop, store, replies):
    """A new worker: same store and executor, a fresh model client."""
    model = ScriptedModel(replies)
    return (
        InvestigationLoop(
            model=model, executor=loop.executor, store=store, clock=loop.clock
        ),
        model,
    )


def _transcript(store, request):
    return rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
        input=request.as_input(),
    )


# --- keys and input snapshot -------------------------------------------


def test_step_key_round_trips_and_reads_the_legacy_form():
    assert step_key("ctx0", 3) == "ctx0:round-3"
    assert parse_step_key("ctx2:round-10") == ("ctx2", 10)
    assert parse_step_key("round-4") == (INITIAL_SEGMENT, 4)
    assert parse_step_key("conclusion:ctx0:round-4") is None
    assert parse_step_key("ctx0:round-04") is None
    with pytest.raises(ContextError, match="INVALID_INPUT"):
        step_key("a:b", 1)


def test_input_snapshot_round_trips_and_rejects_a_tampered_tool_face():
    _, request, _, _, _, _ = _wide(replies=[])
    snapshot = request.as_input().as_json()
    restored = InvestigationInput.from_json(json.loads(json.dumps(snapshot)))
    assert restored == request.as_input()
    tampered = dict(snapshot)
    tampered["tool_schemas"] = []
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        InvestigationInput.from_json(tampered)
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        InvestigationInput.from_json({**snapshot, "model_requests": 99})


def test_frozen_limits_are_the_default_and_a_wider_instance_is_not_within_them():
    _, request, _, _, _, _ = assemble(replies=[])
    assert request.limits == M1_FROZEN_LIMITS
    assert WIDE.within(M1_FROZEN_LIMITS) is False
    assert M1_FROZEN_LIMITS.within(WIDE) is True


# --- more logical rounds than the frozen four -----------------------------


def test_the_loop_runs_more_than_four_logical_rounds_under_test_limits():
    loop, request, model, transport, store, _ = _wide(
        replies=[*_tool_rounds(6), report_from_transcript], model_requests=8
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert len(model.calls) == 7
    assert outcome.model_requests_used == 7
    assert len(outcome.evidence_ids) == 6
    assert [
        parse_step_key(key) for key in store.steps if not key.startswith("conclusion:")
    ] == [("ctx0", index) for index in range(1, 8)]
    assert outcome.final_step_id is not None
    assert outcome.conclusion["conclusion"]["rounds"] == 7
    assert outcome.conclusion["conclusion"]["execution"] == "completed"
    assert "reasoning_content" not in json.dumps(outcome.conclusion)


# --- restart at each C3 §7 breakpoint ------------------------------------


def test_restart_before_the_model_response_is_committed_continues_from_the_rows():
    loop, request, _, transport, store, _ = _wide(
        replies=[*_tool_rounds(2), Crash("killed mid-request")]
    )
    with pytest.raises(Crash):
        loop.run(request)
    assert store.usage().model_requests_used == 3  # the in-flight slot stays occupied
    executed_before = len(transport.requests)

    transcript = _transcript(store, request)
    assert transcript.next_round == 3
    assert transcript.live_steps == 2
    assert [m["role"] for m in transcript.messages][-4:] == [
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert len(transcript.delivered) == 2

    second, model = _restart(
        loop, store, [*_tool_rounds(1, start=3), report_from_transcript]
    )
    outcome = second.resume(transcript)
    assert outcome.execution == "completed"
    # Round 3 was re-sent with the rebuilt context, round 4 was the report.
    assert model.calls[0].tools is not None
    assert [m["role"] for m in model.calls[0].messages][-2:] == ["assistant", "tool"]
    assert outcome.model_requests_used == 5  # 2 + 1 crashed + 2 new, never reset
    assert len(outcome.evidence_ids) == 3
    assert parse_step_key(
        [k for k in store.steps if not k.startswith("conclusion:")][-1]
    ) == (
        "ctx0",
        4,
    )
    assert len(transport.requests) == executed_before + 1  # only round 3's query


def test_restart_after_the_response_is_committed_but_before_its_tools_ran():
    class DieOnce:
        def __init__(self, inner):
            self.inner = inner
            self.armed = True
            self.dispatched = 0

        @property
        def scope(self):
            return self.inner.scope

        @property
        def tool_seconds_used(self):
            return self.inner.tool_seconds_used

        def execute(self, request):
            if self.armed:
                self.armed = False
                raise Crash("killed before the tool ran")
            self.dispatched += 1
            return self.inner.execute(request)

    loop, request, _, _, store, _ = _wide(replies=[*_tool_rounds(1)])
    dying = DieOnce(loop.executor)
    loop.executor = dying
    with pytest.raises(Crash):
        loop.run(request)
    committed = [k for k in store.steps if not k.startswith("conclusion:")]
    assert committed == ["ctx0:round-1"]
    step_id = store.step_ids["ctx0:round-1"]
    assert store.tool_results[step_id] == []

    # A rebuild refuses while the current generation still owes a tool result.
    with pytest.raises(ContextError, match="PENDING_TOOLS"):
        _transcript(store, request)

    # The recovery session replays only the outstanding ordinal ...
    call = store.steps["ctx0:round-1"]["response"]["assistant"]["tool_calls"][0]
    outcome = dying.inner.execute(
        tool_request_for(
            step_id,
            0,
            call,
            target_ref=next(iter(request.scope.target_ids)),
            window=request.scope.window.as_json(),
        )
    )
    store.commit_tool(step_id, 0, dict(outcome.model_view))
    # ... after which the transcript is complete and the loop continues.
    transcript = _transcript(store, request)
    assert transcript.next_round == 2 and len(transcript.delivered) == 1
    second, model = _restart(loop, store, [report_from_transcript])
    second.executor = dying.inner
    result = second.resume(transcript)
    assert result.execution == "completed"
    assert result.evidence_ids == tuple(transcript.evidence_ids)
    assert dying.dispatched == 0  # the completed tool was never queried again


def test_restart_after_tool_results_are_committed_does_not_query_again():
    loop, request, _, transport, store, _ = _wide(
        replies=[*_tool_rounds(3), Crash("killed before round 4 went out")]
    )
    with pytest.raises(Crash):
        loop.run(request)
    queries_before = len(transport.requests)
    transcript = _transcript(store, request)
    second, model = _restart(loop, store, [report_from_transcript])
    outcome = second.resume(transcript)
    assert outcome.execution == "completed"
    assert len(transport.requests) == queries_before
    # Budget remained (8 planned, 4 used), so round 4 still offered tools; the
    # scripted model answered with the report anyway. One call, no re-query.
    assert len(model.calls) == 1
    assert [m["role"] for m in model.calls[0].messages].count("tool") == 3
    assert outcome.evidence_ids == tuple(transcript.evidence_ids)


def test_a_committed_conclusion_that_was_never_published_is_reported_for_publish():
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    outcome = loop.run(request)
    assert outcome.final_step_id is not None
    transcript = _transcript(store, request)
    assert transcript.pending_publish is not None
    step_id, conclusion = transcript.pending_publish
    assert step_id == outcome.final_step_id
    assert conclusion == outcome.conclusion
    # Once published, nothing is pending.
    store.publish(conclusion, step_id=step_id)
    assert _transcript(store, request).pending_publish is None


def test_rebuilt_delivered_views_match_the_original_attempt():
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(2), report_from_transcript]
    )
    outcome = loop.run(request)
    transcript = _transcript(store, request)
    assert tuple(transcript.evidence_ids) == outcome.evidence_ids
    assert [view.time_scope_refs for view in transcript.delivered] == [
        frozenset({"policy-window-1"})
    ] * 2


# --- generations, authorization and fail-closed rules ----------------------


def test_an_incomplete_group_from_a_superseded_generation_is_dropped_whole():
    loop, request, _, _, store, _ = _wide(replies=[*_tool_rounds(1)])

    class DieBeforeTools:
        def __init__(self, inner):
            self.inner = inner

        scope = property(lambda self: self.inner.scope)
        tool_seconds_used = property(lambda self: self.inner.tool_seconds_used)

        def execute(self, request):
            raise Crash("killed before the tool ran")

    loop.executor = DieBeforeTools(loop.executor)
    with pytest.raises(Crash):
        loop.run(request)
    orphan = store.step_ids["ctx0:round-1"]
    # A human follow-up moved the incident on; the old plan can never be finished.
    store.advance_generation()
    transcript = _transcript(store, request)
    assert transcript.dropped_groups == (orphan,)
    assert all(m["role"] != "tool" for m in transcript.messages)
    assert transcript.next_round == 2  # the round number is not reused
    assert transcript.delivered == []


def test_a_view_whose_target_is_no_longer_authorized_is_stubbed_and_not_cited():
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    loop.run(request)
    transcript = rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=frozenset(),
        input=request.as_input(),
    )
    tool_messages = [m for m in transcript.messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    stub = json.loads(tool_messages[0]["content"])
    assert stub["status"] == "revoked" and stub["reason"] == "TARGET_NOT_AUTHORIZED"
    assert "content" not in stub
    assert transcript.delivered == [] and transcript.evidence_ids == []


def test_rows_of_another_run_fail_closed():
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    loop.run(request)
    snapshot = store.snapshot()
    snapshot["steps"][0]["run_id"] = "someone-else"
    with pytest.raises(ContextError, match="INCONSISTENT_STATE"):
        rebuild_transcript(
            snapshot,
            run_id=request.run_id,
            authorized_targets=request.scope.target_ids,
            input=request.as_input(),
        )
    snapshot = store.snapshot()
    snapshot["run"]["run_id"] = "someone-else"
    with pytest.raises(ContextError, match="RUN_MISMATCH"):
        rebuild_transcript(
            snapshot,
            run_id=request.run_id,
            authorized_targets=request.scope.target_ids,
            input=request.as_input(),
        )


def test_an_unknown_step_kind_or_missing_input_fails_closed():
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    loop.run(request)
    snapshot = store.snapshot()
    snapshot["steps"][0]["response"]["kind"] = "summary-v9"
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        rebuild_transcript(
            snapshot,
            run_id=request.run_id,
            authorized_targets=request.scope.target_ids,
            input=request.as_input(),
        )
    with pytest.raises(ContextError, match="INPUT_MISSING"):
        rebuild_transcript(
            store.snapshot(),
            run_id=request.run_id,
            authorized_targets=request.scope.target_ids,
        )


def test_a_step_without_reasoning_content_cannot_be_replayed_with_tools():
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    loop.run(request)
    snapshot = store.snapshot()
    del snapshot["steps"][0]["response"]["assistant"]["reasoning_content"]
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        rebuild_transcript(
            snapshot,
            run_id=request.run_id,
            authorized_targets=request.scope.target_ids,
            input=request.as_input(),
        )


def test_the_active_time_budget_survives_a_restart():
    loop, request, _, _, store, _ = _wide(replies=[*_tool_rounds(1), Crash("dead")])
    with pytest.raises(Crash):
        loop.run(request)
    # The crashed request is unsettled: it counts at its reserved upper bound.
    assert store.usage().model_seconds_used >= WIDE.model_request_timeout_seconds
    tiny = RunLimits(
        model_requests=12, active_seconds=WIDE.model_request_timeout_seconds
    )
    from dataclasses import replace

    request = replace(request, limits=tiny)
    store.input = request.as_input().as_json()
    transcript = rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
    )
    second, model = _restart(loop, store, [report_from_transcript])
    outcome = second.resume(transcript)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("WALL_TIME_EXHAUSTED",)
    assert model.calls == []
    # The Run-level figures are cumulative across attempts, like
    # ``model_requests_used`` (bot review, PR #29, comment 4068748992).
    assert outcome.model_seconds_used >= WIDE.model_request_timeout_seconds
    assert outcome.conclusion["conclusion"]["model_seconds_used"] == pytest.approx(
        outcome.model_seconds_used
    )


def test_resume_refuses_a_transcript_for_another_run():
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    loop.run(request)
    transcript = _transcript(store, request)
    other = MemoryStepStore(
        budget_limit=12, deadline=store.deadline, clock=loop.clock, run_id="other-run"
    )
    stranger, _ = _restart(loop, other, [report_from_transcript])
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        stranger.resume(transcript)


# --- independent review findings (2026-09-21) -------------------------------


def test_an_accepted_report_whose_conclusion_was_never_committed_finishes_on_resume():
    """C3 §7 row 5: report step committed, process died before the conclusion."""
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )

    class DieOnConclusion(MemoryStepStore):
        armed = True

        def commit_step(self, logical_key, response):
            if self.armed and logical_key.startswith("conclusion:"):
                self.armed = False  # the next attempt is a different process
                raise Crash("killed before the conclusion row")
            return super().commit_step(logical_key, response)

    dying = DieOnConclusion(
        budget_limit=12,
        deadline=store.deadline,
        clock=loop.clock,
        run_id=request.run_id,
    )
    loop.store = dying
    with pytest.raises(Crash):
        loop.run(request)
    assert [k for k in dying.steps] == ["ctx0:round-1", "ctx0:round-2"]
    transcript = _transcript(dying, request)
    assert transcript.messages[-1]["role"] == "assistant"
    assert transcript.pending_publish is None
    second, model = _restart(loop, dying, [])  # any model call would raise
    outcome = second.resume(transcript)
    assert outcome.execution == "completed" and model.calls == []
    assert outcome.model_requests_used == 2  # nothing new was spent
    assert outcome.final_step_id is not None
    assert outcome.evidence_ids == tuple(transcript.evidence_ids)


def _die_on_conclusion(loop, store, request, *, armed=True):
    """A store whose process dies on the next conclusion row once ``armed``;
    the loop uses it."""

    class DieOnConclusion(MemoryStepStore):
        armed = True

        def commit_step(self, logical_key, response):
            if self.armed and logical_key.startswith("conclusion:"):
                self.armed = False  # the next attempt is a different process
                raise Crash("killed before the conclusion row")
            return super().commit_step(logical_key, response)

    dying = DieOnConclusion(
        budget_limit=12,
        deadline=store.deadline,
        clock=loop.clock,
        run_id=request.run_id,
    )
    dying.armed = armed
    loop.store = dying
    return dying


def _truncated_report(call):
    """A final answer the provider cut off (``finish_reason=length``) whose
    text nevertheless still parses as a complete report."""
    from dataclasses import replace

    return replace(report_from_transcript(call), finish_reason="length")


def test_a_truncated_final_answer_is_not_accepted_on_resume():
    """Bot review (PR #29, comment 4067626637): the live round refuses a
    ``length`` reply as ``OUTPUT_LENGTH``; a restart between that committed
    row and the conclusion must reach the same verdict from the persisted
    finish reason, not accept the text under a synthesized ``stop``."""
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), _truncated_report], model_requests=2
    )
    dying = _die_on_conclusion(loop, store, request)
    with pytest.raises(Crash):
        loop.run(request)
    assert dying.steps["ctx0:round-2"]["response"]["finish_reason"] == "length"
    transcript = _transcript(dying, request)
    assert (
        transcript.last_round.finish_reason == "length" and transcript.last_round.final
    )
    second, model = _restart(loop, dying, [])
    outcome = second.resume(transcript)
    assert outcome.execution == "failed" and model.calls == []
    assert outcome.handoff_reasons == ("OUTPUT_LENGTH",)
    assert outcome.report is None and outcome.model_requests_used == 2


def test_a_truncated_answer_before_the_final_round_is_retried_on_resume():
    """Same row, but the round was not the reserved final one: the live loop
    would have gone on to another request, and so does the resumed one."""
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), _truncated_report, Crash("died in flight")]
    )
    with pytest.raises(Crash):
        loop.run(request)
    transcript = _transcript(store, request)
    assert (
        transcript.last_round.finish_reason == "length"
        and not transcript.last_round.final
    )
    second, model = _restart(loop, store, [report_from_transcript])
    outcome = second.resume(transcript)
    assert outcome.execution == "completed" and len(model.calls) == 1
    # Three committed rounds plus the request that died in flight, which
    # stays reserved as unknown spend (C3 §13: budgets never reset).
    assert outcome.model_requests_used == 4
    assert outcome.conclusion["conclusion"]["rounds"] == 3


def test_a_refused_plan_ends_the_resumed_attempt_the_way_it_ended_the_live_one():
    """A rejected plan is committed with its reason; dying before the
    conclusion must not turn it into a fresh round (or, when its text happens
    to parse, into an accepted report)."""
    loop, request, _, _, store, _ = _wide(
        replies=[reply(tool_calls=[tool_call()], finish="stop")]
    )
    dying = _die_on_conclusion(loop, store, request)
    with pytest.raises(Crash):
        loop.run(request)
    transcript = _transcript(dying, request)
    assert transcript.last_round.rejection == "TOOL_PAIRING_INVALID"
    second, model = _restart(loop, dying, [])
    outcome = second.resume(transcript)
    assert outcome.execution == "failed" and model.calls == []
    assert outcome.handoff_reasons == ("TOOL_PAIRING_INVALID",)
    assert outcome.final_step_id is not None


def test_a_report_committed_after_a_follow_up_still_finishes_on_resume():
    """Bot review (PR #29, comment 4068683860): the superseded-conclusion
    guard covers the report that predates the follow-up, not a report the new
    generation committed afterwards and died before concluding."""
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    dying = _die_on_conclusion(loop, store, request, armed=False)
    assert loop.run(request).execution == "completed"
    dying.advance_generation()  # operator follow-up before the publish landed
    transcript = _transcript(dying, request)
    assert transcript.last_round.concluded  # the old report is history
    second, _ = _restart(
        loop, dying, [*_tool_rounds(1, start=2), report_from_transcript]
    )
    dying.armed = True
    with pytest.raises(Crash):
        second.resume(transcript)
    again = _transcript(dying, request)
    assert not again.last_round.concluded
    assert again.messages[-1]["role"] == "assistant" and again.next_round == 5
    third, model = _restart(loop, dying, [])  # any model call would raise
    outcome = third.resume(again)
    assert outcome.execution == "completed" and model.calls == []
    assert outcome.model_requests_used == 4 and outcome.final_step_id is not None


def test_a_report_row_from_an_older_generation_is_history_even_without_its_conclusion():
    """Bot review (PR #29, comment 4069466067): a follow-up that lands between
    the report row and its conclusion row fences the conclusion into
    late_result; the report row itself is then older than the current
    generation and is not re-adopted, exactly as when the conclusion row had
    landed under the superseded generation."""
    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    dying = _die_on_conclusion(loop, store, request)
    with pytest.raises(Crash):
        loop.run(request)
    dying.advance_generation()  # operator follow-up before the conclusion row
    transcript = _transcript(dying, request)
    assert transcript.last_round is not None and transcript.last_round.concluded
    second, model = _restart(loop, dying, [report_from_transcript])
    outcome = second.resume(transcript)
    assert outcome.execution == "completed" and len(model.calls) == 1


def test_a_rejected_tool_plan_is_persisted_but_never_becomes_pending_work():
    from opspilot.persistence import _tool_plan

    loop, request, _, _, store, _ = _wide(
        replies=[reply(tool_calls=[tool_call()], finish="stop")]
    )
    outcome = loop.run(request)
    assert outcome.handoff_reasons == ("TOOL_PAIRING_INVALID",)
    row = store.steps["ctx0:round-1"]["response"]
    assert "tool_calls" not in row["assistant"]
    assert row["rejected_plan"]["reason"] == "TOOL_PAIRING_INVALID"
    assert row["rejected_plan"]["tool_calls"][0]["id"] == "call-1"
    assert _tool_plan(row) == []
    transcript = _transcript(store, request)
    assert transcript.pending_publish is not None  # the handoff conclusion
    assert all(m["role"] != "tool" for m in transcript.messages)


def test_a_tool_plan_missing_reasoning_content_is_rejected_and_never_replayed():
    """Bot review finding: online and resume must judge a tool plan missing
    ``reasoning_content`` the same way -- rejected before any tool runs, not
    only when a later ``pair_tool_results`` call notices. This mirrors
    ``test_a_rejected_tool_plan_is_persisted_but_never_becomes_pending_work``
    above for that rejection reason; the resume side is the same
    ``_round_verdict`` rule both paths already share (see the assembled
    ``transcript`` below, which is exactly what a resumed attempt rebuilds
    from)."""
    from opspilot.persistence import _tool_plan

    loop, request, _, transport, store, _ = _wide(
        replies=[reply(tool_calls=[tool_call()], finish="tool_calls", reasoning=None)]
    )
    outcome = loop.run(request)
    assert outcome.handoff_reasons == ("PRIVATE_PROTOCOL_MISSING",)
    assert transport.called is False  # no tool ever dispatched
    row = store.steps["ctx0:round-1"]["response"]
    assert "tool_calls" not in row["assistant"]
    assert row["rejected_plan"]["reason"] == "PRIVATE_PROTOCOL_MISSING"
    assert row["rejected_plan"]["tool_calls"][0]["id"] == "call-1"
    assert _tool_plan(row) == []
    transcript = _transcript(store, request)
    assert transcript.pending_publish is not None  # the handoff conclusion
    assert all(m["role"] != "tool" for m in transcript.messages)
    assert transcript.last_round is not None
    assert transcript.last_round.rejection == "PRIVATE_PROTOCOL_MISSING"


def test_a_conclusion_from_a_superseded_generation_is_not_republished():
    from opspilot.investigation.context import pending_conclusion

    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    outcome = loop.run(request)
    assert pending_conclusion(store.snapshot()) == (
        outcome.final_step_id,
        outcome.conclusion,
    )
    store.advance_generation()  # operator follow-up before the publish landed
    assert pending_conclusion(store.snapshot()) is None
    transcript = _transcript(store, request)
    assert transcript.pending_publish is None
    assert transcript.next_round == 3  # the loop continues instead of republishing


def test_a_rebuild_whose_bytes_differ_from_what_was_sent_fails_closed():
    from opspilot.investigation.context import messages_hash

    loop, request, model, _, store, _ = _wide(
        replies=[*_tool_rounds(2), report_from_transcript]
    )
    loop.run(request)
    # Positive path: the recorded hash of round 2 is the hash of the rebuilt
    # transcript up to (not including) round 2.
    transcript = _transcript(store, request)
    round_two = store.steps["ctx0:round-2"]["response"]["context"]
    sent = list(model.calls[1].messages)
    assert round_two["input_snapshot_hash"] == messages_hash(sent)
    assert transcript.messages[: len(sent)] == sent
    # Tampering with round 1's persisted reply changes what round 2 "sent".
    snapshot = store.snapshot()
    snapshot["steps"][0]["response"]["assistant"]["content"] = "edited after the fact"
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        rebuild_transcript(
            snapshot,
            run_id=request.run_id,
            authorized_targets=request.scope.target_ids,
            input=request.as_input(),
        )


def test_a_conclusion_after_a_follow_up_gets_its_own_key_and_can_be_published():
    """Bot review (PR #29, comment 4067523565): after a follow-up supersedes an
    unpublished conclusion, a pre-dispatch halt must not land on the old key."""
    from opspilot.investigation.context import pending_conclusion

    loop, request, _, _, store, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    first = loop.run(request)
    old_key = next(k for k in store.steps if k.startswith("conclusion:"))
    store.advance_generation()
    tiny = RunLimits(model_requests=12, request_bytes=1)  # halts before dispatch
    from dataclasses import replace

    store.input = replace(request, limits=tiny).as_input().as_json()
    transcript = rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
    )
    second, model = _restart(loop, store, [])
    outcome = second.resume(transcript)
    assert outcome.handoff_reasons == ("REQUEST_TOO_LARGE",) and model.calls == []
    new_key = [k for k in store.steps if k.startswith("conclusion:") and k != old_key]
    assert len(new_key) == 1 and new_key[0].startswith("conclusion:g1:")
    assert outcome.final_step_id == store.step_ids[new_key[0]]
    assert outcome.final_step_id != first.final_step_id
    assert pending_conclusion(store.snapshot()) == (
        outcome.final_step_id,
        outcome.conclusion,
    )


# --- human inputs (follow_up / correct / event) and the rebuild ---------------


def test_a_run_that_received_a_follow_up_between_rounds_rebuilds_and_resumes():
    """Independent review (PR #31, P1): the projected ``investigation_inputs``
    message is part of what a round sends, so the recorded
    ``input_snapshot_hash`` covers it; the rebuild must re-insert the same
    message at the same position from the persisted inputs and the frozen
    watermark, or every Run that ever received an input blocks on resume."""
    from opspilot.investigation.context import messages_hash

    loop, request, model, _, store, _ = _wide(replies=[])

    def tool_round_then_follow_up(call):
        # A human follow-up lands after round 1 went out and before round 2.
        store.append_input("follow_up", {"question": "check payments too"})
        return _tool_rounds(1)[0]

    loop.model = model = ScriptedModel(
        [tool_round_then_follow_up, *_tool_rounds(1, start=2), Crash("in flight")]
    )
    with pytest.raises(Crash):
        loop.run(request)
    assert list(store.steps) == ["ctx0:round-1", "ctx0:round-2"]
    first = store.steps["ctx0:round-1"]["response"]["context"]
    second = store.steps["ctx0:round-2"]["response"]["context"]
    assert first["input_watermark"] == 0 and second["input_watermark"] == 1
    # Round 1 saw no input; round 2 saw the follow-up as its trailing message.
    assert all(
        "investigation_inputs" not in m["content"] for m in model.calls[0].messages
    )
    trailing = model.calls[1].messages[-1]
    assert trailing["role"] == "user" and "investigation_inputs" in trailing["content"]
    assert '"question":"check payments too"' in trailing["content"]
    assert second["input_snapshot_hash"] == messages_hash(model.calls[1].messages)

    transcript = _transcript(store, request)  # used to raise INCOMPATIBLE_STATE
    # The inputs are a per-round trailing message, never durable transcript.
    assert all(
        "investigation_inputs" not in str(m.get("content")) for m in transcript.messages
    )
    assert transcript.next_round == 3
    third, model3 = _restart(loop, store, [report_from_transcript])
    outcome = third.resume(transcript)
    assert outcome.execution == "completed", outcome.handoff_reasons
    # The resumed round sends the same follow-up again, in the same position.
    assert model3.calls[0].messages[-1] == trailing


def test_a_run_with_a_follow_up_present_from_the_start_rebuilds_its_first_round():
    """Same finding, the reviewer's reproduction: one follow_up row present
    before round 1 -- ``rebuild_transcript(store.snapshot())`` must reproduce
    round 1's recorded hash instead of failing closed."""
    loop, request, model, _, store, _ = _wide(
        replies=[*_tool_rounds(1), Crash("in flight")]
    )
    seeded = MemoryStepStore(
        budget_limit=12,
        deadline=store.deadline,
        clock=loop.clock,
        run_id=request.run_id,
        inputs=[{"sequence": 1, "kind": "follow_up", "content": {"text": "hi"}}],
    )
    loop.store = seeded
    with pytest.raises(Crash):
        loop.run(request)
    assert seeded.steps["ctx0:round-1"]["response"]["context"]["input_watermark"] == 1
    transcript = _transcript(seeded, request)
    assert transcript.next_round == 2 and transcript.messages[-1]["role"] == "tool"
    second, model2 = _restart(loop, seeded, [report_from_transcript])
    assert second.resume(transcript).execution == "completed"
    assert model2.calls[0].messages[-1] == model.calls[0].messages[-1]


def test_a_follow_up_row_missing_from_the_snapshot_fails_the_rebuild_closed():
    """The recorded hash names an input the snapshot no longer carries: the
    bytes cannot be reproduced, so the rebuild refuses (C3 §5), it does not
    guess."""
    loop, request, _, _, store, _ = _wide(replies=[*_tool_rounds(1), Crash("x")])
    seeded = MemoryStepStore(
        budget_limit=12,
        deadline=store.deadline,
        clock=loop.clock,
        run_id=request.run_id,
        inputs=[{"sequence": 1, "kind": "follow_up", "content": {"text": "hi"}}],
    )
    loop.store = seeded
    with pytest.raises(Crash):
        loop.run(request)

    def rebuild(snapshot):
        return rebuild_transcript(
            snapshot,
            run_id=request.run_id,
            authorized_targets=request.scope.target_ids,
            input=request.as_input(),
        )

    # With the inputs present the rebuild reproduces round 1's bytes; the
    # same snapshot minus its inputs cannot, and refuses. Asserting both on
    # one snapshot is what makes this test discriminate (independent review,
    # PR #31 P3-1): a rebuild that ignored inputs altogether would fail the
    # first half, one that fabricated them would fail the second.
    snapshot = seeded.snapshot()
    assert [row["sequence"] for row in snapshot["inputs"]] == [1]
    assert rebuild(snapshot).next_round == 2
    snapshot["inputs"] = []
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        rebuild(snapshot)


# --- credential redaction on the model-facing input projection ---------------


def test_the_input_projection_redacts_credential_bearing_spans_before_the_model():
    """codex review (PR #31, security P2): allowlisted free text reached the
    model unchanged, so ``Authorization: Bearer …``, ``api_key=…`` or a URL
    with userinfo crossed the credential boundary (PRODUCT-CONSTRAINTS
    "Credentials and secret-bearing raw inputs must not enter prompts").
    The projection redacts such spans with a fixed marker and keeps the rest
    of the note; the stored row is not touched."""
    from opspilot.investigation.context import inputs_message, project_input_content

    content = {
        "text": (
            "retry with Authorization: Bearer eyJhbGciOi.sk-secret-1 against "
            "https://ops:hunter2@metrics.internal/api?api_key=AKIA1234SECRET&q=1 "
            "then check the payments pod"
        ),
        "question": "why does token=tok_live_99 still fail?",
        "channel": "web",
    }
    projected = project_input_content(content)
    for secret in (
        "eyJhbGciOi.sk-secret-1",
        "hunter2",
        "AKIA1234SECRET",
        "tok_live_99",
    ):
        assert secret not in str(projected), (secret, projected)
    assert "check the payments pod" in projected["text"]
    assert projected["text"].startswith("retry with Authorization: ")
    assert "metrics.internal/api" in projected["text"]
    assert projected["question"].startswith("why does token=")
    assert projected["question"].endswith(" still fail?")
    assert projected["channel"] == "web"
    message = inputs_message([{"sequence": 1, "kind": "follow_up", "content": content}])
    assert message is not None and "hunter2" not in message["content"]


def test_the_input_projection_leaves_an_ordinary_note_unchanged():
    from opspilot.investigation.context import project_input_content

    note = "please check the payments pod after 10:30; error: connection refused"
    assert project_input_content({"text": note, "question": "why?"}) == {
        "text": note,
        "question": "why?",
    }


def test_a_redacted_follow_up_still_rebuilds_byte_for_byte():
    """The redaction is part of the shared projection, so the bytes the round
    sent and the bytes the rebuild reproduces agree -- and neither carries
    the secret."""
    loop, request, model, _, store, _ = _wide(replies=[*_tool_rounds(1), Crash("x")])
    store.append_input("follow_up", {"text": "use api_key=sk-live-42 for the probe"})
    with pytest.raises(Crash):
        loop.run(request)
    sent = model.calls[0].messages[-1]["content"]
    assert "investigation_inputs" in sent and "sk-live-42" not in sent
    assert "for the probe" in sent
    transcript = _transcript(store, request)  # hash check passes
    assert transcript.next_round == 2
    second, model2 = _restart(loop, store, [report_from_transcript])
    assert second.resume(transcript).execution == "completed"
    assert model2.calls[0].messages[-1]["content"] == sent
    # The business record keeps the operator's raw text.
    assert store.snapshot()["inputs"][0]["content"]["text"].endswith("for the probe")
    assert "sk-live-42" in store.snapshot()["inputs"][0]["content"]["text"]


def test_redact_credentials_reuses_the_registry_rules_and_is_deterministic():
    from opspilot.tools.registry import redact_credentials

    cases = {
        "Authorization: Bearer abc.def.ghi": "Authorization: [REDACTED_CREDENTIAL]",
        "x-api-key=sk-123 rest": "x-api-key=[REDACTED_CREDENTIAL] rest",
        "password: hunter2": "password: [REDACTED_CREDENTIAL]",
        "https://user:pw@host/p?token=t1&q=2": (
            "https://[REDACTED_CREDENTIAL]@host/p?token=[REDACTED_CREDENTIAL]&q=2"
        ),
        "bearer AbCdEf0123456789": "bearer [REDACTED_CREDENTIAL]",
        "the author_filter=alice option": "the author_filter=alice option",
        "label_key=env and group_by_key=pod": "label_key=env and group_by_key=pod",
        "no secrets here": "no secrets here",
    }
    for text, expected in cases.items():
        assert redact_credentials(text) == expected, text
        assert redact_credentials(redact_credentials(text)) == expected  # idempotent
