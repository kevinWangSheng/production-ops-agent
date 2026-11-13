"""ITBench-Lite metrics TSVs -> one OpenMetrics file for promtool (#581).

Output is meant for ``promtool tsdb create-blocks-from openmetrics``.

- Duplicate samples (same series, same millisecond) keep the first value;
  rows with an empty or non-numeric value, unparseable tags or an invalid
  metric or label name are skipped and counted.
- ``traces_span_metrics_calls_total`` is exported only for
  ``STATUS_CODE_ERROR``, which makes every error ratio 1. It is replaced by a
  series per status derived from the duration histogram's ``le="+Inf"``
  bucket (the same span count). An original series with no bucket
  counterpart is kept as is.
- Every sample moves by the scenario's offset (whole seconds, see ``shift``).
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from scripts.itbench_lite.shift import parse_clickhouse_ns
from scripts.itbench_lite.snapshot import Snapshot, load_snapshot

CALLS = "traces_span_metrics_calls_total"
BUCKET = "traces_span_metrics_duration_milliseconds_bucket"
_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

Labels = tuple[tuple[str, str], ...]
SeriesKey = tuple[str, Labels]
Series = dict[SeriesKey, dict[int, float]]


@dataclass
class MetricsStats:
    rows: int = 0
    empty_values: int = 0
    bad_rows: int = 0
    duplicate_samples: int = 0
    conflicting_duplicates: int = 0
    original_calls_series: int = 0
    derived_calls_series: int = 0
    derived_equal_to_original: int = 0
    derived_differs_from_original: int = 0
    original_calls_kept: int = 0
    families: int = 0
    series: int = 0
    samples: int = 0
    start_ms: int | None = None
    end_ms: int | None = None


class _BadRow(ValueError):
    pass


def _labels(row: dict[str, str]) -> Labels:
    try:
        tags = ast.literal_eval(row["tags"]) if (row.get("tags") or "").strip() else {}
    except (ValueError, SyntaxError) as exc:
        raise _BadRow("tags") from exc
    if not isinstance(tags, dict):
        raise _BadRow("tags")
    labels = {
        str(k): str(v)
        for k, v in tags.items()
        if not str(k).startswith("__") and v not in (None, "")
    }
    for column in ("namespace", "service_name"):
        if row.get(column) and column not in labels:
            labels[column] = row[column]
    if row.get("pod_name") and "pod" not in labels:
        labels["pod"] = row["pod_name"]
    if any(_LABEL_NAME.fullmatch(k) is None for k in labels):
        raise _BadRow("label name")
    return tuple(sorted(labels.items()))


def collect_series(
    snapshot: Snapshot, *, offset_seconds: int
) -> tuple[Series, MetricsStats]:
    """Every series of the scenario, deduplicated and moved by the offset."""
    stats = MetricsStats()
    series: Series = defaultdict(dict)
    for path in snapshot.metrics:
        for row in snapshot.iter_tsv(path):
            stats.rows += 1
            raw = (row.get("value") or "").strip()
            if not raw:
                stats.empty_values += 1
                continue
            try:
                value = float(raw)
                name = row["metric_name"]
                if _METRIC_NAME.fullmatch(name or "") is None:
                    raise _BadRow("metric name")
                labels = _labels(row)
                ms = parse_clickhouse_ns(row["timestamp"]) // 1_000_000
            except (KeyError, ValueError):
                stats.bad_rows += 1
                continue
            ms += offset_seconds * 1000
            points = series[(name, labels)]
            if ms in points:
                stats.duplicate_samples += 1
                if not _same(points[ms], value):
                    stats.conflicting_duplicates += 1
                continue
            points[ms] = value
    return dict(series), stats


def _same(a: float, b: float) -> bool:
    return a == b or (math.isnan(a) and math.isnan(b))


def derive_calls_total(series: Series, stats: MetricsStats) -> Series:
    """``series`` with ``calls_total`` rebuilt from the ``le="+Inf"`` buckets."""
    derived: Series = {}
    for (name, labels), points in series.items():
        if name == BUCKET and dict(labels).get("le") == "+Inf":
            key = (CALLS, tuple(item for item in labels if item[0] != "le"))
            derived[key] = dict(points)
    out = {k: v for k, v in series.items() if k[0] != CALLS}
    for key, points in series.items():
        if key[0] != CALLS:
            continue
        stats.original_calls_series += 1
        twin = derived.get(key)
        if twin is None:
            stats.original_calls_kept += 1
            out[key] = points
        elif all(_same(points[t], twin[t]) for t in set(points) & set(twin)):
            stats.derived_equal_to_original += 1
        else:
            stats.derived_differs_from_original += 1
    stats.derived_calls_series = len(derived)
    out.update(derived)
    return out


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(value)


def _timestamp(ms: int) -> str:
    seconds, rest = divmod(ms, 1000)
    return f"{seconds}.{rest:03d}"


def write_openmetrics(series: Series, out: TextIO, stats: MetricsStats) -> None:
    """One ``unknown`` family per metric name, series and samples in order."""
    by_name: dict[str, list[SeriesKey]] = defaultdict(list)
    for key in series:
        by_name[key[0]].append(key)
    for name in sorted(by_name):
        out.write(f"# TYPE {name} unknown\n")
        for key in sorted(by_name[name]):
            rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in key[1])
            head = f"{name}{{{rendered}}}" if rendered else name
            for ms in sorted(series[key]):
                out.write(f"{head} {_number(series[key][ms])} {_timestamp(ms)}\n")
                stats.samples += 1
                stats.start_ms = (
                    ms if stats.start_ms is None else min(stats.start_ms, ms)
                )
                stats.end_ms = ms if stats.end_ms is None else max(stats.end_ms, ms)
    out.write("# EOF\n")
    stats.families = len(by_name)
    stats.series = len(series)


def convert_metrics(
    snapshot: Snapshot, out: TextIO, *, offset_seconds: int
) -> MetricsStats:
    series, stats = collect_series(snapshot, offset_seconds=offset_seconds)
    write_openmetrics(derive_calls_total(series, stats), out, stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot")
    parser.add_argument("out", type=Path)
    parser.add_argument("--offset-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    snapshot = load_snapshot(args.snapshot)
    with args.out.open("w", encoding="utf-8") as out:
        stats = convert_metrics(snapshot, out, offset_seconds=args.offset_seconds)
    json.dump(asdict(stats), sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
