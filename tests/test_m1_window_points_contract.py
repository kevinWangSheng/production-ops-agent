"""Contract tests for M1-01 "metrics views return only in-window points".

Written from the behavior contract in
``docs/tasks/2026-09-27-m1-01-window-points.md`` (rules 1-5 and 7) and the
public interface of ``opspilot.tools.otel_demo`` / ``opspilot.tools`` --
deliberately not from the implementation diff. Hermetic: no network, no
PostgreSQL; a fake ``urllib`` opener stands in for Prometheus.

The bug this closes: ``metrics_range_query`` sent Prometheus a ``start``
pinned to the authorized window start regardless of the query's range
selector, so ``rate()``/``increase()`` etc. at the first few returned points
read samples from *before* the window and the model reported them as
in-window values. The fix shifts the request's ``start`` forward by the
selector's own lookback so every returned point's range is entirely inside
``[window_start, window_end]``.

RED on the pre-fix tree for every test below except the unchanged-refusal
group (rule 5), which pins that the refusal rules did not loosen.
"""

from __future__ import annotations

import urllib.request
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

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
)
from tests.m1_tool_support import WINDOW_START as GENERIC_WINDOW_START
from tests.m1_tool_support import body, build, request
from tests.test_m1_otel_demo_contract import EMPTY_BODY, JAEGER_URL, PROMETHEUS_URL, WINDOW, FakeOpener

WINDOW_SECONDS = int(WINDOW.seconds)  # 300, from tests/fixtures/otel_demo/window.txt


def _url(request) -> str:
    return request.full_url if isinstance(request, urllib.request.Request) else request


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


def _sent_query(opener: FakeOpener) -> dict:
    (sent,) = opener.requests
    return parse_qs(urlsplit(_url(sent)).query)


# -- rule 1 & 2: query_range start/end follow the selector's lookback -------


def test_start_is_window_start_plus_lookback_for_a_partial_window_selector():
    """``increase(x[60s])`` over the 300 s window: every returned point's
    60 s range must stay inside the window, so the request starts 60 s
    after the window start, not at it."""
    expr = "increase(x[60s])"
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    _transport(opener).fetch(_metrics_request({"expr": expr}))

    query = _sent_query(opener)
    assert float(query["start"][0]) == (WINDOW.start + timedelta(seconds=60)).timestamp()
    assert float(query["end"][0]) == WINDOW.end.timestamp()


def test_start_equals_end_when_the_selector_spans_the_whole_window():
    """A selector exactly as long as the window is evaluated once, at the
    window end, to produce a single whole-window aggregate."""
    expr = f"increase(x[{WINDOW_SECONDS}s])"
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    _transport(opener).fetch(_metrics_request({"expr": expr}))

    query = _sent_query(opener)
    assert float(query["start"][0]) == WINDOW.end.timestamp()
    assert float(query["end"][0]) == WINDOW.end.timestamp()


def test_start_is_window_start_for_an_instant_selector_with_no_range():
    """No range or subquery selector at all (L = 0): the request still
    starts at the window start, unchanged."""
    expr = 'app_payment_transactions_total{service_name="payment"}'
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    _transport(opener).fetch(_metrics_request({"expr": expr}))

    query = _sent_query(opener)
    assert float(query["start"][0]) == WINDOW.start.timestamp()
    assert float(query["end"][0]) == WINDOW.end.timestamp()


def test_start_uses_the_longer_of_a_plain_range_and_a_subquery_selector():
    """L is the *longest* range-or-subquery selector in the expression: the
    120 s subquery here outranges the 30 s plain range selector, so the
    request starts 120 s after the window start, not 30 s."""
    expr = "rate(x[30s]) + y[120s:15s]"
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    _transport(opener).fetch(_metrics_request({"expr": expr}))

    query = _sent_query(opener)
    assert float(query["start"][0]) == (WINDOW.start + timedelta(seconds=120)).timestamp()
    assert float(query["end"][0]) == WINDOW.end.timestamp()


# -- rule 1 (2026-09-27 independent-review addendum): a subquery's L adds
# its own outer range to its inner expression's L, recursively, rather than
# taking the single longest bracket in the expression. ----------------------


def test_subquery_lookback_adds_the_outer_range_to_the_inner_selector():
    """Contract example 1: ``max_over_time(rate(x[1m])[4m:30s])`` -- the
    outer subquery's 240 s range plus the inner ``rate(x[1m])``'s 60 s range
    is 300 s, exactly the window length, so this is evaluated once at the
    window end. A flat "longest single bracket" reading would wrongly stop
    at 240 s (the ``4m`` before the colon) and never reach 300."""
    expr = "max_over_time(rate(x[1m])[4m:30s])"
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    _transport(opener).fetch(_metrics_request({"expr": expr}))

    query = _sent_query(opener)
    assert float(query["start"][0]) == WINDOW.end.timestamp()
    assert float(query["end"][0]) == WINDOW.end.timestamp()


def test_subquery_lookback_recursion_over_the_window_is_refused():
    """Contract example 2: the same shape with a 5 m outer range --
    ``max_over_time(rate(x[1m])[5m:30s])`` -- sums to 300 + 60 = 360 s,
    over the 300 s window, so it is refused before any request is sent."""
    expr = "max_over_time(rate(x[1m])[5m:30s])"
    opener = FakeOpener(routes={"/api/v1/query_range": EMPTY_BODY})

    response = _transport(opener).fetch(_metrics_request({"expr": expr}))

    assert response.source_status == "QUERY_OUT_OF_WINDOW"
    assert not opener.called


def test_subquery_lookback_recursion_sums_every_nesting_level():
    """Three levels deep, shaped after the canonical Prometheus nested-
    subquery example (``max_over_time(deriv(rate(x[5s])[20s:5s])[1m:30s])``):
    innermost ``rate(x[5s])`` is 5 s; the middle subquery ``[20s:5s]`` adds
    its own 20 s (25 s so far); ``deriv`` passes that through unchanged; the
    outer subquery ``[1m:30s]`` adds its own 60 s, for 85 s total. A reading
    that only summed two levels, or picked the single longest bracket (60 s),
    would get this wrong."""
    expr = "max_over_time(deriv(rate(x[5s])[20s:5s])[1m:30s])"
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    _transport(opener).fetch(_metrics_request({"expr": expr}))

    query = _sent_query(opener)
    assert float(query["start"][0]) == (WINDOW.start + timedelta(seconds=85)).timestamp()
    assert float(query["end"][0]) == WINDOW.end.timestamp()


def test_subquery_lookback_recursion_is_maxed_against_a_sibling_selector():
    """Contract rule 1's "multiple parallel selectors take the max" still
    applies once a sibling's own L is computed recursively: the 200 s plain
    selector here outranges the recursively-summed 60 s subquery
    (50 s outer + 10 s inner), so L is 200, not 60."""
    expr = "rate(x[200s]) + max_over_time(rate(y[10s])[50s:10s])"
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    _transport(opener).fetch(_metrics_request({"expr": expr}))

    query = _sent_query(opener)
    assert float(query["start"][0]) == (WINDOW.start + timedelta(seconds=200)).timestamp()
    assert float(query["end"][0]) == WINDOW.end.timestamp()


# -- rule 3: view fields --------------------------------------------------


def test_view_lookback_start_at_is_the_window_start_not_before_it():
    """``lookback_start_at`` is the earliest instant the query could have
    read a sample from -- now the window start itself, because the adapter's
    first evaluation point sits ``lookback_seconds`` *after* the window
    start, never before it."""
    executor, transport, _, _ = build()
    first_sample = GENERIC_WINDOW_START + timedelta(seconds=300)
    transport.response = TransportResponse(
        body=body([{"metric": "x", "value": 1}]),
        data_as_of=first_sample,
        source_start_at=first_sample,
        source_end_at=first_sample,
        lookback_seconds=300,
    )

    outcome = executor.execute(request())
    view = outcome.model_view

    assert view["lookback_seconds"] == 300
    assert isinstance(view["lookback_start_at"], str)
    assert datetime.fromisoformat(view["lookback_start_at"]) == GENERIC_WINDOW_START
    # The first returned sample is at or after window_start + lookback_seconds.
    assert first_sample >= GENERIC_WINDOW_START + timedelta(seconds=300)
    assert outcome.evidence.view["lookback_start_at"] == view["lookback_start_at"]


def test_view_lookback_fields_stay_none_when_the_transport_does_not_report_them():
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([{"metric": "x", "value": 1}]))

    view = executor.execute(request()).model_view

    assert view["lookback_seconds"] is None
    assert view["lookback_start_at"] is None


# -- rule 4: model-visible description -------------------------------------


def _metrics_description() -> str:
    (metrics,) = [s for s in TOOL_SCHEMAS if s["function"]["name"] == METRICS_TOOL]
    return metrics["function"]["description"]


def test_description_drops_the_first_points_may_reflect_before_window_claim():
    text = _metrics_description().lower()
    # The exact sentence rule 4 names for deletion.
    assert "reflect samples from before the window" not in text
    assert "may reflect samples" not in text


def test_description_hints_whole_window_aggregates_use_one_promql_evaluation():
    """Rule 4: point the model at PromQL for a whole-window total/max/min
    (one evaluation), rather than summing/maxing the returned points
    itself. Wording is not pinned by the contract, so this checks for any
    of the phrasings that would satisfy it."""
    text = _metrics_description().lower()
    assert any(
        phrase in text
        for phrase in (
            "single query",
            "one query",
            "single evaluation",
            "one evaluation",
            "one promql",
        )
    ), text


def test_tool_schema_revision_is_hash_shaped():
    assert TOOL_SCHEMA_REVISION.startswith("otel-demo-")
    assert len(TOOL_SCHEMA_REVISION) == len("otel-demo-") + 12


def test_tool_schema_revision_differs_from_the_pre_task_baseline():
    """Rule 7: the description changed, so the content-hash revision must
    move. Pinned by importing ``opspilot.tools.otel_demo`` at commit
    4431456 (this task's starting point, PR #57 HEAD) in a clean worktree,
    *before* this task's fix landed -- not a guess at the new value."""
    baseline = "otel-demo-e8c29b2fbffd"
    assert TOOL_SCHEMA_REVISION != baseline


# -- rule 5: refusal rules are unchanged -----------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        f"rate(x[{WINDOW_SECONDS + 60}s])",  # range selector longer than the window
        "rate(x[1m] offset 30s)",
        "x @ 1790431090",
    ],
)
def test_refusal_rules_still_reject_oversized_range_offset_and_at(expr):
    opener = FakeOpener(routes={"/api/v1/query_range": EMPTY_BODY})

    response = _transport(opener).fetch(_metrics_request({"expr": expr}))

    assert response.source_status == "QUERY_OUT_OF_WINDOW"
    assert not opener.called


def test_refusal_rules_still_admit_a_selector_of_exactly_the_window_length():
    expr = f"rate(x[{WINDOW_SECONDS}s])"
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    response = _transport(opener).fetch(_metrics_request({"expr": expr}))

    assert response.source_status is None
    assert opener.called


@pytest.mark.parametrize("step", [14, 0, -1])
def test_step_bounds_refusal_is_unchanged(step):
    expr = 'app_payment_transactions_total{service_name="payment"}'
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    response = _transport(opener).fetch(
        _metrics_request({"expr": expr, "step_seconds": step})
    )

    assert response.source_status == "INVALID_STEP"
    assert not opener.called


def test_step_longer_than_the_window_is_still_refused():
    expr = 'app_payment_transactions_total{service_name="payment"}'
    opener = FakeOpener(by_query={expr: EMPTY_BODY})

    response = _transport(opener).fetch(
        _metrics_request({"expr": expr, "step_seconds": WINDOW_SECONDS + 1})
    )

    assert response.source_status == "INVALID_STEP"
    assert not opener.called
