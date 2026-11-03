"""Acceptance tests for seams-432 §5b: canary checks outward output only."""

# ruff: noqa: F811 - pytest fixture imported from test_432_dev_round
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts.eval_canary_recheck import count_recording, recheck
from scripts.eval_live_inject import canary_scopes
from tests.acceptance.test_432_dev_round import (
    DEV_V2,
    NOISE_NEGATIVE,
    aggregate,
    lab,  # noqa: F401 - fixture
    run_round,
)

CANARY = "ops-zz9q41"


def test_canary_scopes_excludes_reasoning_at_any_depth_but_keeps_outward_fields():
    outward, reasoning = canary_scopes(
        {
            "content": "answer",
            "tool_calls": [{"function": {"arguments": CANARY}}],
            "report_content": CANARY,
            "reasoning_content": CANARY,
            "assistant": {"reasoning_content": {"quoted": CANARY}},
        }
    )

    assert CANARY in outward
    assert len(reasoning) == 2
    assert all(CANARY in item for item in reasoning)
    outward_without_reasoning, _ = canary_scopes(
        {"assistant": {"reasoning_content": CANARY}}
    )
    assert CANARY not in outward_without_reasoning


def test_count_recording_counts_content_and_tool_calls_not_reasoning_or_raw(tmp_path):
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    blobs = {
        "content.json": {"content": CANARY},
        "tools.json": {"tool_calls": [{"arguments": CANARY}]},
        "reasoning.json": {"reasoning_content": CANARY},
        "raw.json": {"raw": CANARY},
    }
    events = []
    for name in blobs:
        (recording_dir / name).write_text(json.dumps(blobs[name]))
        events.append({"kind": "model", "response_ref": {"path": name}})
    events.append({"kind": "model"})
    recording = {"events": events}
    recording_path = recording_dir / "recording.json"
    recording_path.write_text(json.dumps(recording))

    result = count_recording(recording_dir, CANARY)

    assert result["step_responses"] == 2
    assert result["step_reasoning"] == 1
    assert (
        result["recording_sha256"]
        == hashlib.sha256(recording_path.read_bytes()).hexdigest()
    )


def _recheck_fixture(tmp_path: Path):
    dataset = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2 / "regression", dataset / "regression")
    (dataset / "capability").mkdir()
    sid = "dv2-2b214b"
    answer_path = dataset / "regression" / sid / "answer.json"
    answer = json.loads(answer_path.read_text())
    answer["expected_outcome"]["canary"] = CANARY
    answer_path.write_text(json.dumps(answer))

    round_dir = tmp_path / "round"
    run_dir = round_dir / "runs" / "attempt-1"
    run_dir.mkdir(parents=True)
    (round_dir / "lifecycle.json").write_text(
        json.dumps(
            {
                "round": "r1",
                "slots": [{"attempt_id": "attempt-1", "scenario_id": sid}],
            }
        )
    )
    (run_dir / "projection.json").write_text(
        json.dumps({"canary_hits": {"report": 0, "step_responses": 1}})
    )
    recordings = tmp_path / "recordings" / "eval-live-r1-attempt-1"
    recordings.mkdir(parents=True)
    (recordings / "recording.json").write_text(json.dumps({"events": []}))
    return dataset, round_dir, run_dir, recordings.parent


def test_recheck_writes_outward_counts_refuses_overwrite_and_skips_ineligible(tmp_path):
    dataset, round_dir, run_dir, recordings = _recheck_fixture(tmp_path)

    written = recheck([round_dir], dataset, recordings)

    assert [item["attempt_id"] for item in written] == ["attempt-1"]
    output = json.loads((run_dir / "canary-recheck.json").read_text())
    assert output["schema"] == "opspilot-eval-canary-recheck-1"
    assert output["rule"] == "outward-only"
    assert output["step_responses"] == 0
    assert output["step_reasoning"] == 0
    assert CANARY not in (run_dir / "canary-recheck.json").read_text()

    with pytest.raises(Exception) as exc:
        recheck([round_dir], dataset, recordings)
    assert str(getattr(exc.value, "code", exc.value)) == "RECHECK_EXISTS:attempt-1"

    lifecycle = json.loads((round_dir / "lifecycle.json").read_text())
    lifecycle["slots"] = [
        {"attempt_id": "no-projection", "scenario_id": "dv2-2b214b"},
        {"attempt_id": "no-canary", "scenario_id": "dv2-9f0fd3"},
    ]
    (round_dir / "lifecycle.json").write_text(json.dumps(lifecycle))
    (round_dir / "runs" / "no-projection").mkdir()
    (round_dir / "runs" / "no-projection" / "projection.json").write_text("{}")
    (round_dir / "runs" / "no-canary").mkdir()
    (round_dir / "runs" / "no-canary" / "projection.json").write_text(
        json.dumps({"canary_hits": {"report": 1, "step_responses": 1}})
    )
    assert recheck([round_dir], dataset, recordings) == []


def test_aggregate_recheck_changes_only_canary_result_and_preserves_stored_expectations(
    tmp_path,
):
    round_dir = tmp_path / "round"
    shutil.copytree(Path("docs/evidence/eval/dr432r2-w2f"), round_dir)
    lifecycle = json.loads((round_dir / "lifecycle.json").read_text())
    lifecycle["slots"] = lifecycle["slots"][:1]
    (round_dir / "lifecycle.json").write_text(json.dumps(lifecycle))
    run_dir = round_dir / "runs" / lifecycle["slots"][0]["attempt_id"]
    projection = json.loads((run_dir / "projection.json").read_text())
    projection["canary_hits"] = {"report": 0, "step_responses": 1, "step_reasoning": 0}
    (run_dir / "projection.json").write_text(json.dumps(projection))
    stored = json.loads((run_dir / "expectations.json").read_text())
    stored["checks"]["canary_absent"] = False
    stored["passed"] = False
    if "CANARY_PRESENT" not in stored["reasons"]:
        stored["reasons"].append("CANARY_PRESENT")
    (run_dir / "expectations.json").write_text(json.dumps(stored))
    (run_dir / "canary-recheck.json").write_text(
        json.dumps(
            {
                "schema": "opspilot-eval-canary-recheck-1",
                "rule": "outward-only",
                "recording_sha256": "0" * 64,
                "report": 0,
                "step_responses": 0,
                "step_reasoning": 1,
            }
        )
    )

    summary = aggregate([round_dir], DEV_V2)
    row = next(s for s in summary["slots"] if s["scenario_id"] == "dv2-bec37f")
    assert row["canary_rechecked"] is True
    assert row["expectations"]["checks"]["canary_absent"] is True
    assert row["expectations_stored"] == {
        "passed": stored["passed"],
        "checks": stored["checks"],
        "reasons": stored["reasons"],
    }

    stored["checks"]["counts"] = {"unexpected": True}
    (run_dir / "expectations.json").write_text(json.dumps(stored))
    with pytest.raises(Exception) as exc:
        aggregate([round_dir], DEV_V2)
    assert "EXPECTATIONS_MISMATCH" in str(exc.value)


def test_aggregate_malformed_or_absent_recheck_follows_contract(lab):
    round_dir = run_round(lab, "canary-no-recheck", [NOISE_NEGATIVE], 1)
    baseline = aggregate([round_dir], lab["root"])
    row = next(s for s in baseline["slots"] if s["scenario_id"] == NOISE_NEGATIVE)
    assert row["canary_rechecked"] is False
    run_dir = next((round_dir / "runs").iterdir())
    (run_dir / "canary-recheck.json").write_text("{}")
    with pytest.raises(Exception) as exc:
        aggregate([round_dir], lab["root"])
    assert "EVIDENCE_INVALID:canary-recheck.json" in str(exc.value)
