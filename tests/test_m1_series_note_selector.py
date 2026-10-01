"""Regression tests for identifying the calls-total metric selector."""

from __future__ import annotations

import pytest

from opspilot.tools.otel_demo import METRICS_TOOL, SERIES_NOTE
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_BODY,
    FakeOpener,
    _call,
    _executor,
)


@pytest.mark.parametrize(
    "expr",
    [
        'rate(foo{source="traces_span_metrics_calls_total"}[5m])',
        'rate(foo{source=~"traces_span_metrics_calls_total|other"}[5m])',
        "rate(traces_span_metrics_calls_total_extra[5m])",
    ],
)
def test_series_note_ignores_quoted_values_and_longer_metric_names(monkeypatch, expr):
    opener = FakeOpener(by_query={expr: CHECKOUT_BODY})
    executor, _, _, _ = _executor(monkeypatch, opener)

    outcome = executor.execute(_call(METRICS_TOOL, {"expr": expr}))

    assert outcome.status == "ok"
    assert "series_note" not in outcome.model_view


@pytest.mark.parametrize(
    "expr",
    [
        "rate(traces_span_metrics_calls_total[5m])",
        'sum by(service_name) (traces_span_metrics_calls_total{service_name="checkout"})',
    ],
)
def test_series_note_matches_real_metric_selectors(monkeypatch, expr):
    opener = FakeOpener(by_query={expr: CHECKOUT_BODY})
    executor, _, _, _ = _executor(monkeypatch, opener)

    outcome = executor.execute(_call(METRICS_TOOL, {"expr": expr}))

    assert outcome.status == "ok"
    assert outcome.model_view["series_note"] == SERIES_NOTE
