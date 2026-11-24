"""#666 PR-B contract: driver rules that do not need a running stack
(H-7, H-17, H-18, H-19, H-21, H-24).

Independent contract tests written from the task record's PR-B public
interface (``driver.py``) without reading the implementation. No PG, web,
worker, ES, model or network is started.

Rules under test:

* void line: setup end to delivery over 120 s voids the attempt;
* 280 waits for UTC midnight when less than 10 minutes remain (until 5 s
  past midnight); every other question and time waits 0;
* resume: ``done`` needs the completion marker, a ``completed`` summary and
  a final judgement (a choice, ``no_report`` or ``JUDGE_REQUEST_OVERSIZE``);
  a ``missing`` summary with a reason is terminal; anything else is not done;
* ``trial_record`` maps a summary to PR-A ``scoring.Trial`` categories;
* the command line aborts with exit 2 and ``{"status":"aborted","code":...}``
  on a bad round label, an unknown question or an upstream mismatch, and
  never prints a secret read from ``--env-file``.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* the per-trial directories are named by the full question directory name
  (``<tmp_root>/<round>/<qid>/trial-<n>/complete.json`` and
  ``<evidence_dir>/<qid>/trial-<n>/summary.json``, H-20); the interface
  writes "<题号>" (reported as a spec gap);
* the marker's content is not fixed; the tests write
  ``{"status": "completed"}`` as ITBench's marker does;
* exactly 120 s and exactly 10 minutes are left untested (the interface
  does not say which side of the line they fall on).
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.holmes_evals_support import mod, upstream_root

REPO = Path(__file__).resolve().parents[1]
MODEL = "model-fake-value-4"
Q275 = "275_es_field_name_correction"
Q185 = "185_elasticsearch_cross_region_search"
Q280 = "280_es_multi_quirk"
Q235 = "235_elasticsearch_cluster_mismatch"


def _cfg(tmp_path: Path, **overrides: Any) -> Any:
    driver = mod("driver")
    wanted: dict[str, Any] = {
        "round": "t1",
        "upstream": tmp_path / "upstream",
        "questions": (Q275,),
        "es_mode": "fake",
        "es_url": "http://127.0.0.1:29292",
        "es_readonly_env": None,
        "k8s_env_file": None,
        "kubeconfig": None,
        "env_file": None,
        "trace": "off",
        "judge": False,
        "allow_dirty": True,
        "tmp_root": tmp_path / "tmp",
        "evidence_root": tmp_path / "evidence",
        **overrides,
    }
    names = {f.name for f in dataclasses.fields(driver.Config)}
    return driver.Config(**{k: v for k, v in wanted.items() if k in names})


# -- void line and the 280 midnight rule ----------------------------------------


def test_void_line() -> None:
    driver = mod("driver")
    assert driver.VOID_SECONDS == 120
    ended = datetime(2026, 11, 18, 10, 0, 0, tzinfo=UTC)
    assert driver.setup_to_delivery_ok(ended, ended) is True
    assert driver.setup_to_delivery_ok(ended, ended + timedelta(seconds=119)) is True
    assert driver.setup_to_delivery_ok(ended, ended + timedelta(seconds=121)) is False
    assert driver.setup_to_delivery_ok(ended, ended + timedelta(hours=1)) is False


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 18, 23, 55, 0, tzinfo=UTC), 305.0),
        (datetime(2026, 11, 18, 23, 50, 1, tzinfo=UTC), 604.0),
        (datetime(2026, 11, 18, 23, 59, 59, tzinfo=UTC), 6.0),
        (datetime(2026, 11, 18, 23, 49, 0, tzinfo=UTC), 0.0),
        (datetime(2026, 11, 19, 0, 0, 30, tzinfo=UTC), 0.0),
        (datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC), 0.0),
    ],
)
def test_utc_midnight_wait_for_280(now: datetime, expected: float) -> None:
    wait = mod("driver").seconds_to_wait_for_utc_midnight(Q280, now)
    assert wait == pytest.approx(expected, abs=0.01)


def test_utc_midnight_wait_other_questions_zero() -> None:
    now = datetime(2026, 11, 18, 23, 55, 0, tzinfo=UTC)
    f = mod("driver").seconds_to_wait_for_utc_midnight
    for qid in mod("questions").B1_QUESTIONS:
        if qid != Q280:
            assert f(qid, now) == 0


# -- summaries -------------------------------------------------------------------


def _judge(choice: str | None, passed: bool | None, error: str | None = None):
    return {
        "receiver": "zai",
        "model": "glm-5.3-flashx",
        "mode": "strict",
        "prompt_sha256": "0" * 64,
        "choice": choice,
        "passed": passed,
        "error_code": error,
        "attempts": 1 if choice else 3,
        "request_bytes": 1000,
    }


def _completed(qid: str, trial: int, kind: str) -> dict[str, Any]:
    reason = None
    text_sha = "a" * 64
    if kind == "pass":
        judge, category = _judge("A", True), "ok"
    elif kind == "fail":
        judge, category = _judge("B", False), "ok"
    elif kind == "no_report":
        judge = dict.fromkeys(_judge(None, None), None)
        judge["receiver"] = "zai"
        category, reason, text_sha = "no_report", "REPORT_REJECTED_BY_PRODUCT", None
    elif kind == "oversize":
        judge, category = (
            _judge(None, None, "JUDGE_REQUEST_OVERSIZE"),
            ("judge_exhausted"),
        )
    elif kind == "exhausted":
        judge, category = (
            _judge(None, None, "JUDGE_UPSTREAM_ERROR"),
            ("judge_exhausted"),
        )
    elif kind == "unjudged":
        judge = dict.fromkeys(_judge(None, None), None)
        category = None
    else:
        raise AssertionError(kind)
    return {
        "status": "completed",
        "run": {"run_id": "22222222-2222-4222-8222-222222222222"},
        "trace": None,
        "holmes_eval": {
            "schema": "holmes-eval-trial-1",
            "track": "HolmesGPT 公开评测集（竞品自评集）",
            "round": "t1",
            "qid": qid,
            "trial": trial,
            "chance": 1,
            "conversion": {
                "text_sha256": text_sha,
                "with_tool_calls": False,
                "tool_calls": 0,
                "tool_calls_sha256": None,
                "deshift_count": 0,
                "reason": reason,
            },
            "judge": judge,
            "category": category,
            "time_box_limited": qid in mod("shift").TIME_BOX_LIMITED,
            "disclosures": [],
            "history": [],
        },
    }


def _missing(qid: str, trial: int, reason: str | None = "CHANCES_EXHAUSTED"):
    out: dict[str, Any] = {
        "status": "missing",
        "holmes_eval": {"qid": qid, "trial": trial, "history": [], "git": {}},
    }
    if reason is not None:
        out["reason"] = reason
    return out


@pytest.mark.parametrize(
    ("kind", "passed", "category"),
    [
        ("pass", True, "ok"),
        ("fail", False, "ok"),
        ("no_report", False, "no_report"),
        ("exhausted", False, "judge_exhausted"),
        ("oversize", False, "judge_exhausted"),
    ],
)
def test_trial_record_categories(kind: str, passed: bool, category: str) -> None:
    trial = mod("driver").trial_record(_completed(Q275, 2, kind))
    assert isinstance(trial, mod("scoring").Trial)
    assert (trial.qid, trial.index, trial.passed, trial.category) == (
        Q275,
        2,
        passed,
        category,
    )
    assert trial.time_box_limited is False


def test_trial_record_missing_is_env_broken_and_time_box_flag() -> None:
    # #809 (B4-12): 235 stays time-box-limited; 185 left the set.
    trial = mod("driver").trial_record(_missing(Q235, 3))
    assert (trial.qid, trial.index, trial.passed, trial.category) == (
        Q235,
        3,
        False,
        "env_broken",
    )
    assert trial.time_box_limited is True
    ok = mod("driver").trial_record(_completed(Q235, 1, "pass"))
    assert ok.time_box_limited is True
    assert mod("driver").trial_record(_missing(Q185, 3)).time_box_limited is False
    assert (
        mod("driver").trial_record(_completed(Q185, 1, "pass")).time_box_limited
        is False
    )


# -- resume ----------------------------------------------------------------------


def _paths(cfg: Any, qid: str, trial: int) -> tuple[Path, Path]:
    # ASSUMPTION: directories named by the full qid (module docstring).
    summary = Path(cfg.evidence_dir) / qid / f"trial-{trial}" / "summary.json"
    marker = Path(cfg.tmp_root) / cfg.round / qid / f"trial-{trial}" / "complete.json"
    return summary, marker


def _write(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


@pytest.mark.parametrize("kind", ["pass", "fail", "no_report", "oversize"])
def test_trial_state_done_needs_marker_summary_and_final_judgement(
    tmp_path: Path, kind: str
) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    summary, marker = _paths(cfg, Q275, 1)
    _write(summary, _completed(Q275, 1, kind))
    assert driver.trial_state(cfg, Q275, 1) != "done"  # no marker yet
    _write(marker, {"status": "completed"})
    assert driver.trial_state(cfg, Q275, 1) == "done"


def test_trial_state_marker_without_final_judgement_is_not_done(
    tmp_path: Path,
) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    summary, marker = _paths(cfg, Q275, 2)
    _write(summary, _completed(Q275, 2, "unjudged"))
    _write(marker, {"status": "completed"})
    assert driver.trial_state(cfg, Q275, 2) in {"judge", "rerun"}


def test_trial_state_marker_without_summary_is_rerun(tmp_path: Path) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    _, marker = _paths(cfg, Q275, 3)
    _write(marker, {"status": "completed"})
    assert driver.trial_state(cfg, Q275, 3) == "rerun"


def test_trial_state_missing_and_nothing(tmp_path: Path) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    assert driver.trial_state(cfg, Q275, 1) == "rerun"
    summary, _ = _paths(cfg, Q275, 1)
    _write(summary, _missing(Q275, 1))
    assert driver.trial_state(cfg, Q275, 1) == "missing"
    summary2, _ = _paths(cfg, Q275, 2)
    _write(summary2, _missing(Q275, 2, reason=None))
    assert driver.trial_state(cfg, Q275, 2) == "rerun"
    summary3, _ = _paths(cfg, Q275, 3)
    _write(summary3, _missing(Q275, 3, reason="SHIFT_OVERLAP"))
    assert driver.trial_state(cfg, Q275, 3) == "missing"


def test_trial_state_is_per_trial(tmp_path: Path) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    summary, marker = _paths(cfg, Q275, 1)
    _write(summary, _completed(Q275, 1, "pass"))
    _write(marker, {"status": "completed"})
    assert driver.trial_state(cfg, Q275, 1) == "done"
    assert driver.trial_state(cfg, Q275, 2) == "rerun"
    assert driver.trial_state(cfg, Q185, 1) == "rerun"


# -- command line aborts -----------------------------------------------------------


def _cli(tmp_path: Path, *args: str) -> tuple[int, str, str]:
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"DEEPSEEK_API_KEY={MODEL}\n")
    env_file.chmod(0o600)
    env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "TMPDIR", "LANG", "TZ"} or k.startswith("LC_")
    }
    env["PYTHON_DOTENV_DISABLED"] = "1"
    proc = subprocess.run(
        [
            sys.executable, "-m", "scripts.holmes_evals.driver", *args,
            "--env-file", str(env_file), "--trace", "off", "--no-judge",
            "--allow-dirty", "--tmp-root", str(tmp_path / "tmp"),
            "--evidence-root", str(tmp_path / "evidence"),
        ],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return proc.returncode, proc.stdout, proc.stderr


def _aborted(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.strip().splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("status") == "aborted":
            return doc
    raise AssertionError(f"no aborted line in {stdout!r}")


def test_cli_bad_round_aborts(tmp_path: Path) -> None:
    code, out, err = _cli(
        tmp_path, "round", "--round", "Bad Round!", "--upstream", str(tmp_path)
    )
    assert code == 2
    assert _aborted(out)["code"] == "BAD_ROUND"
    assert MODEL not in out and MODEL not in err


def test_cli_upstream_mismatch_aborts(tmp_path: Path) -> None:
    (tmp_path / "not-upstream").mkdir()
    code, out, err = _cli(
        tmp_path,
        "round", "--round", "t1", "--upstream", str(tmp_path / "not-upstream"),
        "--questions", "275",
    )  # fmt: skip
    assert code == 2
    assert str(_aborted(out)["code"]).startswith("UPSTREAM_")
    assert MODEL not in out and MODEL not in err


def test_cli_unknown_question_aborts(tmp_path: Path) -> None:
    root = upstream_root()
    if root is None:
        pytest.skip("pinned upstream checkout not present")
    code, out, err = _cli(
        tmp_path,
        "round", "--round", "t1", "--upstream", str(root),
        "--questions", "999_no_such_question",
    )  # fmt: skip
    assert code == 2
    assert _aborted(out)["code"] == "QUESTION_UNKNOWN"
    assert MODEL not in out and MODEL not in err
    assert not (tmp_path / "evidence" / "holmes-evals-t1").exists() or not any(
        (tmp_path / "evidence" / "holmes-evals-t1").rglob("summary.json")
    )
