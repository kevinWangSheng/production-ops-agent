"""Paired schedule ``check`` and ``load_rules`` refuse what the preregistered
rules do not allow (#514 rules drift, #512 rerun without basis)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.eval_pair_schedule import (
    LOG_SCHEMA,
    RULES_PATH,
    ScheduleError,
    check,
    load_rules,
)


def _slots(status: str = "ran") -> list[dict[str, object]]:
    return [
        {"system": "opspilot", "position": 0, "status": status, "attempt_id": "a"},
        {"system": "holmes", "position": 1, "status": status, "attempt_id": "b"},
    ]


def _window(wid: str, injected: str, restored: str, **extra: object) -> dict:
    return {
        "window_id": wid,
        "scenario_id": "s1",
        "lab": "lab-0",
        "injected_at": injected,
        "started_at": injected,
        "ended_at": restored,
        "restored_at": restored,
        "slots": _slots(),
        **extra,
    }


def _documents(sha: str | None) -> tuple[dict, dict]:
    plan = {
        "rules_sha256": sha,
        "windows": [
            {"window_id": "w1", "scenario_id": "s1", "systems": ["opspilot", "holmes"]}
        ],
    }
    log = {
        "schema": LOG_SCHEMA,
        "rules_sha256": sha,
        "windows": [
            # both slots ran fine, yet the record claims a setup failure
            _window(
                "w1",
                "2026-11-05T00:00:00+00:00",
                "2026-11-05T00:30:00+00:00",
                void="ENV_SETUP_FAILED",
                decision={"action": "rerun_pair", "systems": [], "note": None},
            ),
            _window(
                "w1-rerun",
                "2026-11-05T02:00:00+00:00",
                "2026-11-05T02:30:00+00:00",
                rerun_of="w1",
            ),
        ],
    }
    return plan, log


def test_rules_drift_is_refused_and_digest_is_checked(tmp_path: Path) -> None:
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    data["void_rerun"]["max_reruns_per_window"] = 2
    drifted = tmp_path / "rules.json"
    drifted.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ScheduleError) as refused:
        load_rules(drifted)
    assert refused.value.code == "RULES_UNSUPPORTED"

    rules = load_rules()
    plan, log = _documents("0" * 64)
    violations = check(plan, log, rules)["violations"]
    assert "RULES_MISMATCH:plan" in violations
    assert "RULES_MISMATCH:log" in violations


def test_rerun_without_basis_is_flagged_despite_self_reported_void() -> None:
    rules = load_rules()
    plan, log = _documents(rules.sha256)
    assert check(plan, log, rules)["violations"] == ["RERUN_UNJUSTIFIED:w1-rerun"]

    # the same rerun with a slot that really failed setup is justified
    log["windows"][0]["slots"][1]["status"] = "setup_failed"
    log["windows"][1]["slots"] = [log["windows"][1]["slots"][1]]
    assert check(plan, log, rules)["violations"] == []
