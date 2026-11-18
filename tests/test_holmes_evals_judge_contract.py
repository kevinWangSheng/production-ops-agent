"""#666 PR-A contract: judge prompts and one judged trial (H-16, H-15, H-3).

Independent contract tests written from the task record's PR-A public
interface and the pre-review, without reading ``scripts/holmes_evals/``.
Red until the implementation lands; collection stays green (lazy import).
No network: ``judge_trial`` gets a fake ``CompletionClient``.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``judge_messages`` returns one user message, as
  ``binary_judge.judge_messages`` does: template (strict or loose) +
  ``ANSWER_INSTRUCTION``, ``{{expected}}`` filled with ``"\\n- ".join``,
  output substituted last and once;
* ``judge_trial`` sends exactly ``judge_messages(expected, output, mode)``
  and does not itself call ``verify_upstream`` (the root here is synthetic);
* "失败重试最多 2 次" means at most 3 client calls (ITBench precedent
  ``JUDGE_TRIES = 3``); ``binary_judge.MAX_ATTEMPTS`` (2) also passes, so
  only ``2 <= calls <= 3`` is asserted.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import uuid
from pathlib import Path

import pytest

from opspilot.evaluation import binary_judge as bj
from tests.holmes_evals_support import CLASSIFIERS, mod, upstream_root, write_question

# Upstream a0009195 tests/llm/utils/classifiers.py, loose template, copied by
# hand from the file text; checked against the file below when present.
LOOSE_LITERAL = """
You are evaluating the correctness of an OUTPUT given by a LLM. You must return a score that
represents the correctness of that OUTPUT.

The correctness is defined by the presence of EXPECTED in the OUTPUT.
Make a judgement call whether each ELEMENT sufficiently matches the OUTPUT. ELEMENTS do
not need to appear verbatim or be a perfect match but their essence should be
present in the whole OUTPUT, even if it spans multiple sentences.

# EXPECTED

{{expected}}

# OUTPUT

{{output}}


Return a choice based on the number of EXPECTED presence in the OUTPUT.
Possible choices:
- A: The OUTPUT reasonably matches the EXPECTED content
- B: The OUTPUT does not match the EXPECTED content
"""


def _upstream_templates() -> tuple[str, str] | None:
    """(strict, loose) from classifiers.py via ast, without importing it."""
    root = upstream_root()
    if root is None:
        return None
    tree = ast.parse((root / CLASSIFIERS).read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "evaluate_correctness"
    )
    strict = loose = None
    for node in fn.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "prompt_prefix"
        ):
            strict = node.value.value
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "evaluation_type"
            and node.test.comparators[0].value == "loose"
        ):
            loose = node.body[0].value.value
    assert strict is not None and loose is not None
    return strict, loose


def _prompt(template: str, expected: list[str], output: str) -> str:
    head, tail = (template + bj.ANSWER_INSTRUCTION).split("{{output}}")
    return head.replace("{{expected}}", "\n- ".join(expected)) + output + tail


# --------------------------------------------------------------------------
# 7. Prompts


def test_loose_literal_matches_upstream_file():
    templates = _upstream_templates()
    if templates is None:
        pytest.skip("pinned upstream checkout not on this machine")
    strict, loose = templates
    assert loose == LOOSE_LITERAL
    assert strict == bj.UPSTREAM_STRICT_PROMPT


def test_upstream_loose_prompt_verbatim():
    judge = mod("judge")
    assert judge.UPSTREAM_LOOSE_PROMPT == LOOSE_LITERAL


def test_prompt_hash_constant():
    judge = mod("judge")
    digest = judge.HOLMES_EVAL_PROMPT_SHA256
    assert isinstance(digest, str)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    # Different template set from the M3 judge (strict + FAULT/NORMAL).
    assert digest != bj.PROMPT_SHA256


EXPECTED = [
    "SYNTH-EXP-1 the checkout service logged HTTP 500",
    "SYNTH-EXP-2 errors began after the deploy",
]
OUTPUT = "Assessment: completed\nConclusion: supported\n\nSummary:\nsee {{expected}} literal\n"


@pytest.mark.parametrize(
    ("mode", "template"),
    [("strict", bj.UPSTREAM_STRICT_PROMPT), ("loose", LOOSE_LITERAL)],
)
def test_judge_messages(mode, template):
    judge = mod("judge")
    got = judge.judge_messages(EXPECTED, OUTPUT, mode)
    assert got == [{"role": "user", "content": _prompt(template, EXPECTED, OUTPUT)}]
    content = got[0]["content"]
    assert "\n- ".join(EXPECTED) in content
    # Output substituted last: its literal "{{expected}}" is left alone.
    assert "see {{expected}} literal" in content
    assert "{{output}}" not in content
    assert "FAULT" not in content and "No fault was injected" not in content


# --------------------------------------------------------------------------
# 8. judge_trial

MODEL = "glm-5.3-flashx"
QID_STRICT = "183c_elasticsearch_log_search"
QID_LOOSE = "190_elasticsearch_cross_service_correlation"
QID_SKILL = "275_es_field_name_correction"


def _config() -> bj.ProviderConfig:
    return bj.ProviderConfig(
        "glm",
        "zai",
        "https://api.z.ai/api/paas/v4/chat/completions",
        MODEL,
        "synthetic-key",
    )


def _completion(content: str, model: str = MODEL) -> bytes:
    return json.dumps(
        {
            "id": "c1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode()


class FakeClient:
    def __init__(self, *replies):
        """Each reply: bytes to return, or an exception to raise. The last
        reply repeats."""
        self.replies = list(replies)
        self.bodies: list[bytes] = []

    def complete(self, config, body: bytes) -> bytes:
        self.bodies.append(body)
        reply = self.replies[min(len(self.bodies), len(self.replies)) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def messages(self, i: int = 0):
        return json.loads(self.bodies[i])["messages"]


def _verdict(choice: str, rationale: str) -> bytes:
    return _completion(json.dumps({"rationale": rationale, "choice": choice}))


def _root(tmp_path: Path, qid: str = QID_STRICT, **extra) -> Path:
    root = tmp_path / "upstream"
    doc = {"user_prompt": "Synthetic question?", "expected_output": list(EXPECTED)}
    doc.update(extra)
    write_question(root, qid, doc)
    return root


def _fields(result) -> set[str]:
    if dataclasses.is_dataclass(result):
        return {f.name for f in dataclasses.fields(result)}
    return set(result._fields)


def test_judge_trial_pass_and_request(tmp_path):
    judge = mod("judge")
    root = _root(tmp_path)
    rationale_dir = tmp_path / "answer-derived"
    rationale = f"SYNTH-RATIONALE-{uuid.uuid4().hex}"
    client = FakeClient(_verdict("A", rationale))

    result = judge.judge_trial(
        client, _config(), root, QID_STRICT, OUTPUT, rationale_dir
    )

    assert _fields(result) == {
        "choice",
        "passed",
        "error_code",
        "attempts",
        "prompt_sha256",
        "request_bytes",
    }
    assert result.choice == "A"
    assert result.passed is True
    assert result.error_code is None
    assert result.attempts == 1
    assert result.prompt_sha256 == judge.HOLMES_EVAL_PROMPT_SHA256
    assert len(client.bodies) == 1
    assert result.request_bytes == len(client.bodies[0])
    body = json.loads(client.bodies[0])
    assert body["model"] == MODEL
    assert body["messages"] == judge.judge_messages(EXPECTED, OUTPUT, "strict")


def test_judge_trial_choice_b_fails(tmp_path):
    judge = mod("judge")
    client = FakeClient(_verdict("B", "SYNTH-RATIONALE-b"))
    result = judge.judge_trial(
        client, _config(), _root(tmp_path), QID_STRICT, OUTPUT, tmp_path / "r"
    )
    assert result.choice == "B"
    assert result.passed is False
    assert result.error_code is None


def test_rationale_only_under_rationale_dir(tmp_path):
    judge = mod("judge")
    root = _root(tmp_path)
    rationale_dir = tmp_path / "answer-derived"
    rationale = f"SYNTH-RATIONALE-{uuid.uuid4().hex}"
    result = judge.judge_trial(
        FakeClient(_verdict("A", rationale)),
        _config(),
        root,
        QID_STRICT,
        OUTPUT,
        rationale_dir,
    )

    holders = [
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and rationale in p.read_text(errors="ignore")
    ]
    assert holders, "rationale was not written"
    assert all(rationale_dir in p.parents for p in holders), holders

    rendered = repr(result) + str(result)
    values = [str(getattr(result, f)) for f in _fields(result)]
    assert rationale not in rendered
    assert all(rationale not in v for v in values)
    for element in EXPECTED:
        assert element not in rendered


def test_loose_question_uses_loose_template(tmp_path):
    judge = mod("judge")
    root = _root(tmp_path, QID_LOOSE, evaluation={"correctness": {"type": "loose"}})
    client = FakeClient(_verdict("A", "r"))
    judge.judge_trial(client, _config(), root, QID_LOOSE, OUTPUT, tmp_path / "r")
    assert client.messages() == judge.judge_messages(EXPECTED, OUTPUT, "loose")
    assert client.messages()[0]["content"] == _prompt(LOOSE_LITERAL, EXPECTED, OUTPUT)


def test_string_expected_output_is_one_element(tmp_path):
    # Upstream wraps a string expected_output in a list (property_manager).
    judge = mod("judge")
    root = tmp_path / "upstream"
    write_question(
        root,
        QID_STRICT,
        {"user_prompt": "Synthetic?", "expected_output": "SYNTH-EXP-single element"},
    )
    client = FakeClient(_verdict("A", "r"))
    judge.judge_trial(client, _config(), root, QID_STRICT, OUTPUT, tmp_path / "r")
    assert client.messages() == judge.judge_messages(
        ["SYNTH-EXP-single element"], OUTPUT, "strict"
    )


def test_transport_failures_exhaust_retries(tmp_path):
    judge = mod("judge")
    client = FakeClient(bj.BinaryJudgeError("JUDGE_TRANSPORT_ERROR"))
    result = judge.judge_trial(
        client, _config(), _root(tmp_path), QID_STRICT, OUTPUT, tmp_path / "r"
    )
    assert 2 <= len(client.bodies) <= 3
    assert result.attempts == len(client.bodies)
    assert isinstance(result.error_code, str) and result.error_code
    assert not result.passed
    assert result.choice != "A"


def test_transient_failure_then_success(tmp_path):
    judge = mod("judge")
    client = FakeClient(
        bj.BinaryJudgeError("JUDGE_TRANSPORT_ERROR"), _verdict("A", "r")
    )
    result = judge.judge_trial(
        client, _config(), _root(tmp_path), QID_STRICT, OUTPUT, tmp_path / "r"
    )
    assert len(client.bodies) == 2
    assert result.attempts == 2
    assert result.passed is True
    assert result.error_code is None


def test_invalid_output_bounded(tmp_path):
    judge = mod("judge")
    client = FakeClient(_completion("not json at all"))
    result = judge.judge_trial(
        client, _config(), _root(tmp_path), QID_STRICT, OUTPUT, tmp_path / "r"
    )
    assert 1 <= len(client.bodies) <= 3
    assert isinstance(result.error_code, str) and result.error_code
    assert not result.passed


def test_oversize_request_never_sent(tmp_path):
    judge = mod("judge")
    client = FakeClient(_verdict("A", "r"))
    huge = "x" * (4 * 1024 * 1024 + 1)
    result = judge.judge_trial(
        client, _config(), _root(tmp_path), QID_STRICT, huge, tmp_path / "r"
    )
    assert client.bodies == []
    assert result.error_code == "JUDGE_REQUEST_OVERSIZE"
    assert not result.passed


def test_skill_assertion_dropped_question_has_no_suggested_skills(tmp_path):
    judge = mod("judge")
    questions = mod("questions")
    assert QID_SKILL in questions.SKILL_ASSERTION_DROPPED
    root = _root(tmp_path, QID_SKILL, memories_generated=True, rerun_with_memory=True)
    client = FakeClient(_verdict("A", "r"))
    judge.judge_trial(client, _config(), root, QID_SKILL, OUTPUT, tmp_path / "r")
    assert len(client.bodies) == 1  # no rerun_with_memory replay
    content = "".join(m["content"] for m in client.messages())
    assert "# Suggested Skills" not in content
    assert "SuggestSkills" not in content
    assert client.messages() == judge.judge_messages(EXPECTED, OUTPUT, "strict")
