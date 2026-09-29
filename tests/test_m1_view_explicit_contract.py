"""Contract tests for M1-01 round 2: explicit "unknown" and units in views.

Written from ``docs/tasks/2026-09-27-m1-01-window-points.md``, section "第二轮：
视图显式表达"未知"与单位" (rules A, B and C) -- the public interface of
``opspilot.tools.otel_demo`` and ``opspilot.investigation.loop`` /
``.reports`` / ``.context``, not the implementation, which does not exist yet
on this tree. Hermetic: no network, no PostgreSQL.

Rule D (the ``discipline.py`` wording cleanup) is not covered here; the task
assigns it no dedicated acceptance test.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from opspilot.investigation.context import delivered_view, rebuild_transcript
from opspilot.investigation.loop import prompt_revision_versions
from opspilot.investigation.reports import FINAL_REPORT_INSTRUCTION
from opspilot.persistence import Lease
from opspilot.tools import ReadOnlyToolExecutor, ToolRequest, TransportResponse
from opspilot.tools.otel_demo import (
    TARGET_ID,
    TOOL_SCHEMAS,
    TRACES_TOOL,
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_face,
)
from tests.m1_investigation_support import (
    assemble,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_tool_support import FakeClock, RecordingSink, body
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_TRACES,
    JAEGER_URL,
    NOW,
    PROMETHEUS_URL,
    WINDOW,
    FakeOpener,
    FakeStore,
    _projected,
)

# -- shared trace-executor glue (duplicated, not imported, from
# tests/test_m1_otel_demo_contract.py's private ``_executor``/``_call``/
# ``_lease``/``_input``: those are underscore-private to that module, and the
# sibling contract files in this tree -- test_m1_otel_demo_lookback.py,
# test_m1_window_points_contract.py -- likewise re-declare their own small
# glue instead of reaching across the underscore boundary) ------------------


def _trace_lease() -> Lease:
    return Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=3,
        global_suspension_generation=1,
        target_suspension_generation=2,
    )


def _trace_input(run_id: str):
    face = otel_demo_face(FakeClock(start=WINDOW.end))
    return face.input_for(
        run_id=run_id,
        question="Why is checkout erroring?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )


def _trace_executor(monkeypatch, opener: FakeOpener) -> ReadOnlyToolExecutor:
    store = FakeStore(_trace_lease(), deadline=NOW + timedelta(minutes=10))
    lease = store.lease
    clock = FakeClock(start=NOW)
    sink = RecordingSink()

    def build_opener(*handlers):
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    config = OtelDemoConfig(
        prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL, token=None
    )
    factory = otel_demo_executor_factory(
        store, evidence=sink, clock=clock, config=config
    )
    executor = factory(lease, _trace_input(str(lease.run_id)))
    assert isinstance(executor, ReadOnlyToolExecutor)
    return executor


def _trace_call(params: dict) -> ToolRequest:
    return ToolRequest(
        step_id="step-1",
        tool_index=0,
        tool_name=TRACES_TOOL,
        target_ref=TARGET_ID,
        params=params,
        window=WINDOW.as_json(),
    )


def _traces_description() -> str:
    (schema,) = [s for s in TOOL_SCHEMAS if s["function"]["name"] == TRACES_TOOL]
    return schema["function"]["description"]


# -- fixture sanity (green on this tree; proves the doubles below, not the
# new behavior, are wired correctly -- every test past this point is red on
# this tree because the new fields/message do not exist yet, not because the
# executor/loop double setup is broken) --------------------------------------


def test_sanity_the_checkout_trace_fixture_has_both_tagged_and_untagged_rows(
    monkeypatch,
):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "checkout", "limit": 10}))
    assert outcome.status == "ok"
    rows = outcome.model_view["content"]
    assert any(r["status_tags"] for r in rows)
    assert any(not r["status_tags"] for r in rows)


def test_sanity_a_tool_round_then_final_round_reaches_the_model_twice():
    loop, request, model, transport, store, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            reply(content=json.dumps(_CLEAN_REPORT), finish="stop"),
        ],
        model_requests=2,
    )
    transport.response = TransportResponse(
        body=body([{"metric": "a", "value": 1}], partial=True)
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert len(model.calls) == 2
    assert model.calls[0].tools is not None  # a real tool round happened first
    assert model.calls[1].json_mode is True  # the second round was the final one
    (evidence_id,) = _own_evidence_ids(model.calls[1].messages)
    assert evidence_id  # a real evidence_id was delivered before the final round
    # Deliberately not asserting the exact shape of the final round's trailing
    # messages here: that is the behavior under test above and below, and
    # pinning it in this sanity fixture would make it red the moment that
    # behavior lands, defeating its purpose (it exists to show the double
    # setup itself -- tool round then final round, evidence delivered -- is
    # sound, independent of round 2).


# -- A: trace row status existence -------------------------------------------


def test_a_row_with_a_status_tag_is_recorded_and_status_tags_is_unchanged(
    monkeypatch,
):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "checkout", "limit": 10}))
    assert (outcome.status, outcome.reason) == ("ok", None), (
        outcome.model_view
    )  # sanity
    rows = outcome.model_view["content"]
    expected, _, _ = _projected(CHECKOUT_TRACES, service="checkout", limit=10)
    expected_rows = {
        (r["trace_id"], r["span_id"]): r["status_tags"]
        for r in expected["data"]["sampled_spans"]
    }
    tagged = [r for r in rows if r["status_tags"]]
    assert tagged, (
        "fixture sanity: at least one sampled row carries a status tag"
    )  # sanity
    for row in tagged:
        assert row["status_tags"] == expected_rows[(row["trace_id"], row["span_id"])]
        assert row["status_state"] == "recorded"


def test_a_row_without_any_status_tag_is_not_recorded(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "checkout", "limit": 10}))
    rows = outcome.model_view["content"]
    untagged = [r for r in rows if not r["status_tags"]]
    assert untagged, (
        "fixture sanity: at least one sampled row carries no status tag"
    )  # sanity
    for row in untagged:
        assert row["status_tags"] == {}
        assert row["status_state"] == "not_recorded"


def test_a_status_code_zero_is_recorded_not_not_recorded(monkeypatch):
    """A span that explicitly carries a listed status key valued 0/"OK" is a
    recorded observation, distinct from a span with no status key at all."""
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "checkout", "limit": 10}))
    rows = outcome.model_view["content"]
    zeroed = [
        r
        for r in rows
        if r["status_tags"] == {"rpc.grpc.status_code": 0}  # explicit, non-error code
    ]
    assert zeroed, (
        "fixture sanity: a row with an explicit zero status code exists"
    )  # sanity
    assert all(r["status_state"] == "recorded" for r in zeroed)


def test_a_traces_tool_description_explains_not_recorded_is_not_status_zero_or_ok():
    description = _traces_description().lower()
    assert "status_state" in description
    assert "not_recorded" in description
    assert "recorded" in description
    assert "unset" in description


# -- B: trace view counts and units ------------------------------------------


def test_b_complete_search_reports_backend_and_shown_counts(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "checkout", "limit": 10}))
    view = outcome.model_view
    # B1 (docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md) removed the
    # old 20-span sampling cap; CHECKOUT_TRACES' 90 spans stay well under the
    # view token cap (MAX_VIEW_TOKENS, 19k of 25k tokens), so none are dropped either.
    assert view["result_count"] == 90 and view["returned_count"] == 90
    assert (
        view["incomplete"] is False and view["query"]["limit"] == 10
    )  # sanity: unchanged
    record = json.loads(outcome.evidence.raw)
    assert view["traces_requested"] == 10
    assert (
        view["backend_traces_returned"] == record["backend_returned_trace_count"] == 2
    )
    assert view["backend_spans_returned"] == record["backend_returned_span_count"] == 90
    assert view["spans_shown"] == len(view["content"]) == view["returned_count"]
    assert view["spans_omitted"] == view["backend_spans_returned"] - view["spans_shown"]
    assert view["spans_omitted"] == 0
    assert view["incomplete_reason"] is None


def test_b_a_full_limit_search_reports_a_nonnull_incomplete_reason(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "checkout", "limit": 2}))
    view = outcome.model_view
    assert view["incomplete"] is True  # sanity: unchanged (2 traces >= limit 2)
    assert (
        isinstance(view["incomplete_reason"], str) and view["incomplete_reason"].strip()
    )
    assert view["traces_requested"] == 2


def _heavy_traces_body(span_count: int) -> bytes:
    """``span_count`` ~3.48 KB error spans, one per trace, all of the requested
    service (measured: 20 of them ~19.9k real DeepSeek tokens, 40 of them
    ~27.6k)."""
    start_us = int(WINDOW.start.timestamp() * 1_000_000) + 1_000_000

    def make_span(i: int) -> dict:
        return {
            "traceID": f"trace{i:04x}",
            "spanID": f"span{i:04x}",
            "operationName": "op-" + "x" * 140,
            "startTime": start_us + i,
            "duration": 1000 + i,
            "processID": "p1",
            "tags": [
                {"key": "error", "value": True},
                {"key": "otel.status_code", "value": "ERROR"},
            ],
            "logs": [
                {
                    "timestamp": start_us + i,
                    "fields": [
                        {"key": "exception.stacktrace", "value": "E" * 700},
                        {"key": "exception.message", "value": "M" * 700},
                        {"key": "otel.status_description", "value": "D" * 700},
                        {"key": "error.description", "value": "F" * 700},
                    ],
                }
            ],
            "references": [],
        }

    traces = [
        {
            "traceID": f"trace{i:04x}",
            "spans": [make_span(i)],
            "processes": {"p1": {"serviceName": "checkout", "tags": []}},
        }
        for i in range(span_count)
    ]
    return json.dumps({"data": traces}).encode()


def test_b_a_view_over_the_token_cap_is_refused_whole_not_counted_as_omitted(
    monkeypatch,
):
    """M1-01 A (v3) replaced the byte truncation this test pinned (40 heavy
    spans, 28 shown, ``omitted_rows == 12``, ``spans_omitted == 12``): rows are
    no longer dropped for size. The same 40 spans (~27.6k real tokens, over the
    25,000-token cap) are now refused as a whole with the true count, and no
    span is described as shown or omitted."""
    SPAN_COUNT = 40
    opener = FakeOpener(routes={"/api/traces": _heavy_traces_body(SPAN_COUNT)})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(
        _trace_call({"service": "checkout", "limit": SPAN_COUNT})
    )
    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert view["content"] is None and outcome.evidence is None
    assert view["max_view_tokens"] == 25_000 and view["view_tokens"] > 25_000
    for field in ("truncated", "omitted_rows", "spans_shown", "spans_omitted"):
        assert field not in view, field


def test_b_a_view_under_the_token_cap_accounts_for_every_backend_span(monkeypatch):
    """The counts contract that survives: 20 heavy spans (~19.9k tokens, under
    the cap) are all shown, and ``spans_omitted`` -- now only the adapter's own
    sampling gap -- is 0 and consistent with the backend count."""
    SPAN_COUNT = 20
    opener = FakeOpener(routes={"/api/traces": _heavy_traces_body(SPAN_COUNT)})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(
        _trace_call({"service": "checkout", "limit": SPAN_COUNT})
    )
    view = outcome.model_view
    assert outcome.status == "ok", view
    assert view["truncated"] is False and view["omitted_rows"] == 0
    record = json.loads(outcome.evidence.raw)
    assert record["omitted_span_count"] == 0
    assert view["backend_spans_returned"] == SPAN_COUNT
    assert view["spans_shown"] == len(view["content"]) == SPAN_COUNT
    assert view["spans_omitted"] == 0
    assert view["incomplete_reason"] is not None  # SPAN_COUNT traces >= limit


def test_b_traces_tool_description_documents_the_new_count_fields():
    description = _traces_description()
    for field in (
        "traces_requested",
        "backend_traces_returned",
        "backend_spans_returned",
        "spans_shown",
        "spans_omitted",
        "incomplete_reason",
    ):
        assert field in description, field


# -- C: run coverage summary before the final report -------------------------


def _own_evidence_ids(messages) -> list[str]:
    return [
        json.loads(m["content"])["evidence_id"]
        for m in messages
        if m.get("role") == "tool"
    ]


def _final_pair(messages):
    """The final round's ``(FINAL_REPORT_INSTRUCTION message, coverage message)``."""
    assert len(messages) >= 2
    return messages[-2], messages[-1]


def _count_none(text: str) -> int:
    return len(re.findall(r"\bnone\b", text.lower()))


def _mentions_total(text: str, expected: int) -> bool:
    return re.search(rf"\b{expected}\b", text) is not None


_CLEAN_REPORT = {
    "schema_version": "m0-report-v2",
    "assessment_status": "incomplete",
    "conclusion": "inconclusive",
    "summary": "No further evidence was required for this test.",
    "claims": [],
    "gaps": ["Not investigated further."],
    "next_steps": ["None."],
}


def test_c_final_round_message_immediately_follows_the_final_report_instruction():
    loop, request, model, transport, store, _ = assemble(
        replies=[reply(content=json.dumps(_CLEAN_REPORT), finish="stop")],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"  # sanity
    instruction_msg, coverage_msg = _final_pair(model.calls[0].messages)
    assert instruction_msg == {"role": "user", "content": FINAL_REPORT_INSTRUCTION}
    assert coverage_msg["role"] == "user"
    assert isinstance(coverage_msg["content"], str) and coverage_msg["content"].strip()


def test_c_a_run_with_no_delivered_views_lists_none_for_every_category():
    loop, request, model, transport, store, _ = assemble(
        replies=[reply(content=json.dumps(_CLEAN_REPORT), finish="stop")],
        model_requests=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"  # sanity
    _, coverage_msg = _final_pair(model.calls[0].messages)
    assert _count_none(coverage_msg["content"]) == 3
    assert _mentions_total(coverage_msg["content"], 0)


def test_c_an_incomplete_view_is_listed_and_the_other_categories_are_none():
    loop, request, model, transport, store, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            reply(content=json.dumps(_CLEAN_REPORT), finish="stop"),
        ],
        model_requests=2,
    )
    transport.response = TransportResponse(
        body=body([{"metric": "a", "value": 1}], partial=True)
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"  # sanity
    (evidence_id,) = _own_evidence_ids(model.calls[1].messages)
    _, coverage_msg = _final_pair(model.calls[1].messages)
    content = coverage_msg["content"]
    assert evidence_id in content
    assert _count_none(content) == 2  # truncated: none, non-ok status: none
    assert _mentions_total(content, 1)


def test_c_a_historical_v5_truncated_view_is_listed_and_the_other_categories_are_none():
    """M1-01 A (v3) contract change: the executor no longer truncates rows, so
    a Run can no longer *produce* a view with ``truncated: true`` (this test
    used to make one with 30 rows under the old default cap). The category is
    not dead, though: a view committed by a pre-upgrade (projection v5) Run may
    carry ``truncated: true``, and rebuilding the transcript from stored rows
    must still classify it. So the view is made historical by hand: run
    once, take the stored result, mark it as v5 wrote a truncated one, derive
    the ``DeliveredView`` the way the rebuild path does (``delivered_view``)
    and feed it through the single-source ``run_coverage_message``."""
    from opspilot.investigation.reports import run_coverage_message

    loop, request, model, transport, store, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            reply(content=json.dumps(_CLEAN_REPORT), finish="stop"),
        ],
        model_requests=2,
    )
    transport.response = TransportResponse(
        body=body([{"metric": f"m{i}", "value": i} for i in range(30)])
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"  # sanity
    (evidence_id,) = _own_evidence_ids(model.calls[1].messages)
    _, live_coverage = _final_pair(model.calls[1].messages)
    assert _count_none(live_coverage["content"]) == 3  # a clean view today

    (stored,) = [r["result"] for rows in store.tool_results.values() for r in rows]
    historical = {**stored, "truncated": True, "omitted_rows": 12}
    view = delivered_view(
        historical,
        evidence_context=request.evidence_context,
        authorized_targets=request.scope.target_ids,
    )
    assert view is not None and view.evidence_id == evidence_id
    content = run_coverage_message([view])
    assert evidence_id in content
    assert _count_none(content) == 2  # incomplete: none, non-ok status: none
    assert _mentions_total(content, 1)


def test_c_a_non_ok_status_view_is_listed_with_its_status_and_others_are_none():
    loop, request, model, transport, store, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            reply(content=json.dumps(_CLEAN_REPORT), finish="stop"),
        ],
        model_requests=2,
    )
    transport.response = TransportResponse(body=body([]))  # empty rows -> no_data
    outcome = loop.run(request)
    assert outcome.execution == "completed"  # sanity
    (evidence_id,) = _own_evidence_ids(model.calls[1].messages)
    _, coverage_msg = _final_pair(model.calls[1].messages)
    content = coverage_msg["content"]
    assert evidence_id in content
    assert "no_data" in content
    assert _count_none(content) == 2  # incomplete: none, truncated: none
    assert _mentions_total(content, 1)


def test_c_a_fully_clean_view_leaves_every_category_none():
    loop, request, model, transport, store, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=2,
    )
    transport.response = TransportResponse(body=body([{"metric": "a", "value": 1}]))
    outcome = loop.run(request)
    assert outcome.execution == "completed"  # sanity
    _own_evidence_ids(model.calls[1].messages)  # sanity: exactly one view collected
    _, coverage_msg = _final_pair(model.calls[1].messages)
    content = coverage_msg["content"]
    # A clean (ok, not incomplete, not truncated) view earns no place in any
    # of the three category lists -- all three must read "none" -- even
    # though it is the sole view this Run delivered.
    assert _count_none(content) == 3
    assert _mentions_total(content, 1)


def test_c_two_incomplete_views_are_listed_in_delivery_order():
    loop, request, model, transport, store, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call("c1")], finish="tool_calls"),
            # B5 (docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md):
            # distinct params -- an exact repeat of c1 would be deduped to
            # the same evidence_id, collapsing this test's two distinct
            # delivered views into one.
            reply(
                tool_calls=[tool_call("c2", arguments='{"expr":"up"}')],
                finish="tool_calls",
            ),
            reply(content=json.dumps(_CLEAN_REPORT), finish="stop"),
        ],
        model_requests=3,
    )
    transport.response = TransportResponse(
        body=body([{"metric": "a", "value": 1}], partial=True)
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"  # sanity
    ev1, ev2 = _own_evidence_ids(model.calls[2].messages)
    _, coverage_msg = _final_pair(model.calls[2].messages)
    content = coverage_msg["content"]
    assert ev1 in content and ev2 in content
    assert content.index(ev1) < content.index(ev2)


def test_c_an_inherited_view_binding_is_not_counted():
    """A view carried in on ``evidence_context.view_bindings`` (as a
    continuation Run's carried context would supply) is citable evidence,
    but it was not collected by this Run and must not enter its coverage
    summary or its total."""
    from tests.m1_tool_support import historical_window_context

    def _run(with_inherited: bool):
        loop, request, model, transport, store, _ = assemble(
            replies=[
                reply(tool_calls=[tool_call()], finish="tool_calls"),
                report_from_transcript,
            ],
            model_requests=2,
        )
        transport.response = TransportResponse(body=body([{"metric": "a", "value": 1}]))
        if with_inherited:
            context = historical_window_context(request.run_id)
            context["view_bindings"] = {
                "inherited-ev-1": {
                    "status": "ok",
                    "citable_as_fact": True,
                    "target_refs": ["checkout-prod"],
                    "time_scope_refs": ["policy-window-1"],
                }
            }
            request = replace(request, evidence_context=context)
        outcome = loop.run(request)
        assert outcome.execution == "completed"  # sanity
        (own_id,) = _own_evidence_ids(model.calls[1].messages)
        instruction_msg, coverage_msg = _final_pair(model.calls[1].messages)
        # The coverage message must actually exist (a distinct message right
        # after FINAL_REPORT_INSTRUCTION), or the two "own_id not counted
        # differently" checks below would pass vacuously against today's tree.
        assert instruction_msg == {"role": "user", "content": FINAL_REPORT_INSTRUCTION}
        return own_id, coverage_msg["content"]

    baseline_id, baseline_content = _run(with_inherited=False)
    inherited_id, inherited_content = _run(with_inherited=True)
    assert "inherited-ev-1" not in inherited_content
    # The two runs collected the same one view of their own; only the random
    # dispatch id differs, so normalizing it must make the messages equal --
    # the inherited binding must not change the total or any category.
    normalized_baseline = baseline_content.replace(baseline_id, "EVID")
    normalized_inherited = inherited_content.replace(inherited_id, "EVID")
    assert normalized_baseline == normalized_inherited


def test_c_the_final_round_message_is_byte_identical_live_and_rebuilt():
    """The final round is, by definition, the last available model-request
    slot (``remaining <= 1``); interrupting it before it commits would spend
    that last slot and leave a resume with none left (C3 §13, "in-flight
    slot stays occupied" -- see test_m1_investigation_context.py:143), so it
    cannot be exercised the way a mid-run tool round can. Instead: let the
    Run complete once (the live path), then rebuild the transcript from the
    committed rows the way a restarted worker would (the rebuild path) and
    feed its recovered ``delivered`` views through the same single-source
    ``run_coverage_message`` the live round used. Byte-identical reconstruction
    of the coverage message, plus the unchanged prefix in front of it, is
    exactly "live and rebuilt agree" for the message the final round sent.
    """
    from opspilot.investigation.reports import run_coverage_message

    loop, request, model, transport, store, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=2,
    )
    transport.response = TransportResponse(
        body=body([{"metric": "a", "value": 1}], partial=True)
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"  # sanity
    live_messages = model.calls[1].messages
    instruction_msg, coverage_msg = _final_pair(live_messages)
    assert instruction_msg == {"role": "user", "content": FINAL_REPORT_INSTRUCTION}

    transcript = rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
        input=request.as_input(),
    )
    # The final round's own reply (it made no tool calls) is the last message
    # `rebuild_transcript` appended; drop it to recover the exact prefix that
    # round's *request* -- not its response -- was built on.
    assert transcript.messages[-1]["role"] == "assistant"
    rebuilt_prefix = transcript.messages[:-1]
    rebuilt_coverage = run_coverage_message(transcript.delivered)
    rebuilt_messages = [
        *rebuilt_prefix,
        instruction_msg,
        {"role": "user", "content": rebuilt_coverage},
    ]
    assert rebuilt_messages == list(live_messages)


# -- C: prompt_revision moves with the new template --------------------------

# Captured by importing ``opspilot.investigation.loop.prompt_revision_versions``
# at this branch's pre-round-2 HEAD (8b326fd); not a guess at the post-fix
# value (which this file must not hardcode).
_PRE_ROUND2_PROMPT_REVISION = "prompt-replay-candidate-04a9d1a1a80e"


def test_c_prompt_revision_changes_once_the_new_template_lands():
    current = prompt_revision_versions()["prompt_revision"]
    assert current != _PRE_ROUND2_PROMPT_REVISION
