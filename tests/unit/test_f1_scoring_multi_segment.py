"""F1 exam in segments: voids from several schedule checks plus a
cross-segment registry (eval_dev_round ``--schedule-check``/``--voids``,
eval_compare_scoring repeated ``--schedule-log``/``--schedule-check``).
Synthetic fixtures only."""

# ruff: noqa: F811 - pytest fixture imported from test_432_dev_round
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.eval_dev_round import (
    RERUNS_SCHEMA,
    VOIDS_SCHEMA,
    DevRoundError,
    _segment_voids,
)
from tests.acceptance.test_432_dev_round import (
    MODEL_NEGATIVE,
    NOISE_NEGATIVE,
    aggregate,
    lab,  # noqa: F401 - fixture
    refusal,
    run_round,
)
from tests.acceptance.test_432_dev_round_rerun import _clone_round


def _started(sid: str, status: str, hour: int, run: bool = True) -> dict[str, Any]:
    return {
        "status": status,
        "product_run_id": f"run-{hour}" if run else None,
        "scenario_id": sid,
        "fault": {
            "started_at": f"2026-11-05T{hour:02d}:00:00+00:00",
            "ended_at": f"2026-11-05T{hour:02d}:20:00+00:00",
        },
    }


def _row(voided, rerun, rule="ENV_SETUP_FAILED", system="opspilot", sid="a"):
    return {
        "system": system,
        "scenario_id": sid,
        "window_id": f"{sid}-r1",
        "rule": rule,
        "note": "environment not ready, rerun per rule",
        "voided_attempt_id": voided,
        "rerun_window_id": f"{sid}-r1-rerun",
        "rerun_attempt_id": rerun,
    }


def _registry(*entries):
    return {"schema": VOIDS_SCHEMA, "voided_attempts": list(entries)}


def _reg(voided, rerun, sid="a", system="opspilot", **extra):
    return {
        "system": system,
        "scenario_id": sid,
        "voided_attempt_id": voided,
        "rerun_attempt_id": rerun,
        "basis": "segment 3 reran the window",
        **extra,
    }


STARTED = {
    "ops-1": _started("a", "setup_failed", 1, run=False),
    "ops-2": _started("a", "ran", 2),  # voided by a schedule ENV_SETUP_FAILED
    "ops-3": _started("a", "ran", 3),  # superseded by a later segment
    "ops-4": _started("a", "ran", 4),
    "ops-5": _started("b", "ran", 5),
}


def test_chain_across_checks_and_registry_whatever_status():
    checks = [
        {"voided_attempts": [_row("ops-1", "ops-2", rule="HARNESS_INTERRUPTED")]},
        {
            "voided_attempts": [
                _row("ops-2", "ops-3"),
                # the other system and a never-started slot void nothing here
                _row("h-1", "h-2", system="holmes"),
                _row(None, "ops-x"),
                # another dataset's scenario in the same paired plan: skipped
                _row("dev-1", "dev-2", sid="other-dataset"),
            ]
        },
    ]
    out = _segment_voids(checks, _registry(_reg("ops-3", "ops-4")), STARTED, {"a"})
    assert sorted(out) == ["ops-1", "ops-2", "ops-3"]
    assert out["ops-2"]["rule"] == "ENV_SETUP_FAILED"  # a Run that ran
    assert out["ops-2"]["source"] == "schedule_check"
    assert out["ops-3"] == {
        "rerun_attempt_id": "ops-4",
        "note": "segment 3 reran the window",
        "rule": None,
        "source": "registry",
        "basis": "segment 3 reran the window",
    }


@pytest.mark.parametrize(
    ("checks", "registry", "expected"),
    [
        (
            [{"voided_attempts": [_row("ops-2", "ops-3"), _row("ops-2", "ops-4")]}],
            None,
            "VOID_DUPLICATE:ops-2",
        ),
        (
            [{"voided_attempts": [_row("ops-2", "ops-3")]}],
            _registry(_reg("ops-2", "ops-4")),
            "VOID_DUPLICATE:ops-2",
        ),
        (
            [{"voided_attempts": [_row("ops-1", "ops-3"), _row("ops-2", "ops-3")]}],
            None,
            "RERUN_DUPLICATE:ops-3",
        ),
        ([{"voided_attempts": [_row("ops-3", "ops-2")]}], None, "RERUN_ORDER:ops-2"),
        (
            [{"voided_attempts": [_row("ops-2", "ops-5")]}],
            None,
            "RERUN_SCENARIO_MISMATCH:ops-5",
        ),
        ([{"voided_attempts": [_row("ops-2", "ops-9")]}], None, "RERUN_UNKNOWN:ops-9"),
        # declared scenario b, both attempts of scenario a
        (
            [{"voided_attempts": [_row("ops-2", "ops-3", sid="b")]}],
            None,
            "RERUN_SCENARIO_MISMATCH:ops-3",
        ),
        (
            [],
            _registry(_reg("ops-3", "ops-4", sid="b")),
            "RERUN_SCENARIO_MISMATCH:ops-4",
        ),
        ([], _registry(_reg("ops-9", "ops-4")), "RERUN_UNKNOWN:ops-9"),
        (
            [{"voided_attempts": [_row("ops-2", "ops-3", rule="WHATEVER")]}],
            None,
            "SCHEDULE_CHECK_INVALID",
        ),
        ([{"voided": []}], None, "SCHEDULE_CHECK_INVALID"),
        ([], {"schema": RERUNS_SCHEMA, "voided_attempts": []}, "VOIDS_INVALID"),
        ([], _registry(_reg("ops-3", "ops-4", basis=" ")), "VOIDS_INVALID"),
        ([], _registry(_reg("ops-3", None)), "VOIDS_INVALID"),
        ([], _registry(_reg("ops-3", "ops-4", rule="WHATEVER")), "VOIDS_INVALID"),
        (
            [],
            _registry(
                {k: v for k, v in _reg("ops-3", "ops-4").items() if k != "scenario_id"}
            ),
            "VOIDS_INVALID",
        ),
    ],
)
def test_segment_void_refusals(checks, registry, expected):
    with pytest.raises(DevRoundError) as err:
        _segment_voids(checks, registry, STARTED, {"a", "b"})
    assert err.value.code == expected


def _at(round_dir: Path, start: str, end: str) -> None:
    path = round_dir / "lifecycle.json"
    lifecycle = json.loads(path.read_text())
    lifecycle["slots"][0]["timings"].update(started_at=start, ended_at=end)
    path.write_text(json.dumps(lifecycle, indent=2) + "\n")


@pytest.fixture
def chain(lab, tmp_path):  # noqa: F811
    """One scenario, four attempts in time order (three to void, the valid
    one); the other capability scenario has a single attempt."""
    source = run_round(lab, "segment-source", [NOISE_NEGATIVE], 1)
    other = run_round(lab, "segment-other", [MODEL_NEGATIVE], 1)
    dirs, ids = [], []
    for i in range(4):
        d, attempt = _clone_round(source, tmp_path / f"seg-{i}")
        _at(d, f"2026-11-05T0{i + 1}:00:00+00:00", f"2026-11-05T0{i + 1}:20:00+00:00")
        dirs.append(d)
        ids.append(attempt)
    return dirs + [other], ids


def test_aggregate_chain_keeps_one_valid_attempt_and_every_voided_cost(lab, chain):
    dirs, ids = chain
    checks = [
        {
            "voided_attempts": [
                _row(ids[0], ids[1], "HARNESS_INTERRUPTED", sid=NOISE_NEGATIVE)
            ]
        },
        {"voided_attempts": [_row(ids[1], ids[2], sid=NOISE_NEGATIVE)]},
    ]
    registry = _registry(_reg(ids[2], ids[3], sid=NOISE_NEGATIVE))
    summary = aggregate(dirs, lab["root"], n=1, schedule_checks=checks, voids=registry)
    (slot,) = [s for s in summary["slots"] if s["scenario_id"] == NOISE_NEGATIVE]
    assert slot["attempt_id"] == ids[3] and slot["rerun_of"] == ids[2]
    voided = {v["attempt_id"]: v for v in summary["voided_attempts"]}
    assert sorted(voided) == sorted(ids[:3])
    assert voided[ids[1]]["rule"] == "ENV_SETUP_FAILED"
    assert voided[ids[2]]["source"] == "registry"
    assert voided[ids[2]]["basis"] == "segment 3 reran the window"
    assert "rule" not in voided[ids[2]]
    # voided attempts still count toward cost
    cost = summary["cost"]
    assert cost["attempts_measured"] + cost["attempts_unknown"] == 5


def test_aggregate_unvoided_extra_attempt_is_still_a_redraw(lab, chain):
    dirs, ids = chain
    checks = [{"voided_attempts": [_row(ids[1], ids[2], sid=NOISE_NEGATIVE)]}]
    assert "REDRAW_DETECTED" in refusal(
        lambda: aggregate(dirs, lab["root"], n=1, schedule_checks=checks)
    )


def test_aggregate_refuses_reruns_with_schedule_checks(lab, chain):
    dirs, ids = chain
    reruns = {"schema": RERUNS_SCHEMA, "reruns": []}
    assert "RERUNS_WITH_SCHEDULE_CHECK" in refusal(
        lambda: aggregate(dirs, lab["root"], n=1, reruns=reruns, schedule_checks=[])
    )


def test_cli_takes_one_check_per_segment(lab, chain, tmp_path):
    dirs, ids = chain
    files = []
    for i, rows in enumerate(
        (
            [_row(ids[0], ids[1], "HARNESS_INTERRUPTED", sid=NOISE_NEGATIVE)],
            [_row(ids[1], ids[2], sid=NOISE_NEGATIVE)],
        )
    ):
        path = tmp_path / f"check-{i}.json"
        path.write_text(json.dumps({"voided_attempts": rows}))
        files += ["--schedule-check", str(path)]
    voids = tmp_path / "voids.json"
    voids.write_text(json.dumps(_registry(_reg(ids[2], ids[3], sid=NOISE_NEGATIVE))))
    base = [
        sys.executable,
        "-m",
        "scripts.eval_dev_round",
        "--dataset",
        str(lab["root"]),
        "--n",
        "1",
    ]
    for d in dirs:
        base += ["--round", str(d)]
    out = tmp_path / "summary.json"
    good = subprocess.run(
        [*base, *files, "--voids", str(voids), "--out", str(out)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert good.returncode == 0, good.stdout + good.stderr
    assert len(json.loads(out.read_text())["voided_attempts"]) == 3
    bad = subprocess.run(
        [
            *base,
            *files,
            "--voids",
            str(tmp_path / "missing.json"),
            "--out",
            str(tmp_path / "bad.json"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["reason"] == "VOIDS_INVALID"


def _judge(tmp_path, name, *extra):
    from scripts import eval_compare_scoring as cli
    from tests.test_binary_judge import ENV, FakeClient, _decide, _judge_args

    return cli.run_judge(
        _judge_args(tmp_path / name, *extra), client=FakeClient(_decide), environ=ENV
    )


def _scored():
    from tests.test_binary_judge import DEV_ROUND, HOLMES_FIXTURE

    ops = json.loads(DEV_ROUND.read_text())["slots"]
    holmes = json.loads((HOLMES_FIXTURE / "round.json").read_text())["slots"]
    h = next(x for x in holmes if x["attempt_id"] == "h-pay-1")
    return ops, h["scenario_id"]


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


def test_compare_scoring_unions_segment_pairs_and_registry(tmp_path):
    from scripts import eval_compare_scoring as cli

    ops, h_sid = _scored()
    ids = [s["attempt_id"] for s in ops[:3]]
    pairs = []
    # the same window id in two segments: each check reads only its own log
    for i, victim in enumerate(ids[:2]):
        log = {
            "schema": cli.PAIRED_LOG_SCHEMA,
            "windows": [
                {
                    "window_id": "w1",
                    "void": "ENV_SETUP_FAILED",
                    "slots": [{"system": "opspilot", "attempt_id": victim}],
                }
            ],
        }
        check = {"voided": [{"window_id": "w1", "systems": ["opspilot"]}]}
        pairs += [
            "--schedule-log",
            _write(tmp_path, f"log-{i}.json", log),
            "--schedule-check",
            _write(tmp_path, f"check-{i}.json", check),
        ]
    # registry voids were already applied upstream: they name attempts no
    # longer scored, replaced (via a chain) by a scored attempt
    registry = _registry(
        _reg("ops-gone-1", "ops-gone-2", sid=ops[2]["scenario_id"]),
        _reg("ops-gone-2", ids[2], sid=ops[2]["scenario_id"]),
        _reg("h-gone", "h-pay-1", sid=h_sid, system="holmes"),
        _reg("elsewhere", "elsewhere-2", sid="other-dataset"),
    )
    voids = _write(tmp_path, "voids.json", registry)
    result = _judge(tmp_path, "judge", *pairs, "--voids", voids)
    assert result["voided"] == 2 and result["attempts"] == 40
    items = json.loads((tmp_path / "judge" / "items.json").read_text())
    assert items["voided"] == sorted([*ids[:2], "ops-gone-1", "ops-gone-2", "h-gone"])
    # a log without its check (or the reverse) is refused
    with pytest.raises(cli.BinaryJudgeError) as err:
        _judge(tmp_path, "lone", *pairs, "--schedule-log", str(tmp_path / "log-0.json"))
    assert err.value.code == "SCHEDULE_LOG_AND_CHECK_REQUIRED"


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (
            lambda o, h: [
                _reg(o[0]["attempt_id"], o[1]["attempt_id"], sid=o[0]["scenario_id"])
            ],
            "VOID_SCORED",
        ),
        (
            lambda o, h: [_reg("gone", "ghost", sid=o[0]["scenario_id"])],
            "VOID_RERUN_UNKNOWN:gone",
        ),
        # declared holmes, replaced by an OpsPilot attempt
        (
            lambda o, h: [
                _reg(
                    "gone", o[0]["attempt_id"], sid=o[0]["scenario_id"], system="holmes"
                )
            ],
            "VOID_RERUN_UNKNOWN:gone",
        ),
        (
            lambda o, h: [_reg("gone", "h-pay-1", sid=o[0]["scenario_id"])],
            "VOID_RERUN_UNKNOWN:gone",
        ),
        (lambda o, h: [_reg("a", "b", sid=h), _reg("b", "a", sid=h)], "VOID_CYCLE"),
        (lambda o, h: [{"schema": "x"}], "VOIDS_INVALID"),
    ],
)
def test_compare_scoring_registry_refusals(tmp_path, entries, expected):
    from scripts import eval_compare_scoring as cli

    ops, h_sid = _scored()
    rows = entries(ops, h_sid)
    doc = rows[0] if expected == "VOIDS_INVALID" else _registry(*rows)
    voids = _write(tmp_path, "voids.json", doc)
    with pytest.raises(cli.BinaryJudgeError) as err:
        _judge(tmp_path, "out", "--voids", voids)
    assert err.value.code.startswith(expected)
