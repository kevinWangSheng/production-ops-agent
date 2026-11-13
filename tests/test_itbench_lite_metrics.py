"""#581: metrics TSV -> OpenMetrics: dedupe, empty values, derived calls_total,
offset. Synthetic TSVs only."""

from __future__ import annotations

import io
import re
from pathlib import Path

from scripts.itbench_lite.metrics import BUCKET, CALLS, convert_metrics
from scripts.itbench_lite.snapshot import load_snapshot

HEADER = "timestamp\tmetric_name\tvalue\ttags\tnamespace\tpod_name\tservice_name\n"
SVC_TAGS = "{'service_name': 'checkout', 'span_kind': 'SPAN_KIND_SERVER', 'status_code': '%s'%s}"


def _row(ts: str, name: str, value: str, tags: str, ns="", pod="", svc="") -> str:
    return f"{ts}\t{name}\t{value}\t{tags}\t{ns}\t{pod}\t{svc}\n"


def _snapshot(tmp_path: Path, files: dict[str, str]):
    root = tmp_path / "s"
    (root / "metrics").mkdir(parents=True)
    for name, body in files.items():
        (root / "metrics" / name).write_text(HEADER + body)
    return load_snapshot(root)


def _parse(text: str) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """``{(name, labels): [(value, timestamp), ...]}`` of an OpenMetrics text."""
    assert text.endswith("# EOF\n")
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = re.fullmatch(r"([a-zA-Z_:][\w:]*)(?:\{(.*)\})? (\S+) (\S+)", line)
        assert m, line
        out.setdefault((m[1], m[2] or ""), []).append((m[3], m[4]))
    return out


def _convert(snap, offset=0):
    buf = io.StringIO()
    stats = convert_metrics(snap, buf, offset_seconds=offset)
    return buf.getvalue(), stats


def test_duplicates_collapse_and_empty_values_are_skipped(tmp_path):
    tags = "{'container': 'app', '__name__': 'cpu'}"
    snap = _snapshot(
        tmp_path,
        {
            "pod_x.tsv": _row(
                "2025-12-15 17:30:00.000",
                "cpu_seconds",
                "1.5",
                tags,
                "otel-demo",
                "x-1",
            )
            + _row(
                "2025-12-15 17:30:00.000",
                "cpu_seconds",
                "1.5",
                tags,
                "otel-demo",
                "x-1",
            )
            + _row(
                "2025-12-15 17:30:30.000", "cpu_seconds", "", tags, "otel-demo", "x-1"
            )
            + _row(
                "2025-12-15 17:31:00.000", "cpu_seconds", "2", tags, "otel-demo", "x-1"
            )
            + _row(
                "2025-12-15 17:31:00.000", "cpu_seconds", "2", tags, "otel-demo", "x-1"
            ),
        },
    )
    text, stats = _convert(snap)
    series = _parse(text)
    key = ("cpu_seconds", 'container="app",namespace="otel-demo",pod="x-1"')
    assert series == {key: [("1.5", "1765819800.000"), ("2.0", "1765819860.000")]}
    assert stats.duplicate_samples == 2 and stats.empty_values == 1
    assert stats.conflicting_duplicates == 0
    assert text.count("# TYPE cpu_seconds unknown") == 1


def test_calls_total_is_derived_from_the_inf_bucket_for_every_status(tmp_path):
    rows = ""
    for i, (ok, err, orig) in enumerate(
        [(10, 41, 0), (20, 42, 0), (30, 43, 43), (40, 44, 44)]
    ):
        ts = f"2025-12-15 17:3{i}:00.000"
        for status, value in (("STATUS_CODE_UNSET", ok), ("STATUS_CODE_ERROR", err)):
            rows += _row(
                ts,
                BUCKET,
                str(value),
                SVC_TAGS % (status, ", 'le': '+Inf'"),
                svc="checkout",
            )
            rows += _row(
                ts, BUCKET, "1", SVC_TAGS % (status, ", 'le': '5'"), svc="checkout"
            )
        # the original export: ERROR only, first two points zero-filled
        rows += _row(
            ts, CALLS, str(orig), SVC_TAGS % ("STATUS_CODE_ERROR", ""), svc="checkout"
        )
    rows += _row(
        "2025-12-15 17:30:00.000",
        CALLS,
        "7",
        "{'service_name': 'lonely', 'status_code': 'STATUS_CODE_ERROR'}",
    )
    snap = _snapshot(tmp_path, {"service_checkout.tsv": rows})
    text, stats = _convert(snap)
    series = _parse(text)
    calls = {k[1]: [v for v, _ in pts] for k, pts in series.items() if k[0] == CALLS}
    base = 'service_name="checkout",span_kind="SPAN_KIND_SERVER",status_code='
    assert calls[base + '"STATUS_CODE_UNSET"'] == ["10.0", "20.0", "30.0", "40.0"]
    # derived from the bucket, so the zero-filled leading points are gone
    assert calls[base + '"STATUS_CODE_ERROR"'] == ["41.0", "42.0", "43.0", "44.0"]
    # an original series without a bucket counterpart survives untouched
    assert calls['service_name="lonely",status_code="STATUS_CODE_ERROR"'] == ["7.0"]
    assert stats.derived_calls_series == 2
    assert stats.original_calls_series == 2 and stats.original_calls_kept == 1
    assert stats.derived_differs_from_original == 1
    assert all('le="' not in labels for (name, labels) in series if name == CALLS)


def test_offset_moves_every_sample_and_labels_are_escaped(tmp_path):
    tags = "{'path': 'a\"b\\\\c'}"
    snap = _snapshot(
        tmp_path, {"p.tsv": _row("2025-12-15 17:30:00.123456", "m", "3", tags)}
    )
    text, stats = _convert(snap, offset=3600)
    assert 'm{path="a\\"b\\\\c"} 3.0 1765823400.123\n' in text
    assert stats.start_ms == stats.end_ms == 1765823400123


def test_bad_rows_are_counted_not_emitted(tmp_path):
    snap = _snapshot(
        tmp_path,
        {
            "p.tsv": _row("2025-12-15 17:30:00", "m", "1", "{not python")
            + _row("2025-12-15 17:30:00", "m", "abc", "{}")
            + _row("2025-12-15 17:30:00", "bad-name", "1", "{}")
            + _row("2025-12-15 17:30:00", "m", "1", "{'bad.label': 'x'}")
            + _row("2025-12-15 17:30:00", "m", "1", "{}"),
        },
    )
    text, stats = _convert(snap)
    assert stats.bad_rows == 4 and stats.samples == 1
    assert _parse(text) == {("m", ""): [("1.0", "1765819800.000")]}
