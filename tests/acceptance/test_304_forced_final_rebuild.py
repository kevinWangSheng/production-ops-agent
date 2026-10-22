"""#304 acceptance: a forced final round (L1a/B3) can be rebuilt and resumed.

External behaviour only (docs/tasks/2026-10-22-304-forced-final-rebuild-contract.md
section 5): a Run whose final report was rejected is handed off
(``REPORT_INVALID``); after a human ``follow_up`` the same Run rebuilds its
transcript from the committed rows and resumes with a real model call, instead
of failing closed with ``INCOMPATIBLE_STATE``. A forced final round that was
answered with an acceptable report but died before its conclusion row also
rebuilds. Fail-closed stays for rows that really differ. Written without the
implementation: nothing here names a helper the fix may add.

``MemoryStepStore`` keeps committed rows across loop instances; a second
``InvestigationLoop`` over the same store is the restarted worker.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from opspilot.investigation.context import (
    ContextError,
    context_policy_revision,
    messages_hash,
    rebuild_transcript,
)
from opspilot.investigation.limits import RunLimits
from opspilot.investigation.loop import (
    DISCIPLINE_VARIANT,
    InvestigationLoop,
    prompt_revision_versions,
)
from opspilot.investigation.store import MemoryStepStore
from tests.m1_investigation_support import (
    ScriptedModel,
    assemble,
    reply,
    report_from_transcript,
    report_json,
    tool_call,
)

WIDE = RunLimits(model_requests=120)
FOLLOW_UP = "also compare against the payments service"
EVENT_TEXT = "alertmanager fired HighErrorRate on checkout"


class Crash(RuntimeError):
    pass


def _bad(call):
    return reply(content="not a report")


def _bad_citation(call):
    return reply(content=report_json(evidence_id="ev-that-was-never-delivered"))


def _tool_rounds(count):
    return [
        reply(
            tool_calls=[
                tool_call(
                    call_id=f"call-{i}",
                    arguments=f'{{"expr":"rate(http_errors[{i}m])"}}',
                )
            ],
            finish="tool_calls",
        )
        for i in range(1, count + 1)
    ]


def _assemble(replies, *, model_requests=8):
    loop, request, model, _, store, _ = assemble(
        replies=replies, budget_limit=12, model_requests=model_requests
    )
    return loop, replace(request, limits=WIDE), model, store


def _transcript(store, request):
    return rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
        input=request.as_input(),
    )


def _restart(loop, store, replies):
    model = ScriptedModel(replies)
    return (
        InvestigationLoop(
            model=model, executor=loop.executor, store=store, clock=loop.clock
        ),
        model,
    )


def _handed_off_after_rejected_final(first_bad=_bad, final_bad=_bad):
    loop, request, model, store = _assemble(
        [*_tool_rounds(1), first_bad, final_bad], model_requests=8
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert outcome.model_requests_used == 3
    return loop, request, model, store


def _die_on_conclusion(loop, store, request):
    class DieOnConclusion(MemoryStepStore):
        armed = True

        def commit_step(self, logical_key, response):
            if self.armed and logical_key.startswith("conclusion:"):
                self.armed = False
                raise Crash("killed before the conclusion row")
            return super().commit_step(logical_key, response)

    dying = DieOnConclusion(
        budget_limit=12,
        deadline=store.deadline,
        clock=loop.clock,
        run_id=request.run_id,
    )
    loop.store = dying
    return dying


def _message_text(message):
    return message["content"] if isinstance(message["content"], str) else str(message)


# -- 1. REPORT_INVALID hand-off, then follow_up on the same Run ------------------


@pytest.mark.parametrize(
    "first_bad",
    [_bad, _bad_citation],
    ids=["unparseable-reply", "report-citing-unknown-evidence"],
)
def test_follow_up_after_a_rejected_forced_final_round_resumes_the_same_run(first_bad):
    loop, request, model, store = _handed_off_after_rejected_final(first_bad=first_bad)
    store.advance_generation()
    store.append_input("follow_up", {"question": FOLLOW_UP})

    transcript = _transcript(store, request)  # must not be INCOMPATIBLE_STATE

    second, model2 = _restart(loop, store, [report_from_transcript])
    outcome = second.resume(transcript)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert outcome.handoff_reasons == ()
    assert len(model2.calls) >= 1
    first_input = json.dumps(model2.calls[0].messages)
    assert FOLLOW_UP in first_input
    # Budget continues from the hand-off total (3 used), it does not reset.
    assert outcome.model_requests_used == 3 + len(model2.calls)
    assert outcome.final_step_id is not None


def test_follow_up_after_a_rejected_forced_final_round_can_keep_investigating():
    loop, request, model, store = _handed_off_after_rejected_final()
    store.advance_generation()
    store.append_input("follow_up", {"question": FOLLOW_UP})
    transcript = _transcript(store, request)
    second, model2 = _restart(loop, store, [_tool_rounds(1)[0], report_from_transcript])
    outcome = second.resume(transcript)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert len(model2.calls) == 2
    assert FOLLOW_UP in json.dumps(model2.calls[0].messages)
    assert outcome.model_requests_used == 5


# -- 2. an event input plus a follow_up (F2 C4-A assertion 2 shape) --------------


def test_event_and_follow_up_after_a_rejected_forced_final_round_both_reach_the_model():
    loop, request, model, store = _handed_off_after_rejected_final()
    store.append_input("event", {"text": EVENT_TEXT})
    store.advance_generation()
    store.append_input("follow_up", {"question": FOLLOW_UP})
    transcript = _transcript(store, request)
    second, model2 = _restart(loop, store, [report_from_transcript])
    outcome = second.resume(transcript)
    assert outcome.execution == "completed", outcome.handoff_reasons
    sent = json.dumps(model2.calls[0].messages)
    assert EVENT_TEXT in sent and FOLLOW_UP in sent


# -- 3. crash between an accepted forced-final report and its conclusion ----------


def test_restart_after_an_accepted_forced_final_report_publishes_it_without_a_new_call():
    loop, request, model, store = _assemble(
        [*_tool_rounds(1), _bad, report_from_transcript], model_requests=8
    )
    dying = _die_on_conclusion(loop, store, request)
    with pytest.raises(Crash):
        loop.run(request)
    assert [k for k in dying.steps] == ["ctx0:round-1", "ctx0:round-2", "ctx0:round-3"]
    assert dying.steps["ctx0:round-3"]["response"]["context"]["final"] is True

    transcript = _transcript(dying, request)  # must not be INCOMPATIBLE_STATE
    second, model2 = _restart(loop, dying, [])  # any model call would raise
    outcome = second.resume(transcript)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert model2.calls == []
    assert outcome.model_requests_used == 3  # nothing new was spent
    assert outcome.final_step_id is not None
    assert len(outcome.evidence_ids) == 1


# -- 4. controls: a budget-driven final round has always rebuilt ------------------


def test_a_budget_driven_final_round_rebuilds_and_a_follow_up_resumes_it():
    loop, request, model, store = _assemble([*_tool_rounds(1), _bad], model_requests=2)
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert store.steps["ctx0:round-2"]["response"]["context"]["final"] is True

    _transcript(store, request)  # no failure before the follow_up either
    store.advance_generation()
    store.append_input("follow_up", {"question": FOLLOW_UP})
    transcript = _transcript(store, request)
    second, model2 = _restart(loop, store, [report_from_transcript])
    # The follow-up request is not bounded by the old two-request plan once the
    # operator extends the budget; here the budget is spent, so the resumed
    # attempt must hand off for the budget, not fail closed on the rebuild.
    outcome = second.resume(transcript)
    assert outcome.execution in {"completed", "budget_exhausted"}
    assert outcome.handoff_reasons != ("INCOMPATIBLE_STATE",)


# -- 5. fail-closed stays ---------------------------------------------------------


def _tampered(store, request, mutate):
    snapshot = store.snapshot()
    mutate(snapshot)
    return rebuild_transcript(
        snapshot,
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
        input=request.as_input(),
    )


def _step(snapshot, key):
    return next(s for s in snapshot["steps"] if s["logical_key"] == key)


def test_an_edited_rejected_reply_still_fails_the_rebuild_closed():
    loop, request, model, store = _handed_off_after_rejected_final()
    store.advance_generation()
    store.append_input("follow_up", {"question": FOLLOW_UP})

    def edit(snapshot):
        _step(snapshot, "ctx0:round-2")["response"]["assistant"]["content"] = (
            "edited after the fact"
        )

    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        _tampered(store, request, edit)


def test_a_corrupted_hash_on_the_forced_final_row_still_fails_closed():
    loop, request, model, store = _handed_off_after_rejected_final()

    def corrupt(snapshot):
        context = _step(snapshot, "ctx0:round-3")["response"]["context"]
        context["input_snapshot_hash"] = "0" * 64

    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        _tampered(store, request, corrupt)


def test_a_forced_final_row_that_claims_no_failed_reply_before_it_fails_closed():
    """The row was sent with a retry message; if the committed reply before it
    is replaced by an acceptable report there is nothing to justify that
    message, so the recorded bytes cannot be reproduced."""
    loop, request, model, store = _handed_off_after_rejected_final()

    acceptable = report_from_transcript(model.calls[1]).content

    def make_previous_acceptable(snapshot):
        _step(snapshot, "ctx0:round-2")["response"]["assistant"]["content"] = acceptable

    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        _tampered(store, request, make_previous_acceptable)


def test_a_retry_feedback_row_from_a_different_failure_fails_closed():
    """Swap the failing reply for another failure kind: the feedback sent in
    round 3 named the original failure, so the bytes no longer match."""
    loop, request, model, store = _handed_off_after_rejected_final(first_bad=_bad)

    def other_failure(snapshot):
        _step(snapshot, "ctx0:round-2")["response"]["assistant"]["content"] = (
            report_json(evidence_id="ev-that-was-never-delivered")
        )

    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        _tampered(store, request, other_failure)


# -- 6. model-visible bytes and revisions are unchanged ---------------------------


def test_the_forced_final_round_messages_and_revisions_are_unchanged():
    loop, request, model, store = _handed_off_after_rejected_final()
    final_call = model.calls[-1]
    tail = [_message_text(m) for m in final_call.messages[-3:]]
    assert tail[0] == (
        "Collection is now CLOSED. This is the final report request. No further "
        "tools or fresh queries are permitted. Return only one json object in the "
        "report format already given. Do not emit DSML, tool invocation markup, "
        "Markdown fences or requests for more tools."
    )
    assert tail[1] == (
        "The previous reply was not accepted as the final report (REPORT_INVALID). "
        "JSON parse error: Expecting value: line 1 column 1 (char 0). This is the "
        "forced final retry: return exactly one corrected json object in the "
        "report format already given, citing only evidence_id values already "
        "delivered in this transcript."
    )
    assert tail[2].startswith("Run coverage summary, computed from the structured")
    # The recorded hash is of exactly these bytes.
    row = store.steps["ctx0:round-3"]["response"]["context"]
    assert row["final"] is True
    assert row["input_snapshot_hash"] == messages_hash(final_call.messages)
    # No revision bump (D-R4).
    assert context_policy_revision() == "ctx-ctx-policy-v1-a30e65ea52f4"
    assert prompt_revision_versions(DISCIPLINE_VARIANT) == {
        "prompt_revision": "prompt-replay-candidate-dd9018031487"
    }


def test_a_rebuild_reproduces_the_bytes_the_forced_final_round_sent():
    loop, request, model, store = _handed_off_after_rejected_final()
    transcript = _transcript(store, request)
    sent = list(model.calls[-1].messages)
    # Everything the final round sent before its own trailing messages is the
    # rebuilt durable transcript; the trailing three are not durable.
    assert list(transcript.messages)[: len(sent) - 3] == sent[:-3]
