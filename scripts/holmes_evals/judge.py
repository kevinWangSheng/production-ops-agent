"""Binary judge for the HolmesGPT public eval track (#666 H-16).

One judge, Z.ai ``glm-5.3-flashx``, through ``binary_judge``'s provider config,
HTTP client and completion parser. The prompt is upstream's own strict or
loose correctness template (``tests/llm/utils/classifiers.py`` at a0009195,
chosen by the question's ``evaluation.correctness.type``, strict by default),
``{{expected}}`` filled with ``"\\n- ".join(expected elements)`` as upstream
does, followed by ``binary_judge.ANSWER_INSTRUCTION`` (rationale and choice as
one json object, in place of upstream's chain-of-thought tool call). No M3
fault/normal question is added.

Answer isolation (R8-6): :func:`judge_trial` is the only reader of a
question's ``expected_output``. It returns the choice and bookkeeping only;
the judge's rationale, which is answer-derived, goes to a file under the
caller's ``rationale_dir`` (a git-ignored ``tmp/`` path) and is never
returned or printed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from opspilot.evaluation.binary_judge import (
    ANSWER_INSTRUCTION,
    UPSTREAM_STRICT_PROMPT,
    BinaryJudgeError,
    CompletionClient,
    ProviderConfig,
    parse_completion,
    request_body,
)
from scripts.holmes_evals.questions import judge_mode, load_question, read_test_case

Mode = Literal["strict", "loose"]

# Upstream HolmesGPT a0009195, tests/llm/utils/classifiers.py, loose prompt,
# verbatim (autoevals renders {{expected}} and {{output}}).
UPSTREAM_LOOSE_PROMPT = """
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

#: Pins both upstream templates and the answer instruction.
HOLMES_EVAL_PROMPT_SHA256 = hashlib.sha256(
    "\0".join(
        (UPSTREAM_STRICT_PROMPT, UPSTREAM_LOOSE_PROMPT, ANSWER_INSTRUCTION)
    ).encode()
).hexdigest()

JUDGE_NAME = "glm"
JUDGE_MODEL = "glm-5.3-flashx"
ZAI_BASE = "https://api.z.ai/api/paas/v4"
ZAI_KEY_NAMES = ("ZAI_API_KEY", "GLM_API_KEY")
#: L-D9: a failed judgement is retried at most twice; an oversize request is
#: not retried.
JUDGE_TRIES = 3
RATIONALE_FILE = "rationale.json"
_NOT_RETRIED = frozenset({"JUDGE_REQUEST_OVERSIZE"})


def _template(mode: Mode) -> str:
    if mode == "strict":
        prompt = UPSTREAM_STRICT_PROMPT
    elif mode == "loose":
        prompt = UPSTREAM_LOOSE_PROMPT
    else:
        raise ValueError("JUDGE_MODE_INVALID")
    return prompt + ANSWER_INSTRUCTION


def judge_messages(
    expected: Sequence[str], output: str, mode: Mode
) -> list[dict[str, str]]:
    """The one request the judge gets for one trial's text."""
    if isinstance(expected, str):
        expected = [expected]
    filled = "\n- ".join(expected)
    head, tail = _template(mode).split("{{output}}")
    # Output substituted last, once: report text cannot fill another slot.
    prompt = head.replace("{{expected}}", filled) + output + tail
    return [{"role": "user", "content": prompt}]


def zai_config(environ: Mapping[str, str]) -> ProviderConfig:
    """The single judge's config; error codes name variables, never values."""
    key = next((environ[n] for n in ZAI_KEY_NAMES if environ.get(n, "").strip()), "")
    if not key or any(c in key for c in "\r\n\x00"):
        raise BinaryJudgeError(f"JUDGE_KEY_MISSING:{'|'.join(ZAI_KEY_NAMES)}")
    if environ.get("ZAI_BASE_URL", ZAI_BASE) != ZAI_BASE:
        raise BinaryJudgeError("JUDGE_BASE_URL_NOT_ALLOWED:ZAI_BASE_URL")
    return ProviderConfig(
        JUDGE_NAME, "zai", ZAI_BASE + "/chat/completions", JUDGE_MODEL, key
    )


@dataclass(frozen=True)
class JudgeResult:
    """No rationale and no expected elements: those stay in ``rationale_dir``."""

    choice: Literal["A", "B"] | None
    passed: bool
    error_code: str | None
    attempts: int
    prompt_sha256: str
    request_bytes: int


def _expected_elements(root: Path, qid: str) -> list[str]:
    doc = read_test_case(root, qid)
    expected = doc.get("expected_output")
    del doc
    if isinstance(expected, str):
        return [expected]
    if (
        isinstance(expected, list)
        and expected
        and all(isinstance(e, str) for e in expected)
    ):
        return list(expected)
    raise ValueError("EXPECTED_OUTPUT_INVALID")


def _write_rationale(rationale_dir: Path, record: Mapping[str, Any]) -> None:
    rationale_dir.mkdir(parents=True, exist_ok=True)
    path = rationale_dir / RATIONALE_FILE
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n")
    partial.replace(path)


def judge_trial(
    client: CompletionClient,
    config: ProviderConfig,
    root: Path,
    qid: str,
    output: str,
    rationale_dir: Path,
) -> JudgeResult:
    """Judge one trial's text against the question's expected elements.

    Up to :data:`JUDGE_TRIES` attempts; ``passed`` is False whenever no
    verdict was obtained (the trial scores 0, H-18). The rationale file is
    written for every outcome.
    """
    mode = judge_mode(load_question(root, qid))
    messages = judge_messages(_expected_elements(root, qid), output, mode)
    # binary_judge.request_body refuses an oversize body without returning it;
    # build it without that cap to measure, then apply the configured cap.
    body = request_body(dataclasses.replace(config, max_request_bytes=2**63), messages)
    del messages
    size = len(body)
    log: list[dict[str, Any]] = []
    choice: Literal["A", "B"] | None = None
    error: str | None = None
    attempts = 0
    if size > config.max_request_bytes:
        error = "JUDGE_REQUEST_OVERSIZE"
    else:
        while attempts < JUDGE_TRIES:
            attempts += 1
            try:
                raw = client.complete(config, body)
                passed, rationale, model, usage = parse_completion(config, raw)
            except BinaryJudgeError as exc:
                error = exc.code
            except Exception:  # noqa: BLE001 - recorded as a fixed code
                error = "JUDGE_TRANSPORT_ERROR"
            else:
                error = None
                choice = "A" if passed else "B"
                log.append(
                    {
                        "attempt": attempts,
                        "choice": choice,
                        "rationale": rationale,
                        "response_model": model,
                        "usage": usage,
                    }
                )
                break
            log.append({"attempt": attempts, "error_code": error})
            if error in _NOT_RETRIED:
                break
    _write_rationale(
        rationale_dir,
        {
            "qid": qid,
            "mode": mode,
            "judge": config.public(),
            "prompt_sha256": HOLMES_EVAL_PROMPT_SHA256,
            "request_bytes": size,
            "choice": choice,
            "error_code": error,
            "attempts": log,
        },
    )
    return JudgeResult(
        choice=choice,
        passed=choice == "A",
        error_code=error,
        attempts=attempts,
        prompt_sha256=HOLMES_EVAL_PROMPT_SHA256,
        request_bytes=size,
    )
