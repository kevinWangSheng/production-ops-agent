"""#581: the ITBench-Lite whitelist loader refuses answer files and reads only
metrics, alerts and the four raw TSVs. Synthetic directories only."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.itbench_lite.snapshot import (
    AnswerFilePresent,
    NotWhitelisted,
    load_snapshot,
)

TSVS = (
    "otel_traces_raw.tsv",
    "k8s_objects_raw.tsv",
    "k8s_events_raw.tsv",
    "otel_logs_raw.tsv",
)


def _scenario(root: Path) -> Path:
    (root / "metrics").mkdir(parents=True)
    (root / "metrics" / "pod_a.tsv").write_text("timestamp\tvalue\n")
    (root / "metrics" / "notes.txt").write_text("not a metrics file\n")
    (root / "alerts").mkdir()
    (root / "alerts" / "alerts_at_2025-01-01T00-00-00.json").write_text("[]")
    (root / "alerts_in_alerting_state_2025-01-01T000100Z.json").write_text("[]")
    for name in TSVS:
        (root / name).write_text("Timestamp\tBody\n")
    (root / "README.md").write_text("other file\n")
    return root


def test_whitelist_is_metrics_alerts_and_four_tsvs(tmp_path):
    snap = load_snapshot(_scenario(tmp_path / "s"))
    assert [p.name for p in snap.metrics] == ["pod_a.tsv"]
    assert [p.name for p in snap.alerts] == [
        "alerts_at_2025-01-01T00-00-00.json",
        "alerts_in_alerting_state_2025-01-01T000100Z.json",
    ]
    assert sorted(snap.tsv) == ["events", "logs", "objects", "traces"]


def test_missing_tsvs_are_absent_not_errors(tmp_path):
    root = tmp_path / "s"
    (root / "metrics").mkdir(parents=True)
    snap = load_snapshot(root)
    assert snap.tsv_path("traces") is None and snap.alerts == ()


@pytest.mark.parametrize(
    "relative",
    [
        "ground_truth.yaml",
        "assertion.json",
        "status.json",
        "metrics/ground_truth.yaml",
        "alerts/Status.JSON",
        "nested/deeper/GROUND_TRUTH.yaml",
    ],
)
def test_answer_file_anywhere_refuses_start_without_opening_it(tmp_path, relative):
    root = _scenario(tmp_path / "s")
    answer = root / relative
    answer.parent.mkdir(parents=True, exist_ok=True)
    answer.write_text("x")
    answer.chmod(0)  # an attempt to open it would raise PermissionError instead
    try:
        with pytest.raises(AnswerFilePresent):
            load_snapshot(root)
    finally:
        answer.chmod(0o600)


def test_answer_name_as_directory_also_refuses(tmp_path):
    root = _scenario(tmp_path / "s")
    (root / "status.json").mkdir()
    with pytest.raises(AnswerFilePresent):
        load_snapshot(root)


def test_whitelisted_name_linked_to_an_answer_file_refuses(tmp_path):
    root = _scenario(tmp_path / "s")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "ground_truth.yaml").write_text("x")
    (root / "otel_logs_raw.tsv").unlink()
    os.symlink(outside / "ground_truth.yaml", root / "otel_logs_raw.tsv")
    with pytest.raises(AnswerFilePresent):
        load_snapshot(root)


def test_readers_refuse_paths_outside_the_whitelist(tmp_path):
    root = _scenario(tmp_path / "s")
    snap = load_snapshot(root)
    with pytest.raises(NotWhitelisted):
        list(snap.iter_tsv(root / "README.md"))
    with pytest.raises(NotWhitelisted):
        list(snap.iter_tsv(root / "metrics" / "notes.txt"))
    with pytest.raises(NotWhitelisted):
        snap.read_alert_file(root / "README.md")


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "success", "data": {"alerts": [{"labels": {"a": "1"}}]}},
        {"alerts": [{"labels": {"a": "1"}}]},
        [{"labels": {"a": "1"}}, "junk"],
    ],
)
def test_alert_file_shapes(tmp_path, payload):
    root = tmp_path / "s"
    (root / "alerts").mkdir(parents=True)
    path = root / "alerts" / "alerts_in_alerting_state_x.json"
    path.write_text(json.dumps(payload))
    snap = load_snapshot(root)
    assert snap.read_alert_file(path) == [{"labels": {"a": "1"}}]


def test_tsv_cells_keep_quoted_tabs_and_newlines(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    (root / "otel_logs_raw.tsv").write_text(
        'Timestamp\tBody\n2025-01-01 00:00:00.5\t"line one\nline\ttwo ""q"""\n'
    )
    snap = load_snapshot(root)
    rows = list(snap.iter_tsv(snap.tsv_path("logs")))
    assert rows == [
        {"Timestamp": "2025-01-01 00:00:00.5", "Body": 'line one\nline\ttwo "q"'}
    ]
