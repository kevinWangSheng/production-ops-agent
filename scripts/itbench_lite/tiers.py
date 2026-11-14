"""Coverage tier file generator for the ITBench-Lite SRE run (#605, r6 L-V1-L-V5, L-V10).

Rules (decision table r6, docs/tasks/2026-11-12-tool-expansion-contract.md):

* L-V1 every number is recomputed from the snapshot through the whitelist
  loader (``snapshot.load_snapshot``); no answer file is ever opened. An
  optional census JSON may be given for a cross-check: only the columns in
  ``CENSUS_COLUMNS`` are kept while it is parsed, every other key (``mechanism``
  in particular, which comes from the answers) is dropped unread.
* L-V2 the fault start is the unshifted ``activeAt`` of the entry alert
  (``alerts.select_entry_alert`` over ``alerts.final_alert_file``).
* L-V3 each of metrics, traces, objects, events and logs is judged once:
  ``class end - fault start`` >= 5 minutes is covered, 0 to 5 minutes is
  partial, below 0 (or no data of that class) is not covered.
* L-V4 two tiers: traces covered -> ``TIER_TRACES``, otherwise
  ``TIER_NO_TRACES``; the per-class coverage goes into the detail file.
* L-V5 ``tiers.json`` is ``{"S<id>": "<tier>"}`` for exactly the 35 official
  scenarios (``coverage.load_tiers`` accepts it as is), plus
  ``tiers_detail.json`` keyed the same way: per scenario the ``fault_start``,
  the ``ends`` and ``coverage`` maps over the five classes (times RFC 3339 in
  UTC, ``Z``), the tier and the rule. Neither holds answer content.
* L-V10 every TSV is streamed row by row; nothing is cached.

Scenario ids come from the directory names (``coverage.scenario_key``). A
scenario directory may be the clean copy itself or the driver's
``S<id>/`` directory holding it under ``snapshot/``.

Usage::

    python -m scripts.itbench_lite.tiers <copies root> --out tiers.json \\
        --detail tiers_detail.json [--census survey.json]
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.itbench_lite import shift
from scripts.itbench_lite.alerts import final_alert_file, select_entry_alert
from scripts.itbench_lite.coverage import SCENARIO_IDS, load_tiers, scenario_key
from scripts.itbench_lite.snapshot import Snapshot, UnsafeSnapshot, load_snapshot

TIER_TRACES = "指标+trace"
TIER_NO_TRACES = "故障窗无 trace"
COVERED = "covered"
PARTIAL = "partial"
NOT_COVERED = "not_covered"
#: The question window: a class covers the fault when it runs this long past it.
WINDOW = timedelta(minutes=5)
WINDOW_NS = WINDOW // timedelta(microseconds=1) * 1000
#: The five judged classes and their end-time readers (alerts are not judged:
#: they define the fault start).
CLASS_READERS: Mapping[str, Callable[[Snapshot], int | None]] = {
    "metrics": shift.metrics_end_ns,
    "traces": shift.traces_end_ns,
    "objects": shift.objects_end_ns,
    "events": shift.events_end_ns,
    "logs": shift.logs_end_ns,
}
#: Census columns that carry no answer information (L-V1, L11).
CENSUS_COLUMNS = frozenset(
    {
        "scenario",
        "fault_start_proxy",
        "trace_end",
        "k8s_objects",
        "k8s_events",
        "otel_logs",
    }
)
CLEAN_COPY_DIR = "snapshot"
RULE = {
    "window_seconds": int(WINDOW.total_seconds()),
    "fault_start": "unshifted activeAt of alerts.select_entry_alert(final_alert_file)",
    "classes": list(CLASS_READERS),
    "covered": f"end - fault_start >= {int(WINDOW.total_seconds())} s",
    "partial": f"0 s <= end - fault_start < {int(WINDOW.total_seconds())} s",
    "not_covered": "end - fault_start < 0 s, or no data of the class",
    "tier": f"traces covered -> {TIER_TRACES}; otherwise {TIER_NO_TRACES}",
    "source": "snapshot whitelist only (load_snapshot); no answer file",
}


def judge(end_ns: int | None, fault_start_ns: int) -> str:
    """L-V3 coverage of one class."""
    if end_ns is None:
        return NOT_COVERED
    delta = end_ns - fault_start_ns
    if delta >= WINDOW_NS:
        return COVERED
    if delta >= 0:
        return PARTIAL
    return NOT_COVERED


def tier_of(classes: Mapping[str, str]) -> str:
    """L-V4: only covered traces put a scenario in the trace tier."""
    return TIER_TRACES if classes.get("traces") == COVERED else TIER_NO_TRACES


def _iso(value: int | None) -> str | None:
    """RFC 3339 in UTC with ``Z``; nanosecond digits only as far as needed."""
    if value is None:
        return None
    seconds, rest = divmod(value, shift.NS)
    text = shift.from_ns(seconds * shift.NS).strftime("%Y-%m-%dT%H:%M:%S")
    if rest:
        text += "." + f"{rest:09d}".rstrip("0")
    return text + "Z"


def scenario_label(key: str) -> str:
    """The id form used as key in both files (``16`` -> ``S16``)."""
    return f"S{key}"


def fault_start(snapshot: Snapshot) -> tuple[int, str]:
    """``(epoch ns, alertname)`` of the entry alert's unshifted ``activeAt`` (L-V2)."""
    entry = select_entry_alert(snapshot.read_alert_file(final_alert_file(snapshot)))
    labels = entry.get("labels") or {}
    return shift.parse_rfc3339_ns(entry["activeAt"]), str(labels.get("alertname", ""))


def scenario_detail(snapshot: Snapshot) -> dict[str, Any]:
    """Per-class ends and coverage plus the tier of one loaded scenario."""
    start_ns, alertname = fault_start(snapshot)
    ends: dict[str, str | None] = {}
    after: dict[str, float | None] = {}
    coverage: dict[str, str] = {}
    for kind, reader in CLASS_READERS.items():
        end_ns = reader(snapshot)
        ends[kind] = _iso(end_ns)
        after[kind] = None if end_ns is None else (end_ns - start_ns) / shift.NS
        coverage[kind] = judge(end_ns, start_ns)
    return {
        "fault_start": _iso(start_ns),
        "entry_alertname": alertname,
        "ends": ends,
        "seconds_after_fault_start": after,
        "coverage": coverage,
        "tier": tier_of(coverage),
        "rule": RULE,
    }


def scenario_dirs(root: Path) -> dict[str, Path]:
    """``{id: clean copy}`` for every child directory whose name is a scenario id.

    Lists names only. A child that is a symlink is refused; a child holding a
    ``snapshot/`` directory (the driver's layout) points at that directory.
    """
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    found: dict[str, Path] = {}
    with os.scandir(root) as entries:
        children = sorted(entries, key=lambda e: e.name)
    for entry in children:
        if entry.is_symlink():
            raise UnsafeSnapshot(f"{entry.name}: scenario directory is a symlink")
        if not entry.is_dir(follow_symlinks=False):
            continue
        try:
            key = scenario_key(entry.name)
        except ValueError:
            continue
        if key in found:
            raise ValueError(f"two directories for scenario {key}")
        path = Path(entry.path)
        inner = path / CLEAN_COPY_DIR
        if inner.is_symlink():
            raise UnsafeSnapshot(f"{entry.name}: {CLEAN_COPY_DIR} is a symlink")
        found[key] = inner if inner.is_dir() else path
    return found


# -- optional census cross-check ---------------------------------------------------


def _keep_census_columns(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object hook: a census row keeps only the allowed columns.

    Any object with a ``scenario`` key is a row; other objects (containers)
    keep their keys except ``mechanism``. Dropped values are never looked at.
    """
    keys = {k for k, _ in pairs}
    if "scenario" in keys:
        return {k: v for k, v in pairs if k in CENSUS_COLUMNS}
    return {k: v for k, v in pairs if k != "mechanism"}


def _census_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        if "scenario" in value:
            return [value]
        return [row for v in value.values() for row in _census_rows(v)]
    if isinstance(value, list):
        return [row for v in value for row in _census_rows(v)]
    return []


def load_census(path: Path) -> dict[str, dict[str, Any]]:
    """``{id: allowed columns}`` of a census JSON (see ``_keep_census_columns``)."""
    data = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_keep_census_columns
    )
    rows: dict[str, dict[str, Any]] = {}
    for row in _census_rows(data):
        try:
            key = scenario_key(str(row["scenario"]))
        except ValueError:
            continue
        rows[key] = {k: v for k, v in row.items() if k != "scenario"}
    return rows


def _parse_time(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return shift.parse_rfc3339_ns(value)
    except ValueError:
        pass
    try:
        return shift.parse_clickhouse_ns(value)
    except ValueError:
        return None


def _compare(census_value: Any, ours: str | None) -> str:
    theirs = _parse_time(census_value)
    if theirs is None:
        return "unparsed"
    mine = None if ours is None else _parse_time(ours)
    if mine is None:
        return "differ"
    return "agree" if theirs == mine else "differ"


def cross_check(detail: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Census columns of one scenario and whether the two time columns agree."""
    return {
        "columns": dict(row),
        "fault_start_proxy": _compare(
            row.get("fault_start_proxy"), detail["fault_start"]
        ),
        "trace_end": _compare(row.get("trace_end"), detail["ends"]["traces"]),
    }


# -- whole run ------------------------------------------------------------------------


def build(
    root: Path,
    *,
    census: Mapping[str, Mapping[str, Any]] | None = None,
    expected: frozenset[str] = SCENARIO_IDS,
) -> tuple[dict[str, str], dict[str, Any]]:
    """``(tiers, detail)`` over the scenario copies under ``root``, both keyed
    ``S<id>`` in id order."""
    dirs = scenario_dirs(root)
    if dirs.keys() != expected:
        missing = sorted(expected - dirs.keys(), key=int)
        extra = sorted(dirs.keys() - expected, key=int)
        raise ValueError(
            f"copies root must hold exactly the {len(expected)} official scenarios;"
            f" missing {missing}, unexpected {extra}"
        )
    scenarios: dict[str, Any] = {}
    for key in sorted(dirs, key=int):
        snapshot = load_snapshot(dirs[key])
        info = {"directory": dirs[key].relative_to(root).as_posix()}
        try:
            info.update(scenario_detail(snapshot))
        except Exception as error:
            error.add_note(f"scenario {key}")
            raise
        if census is not None:
            row = census.get(key)
            info["census"] = None if row is None else cross_check(info, row)
        scenarios[scenario_label(key)] = info
    tiers = {label: info["tier"] for label, info in scenarios.items()}
    return tiers, scenarios


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="directory holding the clean scenario copies")
    parser.add_argument("--out", required=True, help="tiers.json")
    parser.add_argument("--detail", required=True, help="tiers_detail.json")
    parser.add_argument(
        "--census", help="optional census JSON; only non-answer columns are kept"
    )
    args = parser.parse_args(argv)
    census = load_census(Path(args.census)) if args.census else None
    tiers, detail = build(Path(args.root), census=census)
    load_tiers(tiers)  # the file must be what ``coverage.py --tiers`` accepts
    _write_json(Path(args.out), tiers)
    _write_json(Path(args.detail), detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
