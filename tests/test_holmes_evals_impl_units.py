"""#666 PR-A implementer unit checks. Synthetic question directories only;
no real upstream ``expected_output`` is read, no model is called."""

from __future__ import annotations

import ast
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opspilot.evaluation.binary_judge import (
    ANSWER_INSTRUCTION,
    UPSTREAM_STRICT_PROMPT,
    BinaryJudgeError,
    ProviderConfig,
)
from scripts.holmes_evals import judge, questions, scoring, text

UPSTREAM = Path(
    os.environ.get("HOLMES_UPSTREAM_ROOT")
    or Path(__file__).resolve().parents[1] / "tmp/upstream/holmesgpt-a0009195"
)
SECRET = "SYNTHETIC-EXPECTED-ELEMENT-7f3a"


def _question(root: Path, qid: str, body: str) -> Path:
    d = root / questions.QUESTIONS_DIR / qid
    d.mkdir(parents=True)
    (d / "test_case.yaml").write_text(body)
    return d


def _config() -> ProviderConfig:
    return ProviderConfig("glm", "zai", "http://127.0.0.1:1/x", "glm-5.3-flashx", "k")


def _completion(choice: str, rationale: str = "because") -> bytes:
    content = json.dumps({"rationale": rationale, "choice": choice})
    return json.dumps(
        {
            "model": "glm-5.3-flashx",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        }
    ).encode()


class FakeClient:
    def __init__(self, replies: list[bytes | Exception]) -> None:
        self.replies = replies
        self.bodies: list[bytes] = []

    def complete(self, config: ProviderConfig, body: bytes) -> bytes:
        self.bodies.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


# -- questions --------------------------------------------------------------


def test_question_lists() -> None:
    assert len(questions.B1_QUESTIONS) == len(set(questions.B1_QUESTIONS)) == 34
    assert len(questions.MULTI_CLUSTER) == 10
    assert questions.MULTI_CLUSTER <= set(questions.B1_QUESTIONS)
    assert questions.SKILL_ASSERTION_DROPPED <= set(questions.B1_QUESTIONS)
    for excluded in ("271", "274", "279"):
        assert not any(q.startswith(excluded + "_") for q in questions.B1_QUESTIONS)


def test_loader_keeps_only_whitelist(tmp_path: Path) -> None:
    _question(
        tmp_path,
        "900_synthetic",
        f"user_prompt: what broke?\nexpected_output:\n  - {SECRET}\n"
        f"description: {SECRET}\nexpected_replay_output: {SECRET}\n"
        f"expected_skill_updates: [{SECRET}]\ncluster_name: prod-a\n"
        "evaluation:\n  correctness:\n    type: loose\n",
    )
    q = questions.load_question(tmp_path, "900_synthetic")
    assert SECRET not in repr(q)
    assert set(q.__dataclass_fields__) == set(questions.WHITELIST)
    assert q.cluster_name == "prod-a" and q.include_tool_calls is False
    assert questions.judge_mode(q) == "loose"


def test_loader_rejects_path_ids(tmp_path: Path) -> None:
    for bad in ("..", "a/b", ""):
        with pytest.raises(ValueError):
            questions.load_question(tmp_path, bad)


def test_verify_upstream_synthetic(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text(questions.UPSTREAM_COMMIT + "\n")
    with pytest.raises(questions.UpstreamMismatch) as err:
        questions.verify_upstream(tmp_path)
    assert err.value.code == "UPSTREAM_CLASSIFIERS_MISSING"
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".git/refs/heads").mkdir(parents=True)
    (tmp_path / ".git/refs/heads/main").write_text("0" * 40 + "\n")
    with pytest.raises(questions.UpstreamMismatch) as err:
        questions.verify_upstream(tmp_path)
    assert err.value.code == "UPSTREAM_HEAD_MISMATCH"


@pytest.mark.skipif(not UPSTREAM.is_dir(), reason="upstream checkout absent")
def test_pins_match_real_checkout() -> None:
    questions.verify_upstream(UPSTREAM)
    src = (UPSTREAM / questions.CLASSIFIERS_PATH).read_text()
    prompts = [
        n.value
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "You are evaluating" in n.value
    ]
    assert prompts == [UPSTREAM_STRICT_PROMPT, judge.UPSTREAM_LOOSE_PROMPT]
    for qid in questions.B1_QUESTIONS:
        assert (UPSTREAM / questions.QUESTIONS_DIR / qid / "test_case.yaml").is_file()


# -- text -------------------------------------------------------------------


def test_tool_call_block_layout() -> None:
    calls = [
        text.ToolCall("es_search", {"b": 1, "a": "x"}, {"hits": ["l1\nl2"]}),
        text.ToolCall("es_mappings", {}, None, "SCOPE_DENIED", "outside window"),
        text.ToolCall("es_list", {"q": 1}, None),
    ]
    out = text.format_tool_calls(calls)
    assert out.startswith('\n\n# Tool Calls\n\n* Tool #1: es_search({"a":"x","b":1})\n')
    assert 'Output:\n  {\n    "hits": [\n' in out
    assert (
        "* Tool #2: es_mappings({})\nOutput:\n  REFUSED SCOPE_DENIED: outside window\n---\n"
        in out
    )
    assert out.endswith('* Tool #3: es_list({"q":1})\n---\n')
    assert text.format_tool_calls([]) == ""


def test_report_to_text_without_report() -> None:
    out = text.report_to_text(None, [text.ToolCall("t", {}, "x")], None)
    assert out.text is None and out.reason == "REPORT_REJECTED_BY_PRODUCT"


BASE = datetime(2026, 11, 18, 10, 0, 0, tzinfo=UTC)
OFFSET = 400 * 86400 + 37
SHIFT = text.Shift(OFFSET, BASE, BASE + timedelta(hours=2))


@pytest.mark.parametrize(
    ("shifted", "original"),
    [
        ("2026-11-18T10:00:37Z", "2025-10-14T10:00:00Z"),
        ("2026-11-18T10:00:37.250Z", "2025-10-14T10:00:00.250Z"),
        ("2026-11-18 10:00:37", "2025-10-14 10:00:00"),
        ("2026-11-18T11:00:37+01:00", "2025-10-14T11:00:00+01:00"),
        # Minute precision: whole minutes of the offset only.
        ("2026-11-18T10:00", "2025-10-14T10:00"),
        ("2026-11-18", "2025-10-14"),
    ],
)
def test_deshift_in_range(shifted: str, original: str) -> None:
    assert text.deshift(f"at {shifted}.", SHIFT) == (f"at {original}.", 1)


@pytest.mark.parametrize(
    "untouched",
    [
        "2024-01-15T10:15:00.050Z",  # embedded original-era time, out of range
        "2026-11-18T14:00:01Z",  # past the +1 h margin
        "2026-11-18T10:00:00-05:00",  # in UTC, out of range
        "app-185-eu-2026.11.18",  # dotted index-name date
        "14:00",  # time of day without a date
    ],
)
def test_deshift_leaves_out_of_range(untouched: str) -> None:
    assert text.deshift(untouched, SHIFT) == (untouched, 0)


def test_deshift_range_and_none() -> None:
    a, b = "2026-11-18T11:00:37", "2026-11-18T12:00:37"
    assert text.deshift(f"{a}-{b}", SHIFT) == (
        "2025-10-14T11:00:00-2025-10-14T12:00:00",
        2,
    )
    assert text.deshift(a, None) == (a, 0)


# -- judge ------------------------------------------------------------------


def test_prompt_hash_and_messages() -> None:
    import hashlib

    assert (
        judge.HOLMES_EVAL_PROMPT_SHA256
        == hashlib.sha256(
            "\0".join(
                (
                    UPSTREAM_STRICT_PROMPT,
                    judge.UPSTREAM_LOOSE_PROMPT,
                    ANSWER_INSTRUCTION,
                )
            ).encode()
        ).hexdigest()
    )
    [msg] = judge.judge_messages(["e1", "e2"], "OUT {{expected}}", "strict")
    content = msg["content"]
    assert "- e1\n- e2\n" in content and "OUT {{expected}}" in content
    assert content.endswith(ANSWER_INSTRUCTION)
    [loose] = judge.judge_messages(["e1"], "o", "loose")
    assert "# EXPECTED\n\ne1\n" in loose["content"]


def test_judge_trial_retries_and_isolates(tmp_path: Path) -> None:
    _question(tmp_path, "901_synth", f"user_prompt: q\nexpected_output: [{SECRET}]\n")
    client = FakeClient(
        [BinaryJudgeError("JUDGE_TIMEOUT"), b"not json", _completion("A", SECRET)]
    )
    out = judge.judge_trial(
        client, _config(), tmp_path, "901_synth", "report", tmp_path / "r"
    )
    assert (out.choice, out.passed, out.error_code, out.attempts) == (
        "A",
        True,
        None,
        3,
    )
    assert SECRET not in repr(out)
    assert SECRET.encode() in client.bodies[0]
    assert out.request_bytes == len(client.bodies[0])
    assert SECRET in (tmp_path / "r" / judge.RATIONALE_FILE).read_text()


def test_judge_trial_exhausted_and_oversize(tmp_path: Path) -> None:
    _question(tmp_path, "902_synth", "user_prompt: q\nexpected_output: x\n")
    client = FakeClient([BinaryJudgeError("JUDGE_HTTP_ERROR")] * 3)
    out = judge.judge_trial(
        client, _config(), tmp_path, "902_synth", "r", tmp_path / "a"
    )
    assert (out.passed, out.error_code, out.attempts) == (False, "JUDGE_HTTP_ERROR", 3)
    small = ProviderConfig(
        "glm", "zai", "http://x", "glm-5.3-flashx", "k", max_request_bytes=10
    )
    idle = FakeClient([])
    out = judge.judge_trial(idle, small, tmp_path, "902_synth", "r", tmp_path / "b")
    assert (out.error_code, out.attempts, idle.bodies) == (
        "JUDGE_REQUEST_OVERSIZE",
        0,
        [],
    )
    assert out.request_bytes > 10


def test_zai_config() -> None:
    cfg = judge.zai_config({"GLM_API_KEY": "k"})
    assert cfg.model == "glm-5.3-flashx" and cfg.vendor == "zai"
    with pytest.raises(BinaryJudgeError):
        judge.zai_config({})
    with pytest.raises(BinaryJudgeError):
        judge.zai_config({"ZAI_API_KEY": "k", "ZAI_BASE_URL": "http://evil"})


# -- scoring ----------------------------------------------------------------


def test_score_rows() -> None:
    qs = ["259_wrong_cluster_logs_confusion", "185_x", "189_y"]
    T = scoring.Trial
    trials = [
        T(qs[0], 1, True, "ok"),
        T(qs[0], 2, True, "ok"),
        T(qs[0], 3, False, "ok"),
        T(qs[0], 4, True, "ok"),
        T(qs[1], 1, True, "ok", True),
        T(qs[1], 2, True, "no_report", True),  # inconsistent pass still 0
        T(qs[1], 3, False, "judge_exhausted", True),
        T(qs[2], 1, False, "env_broken"),
        T(qs[2], 2, True, "ok"),
    ]
    out = scoring.score(trials, qs)
    assert out["main"] == {"passes": 4, "denominator": 9, "rate": 4 / 9}
    assert out["swing_questions"] == qs
    assert out["rerun"]["passes"] == 1 and out["rerun"]["in_main_score"] is False
    assert out["multi_cluster"]["questions"] == [qs[0]]
    assert out["time_box_limited"]["questions"] == [qs[1]]
    assert out["excluding_env_broken"]["excluded"] == [qs[2]]
    assert out["excluding_env_broken"]["denominator"] == 6
    assert out["missing_trials"] == [f"{qs[2]}#3"]
    assert out["zero_scored"]["env_broken"] == [f"{qs[2]}#1"]
    with pytest.raises(ValueError):
        scoring.score([T(qs[0], 1, True, "ok")] * 2, qs)
    with pytest.raises(ValueError):
        scoring.score([T("other", 1, True, "ok")], qs)
    with pytest.raises(ValueError):
        scoring.score([T(qs[0], 7, True, "ok")], qs)
