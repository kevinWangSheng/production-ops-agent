"""Contract tests for the M1-01 ``series_note`` view field.

These tests drive the public OTel Demo executor with the same recorded
transport fixture used by the neighbouring OTel Demo contract tests.  They
are written from docs/tasks/2026-10-01-m1-01-series-note.md and deliberately
do not depend on the implementation's private projection helpers.
"""

from __future__ import annotations

import json

import pytest

from opspilot.tools import PROJECTION_REVISION
from opspilot.tools.otel_demo import METRICS_TOOL, TOOL_SCHEMA_REVISION, TOOL_SCHEMAS
from opspilot.tools.registry import canonical_hash
from tests.test_m1_otel_demo_contract import (
    BAD_EXPR,
    CHECKOUT_BODY,
    GOOD_EXPR,
    FakeOpener,
    _call,
    _executor,
)

SERIES_NOTE = (
    "counts spans of every operation of the service (internal, client and "
    "server spans alike), summed over the labels kept in this query; it is "
    "not a count of requests. status_code STATUS_CODE_UNSET means the span "
    "carried no status; it does not mean the call succeeded."
)


def test_contract_1_calls_total_ok_view_has_the_exact_series_note(monkeypatch):
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    executor, _, _, _ = _executor(monkeypatch, opener)

    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))

    assert (outcome.status, outcome.reason) == ("ok", None)
    assert outcome.model_view["series_note"] == SERIES_NOTE
    assert outcome.evidence is not None
    assert outcome.evidence.view["series_note"] == SERIES_NOTE


@pytest.mark.parametrize(
    ("expr", "route", "expected_status"),
    [
        ("sum(rate(http_requests_total[5m]))", CHECKOUT_BODY, "ok"),
        (BAD_EXPR, CHECKOUT_BODY, "error"),
        (GOOD_EXPR, 503, "error"),
    ],
    ids=["other-metric", "refused-view", "error-view"],
)
def test_contract_2_series_note_is_absent_outside_calls_total_ok_views(
    monkeypatch, expr, route, expected_status
):
    opener = FakeOpener(by_query={expr: route})
    if expected_status == "error" and route == 503:
        opener = FakeOpener(routes={"/api/v1/query_range": route})
    executor, _, _, _ = _executor(monkeypatch, opener)

    outcome = executor.execute(_call(METRICS_TOOL, {"expr": expr}))

    assert outcome.status == expected_status
    assert "series_note" not in outcome.model_view
    if outcome.evidence is not None:
        assert "series_note" not in outcome.evidence.view


def test_contract_3_series_note_is_hashed_without_changing_citation_semantics(
    monkeypatch,
):
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    executor, _, _, _ = _executor(monkeypatch, opener)

    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))

    assert outcome.status == "ok" and outcome.adopted
    assert outcome.model_view["citable_as_fact"] is True
    assert outcome.evidence is not None
    view = outcome.evidence.view
    assert view["series_note"] == SERIES_NOTE
    assert outcome.evidence.view_sha256 == canonical_hash(view)
    without_note = dict(view)
    without_note.pop("series_note")
    assert outcome.evidence.view_sha256 != canonical_hash(without_note)


def test_contract_4_series_note_semantics_advance_an_existing_revision():
    """The view contract must be versioned under an established revision path."""
    assert (
        PROJECTION_REVISION != "m1-01-tool-view-v6"
        or TOOL_SCHEMA_REVISION != "otel-demo-34bc78980747"
    )


def test_contract_5_series_note_is_not_added_to_the_tool_prompt():
    schema_text = json.dumps(TOOL_SCHEMAS, ensure_ascii=False)
    assert "series_note" not in schema_text
    assert SERIES_NOTE not in schema_text
