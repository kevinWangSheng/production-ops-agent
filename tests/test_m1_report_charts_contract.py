"""Contract tests for report evidence charts (M1-01 follow-up 2).

Written from the "合同" section of docs/tasks/2026-09-29-m1-01-report-charts.md
and the public interfaces only: ``GET /incidents/{id}`` and the pure function
``opspilot.web.charts.render_evidence_chart``. Assertions parse the DOM and
its attributes; none snapshots an SVG string. ``opspilot.web.charts`` is
imported inside each test so a missing module fails case by case.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest

from opspilot.tools.registry import canonical_hash
from opspilot.web.evidence import StoredEvidence
from tests.m1_investigation_support import (
    reply,
    report_json,
    tool_call,
)
from tests.m1_web_support import (
    ScriptedInvestigator,
    basic,
    build_workbench,
    call,
    submit_incident,
)

START = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=30)
T0 = int(START.timestamp())
WINDOW_SECONDS = 1800
METRICS = "metrics_range_query"

CATEGORY_WORD = {
    "fact": "fact",
    "hypothesis": "hypothes",
    "counter_evidence": "counter",
    "rejected_hypothesis": "rejected",
    "recommendation": "recommend",
}


# -- charts module, imported per test -------------------------------------------


def render(evidence, cited_by=("facts",)):
    from opspilot.web.charts import render_evidence_chart

    return render_evidence_chart(evidence, list(cited_by))


# -- a small DOM ----------------------------------------------------------------

_VOID = {
    "br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed",
    "source", "track", "wbr", "polyline", "circle", "path", "line", "rect",
}  # fmt: skip


class Node:
    def __init__(self, tag, attrs, parent, order):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.order = order
        self.children = []

    def iter(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag=None, cls=None, **attrs):
        found = []
        for node in self.iter():
            if tag and node.tag != tag:
                continue
            if cls and cls not in node.attrs.get("class", "").split():
                continue
            if any(node.attrs.get(k.replace("_", "-")) != v for k, v in attrs.items()):
                continue
            found.append(node)
        return found

    def find(self, *args, **kwargs):
        found = self.find_all(*args, **kwargs)
        return found[0] if found else None

    def text(self):
        parts = []
        for child in self.children:
            parts.append(child.text() if isinstance(child, Node) else child)
        return "".join(parts)


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None, 0)
        self.stack = [self.root]
        self.count = 0

    def _new(self, tag, attrs):
        self.count += 1
        node = Node(
            tag,
            {k: (v if v is not None else "") for k, v in attrs},
            self.stack[-1],
            self.count,
        )
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._new(tag, attrs)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._new(tag, attrs)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(html):
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


# -- evidence builders ----------------------------------------------------------


def iso(offset_seconds):
    return (START + timedelta(seconds=offset_seconds)).isoformat()


def row(labels, points):
    """points: [(offset_seconds, value)]; value is sent as a string."""
    return {
        "metric": dict(labels),
        "values": [
            [T0 + off, val if isinstance(val, str) else repr(val)]
            for off, val in points
        ],
    }


_MISSING = object()


def evidence(
    evidence_id="run-1-t0:d1",
    *,
    rows=None,
    content=_MISSING,
    tool=METRICS,
    status="ok",
    adopted=True,
    step=60,
    expr="rate(http_errors[5m])",
    truncated=False,
    omitted_rows=0,
    verified=True,
    run_id="run-1",
    subject_id="incident-1",
):
    if content is _MISSING:
        content = (
            rows if rows is not None else [row({"job": "api"}, [(0, 1.0), (60, 2.0)])]
        )
    query = {"expr": expr}
    if step is not None:
        query["step_seconds"] = step
    view = {
        "evidence_id": evidence_id,
        "tool": tool,
        "status": status,
        "adopted": adopted,
        "query": query,
        "window": {"start": START.isoformat(), "end": END.isoformat()},
        "truncated": truncated,
        "omitted_rows": omitted_rows,
        "content": content,
    }
    raw = b'{"data":{"result":[{"metric":{"raw_only":"1"},"values":[[1,"999"]]}]}}'
    return StoredEvidence(
        evidence_id=evidence_id,
        run_id=run_id,
        subject_id=subject_id,
        status=status,
        adopted=adopted,
        raw=raw,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        view=view,
        view_sha256=canonical_hash(view) if verified else "0" * 64,
        projection_revision="m1-01-tool-view-v5",
        observed_at=END,
        data_as_of=END,
    )


class StubEvidenceStore:
    """Read-only store: any call other than ``get`` fails the test."""

    def __init__(self, records):
        self.records = dict(records)
        self.gets = []

    def get(self, evidence_id):
        self.gets.append(evidence_id)
        return self.records.get(evidence_id)


# -- reading a rendered chart ---------------------------------------------------


def figure_of(html):
    figures = parse(html).find_all("figure", cls="evidence-chart")
    assert len(figures) == 1, html
    return figures[0]


def svg_of(figure):
    svgs = figure.find_all("svg")
    assert len(svgs) == 1
    return svgs[0]


def series_of(figure):
    return svg_of(figure).find_all("g", cls="series")


def _floats(text):
    return [float(x) for x in re.split(r"[\s,]+", text.strip()) if x]


def segments(group):
    """Drawn segments of one series in document order: [[(x, y), ...], ...]."""
    out = []
    for shape in group.iter():
        if shape.tag == "polyline":
            nums = _floats(shape.attrs["points"])
            pairs = list(zip(nums[0::2], nums[1::2]))
            assert len(nums) % 2 == 0 and len(pairs) >= 2, (
                "a polyline is a run of >= 2 points"
            )
            out.append(pairs)
        elif shape.tag == "circle":
            out.append([(float(shape.attrs["cx"]), float(shape.attrs["cy"]))])
    return out


def seg_sizes(group):
    return [len(s) for s in segments(group)]


def drawn_points(group):
    return [p for seg in segments(group) for p in seg]


def caption(figure):
    found = figure.find("figcaption")
    assert found is not None
    return found.text()


def svg_numbers(svg):
    nums = []
    for node in svg.find_all("text"):
        nums += [
            float(x)
            for x in re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", node.text())
        ]
    return nums


def assert_affine(xs_ts, tolerance=0.05):
    """(t, x) pairs must lie on one straight line with a positive slope."""
    ts = sorted({t for t, _ in xs_ts})
    assert len(ts) >= 3
    (t1, x1), (t2, x2) = xs_ts[0], xs_ts[-1]
    assert t2 != t1
    slope = (x2 - x1) / (t2 - t1)
    for t, x in xs_ts:
        assert abs(x - (x1 + slope * (t - t1))) <= tolerance, (t, x)
    return slope


# -- fragment contract: render_evidence_chart -----------------------------------


def test_returns_a_figure_with_an_svg_role_img_and_title():
    html = render(evidence("ev-1"), ["facts"])
    assert isinstance(html, str)
    figure = figure_of(html)
    assert figure.attrs["data-evidence-id"] == "ev-1"
    svg = svg_of(figure)
    assert svg.attrs.get("role") == "img"
    title = svg.find("title")
    assert title is not None and title.text().strip()
    assert parse(html).find_all("p", cls="chart-unavailable") == []


def test_non_metrics_evidence_returns_none():
    assert render(evidence(tool="traces_search"), ["facts"]) is None
    assert render(evidence(tool="traces_search", status="no_data"), ["facts"]) is None
    assert render(evidence(tool="fixture_query", rows=[]), ["facts"]) is None


def test_render_is_pure_and_repeatable():
    ev = evidence(rows=[row({"job": "api"}, [(0, 1.0), (60, 2.0), (120, 3.0)])])
    before = copy.deepcopy(dict(ev.view))
    first = render(ev, ["facts", "hypotheses"])
    second = render(ev, ["facts", "hypotheses"])
    assert first == second
    assert dict(ev.view) == before


def test_series_group_carries_label_and_actual_point_count():
    ev = evidence(
        rows=[
            row({"job": "api", "code": "500"}, [(0, 1.0), (60, 2.0), (120, 3.0)]),
            row({}, [(0, 4.0), (60, 5.0)]),
        ]
    )
    groups = series_of(figure_of(render(ev)))
    assert len(groups) == 2
    labels = [g.attrs["data-series-label"] for g in groups]
    assert 'job="api"' in labels[0] and 'code="500"' in labels[0]
    assert labels[1] == "{}"
    assert [g.attrs["data-point-count"] for g in groups] == ["3", "2"]
    assert [len(drawn_points(g)) for g in groups] == [3, 2]


def test_contiguous_points_are_one_polyline():
    ev = evidence(
        rows=[row({"job": "a"}, [(0, 1.0), (60, 2.0), (120, 1.5), (180, 3.0)])]
    )
    (group,) = series_of(figure_of(render(ev)))
    assert seg_sizes(group) == [4]
    assert len(group.find_all("polyline")) == 1 and group.find_all("circle") == []


def test_gap_larger_than_one_and_a_half_steps_breaks_the_line():
    ev = evidence(
        rows=[
            row({"job": "a"}, [(0, 1.0), (60, 2.0), (120, 3.0), (300, 4.0), (360, 5.0)])
        ],
        step=60,
    )
    (group,) = series_of(figure_of(render(ev)))
    assert seg_sizes(group) == [3, 2]
    assert len(group.find_all("polyline")) == 2


def test_gap_of_exactly_one_and_a_half_steps_does_not_break():
    ev = evidence(rows=[row({"job": "a"}, [(0, 1.0), (90, 2.0), (180, 3.0)])], step=60)
    (group,) = series_of(figure_of(render(ev)))
    assert seg_sizes(group) == [3]


def test_gap_just_over_the_threshold_breaks_and_isolated_points_are_circles():
    ev = evidence(rows=[row({"job": "a"}, [(0, 1.0), (91, 2.0)])], step=60)
    (group,) = series_of(figure_of(render(ev)))
    assert seg_sizes(group) == [1, 1]
    assert group.find_all("polyline") == [] and len(group.find_all("circle")) == 2
    assert group.attrs["data-point-count"] == "2"


def test_isolated_point_between_runs_is_a_circle_not_dropped():
    ev = evidence(
        rows=[
            row({"job": "a"}, [(0, 1.0), (60, 2.0), (400, 9.0), (800, 3.0), (860, 4.0)])
        ],
        step=60,
    )
    (group,) = series_of(figure_of(render(ev)))
    assert seg_sizes(group) == [2, 1, 2]
    assert len(group.find_all("circle")) == 1


def test_no_step_means_no_break_judgement():
    ev = evidence(
        rows=[row({"job": "a"}, [(0, 1.0), (60, 2.0), (1500, 3.0)])], step=None
    )
    (group,) = series_of(figure_of(render(ev)))
    assert seg_sizes(group) == [3]


def test_single_point_series_is_visible_and_its_value_is_in_the_caption():
    ev = evidence(rows=[row({"span": "POST"}, [(300, 42.5)])], step=300)
    figure = figure_of(render(ev))
    (group,) = series_of(figure)
    assert group.attrs["data-point-count"] == "1"
    assert seg_sizes(group) == [1] and len(group.find_all("circle")) == 1
    assert "42.5" in caption(figure)
    svg = svg_of(figure)
    assert float(svg.attrs["data-y-min"]) < float(svg.attrs["data-y-max"])
    assert float(svg.attrs["data-y-min"]) <= 42.5 <= float(svg.attrs["data-y-max"])


def test_does_not_interpolate_or_zero_fill_across_a_missing_stretch():
    # 1 sample, a 12-step hole, then 2 samples: never a line across the hole
    # and never a value invented for the hole.
    ev = evidence(
        rows=[row({"job": "a"}, [(0, 10.0), (720, 30.0), (780, 40.0)])], step=60
    )
    figure = figure_of(render(ev))
    (group,) = series_of(figure)
    assert seg_sizes(group) == [1, 2]
    assert group.attrs["data-point-count"] == "3"
    svg = svg_of(figure)
    assert (
        float(svg.attrs["data-y-min"]) <= 10.0
        and float(svg.attrs["data-y-max"]) >= 40.0
    )


def test_positions_are_linear_in_time_and_value():
    points = [(0, 10.0), (60, 20.0), (300, 60.0), (900, 35.0), (1800, 5.0)]
    ev = evidence(rows=[row({"job": "a"}, points)], step=None)
    figure = figure_of(render(ev))
    (group,) = series_of(figure)
    drawn = drawn_points(group)
    assert len(drawn) == 5
    x_slope = assert_affine([(t, x) for (t, _), (x, _) in zip(points, drawn)])
    assert x_slope > 0
    y_slope = assert_affine([(v, y) for (_, v), (_, y) in zip(points, drawn)])
    assert y_slope < 0, "a larger value is drawn higher on the page"


def test_svg_declares_axis_extents_and_shows_them_as_text():
    ev = evidence(rows=[row({"job": "a"}, [(0, 5.0), (60, 25.0), (120, 15.0)])])
    figure = figure_of(render(ev))
    svg = svg_of(figure)
    for attr, expected in (("data-x-start", START), ("data-x-end", END)):
        value = svg.attrs[attr]
        as_time = None
        try:
            as_time = datetime.fromisoformat(value)
        except ValueError:
            pass
        assert (
            value == expected.isoformat()
            or as_time == expected
            or (
                re.fullmatch(r"\d+(\.\d+)?", value)
                and float(value) == expected.timestamp()
            )
        ), (attr, value)
    y_min, y_max = float(svg.attrs["data-y-min"]), float(svg.attrs["data-y-max"])
    assert y_min < y_max and y_min <= 5.0 and y_max >= 25.0
    numbers = svg_numbers(svg)
    assert any(math.isclose(n, y_min, rel_tol=1e-3, abs_tol=1e-6) for n in numbers)
    assert any(math.isclose(n, y_max, rel_tol=1e-3, abs_tol=1e-6) for n in numbers)
    axis_text = " ".join(t.text() for t in svg.find_all("text"))
    assert "10:00" in axis_text and "10:30" in axis_text


def test_constant_series_still_gets_a_non_degenerate_y_range():
    ev = evidence(rows=[row({"job": "a"}, [(0, 5.0), (60, 5.0), (120, 5.0)])])
    svg = svg_of(figure_of(render(ev)))
    assert float(svg.attrs["data-y-min"]) < float(svg.attrs["data-y-max"])
    assert float(svg.attrs["data-y-min"]) <= 5.0 <= float(svg.attrs["data-y-max"])


def test_y_range_covers_every_series_and_negative_values():
    ev = evidence(
        rows=[
            row({"job": "a"}, [(0, -30.0), (60, 2.0)]),
            row({"job": "b"}, [(0, 1.0), (60, 80.0)]),
        ]
    )
    svg = svg_of(figure_of(render(ev)))
    assert float(svg.attrs["data-y-min"]) <= -30.0
    assert float(svg.attrs["data-y-max"]) >= 80.0


def test_points_outside_the_window_are_not_drawn_and_are_reported():
    ev = evidence(
        rows=[
            row(
                {"job": "a"},
                [
                    (-120, 100.0),
                    (-60, 100.0),
                    (0, 1.0),
                    (60, 2.0),
                    (1800, 3.0),
                    (1860, 100.0),
                ],
            )
        ],
        step=60,
    )
    figure = figure_of(render(ev))
    (group,) = series_of(figure)
    # The window bounds themselves are inside the window.
    assert group.attrs["data-point-count"] == "3"
    assert len(drawn_points(group)) == 3
    svg = svg_of(figure)
    assert float(svg.attrs["data-y-max"]) < 100.0
    text = caption(figure).lower()
    assert "out of window" in text and re.search(r"\b3\b", text)


def test_no_out_of_window_note_when_every_point_is_inside():
    figure = figure_of(render(evidence()))
    assert "out of window" not in caption(figure).lower()


def test_nan_inf_and_non_numeric_points_are_skipped_and_counted():
    ev = evidence(
        rows=[
            row(
                {"job": "a"},
                [
                    (0, "1"),
                    (60, "NaN"),
                    (120, "+Inf"),
                    (180, "-Inf"),
                    (240, "Inf"),
                    (300, "abc"),
                    (360, "2"),
                    (420, "3"),
                ],
            )
        ],
        step=60,
    )
    figure = figure_of(render(ev))
    (group,) = series_of(figure)
    assert group.attrs["data-point-count"] == "3"
    assert len(drawn_points(group)) == 3
    text = caption(figure).lower()
    assert "skipped" in text and re.search(r"\b5\b", text)
    svg = svg_of(figure)
    assert (
        float(svg.attrs["data-y-max"]) < 100 and float(svg.attrs["data-y-min"]) > -100
    )


def test_a_skipped_point_is_not_bridged_by_a_line():
    ev = evidence(
        rows=[row({"job": "a"}, [(0, "1"), (60, "NaN"), (120, "3")])], step=60
    )
    (group,) = series_of(figure_of(render(ev)))
    assert seg_sizes(group) == [1, 1]


def test_no_skip_note_when_all_points_are_numeric():
    assert "skipped" not in caption(figure_of(render(evidence()))).lower()


def test_truncated_view_is_stated_with_omitted_rows():
    figure = figure_of(render(evidence(truncated=True, omitted_rows=7)))
    assert "series truncated, omitted_rows=7" in caption(figure)


def test_untruncated_view_has_no_truncation_note():
    assert "omitted_rows" not in caption(figure_of(render(evidence())))


def test_empty_series_is_absent_and_counted_when_others_are_drawn():
    ev = evidence(
        rows=[
            row({"job": "empty"}, []),
            row({"job": "all-nan"}, [(0, "NaN"), (60, "NaN")]),
            row({"job": "ok"}, [(0, 1.0), (60, 2.0)]),
        ]
    )
    figure = figure_of(render(ev))
    groups = series_of(figure)
    assert [g.attrs["data-series-label"] for g in groups] == ['job="ok"']
    assert "2 series without plottable points" in caption(figure)


def test_series_cap_is_ten_and_the_rest_is_reported_in_the_caption():
    rows = [row({"n": str(i)}, [(0, float(i)), (60, float(i) + 1)]) for i in range(12)]
    figure = figure_of(render(evidence(rows=rows)))
    groups = series_of(figure)
    assert len(groups) == 10
    assert [g.attrs["data-series-label"] for g in groups] == [
        f'n="{i}"' for i in range(10)
    ]
    text = caption(figure)
    assert re.search(r"\b2\b", text) and "series" in text.lower()


def test_ten_series_produce_no_overflow_note():
    rows = [row({"n": str(i)}, [(0, float(i)), (60, float(i) + 1)]) for i in range(10)]
    figure = figure_of(render(evidence(rows=rows)))
    assert len(series_of(figure)) == 10
    assert "not drawn" not in caption(figure).lower()


def test_caption_names_evidence_link_expr_window_step_and_citing_categories():
    ev = evidence(
        "run-1-t0:d9",
        expr='sum by (status) (rate(http_requests_total{job="checkout"}[5m]))',
        step=60,
        subject_id="incident-77",
    )
    figure = figure_of(render(ev, ["facts", "hypotheses"]))
    cap = figure.find("figcaption")
    links = cap.find_all("a")
    assert [a.attrs["href"] for a in links] == [
        "/incidents/incident-77/evidence/run-1-t0:d9"
    ]
    assert "run-1-t0:d9" in links[0].text()
    text = cap.text()
    assert 'sum by (status) (rate(http_requests_total{job="checkout"}[5m]))' in text
    assert "2026-09-20" in text and "10:00" in text and "10:30" in text
    assert "60" in text
    assert "facts" in text.lower() and "hypotheses" in text.lower()


def test_caption_lists_every_citing_category():
    figure = figure_of(render(evidence(), ["counter_evidence", "recommendations"]))
    text = caption(figure).lower()
    assert "counter" in text and "recommend" in text


# unavailable placeholders


def unavailable(html):
    nodes = parse(html).find_all("p", cls="chart-unavailable")
    assert len(nodes) == 1, html
    assert parse(html).find_all("figure") == []
    assert parse(html).find_all("svg") == []
    return nodes[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "no_data"},
        {"status": "error"},
        {"adopted": False},
    ],
)
def test_not_ok_or_not_adopted_is_unavailable_not_ok(kwargs):
    ev = evidence("ev-x", **kwargs)
    node = unavailable(render(ev))
    assert node.attrs["data-evidence-id"] == "ev-x"
    assert node.attrs["data-reason"] == "not_ok"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": []},
        {"content": None},
        {"rows": [row({"job": "a"}, [])]},
        {"rows": [row({"job": "a"}, [(0, "NaN"), (60, "+Inf")])]},
        {"rows": [row({"job": "a"}, [(-600, 1.0), (2400, 2.0)])]},
    ],
)
def test_nothing_plottable_is_unavailable_no_series(kwargs):
    node = unavailable(render(evidence("ev-y", **kwargs)))
    assert node.attrs["data-evidence-id"] == "ev-y"
    assert node.attrs["data-reason"] == "no_series"


@pytest.mark.parametrize(
    "content",
    [
        [{"metric": "checkout", "value": 3}],
        [{"metric": {"job": "a"}, "value": 3}],
        [{"metric": "checkout", "values": [[T0, "1"]]}],
        [{"metric": {"job": "a"}, "values": "not-a-list"}],
        [{"metric": {"job": "a"}, "values": [["x", "1"]]}],
        ["not-a-row"],
        {"metric": {"job": "a"}, "values": [[T0, "1"]]},
    ],
)
def test_content_of_the_wrong_shape_is_unrecognized_shape(content):
    node = unavailable(render(evidence("ev-z", content=content)))
    assert node.attrs["data-evidence-id"] == "ev-z"
    assert node.attrs["data-reason"] == "unrecognized_shape"


def test_hash_mismatch_is_unavailable_hash_mismatch_and_does_not_chart():
    ev = evidence("ev-h", verified=False)
    assert ev.hashes_verified is False
    node = unavailable(render(ev))
    assert node.attrs["data-reason"] == "hash_mismatch"


def test_data_comes_from_the_view_not_from_raw():
    html = render(evidence(rows=[row({"job": "a"}, [(0, 1.0), (60, 2.0)])]))
    assert "raw_only" not in html and "999" not in html


# fragment escaping


def test_hostile_labels_expr_and_ids_are_escaped_in_the_fragment():
    payload = '<script>alert(1)</script>"><img src=x onerror=alert(1)>'
    ev = evidence(
        'ev"><script>alert(2)</script>',
        rows=[
            row(
                {"job": payload, "url": "https://evil.example/x"}, [(0, 1.0), (60, 2.0)]
            ),
            row({"href": "javascript:alert(3)"}, [(0, 3.0), (60, 4.0)]),
        ],
        expr=f'up{{a="{payload}"}} # https://evil.example/e javascript:alert(4)',
    )
    html = render(ev, ["facts"])
    assert "<script" not in html.lower()
    root = parse(html)
    assert root.find_all("script") == []
    assert len(root.find_all("figure")) == 1
    assert figure_of(html).attrs["data-evidence-id"] == 'ev"><script>alert(2)</script>'
    forbidden = {
        "script",
        "iframe",
        "object",
        "embed",
        "image",
        "use",
        "foreignobject",
        "img",
    }
    for node in root.iter():
        assert node.tag not in forbidden, node.tag
        for name, value in node.attrs.items():
            assert not name.startswith("on"), (node.tag, name)
            if name in {"href", "xlink:href", "src", "action", "formaction"}:
                assert value.startswith("/incidents/"), (node.tag, name, value)
    assert "evil.example" not in " ".join(
        v
        for n in root.iter()
        for k, v in n.attrs.items()
        if k in {"href", "xlink:href", "src"}
    )


def test_series_label_is_truncated_to_eighty_characters():
    long_value = "x" * 300
    ev = evidence(rows=[row({"job": long_value}, [(0, 1.0), (60, 2.0)])])
    (group,) = series_of(figure_of(render(ev)))
    label = group.attrs["data-series-label"]
    assert 0 < len(label) <= 80 and "x" * 81 not in label


# -- page contract: GET /incidents/{id} -----------------------------------------


def _claims_json(claims):
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": "Checkout errors increased in the authorized window.",
        "claims": [
            {
                "kind": kind,
                "text": f"claim {i} about the observation",
                "evidence_ids": list(ids),
                "target_refs": ["checkout-prod"],
                "time_scope_ref": "policy-window-1",
            }
            for i, (kind, ids) in enumerate(claims)
        ],
        "gaps": ["No HealthProfile was supplied."],
        "next_steps": ["Have a human compare the sample against the service SLO."],
    }
    if not any(kind == "fact" and ids for kind, ids in claims):
        payload["claims"].append(
            {
                "kind": "fact",
                "text": "a fact citing nothing chartable",
                "evidence_ids": ["fixture-anchor"],
                "target_refs": ["checkout-prod"],
                "time_scope_ref": "policy-window-1",
            }
        )
    return json.dumps(payload)


class Page:
    def __init__(self, app, status, text, workbench, incident, store):
        self.app = app
        self.status = status
        self.text = text
        self.workbench = workbench
        self.incident = incident
        self.store = store
        self.root = parse(text)

    def charts_section(self):
        return self.root.find("section", id="evidence-charts")

    def figures(self):
        return self.root.find_all("figure", cls="evidence-chart")

    def placeholders(self):
        return self.root.find_all("p", cls="chart-unavailable")


def published_page(claims=(), records=(), *, report_text=None):
    """An incident with a published conclusion citing ``claims`` and a store
    holding ``records`` (StoredEvidence built by ``evidence``)."""
    app, workbench, _clock = build_workbench()
    incident = submit_incident(app, key="charts-1").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    run_id = next(iter(workbench.incidents.run_ids(subject)))
    fixed = []
    for rec in records:
        fixed.append(_rebind(rec, run_id, str(subject)))
    store = StubEvidenceStore({r.evidence_id: r for r in fixed})
    workbench.evidence = store
    text = report_text if report_text is not None else _claims_json(claims)
    workbench.incidents.incidents[subject]["conclusion"] = {
        "kind": "conclusion",
        "conclusion": {"report_content": text},
    }
    return fetch(app, workbench, incident, store)


def _rebind(rec, run_id, subject_id):
    from dataclasses import replace

    return replace(rec, run_id=run_id, subject_id=subject_id)


def fetch(app, workbench, incident, store):
    response = call(app, "GET", f"/incidents/{incident}", headers=basic())
    return Page(app, response.status, response.text, workbench, incident, store)


def order_of(page):
    return [f.attrs["data-evidence-id"] for f in page.figures()]


def ok_metrics(eid, **kwargs):
    rows = kwargs.pop("rows", None) or [
        row({"job": eid}, [(0, 1.0), (60, 2.0), (120, 3.0)])
    ]
    return evidence(eid, rows=rows, **kwargs)


def test_page_charts_each_cited_metrics_evidence_once_with_dom_contract():
    page = published_page(
        [("fact", ["ev-a"]), ("hypothesis", ["ev-a", "ev-b"])],
        [ok_metrics("ev-a"), ok_metrics("ev-b")],
    )
    assert page.status == 200
    section = page.charts_section()
    assert section is not None
    assert order_of(page) == ["ev-a", "ev-b"]
    for figure in page.figures():
        assert figure.find_all("svg")[0].attrs.get("role") == "img"
        assert figure.find("svg").find("title") is not None
        assert figure.find_all("g", cls="series")
    a = page.figures()[0]
    text = caption(a).lower()
    assert "fact" in text and "hypothes" in text
    b_text = caption(page.figures()[1]).lower()
    assert "hypothes" in b_text and "fact" not in b_text
    assert page.placeholders() == []


def test_page_section_comes_after_the_report_and_keeps_existing_ids_and_links():
    page = published_page([("fact", ["ev-a"])], [ok_metrics("ev-a")])
    section = page.charts_section()
    status = page.root.find("p", id="report-status")
    assert status is not None and section.order > status.order
    assert page.root.find(id="run-state") is not None or "run-state" in page.text
    link = f"/incidents/{page.incident}/evidence/ev-a"
    hrefs = [a.attrs.get("href") for a in page.root.find_all("a")]
    assert link in hrefs
    assert "Facts (1)" in page.text and "Unknown / gaps (1)" in page.text


def test_page_figure_link_targets_the_incident_evidence_route():
    page = published_page([("fact", ["ev-a"])], [ok_metrics("ev-a")])
    (figure,) = page.figures()
    hrefs = [a.attrs["href"] for a in figure.find_all("a")]
    assert f"/incidents/{page.incident}/evidence/ev-a" in hrefs


def test_page_order_follows_claim_categories_then_claim_order_then_id_order():
    claims = [
        ("recommendation", ["ev-rec"]),
        ("rejected_hypothesis", ["ev-rej"]),
        ("counter_evidence", ["ev-cnt"]),
        ("hypothesis", ["ev-hyp"]),
        ("fact", ["ev-f2", "ev-f1"]),
        ("fact", ["ev-f3"]),
    ]
    records = [
        ok_metrics(e)
        for e in ("ev-rec", "ev-rej", "ev-cnt", "ev-hyp", "ev-f1", "ev-f2", "ev-f3")
    ]
    page = published_page(claims, records)
    assert order_of(page) == [
        "ev-f2", "ev-f1", "ev-f3", "ev-hyp", "ev-cnt", "ev-rej", "ev-rec",
    ]  # fmt: skip
    words = {f.attrs["data-evidence-id"]: caption(f).lower() for f in page.figures()}
    assert "counter" in words["ev-cnt"] and "rejected" in words["ev-rej"]
    assert "recommend" in words["ev-rec"]


def test_page_does_not_chart_metrics_evidence_no_claim_cites():
    page = published_page(
        [("fact", ["ev-a"])], [ok_metrics("ev-a"), ok_metrics("ev-uncited")]
    )
    assert order_of(page) == ["ev-a"]
    assert "ev-uncited" not in [
        n.attrs.get("data-evidence-id") for n in page.placeholders()
    ]


def test_page_non_metrics_and_unknown_evidence_get_no_figure_and_no_placeholder():
    other_incident = evidence("ev-foreign", run_id="some-other-run")
    page = published_page(
        [("fact", ["ev-trace", "ev-a", "ev-missing", "ev-foreign"])],
        [
            ok_metrics("ev-a"),
            evidence("ev-trace", tool="traces_search", content=[{"x": 1}]),
        ],
    )
    # ev-foreign is registered under another Run: rebind puts it on this Run, so
    # replace it with a store entry the incident does not own.
    page.store.records["ev-foreign"] = other_incident
    page = fetch(*_again(page))
    assert page.status == 200
    assert order_of(page) == ["ev-a"]
    assert page.placeholders() == []
    hrefs = [a.attrs.get("href") for a in page.root.find_all("a")]
    for eid in ("ev-trace", "ev-missing", "ev-foreign"):
        assert f"/incidents/{page.incident}/evidence/{eid}" in hrefs


def _again(page):
    return page.app, page.workbench, page.incident, page.store


@pytest.mark.parametrize(
    "reason,record",
    [
        ("not_ok", lambda: ok_metrics("ev-u", status="no_data")),
        ("not_ok", lambda: ok_metrics("ev-u", adopted=False)),
        ("no_series", lambda: evidence("ev-u", content=[])),
        ("no_series", lambda: evidence("ev-u", content=None)),
        (
            "unrecognized_shape",
            lambda: evidence("ev-u", content=[{"metric": "checkout", "value": 3}]),
        ),
        ("hash_mismatch", lambda: ok_metrics("ev-u", verified=False)),
    ],
)
def test_page_unavailable_reasons_do_not_break_the_page(reason, record):
    page = published_page([("fact", ["ev-u"])], [record()])
    assert page.status == 200
    assert page.figures() == []
    (node,) = page.placeholders()
    assert node.attrs["data-evidence-id"] == "ev-u"
    assert node.attrs["data-reason"] == reason
    assert "Facts (1)" in page.text and "Unknown / gaps (1)" in page.text


def test_page_mixes_figures_and_placeholders_in_one_section():
    page = published_page(
        [("fact", ["ev-a", "ev-bad"])],
        [ok_metrics("ev-a"), evidence("ev-bad", content=[])],
    )
    section = page.charts_section()
    assert [f.attrs["data-evidence-id"] for f in section.find_all("figure")] == ["ev-a"]
    assert [
        p.attrs["data-evidence-id"]
        for p in section.find_all("p", cls="chart-unavailable")
    ] == ["ev-bad"]


def test_page_caps_at_six_figures_and_reports_the_rest():
    ids = [f"ev-{i}" for i in range(8)]
    page = published_page([("fact", ids)], [ok_metrics(e) for e in ids])
    assert page.status == 200
    assert order_of(page) == ids[:6]
    section_text = page.charts_section().text()
    assert "2 more cited metrics evidence not charted" in section_text


def test_page_six_figures_have_no_overflow_note():
    ids = [f"ev-{i}" for i in range(6)]
    page = published_page([("fact", ids)], [ok_metrics(e) for e in ids])
    assert order_of(page) == ids
    assert "not charted" not in page.charts_section().text()


def test_page_per_chart_series_cap_is_ten():
    rows = [row({"n": str(i)}, [(0, float(i)), (60, float(i) + 1)]) for i in range(12)]
    page = published_page([("fact", ["ev-a"])], [evidence("ev-a", rows=rows)])
    (figure,) = page.figures()
    assert len(figure.find_all("g", cls="series")) == 10


def test_page_truncated_and_skipped_notes_reach_the_page_caption():
    ev = ok_metrics(
        "ev-a",
        rows=[row({"job": "a"}, [(0, "1"), (60, "NaN"), (120, "3"), (2400, "9")])],
        truncated=True,
        omitted_rows=4,
    )
    page = published_page([("fact", ["ev-a"])], [ev])
    (figure,) = page.figures()
    text = caption(figure)
    assert "series truncated, omitted_rows=4" in text
    assert "skipped" in text.lower() and "out of window" in text.lower()


def test_page_data_never_comes_from_raw_or_the_report_text():
    page = published_page([("fact", ["ev-a"])], [ok_metrics("ev-a")])
    assert "raw_only" not in page.charts_section().text()
    assert "raw_only" not in " ".join(
        v for n in page.charts_section().iter() for v in n.attrs.values()
    )


def test_page_is_read_only_only_get_touches_the_evidence_store():
    page = published_page([("fact", ["ev-a"])], [ok_metrics("ev-a")])
    subject = page.workbench.list_incidents()[0].incident_id
    events_before = len(page.workbench.events.read_after(subject, 0))
    inputs_before = len(page.workbench.incidents.inputs)
    controls_before = len(page.workbench.incidents.controls)
    again = fetch(*_again(page))
    assert again.status == 200
    assert len(page.workbench.events.read_after(subject, 0)) == events_before
    assert len(page.workbench.incidents.inputs) == inputs_before
    assert len(page.workbench.incidents.controls) == controls_before
    assert isinstance(page.store, StubEvidenceStore)  # no register/commit exist


def test_page_hostile_evidence_cannot_add_script_or_external_urls():
    baseline = published_page([("fact", ["ev-a"])], [ok_metrics("ev-a")])
    payload = '<script>alert(1)</script>"><img src=x onerror=alert(1)>'
    hostile = evidence(
        "ev-a",
        rows=[
            row({"job": payload, "u": "https://evil.example/p"}, [(0, 1.0), (60, 2.0)]),
            row({"href": "javascript:alert(3)"}, [(0, 3.0), (60, 4.0)]),
        ],
        expr=f'up{{a="{payload}"}} https://evil.example/e javascript:alert(4)',
    )
    page = published_page([("fact", ["ev-a"])], [hostile])
    assert page.status == 200
    assert page.text.lower().count("<script") == baseline.text.lower().count("<script")
    assert page.root.find_all("img") == []
    section = page.charts_section()
    assert section is not None and section.find_all("script") == []
    forbidden = {
        "script",
        "iframe",
        "object",
        "embed",
        "image",
        "use",
        "foreignobject",
        "img",
    }
    for node in section.iter():
        assert node.tag not in forbidden, node.tag
        for name, value in node.attrs.items():
            assert not name.startswith("on"), (node.tag, name)
            if name in {"href", "xlink:href", "src"}:
                assert value.startswith("/incidents/"), (node.tag, name, value)
    assert "javascript:" not in " ".join(
        v.lower()
        for n in page.root.iter()
        for k, v in n.attrs.items()
        if k in {"href", "xlink:href", "src"}
    )


def test_page_hostile_evidence_id_is_escaped():
    weird = 'ev"><script>x</script>'
    page = published_page([("fact", [weird])], [ok_metrics(weird)])
    assert page.status == 200
    section = page.charts_section()
    assert section is not None and section.find_all("script") == []
    assert [f.attrs["data-evidence-id"] for f in section.find_all("figure")] == [weird]


# no report / unparseable / nothing cited


def test_page_without_a_report_has_no_charts_and_no_placeholders():
    app, workbench, _clock = build_workbench()
    incident = submit_incident(app, key="none-1").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    run_id = next(iter(workbench.incidents.run_ids(subject)))
    store = StubEvidenceStore(
        {"ev-a": _rebind(ok_metrics("ev-a"), run_id, str(subject))}
    )
    workbench.evidence = store
    page = fetch(app, workbench, incident, store)
    assert page.status == 200
    assert page.figures() == [] and page.placeholders() == []
    assert page.root.find(id="run-state") is not None or "run-state" in page.text


def test_page_with_unparseable_report_has_no_charts_and_no_placeholders():
    page = published_page(
        records=[ok_metrics("ev-a"), evidence("ev-bad", content=[])],
        report_text="this is not m0-report-v2 json ev-a ev-bad",
    )
    assert page.status == 200
    assert page.root.find(id="report-unparseable") is not None
    assert page.figures() == [] and page.placeholders() == []


def test_page_with_report_citing_no_metrics_evidence_has_no_charts_or_placeholders():
    page = published_page(
        [("fact", ["ev-trace"])],
        [
            evidence("ev-trace", tool="traces_search", content=[{"span": "x"}]),
            ok_metrics("ev-a"),
        ],
    )
    assert page.status == 200
    assert page.figures() == [] and page.placeholders() == []


# handoff report shares the rule


def test_page_unpublished_handoff_report_is_charted_like_a_published_one():
    app, workbench, clock = build_workbench()
    incident = submit_incident(app, key="handoff-1").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id

    def incomplete(call_):
        eid = None
        for message in reversed(call_.messages):
            if message.get("role") == "tool":
                eid = json.loads(message["content"])["evidence_id"]
                break
        return reply(content=report_json(evidence_id=eid, status="incomplete"))

    investigator = ScriptedInvestigator(
        clock,
        replies=[reply(tool_calls=[tool_call()], finish="tool_calls"), incomplete],
    )
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None and outcome.handoff is True
    (cited,) = [k for k in workbench.evidence._records]
    run_id = workbench.evidence._records[cited].run_id
    real = ok_metrics(cited)
    store = StubEvidenceStore({cited: _rebind(real, run_id, str(subject))})
    workbench.evidence = store
    page = fetch(app, workbench, incident, store)
    assert page.status == 200
    assert page.root.find(id="handoff-report") is not None
    assert order_of(page) == [cited]
    assert page.charts_section() is not None
