"""Q8 (user ruling 2026-11-06): the deterministic gate of an attempt whose
scenario expects no Run is that attempt's own expectations.

Every other case keeps the existing gate, and a gate computed without
expectations (a stored per-attempt summary) is unchanged.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from opspilot.evaluation.expectations import ExpectationResult, check_expectations
from opspilot.evaluation.gate import (
    GATE_CHECKS,
    expects_no_run,
    no_run_gate,
    run_gate,
)
from scripts import eval_dev_round
from tests.unit.test_432_impl import (
    DEV_V2,
    NOISE_POS,
    QUESTION_INTAKE,
    WT_POS,
    _dims,
    _loaded,
    _projection,
)

LEGACY_NO_RUN_REASONS = (
    "EXECUTION_NOT_COMPLETED",
    "REPORT_INVALID",
    "PERMISSION_VIOLATION",
)

NO_RUN_PROJECTION = _projection(
    counts={"incidents": 1, "runs": 0, "episodes": 1},
    runs=[],
    outcome=None,
    alert={
        "handoff": True,
        "handoff_reason": "TARGET_UNRESOLVED",
        "run_ids": [],
        "lifecycle": "open",
    },
    tool_results=[],
    report=None,
)
NO_RUN_INTAKE = {
    "entry": "alertmanager",
    "deliveries": [
        {"seq": 1, "outcome": "handoff_created", "reason": "TARGET_UNRESOLVED"}
    ],
    "counts": {"incidents": 1, "runs": 0, "episodes": 1},
}


def _expect(sid: str, projection: Any, intake: Any) -> ExpectationResult:
    _spec, answer = _loaded()[sid]
    return check_expectations(
        answer,
        attempt_status="ran",
        attempt_reason=None,
        projection=projection,
        intake=intake,
    )


def _gate(sid: str, expectations: ExpectationResult | None = None, **kw: Any):
    spec, answer = _loaded()[sid]
    if expectations is None:
        return run_gate(spec, answer, None, None, {}, {}, **kw)
    return run_gate(spec, answer, None, None, {}, {}, expectations=expectations)


# -- which scenarios expect no Run --------------------------------------------------


def test_only_a_no_run_only_answer_expects_no_run():
    loaded = _loaded()
    assert [sid for sid, (_s, a) in loaded.items() if expects_no_run(a)] == [WT_POS]
    _spec, answer = loaded[WT_POS]
    either = replace(
        answer,
        expected_outcome=replace(
            answer.expected_outcome, final_states=("no_run", "completed")
        ),
    )
    assert not expects_no_run(either)


# -- run_gate ---------------------------------------------------------------------


def test_no_run_attempt_with_passing_expectations_passes_the_gate():
    expectations = _expect(WT_POS, NO_RUN_PROJECTION, NO_RUN_INTAKE)
    assert expectations.passed
    gate = _gate(WT_POS, expectations)
    assert gate.passed is True
    assert gate.reasons == ()
    assert dict(gate.checks) == dict.fromkeys(GATE_CHECKS, True)


def test_no_run_attempt_with_failing_expectations_fails_the_gate():
    bad_intake = {**NO_RUN_INTAKE, "counts": {"incidents": 1, "runs": 1, "episodes": 1}}
    expectations = _expect(WT_POS, NO_RUN_PROJECTION, bad_intake)
    assert not expectations.passed
    gate = _gate(WT_POS, expectations)
    assert gate.passed is False
    assert gate.reasons[0] == "EXPECTATIONS_FAILED"
    assert "COUNTS_UNEXPECTED" in gate.reasons
    assert dict(gate.checks) == dict.fromkeys(GATE_CHECKS, False)


def test_no_run_attempt_without_a_projection_fails_the_gate():
    expectations = _expect(WT_POS, None, NO_RUN_INTAKE)
    gate = _gate(WT_POS, expectations)
    assert gate.passed is False
    assert "PROJECTION_MISSING" in gate.reasons


def test_missing_expectations_fail_the_no_run_gate():
    gate = no_run_gate(None)
    assert gate.passed is False
    assert gate.reasons == ("EXPECTATIONS_MISSING",)


def test_a_setup_failure_fails_the_no_run_gate():
    _spec, answer = _loaded()[WT_POS]
    expectations = check_expectations(
        answer,
        attempt_status="setup_failed",
        attempt_reason="NOISE_NOT_OBSERVED",
        projection=None,
        intake=None,
    )
    assert _gate(WT_POS, expectations).passed is False


def test_without_expectations_the_no_run_gate_is_unchanged():
    """A stored per-attempt summary is written before its expectations exist
    and must still re-derive to the same result."""
    gate = _gate(WT_POS)
    assert gate.passed is False
    assert gate.reasons == LEGACY_NO_RUN_REASONS


def test_an_ordinary_scenario_ignores_expectations():
    passing = _expect(NOISE_POS, _projection(), QUESTION_INTAKE)
    assert passing.passed
    assert _gate(NOISE_POS, passing) == _gate(NOISE_POS)
    assert _gate(NOISE_POS).passed is False


# -- development round summary ------------------------------------------------------


def _write_round(base: Path, name: str, attempts: list[tuple[str, Any, Any]]) -> Path:
    round_dir = base / name
    slots = []
    for index, (sid, projection, intake) in enumerate(attempts):
        attempt_id = f"{name}-{index}"
        run_dir = round_dir / "runs" / attempt_id
        run_dir.mkdir(parents=True)
        stored_gate = _gate(sid)
        summary = {
            "capture": {
                "attempt": {"status": "ran"},
                "started_at": f"2026-11-02T0{index}:00:00+00:00",
                "manifest": {"dimensions": _dims()},
                "cost": {"delta_cny": "0.10"},
            },
            "evaluation": {
                "gate": {
                    "passed": stored_gate.passed,
                    "checks": dict(stored_gate.checks),
                    "reasons": list(stored_gate.reasons),
                }
            },
            "trace": {"entries": []},
        }
        (run_dir / "summary.json").write_text(json.dumps(summary))
        (run_dir / "projection.json").write_text(json.dumps(projection))
        (run_dir / "intake.json").write_text(json.dumps(intake))
        checked = _expect(sid, projection, intake)
        (run_dir / "expectations.json").write_text(json.dumps(checked.as_json()))
        slots.append(
            {
                "scenario_id": sid,
                "repetition": 1,
                "attempt_id": attempt_id,
                "status": "ran",
                "reason": None,
            }
        )
    (round_dir / "lifecycle.json").write_text(
        json.dumps(
            {
                "round": name,
                "manifest_hash": "h",
                "round_status": "finished",
                "reason": None,
                "slots": slots,
            }
        )
    )
    return round_dir


def _slots(doc: dict[str, Any], sid: str) -> list[dict[str, Any]]:
    return [s for s in doc["slots"] if s["attempt_id"] and s["scenario_id"] == sid]


def test_dev_round_gates_a_no_run_slot_by_its_expectations(tmp_path):
    alert_round = _write_round(
        tmp_path, "dv2-q8aaaa", [(WT_POS, NO_RUN_PROJECTION, NO_RUN_INTAKE)]
    )
    question_round = _write_round(
        tmp_path, "dv2-q8dddd", [(NOISE_POS, _projection(), QUESTION_INTAKE)]
    )
    summary_before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    doc = eval_dev_round.aggregate([alert_round, question_round], DEV_V2)
    (wt,) = _slots(doc, WT_POS)
    assert wt["expectations"]["passed"] is True
    assert wt["gate"] == {"passed": True, "reasons": []}
    assert wt["gate_recorded"] == {
        "passed": False,
        "reasons": list(LEGACY_NO_RUN_REASONS),
    }
    # an ordinary slot keeps its stored gate
    (noise,) = _slots(doc, NOISE_POS)
    assert noise["gate"] == {"passed": False, "reasons": list(LEGACY_NO_RUN_REASONS)}
    assert noise["gate_recorded"] is None
    assert doc["totals"]["capability"]["gate_passed"] == 1
    # stored evidence is read, never rewritten
    assert {p: p.read_bytes() for p in summary_before} == summary_before
    assert set(tmp_path.rglob("*.json")) == set(summary_before)


def test_dev_round_fails_a_no_run_slot_whose_expectations_fail(tmp_path):
    bad_intake = {**NO_RUN_INTAKE, "counts": {"incidents": 1, "runs": 1, "episodes": 1}}
    round_dir = _write_round(
        tmp_path, "dv2-q8bbbb", [(WT_POS, NO_RUN_PROJECTION, bad_intake)]
    )
    doc = eval_dev_round.aggregate([round_dir], DEV_V2)
    (wt,) = _slots(doc, WT_POS)
    assert wt["expectations"]["passed"] is False
    assert wt["gate"]["passed"] is False
    assert wt["gate"]["reasons"][0] == "EXPECTATIONS_FAILED"
    assert doc["totals"]["capability"]["gate_passed"] == 0


def test_not_started_slots_carry_no_recorded_gate(tmp_path):
    round_dir = _write_round(
        tmp_path, "dv2-q8cccc", [(WT_POS, NO_RUN_PROJECTION, NO_RUN_INTAKE)]
    )
    doc = eval_dev_round.aggregate([round_dir], DEV_V2)
    empty = [s for s in doc["slots"] if s["attempt_id"] is None]
    assert empty and all(s["gate_recorded"] is None for s in empty)
