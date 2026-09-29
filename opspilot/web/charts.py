"""Server-rendered time-series charts for metrics evidence cited by a report.

A pure projection of one stored evidence view into an inline SVG: no I/O, no
script, no external reference. The data is the committed model view
(``StoredEvidence.view``), never ``raw`` and never report prose, so a chart
shows what the model saw and what the evidence link resolves to
(PRODUCT-CONSTRAINTS, "Evidence and context requirements"). What the chart
cannot show is stated, not filled: a missing stretch breaks the line, a
non-numeric or out-of-window sample is skipped and counted, a truncated view
says so.

Metric labels, the query text and evidence ids are untrusted evidence; every
one is escaped on the way out.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any
from urllib.parse import quote

from markupsafe import Markup, escape

from opspilot.web.evidence import StoredEvidence

__all__ = [
    "MAX_FIGURES",
    "MAX_SERIES",
    "ChartFragment",
    "evidence_chart",
    "render_evidence_chart",
]

METRICS_TOOL = "metrics_range_query"
#: Charts per incident page and series per chart (contract, task record).
MAX_FIGURES = 6
MAX_SERIES = 10
#: A gap wider than this many steps between drawn points breaks the line.
GAP_STEPS = 1.5
LABEL_LIMIT = 80

_WIDTH, _HEIGHT = 720, 260
_LEFT, _RIGHT, _TOP, _BOTTOM = 64, _WIDTH - 12, 12, _HEIGHT - 32
_COLORS = (
    "#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e",
    "#17becf", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
)  # fmt: skip
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


@dataclass(frozen=True)
class ChartFragment:
    #: ``figure`` for a drawn chart, ``unavailable`` for a placeholder.
    kind: str
    html: Markup


class _Shape(Exception):
    pass


class _Unplottable(Exception):
    """Values or their span cannot be mapped to finite coordinates."""


def render_evidence_chart(
    evidence: StoredEvidence, cited_by: Sequence[str]
) -> str | None:
    """The HTML fragment for one cited evidence, or ``None`` if not metrics."""
    fragment = evidence_chart(evidence, cited_by)
    return None if fragment is None else str(fragment.html)


def evidence_chart(
    evidence: StoredEvidence, cited_by: Sequence[str]
) -> ChartFragment | None:
    view = evidence.view
    if view.get("tool") != METRICS_TOOL:
        return None
    if not evidence.hashes_verified:
        return _unavailable(evidence, "hash_mismatch")
    if not (
        view.get("status") == "ok"
        and view.get("adopted") is True
        and evidence.status == "ok"
        and evidence.adopted
    ):
        return _unavailable(evidence, "not_ok")
    content = view.get("content")
    if content is None or content == []:
        return _unavailable(evidence, "no_series")
    try:
        window = _window(view)
        series = _series(content)
    except _Shape:
        return _unavailable(evidence, "unrecognized_shape")
    step = _step(view)
    plotted, skipped, outside, empty = [], 0, 0, 0
    for labels, points in series:
        kept = []
        for ts, raw in points:
            value = _value(raw)
            if value is None:
                skipped += 1
            elif not window[0] <= ts <= window[1]:
                outside += 1
            else:
                kept.append((ts, value))
        if kept:
            plotted.append((labels, kept))
        else:
            empty += 1
    if not plotted:
        return _unavailable(evidence, "no_series")
    try:
        html = _figure(
            evidence, cited_by, window, step, plotted, skipped, outside, empty
        )
    except _Unplottable:
        return _unavailable(evidence, "unplottable_range")
    return ChartFragment("figure", html)


# -- parsing ----------------------------------------------------------------


def _window(view: Mapping[str, Any]) -> tuple[float, float]:
    window = view.get("window")
    if not isinstance(window, Mapping):
        raise _Shape
    try:
        start = datetime.fromisoformat(str(window["start"]))
        end = datetime.fromisoformat(str(window["end"]))
    except (KeyError, ValueError):
        raise _Shape from None
    if start.tzinfo is None or end.tzinfo is None:
        raise _Shape
    if end <= start:
        raise _Shape
    return start.timestamp(), end.timestamp()


def _step(view: Mapping[str, Any]) -> float | None:
    query = view.get("query")
    step = query.get("step_seconds") if isinstance(query, Mapping) else None
    if isinstance(step, bool) or not isinstance(step, (int, float)):
        return None
    return float(step) if isfinite(step) and step > 0 else None


def _series(
    content: object,
) -> list[tuple[Mapping[str, Any], list[tuple[float, str]]]]:
    if not isinstance(content, list):
        raise _Shape
    out = []
    for row in content:
        if not isinstance(row, Mapping):
            raise _Shape
        metric, values = row.get("metric"), row.get("values")
        if not isinstance(metric, Mapping) or not isinstance(values, list):
            raise _Shape
        points = []
        for pair in values:
            if not isinstance(pair, list) or len(pair) != 2:
                raise _Shape
            ts, raw = pair
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                raise _Shape
            if not isinstance(raw, str):
                raise _Shape
            try:
                seconds = float(ts)
            except OverflowError:
                raise _Shape from None
            if not isfinite(seconds):
                raise _Shape
            points.append((seconds, raw))
        out.append((metric, points))
    return out


def _value(raw: str) -> float | None:
    """A finite number, else ``None`` (NaN, ±Inf and text are skipped)."""
    text = raw.strip()
    if not _NUMBER.fullmatch(text):
        return None
    value = float(text)
    return value if value == value and abs(value) != float("inf") else None


# -- drawing ----------------------------------------------------------------


def _figure(
    evidence: StoredEvidence,
    cited_by: Sequence[str],
    window: tuple[float, float],
    step: float | None,
    plotted: list[tuple[Mapping[str, Any], list[tuple[float, float]]]],
    skipped: int,
    outside: int,
    empty: int,
) -> Markup:
    shown, hidden = plotted[:MAX_SERIES], len(plotted) - MAX_SERIES
    values = [v for _, pts in shown for _, v in pts]
    low = min(values)
    if low >= 0:
        low = 0.0
    high = max(values)
    if high <= low:
        # A constant series: widen by a magnitude-relative amount so the span
        # survives float precision (``-1e20 + 1.0 == -1e20``).
        high = low + max(1.0, abs(low) * 1e-6)
    if not isfinite(high - low) or high <= low:
        raise _Unplottable
    t0, t1 = window

    def px(ts: float) -> float:
        return round(_LEFT + (ts - t0) / (t1 - t0) * (_RIGHT - _LEFT), 2)

    def py(value: float) -> float:
        return round(_BOTTOM - (value - low) / (high - low) * (_BOTTOM - _TOP), 2)

    eid = str(escape(evidence.evidence_id))
    if not all(isfinite(px(ts)) for _, pts in shown for ts, _ in pts) or not all(
        isfinite(py(v)) for v in values
    ):
        raise _Unplottable
    expr = _expr(evidence.view)
    groups, legend, singles = [], [], []
    for index, (labels, points) in enumerate(shown):
        color = _COLORS[index % len(_COLORS)]
        label = _label(labels)
        shapes = []
        for run in _runs(points, step):
            if len(run) == 1:
                ts, value = run[0]
                shapes.append(
                    f'<circle cx="{px(ts)}" cy="{py(value)}" r="3" fill="{color}"/>'
                )
            else:
                coords = " ".join(f"{px(ts)},{py(v)}" for ts, v in run)
                shapes.append(
                    f'<polyline points="{coords}" fill="none" '
                    f'stroke="{color}" stroke-width="1.5"/>'
                )
        groups.append(
            f'<g class="series" data-series-label="{escape(label)}" '
            f'data-point-count="{len(points)}">{"".join(shapes)}</g>'
        )
        legend.append(
            f'<li><span style="color:{color}">&#9632;</span> '
            f"<code>{escape(label)}</code></li>"
        )
        if len(points) == 1:
            singles.append(f"{escape(label)} = {_num(points[0][1])}")
    axis = (
        f'<line x1="{_LEFT}" y1="{_BOTTOM}" x2="{_RIGHT}" y2="{_BOTTOM}" '
        'stroke="#888"/>'
        f'<line x1="{_LEFT}" y1="{_TOP}" x2="{_LEFT}" y2="{_BOTTOM}" stroke="#888"/>'
        f'<text x="{_LEFT - 4}" y="{_TOP + 4}" text-anchor="end" font-size="11">'
        f"{_num(high)}</text>"
        f'<text x="{_LEFT - 4}" y="{_BOTTOM}" text-anchor="end" font-size="11">'
        f"{_num(low)}</text>"
        f'<text x="{_LEFT}" y="{_HEIGHT - 8}" font-size="11">{_clock(t0)} UTC</text>'
        f'<text x="{_RIGHT}" y="{_HEIGHT - 8}" text-anchor="end" font-size="11">'
        f"{_clock(t1)} UTC</text>"
    )
    svg = (
        f'<svg role="img" viewBox="0 0 {_WIDTH} {_HEIGHT}" width="100%" '
        f'style="max-width:{_WIDTH}px" '
        f'data-x-start="{_iso(t0)}" data-x-end="{_iso(t1)}" '
        f'data-y-min="{low!r}" data-y-max="{high!r}">'
        f"<title>Time series for {eid}: {escape(expr)}</title>"
        f"{axis}{''.join(groups)}</svg>"
    )
    everywhere = (
        " (counted over all series, including those not drawn)" if hidden > 0 else ""
    )
    notes = []
    if step is not None:
        notes.append(f"step {_num(step)} s")
    if singles:
        notes.append("single-point series: " + "; ".join(singles))
    if skipped:
        notes.append(f"{skipped} non-finite point(s) skipped{everywhere}")
    if outside:
        notes.append(f"{outside} point(s) out of window not drawn{everywhere}")
    if empty:
        notes.append(f"{empty} series without plottable points")
    if hidden > 0:
        notes.append(f"{hidden} series not drawn (limit {MAX_SERIES})")
    view = evidence.view
    if view.get("truncated"):
        notes.append(
            f"series truncated, omitted_rows={escape(str(view.get('omitted_rows')))}"
        )
    cited = ", ".join(str(escape(c)) for c in cited_by)
    caption = (
        f"<figcaption>{_link(evidence)} · <code>{escape(expr)}</code> · "
        f"{_clock(t0, date=True)} to {_clock(t1, date=True)} UTC"
        f"{''.join(' · ' + n for n in notes)}"
        f"{' · cited by: ' + cited if cited else ''}</figcaption>"
    )
    return Markup(
        f'<figure class="evidence-chart" data-evidence-id="{eid}" '
        f'data-tool="{METRICS_TOOL}">{svg}'
        f'<ul class="chart-legend">{"".join(legend)}</ul>{caption}</figure>'
    )


def _runs(
    points: list[tuple[float, float]], step: float | None
) -> list[list[tuple[float, float]]]:
    """Consecutive drawn points; a gap wider than 1.5 steps starts a new run."""
    runs: list[list[tuple[float, float]]] = []
    for point in points:
        if runs and (step is None or point[0] - runs[-1][-1][0] <= GAP_STEPS * step):
            runs[-1].append(point)
        else:
            runs.append([point])
    return runs


def _unavailable(evidence: StoredEvidence, reason: str) -> ChartFragment:
    return ChartFragment(
        "unavailable",
        Markup(
            f'<p class="chart-unavailable" '
            f'data-evidence-id="{escape(evidence.evidence_id)}" '
            f'data-reason="{reason}">No chart for {_link(evidence)}: '
            f"{reason.replace('_', ' ')}.</p>"
        ),
    )


def _link(evidence: StoredEvidence) -> Markup:
    href = (
        f"/incidents/{quote(str(evidence.subject_id), safe=':')}"
        f"/evidence/{quote(evidence.evidence_id, safe=':')}"
    )
    return Markup(
        f'<a href="{escape(href)}"><code>{escape(evidence.evidence_id)}</code></a>'
    )


def _expr(view: Mapping[str, Any]) -> str:
    query = view.get("query")
    expr = query.get("expr") if isinstance(query, Mapping) else None
    return expr if isinstance(expr, str) else ""


def _label(labels: Mapping[str, Any]) -> str:
    text = ", ".join(f'{k}="{v}"' for k, v in labels.items()) or "{}"
    return text if len(text) <= LABEL_LIMIT else text[: LABEL_LIMIT - 1] + "…"


def _num(value: float) -> str:
    return format(value, ".6g")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _clock(ts: float, *, date: bool = False) -> str:
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    return when.strftime("%Y-%m-%d %H:%M:%S" if date else "%m-%d %H:%M")
