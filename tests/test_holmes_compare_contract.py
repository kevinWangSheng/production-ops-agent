"""#668 contract: Holmes same-question comparison harness and merge table.

Synthetic two-agent records only; no upstream ``test_case.yaml`` is opened,
no model, judge or ES is called. Checks: one scoring rule for both agents,
missing trials and judge failures never pass, the R8-13 questions, no answer
or credential reaching the table, the Holmes attempt classification, the
harness environment's key isolation, and the upstream-side pytest plugin
end to end against a stand-in of upstream's test module.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_compare import merge, records, runner
from scripts.holmes_evals.questions import (
    B1_QUESTIONS,
    R8_13_SKILL_ASSERTION_DROPPED,
    SKILL_ASSERTION_DROPPED,
)

REPO = Path(__file__).resolve().parents[1]
Q_REL = "254_elasticsearch_dr_test_log_check"  # relative time, not R8-13
Q_SKILL = "275_es_field_name_correction"  # R8-13, fixed time
Q_OTHER = "183b_elasticsearch_index_discovery"
Q_LOKI = "282_loki_custom_stream_label"  # R8-13, C5
POISON = (
    "EXPECTED-ANSWER-7x9k",
    "RATIONALE-TEXT-q2",
    "sk-test-deepseek-0000",
    "zai-test-key-0000",
    "es-engineer-key-0000",
)


# -- synthetic summaries --------------------------------------------------------------------


def ops_summary(
    qid: str,
    trial: int,
    choice: str | None,
    *,
    missing: bool = False,
    no_report: bool = False,
) -> dict[str, Any]:
    if missing:
        return {
            "status": "missing",
            "holmes_eval": {"qid": qid, "trial": trial, "history": []},
        }
    return {
        "status": "completed",
        "run": {
            "tool_requests": [{"tool": "es_search"}, {"tool": "es_search"}],
            "report": {"summary": POISON[0]},
        },
        "holmes_eval": {
            "qid": qid,
            "trial": trial,
            "category": "no_report" if no_report else "ok",
            "run_wait": {"seconds": 41.5},
            "shift": {"applied": qid != Q_REL},
            "judge": {
                "receiver": "zai",
                "choice": choice,
                "passed": choice == "A",
                "error_code": None if choice else "JUDGE_EXHAUSTED",
                "rationale": POISON[1],
                "expected": POISON[0],
            },
            "secrets": {"DEEPSEEK_API_KEY": POISON[2]},
        },
    }


def holmes_summary(
    qid: str,
    trial: int,
    choice: str | None,
    *,
    category: str = "ok",
    missing: bool = False,
    passed: bool | None = None,
    judge_path: str = "opspilot",
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "qid": qid,
        "trial": trial,
        "category": "env_broken",
        "history": [],
    }
    if not missing:
        block.update(
            category=category,
            seconds=33.0,
            tool_calls=5,
            shift={"applied": qid != Q_REL},
            judge={
                "path": judge_path,
                "choice": choice,
                "passed": choice == "A" if passed is None else passed,
                "error_code": None if choice else "JUDGE_NO_CHOICE",
                "rationale": POISON[1],
            },
            expected_output=[POISON[0]],
            env={"ZAI_API_KEY": POISON[3], "ELASTICSEARCH_API_KEY": POISON[4]},
        )
    return {
        "schema": records.COMPARE_SCHEMA,
        "status": "missing" if missing else "completed",
        "holmes_compare": block,
    }


def write(root: Path, summary: dict[str, Any], block: str) -> None:
    b = summary[block]
    path = root / b["qid"] / f"trial-{b['trial']}" / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary))


def table(
    tmp_path: Path,
    ops: list[dict[str, Any]],
    hol: list[dict[str, Any]],
    questions: tuple[str, ...] = B1_QUESTIONS,
    es_mode: str = "external",
) -> tuple[dict[str, Any], str]:
    o, h = tmp_path / "ops", tmp_path / "hol"
    for s in ops:
        write(o, s, "holmes_eval")
    for s in hol:
        write(h, s, "holmes_compare")
    out_md, out_json = tmp_path / "t.md", tmp_path / "t.json"
    assert (
        merge.main(
            [
                "--opspilot-evidence",
                str(o),
                "--holmes-evidence",
                str(h),
                "--out-md",
                str(out_md),
                "--out-json",
                str(out_json),
                "--opspilot-es-mode",
                es_mode,
                *(
                    ["--questions", ",".join(questions)]
                    if questions != B1_QUESTIONS
                    else []
                ),
            ]
        )
        == 0
    )
    return json.loads(out_json.read_text()), out_md.read_text()


def row(result: dict[str, Any], qid: str) -> dict[str, Any]:
    return next(r for r in result["rows"] if r["qid"] == qid)


# -- scoring --------------------------------------------------------------------------------


def test_same_outcomes_score_the_same_on_both_sides(tmp_path: Path) -> None:
    choices = ["A", "B", "A"]
    ops = [ops_summary(Q_REL, i + 1, c) for i, c in enumerate(choices)]
    hol = [holmes_summary(Q_REL, i + 1, c) for i, c in enumerate(choices)]
    result, _ = table(tmp_path, ops, hol, (Q_REL, Q_OTHER))
    r = row(result, Q_REL)
    for key in ("passes", "of", "rate", "swing", "trials"):
        assert r["opspilot"][key] == r["holmes"][key], key
    assert r["holmes"]["passes"] == 2 and r["holmes"]["swing"] is True
    assert (
        result["totals"]["opspilot"]["passes"]
        == result["totals"]["holmes"]["passes"]
        == 2
    )
    assert result["totals"]["holmes"]["denominator"] == 6  # Q_OTHER: 3 missing trials
    assert result["totals_merged"] is False
    assert set(result["totals"]) == {"opspilot", "holmes"}


def test_missing_trials_and_judge_failures_never_pass(tmp_path: Path) -> None:
    ops = [
        ops_summary(Q_REL, 1, "A"),
        ops_summary(Q_REL, 2, None),
        ops_summary(Q_REL, 3, None, missing=True),
    ]
    hol = [
        holmes_summary(Q_REL, 1, "A"),
        # a malformed record claiming a pass without a verdict
        holmes_summary(Q_REL, 2, None, category="judge_exhausted", passed=True),
        holmes_summary(Q_REL, 3, "A", category="no_report"),
    ]
    result, md = table(tmp_path, ops, hol, (Q_REL,))
    r = row(result, Q_REL)
    assert r["opspilot"]["passes"] == 1 and r["opspilot"]["of"] == 3
    assert r["opspilot"]["missing"] == 1 and r["opspilot"]["judge_failed"] == 1
    assert r["holmes"]["passes"] == 1
    assert r["holmes"]["judge_failed"] == 1 and r["holmes"]["no_answer"] == 1
    assert r["holmes"]["trials"] == [True, False, False]

    # A trial that was never run counts 0 and stays in the denominator.
    result2, _ = table(tmp_path / "b", [], [holmes_summary(Q_REL, 1, "A")], (Q_REL,))
    h = row(result2, Q_REL)["holmes"]
    assert (h["passes"], h["of"], h["missing"]) == (1, 3, 2)
    assert h["trials"] == [True, None, None]


def test_holmes_ok_without_a_verdict_is_a_judge_failure() -> None:
    r = records.holmes_row(holmes_summary(Q_REL, 1, None, category="ok", passed=True))
    assert (r.category, r.passed) == ("judge_exhausted", False)


def test_reruns_stay_out_of_the_main_score(tmp_path: Path) -> None:
    hol = [holmes_summary(Q_REL, i, "A") for i in (1, 4, 5, 6)]
    result, _ = table(tmp_path, [], hol, (Q_REL,))
    h = row(result, Q_REL)["holmes"]
    assert (h["passes"], h["rerun_passes"], h["rerun_trials"]) == (1, 3, 3)


# -- R8-13 ----------------------------------------------------------------------------------


def test_skill_assertion_questions_are_flagged_and_dropped(tmp_path: Path) -> None:
    result, md = table(tmp_path, [], [])
    flagged = {r["qid"] for r in result["rows"] if r["skill_assertion_dropped"]}
    assert (
        flagged
        == set(SKILL_ASSERTION_DROPPED)
        == {
            "275_es_field_name_correction",
            "276_es_numeric_severity_field",
            "277_es_timestamp_keyword_field",
            "278_es_schema_discovery",
            "280_es_multi_quirk",
        }
    )
    assert any("R8-13" in n for n in result["notes"])
    # R8-13 counts 6 across tracks: the B1 five plus C5's 282 (lead, 2026-11-19).
    assert R8_13_SKILL_ASSERTION_DROPPED == set(SKILL_ASSERTION_DROPPED) | {Q_LOKI}
    cfg = _cfg(tmp_path)
    assert runner.pytest_env(cfg, Q_SKILL, tmp_path)["OHC_DROP_SKILL"] == "1"
    assert runner.pytest_env(cfg, Q_LOKI, tmp_path)["OHC_DROP_SKILL"] == "1"
    assert runner.pytest_env(cfg, Q_REL, tmp_path)["OHC_DROP_SKILL"] == "0"


def test_table_discloses_tool_face_and_time_shift(tmp_path: Path) -> None:
    hol = [holmes_summary(Q_SKILL, 1, "A")]
    ops = [ops_summary(Q_SKILL, 1, "A")]
    result, md = table(tmp_path, ops, hol)
    assert "Holmes 按官方默认配置运行，含 OpsPilot 按只读约束不提供的工具" in md
    assert row(result, Q_SKILL)["fixed_time"] is True
    assert row(result, Q_SKILL)["holmes"]["shift_applied"] is True
    assert row(result, Q_REL)["fixed_time"] is False
    assert "不合并" in md


# -- isolation ------------------------------------------------------------------------------


def test_table_carries_no_answer_rationale_or_credential(tmp_path: Path) -> None:
    ops = [ops_summary(Q_REL, i, "A") for i in (1, 2, 3)]
    hol = [holmes_summary(Q_REL, i, "B") for i in (1, 2, 3)]
    result, md = table(tmp_path, ops, hol)
    dumped = json.dumps(result, ensure_ascii=False)
    for needle in POISON:
        assert needle not in dumped and needle not in md
    keys: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            keys.update(v)
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(result)
    banned = {
        "expected",
        "expected_output",
        "rationale",
        "env",
        "secrets",
        "report",
        "api_key",
        "text",
    }
    assert not keys & banned


def _cfg(tmp_path: Path, judge_path: str = "opspilot") -> runner.Config:
    return runner.Config(
        round="t1",
        upstream=tmp_path,
        holmes_python=tmp_path / "python",
        judge_path=judge_path,
        secrets={
            "DEEPSEEK_API_KEY": POISON[2],
            "ZAI_API_KEY": POISON[3],
            "ELASTICSEARCH_URL": "http://localhost:9200",
            "ELASTICSEARCH_API_KEY": POISON[4],
        },
    )


def test_judge_key_reaches_the_harness_only_in_upstream_judge_mode(
    tmp_path: Path,
) -> None:
    ops_env = runner.pytest_env(_cfg(tmp_path, "opspilot"), Q_REL, tmp_path)
    assert POISON[3] not in ops_env.values()
    assert ops_env["OPENAI_API_BASE"] == runner.DEEPSEEK_BASE
    up_env = runner.pytest_env(_cfg(tmp_path, "upstream"), Q_REL, tmp_path)
    assert up_env["OPENAI_API_KEY"] == POISON[3]
    for env in (ops_env, up_env):
        assert env["MODEL"] == "deepseek/deepseek-flash"
        assert (
            env["THINKING"] == '{"type": "enabled"}'
            and env["REASONING_EFFORT"] == "high"
        )
        assert env["OHC_QID"] == Q_REL and env["ITERATIONS"] == "1"
        assert not any(
            k.startswith(("OPSPILOT_", "LANGSMITH", "BRAINTRUST")) for k in env
        )


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://localhost:9200", None),
        ("http://localhost", None),
        ("https://abc.us-east-1.aws.cloud.es.io", None),
        ("http://127.0.0.1:9200", "ES_URL_USE_LOCALHOST"),
        ("https://localhost:9200", "ES_URL_NOT_PASSTHRU"),
        ("http://localhost.evil.example:9200", "ES_URL_NOT_PASSTHRU"),
    ],
)
def test_es_url_must_be_on_upstreams_passthru_list(url: str, code: str | None) -> None:
    if code is None:
        runner.check_es_url(url)
    else:
        with pytest.raises(runner.RoundAbort) as exc:
            runner.check_es_url(url)
        assert exc.value.code == code
        if code == "ES_URL_USE_LOCALHOST":
            assert exc.value.hint and "http://localhost:<port>" in exc.value.hint


def test_judge_path_defaults_to_opspilot_and_is_named_on_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        runner, "run_round", lambda cfg: seen.setdefault("p", cfg.judge_path)
    )
    monkeypatch.setattr(runner, "load_env_file", lambda path: {})
    argv = [
        "round",
        "--round",
        "r1",
        "--upstream",
        str(tmp_path),
        "--holmes-python",
        str(tmp_path / "py"),
        "--env-file",
        str(tmp_path / "e"),
    ]
    assert runner.main(argv) == 0 and seen["p"] == "opspilot"
    result, md = table(tmp_path, [], [holmes_summary(Q_REL, 1, "A")], (Q_REL,))
    assert result["holmes_judge_path"] == "opspilot"
    assert "默认 opspilot" in md.split("|", 1)[0]  # in the page header
    up, md_up = table(
        tmp_path / "u",
        [],
        [holmes_summary(Q_REL, 1, "A", judge_path="upstream")],
        (Q_REL,),
    )
    assert up["holmes_judge_path"] == "upstream" and "可选对照" in md_up


def test_merge_refuses_fake_es_and_mixed_judge_paths(tmp_path: Path) -> None:
    hol = [holmes_summary(Q_REL, 1, "A")]
    with pytest.raises(AssertionError):
        table(
            tmp_path / "f", [ops_summary(Q_REL, 1, "A")], hol, (Q_REL,), es_mode="fake"
        )
    assert not (tmp_path / "f" / "t.json").exists()
    mixed = [
        holmes_summary(Q_REL, 1, "A"),
        holmes_summary(Q_REL, 2, "A", judge_path="upstream"),
    ]
    with pytest.raises(AssertionError):
        table(tmp_path / "m", [], mixed, (Q_REL,))
    assert not (tmp_path / "m" / "t.json").exists()


# -- attempt classification ------------------------------------------------------------------


def _h(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "selected": 1,
        "setup_failed": False,
        "shift": {"status": "ok"},
        "captured": True,
        "errors": {},
    }
    base.update(kw)
    return base


@pytest.mark.parametrize(
    ("harness", "judge", "timed_out", "expected"),
    [
        (None, None, False, ("env_broken", "HARNESS_NO_STATE")),
        (None, None, True, ("env_broken", "PYTEST_TIMEOUT")),
        (_h(selected=0), None, False, ("env_broken", "HARNESS_SELECT_FAILED")),
        (
            _h(setup_failed=True, shift=None, captured=False),
            None,
            False,
            ("env_broken", "SETUP_FAILED"),
        ),
        (
            _h(shift={"status": "error", "code": "SHIFT_OVERLAP"}, captured=False),
            None,
            False,
            ("env_broken", "SHIFT_OVERLAP"),
        ),
        (_h(shift=None, captured=False), None, True, ("env_broken", "PYTEST_TIMEOUT")),
        (_h(captured=False), None, True, ("no_report", "HOLMES_TIMEOUT")),
        (_h(captured=False), None, False, ("no_report", "HOLMES_NO_ANSWER")),
        (_h(), None, False, ("judge_exhausted", "JUDGE_NOT_RUN")),
        (
            _h(),
            {"choice": None, "error_code": "JUDGE_EXHAUSTED"},
            False,
            ("judge_exhausted", "JUDGE_EXHAUSTED"),
        ),
        (_h(), {"choice": "B"}, False, ("ok", None)),
        (_h(), {"choice": "A"}, False, ("ok", None)),
    ],
)
def test_classify_holmes(
    harness: Any, judge: Any, timed_out: bool, expected: tuple[str, str | None]
) -> None:
    assert records.classify_holmes(harness, judge, timed_out=timed_out) == expected


def test_upstream_judge_score_maps_to_choice() -> None:
    assert runner.judge_upstream({"upstream_score": 1})["choice"] == "A"
    assert runner.judge_upstream({"upstream_score": 0})["choice"] == "B"
    none = runner.judge_upstream({"upstream_score": None})
    assert none["choice"] is None and none["passed"] is False


# -- the upstream-side plugin, end to end ------------------------------------------------------

_FAKE_TEST = """
import pytest


class Case:
    def __init__(self, id, prompt):
        self.id = id
        self.user_prompt = prompt
        self.memories_generated = True
        self.rerun_with_memory = True
        self.expected_output = ["EXPECTED-ANSWER-7x9k"]


CASES = [Case("{q}", "How many logs on 2026-01-01?"), Case("{other}", "Other question")]


@pytest.fixture(scope="session")
def shared_test_infrastructure():
    return {{"setup_failures": {{}}}}


@pytest.mark.parametrize("test_case", CASES, ids=[c.id for c in CASES])
def test_ask_holmes(test_case, request, shared_test_infrastructure):
    from tests.llm.utils.classifiers import evaluate_correctness  # as property_manager
    request.node.user_properties.append(("holmes_duration", 12.5))
    request.node.user_properties.append(("tool_call_count", 4))
    assert test_case.memories_generated is None and test_case.rerun_with_memory is False
    output = "ANSWER for: " + test_case.user_prompt
    result = evaluate_correctness(expected_elements=test_case.expected_output,
                                  output=output, parent_span=None, caplog=None)
    assert int(result.score) == 1, f"Expected: {{test_case.expected_output}}"
"""

_FAKE_CLASSIFIERS = """
class R:
    score = 0
    metadata = {"rationale": "RATIONALE-TEXT-q2"}


def evaluate_correctness(expected_elements, output, parent_span, caplog,
                         evaluation_type="strict"):
    return R()
"""


@pytest.mark.parametrize("judge_path", ["opspilot", "upstream"])
def test_plugin_selects_one_question_and_captures_judge_text(
    tmp_path: Path, judge_path: str
) -> None:
    root = tmp_path / "upstream"
    utils = root / "tests" / "llm" / "utils"
    utils.mkdir(parents=True)
    for d in (root / "tests", root / "tests" / "llm", utils):
        (d / "__init__.py").write_text("")
    (root / "tests" / "llm" / "test_ask_holmes.py").write_text(
        textwrap.dedent(_FAKE_TEST.format(q=Q_REL, other=Q_OTHER))
    )
    (utils / "classifiers.py").write_text(textwrap.dedent(_FAKE_CLASSIFIERS))
    (root / "pytest.ini").write_text("[pytest]\n")
    trial = tmp_path / "trial"
    trial.mkdir()
    env = runner.pytest_env(_cfg(tmp_path, judge_path), Q_REL, trial)
    env["OHC_DROP_SKILL"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/llm/test_ask_holmes.py",
            "-p",
            runner.PLUGIN,
            "-p",
            "no:cacheprovider",
            "-q",
            "--tb=no",
            "-p",
            "no:langsmith_plugin",
            "-c",
            str(root / "pytest.ini"),
            "--rootdir",
            str(root),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    harness = json.loads((trial / "harness.json").read_text())
    assert harness["selected"] == 1
    assert harness["shift"]["status"] == "ok" and harness["shift"]["applied"] is False
    assert harness["captured"] is True and harness["deshift_count"] == 0
    assert harness["properties"] == {"holmes_duration": 12.5, "tool_call_count": 4}
    assert (trial / "judge-input.txt").read_text() == (
        "ANSWER for: How many logs on 2026-01-01?"
    )
    if judge_path == "opspilot":
        # upstream's judge is not called; the stub keeps the assertion silent
        assert harness["upstream_score"] is None
        assert harness["phases"]["call"] == "passed", proc.stdout
    else:
        assert harness["upstream_score"] == 0
        assert harness["phases"]["call"] == "failed"
        assert harness["errors"]["call"] == "AssertionError"
    stored = "".join(p.read_text() for p in trial.iterdir() if p.is_file())
    for needle in POISON:
        assert needle not in stored
    assert "EXPECTED-ANSWER-7x9k" not in proc.stdout + proc.stderr


def _fake_rental_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway git checkout holding run_rental.sh with recording stubs."""
    root = tmp_path / "repo"
    pkg = root / "scripts" / "holmes_compare"
    pkg.mkdir(parents=True)
    (pkg / "run_rental.sh").write_text(
        (REPO / "scripts" / "holmes_compare" / "run_rental.sh").read_text()
    )
    (pkg / "install_upstream.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    log = tmp_path / "calls.log"
    venv = root / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(f'#!/usr/bin/env bash\necho "$*" >> {log}\n')
    for p in (pkg / "run_rental.sh", pkg / "install_upstream.sh", venv / "python"):
        p.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root, log


@pytest.mark.parametrize("form", ["split", "equals"])
def test_run_rental_scores_use_the_round_evidence_root_and_questions(
    tmp_path: Path, form: str
) -> None:
    root, log = _fake_rental_repo(tmp_path)
    ev = str(tmp_path / "ev")
    extra = (
        ["--evidence-root", ev, "--questions", "c5", "--trials", "1"]
        if form == "split"
        else [f"--evidence-root={ev}", "--questions=c5", "--trials", "1"]
    )
    subprocess.run(
        ["scripts/holmes_compare/run_rental.sh", "r9", "env.txt", *extra],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=60,
    )
    round_call, scores_call = log.read_text().splitlines()
    assert round_call.startswith("-m scripts.holmes_compare.runner round --round r9")
    assert scores_call.split() == [
        "-m",
        "scripts.holmes_compare.runner",
        "scores",
        "--round",
        "r9",
        "--evidence-root",
        ev,
        "--questions",
        "c5",
    ]


def test_run_rental_scores_default_root_without_extra_args(tmp_path: Path) -> None:
    root, log = _fake_rental_repo(tmp_path)
    subprocess.run(
        ["scripts/holmes_compare/run_rental.sh", "r9", "env.txt"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=60,
    )
    assert log.read_text().splitlines()[1] == (
        "-m scripts.holmes_compare.runner scores --round r9"
    )


def test_scores_creates_the_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "fresh" / "holmes-compare-r9" / "scores.json"
    assert (
        runner.main(
            [
                "scores",
                "--round",
                "r9",
                "--evidence-root",
                str(tmp_path / "ev"),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert json.loads(out.read_text())["track"] == runner.TRACK
