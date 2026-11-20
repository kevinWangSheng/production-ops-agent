"""#667 PR-A contract, group 8: whitelisted result reading (S-8).

Synthetic result CSVs carry answer-like columns (judge reasoning, checklist,
root cause, submission, problem ID) and an answer-bearing ``fault-spec.json``
beside them, each with a canary. Nothing outside the whitelist may reach the
returned ``AttemptOutcome``. Column spelling follows the task record; the real
column set is unconfirmed until #671.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import os
from pathlib import Path

import pytest

from tests.sregym_eval_support import code_of, mod

PID = "zz_synthetic_fault_problem"
CANARY = "zzcanaryrootcause"
WHITELIST_PHASES = (
    "deploy",
    "baseline",
    "inject_fault",
    "propagation",
    "stage:diagnosis",
    "evaluate:diagnosis",
    "cleanup",
    "fix_kubernetes",
    "undeploy_leftovers",
)


def _row(attempt=1, **over):
    # ASSUMPTION: Diagnosis.dimensions is a Python-literal list of dicts with
    # ``id``, ``name`` and ``score`` (unconfirmed until #671).
    dims = [
        {"id": "D1", "name": f"component {CANARY}", "score": 1.0, "reason": CANARY},
        {"id": "D2", "name": f"mechanism {CANARY}", "score": 0.5, "reason": CANARY},
        {"id": "D3", "name": f"evidence {CANARY}", "score": 0.0, "reason": CANARY},
    ]
    row = {
        "problem_id": f"{PID}_{CANARY}",
        "attempt": str(attempt),
        "Diagnosis.success": "True",
        "Diagnosis.accuracy": "0.8",
        "Diagnosis.composite_score": "0.75",
        "Diagnosis.dimensions": repr(dims),
        "Diagnosis.unscored_reason": "",
        "Diagnosis.oracles": "",
        "Diagnosis.reasoning": f"the judge thinks {CANARY}",
        "Diagnosis.checklist": f"[{CANARY}]",
        "Diagnosis.judgment": CANARY,
        "Diagnosis.submission": f"user submitted {CANARY}",
        "Diagnosis.root_cause": CANARY,
        "Diagnosis.judge_raw_response": CANARY,
        "reasoning": CANARY,
        "checklist": CANARY,
        "judgment": CANARY,
        "submission": CANARY,
        "root_cause": CANARY,
        "timed_out": "False",
        "deploy_failed": "False",
        "deploy_attempts": "1",
        "run_status": "complete",
        "incomplete_reason": "",
        "TTL": "312.5",
        "deployment_profile": CANARY,
        "phase.fault_spec_dump.duration_s": "9.0",
    }
    for i, name in enumerate(WHITELIST_PHASES):
        row[f"phase.{name}.duration_s"] = str(10.0 + i)
    row.update(over)
    return row


def _write_csv(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for r in rows for k in r})
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


def _attempt_dir(root: Path, stamp: str, agent="opspilot", pid=PID) -> Path:
    return root / stamp / agent / pid


def _csv_at(root: Path, stamp: str, rows=None, agent="opspilot", mtime=None) -> Path:
    d = _attempt_dir(root, stamp, agent)
    p = _write_csv(d / f"{PID}_{agent}_results.csv", rows or [_row()])
    if mtime is not None:
        os.utime(p, (mtime, mtime))
        os.utime(d, (mtime, mtime))
    return p


def _ledger(csv_path: Path, records, attempt=1) -> Path:
    p = csv_path.parent / f"phases_attempt{attempt}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


def _no_canary(outcome) -> None:
    blob = repr(outcome) + json.dumps(dataclasses.asdict(outcome), default=str)
    assert CANARY not in blob
    assert PID not in blob


# --- locating files -------------------------------------------------------------


def test_find_result_csv_picks_latest_and_skips_running(tmp_path):
    r = mod("results")
    _csv_at(tmp_path, "2026-11-19_09-00-00", mtime=1_700_000_000)
    newest = _csv_at(tmp_path, "2026-11-19_10-00-00", mtime=1_700_003_600)
    # A later directory with only a running file, and another agent's result.
    run_dir = _attempt_dir(tmp_path, "2026-11-19_11-00-00")
    _write_csv(run_dir / f"_running_{PID}_opspilot_results.csv", [_row()])
    _csv_at(tmp_path, "2026-11-19_12-00-00", agent="claudecode", mtime=1_700_010_000)
    found = r.find_result_csv(tmp_path, PID)
    assert found is not None and Path(found).resolve() == newest.resolve()


def test_find_result_csv_none_when_absent(tmp_path):
    r = mod("results")
    assert r.find_result_csv(tmp_path, PID) is None
    _csv_at(tmp_path, "2026-11-19_10-00-00", agent="claudecode")
    assert r.find_result_csv(tmp_path, PID) is None
    assert r.find_result_csv(tmp_path, "zz_other_problem", agent="claudecode") is None


def test_phases_path_is_sibling_per_attempt(tmp_path):
    r = mod("results")
    p = tmp_path / "x" / f"{PID}_opspilot_results.csv"
    assert r.phases_path(p) == p.parent / "phases_attempt1.jsonl"
    assert r.phases_path(p, 3) == p.parent / "phases_attempt3.jsonl"


# --- whitelist ---------------------------------------------------------------------


def test_read_attempt_whitelisted_values(tmp_path):
    r = mod("results")
    p = _csv_at(tmp_path, "t1")
    (p.parent / "fault-spec.json").write_text(json.dumps({"root_cause": CANARY}))
    _ledger(
        p,
        [
            {"phase": "deploy", "event": "start", "ts": "2026-11-19T10:00:00Z"},
            {"phase": "deploy", "event": "end", "ts": "2026-11-19T10:01:00Z"},
            {"phase": "inject_fault", "event": "start", "ts": "2026-11-19T10:02:00Z"},
            {
                "phase": "inject_fault",
                "event": "end",
                "ts": "2026-11-19T10:02:07Z",
                "detail": CANARY,
            },
            {"phase": "propagation", "event": "end", "ts": "2026-11-19T10:03:00Z"},
        ],
    )
    out = r.read_attempt(p)
    assert out.success is True
    assert out.accuracy == pytest.approx(0.8)
    assert out.composite_score == pytest.approx(0.75)
    assert out.dimensions == {"D1": 1.0, "D2": 0.5, "D3": 0.0}
    assert out.unscored_reason is None
    assert out.multi_cause is False
    assert out.timed_out is False
    assert out.deploy_failed is False
    assert out.deploy_attempts == 1
    assert out.run_status == "complete"
    assert out.incomplete_reason is None
    assert out.ttl_s == pytest.approx(312.5)
    assert out.phase_seconds == {
        n: pytest.approx(10.0 + i) for i, n in enumerate(WHITELIST_PHASES)
    }
    assert out.fault_injected_at == "2026-11-19T10:02:07Z"
    _no_canary(out)


def test_outcome_has_exactly_the_interface_fields():
    r = mod("results")
    assert {f.name for f in dataclasses.fields(r.AttemptOutcome)} == {
        "success",
        "accuracy",
        "composite_score",
        "dimensions",
        "unscored_reason",
        "multi_cause",
        "timed_out",
        "deploy_failed",
        "deploy_attempts",
        "run_status",
        "incomplete_reason",
        "ttl_s",
        "phase_seconds",
        "fault_injected_at",
    }


def test_non_whitelisted_phase_is_dropped(tmp_path):
    r = mod("results")
    out = r.read_attempt(_csv_at(tmp_path, "t1"))
    assert "fault_spec_dump" not in out.phase_seconds
    assert set(out.phase_seconds) <= set(WHITELIST_PHASES)


def test_multi_cause_oracles_only_presence(tmp_path):
    r = mod("results")
    oracles = repr(
        [{"name": "cause-a", "reason": CANARY}, {"name": "cause-b", "reason": CANARY}]
    )
    out = r.read_attempt(
        _csv_at(tmp_path, "t1", rows=[_row(**{"Diagnosis.oracles": oracles})])
    )
    assert out.multi_cause is True
    _no_canary(out)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("judge_parse_failed", "judge_parse_failed"),
        ("", None),
        (f"Judge said the root cause is {CANARY}", "other"),
        ("x" * 65, "other"),
        ("UPPER_CASE", "other"),
    ],
)
def test_unscored_and_incomplete_reasons_keep_short_codes_only(tmp_path, raw, want):
    r = mod("results")
    row = _row(
        **{
            "Diagnosis.success": "",
            "Diagnosis.unscored_reason": raw,
            "incomplete_reason": raw,
        }
    )
    out = r.read_attempt(_csv_at(tmp_path, "t1", rows=[row]))
    assert out.unscored_reason == want
    assert out.incomplete_reason == want
    _no_canary(out)


def test_unscored_row_has_none_success(tmp_path):
    r = mod("results")
    row = _row(
        **{
            "Diagnosis.success": "",
            "Diagnosis.accuracy": "",
            "Diagnosis.composite_score": "",
            "Diagnosis.dimensions": "",
            "Diagnosis.unscored_reason": "judge_retries_exhausted",
        }
    )
    out = r.read_attempt(_csv_at(tmp_path, "t1", rows=[row]))
    assert out.success is None
    assert out.accuracy is None
    assert out.composite_score is None
    assert out.dimensions == {}
    assert out.unscored_reason == "judge_retries_exhausted"


def test_timed_out_and_deploy_failed_rows(tmp_path):
    r = mod("results")
    rows = [
        _row(1, timed_out="True", **{"Diagnosis.success": "False"}),
        {
            "problem_id": PID,
            "attempt": "2",
            "deploy_failed": "True",
            "deploy_attempts": "3",
        },
    ]
    p = _csv_at(tmp_path, "t1", rows=rows)
    first = r.read_attempt(p, 1)
    assert first.timed_out is True and first.success is False
    second = r.read_attempt(p, 2)
    assert second.deploy_failed is True
    assert second.deploy_attempts == 3
    assert second.success is None
    assert second.fault_injected_at is None


def test_read_attempt_picks_row_by_attempt_column(tmp_path):
    r = mod("results")
    rows = [
        _row(2, **{"Diagnosis.accuracy": "0.2"}),
        _row(1, **{"Diagnosis.accuracy": "0.9"}),
    ]
    p = _csv_at(tmp_path, "t1", rows=rows)
    assert r.read_attempt(p, 1).accuracy == pytest.approx(0.9)
    assert r.read_attempt(p, 2).accuracy == pytest.approx(0.2)


def test_missing_file_or_row_is_result_missing(tmp_path):
    r = mod("results")
    with pytest.raises(r.ResultError) as ei:
        r.read_attempt(tmp_path / "nope.csv")
    assert code_of(ei.value) == "RESULT_MISSING"
    p = _csv_at(tmp_path, "t1")
    with pytest.raises(r.ResultError) as ei:
        r.read_attempt(p, 2)
    assert code_of(ei.value) == "RESULT_MISSING"


def test_fault_injected_at_uses_attempt_ledger_and_end_event(tmp_path):
    r = mod("results")
    p = _csv_at(tmp_path, "t1", rows=[_row(1), _row(2)])
    _ledger(p, [{"phase": "inject_fault", "event": "end", "ts": "A1"}], attempt=1)
    _ledger(
        p,
        [
            {"phase": "inject_fault", "event": "start", "ts": "B0"},
            {"phase": "stage:diagnosis", "event": "end", "ts": "B9"},
            {"phase": "inject_fault", "event": "end", "ts": "B1"},
        ],
        attempt=2,
    )
    assert r.read_attempt(p, 1).fault_injected_at == "A1"
    assert r.read_attempt(p, 2).fault_injected_at == "B1"


def test_fault_injected_at_none_without_ledger_or_end(tmp_path):
    r = mod("results")
    p = _csv_at(tmp_path, "t1")
    assert r.read_attempt(p).fault_injected_at is None
    _ledger(p, [{"phase": "inject_fault", "event": "start", "ts": "X"}])
    assert r.read_attempt(p).fault_injected_at is None


def test_read_attempt_prints_nothing_answer_like(tmp_path, capsys, caplog):
    r = mod("results")
    caplog.set_level("DEBUG")
    p = _csv_at(tmp_path, "t1", rows=[_row(**{"Diagnosis.oracles": CANARY})])
    r.read_attempt(p)
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err
    assert CANARY not in caplog.text
