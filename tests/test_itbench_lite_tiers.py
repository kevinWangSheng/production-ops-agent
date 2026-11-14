"""#605: coverage tier generator over synthetic scenario copies."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.itbench_lite import tiers
from scripts.itbench_lite.coverage import SCENARIO_IDS, load_tiers
from scripts.itbench_lite.snapshot import (
    AnswerFilePresent,
    UnsafeSnapshot,
    load_snapshot,
)

FAULT = datetime(2025, 12, 15, 17, 30, tzinfo=UTC)


def _ch(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f") + "000"


def _rfc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tsv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _alert(name: str, at: datetime, **labels: str) -> dict:
    return {
        "labels": {"alertname": name, **labels},
        "annotations": {},
        "state": "firing",
        "activeAt": _rfc(at),
    }


def make_scenario(
    root: Path,
    *,
    traces_after: timedelta | None = timedelta(minutes=10),
    other_after: timedelta = timedelta(minutes=10),
    logs: bool = True,
) -> Path:
    root.mkdir(parents=True)
    (root / "metrics").mkdir()
    _tsv(
        root / "metrics" / "m.tsv",
        [
            {"timestamp": _ch(FAULT + other_after), "metric_name": "x", "value": "1"},
            {
                "timestamp": _ch(FAULT + timedelta(hours=1)),
                "metric_name": "x",
                "value": "",
            },
        ],
    )
    alerts = [
        _alert("Watchdog", FAULT - timedelta(hours=1)),
        _alert("RequestErrorRate", FAULT, service_name="checkout"),
        _alert("RequestLatency", FAULT + timedelta(minutes=1), service_name="cart"),
    ]
    (root / "alerts_early.json").write_text(
        json.dumps([_alert("Early", FAULT - timedelta(hours=2))])
    )
    (root / "alerts_alerting_state.json").write_text(
        json.dumps({"data": {"alerts": alerts}})
    )
    if traces_after is not None:
        _tsv(
            root / "otel_traces_raw.tsv",
            [
                {
                    "Timestamp": _ch(FAULT + traces_after - timedelta(seconds=1)),
                    "Duration": str(1_000_000_000),
                    "ServiceName": "checkout",
                    "Events.Timestamp": "[]",
                }
            ],
        )
    body_row = {
        "Timestamp": _ch(FAULT + other_after),
        "Body": json.dumps({"metadata": {"name": "p"}}),
        "ResourceAttributes": "{}",
        "LogAttributes": "{'k8s.resource.name': 'pods'}",
    }
    _tsv(root / "k8s_objects_raw.tsv", [body_row])
    _tsv(root / "k8s_events_raw.tsv", [dict(body_row, Body="{}")])
    if logs:
        _tsv(root / "otel_logs_raw.tsv", [dict(body_row, Body="hello")])
    return root


def test_iso_keeps_nanoseconds_only_as_needed():
    assert tiers._iso(0) == "1970-01-01T00:00:00Z"
    assert tiers._iso(1_500_000_000) == "1970-01-01T00:00:01.5Z"
    assert tiers._iso(1_000_000_007) == "1970-01-01T00:00:01.000000007Z"
    assert tiers._iso(None) is None


def test_judge_thresholds():
    start = 10 * 60 * 10**9
    assert tiers.judge(start + 300 * 10**9, start) == tiers.COVERED
    assert tiers.judge(start + 300 * 10**9 - 1, start) == tiers.PARTIAL
    assert tiers.judge(start, start) == tiers.PARTIAL
    assert tiers.judge(start - 1, start) == tiers.NOT_COVERED
    assert tiers.judge(None, start) == tiers.NOT_COVERED


def test_scenario_detail_uses_entry_alert_and_judges_five_classes(tmp_path):
    snap = load_snapshot(make_scenario(tmp_path / "s", logs=False))
    detail = tiers.scenario_detail(snap)
    assert detail["fault_start"] == "2025-12-15T17:30:00Z"
    assert detail["entry_alertname"] == "RequestErrorRate"
    classes = {"metrics", "traces", "objects", "events", "logs"}
    assert set(detail["ends"]) == set(detail["coverage"]) == classes
    assert detail["coverage"]["traces"] == tiers.COVERED
    assert detail["ends"]["traces"] == "2025-12-15T17:40:00Z"
    assert detail["seconds_after_fault_start"]["traces"] == 600.0
    # The valueless metrics row an hour later does not count.
    assert detail["seconds_after_fault_start"]["metrics"] == 600.0
    assert detail["ends"]["logs"] is None
    assert detail["seconds_after_fault_start"]["logs"] is None
    assert detail["coverage"]["logs"] == tiers.NOT_COVERED
    assert detail["tier"] == tiers.TIER_TRACES


@pytest.mark.parametrize(
    ("after", "coverage"),
    [
        (timedelta(minutes=2), tiers.PARTIAL),
        (timedelta(minutes=-3), tiers.NOT_COVERED),
        (None, tiers.NOT_COVERED),
    ],
)
def test_traces_not_covered_go_to_the_no_trace_tier(tmp_path, after, coverage):
    snap = load_snapshot(make_scenario(tmp_path / "s", traces_after=after))
    detail = tiers.scenario_detail(snap)
    assert detail["coverage"]["traces"] == coverage
    assert detail["tier"] == tiers.TIER_NO_TRACES


def _full_root(root: Path) -> Path:
    for i, key in enumerate(sorted(SCENARIO_IDS, key=int)):
        if i % 3 == 0:
            make_scenario(root / f"S{key}" / "snapshot", traces_after=None)
            (root / f"S{key}" / "trial-1").mkdir()
        else:
            make_scenario(root / f"Scenario-{key}")
    (root / "notes.txt").write_text("not a scenario")
    (root / "extra").mkdir()
    return root


def test_cli_writes_files_coverage_accepts(tmp_path):
    root = _full_root(tmp_path / "copies")
    out, detail_path = tmp_path / "tiers.json", tmp_path / "tiers_detail.json"
    assert tiers.main([str(root), "--out", str(out), "--detail", str(detail_path)]) == 0
    written = json.loads(out.read_text())
    assert set(load_tiers(written)) == SCENARIO_IDS
    assert list(written) == [f"S{k}" for k in sorted(SCENARIO_IDS, key=int)]
    assert written["S1"] == tiers.TIER_NO_TRACES
    assert written["S2"] == tiers.TIER_TRACES
    detail = json.loads(detail_path.read_text())
    assert detail["S1"]["directory"] == "S1/snapshot"
    assert detail["S2"]["directory"] == "Scenario-2"
    assert set(detail) == set(written)


def test_missing_scenario_is_refused(tmp_path):
    root = _full_root(tmp_path / "copies")
    (root / "Scenario-2" / "otel_traces_raw.tsv").unlink()
    (root / "Scenario-2").rename(root / "spare")
    with pytest.raises(ValueError, match=r"missing \['2'\]"):
        tiers.build(root)


def test_answer_file_in_a_copy_is_refused(tmp_path):
    root = tmp_path / "copies"
    make_scenario(root / "S1")
    (root / "S1" / "ground_truth.yaml").write_text("")
    with pytest.raises(AnswerFilePresent):
        tiers.build(root, expected=frozenset({"1"}))


def test_symlinked_scenario_directory_is_refused(tmp_path):
    make_scenario(tmp_path / "real")
    root = tmp_path / "copies"
    root.mkdir()
    (root / "S1").symlink_to(tmp_path / "real")
    with pytest.raises(UnsafeSnapshot):
        tiers.build(root, expected=frozenset({"1"}))
