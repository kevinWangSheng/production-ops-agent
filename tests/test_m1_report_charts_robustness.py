"""Robustness regressions for report charts (independent review P2-1).

Implementer-written; the contract tests live in
``test_m1_report_charts_contract.py`` and are not touched.
"""

from __future__ import annotations

import pytest

from opspilot.web.charts import render_evidence_chart
from tests.test_m1_report_charts_contract import (
    evidence,
    parse,
    published_page,
    row,
)


@pytest.mark.parametrize("magnitude", ["-1e20", "-1e17", "-1e16", "1e20", "0"])
def test_constant_series_of_any_magnitude_renders_a_figure(magnitude):
    ev = evidence(rows=[row({"job": "a"}, [(0, magnitude), (60, magnitude)])])
    root = parse(render_evidence_chart(ev, ["facts"]))
    (svg,) = root.find_all("svg")
    assert float(svg.attrs["data-y-min"]) < float(svg.attrs["data-y-max"])
    assert len(root.find_all("g", cls="series")) == 1


def test_overflowing_span_is_unavailable_not_an_error():
    ev = evidence(
        rows=[row({"job": "a"}, [(0, "-1.5e308"), (60, "1.5e308")])],
    )
    root = parse(render_evidence_chart(ev, ["facts"]))
    (node,) = root.find_all("p", cls="chart-unavailable")
    assert node.attrs["data-reason"] == "unplottable_range"
    assert root.find_all("figure") == []


def test_non_finite_step_means_no_break_judgement():
    ev = evidence(
        rows=[row({"job": "a"}, [(0, 1.0), (60, 2.0), (1500, 3.0)])],
        step=float("inf"),
    )
    root = parse(render_evidence_chart(ev, ["facts"]))
    assert len(root.find_all("polyline")) == 1


def test_hostile_omitted_rows_is_escaped():
    ev = evidence(truncated=True, omitted_rows="<script>x</script>")
    html = render_evidence_chart(ev, ["facts"])
    assert "<script" not in html.lower()


def test_page_survives_an_unplottable_chart_and_a_crashing_one(monkeypatch):
    page = published_page(
        [("fact", ["ev-a", "ev-b"])],
        [
            evidence(
                "ev-a", rows=[row({"j": "a"}, [(0, "-1.5e308"), (60, "1.5e308")])]
            ),
            evidence("ev-b", rows=[row({"j": "b"}, [(0, 1.0), (60, 2.0)])]),
        ],
    )
    assert page.status == 200
    assert [n.attrs["data-reason"] for n in page.placeholders()] == [
        "unplottable_range"
    ]
    assert [f.attrs["data-evidence-id"] for f in page.figures()] == ["ev-b"]

    import opspilot.web.service as service

    real = service.evidence_chart

    def flaky(record, kinds):
        if record.evidence_id == "ev-a":
            raise RuntimeError("boom")
        return real(record, kinds)

    monkeypatch.setattr(service, "evidence_chart", flaky)
    from tests.test_m1_report_charts_contract import fetch

    again = fetch(page.app, page.workbench, page.incident, page.store)
    assert again.status == 200
    assert [f.attrs["data-evidence-id"] for f in again.figures()] == ["ev-b"]


@pytest.mark.parametrize("ts", [10**400, float("inf"), float("nan")])
def test_out_of_range_timestamp_is_unrecognized_shape(ts):
    ev = evidence(
        content=[{"metric": {"job": "a"}, "values": [[ts, "1"]]}],
    )
    root = parse(render_evidence_chart(ev, ["facts"]))
    (node,) = root.find_all("p", cls="chart-unavailable")
    assert node.attrs["data-reason"] == "unrecognized_shape"
