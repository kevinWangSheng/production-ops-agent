"""M1-01 loop-limits contract (docs/tasks/2026-09-28-m1-01-loop-limits.md).

Independent, pre-implementation contract tests for L1-L5: the user decided
2026-09-28 to drop the per-Run budget scaffolding (4 model requests, 20 tool
calls, 240 s of tool time, 16384-token completions, a 1800 s Run wall) and
align the loop with upstream HolmesGPT -- one anti-loop ceiling
(``max_steps`` defaults to 100), a model that decides for itself when to stop
calling tools and answer, and no budget sentence in the system prompt.

L1a and L3a (added to the contract 2026-09-28 after independent review, see
the task record's "合同补充" section) are written the same way, against
``opspilot/investigation/limits.py`` at commit c1d5d82, before L1a/L3a were
implemented: L1a (no-tool-call replies end the Run immediately, an invalid
one forces exactly one final-report retry rather than another tool round)
and L3a (the input budget must not shrink below its pre-2026-09-28 floor,
and the HTTP request-byte ceiling must not trip before that budget does).

Written from the public interface only (no implementation code read): the
investigation loop's observable request/response shape
(``tests.m1_investigation_support.assemble``), the tool executor's observable
refusal reasons (``tests.m1_tool_support.build``), and the workbench's
observable ``submit()`` deadline. Every assertion tied to a contract number
uses the literal from the task record, not a re-import of the constant it is
checking, so a test cannot go green just because some other constant drifted
to match it.
"""

from __future__ import annotations

import json
from datetime import timedelta

from opspilot.investigation.context import estimate_tokens
from opspilot.investigation.limits import (
    M1_FROZEN_LIMITS,
    MAX_CONTEXT_TOKENS,
    MAX_HTTP_REQUEST_BYTES,
    MAX_MODEL_REQUESTS_PER_RUN,
    MAX_OUTPUT_TOKENS,
    RUN_WALL_SECONDS,
)
from opspilot.investigation.loop import (
    InvestigationRequest,
    ModelCall,
    serialized_request,
)
from opspilot.tools import TransportResponse
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    assemble,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_tool_support import (
    WINDOW_START,
    FakeClock,
    FakeTransport,
    body,
    build,
    registration,
)
from tests.m1_tool_support import request as make_tool_request
from tests.m1_web_support import build_workbench, submit_incident

_FORBIDDEN_BUDGET_PHRASES = (
    "You have at most",
    "twenty tool queries",
    "reserved for the final report",
    "Gather multiple useful independent queries per turn",
)
_REQUIRED_STOP_SENTENCE = (
    "Use the tools as needed until the evidence is sufficient, then return the report."
)
_FINAL_REPORT_MARKER = "Collection is now CLOSED"
# Not parseable JSON (a Markdown fence), the L1a example of an invalid
# non-tool-call reply -- distinct from a citation failure, so it exercises
# ``parse_report``'s own REPORT_INVALID path rather than the evidence check.
_INVALID_FENCE_REPLY = '```json\n{"not": "bare json"}\n```'


def _report_json_with_no_claims() -> str:
    """A schema-valid report that cites no evidence at all.

    Needed for the L1a tests below: they end a Run before any tool ever ran,
    so no ``evidence_id`` exists yet for a claim to cite. ``incomplete`` +
    ``inconclusive`` + a nonempty ``gaps`` + zero claims satisfies
    ``ReportV2.coherent`` without needing a supported fact.
    """
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "incomplete",
        "conclusion": "inconclusive",
        "summary": "Ending without querying any tool; no claims are made.",
        "claims": [],
        "gaps": ["No evidence was gathered."],
        "next_steps": [],
    }
    return json.dumps(payload, ensure_ascii=False)


# -- fixture sanity (must stay green before and after the implementation) ----


def test_sanity_single_tool_call_still_succeeds():
    """Baseline executor plumbing, unrelated to any of L1-L5's ceilings."""
    executor, transport, sink, clock = build()
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 1}]), data_as_of=WINDOW_START
    )
    outcome = executor.execute(make_tool_request())
    assert outcome.status == "ok"


def test_sanity_assemble_fixture_completes_a_tool_then_report_run():
    """Baseline loop plumbing: one tool round, then a report citing it."""
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"


# -- L1: the model-request ceiling and the forced final-report path ---------


def test_l1_default_model_request_ceiling_is_100():
    assert MAX_MODEL_REQUESTS_PER_RUN == 100
    assert M1_FROZEN_LIMITS.model_requests == 100
    executor, *_ = build()
    default_request = InvestigationRequest(
        run_id=executor.scope.run_id,
        question="Why is checkout erroring?",
        scope=executor.scope,
        tool_schemas=TOOL_SCHEMAS,
    )
    assert default_request.model_requests == 100


def test_l1_non_final_rounds_keep_tools_and_end_early_on_a_valid_report():
    """5 planned rounds, well past the old 4-request ceiling: a mid-course
    report with no tool calls still ends the Run, and every round sent
    carried tool definitions with no final-report instruction appended."""
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=5,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert outcome.model_requests_used == 2
    assert len(model.calls) == 2
    for call in model.calls:
        assert call.tools == TOOL_SCHEMAS
        assert not any(
            _FINAL_REPORT_MARKER in str(message.get("content"))
            for message in call.messages
        )


def test_l1_forced_final_report_request_only_when_one_slot_remains():
    """With a small cap (3) the last round -- and only the last -- drops
    tools and appends the final-report instruction plus the coverage
    message, exactly like today's forced path."""
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(tool_calls=[tool_call(call_id="c1")], finish="tool_calls"),
            reply(tool_calls=[tool_call(call_id="c2")], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=3,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert len(model.calls) == 3
    assert model.calls[0].tools == TOOL_SCHEMAS
    assert model.calls[1].tools == TOOL_SCHEMAS
    assert model.calls[2].tools is None
    for call in model.calls[:2]:
        assert not any(
            _FINAL_REPORT_MARKER in str(message.get("content"))
            for message in call.messages
        )
    assert any(
        _FINAL_REPORT_MARKER in str(message.get("content"))
        for message in model.calls[2].messages
    )


# -- L1a: a no-tool-call reply ends the round immediately, valid or not -----


def test_l1a_valid_report_with_no_tool_calls_ends_the_run_on_the_first_request():
    """Not new on its own (an early valid report already ended the Run before
    L1a); recorded here so the L1a section states the whole contrasting pair
    at model_requests=100."""
    loop, request, model, transport, store, sink = assemble(
        replies=[reply(content=_report_json_with_no_claims())],
        model_requests=100,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert outcome.model_requests_used == 1
    assert len(model.calls) == 1
    assert model.calls[0].tools == TOOL_SCHEMAS


def test_l1a_invalid_non_tool_reply_forces_exactly_one_final_report_request():
    """A non-final round with an invalid, no-tool-call reply must go straight
    to the forced final-report request (no tools, JSON mode, the final-report
    instruction and coverage message) -- not back to another tool-bearing
    round, even though 98 requests of the 100-request budget remain."""
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(content=_INVALID_FENCE_REPLY, finish="stop"),
            reply(content=_report_json_with_no_claims(), finish="stop"),
        ],
        model_requests=100,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert outcome.model_requests_used == 2
    assert len(model.calls) == 2
    assert model.calls[0].tools == TOOL_SCHEMAS
    assert model.calls[1].tools is None
    assert any(
        _FINAL_REPORT_MARKER in str(message.get("content"))
        for message in model.calls[1].messages
    )


def test_l1a_invalid_forced_final_report_ends_with_report_invalid():
    """When the forced final-report request (triggered by the first invalid
    reply) is itself invalid, the Run ends REPORT_INVALID after exactly two
    requests -- it must not spend a third request on another tool round."""
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(content=_INVALID_FENCE_REPLY, finish="stop"),
            reply(content=_INVALID_FENCE_REPLY, finish="stop"),
        ],
        model_requests=100,
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert outcome.model_requests_used == 2
    assert len(model.calls) == 2
    assert model.calls[1].tools is None


# -- L2: no more per-Run tool-call-count or cumulative-tool-time ceiling ----


def test_l2_more_than_twenty_tool_calls_in_one_run_are_not_refused():
    executor, transport, sink, clock = build()
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 1}]), data_as_of=WINDOW_START
    )
    outcomes = [
        executor.execute(make_tool_request(step_id=f"step-{i}")) for i in range(25)
    ]
    assert executor.operations_used == 25
    assert all(outcome.status == "ok" for outcome in outcomes)
    assert not any(
        outcome.reason == "OPERATION_BUDGET_EXHAUSTED" for outcome in outcomes
    )


def test_l2_more_than_240_cumulative_tool_seconds_are_not_refused():
    clock = FakeClock()
    transport = FakeTransport(clock=clock, duration=30.0)
    executor, transport, sink, clock = build(
        clock=clock,
        transport=transport,
        registrations=[registration(request_timeout_seconds=30.0)],
    )
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 1}]), data_as_of=WINDOW_START
    )
    outcomes = [
        executor.execute(make_tool_request(step_id=f"step-{i}")) for i in range(9)
    ]
    assert executor.tool_seconds_used > 240.0
    assert all(outcome.status == "ok" for outcome in outcomes)
    assert not any(outcome.reason == "TIME_BUDGET_EXHAUSTED" for outcome in outcomes)


def test_l2_single_tool_call_timeout_ceiling_is_still_enforced():
    """L2 removes the per-Run cumulative caps only; a per-call read that
    overruns its own registered timeout is still refused."""
    clock = FakeClock()
    transport = FakeTransport(clock=clock, duration=6.0)
    executor, transport, sink, clock = build(
        clock=clock,
        transport=transport,
        registrations=[registration(request_timeout_seconds=5.0)],
    )
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 1}]), data_as_of=WINDOW_START
    )
    outcome = executor.execute(make_tool_request())
    assert outcome.status == "timeout"
    assert outcome.reason == "GATEWAY_TIMEOUT"


def test_l2_result_byte_ceiling_is_still_enforced():
    executor, transport, sink, clock = build(
        registrations=[registration(max_result_bytes=64, max_view_bytes=64)]
    )
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": "x" * 200}]),
        data_as_of=WINDOW_START,
    )
    outcome = executor.execute(make_tool_request())
    assert outcome.status == "error"
    assert outcome.reason == "RESULT_TOO_LARGE"


# -- L3: the per-request completion budget is 65536, context stays 131072 ---


def test_l3_model_max_tokens_is_65536_and_context_budget_grew_under_l3a():
    """L3 alone left ``MAX_CONTEXT_TOKENS`` at 131_072 ("unchanged"); L3a
    (2026-09-28 contract supplement, task record) supersedes that specific
    clause and moves it to the official DeepSeek Flash context window,
    1_048_576 -- see ``opspilot/investigation/limits.py`` for the sourced
    value and date. ``MAX_OUTPUT_TOKENS`` is untouched by L3a."""
    assert MAX_OUTPUT_TOKENS == 65_536
    assert M1_FROZEN_LIMITS.output_tokens == 65_536
    assert MAX_CONTEXT_TOKENS == 1_048_576
    assert M1_FROZEN_LIMITS.context_tokens == 1_048_576
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert [call.max_tokens for call in model.calls] == [65_536, 65_536]


# -- L3a: input budget must not shrink; the byte ceiling must not trip first -


def test_l3a_input_budget_is_not_smaller_than_before_the_2026_09_28_decision():
    """L3 alone (context unchanged at 131072, output raised to 65536) shrinks
    the input budget from 114688 (131072-16384, the pre-2026-09-28 floor) to
    65536. L3a requires the context ceiling to grow enough that the input
    budget -- read live from the limits module, no value hardcoded here since
    the task record has not pinned the official DeepSeek window yet -- is
    back to at least that floor."""
    input_budget = MAX_CONTEXT_TOKENS - MAX_OUTPUT_TOKENS
    assert input_budget >= 114_688
    assert M1_FROZEN_LIMITS.context_tokens - M1_FROZEN_LIMITS.output_tokens >= 114_688


def test_l3a_http_request_byte_ceiling_does_not_trip_before_a_full_budget_request():
    """A request sized to the (live) input budget must not be refused
    REQUEST_TOO_LARGE by the byte ceiling before the context management ever
    gets a chance to compact it. Sizing uses the loop's own token estimator
    (``estimate_tokens``, 4 bytes/token) rather than a guessed ratio, and
    reads both ceilings from the limits module rather than a hardcoded
    number, so the check tracks whatever context window L3a settles on."""
    input_budget = MAX_CONTEXT_TOKENS - MAX_OUTPUT_TOKENS
    padding = "x" * (input_budget * 4)
    call = ModelCall(
        messages=({"role": "user", "content": padding},),
        tools=None,
        json_mode=False,
        max_tokens=MAX_OUTPUT_TOKENS,
        timeout_seconds=30.0,
    )
    assert estimate_tokens(call.messages) >= input_budget
    assert len(serialized_request(call)) <= MAX_HTTP_REQUEST_BYTES


# -- L4: the Run wall is 7200 s, only as a stuck-run backstop ---------------


def test_l4_run_wall_seconds_constant_is_7200():
    assert RUN_WALL_SECONDS == 7200.0


def test_l4_submitted_run_deadline_is_submission_time_plus_7200_seconds():
    app, workbench, clock = build_workbench()
    # build_workbench() pins run_seconds=600 for fast, unrelated tests;
    # exercise the frozen default this contract item changes instead.
    workbench.run_seconds = RUN_WALL_SECONDS
    before = clock.now()
    submit_incident(app, key="l4-deadline")
    run_id = workbench.list_incidents()[0].current_run_id
    deadline = workbench.incidents.runs[run_id]["deadline"]
    assert deadline == before + timedelta(seconds=7200)


# -- L5: the budget sentence is gone from the system prompt -----------------


def test_l5_budget_sentence_removed_and_stop_sentence_present():
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    system = model.calls[0].messages[0]
    assert system["role"] == "system"
    text = system["content"]
    for forbidden in _FORBIDDEN_BUDGET_PHRASES:
        assert forbidden not in text, forbidden
    assert _REQUIRED_STOP_SENTENCE in text
