"""Acceptance tests for the registered-rerun extension in seams-432 §5a."""

# ruff: noqa: F811 - pytest fixture imported from test_432_dev_round
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.acceptance.test_432_dev_round import (
    CAPABILITY,
    NOISE_NEGATIVE,
    aggregate,
    lab,  # noqa: F401 - fixture
    refusal,
    run_round,
)


def _replace(value, old: str, new: str):
    if isinstance(value, dict):
        return {k: _replace(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace(v, old, new) for v in value]
    return new if value == old else value


def _rewrite_json(path: Path, old: str, new: str) -> dict:
    data = json.loads(path.read_text())
    data = _replace(data, old, new)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


def _clone_round(
    source: Path,
    destination: Path,
    *,
    status: str | None = None,
    product_run_id=None,
    timing_role: str | None = None,
):
    shutil.copytree(source, destination)
    lifecycle = json.loads((destination / "lifecycle.json").read_text())
    old_id = lifecycle["slots"][0]["attempt_id"]
    new_id = str(uuid4())
    (destination / "runs" / old_id).rename(destination / "runs" / new_id)
    for path in (
        destination / "lifecycle.json",
        destination / "runs" / new_id / "attempt.json",
        destination / "runs" / new_id / "summary.json",
        destination / "runs" / new_id / "cost.json",
    ):
        if path.exists():
            _rewrite_json(path, old_id, new_id)
    lifecycle = json.loads((destination / "lifecycle.json").read_text())
    slot = lifecycle["slots"][0]
    lifecycle["round"] = destination.name
    if status is not None:
        slot["status"] = status
    if product_run_id is not None or status == "setup_failed":
        slot["product_run_id"] = product_run_id
    if timing_role == "voided":
        slot["timings"]["started_at"] = "2026-11-03T10:00:00+00:00"
        slot["timings"]["ended_at"] = "2026-11-03T10:05:00+00:00"
    elif timing_role == "rerun":
        slot["timings"]["started_at"] = "2026-11-03T10:10:00+00:00"
        slot["timings"]["ended_at"] = "2026-11-03T10:15:00+00:00"
    elif slot["status"] == "setup_failed":
        slot["stage"] = "setup"
        slot["reason"] = "NOISE_NOT_OBSERVED"
        slot["timings"]["started_at"] = "2026-11-03T10:00:00+00:00"
        slot["timings"]["ended_at"] = "2026-11-03T10:05:00+00:00"
    else:
        slot["timings"]["started_at"] = "2026-11-03T10:10:00+00:00"
        slot["timings"]["ended_at"] = "2026-11-03T10:15:00+00:00"
    (destination / "lifecycle.json").write_text(json.dumps(lifecycle, indent=2) + "\n")
    summary_path = next((destination / "runs").glob("*/summary.json"))
    summary = json.loads(summary_path.read_text())
    if status is not None:
        summary["capture"]["attempt"]["status"] = status
    if product_run_id is not None or status == "setup_failed":
        summary["capture"]["attempt"]["product_run_id"] = product_run_id
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    return destination, new_id


def _pair(lab, tmp_path: Path):
    source = run_round(lab, "rerun-source", [NOISE_NEGATIVE], 1)
    voided_dir, voided = _clone_round(
        source, tmp_path / "voided", status="setup_failed", timing_role="voided"
    )
    rerun_dir, rerun = _clone_round(source, tmp_path / "rerun", timing_role="rerun")
    return [voided_dir, rerun_dir], voided_dir, rerun_dir, voided, rerun


def _registration(voided: str, rerun: str, **extra):
    item = {
        "voided_attempt_id": voided,
        "rerun_attempt_id": rerun,
        "note": "registered",
    }
    item.update(extra)
    return {"schema": "opspilot-eval-dev-reruns-1", "reruns": [item]}


def test_without_reruns_keeps_third_started_attempt_as_redraw(lab, tmp_path):
    source = run_round(lab, "rerun-no-registration", [NOISE_NEGATIVE], 1)
    dirs = [source]
    for i in range(2):
        clone = tmp_path / f"extra-{i}"
        _clone_round(source, clone)
        dirs.append(clone)
    assert "REDRAW_DETECTED" in refusal(lambda: aggregate(dirs, lab["root"]))


def test_valid_registration_voids_slot_keeps_cost_and_marks_rerun(lab, tmp_path):
    dirs, voided_dir, rerun_dir, voided, rerun = _pair(lab, tmp_path)
    summary = aggregate(dirs, lab["root"], reruns=_registration(voided, rerun))
    slots = [s for s in summary["slots"] if s["scenario_id"] == NOISE_NEGATIVE]
    assert len(slots) == 2
    assert all(s["attempt_id"] != voided for s in slots)
    rerun_slot = next(s for s in slots if s["attempt_id"] == rerun)
    assert rerun_slot["rerun_of"] == voided
    assert summary["voided_attempts"] == [
        {
            "round": "voided",
            "attempt_id": voided,
            "scenario_id": NOISE_NEGATIVE,
            "status": "setup_failed",
            "reason": "NOISE_NOT_OBSERVED",
            "rerun_attempt_id": rerun,
            "note": "environment not ready, rerun per rule",
        }
    ]
    assert summary["totals"]["capability"]["started"] == 1
    assert summary["f1_step2"]["achieved"] is False
    assert summary["cost"]["attempts_measured"] >= 2


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("unknown", "RERUN_UNKNOWN"),
        ("ran", "RERUN_NOT_ELIGIBLE"),
        ("setup_with_run", "RERUN_NOT_ELIGIBLE"),
        ("mismatch", "RERUN_SCENARIO_MISMATCH"),
        ("order", "RERUN_ORDER"),
        ("voided_twice", "RERUN_CHAINED"),
        ("rerun_twice", "RERUN_CHAINED"),
        ("rerun_is_voided", "RERUN_CHAINED"),
        ("invalid", "RERUNS_INVALID"),
    ],
)
def test_registered_rerun_refusal_codes(case, expected, lab, tmp_path):
    dirs, voided_dir, rerun_dir, voided, rerun = _pair(lab, tmp_path)
    if case == "unknown":
        registrations = _registration("missing", rerun)
    elif case == "ran":
        lifecycle = json.loads((rerun_dir / "lifecycle.json").read_text())
        lifecycle["slots"][0]["status"] = "ran"
        lifecycle["slots"][0]["product_run_id"] = "existing-run"
        lifecycle["slots"][0]["timings"]["started_at"] = "2026-11-03T10:00:00+00:00"
        lifecycle["slots"][0]["timings"]["ended_at"] = "2026-11-03T10:05:00+00:00"
        (rerun_dir / "lifecycle.json").write_text(json.dumps(lifecycle))
        voided_lifecycle = json.loads((voided_dir / "lifecycle.json").read_text())
        voided_lifecycle["slots"][0]["timings"]["started_at"] = (
            "2026-11-03T10:10:00+00:00"
        )
        voided_lifecycle["slots"][0]["timings"]["ended_at"] = (
            "2026-11-03T10:15:00+00:00"
        )
        (voided_dir / "lifecycle.json").write_text(json.dumps(voided_lifecycle))
        summary_path = next((rerun_dir / "runs").glob("*/summary.json"))
        summary = json.loads(summary_path.read_text())
        summary["capture"]["attempt"]["product_run_id"] = "existing-run"
        summary_path.write_text(json.dumps(summary))
        registrations = _registration(rerun, voided)
    elif case == "setup_with_run":
        lifecycle = json.loads((voided_dir / "lifecycle.json").read_text())
        lifecycle["slots"][0]["product_run_id"] = "existing-run"
        (voided_dir / "lifecycle.json").write_text(json.dumps(lifecycle))
        summary_path = next((voided_dir / "runs").glob("*/summary.json"))
        summary = json.loads(summary_path.read_text())
        summary["capture"]["attempt"]["product_run_id"] = "existing-run"
        summary_path.write_text(json.dumps(summary))
        registrations = _registration(voided, rerun)
    elif case == "mismatch":
        lifecycle = json.loads((rerun_dir / "lifecycle.json").read_text())
        lifecycle["slots"][0]["scenario_id"] = CAPABILITY[1]
        (rerun_dir / "lifecycle.json").write_text(json.dumps(lifecycle))
        summary_path = next((rerun_dir / "runs").glob("*/summary.json"))
        summary = json.loads(summary_path.read_text())
        summary["capture"]["attempt"]["scenario_id"] = CAPABILITY[1]
        summary["capture"]["answer"]["scenario_id"] = CAPABILITY[1]
        summary_path.write_text(json.dumps(summary))
        attempt_path = next((rerun_dir / "runs").glob("*/attempt.json"))
        attempt = json.loads(attempt_path.read_text())
        if isinstance(attempt.get("capture"), dict):
            attempt["capture"].setdefault("attempt", {})["scenario_id"] = CAPABILITY[1]
        attempt_path.write_text(json.dumps(attempt))
        registrations = _registration(voided, rerun)
    elif case == "order":
        lifecycle = json.loads((rerun_dir / "lifecycle.json").read_text())
        lifecycle["slots"][0]["timings"]["started_at"] = "2026-11-03T09:00:00+00:00"
        (rerun_dir / "lifecycle.json").write_text(json.dumps(lifecycle))
        registrations = _registration(voided, rerun)
    elif case == "voided_twice":
        registrations = {
            "schema": "opspilot-eval-dev-reruns-1",
            "reruns": [
                _registration(voided, rerun)["reruns"][0],
                _registration(voided, rerun)["reruns"][0],
            ],
        }
    elif case == "rerun_twice":
        registrations = {
            "schema": "opspilot-eval-dev-reruns-1",
            "reruns": [
                _registration(voided, rerun)["reruns"][0],
                _registration(voided, rerun)["reruns"][0],
            ],
        }
    elif case == "rerun_is_voided":
        registrations = {
            "schema": "opspilot-eval-dev-reruns-1",
            "reruns": [
                _registration(voided, rerun)["reruns"][0],
                _registration(rerun, voided)["reruns"][0],
            ],
        }
    else:
        registrations = {"schema": "wrong", "reruns": [{}]}
    assert expected in refusal(
        lambda: aggregate(dirs, lab["root"], reruns=registrations)
    )


def test_unregistered_extra_attempt_still_redraws_with_valid_registration(
    lab, tmp_path
):
    dirs, voided_dir, rerun_dir, voided, rerun = _pair(lab, tmp_path)
    extra_dirs = []
    for i in range(2):
        extra_dir, _ = _clone_round(rerun_dir, tmp_path / f"extra-{i}")
        extra_dirs.append(extra_dir)
    assert "REDRAW_DETECTED" in refusal(
        lambda: aggregate(
            dirs + extra_dirs, lab["root"], reruns=_registration(voided, rerun)
        )
    )


def test_cli_reruns_refusal_is_json_and_success_writes_summary(lab, tmp_path):
    dirs, voided_dir, rerun_dir, voided, rerun = _pair(lab, tmp_path)
    reruns = tmp_path / "reruns.json"
    reruns.write_text(json.dumps(_registration("unknown", rerun)))
    bad = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.eval_dev_round",
            "--dataset",
            str(lab["root"]),
            "--round",
            str(dirs[0]),
            "--round",
            str(dirs[1]),
            "--reruns",
            str(reruns),
            "--out",
            str(tmp_path / "bad.json"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["reason"].startswith("RERUN_UNKNOWN:")
    reruns.write_text(json.dumps(_registration(voided, rerun)))
    out = tmp_path / "summary.json"
    good = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.eval_dev_round",
            "--dataset",
            str(lab["root"]),
            "--round",
            str(dirs[0]),
            "--round",
            str(dirs[1]),
            "--reruns",
            str(reruns),
            "--out",
            str(out),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert good.returncode == 0
    assert out.exists()
    assert json.loads(out.read_text())["voided_attempts"]
