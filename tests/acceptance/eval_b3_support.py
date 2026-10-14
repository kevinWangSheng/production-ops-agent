"""Independent fixtures for the PR-B3 judge/calibration contract; no B3 code read.

Authority: docs/tasks/2026-10-14-eval-pr-b3-interface.md (cited B3:<line>),
docs/tasks/2026-10-14-eval-pr-b-contract.md section 4 (C:<line>), contract
r8 P6/P7/D16 and PRODUCT-CONSTRAINTS.md:48. Fake transports only; the
network guard below records every socket connect/DNS attempt.

Credentials here are canary strings: they are never valid keys and exist so
tests can prove they do not leak into bodies, errors, reprs or artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from eval_contract_support import END, SCOPE, START, case, micros, span
from opspilot.evaluation.calibration_flow import (
    AnnotationSet,
    BlindLabel,
    CalibrationItem,
    LabelPack,
)
from opspilot.evaluation.judge import (
    JudgeHTTPResponse,
    JudgeInput,
    load_judge_config,
)

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation import (
    Answer,
    DeliveredRecord,
    JudgedDiagnosis,
    ReportCapture,
    ScenarioSpec,
    Window,
    judge_request,
    load_scenarios,
)
from opspilot.tools.registry import canonical_hash

REPO = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPO / "evals" / "scenarios"

# ------------------------------------------------------------- credentials

ZAI_CANARY = "canary-zai-b3-judge-credential"
OPENAI_CANARY = "canary-openai-b3-judge-credential"
DEEPSEEK_CANARY = "canary-deepseek-b3-judge-credential"
CANARIES = (ZAI_CANARY, OPENAI_CANARY, DEEPSEEK_CANARY)

SOL_MODEL = "gpt-6.1-sol"  # B3:20 - unverified Sol API name, supplied explicitly
MODELS = {"zai": "glm-5.3", "openai": SOL_MODEL, "deepseek": "deepseek-flash"}
ENDPOINTS = {  # B3:20 fixed endpoints
    "zai": "https://api.z.ai/api/paas/v4/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/chat/completions",
}
CANARY_OF = {"zai": ZAI_CANARY, "openai": OPENAI_CANARY, "deepseek": DEEPSEEK_CANARY}
CREDENTIAL_NAMES = {
    "zai": "ZAI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
CONFIG_CODES = frozenset(
    {  # B3:18
        "CONFIG_ABSOLUTE_PATH_REQUIRED",
        "CONFIG_PRIVATE_REGULAR_FILE_REQUIRED",
        "CONFIG_UNREADABLE",
        "CONFIG_SYNTAX",
        "CONFIG_ENV_CONFLICT",
        "CONFIG_INVALID",
        "JUDGE_IDENTITY_MISMATCH",
    }
)
PRIVATE_KEYS = (  # B3:26
    "reasoning_content",
    "reasoning",
    "thinking",
    "redacted_thinking",
    "encrypted_content",
)
MAX_REQUEST = 8 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
JUDGE_SCHEMA = "opspilot-judge-1"


def env_pairs() -> list[tuple[str, str]]:
    """Every whitelisted key of B3:18 with a valid value."""
    return [
        (CREDENTIAL_NAMES["zai"], ZAI_CANARY),
        ("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4"),
        ("OPSPILOT_JUDGE_MODEL", "glm-5.3"),
        (CREDENTIAL_NAMES["openai"], OPENAI_CANARY),
        ("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        ("OPENAI_JUDGE_MODEL", SOL_MODEL),
        ("OPENAI_JUDGE_RESPONSE_MODEL", SOL_MODEL),
        (CREDENTIAL_NAMES["deepseek"], DEEPSEEK_CANARY),
        ("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    ]


def env_text(pairs=None, *, drop=(), extra=(), override=None) -> str:
    override = override or {}
    lines = ["# judge credentials (test canaries)", ""]
    for name, value in env_pairs() if pairs is None else pairs:
        if name in drop:
            continue
        lines.append(f"{name}={override.get(name, value)}")
    lines.extend(extra)
    return "\n".join(lines) + "\n"


def write_env(directory: Path, text: str, *, mode: int = 0o600, name=".env.judge"):
    path = directory.resolve() / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)
    return path


def load(path: Path, provider: str = "zai", environ: Mapping[str, str] | None = None):
    return load_judge_config(
        path, environ={} if environ is None else environ, provider=provider
    )


def error_code(exc: BaseException) -> str | None:
    """Fixed error code of a contract error: `.code`, or an UPPER_SNAKE message."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    text = str(exc)
    if isinstance(exc, ValueError) and text and text.replace("_", "").isupper():
        return text
    return None


def leak_surface(exc: BaseException) -> str:
    """Everything an operator could see from an exception, including its chain."""
    import traceback

    parts = ["".join(traceback.format_exception(exc))]
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts += [str(current), repr(current), repr(current.args)]
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


# ---------------------------------------------------------------- transport


@dataclass
class Call:
    endpoint: str
    headers: dict[str, str]
    body: bytes
    timeout_seconds: float
    max_response_bytes: int


def payload(body: bytes) -> dict[str, Any]:
    return json.loads(body)


def user_payload(body: bytes) -> dict[str, Any]:
    """The whitelist JSON carried by the single user message (B3:20)."""
    messages = payload(body)["messages"]
    users = [m for m in messages if m.get("role") == "user"]
    assert len(users) == 1, "exactly one user message"
    content = users[0]["content"]
    if isinstance(content, list):  # tolerate content parts
        content = "".join(part["text"] for part in content)
    return json.loads(content)


def http(
    body: bytes, *, status: int = 200, chunk: int | None = None
) -> JudgeHTTPResponse:
    size = chunk or max(1, len(body))
    pieces = [body[i : i + size] for i in range(0, len(body), size)] or [b""]
    return JudgeHTTPResponse(status_code=status, chunks=iter(pieces))


class Probe:
    """Shared across transports: detects concurrent sends (B3:51 serial)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0


class FakeTransport:
    def __init__(self, respond: Callable[[Call], Any], probe: Probe | None = None):
        self.respond = respond
        self.calls: list[Call] = []
        self.probe = probe or Probe()

    def send(self, *, endpoint, headers, body, timeout_seconds, max_response_bytes):
        with self.probe.lock:
            self.probe.active += 1
            self.probe.max_active = max(self.probe.max_active, self.probe.active)
        try:
            call = Call(
                endpoint,
                dict(headers),
                bytes(body),
                timeout_seconds,
                max_response_bytes,
            )
            self.calls.append(call)
            result = self.respond(call)
        finally:
            with self.probe.lock:
                self.probe.active -= 1
        if isinstance(result, BaseException):
            raise result
        return result


def diag_json(d: JudgedDiagnosis) -> dict[str, Any]:
    out = asdict(d)
    out["cited_evidence_ids"] = list(d.cited_evidence_ids)
    return out


def content(diagnoses, verdict, **extra) -> str:
    doc = {
        "schema": JUDGE_SCHEMA,
        "diagnoses": [
            diag_json(d) if isinstance(d, JudgedDiagnosis) else d for d in diagnoses
        ],
        "verdict": verdict,
    }
    doc.update(extra)
    return json.dumps(doc)


def completion(
    text: str,
    *,
    model: str = "glm-5.3",
    finish_reason: str = "stop",
    message: Mapping[str, Any] | None = None,
    choices: list[Any] | None = None,
    usage: Any = None,
) -> bytes:
    msg = {"role": "assistant", "content": text}
    msg.update(message or {})
    doc = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_760_000_000,
        "model": model,
        "choices": choices
        if choices is not None
        else [{"index": 0, "message": msg, "finish_reason": finish_reason}],
    }
    if usage is not None:
        doc["usage"] = usage
    return json.dumps(doc).encode()


USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def ok_reply(diagnoses, verdict, *, model="glm-5.3", **kw):
    return lambda _call: http(
        completion(content(diagnoses, verdict), model=model, usage=USAGE, **kw)
    )


def judge_input(data: Mapping[str, Any] | None = None, **changes) -> JudgeInput:
    data = dict(case() if data is None else data)
    data.update(changes)
    return JudgeInput(
        spec=data["spec"],
        answer=data["answer"],
        report=data["report"],
        delivered=data["delivered"],
        authorized=data["authorized"],
        outcome=data["outcome"],
    )


def normalized(value: Any) -> Any:
    return json.loads(json.dumps(value))


def expected_user_payload(request: JudgeInput) -> dict[str, Any]:
    return normalized(
        judge_request(
            request.spec,
            request.answer,
            request.report,
            request.delivered,
            request.authorized,
            outcome=request.outcome,
        )
    )


def input_hash(request: JudgeInput) -> str:
    """B3:46 canonical hash of the actual P6 whitelist input (product canonical_hash)."""
    return canonical_hash(expected_user_payload(request))


def usage_value(usage: Any, name: str) -> Any:
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        return usage.get(name)
    return getattr(usage, name, None)


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def strings_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return set().union(*(strings_in(v) for v in value.values()), set())
    if isinstance(value, list):
        return set().union(*(strings_in(v) for v in value), set())
    return set()


# ---------------------------------------------------------- network guard


class NetworkGuard:
    """Records and refuses every outbound socket connect and DNS lookup."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def _blocked(self, name: str):
        def refuse(*_args, **_kwargs):
            self.attempts.append(name)
            raise ConnectionRefusedError("network disabled in B3 acceptance tests")

        return refuse

    def install(self, mp) -> None:
        mp.setattr(socket.socket, "connect", self._blocked("socket.connect"))
        mp.setattr(socket.socket, "connect_ex", self._blocked("socket.connect_ex"))
        mp.setattr(socket, "create_connection", self._blocked("create_connection"))
        mp.setattr(socket, "getaddrinfo", self._blocked("getaddrinfo"))


# ------------------------------------------------------------- label pack

SIG = "ev-signature"
PLAIN = "ev-plain"
OBSERVATION = "The visible trace query returned observations for checkout."
HYPOTHESIS = "A storage dependency may also be involved; this is unconfirmed."
COVERAGE = (  # B3:46
    "hidden_normal_cause",
    "summary_claim_mismatch",
    "extra_wrong_cause",
    "borrowed_citation",
    "hypothesis",
    "no_positive_diagnosis",
    "insufficient_evidence",
)
FAULT_NEGATIVES = {  # validation negatives per fault scenario; covers B3:46 tags
    "eval-cart-failure": ("extra_wrong_cause", "borrowed_citation"),
    "eval-payment-failure-1": ("summary_claim_mismatch", "insufficient_evidence"),
    "eval-payment-failure-2": ("no_positive_diagnosis", "extra_wrong_cause"),
    "eval-product-catalog-failure": ("borrowed_citation", "summary_claim_mismatch"),
}
ANNOTATORS = (("annotator-a", "claude-opus-5-5"), ("annotator-b", "claude-sonnet-5-5"))


def _view(rows):
    return {
        "tool": "traces_search",
        "status": "ok",
        "trust": "untrusted-evidence",
        "content": rows,
    }


def _record(eid: str, target: str, rows) -> DeliveredRecord:
    view = _view(rows)
    return DeliveredRecord(
        evidence_id=eid,
        target_id=target,
        status="ok",
        citable_as_fact=True,
        time_scope_refs=frozenset({SCOPE}),
        window=Window(START, END),
        view=view,
        view_sha256=canonical_hash(view),
        delivered=True,
    )


def _fact(text: str, eid: str, target: str) -> dict[str, Any]:
    return {
        "kind": "fact",
        "text": text,
        "evidence_ids": [eid],
        "target_refs": [target],
        "time_scope_ref": SCOPE,
    }


def _hypothesis() -> dict[str, Any]:
    return {"kind": "hypothesis", "text": HYPOTHESIS, "evidence_ids": []}


def _report(summary: str, claims, nonce: str) -> ReportCapture:
    doc = {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "partial",
        "summary": summary,
        "claims": claims,
        "gaps": ["Broader impact remains unmeasured."],
        "next_steps": [
            f"Review the visible evidence with the operator (note {nonce})."
        ],
    }
    return ReportCapture(json.dumps(doc), "stop")


def _diag(location, quote, service, operation, cited, *, linked=None, supported=True):
    return JudgedDiagnosis(
        location=location,
        quote=quote,
        factual_causal=True,
        service=service,
        operation=operation,
        cited_evidence_ids=tuple(cited),
        linked_claim=linked,
        supported=supported,
        reason="Authored blind label.",
    )


def _cause(service: str, operation: str) -> str:
    return f"{service} {operation} calls failed and caused the checkout errors."


def _variant(spec: ScenarioSpec, answer: Answer, variant: str, nonce: str):
    """(report, diagnoses, verdict) of an independently authored report variant."""
    target = spec.stimulus.target_id
    if spec.kind == "normal":
        hidden = _cause("cart", "EmptyCart")
        if variant == "positive":
            return (
                _report(OBSERVATION, [_fact(OBSERVATION, PLAIN, target)], nonce),
                (),
                True,
            )
        if variant == "hypothesis":
            claims = [_fact(OBSERVATION, PLAIN, target), _hypothesis()]
            return _report(OBSERVATION, claims, nonce), (), True
        if variant == "hidden_normal_cause":
            d = _diag(
                "claims[0]", hidden, "cart", "EmptyCart", (PLAIN,), supported=False
            )
            return _report(hidden, [_fact(hidden, PLAIN, target)], nonce), (d,), False
        if variant == "hidden_normal_cause_summary":
            d = _diag("summary", hidden, "cart", "EmptyCart", (), supported=False)
            return (
                _report(hidden, [_fact(OBSERVATION, PLAIN, target)], nonce),
                (d,),
                False,
            )
        raise AssertionError(variant)
    service, operation = answer.injected.service, answer.injected.operation
    cause = _cause(service, operation)
    seen = f"Error spans were observed on {operation}."
    right = _diag("claims[0]", cause, service, operation, (SIG,))
    wrong = _cause("currency", "Convert")
    if variant == "positive":
        return _report(cause, [_fact(cause, SIG, target)], nonce), (right,), True
    if variant == "hypothesis":
        claims = [_fact(cause, SIG, target), _hypothesis()]
        return _report(cause, claims, nonce), (right,), True
    if variant == "extra_wrong_cause":
        claims = [_fact(cause, SIG, target), _fact(wrong, PLAIN, target)]
        extra = _diag(
            "claims[1]", wrong, "currency", "Convert", (PLAIN,), supported=False
        )
        return _report(cause, claims, nonce), (right, extra), False
    if variant == "borrowed_citation":
        claims = [_fact(cause, PLAIN, target), _fact(seen, SIG, target)]
        d = _diag("claims[0]", cause, service, operation, (PLAIN,), supported=False)
        return _report(cause, claims, nonce), (d,), False
    if variant == "insufficient_evidence":
        claims = [_fact(OBSERVATION, PLAIN, target), _fact(seen, SIG, target)]
        d = _diag(
            "summary", cause, service, operation, (PLAIN,), linked=0, supported=False
        )
        return _report(cause, claims, nonce), (d,), False
    if variant == "summary_claim_mismatch":
        d = _diag(
            "summary", wrong, "currency", "Convert", (SIG,), linked=0, supported=False
        )
        return _report(wrong, [_fact(cause, SIG, target)], nonce), (d, right), False
    if variant == "no_positive_diagnosis":
        return _report(seen, [_fact(seen, SIG, target)], nonce), (), False
    raise AssertionError(variant)


@dataclass(frozen=True)
class Authored:
    item: CalibrationItem
    label: BlindLabel
    variant: str
    positive: bool


@dataclass
class World:
    """36 authored items (6 scenarios x 2 debug + 4 validation)."""

    authored: tuple[Authored, ...]
    by_raw: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_raw = {a.item.request.report.raw: a.item.item_id for a in self.authored}

    @property
    def items(self) -> tuple[CalibrationItem, ...]:
        return tuple(a.item for a in self.authored)

    @property
    def labels(self) -> dict[str, BlindLabel]:
        return {a.item.item_id: a.label for a in self.authored}

    def get(self, item_id: str) -> Authored:
        return next(a for a in self.authored if a.item.item_id == item_id)

    def pick(self, *, split="validation", positive=None, kind=None, variant=None):
        return [
            a
            for a in self.authored
            if a.item.split == split
            and (positive is None or a.positive == positive)
            and (kind is None or a.item.request.spec.kind == kind)
            and (variant is None or a.variant == variant)
        ]


def author_item(spec, answer, split, k, variant, coverage, *, family=None) -> Authored:
    item_id = f"{spec.scenario_id}/{split}-{k}"
    report, diagnoses, verdict = _variant(spec, answer, variant, item_id)
    target = spec.stimulus.target_id
    delivered = {PLAIN: _record(PLAIN, target, [_plain_span(spec)])}
    if spec.kind == "fault":
        matcher = answer.signature.any_of[0]
        row = span(
            service=matcher.service,
            operation=matcher.operation,
            span_id=f"span-{spec.scenario_id}",
            trace_id=f"trace-{spec.scenario_id}",
            start_us=micros(START + timedelta(seconds=2)),
        )
        delivered[SIG] = _record(SIG, target, [row])
    outcome = IncidentOutcome(
        scenario_id=spec.scenario_id,
        final_state="completed",
        evidence_ids=tuple(sorted(delivered)),
        decision="report_available",
        actions=("read_only_query",),
        permissions=("read_only", "human_control"),
        human_interaction=None,
        handoff_reasons=(),
        report_available=True,
    )
    request = JudgeInput(
        spec=spec,
        answer=answer,
        report=report,
        delivered=delivered,
        authorized={SCOPE: Window(START, END)},
        outcome=outcome,
    )
    digest = input_hash(request)
    item = CalibrationItem(
        item_id=item_id,
        scenario_id=spec.scenario_id,
        split=split,
        source_family=family or f"authored/{item_id}",
        input_hash=digest,
        request=request,
        coverage=tuple(coverage),
    )
    label = BlindLabel(
        item_id=item_id, input_hash=digest, diagnoses=diagnoses, verdict=verdict
    )
    return Authored(item, label, variant, verdict)


def _plain_span(spec: ScenarioSpec) -> dict[str, Any]:
    return span(
        service="frontend",
        operation="GET /api/cart",
        span_id=f"span-plain-{spec.scenario_id}",
        trace_id=f"trace-plain-{spec.scenario_id}",
        status_tags={},
        error_by_visible_tags=False,
    )


def build_world() -> World:
    authored: list[Authored] = []
    for spec, answer in load_scenarios(SCENARIO_ROOT):
        if spec.kind == "fault":
            negatives = FAULT_NEGATIVES[spec.scenario_id]
            debug = (("positive", ()), ("no_positive_diagnosis", ()))
        else:
            negatives = ("hidden_normal_cause", "hidden_normal_cause_summary")
            debug = (("positive", ()), ("hidden_normal_cause", ()))
        for k, (variant, coverage) in enumerate(debug, 1):
            authored.append(author_item(spec, answer, "debug", k, variant, coverage))
        validation = [("hypothesis", ("hypothesis",)), ("hypothesis", ("hypothesis",))]
        validation += [
            (v, ("hidden_normal_cause",) if v.startswith("hidden") else (v,))
            for v in negatives
        ]
        for k, (variant, coverage) in enumerate(validation, 1):
            authored.append(
                author_item(spec, answer, "validation", k, variant, coverage)
            )
    return World(tuple(authored))


def annotation(
    labels: Mapping[str, BlindLabel], index: int = 0, **changes
) -> AnnotationSet:
    who, model = ANNOTATORS[index]
    base = AnnotationSet(
        annotator_identity=who,
        model_identity=model,
        labels=tuple(labels[k] for k in sorted(labels)),
    )
    return replace(base, **changes)


def make_pack(world: World, *, items=None, first=None, second=None, adjudications=()):
    labels = world.labels
    return LabelPack(
        items=tuple(world.items if items is None else items),
        annotations=(
            annotation(labels, 0) if first is None else first,
            annotation(labels, 1) if second is None else second,
        ),
        adjudications=tuple(adjudications),
    )


# ------------------------------------------------- calibration transports


def label_reply(label: BlindLabel, provider: str):
    return http(
        completion(
            content(label.diagnoses, label.verdict), model=MODELS[provider], usage=USAGE
        )
    )


def false_fail(label: BlindLabel, provider: str):
    """A valid judgement that a positive fault item fails (supported=false)."""
    diagnoses = tuple(replace(d, supported=False) for d in label.diagnoses)
    assert diagnoses, "false_fail needs a labelled diagnosis to contradict"
    return http(
        completion(content(diagnoses, False), model=MODELS[provider], usage=USAGE)
    )


def false_pass(label: BlindLabel, provider: str):
    """A valid judgement that a normal negative item passes (no causal diagnosis)."""
    return http(completion(content((), True), model=MODELS[provider], usage=USAGE))


def missed_diagnosis(label: BlindLabel, provider: str):
    """Verdict false (agrees with the negative label) via a different diagnosis than labelled."""
    (d,) = label.diagnoses
    assert d.location == "claims[0]"
    moved = replace(d, location="summary", linked_claim=0)
    return http(
        completion(content((moved,), False), model=MODELS[provider], usage=USAGE)
    )


def http_error(label: BlindLabel, provider: str):
    return http(b'{"error":{"message":"overloaded"}}', status=503)


def timeout(label: BlindLabel, provider: str):
    return TimeoutError("judge timed out")


def not_json(label: BlindLabel, provider: str):
    return http(completion("verdict: false", model=MODELS[provider]))


def oversize(label: BlindLabel, provider: str):
    return http(b" " * (MAX_RESPONSE + 1) + completion(content((), False)), chunk=65536)


class CalibrationTransport(FakeTransport):
    """Answers each item from its blind label unless an override applies.

    overrides: {(item_id, repetition) | (item_id, None): fn(label, provider)}.
    Repetition counts from 1 per item, in call order.
    """

    def __init__(self, provider: str, world: World, probe: Probe, overrides=None):
        self.provider = provider
        self.world = world
        self.overrides = dict(overrides or {})
        self.per_item: dict[str, int] = {}
        super().__init__(self._respond, probe)

    def _respond(self, call: Call):
        item_id = self.world.by_raw[user_payload(call.body)["report_raw"]]
        rep = self.per_item[item_id] = self.per_item.get(item_id, 0) + 1
        label = self.world.labels[item_id]
        fn = self.overrides.get((item_id, rep)) or self.overrides.get((item_id, None))
        return (fn or label_reply)(label, self.provider)


def transports(world: World, probe: Probe | None = None, **overrides):
    probe = probe or Probe()
    return {
        name: CalibrationTransport(name, world, probe, overrides.get(name))
        for name in MODELS
    }


def everywhere(fn, authored) -> dict:
    return {(a.item.item_id, None): fn for a in authored}
