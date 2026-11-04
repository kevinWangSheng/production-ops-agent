"""Q9 binary judge for the F1 closure comparison (S7a).

Two judges from different vendors (z.ai GLM and OpenAI, neither a candidate
model) each give one pass/fail per report. Both systems get the same prompt
and the same input projection: the scenario's expected elements plus the
report as text. OpsPilot's ReportV2 goes through
:func:`project_opspilot_report`; HolmesGPT's final answer is used as written.
When the two judges disagree, or one gives no verdict, the item goes to the
user once.

The prompt is HolmesGPT's own strict correctness prompt at the pinned
upstream commit (``tests/llm/utils/classifiers.py``, ``evaluate_correctness``
with ``evaluation_type="strict"``: choice A = all expected elements present),
followed by the Q9 question (fault: root cause substantively found; no
fault: no false attribution) and the answer format.

Q11 deterministic checks veto first. Scenario rows the closure contract
marks "deterministic only" never reach a judge. OpsPilot gets one more
column, the root-cause evidence citation rate (pre-review section 1 D). It
is reported for OpsPilot only and never enters the comparison denominator.

Credentials are read only by :func:`provider_configs`, and keys travel only
in the ``Authorization`` header of :class:`HTTPCompletionClient`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

from opspilot.evaluation.gate import report_signature_matches
from opspilot.evaluation.model import DeliveredRecord, Window
from opspilot.investigation.reports import ReportV2

Vendor = Literal["zai", "openai"]

#: R10: two vendors, neither a candidate model. ``name`` keys an attempt's
#: ``verdicts``. Models are the P7 adapters' (judge.py ``_MODELS``).
JUDGES: tuple[Mapping[str, str], ...] = (
    {"name": "glm", "vendor": "zai", "model": "glm-5.3-flashx"},
    {"name": "openai", "vendor": "openai", "model": "gpt-5.6-luna"},
)
JUDGE_NAMES = tuple(j["name"] for j in JUDGES)

# Upstream HolmesGPT a0009195, tests/llm/utils/classifiers.py, strict prompt,
# verbatim (autoevals renders {{expected}} and {{output}}).
UPSTREAM_STRICT_PROMPT = """
You are evaluating the correctness of an OUTPUT given by a LLM. You must return a score that
represents the correctness of that OUTPUT.

The correctness is defined by the presence of EXPECTED ELEMENTS in the OUTPUT.
Make a judgement call whether each ELEMENT sufficiently matches the OUTPUT. ELEMENTS do
not need to appear verbatim or be a perfect match but their essence should be
present in the whole OUTPUT, even if it spans multiple sentences.

# EXPECTED ELEMENTS

- {{expected}}

# OUTPUT

{{output}}


Return a choice based on the number of EXPECTED ELEMENTS present in the OUTPUT.
Possible choices:
- A: All elements are presents
- B: Either no element is present or only some but not all elements are present
"""
# Q9: one binary question per report, by whether a fault was injected.
FAULT_QUESTION = (
    "\nA fault was injected in this scenario. Choose A only if the OUTPUT "
    "substantively identifies the root cause the EXPECTED ELEMENTS describe.\n"
)
NORMAL_QUESTION = (
    "\nNo fault was injected on the investigated path in this scenario. "
    "Choose A only if the OUTPUT attributes no failure to a cause that the "
    "EXPECTED ELEMENTS do not describe.\n"
)
# Upstream asks autoevals for a chain-of-thought tool call (use_cot=True:
# rationale, then choice); a strict json object carries the same two fields
# on both endpoints.
ANSWER_INSTRUCTION = (
    "\nAnswer with one json object and nothing else: "
    '{"rationale": "<step-by-step reasoning, written before choosing>", '
    '"choice": "A" or "B"}\n'
)
PROMPT_SHA256 = hashlib.sha256(
    "\0".join(
        (UPSTREAM_STRICT_PROMPT, FAULT_QUESTION, NORMAL_QUESTION, ANSWER_INSTRUCTION)
    ).encode()
).hexdigest()

_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "choice": {"type": "string", "enum": ["A", "B"]},
    },
    "required": ["rationale", "choice"],
    "additionalProperties": False,
}

# Closure contract r1, six-category table, column 判法: these rows are
# deterministic only. Regression rows are fault/normal judge rows.
_DETERMINISTIC_ONLY = frozenset(
    {
        ("missing_telemetry", "positive"),
        ("duplicate_event", "negative"),
        ("model_failure", "positive"),
        ("wrong_target", "positive"),
    }
)
MAX_ATTEMPTS = 2


class BinaryJudgeError(ValueError):
    """A fixed, credential-free code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# --------------------------------------------------------------------------
# What is judged, and how it is projected


def judge_applicable(answer: Any) -> bool:
    return (answer.category, answer.polarity) not in _DETERMINISTIC_ONLY


def _template(answer: Any) -> str:
    question = FAULT_QUESTION if answer.signature is not None else NORMAL_QUESTION
    return UPSTREAM_STRICT_PROMPT + question + ANSWER_INSTRUCTION


def judge_messages(answer: Any, report_text: str) -> list[dict[str, str]]:
    """The one request every judge gets for one report of either system."""
    expected = "\n- ".join(answer.expected_output)
    head, tail = _template(answer).split("{{output}}")
    # Output substituted last, once: report text cannot fill another slot.
    prompt = head.replace("{{expected}}", expected) + report_text + tail
    return [{"role": "user", "content": prompt}]


_KIND_LABELS = {
    "fact": "Finding",
    "hypothesis": "Hypothesis",
    "recommendation": "Recommendation",
    "counter_evidence": "Counter-evidence",
    "rejected_hypothesis": "Rejected hypothesis",
}


def project_opspilot_report(report: ReportV2) -> str:
    """ReportV2 as plain text, fields in the report's own order.

    Opaque evidence IDs become ``E<n>``. The text is kept verbatim, so the
    judge sees the same words a reader of the report sees.
    """
    numbers: dict[str, int] = {}
    for claim in report.claims:
        for eid in claim.evidence_ids:
            numbers.setdefault(eid, len(numbers) + 1)
    lines = [
        f"Assessment: {report.assessment_status}",
        f"Conclusion: {report.conclusion}",
        "",
        "Summary:",
        report.summary,
    ]
    if report.claims:
        lines += ["", "Statements:"]
        for claim in report.claims:
            cites = ", ".join(f"E{numbers[eid]}" for eid in claim.evidence_ids)
            suffix = f" [evidence: {cites}]" if cites else ""
            lines.append(f"- {_KIND_LABELS[claim.kind]}: {claim.text}{suffix}")
    for title, values in (("Gaps", report.gaps), ("Next steps", report.next_steps)):
        if values:
            lines += ["", f"{title}:"]
            lines += [f"- {value}" for value in values]
    return "\n".join(lines) + "\n"


def project_holmes_output(output: Mapping[str, Any]) -> str | None:
    """HolmesGPT's final answer (``result``) as written; blank or absent: None."""
    result = output.get("result")
    if not isinstance(result, str) or not result.strip():
        return None
    return result


def root_cause_citation(
    answer: Any,
    report: ReportV2 | None,
    delivered: Mapping[str, DeliveredRecord],
    authorized: Mapping[str, Window],
    scope: Any,
) -> bool | None:
    """Section 1 D: conclusion supported/partial and a fact or hypothesis
    claim cites a signature-hit record with the gate's fact bindings.

    This proves the fault evidence was cited in support. It does not prove
    the report named it the cause: "payment errors, but not the cause"
    also passes. ``None`` when the scenario has no signature.
    """
    if answer.signature is None:
        return None
    if report is None or report.conclusion not in {"supported", "partial"}:
        return False
    kept = [c for c in report.claims if c.kind in {"fact", "hypothesis"}]
    if not kept:
        return False
    narrowed = report.model_copy(update={"claims": kept})
    return bool(
        report_signature_matches(answer, narrowed, delivered, authorized, scope)
    )


# --------------------------------------------------------------------------
# Providers

_BASES: Mapping[Vendor, str] = {
    "zai": "https://api.z.ai/api/paas/v4",
    "openai": "https://api.openai.com/v1",
}
# Key names: judge.py's, plus GLM_API_KEY, the z.ai key name in the user's
# environment (lead 2026-11-04), used only when ZAI_API_KEY is unset.
_KEYS: Mapping[Vendor, tuple[str, ...]] = {
    "zai": ("ZAI_API_KEY", "GLM_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
}
_BASE_NAMES: Mapping[Vendor, str] = {"zai": "ZAI_BASE_URL", "openai": "OPENAI_BASE_URL"}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    vendor: Vendor
    endpoint: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 600.0
    max_output_tokens: int = 16384  # upstream classifier max_tokens
    max_request_bytes: int = 4 * 1024 * 1024
    max_response_bytes: int = 2 * 1024 * 1024

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vendor": self.vendor,
            "endpoint": self.endpoint,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
        }


def provider_configs(environ: Mapping[str, str]) -> tuple[ProviderConfig, ...]:
    """Both judges; error codes name variables, never values."""
    configs: list[ProviderConfig] = []
    for judge in JUDGES:
        vendor: Vendor = judge["vendor"]  # type: ignore[assignment]
        names = _KEYS[vendor]
        key = next((environ[n] for n in names if environ.get(n, "").strip()), "")
        if not key or any(c in key for c in "\r\n\x00"):
            raise BinaryJudgeError(f"JUDGE_KEY_MISSING:{'|'.join(names)}")
        base = environ.get(_BASE_NAMES[vendor], _BASES[vendor])
        if base != _BASES[vendor]:
            raise BinaryJudgeError(f"JUDGE_BASE_URL_NOT_ALLOWED:{_BASE_NAMES[vendor]}")
        configs.append(
            ProviderConfig(
                judge["name"], vendor, base + "/chat/completions", judge["model"], key
            )
        )
    return tuple(configs)


def judge_config_record() -> dict[str, Any]:
    return {
        "prompt_sha256": PROMPT_SHA256,
        "prompt_source": "HolmesGPT a0009195 tests/llm/utils/classifiers.py strict",
        "judges": [dict(j) for j in JUDGES],
        "max_output_tokens": ProviderConfig.max_output_tokens,
        "timeout_seconds": ProviderConfig.timeout_seconds,
    }


def request_body(
    config: ProviderConfig, messages: Sequence[Mapping[str, str]]
) -> bytes:
    body: dict[str, Any] = {
        "model": config.model,
        "messages": [dict(m) for m in messages],
        "stream": True,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "binary_correctness",
                "strict": True,
                "schema": _OUTPUT_SCHEMA,
            },
        },
    }
    if config.vendor == "zai":
        # As P7: GLM-5.3 defaults to max effort, near the timeout.
        body["reasoning_effort"] = "high"
        body["max_tokens"] = config.max_output_tokens
    else:
        body["stream_options"] = {"include_usage": True}
        body["max_completion_tokens"] = config.max_output_tokens
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(raw) > config.max_request_bytes:
        raise BinaryJudgeError("JUDGE_REQUEST_OVERSIZE")
    return raw


class CompletionClient(Protocol):
    """Sends one request; returns the assembled ``chat.completion`` document."""

    def complete(self, config: ProviderConfig, body: bytes) -> bytes: ...


class HTTPCompletionClient:
    """Streams through the P7 transport and SSE assembler (judge.py)."""

    def complete(self, config: ProviderConfig, body: bytes) -> bytes:
        from opspilot.evaluation.judge import (
            HTTPJudgeTransport,
            JudgeError,
            StreamTruncated,
        )

        headers = {
            "Authorization": "Bearer " + config.api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            response = HTTPJudgeTransport().send(
                endpoint=config.endpoint,
                headers=headers,
                body=body,
                timeout_seconds=config.timeout_seconds,
                max_response_bytes=config.max_response_bytes,
            )
        except TimeoutError:
            raise BinaryJudgeError("JUDGE_TIMEOUT") from None
        except StreamTruncated:
            raise BinaryJudgeError("JUDGE_TRANSPORT_ERROR") from None
        except JudgeError as exc:
            code = exc.code if exc.code.startswith("JUDGE_") else "JUDGE_OUTPUT_INVALID"
            raise BinaryJudgeError(code) from None
        except Exception:
            raise BinaryJudgeError("JUDGE_TRANSPORT_ERROR") from None
        if not 200 <= response.status_code < 300:
            raise BinaryJudgeError("JUDGE_HTTP_ERROR")
        return b"".join(response.chunks)


@dataclass(frozen=True)
class Verdict:
    name: str
    model: str
    response_model: str | None
    passed: bool | None
    rationale: str | None
    code: str | None
    attempts: int
    usage: Mapping[str, int | None]


_DATED = re.compile(r"(?P<base>.+)-(?P<day>\d{4}-\d{2}-\d{2})")
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _identity_ok(request_model: str, response_model: object) -> bool:
    if type(response_model) is not str:
        return False
    if response_model == request_model:
        return True
    match = _DATED.fullmatch(response_model)
    if match is None or match["base"] != request_model:
        return False
    try:
        date.fromisoformat(match["day"])
    except ValueError:
        return False
    return True


def parse_completion(
    config: ProviderConfig, raw: bytes
) -> tuple[bool, str, str, dict[str, int | None]]:
    try:
        doc = json.loads(raw)
    except ValueError:
        raise BinaryJudgeError("JUDGE_OUTPUT_INVALID") from None
    if not isinstance(doc, Mapping):
        raise BinaryJudgeError("JUDGE_OUTPUT_INVALID")
    if not _identity_ok(config.model, doc.get("model")):
        raise BinaryJudgeError("JUDGE_IDENTITY_MISMATCH")
    choices = doc.get("choices")
    if type(choices) is not list or len(choices) != 1:
        raise BinaryJudgeError("JUDGE_OUTPUT_INVALID")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, Mapping) else None
    if (
        not isinstance(message, Mapping)
        or choice.get("finish_reason") != "stop"
        or type(message.get("content")) is not str
    ):
        raise BinaryJudgeError("JUDGE_OUTPUT_INVALID")
    try:
        content = json.loads(message["content"])
    except ValueError:
        raise BinaryJudgeError("JUDGE_OUTPUT_INVALID") from None
    if (
        not isinstance(content, Mapping)
        or set(content) != {"rationale", "choice"}
        or content["choice"] not in ("A", "B")
        or type(content["rationale"]) is not str
    ):
        raise BinaryJudgeError("JUDGE_OUTPUT_INVALID")
    raw_usage = doc.get("usage")
    usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
    counts = {k: v if type(v := usage.get(k)) is int else None for k in _USAGE_KEYS}
    return content["choice"] == "A", content["rationale"], doc["model"], counts


def judge_once(
    messages: Sequence[Mapping[str, str]],
    config: ProviderConfig,
    client: CompletionClient,
) -> Verdict:
    """One judge's verdict; a transport failure or timeout is retried once."""
    attempts = 0
    try:
        body = request_body(config, messages)
        while True:
            attempts += 1
            try:
                raw = client.complete(config, body)
                break
            except BinaryJudgeError as exc:
                if (
                    exc.code not in {"JUDGE_TRANSPORT_ERROR", "JUDGE_TIMEOUT"}
                    or attempts >= MAX_ATTEMPTS
                ):
                    raise
        passed, rationale, model, usage = parse_completion(config, raw)
    except BinaryJudgeError as exc:
        code = exc.code
    except Exception:
        code = "JUDGE_TRANSPORT_ERROR"
    else:
        return Verdict(
            config.name, config.model, model, passed, rationale, None, attempts, usage
        )
    return Verdict(
        config.name,
        config.model,
        None,
        None,
        None,
        code,
        max(1, attempts),
        dict.fromkeys(_USAGE_KEYS),
    )


# --------------------------------------------------------------------------
# Scoring
#
# An attempt is a plain mapping (closure acceptance S7a):
#   {"attempt_id", "system": "opspilot"|"holmes", "scenario_id",
#    "judge_applicable", "verdicts": {judge name: bool | None},
#    "checks": {name: bool | None}, "root_cause_citation": bool | None}
# ``checks`` are Q11 (and, for OpsPilot, Q20) items; ``None`` = not applicable.


def vetoed(attempt: Mapping[str, Any]) -> bool:
    return any(value is False for value in attempt["checks"].values())


def disputed(attempt: Mapping[str, Any]) -> bool:
    """Judges differ or one gave no verdict (never for a vetoed attempt)."""
    if vetoed(attempt) or not attempt["judge_applicable"]:
        return False
    values = [attempt["verdicts"].get(name) for name in JUDGE_NAMES]
    return any(v is None for v in values) or len(set(values)) != 1


def attempt_pass(
    attempt: Mapping[str, Any], rulings: Mapping[str, bool]
) -> bool | None:
    """Q11 veto first; then deterministic-only, agreed verdict, or ruling."""
    if vetoed(attempt):
        return False
    if not attempt["judge_applicable"]:
        return True
    if disputed(attempt):
        return rulings.get(attempt["attempt_id"])
    return bool(attempt["verdicts"][JUDGE_NAMES[0]])


def wilson(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson score interval; ``None`` for n = 0."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


def _system_row(
    attempts: Sequence[Mapping[str, Any]],
    results: Mapping[str, bool | None],
    k: int,
) -> dict[str, Any]:
    own = [results[a["attempt_id"]] for a in attempts]
    pending = any(r is None for r in own)
    passed = sum(1 for r in own if r is True)
    scenarios: dict[str, list[bool | None]] = {}
    for a in attempts:
        scenarios.setdefault(a["scenario_id"], []).append(results[a["attempt_id"]])
    for sid, values in scenarios.items():
        if len(values) > k:
            raise BinaryJudgeError(f"REDRAW_DETECTED:{sid}")
    # A scenario with fewer than k attempts has not had k passes.
    all_k = sum(1 for v in scenarios.values() if len(v) == k and all(v))
    n = len(own)
    return {
        "n_attempts": n,
        "passed": passed,
        "pass_rate": None if pending or n == 0 else passed / n,
        "pass_rate_interval": None if pending else wilson(passed, n),
        "n_scenarios": len(scenarios),
        "pass_k": None if pending or not scenarios else all_k / len(scenarios),
        "pass_k_interval": None if pending else wilson(all_k, len(scenarios)),
        "q11_vetoed": sum(1 for a in attempts if vetoed(a)),
        "deterministic_only": sum(1 for a in attempts if not a["judge_applicable"]),
    }


def summarize(
    attempts: Sequence[Mapping[str, Any]],
    rulings: Mapping[str, bool],
    *,
    k: int,
) -> dict[str, Any]:
    if k < 1:
        raise BinaryJudgeError("K_INVALID")
    ids = [a["attempt_id"] for a in attempts]
    if len(set(ids)) != len(ids):
        raise BinaryJudgeError("ATTEMPT_DUPLICATE")
    results = {a["attempt_id"]: attempt_pass(a, rulings) for a in attempts}
    needs = [a["attempt_id"] for a in attempts if results[a["attempt_id"]] is None]
    judged = [
        a
        for a in attempts
        if a["judge_applicable"]
        and not vetoed(a)
        and all(a["verdicts"].get(n) is not None for n in JUDGE_NAMES)
    ]
    agreed = sum(1 for a in judged if not disputed(a))
    systems: dict[str, Any] = {}
    for system in dict.fromkeys(a["system"] for a in attempts):
        own = [a for a in attempts if a["system"] == system]
        row = _system_row(own, results, k)
        if system == "opspilot":
            # Section 1 D: OpsPilot only, outside the comparison denominator.
            column = [
                a["root_cause_citation"]
                for a in own
                if a.get("root_cause_citation") is not None
            ]
            cited = sum(1 for c in column if c)
            row["root_cause_citation"] = {
                "n": len(column),
                "cited": cited,
                "rate": cited / len(column) if column else None,
                "in_comparison_denominator": False,
            }
        systems[system] = row
    return {
        "final": not needs,
        "needs_ruling": needs,
        "agreement": {
            "agreed": agreed,
            "judged": len(judged),
            "rate": agreed / len(judged) if judged else None,
        },
        "attempts": {aid: {"pass": results[aid]} for aid in ids},
        "systems": systems,
    }


# --------------------------------------------------------------------------
# The user's one-time ruling list

_SYSTEM_NAMES = {"opspilot": "OpsPilot", "holmes": "HolmesGPT"}
_JUDGE_LABELS = {"glm": "z.ai GLM", "openai": "OpenAI"}


def disputes_markdown(attempts: Sequence[Mapping[str, Any]]) -> str:
    """Plain language, one entry per disputed attempt, for one user pass."""
    rows = [a for a in attempts if disputed(a)]
    lines = [
        "# 两家 judge 分歧，请逐题裁决",
        "",
        f"共 {len(rows)} 处。每处对应一份报告：两家 judge 一家判过、一家判不过，"
        "或者有一家没给出结论。",
        "判法和 judge 一样：「期望要素」每一条的意思报告里都说到了，就算过；"
        "缺任何一条就不过。不要求和原文一字不差。",
        '在 `rulings.json` 里把对应编号的 `null` 改成 `"pass"` 或 `"fail"`。',
    ]
    for n, a in enumerate(rows, 1):
        lines += [
            "",
            f"## {n}. {_SYSTEM_NAMES.get(a['system'], a['system'])}，"
            f"题目 {a['scenario_id']}，第 {a.get('slot', '?')} 次",
            "",
            f"编号：`{a['attempt_id']}`",
            "",
            "期望要素（全部要有才算过）：",
            "",
            *[f"- {e}" for e in a.get("expected_elements", ())],
            "",
            "两家 judge 怎么说：",
            "",
        ]
        for name in JUDGE_NAMES:
            verdict = a["verdicts"].get(name)
            if verdict is None:
                words = f"没给出结论（{a.get('errors', {}).get(name)}）"
            else:
                words = "过：期望要素都在" if verdict else "不过：缺了期望要素"
            lines.append(f"- {_JUDGE_LABELS[name]}：{words}")
            rationale = a.get("rationales", {}).get(name)
            if rationale:
                lines += ["", "  > " + rationale.strip().replace("\n", "\n  > "), ""]
        lines += [
            "",
            "报告原文：",
            "",
            "````text",
            str(a.get("report_text") or "").rstrip(),
            "````",
            "",
            "请回答：过，还是不过？",
        ]
    return "\n".join(lines) + "\n"


RULINGS_SCHEMA = "opspilot-binary-judge-rulings-1"


def rulings_template(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema": RULINGS_SCHEMA,
        "rulings": {a["attempt_id"]: None for a in attempts if disputed(a)},
    }


def load_rulings(
    doc: Mapping[str, Any] | None, attempts: Sequence[Mapping[str, Any]]
) -> dict[str, bool]:
    """Filled rulings; ``null`` stays pending. An id that is not disputed fails."""
    if doc is None:
        return {}
    if doc.get("schema") != RULINGS_SCHEMA or not isinstance(
        doc.get("rulings"), Mapping
    ):
        raise BinaryJudgeError("RULINGS_SCHEMA_INVALID")
    open_ids = {a["attempt_id"] for a in attempts if disputed(a)}
    out: dict[str, bool] = {}
    for attempt_id, value in doc["rulings"].items():
        if attempt_id not in open_ids:
            raise BinaryJudgeError("RULING_NOT_DISPUTED")
        if value is None:
            continue
        if value not in ("pass", "fail"):
            raise BinaryJudgeError("RULING_VALUE_INVALID")
        out[attempt_id] = value == "pass"
    return out
