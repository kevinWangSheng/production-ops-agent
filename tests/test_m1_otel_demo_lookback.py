"""OTel Demo metrics lookback: how far back each range-selector point reads.

``promql_problem`` already lets a range selector up to the window length
through; originally a selector evaluated at the window start read that far
back before it (the window-points contract has since moved the first
evaluation forward by the selector length, so ``lookback_start_at`` is now
the window start). The v4 packet's model-visible description said the opposite ("a
query that reads outside it returns an error"), so the model could not tell
whether ``rate(x[5m])`` at the window start was in or out of scope, and the
view did not record how far back a sample's rate actually reached.

The decision: keep the refusal rules exactly as they are, expose the
lookback as a pure function and a transport/view field, and tell the model
the truth in the tool description. The revision literal recorded in the
packet (``otel-demo-3936d7ae7edb``, ``run.md``) must move with the text.

RED on the pre-fix tree except the ``promql_problem`` regression, which
pins that the refusal rules did not loosen.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from opspilot.tools import TransportRequest, TransportResponse
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    SOURCE,
    TARGET_ID,
    TOOL_SCHEMA_REVISION,
    TOOL_SCHEMAS,
    OtelDemoTransport,
    promql_problem,
)
from tests.m1_tool_support import WINDOW_START, body, build, request
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_BODY,
    EMPTY_BODY,
    GOOD_EXPR,
    JAEGER_URL,
    PROMETHEUS_URL,
    WINDOW,
    FakeOpener,
)

# Pinned from docs/evidence/m1-01-v4-acceptance/run.md (worker.log first line)
# and every ledger.json ``runs[0].versions.tool_schema_revision``.
PRE_FIX_TOOL_SCHEMA_REVISION = "otel-demo-3936d7ae7edb"


def _lookback(expr: str) -> int:
    # Imported inside the test so the rest of this module still collects on
    # the pre-fix tree, where the symbol does not exist yet.
    from opspilot.tools.otel_demo import promql_lookback_seconds

    return promql_lookback_seconds(expr)


def _metrics_request(params: dict, **over) -> TransportRequest:
    fields = {
        "operation_id": "step-1-t0",
        "source": SOURCE,
        "verb": "query",
        "endpoint": PROMETHEUS_URL,
        "selector": {
            "opspilot.integration.id": TARGET_ID,
            "traces_endpoint": JAEGER_URL,
        },
        "params": params,
        "window": WINDOW,
        "timeout_seconds": 30.0,
        "max_result_bytes": 1_048_576,
        "credential_ref": CREDENTIAL_REF,
        "tool": METRICS_TOOL,
    }
    fields.update(over)
    return TransportRequest(**fields)


def _transport(opener: FakeOpener) -> OtelDemoTransport:
    return OtelDemoTransport(credentials={CREDENTIAL_REF: None}, opener=opener)


# -- promql_lookback_seconds ------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "seconds"),
    [
        ("rate(x[5m])", 300),
        ("rate(x[1m]) + increase(y[2m])", 120),
        ("x[5m:1m]", 300),
        ("x[1h]", 3600),
        ("rate(x[90s])", 90),
        ("sum by(service_name) (increase(traces_span_metrics_calls_total[5m]))", 300),
        ('app_payment_transactions_total{service_name="payment"}', 0),
        ("", 0),
    ],
)
def test_promql_lookback_seconds_is_the_largest_range_selector(expr, seconds):
    assert _lookback(expr) == seconds
    assert type(_lookback(expr)) is int


# -- promql_problem regression: nothing loosened ----------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "rate(m[6m])",
        "sum(rate(traces_span_metrics_calls_total[5m] offset 1h))",
        "rate(m[1m] OFFSET 30s)",
        "m @ 1790431090",
        "m[5m:6m]",
    ],
)
def test_promql_problem_still_refuses_longer_selectors_offset_and_at(expr):
    """Exposing the lookback does not admit reads outside the window."""
    assert promql_problem(expr, 300) == "QUERY_OUT_OF_WINDOW"


def test_promql_problem_still_admits_a_selector_of_the_window_length():
    assert promql_problem("rate(m[5m])", 300) is None


# -- TransportResponse / OtelDemoTransport ---------------------------------


def test_transport_response_lookback_defaults_to_none():
    response = TransportResponse(body=b"{}")
    assert response.lookback_seconds is None


def test_metrics_transport_reports_the_query_lookback_on_200():
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})

    response = _transport(opener).fetch(_metrics_request({"expr": GOOD_EXPR}))

    assert response.source_status is None
    assert response.lookback_seconds == 300
    # source_start_at keeps its meaning: the first returned sample, not
    # window start minus lookback.
    assert response.source_start_at is not None
    assert response.source_start_at >= WINDOW.start


def test_metrics_transport_reports_zero_lookback_for_an_instant_selector():
    expr = 'app_payment_transactions_total{service_name="payment"}'
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    response = _transport(opener).fetch(_metrics_request({"expr": expr}))

    assert response.source_status is None
    assert response.lookback_seconds == 0


def test_metrics_transport_reports_no_lookback_on_a_source_error():
    opener = FakeOpener(routes={"/api/v1/query_range": 400})

    response = _transport(opener).fetch(_metrics_request({"expr": GOOD_EXPR}))

    assert response.source_status == "400"
    assert response.lookback_seconds is None


def test_metrics_transport_refusal_carries_no_lookback():
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    expr = "rate(m[6m])"

    response = _transport(opener).fetch(_metrics_request({"expr": expr}))

    assert response.source_status == "QUERY_OUT_OF_WINDOW"
    assert response.sent is False
    assert response.lookback_seconds is None
    assert not opener.called


# -- executor view -----------------------------------------------------------


def test_view_carries_lookback_seconds_and_lookback_start_at():
    """``lookback_start_at`` is the earliest instant any returned point reads,
    i.e. the requested window start (the adapter starts evaluating
    ``lookback_seconds`` after it), as an ISO-8601 string; ``source_start_at``
    stays the first returned sample (window-points contract, rule 3)."""
    executor, transport, _, _ = build()
    first_sample = WINDOW_START + timedelta(minutes=2)
    transport.response = TransportResponse(
        body=body([{"metric": "x", "value": 1}]),
        data_as_of=first_sample,
        source_start_at=first_sample,
        source_end_at=first_sample,
        lookback_seconds=300,
    )

    outcome = executor.execute(request())

    assert outcome.status == "ok"
    view = outcome.model_view
    assert view["lookback_seconds"] == 300
    assert isinstance(view["lookback_start_at"], str)
    assert datetime.fromisoformat(view["lookback_start_at"]) == WINDOW_START
    assert datetime.fromisoformat(view["source_start_at"]) == first_sample
    assert outcome.evidence.view["lookback_seconds"] == 300
    assert outcome.evidence.view["lookback_start_at"] == view["lookback_start_at"]


def test_view_lookback_is_none_when_the_transport_reports_none():
    """The fixture transport does not know the lookback; the view must say
    so rather than compute one from the query text."""
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([{"metric": "x", "value": 1}]))

    view = executor.execute(request()).model_view

    assert "lookback_seconds" in view and view["lookback_seconds"] is None
    assert "lookback_start_at" in view and view["lookback_start_at"] is None


# -- model-visible description and revision ---------------------------------


def _metrics_description() -> str:
    (metrics,) = [s for s in TOOL_SCHEMAS if s["function"]["name"] == METRICS_TOOL]
    return metrics["function"]["description"]


def test_metrics_description_no_longer_claims_every_outside_read_errors():
    assert "a query that reads outside it returns an error" not in (
        _metrics_description()
    )


def test_metrics_description_states_the_lookback_and_the_refusals():
    text = _metrics_description().lower()
    assert "window length" in text
    assert "before" in text
    assert "offset" in text
    assert "refused" in text
    assert "@" in text


def test_tool_schema_revision_moved_with_the_description():
    """C3 §5: the revision is a content hash of the model-visible face; the
    packet's Runs were recorded under the old literal and must not be
    reclaimed under the new one."""
    assert TOOL_SCHEMA_REVISION != PRE_FIX_TOOL_SCHEMA_REVISION


# -- end to end through the OTel executor path -------------------------------


def test_window_length_selector_view_is_adopted_and_citable_end_to_end(monkeypatch):
    """A ``[5m]`` selector over the 300 s window is evaluated once at the
    window end and reads back exactly to the window start; the view says so,
    stays adopted and citable, and a fact citing it binds to the Run's own
    historical_window policy."""
    from opspilot.investigation.context import delivered_view
    from opspilot.investigation.reports import parse_report, unsupported_citations
    from opspilot.tools.otel_demo import _evidence_context
    from tests.test_m1_otel_demo_contract import _call, _executor

    expr = "rate(traces_span_metrics_calls_total[5m])"
    opener = FakeOpener(by_query={expr: CHECKOUT_BODY})
    executor, _, lease, _ = _executor(monkeypatch, opener)

    outcome = executor.execute(_call(METRICS_TOOL, {"expr": expr}))

    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    view = outcome.model_view
    assert view["adopted"] is True
    assert view["citable_as_fact"] is True
    assert view["lookback_seconds"] == 300
    assert view["lookback_start_at"] == WINDOW.start.isoformat()
    first_sample = datetime.fromisoformat(view["source_start_at"])
    assert WINDOW.start <= first_sample <= WINDOW.end
    assert first_sample == outcome.evidence.source_start_at
    assert opener.called

    authorized = frozenset({TARGET_ID})
    context = _evidence_context(str(lease.run_id), WINDOW)
    delivered = delivered_view(
        view, evidence_context=context, authorized_targets=authorized
    )
    assert delivered is not None
    assert delivered.status == "ok"
    assert "policy-window-1" in delivered.time_scope_refs

    report, reason = parse_report(
        json.dumps(
            {
                "schema_version": "m0-report-v2",
                "assessment_status": "completed",
                "conclusion": "supported",
                "summary": "One series was returned inside the window.",
                "claims": [
                    {
                        "kind": "fact",
                        "text": "The call-rate series was returned for checkout.",
                        "evidence_ids": [view["evidence_id"]],
                        "target_refs": [TARGET_ID],
                        "time_scope_ref": "policy-window-1",
                    }
                ],
                "gaps": [],
                "next_steps": [],
            }
        ),
        finish_reason="stop",
    )
    assert report is not None, reason
    assert (
        unsupported_citations(
            report,
            views=[delivered],
            authorized_targets=authorized,
            time_policy_ids=("policy-window-1",),
            target_catalog=None,
        )
        is False
    )


def test_selector_longer_than_the_window_is_refused_before_any_request(monkeypatch):
    """Mirrors the ``offset`` refusal: ``[6m]`` over a 300 s window never
    reaches the source and leaves no evidence."""
    from tests.test_m1_otel_demo_contract import _call, _executor

    opener = FakeOpener(routes={"/api/v1/query_range": CHECKOUT_BODY})
    executor, sink, _, _ = _executor(monkeypatch, opener)

    outcome = executor.execute(_call(METRICS_TOOL, {"expr": "rate(x[6m])"}))

    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert not opener.called
    assert sink.records == [] and outcome.evidence is None
    assert outcome.model_view["content"] is None
