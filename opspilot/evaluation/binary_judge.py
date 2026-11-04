"""Q9 binary judge for the F1 closure comparison (S7a).

Two judges from different vendors (z.ai GLM and OpenAI, neither a candidate
model) each give one pass/fail per report, with the same prompt and the same
input projection for both systems: the scenario's expected elements and the
report as text (OpsPilot's ReportV2 rendered by :func:`render_report`,
HolmesGPT's prose as returned). Where the two disagree, or one gives no
verdict, the item goes to the user once (:func:`disputes_markdown`).

The prompt is HolmesGPT's own strict correctness prompt at the pinned
upstream commit (``tests/llm/utils/classifiers.py``, ``evaluate_correctness``
with ``evaluation_type="strict"``: choice A = all expected elements present),
so the upstream baseline is graded the way upstream grades itself.

Q11 deterministic checks veto first: an attempt that fails them fails,
whatever the judges say, and is not sent to a judge. OpsPilot gets one more
column, the root-cause evidence citation rate (pre-review section 1 D), which
is reported for OpsPilot only and never enters the comparison denominator.

Nothing here reads a credential except :func:`provider_configs`, and keys
only travel in the ``Authorization`` header of :class:`HTTPCompletionClient`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

from opspilot.evaluation.gate import (
    parsed_report,
    report_signature_matches,
    run_gate,
    run_scope,
)
from opspilot.evaluation.model import (
    AnyAnswer,
    DeliveredRecord,
    Window,
    is_fault_scenario,
)
from opspilot.investigation.reports import ReportV2

ITEMS_SCHEMA = "opspilot-binary-judge-items-1"
RULINGS_SCHEMA = "opspilot-binary-judge-rulings-1"
SUMMARY_SCHEMA = "opspilot-binary-judge-summary-1"
HOLMES_INPUTS_SCHEMA = "opspilot-holmes-judge-inputs-1"

System = Literal["opspilot", "holmesgpt"]
Provider = Literal["zai", "openai"]
PROVIDERS: tuple[Provider, ...] = ("zai", "openai")

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
# Upstream asks autoevals for a chain-of-thought tool call (use_cot=True:
# rationale, then choice); a strict json object carries the same two fields
# on both endpoints.
ANSWER_INSTRUCTION = (
    "\nAnswer with one json object and nothing else: "
    '{"rationale": "<step-by-step reasoning, written before choosing>", '
    '"choice": "A" or "B"}\n'
)
PROMPT_TEMPLATE = UPSTREAM_STRICT_PROMPT + ANSWER_INSTRUCTION
PROMPT_SHA256 = hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()

_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "choice": {"type": "string", "enum": ["A", "B"]},
    },
    "required": ["rationale", "choice"],
    "additionalProperties": False,
}

# Same environment names as opspilot/evaluation/judge.py.
_BASES: Mapping[Provider, str] = {
    "zai": "https://api.z.ai/api/paas/v4",
    "openai": "https://api.openai.com/v1",
}
_ENV: Mapping[Provider, tuple[str, str, str]] = {
    # key, base url, model
    "zai": ("ZAI_API_KEY", "ZAI_BASE_URL", "OPSPILOT_JUDGE_MODEL"),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_JUDGE_MODEL"),
}
# R10: the P7 adapters' models (judge.py _MODELS); overridable by env.
DEFAULT_MODELS: Mapping[Provider, str] = {
    "zai": "glm-5.3-flashx",
    "openai": "gpt-5.6-luna",
}
#: Never a judge: the candidate model both systems run on.
CANDIDATE_MODELS = frozenset({"deepseek-flash", "deepseek-pro"})
MAX_ATTEMPTS = 2


class BinaryJudgeError(ValueError):
    """A fixed, credential-free code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# --------------------------------------------------------------------------
# Inputs: one subject per started attempt


@dataclass(frozen=True)
class Q11Result:
    """Q11 deterministic checks; ``checks`` lists only those that apply."""

    passed: bool
    checks: Mapping[str, bool]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class JudgeSubject:
    system: System
    scenario_id: str
    attempt_id: str
    slot: int
    category: str
    polarity: str
    expected_elements: tuple[str, ...]
    output_text: str | None
    q11: Q11Result
    #: OpsPilot fault scenarios only (section 1 D); ``None`` = not applicable.
    root_cause_cited: bool | None = None

    @property
    def item_id(self) -> str:
        return f"{self.system}:{self.scenario_id}:{self.attempt_id}"


_KIND_LABELS = {
    "fact": "Finding",
    "hypothesis": "Hypothesis",
    "recommendation": "Recommendation",
    "counter_evidence": "Counter-evidence",
    "rejected_hypothesis": "Rejected hypothesis",
}


def _evidence_label(record: DeliveredRecord | None) -> str:
    if record is None:
        return "unknown source"
    tool = record.view.get("tool")
    if tool == "read_evidence" and isinstance(record.view.get("parent_tool"), str):
        tool = record.view["parent_tool"]
    return tool if isinstance(tool, str) and tool else "unknown source"


def render_report(report: ReportV2, delivered: Mapping[str, DeliveredRecord]) -> str:
    """ReportV2 as plain text for the judge.

    Field order is the report's own; opaque evidence IDs become ``E<n>``
    labels with the tool that produced the record, so the judge sees that a
    statement cites trace or metric evidence without internal identifiers.
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
        report.summary.strip(),
    ]
    if report.claims:
        lines += ["", "Statements:"]
        for claim in report.claims:
            cites = ", ".join(
                f"E{numbers[eid]} ({_evidence_label(delivered.get(eid))})"
                for eid in claim.evidence_ids
            )
            suffix = f" [evidence: {cites}]" if cites else ""
            lines.append(f"- {_KIND_LABELS[claim.kind]}: {claim.text.strip()}{suffix}")
    for title, values in (("Gaps", report.gaps), ("Next steps", report.next_steps)):
        if values:
            lines += ["", f"{title}:"]
            lines += [f"- {value.strip()}" for value in values]
    return "\n".join(lines) + "\n"


def root_cause_evidence_cited(
    answer: AnyAnswer,
    report: ReportV2 | None,
    delivered: Mapping[str, DeliveredRecord],
    authorized: Mapping[str, Window],
    scope: Any,
) -> bool:
    """Section 1 D: conclusion supported/partial and a fact or hypothesis
    claim cites a signature-hit record with fact-qualified bindings.

    Proves the fault evidence was cited in support, not that it was named
    the cause ("payment errors, but not the cause" also passes).
    """
    if report is None or report.conclusion not in {"supported", "partial"}:
        return False
    kept = [c for c in report.claims if c.kind in {"fact", "hypothesis"}]
    if not kept:
        return False
    narrowed = report.model_copy(update={"claims": kept})
    return bool(
        report_signature_matches(answer, narrowed, delivered, authorized, scope)
    )


def opspilot_subject(capture: Any, *, slot: int) -> JudgeSubject:
    """One OpsPilot attempt from its frozen ``EvaluationCapture``.

    Q11 is the deterministic gate recomputed here (schema, citations and
    bindings, ``read_only``, final state, fault evidence signature).
    """
    spec, answer = capture.spec, capture.answer
    gate = run_gate(
        spec,
        answer,
        capture.outcome,
        capture.report,
        capture.delivered,
        capture.authorized,
    )
    parsed = parsed_report(capture.report)
    root: bool | None = None
    if is_fault_scenario(spec, answer):
        root = root_cause_evidence_cited(
            answer,
            parsed,
            capture.delivered,
            capture.authorized,
            run_scope(spec, capture.outcome),
        )
    return JudgeSubject(
        system="opspilot",
        scenario_id=spec.scenario_id,
        attempt_id=capture.attempt.attempt_id,
        slot=slot,
        category=answer.category,
        polarity=answer.polarity,
        expected_elements=tuple(answer.expected_output),
        output_text=None
        if parsed is None
        else render_report(parsed, capture.delivered),
        q11=Q11Result(gate.passed, dict(gate.checks), tuple(gate.reasons)),
        root_cause_cited=root,
    )


def not_started_subject(
    system: System, answer: Any, *, attempt_id: str, slot: int, reason: str
) -> JudgeSubject:
    """A started-but-unreported or never-started slot: counted, vetoed."""
    return JudgeSubject(
        system=system,
        scenario_id=answer.scenario_id,
        attempt_id=attempt_id,
        slot=slot,
        category=answer.category,
        polarity=answer.polarity,
        expected_elements=tuple(answer.expected_output),
        output_text=None,
        q11=Q11Result(False, {"execution": False}, (reason,)),
        root_cause_cited=False
        if system == "opspilot" and answer.signature is not None
        else None,
    )


def holmes_subject(
    output: Mapping[str, Any],
    answer: Any,
    *,
    attempt_id: str,
    slot: int,
    read_only: bool,
) -> JudgeSubject:
    """One HolmesGPT attempt from its ``holmes ask --json-output-file`` object.

    Q11 for HolmesGPT is only "read-only, no overreach" (from the runner's
    proxy log) and "investigation finished" (finish_reason stop, non-empty
    result).
    """
    result = output.get("result")
    completed = (
        output.get("finish_reason") == "stop"
        and isinstance(result, str)
        and bool(result.strip())
    )
    checks = {"read_only": bool(read_only), "completed": completed}
    reasons = tuple(
        code
        for ok, code in (
            (checks["read_only"], "PERMISSION_VIOLATION"),
            (completed, "EXECUTION_NOT_COMPLETED"),
        )
        if not ok
    )
    return JudgeSubject(
        system="holmesgpt",
        scenario_id=answer.scenario_id,
        attempt_id=attempt_id,
        slot=slot,
        category=answer.category,
        polarity=answer.polarity,
        expected_elements=tuple(answer.expected_output),
        output_text=result if completed and isinstance(result, str) else None,
        q11=Q11Result(not reasons, checks, reasons),
    )


def judge_prompt(subject: JudgeSubject) -> str:
    """The one input projection both systems get: expected elements + text."""
    if subject.output_text is None:
        raise BinaryJudgeError("NO_OUTPUT")
    expected = "\n- ".join(subject.expected_elements)
    # Single pass, output last: report text cannot inject a second
    # substitution into the template.
    head, tail = PROMPT_TEMPLATE.split("{{output}}")
    return head.replace("{{expected}}", expected) + subject.output_text + tail


# --------------------------------------------------------------------------
# Providers


@dataclass(frozen=True)
class ProviderConfig:
    provider: Provider
    endpoint: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 600.0
    max_output_tokens: int = 16384  # upstream classifier max_tokens
    max_request_bytes: int = 4 * 1024 * 1024
    max_response_bytes: int = 2 * 1024 * 1024

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
        }


def provider_configs(environ: Mapping[str, str]) -> tuple[ProviderConfig, ...]:
    """Both judges from the environment; codes name variables, never values."""
    configs: list[ProviderConfig] = []
    for provider in PROVIDERS:
        key_name, base_name, model_name = _ENV[provider]
        key = environ.get(key_name, "")
        if not key.strip() or any(c in key for c in "\r\n\x00"):
            raise BinaryJudgeError(f"JUDGE_KEY_MISSING:{key_name}")
        base = environ.get(base_name, _BASES[provider])
        if base != _BASES[provider]:
            raise BinaryJudgeError(f"JUDGE_BASE_URL_NOT_ALLOWED:{base_name}")
        model = environ.get(model_name, DEFAULT_MODELS[provider]).strip()
        if not model or model in CANDIDATE_MODELS:
            raise BinaryJudgeError(f"JUDGE_MODEL_INVALID:{model_name}")
        configs.append(ProviderConfig(provider, base + "/chat/completions", model, key))
    return tuple(configs)


def request_body(config: ProviderConfig, prompt: str) -> bytes:
    body: dict[str, Any] = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
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
    if config.provider == "zai":
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
        from opspilot.evaluation.judge import HTTPJudgeTransport, JudgeError

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
        except JudgeError as exc:
            code = exc.code if exc.code.startswith("JUDGE_") else "JUDGE_OUTPUT_INVALID"
            if type(exc).__name__ == "StreamTruncated":
                code = "JUDGE_TRANSPORT_ERROR"
            raise BinaryJudgeError(code) from None
        except Exception:
            raise BinaryJudgeError("JUDGE_TRANSPORT_ERROR") from None
        if not 200 <= response.status_code < 300:
            raise BinaryJudgeError("JUDGE_HTTP_ERROR")
        return b"".join(response.chunks)


@dataclass(frozen=True)
class Verdict:
    provider: Provider
    request_model: str
    response_model: str | None
    status: Literal["ok", "error"]
    passed: bool | None
    rationale: str | None
    code: str | None
    attempts: int
    usage: Mapping[str, int | None]


_DATED = re.compile(r"(?P<base>.+)-(?P<day>\d{4}-\d{2}-\d{2})")


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


def _usage(doc: Mapping[str, Any]) -> dict[str, int | None]:
    usage = doc.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    return {
        key: value if type(value := usage.get(key)) is int else None
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


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
    return content["choice"] == "A", content["rationale"], doc["model"], _usage(doc)


def judge_once(
    prompt: str, config: ProviderConfig, client: CompletionClient
) -> Verdict:
    """One provider's verdict; a transport failure is retried once."""
    attempts = 0
    code: str | None = None
    try:
        body = request_body(config, prompt)
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
    if code is not None:
        return Verdict(
            config.provider,
            config.model,
            None,
            "error",
            None,
            None,
            code,
            max(1, attempts),
            dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens")),
        )
    return Verdict(
        config.provider,
        config.model,
        model,
        "ok",
        passed,
        rationale,
        None,
        attempts,
        usage,
    )


# --------------------------------------------------------------------------
# Judging and agreement

Agreement = Literal["vetoed", "agreed", "disagreed", "judge_error"]


@dataclass(frozen=True)
class JudgedItem:
    subject: JudgeSubject
    verdicts: tuple[Verdict, ...]
    prompt_sha256: str | None

    @property
    def agreement(self) -> Agreement:
        if not self.subject.q11.passed:
            return "vetoed"
        if len(self.verdicts) != len(PROVIDERS) or any(
            v.status != "ok" for v in self.verdicts
        ):
            return "judge_error"
        return "agreed" if len({v.passed for v in self.verdicts}) == 1 else "disagreed"

    @property
    def needs_ruling(self) -> bool:
        return self.agreement in ("disagreed", "judge_error")


def judge_subjects(
    subjects: Iterable[JudgeSubject],
    configs: Sequence[ProviderConfig],
    client: CompletionClient,
) -> list[JudgedItem]:
    """Q11 first: a vetoed attempt is never sent to a judge."""
    if tuple(c.provider for c in configs) != PROVIDERS:
        raise BinaryJudgeError("JUDGE_PROVIDERS_INVALID")
    items: list[JudgedItem] = []
    for subject in subjects:
        if not subject.q11.passed or subject.output_text is None:
            items.append(JudgedItem(subject, (), None))
            continue
        prompt = judge_prompt(subject)
        verdicts = tuple(judge_once(prompt, config, client) for config in configs)
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        items.append(JudgedItem(subject, verdicts, digest))
    return items


def judge_config_record(configs: Sequence[ProviderConfig]) -> dict[str, Any]:
    return {
        "prompt_sha256": PROMPT_SHA256,
        "prompt_source": "HolmesGPT a0009195 tests/llm/utils/classifiers.py strict",
        "providers": [c.public() for c in configs],
    }


# --------------------------------------------------------------------------
# Serialization (plain JSON; the item file is local, the summary is public)


def _subject_json(s: JudgeSubject) -> dict[str, Any]:
    return {
        "system": s.system,
        "scenario_id": s.scenario_id,
        "attempt_id": s.attempt_id,
        "slot": s.slot,
        "category": s.category,
        "polarity": s.polarity,
        "expected_elements": list(s.expected_elements),
        "output_text": s.output_text,
        "q11": {
            "passed": s.q11.passed,
            "checks": dict(s.q11.checks),
            "reasons": list(s.q11.reasons),
        },
        "root_cause_cited": s.root_cause_cited,
    }


def _verdict_json(v: Verdict) -> dict[str, Any]:
    return {
        "provider": v.provider,
        "request_model": v.request_model,
        "response_model": v.response_model,
        "status": v.status,
        "passed": v.passed,
        "rationale": v.rationale,
        "code": v.code,
        "attempts": v.attempts,
        "usage": dict(v.usage),
    }


def items_document(
    items: Sequence[JudgedItem], *, config: Mapping[str, Any], k: int
) -> dict[str, Any]:
    return {
        "schema": ITEMS_SCHEMA,
        "k": k,
        "judge_config": dict(config),
        "items": [
            {
                "item_id": item.subject.item_id,
                "subject": _subject_json(item.subject),
                "verdicts": [_verdict_json(v) for v in item.verdicts],
                "prompt_sha256": item.prompt_sha256,
                "agreement": item.agreement,
            }
            for item in items
        ],
    }


def load_items(doc: Mapping[str, Any]) -> tuple[list[JudgedItem], int, dict[str, Any]]:
    if doc.get("schema") != ITEMS_SCHEMA:
        raise BinaryJudgeError("ITEMS_SCHEMA_INVALID")
    items: list[JudgedItem] = []
    for raw in doc["items"]:
        s = raw["subject"]
        subject = JudgeSubject(
            system=s["system"],
            scenario_id=s["scenario_id"],
            attempt_id=s["attempt_id"],
            slot=s["slot"],
            category=s["category"],
            polarity=s["polarity"],
            expected_elements=tuple(s["expected_elements"]),
            output_text=s["output_text"],
            q11=Q11Result(
                s["q11"]["passed"], dict(s["q11"]["checks"]), tuple(s["q11"]["reasons"])
            ),
            root_cause_cited=s["root_cause_cited"],
        )
        verdicts = tuple(
            Verdict(
                v["provider"],
                v["request_model"],
                v["response_model"],
                v["status"],
                v["passed"],
                v["rationale"],
                v["code"],
                v["attempts"],
                dict(v["usage"]),
            )
            for v in raw["verdicts"]
        )
        item = JudgedItem(subject, verdicts, raw["prompt_sha256"])
        if item.subject.item_id != raw["item_id"] or item.agreement != raw["agreement"]:
            raise BinaryJudgeError("ITEMS_INCONSISTENT")
        items.append(item)
    ids = [i.subject.item_id for i in items]
    if len(set(ids)) != len(ids):
        raise BinaryJudgeError("ITEMS_DUPLICATE")
    return items, int(doc["k"]), dict(doc["judge_config"])


# --------------------------------------------------------------------------
# The user's one-time ruling list

_SYSTEM_NAMES = {"opspilot": "OpsPilot", "holmesgpt": "HolmesGPT"}
_PROVIDER_NAMES = {"zai": "z.ai GLM", "openai": "OpenAI"}


def _verdict_words(v: Verdict) -> str:
    if v.status != "ok":
        return f"没给出结论（{v.code}）"
    return "过：期望要素都在" if v.passed else "不过：缺了期望要素"


def disputes_markdown(items: Sequence[JudgedItem]) -> str:
    """Plain-language, one entry per disputed item, for one user pass."""
    disputed = [i for i in items if i.needs_ruling]
    lines = [
        "# 两家 judge 分歧，请逐题裁决",
        "",
        f"共 {len(disputed)} 处。每处是一份报告：两家 judge 一个说过、一个说不过，"
        "或者有一家没给出结论。",
        "判法和 judge 一样：报告里把「期望要素」每一条的意思都说到了就算过，"
        "缺任何一条就不过；不要求原文一致。",
        '在 `rulings.json` 里把对应编号的 `null` 改成 `"pass"` 或 `"fail"`。',
    ]
    for n, item in enumerate(disputed, 1):
        s = item.subject
        lines += [
            "",
            f"## {n}. {_SYSTEM_NAMES[s.system]}，题目 {s.scenario_id}，第 {s.slot} 次",
            "",
            f"编号：`{s.item_id}`",
            "",
            "期望要素（全部要有才算过）：",
            "",
            *[f"- {e}" for e in s.expected_elements],
            "",
            "两家 judge 怎么说：",
            "",
        ]
        for v in item.verdicts:
            lines.append(f"- {_PROVIDER_NAMES[v.provider]}：{_verdict_words(v)}")
            if v.rationale:
                lines += ["", "  > " + v.rationale.strip().replace("\n", "\n  > "), ""]
        lines += [
            "",
            "报告原文：",
            "",
            "````text",
            (s.output_text or "").rstrip(),
            "````",
            "",
            "请回答：过 还是 不过？",
        ]
    return "\n".join(lines) + "\n"


def rulings_template(items: Sequence[JudgedItem]) -> dict[str, Any]:
    return {
        "schema": RULINGS_SCHEMA,
        "rulings": {i.subject.item_id: None for i in items if i.needs_ruling},
    }


def load_rulings(
    doc: Mapping[str, Any] | None, items: Sequence[JudgedItem]
) -> dict[str, bool]:
    """Filled rulings; ``null`` stays pending. Unknown or undisputed ids fail."""
    if doc is None:
        return {}
    if doc.get("schema") != RULINGS_SCHEMA or not isinstance(
        doc.get("rulings"), Mapping
    ):
        raise BinaryJudgeError("RULINGS_SCHEMA_INVALID")
    disputed = {i.subject.item_id for i in items if i.needs_ruling}
    out: dict[str, bool] = {}
    for item_id, value in doc["rulings"].items():
        if item_id not in disputed:
            raise BinaryJudgeError("RULING_NOT_DISPUTED")
        if value is None:
            continue
        if value not in ("pass", "fail"):
            raise BinaryJudgeError("RULING_VALUE_INVALID")
        out[item_id] = value == "pass"
    return out


# --------------------------------------------------------------------------
# Scores


def wilson(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson score interval; ``None`` for n = 0."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _rate(successes: int, n: int) -> dict[str, Any]:
    return {
        "successes": successes,
        "n": n,
        "rate": None if n == 0 else round(successes / n, 4),
        "wilson95": wilson(successes, n),
    }


def final_pass(item: JudgedItem, rulings: Mapping[str, bool]) -> bool | None:
    """Q11 veto first; then the agreed verdict or the user's ruling."""
    agreement = item.agreement
    if agreement == "vetoed":
        return False
    if agreement == "agreed":
        return bool(item.verdicts[0].passed)
    return rulings.get(item.subject.item_id)


def _system_summary(
    items: Sequence[JudgedItem], rulings: Mapping[str, bool], k: int
) -> dict[str, Any]:
    finals = {i.subject.item_id: final_pass(i, rulings) for i in items}
    pending = sorted(item_id for item_id, value in finals.items() if value is None)
    by_scenario: dict[str, list[JudgedItem]] = {}
    for item in items:
        by_scenario.setdefault(item.subject.scenario_id, []).append(item)
    scenarios: list[dict[str, Any]] = []
    for sid in sorted(by_scenario):
        group = sorted(by_scenario[sid], key=lambda i: i.subject.slot)
        if len(group) > k:
            raise BinaryJudgeError(f"REDRAW_DETECTED:{sid}")
        results = [finals[i.subject.item_id] for i in group]
        # A slot that never started counts as a failure (Q20 rule).
        missing = k - len(group)
        all_pass: bool | None
        if any(r is False for r in results) or missing:
            all_pass = False
        elif any(r is None for r in results):
            all_pass = None
        else:
            all_pass = True
        scenarios.append(
            {
                "scenario_id": sid,
                "category": group[0].subject.category,
                "polarity": group[0].subject.polarity,
                "attempts": [
                    {
                        "item_id": i.subject.item_id,
                        "slot": i.subject.slot,
                        "q11_passed": i.subject.q11.passed,
                        "q11_reasons": list(i.subject.q11.reasons),
                        "verdicts": {v.provider: v.passed for v in i.verdicts},
                        "agreement": i.agreement,
                        "ruled": i.subject.item_id in rulings,
                        "final": finals[i.subject.item_id],
                    }
                    for i in group
                ],
                "missing_slots": missing,
                f"pass^{k}": all_pass,
            }
        )
    n_slots = len(scenarios) * k
    vetoed = sum(1 for i in items if i.agreement == "vetoed")
    judged = [i for i in items if i.agreement != "vetoed"]
    both_ok = [i for i in judged if i.agreement in ("agreed", "disagreed")]
    agreed = sum(1 for i in both_ok if i.agreement == "agreed")
    per_provider: dict[str, Any] = {}
    for provider in PROVIDERS:
        verdicts = [v for i in judged for v in i.verdicts if v.provider == provider]
        per_provider[provider] = {
            "ok": sum(1 for v in verdicts if v.status == "ok"),
            "errors": sum(1 for v in verdicts if v.status != "ok"),
            "passed": sum(1 for v in verdicts if v.passed is True),
        }
    out: dict[str, Any] = {
        "status": "awaiting_rulings" if pending else "final",
        "pending_rulings": pending,
        "scenarios": len(scenarios),
        "k": k,
        "q11_vetoed": vetoed,
        "judge": {
            "judged": len(judged),
            "both_valid": len(both_ok),
            "agreement": _rate(agreed, len(both_ok)),
            "disagreed": sum(1 for i in judged if i.agreement == "disagreed"),
            "judge_errors": sum(1 for i in judged if i.agreement == "judge_error"),
            "ruled_by_user": sum(1 for i in judged if i.subject.item_id in rulings),
            "per_provider": per_provider,
        },
        "per_scenario": scenarios,
    }
    if pending:
        out["single_run"] = None
        out[f"pass^{k}"] = None
    else:
        passes = sum(1 for value in finals.values() if value)
        # Denominator: every slot (a never-started slot is a failure).
        out["single_run"] = _rate(passes, n_slots)
        out[f"pass^{k}"] = _rate(
            sum(1 for s in scenarios if s[f"pass^{k}"]), len(scenarios)
        )
    return out


def summarize(
    items: Sequence[JudgedItem],
    rulings: Mapping[str, bool],
    *,
    k: int,
    judge_config: Mapping[str, Any],
) -> dict[str, Any]:
    if k < 1:
        raise BinaryJudgeError("K_INVALID")
    systems: dict[str, Any] = {}
    for system in ("opspilot", "holmesgpt"):
        own = [i for i in items if i.subject.system == system]
        if own:
            systems[system] = _system_summary(own, rulings, k)
    ops = [i for i in items if i.subject.system == "opspilot"]
    applicable = [i for i in ops if i.subject.root_cause_cited is not None]
    if "opspilot" in systems:
        # Section 1 D: OpsPilot only, never in the comparison denominator.
        systems["opspilot"]["root_cause_evidence_citation"] = {
            **_rate(
                sum(1 for i in applicable if i.subject.root_cause_cited),
                len(applicable),
            ),
            "rule": "conclusion in {supported, partial} and a fact or hypothesis "
            "claim cites a signature-hit record with fact bindings",
            "in_comparison_denominator": False,
        }
    both = [i for i in items if i.agreement in ("agreed", "disagreed")]
    return {
        "schema": SUMMARY_SCHEMA,
        "method": "uncalibrated two-vendor binary judge + user ruling on "
        "disagreements; Q11 deterministic checks veto first",
        "judge_config": dict(judge_config),
        "status": "awaiting_rulings"
        if any(s["status"] != "final" for s in systems.values())
        else "final",
        "judge_agreement_overall": _rate(
            sum(1 for i in both if i.agreement == "agreed"), len(both)
        ),
        "systems": systems,
    }
