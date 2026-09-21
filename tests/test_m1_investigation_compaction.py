"""Context management of the investigation loop (HolmesGPT's two mechanisms).

Mechanism 1: a single oversized tool view reaches the model as a provenance
stub. Mechanism 2: before a call that would pass the compaction threshold,
the history after the fixed prefix is summarised by one model request and
replaced by a provenance digest plus that summary. Both are deterministic
given the committed rows, so a rebuilt attempt sees the same bytes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from opspilot.investigation import context as ctx
from opspilot.investigation.context import (
    COMPACTION_INSTRUCTION,
    compaction_message,
    context_policy_revision,
    estimate_tokens,
    rebuild_transcript,
)
from opspilot.investigation.limits import RunLimits
from opspilot.investigation.loop import (
    FINAL_REPORT_INSTRUCTION,
    InvestigationLoop,
    investigation_versions,
)
from opspilot.tools import TransportResponse
from opspilot.tools.registry import canonical
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    ScriptedModel,
    assemble,
    reply,
    report_json,
    tool_call,
)
from tests.m1_tool_support import WINDOW_START, body, build, registration


class Crash(RuntimeError):
    pass


def _rounds(count, *, start=1):
    return [
        reply(tool_calls=[tool_call(call_id=f"call-{start + i}")], finish="tool_calls")
        for i in range(count)
    ]


def _cite_first(store):
    """Final report citing the first committed evidence id (survives compaction)."""

    def final(call):
        for results in store.tool_results.values():
            for item in results:
                return reply(
                    content=report_json(evidence_id=item["result"]["evidence_id"])
                )
        return reply(content=report_json(evidence_id="missing"))

    return final


def _summary(
    text="Facts: checkout errors observed. Unknown: cause. Next: final report.",
):
    return reply(content=text, finish="stop")


def _transcript(store, request):
    return rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
        input=request.as_input(),
    )


def _limits_between_rounds(k):
    """Limits whose compaction threshold falls between k-1 and k tool groups.

    Measured on a dry run with wide limits, so the test does not depend on
    the byte size of the discipline text. With ``k=2`` the loop compacts
    before the call that would carry two complete tool groups.
    """
    loop, request, model, _, _, _ = assemble(
        replies=[*_rounds(k + 1)], budget_limit=16, model_requests=k + 3
    )
    request = replace(request, limits=RunLimits(model_requests=16))
    loop.run(request)  # the scripted model runs dry afterwards; that is fine
    tools = tuple(TOOL_SCHEMAS)
    before = estimate_tokens(model.calls[k - 1].messages, tools)
    after = estimate_tokens(model.calls[k].messages, tools)
    assert after > before
    output = 64
    budget = int((before + after) / 2 / ctx.CONTEXT_POLICY.compaction_pct)
    return RunLimits(
        model_requests=16, context_tokens=budget + output, output_tokens=output
    )


def _compacting_run(*, extra_replies=(), model_requests=8, limits=None):
    limits = limits or _limits_between_rounds(2)
    loop, request, model, transport, store, _ = assemble(
        replies=[], budget_limit=16, model_requests=model_requests
    )
    request = replace(request, limits=limits)
    replies = [*_rounds(2), _summary(), *extra_replies]
    model = ScriptedModel(replies + [_cite_first(store)])
    loop.model = model
    return loop, request, model, transport, store


# --- mechanism 2: compaction ------------------------------------------------


def test_history_is_compacted_before_a_call_that_would_pass_the_threshold():
    loop, request, model, _, store = _compacting_run()
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    # Call 0, 1: tool rounds. Call 2: the compaction request (no tools, no
    # JSON mode, instruction last). Call 3: round 3 on the compacted context.
    compaction = model.calls[2]
    assert compaction.tools is None and compaction.json_mode is False
    assert compaction.messages[-1] == {
        "role": "user",
        "content": COMPACTION_INSTRUCTION,
    }
    assert [m["role"] for m in compaction.messages[:-1]].count("tool") == 2
    row = store.steps["ctx0:compact-1"]["response"]
    assert row["kind"] == "compaction" and row["compaction"]["accepted"] is True
    assert row["compaction"]["folded_step_ids"] == [
        str(store.step_ids["ctx0:round-1"]),
        str(store.step_ids["ctx0:round-2"]),
    ]
    digest = row["compaction"]["digest"]
    assert len(digest["views"]) == 2 and len(digest["evidence_ids"]) == 2
    for view in digest["views"]:
        assert view["target_id"] == "checkout-prod"
        assert view["window"] and view["status"] == "ok" and view["adopted"] is True
    prefix = list(model.calls[0].messages)
    sent = list(model.calls[3].messages)
    assert sent[:-1] == prefix
    assert sent[-1] == compaction_message(1, digest, _summary().content)
    assert sent[-1]["content"].startswith("[context compacted rev=1]")
    assert "ctx1:round-3" in store.steps
    assert len(outcome.evidence_ids) == 2
    assert outcome.conclusion["conclusion"]["compactions"] == 1
    assert outcome.model_requests_used == 4


def test_a_rebuild_reproduces_the_compacted_context_byte_for_byte():
    loop, request, model, _, store = _compacting_run()
    loop.run(request)
    transcript = _transcript(store, request)
    last = list(model.calls[-1].messages)
    if last[-1]["content"] == FINAL_REPORT_INSTRUCTION:
        last = last[:-1]
    # The rows also hold the accepted report itself as the last assistant turn.
    assert transcript.messages[-1]["role"] == "assistant"
    assert transcript.messages[:-1] == last
    assert transcript.segment == "ctx1" and transcript.compactions == 1
    assert transcript.next_round == 4
    # Round 3 was committed after the compaction, so it is the only unfolded step.
    assert transcript.folded_since == (store.step_ids["ctx1:round-3"],)
    assert (
        len(transcript.delivered) == 2
    )  # citations come from the rows, not the summary


def test_a_restart_right_after_the_compaction_row_continues_on_the_new_segment():
    loop, request, model, _, store = _compacting_run(extra_replies=[Crash("dead")])
    with pytest.raises(Crash):
        loop.run(request)
    assert "ctx0:compact-1" in store.steps and "ctx1:round-3" not in store.steps
    transcript = _transcript(store, request)
    assert transcript.segment == "ctx1" and transcript.next_round == 3
    second = InvestigationLoop(
        model=ScriptedModel([_cite_first(store)]),
        executor=loop.executor,
        store=store,
        clock=loop.clock,
    )
    outcome = second.resume(transcript)
    assert outcome.execution == "completed"
    sent = list(second.model.calls[0].messages)
    assert sent[-1]["content"].startswith("[context compacted rev=1]")
    assert [m["role"] for m in sent].count("tool") == 0
    assert "ctx1:round-3" in store.steps


def test_a_summary_that_calls_tools_or_says_nothing_hands_off_and_keeps_the_context():
    for bad in (
        reply(tool_calls=[tool_call()], finish="tool_calls"),
        reply(content="  "),
    ):
        loop, request, model, _, store = _compacting_run()
        loop.model = ScriptedModel([*_rounds(2), bad, _cite_first(store)])
        outcome = loop.run(request)
        assert outcome.execution == "failed"
        assert outcome.handoff_reasons == ("COMPACTION_FAILED",)
        assert (
            store.steps["ctx0:compact-1"]["response"]["compaction"]["accepted"] is False
        )
        assert len(loop.model.calls) == 3  # nothing was sent on a broken summary
        transcript = _transcript(store, request)
        assert transcript.segment == "ctx0" and transcript.compactions == 0
        assert [m["role"] for m in transcript.messages].count("tool") == 2
        assert outcome.final_step_id is not None


def test_compaction_needs_two_remaining_slots_or_the_run_hands_off():
    loop, request, model, _, store = _compacting_run(model_requests=3)
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("CONTEXT_EXHAUSTED",)
    assert len(model.calls) == 2 and "ctx0:compact-1" not in store.steps


def test_a_summary_that_still_does_not_fit_is_context_exhausted():
    limits = _limits_between_rounds(2)
    loop, request, model, _, store = _compacting_run(limits=limits)
    loop.model = ScriptedModel(
        [*_rounds(2), _summary("x" * (limits.context_tokens * 8))]
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("CONTEXT_EXHAUSTED",)
    assert len(loop.model.calls) == 3


def test_a_compaction_is_charged_like_any_physical_request():
    loop, request, model, _, store = _compacting_run()
    outcome = loop.run(request)
    assert outcome.model_requests_used == 4
    assert store.usage().model_requests_used == 4
    assert sorted(store.settled.values()) == ["spent"] * 4


# --- mechanism 1: single-view stub -----------------------------------------


def test_an_oversized_tool_view_reaches_the_model_as_a_provenance_stub():
    limits = RunLimits(model_requests=8, context_tokens=6000, output_tokens=200)
    loop, request, model, _, store, _ = assemble(
        replies=[], budget_limit=8, model_requests=4
    )
    # The default registration caps views at 512 bytes; this one lets a large
    # result through so the loop's own single-view cap is what bites.
    executor, transport, _sink, _clock = build(
        registrations=[
            registration(max_result_bytes=2_000_000, max_view_bytes=400_000)
        ],
        clock=loop.clock,
    )
    loop.executor = executor
    request = replace(request, scope=executor.scope, limits=limits)
    transport.response = TransportResponse(
        body=body(
            [{"metric": "checkout", "value": i, "note": "n" * 40} for i in range(200)]
        ),
        data_as_of=WINDOW_START,
    )
    loop.model = ScriptedModel([*_rounds(1), _cite_first(store)])
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    sent = json.loads(loop.model.calls[1].messages[-1]["content"])
    assert sent["spilled"] is True and sent["reason"] == "VIEW_TOO_LARGE"
    assert sent["truncated"] is True and "content" not in sent
    full = next(iter(store.tool_results.values()))[0]["result"]
    assert full["content"] and sent["evidence_id"] == full["evidence_id"]
    assert (
        sent["full_view_sha256"] == hashlib.sha256(canonical(full).encode()).hexdigest()
    )
    assert len(sent["preview"]) <= ctx.CONTEXT_POLICY.stub_preview_chars
    # The rebuilt transcript sends the same stub, and still cites the evidence.
    transcript = _transcript(store, request)
    tool_messages = [m for m in transcript.messages if m["role"] == "tool"]
    assert tool_messages == [loop.model.calls[1].messages[-1]]
    assert [v.evidence_id for v in transcript.delivered] == [full["evidence_id"]]


# --- measurement and versioning ---------------------------------------------


def test_calibration_grows_with_the_provider_count_and_survives_a_rebuild():
    loop, request, _, _, store, _ = assemble(
        replies=[], budget_limit=8, model_requests=4
    )
    request = replace(request, limits=RunLimits(model_requests=8))
    heavy = reply(tool_calls=[tool_call()], finish="tool_calls")
    heavy = replace(heavy, usage={"prompt_tokens": 50_000, "completion_tokens": 1})
    loop.model = ScriptedModel([heavy, _cite_first(store)])
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    recorded = store.steps["ctx0:round-1"]["response"]["context"]
    assert recorded["calibration"] == pytest.approx(
        50_000 / recorded["estimated_prompt_tokens"]
    )
    assert recorded["calibration"] > 1.0
    assert outcome.conclusion["conclusion"]["calibration"] == recorded["calibration"]
    assert _transcript(store, request).calibration == recorded["calibration"]


def test_the_estimator_counts_the_tools_array_and_the_context_policy_is_content_hashed(
    monkeypatch,
):
    messages = [{"role": "user", "content": "x" * 400}]
    assert estimate_tokens(messages, TOOL_SCHEMAS) > estimate_tokens(messages)
    before = context_policy_revision()
    assert before == context_policy_revision()
    assert investigation_versions()["context_policy_revision"] == before
    assert "prompt_revision" in investigation_versions()
    monkeypatch.setattr(ctx, "COMPACTION_INSTRUCTION", "different words")
    assert context_policy_revision() != before


def test_a_compaction_never_takes_the_reserved_final_report_slot():
    """Frozen four: two tool rounds, a compaction, then the report -- never a
    fourth tool round that leaves no slot for the report (review P1-A)."""
    loop, request, model, _, store = _compacting_run(model_requests=4)
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert len(model.calls) == 4 and outcome.model_requests_used == 4
    assert model.calls[2].messages[-1]["content"] == COMPACTION_INSTRUCTION
    assert model.calls[3].tools is None and model.calls[3].json_mode is True
    assert model.calls[3].messages[-1]["content"] == FINAL_REPORT_INSTRUCTION


def test_a_rejected_summary_keeps_its_tool_calls_out_of_the_executable_plan():
    from opspilot.persistence import _tool_plan

    loop, request, model, _, store = _compacting_run()
    loop.model = ScriptedModel(
        [
            *_rounds(2),
            reply(tool_calls=[tool_call(call_id="halluc-1")], finish="tool_calls"),
        ]
    )
    outcome = loop.run(request)
    assert outcome.handoff_reasons == ("COMPACTION_FAILED",)
    row = store.steps["ctx0:compact-1"]["response"]
    assert "tool_calls" not in row["assistant"]
    assert row["rejected_plan"]["tool_calls"][0]["id"] == "halluc-1"
    assert _tool_plan(row) == []


def test_the_digest_lists_only_adopted_evidence_ids():
    from opspilot.investigation.context import fold_digest, revoked_view

    view = {"evidence_id": "ev-1", "adopted": True, "status": "ok", "target_id": "t"}
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [tool_call(call_id="a")]},
        {"role": "tool", "tool_call_id": "a", "content": json.dumps(view)},
        {"role": "assistant", "content": None, "tool_calls": [tool_call(call_id="b")]},
        {
            "role": "tool",
            "tool_call_id": "b",
            "content": json.dumps(revoked_view({**view, "evidence_id": "ev-revoked"})),
        },
    ]
    digest = fold_digest(messages)
    assert digest["evidence_ids"] == ["ev-1"]
    assert len(digest["views"]) == 2 and len(digest["tool_calls"]) == 2
