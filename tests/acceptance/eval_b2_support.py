"""Independent synthetic fixtures for the PR-B2 recording/replay contract.

Written only from docs/tasks/2026-10-14-eval-pr-b2-interface.md ("I:<line>")
and the PR-B pre-review section 4 ("C:<line>"); no B2 implementation was read.
Everything here is synthetic (I:4); nothing touches a network, PG or kind.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation import (
    AttemptRecord,
    DimensionValue,
    Manifest,
    RecordingContext,
    RecordingError,
    ToolBinding,
    load_recording,
    manifest_hash,
    replay,
)
from opspilot.investigation.loop import (
    ACCEPTED_RESPONSE_MODEL,
    ModelCall,
    ModelReply,
)
from opspilot.tools.executor import ToolRequest
from opspilot.tools.outcomes import EvidenceRecord, ToolOperation, ToolOutcome
from opspilot.tools.outcomes import Window as ToolWindow
from opspilot.tools.registry import canonical, canonical_hash
from tests.acceptance.eval_contract_support import DIMENSIONS

START = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
END = START + timedelta(minutes=10)
SOURCE_RUN = "source-run-1"
REPLAY_RUN = "replay-run-1"
SCENARIO_ID = "contract-fault"
TARGET = "target-demo"
TOOL = "traces_search"
SYSTEM = "You are a read-only investigator."
QUESTION = "Investigate checkout symptoms in the authorized window."
SRC_REASONING = "SRC-PRIVATE-REASONING-7f3a"
FULL_VIEW_MARKER = "FULL-VIEW-ONLY-MARKER-91c2"
TOOL_SCHEMA = {
    "type": "function",
    "function": {"name": TOOL, "parameters": {"type": "object"}},
}
# Versions the model-mode compatibility check compares with the manifest
# (I:71). eval_contract_support.manifest() freezes every dimension as
# DimensionValue("version", "v1"), so "v1" here is the compatible value.
COMPAT_DIMENSIONS = ("tools_schema", "registry", "profile", "projection", "permissions")

REQ_A = {
    "tool_name": TOOL,
    "target_ref": TARGET,
    "params": {"query": "service=cart  error=true", "limit": 20, "ids": [1, 2]},
    "window": None,
}
REQ_B = {
    "tool_name": TOOL,
    "target_ref": TARGET,
    "params": {"query": "service=checkout", "limit": 5},
    "window": None,
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    """B1 canonical JSON (I:69 reuses it for recording metadata)."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def iso(moment: datetime) -> str:
    return moment.isoformat()


# --- context ------------------------------------------------------------------


def binding(**changes: Any) -> ToolBinding:
    return replace(
        ToolBinding(
            tool=TOOL,
            version="v1",
            target=TARGET,
            source="jaeger",
            authorized_window={"start": iso(START), "end": iso(END)},
            default_window={
                "start": iso(START + timedelta(minutes=5)),
                "end": iso(END),
            },
            permissions_hash=sha(b"permissions-v1"),
            registry_hash=sha(b"registry-v1"),
            profile_hash=sha(b"profile-v1"),
            projection_hash=sha(b"projection-v1"),
        ),
        **changes,
    )


def context(**changes: Any) -> RecordingContext:
    return replace(
        RecordingContext(
            recording_id="rec-1",
            source_run_id=SOURCE_RUN,
            source_manifest_hash=sha(b"source-manifest"),
            scenario_id=SCENARIO_ID,
            scenario_version="v1",
            synthetic=True,
            versions={name: "v1" for name in COMPAT_DIMENSIONS}
            | {"prompt": "v1", "model_request": "v1", "code": "v1"},
            stimulus={
                "question": QUESTION,
                "subject_id": "subject-demo",
                "target_id": TARGET,
            },
            initial_state={"evidence_views": {}},
            input_watermark="input-0",
            control_watermark="control-0",
            started_at=START,
            ended_at=END,
            bindings=(binding(),),
        ),
        **changes,
    )


def manifest(**overrides: DimensionValue) -> Manifest:
    dims = {name: DimensionValue("version", "v1", None) for name in DIMENSIONS}
    dims.update(overrides)
    return Manifest(round_id="round-b2", dimensions=dims)


def attempt(mode: str = "replay", frozen: Manifest | None = None, **changes: Any):
    frozen = manifest() if frozen is None else frozen
    return replace(
        AttemptRecord(
            attempt_id=f"attempt-{mode}",
            round_id=frozen.round_id,
            manifest_hash=manifest_hash(frozen),
            scenario_id=SCENARIO_ID,
            scenario_version="v1",
            mode=mode,
            repetition=1,
            started=True,
            status="ran",
            replay_miss=False,
            product_run_id=REPLAY_RUN if mode == "replay" else None,
        ),
        **changes,
    )


def outcome(evidence_ids: tuple[str, ...] = ("ev-1",), **changes: Any):
    return replace(
        IncidentOutcome(
            scenario_id=SCENARIO_ID,
            final_state="completed",
            evidence_ids=evidence_ids,
            decision="report_available",
            actions=("read_only_query",),
            permissions=("read_only", "human_control"),
            human_interaction=None,
            handoff_reasons=(),
            report_available=True,
        ),
        **changes,
    )


# --- fake model / executor ----------------------------------------------------


def tool_call(call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": TOOL, "arguments": "{}"},
    }


def reply(
    *,
    calls: tuple[str, ...] = (),
    content: str | None = None,
    reasoning: str | None = None,
    raw: dict[str, Any] | None = None,
    response_id: str = "resp-1",
) -> ModelReply:
    return ModelReply(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tuple(tool_call(c) for c in calls),
        finish_reason="tool_calls" if calls else "stop",
        response_model=ACCEPTED_RESPONSE_MODEL,
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        raw={"id": response_id, "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        if raw is None
        else raw,
    )


class FakeModel:
    """Scripted ModelClient; an Exception item is raised instead of returned."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[ModelCall] = []

    def complete(self, call: ModelCall) -> ModelReply:
        self.calls.append(call)
        if not self.script:
            raise AssertionError("FakeModel script exhausted")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ForbiddenModel:
    """A client that must never be called (structural mode, I:44/I:75)."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, call: ModelCall) -> ModelReply:
        self.calls += 1
        raise AssertionError("model client must not be called")


def tool_outcome(
    request: ToolRequest,
    *,
    evidence_id: str,
    raw: bytes | None = b'{"spans":[{"service":"cart","error":true}]}',
    view: dict[str, Any] | None = None,
) -> ToolOutcome:
    window = ToolWindow(START + timedelta(minutes=5), END)
    op = ToolOperation(
        operation_id=request.operation_id,
        scope_id="scope-1",
        subject_kind="service",
        subject_id="subject-demo",
        run_id=SOURCE_RUN,
        requested_tool=request.tool_name,
        requested_target=request.target_ref,
        registry_revision="registry-v1",
        tool_registry_revision="tool-registry-v1",
        tool=TOOL,
        tool_version="v1",
        source="jaeger",
        target_id=TARGET,
        query=canonical(request.params),
        window=window,
        started_at=START + timedelta(minutes=2),
        dispatched=True,
        finished_at=START + timedelta(minutes=2, seconds=1),
    )
    if view is None:
        view = {
            "tool": TOOL,
            "status": "ok",
            "evidence_id": evidence_id,
            "operation_id": request.operation_id,
            "content": [{"service": "cart", "error": True}],
        }
    if raw is None:
        return ToolOutcome(
            operation=op,
            status="no_data",
            reason="NO_DATA",
            source_contact="confirmed",
            model_view=view,
            evidence=None,
        )
    evidence = EvidenceRecord(
        evidence_id=evidence_id,
        operation=op,
        status="ok",
        raw=raw,
        raw_sha256=sha(raw),
        _view=view,
        view_sha256=canonical_hash(view),
        projection_revision="projection-v1",
        observed_at=START + timedelta(minutes=2, seconds=1),
        data_as_of=None,
        source_start_at=None,
        source_end_at=None,
        result_count=1,
        incomplete=False,
        truncated=False,
        omitted_rows=0,
        omitted_bytes=0,
        adopted=True,
    )
    return ToolOutcome(
        operation=op,
        status="ok",
        reason=None,
        source_contact="confirmed",
        model_view=view,
        evidence=evidence,
    )


class FakeExecutor:
    """Returns pre-built outcomes in call order (outcome factories get the request)."""

    def __init__(self, factories: list[Any]) -> None:
        self.factories = list(factories)
        self.requests: list[ToolRequest] = []

    def execute(self, request: ToolRequest) -> ToolOutcome:
        self.requests.append(request)
        return self.factories.pop(0)(request)


# --- a tiny investigation, shared by source recording and replay drivers -------


def investigate(
    *,
    model: Any,
    tools: Any,
    deliver: Any,
    requests: list[dict[str, Any]],
    step_id: str,
    tick: Any = None,
    stub: bool = False,
    tail_delivery: bool = False,
    echo_reasoning: str | None = "own",
    final_timeout: float = 360.0,
    seen: list[ToolOutcome] | None = None,
) -> tuple[ModelCall, ModelCall]:
    """System+user -> tool calls -> tool messages -> final request.

    ``echo_reasoning="own"`` echoes this Run's reasoning_content (product
    behaviour, loop.py:728); a string echoes that literal value instead.
    """

    def clock(minutes: int) -> None:
        if tick is not None:
            tick(START + timedelta(minutes=minutes))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUESTION},
    ]
    first = ModelCall(
        messages=tuple(messages),
        tools=(TOOL_SCHEMA,),
        json_mode=False,
        max_tokens=1000,
        timeout_seconds=final_timeout,
    )
    clock(1)
    r1 = model.complete(first)
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": r1.content,
        "tool_calls": [dict(c) for c in r1.tool_calls],
    }
    if echo_reasoning == "own":
        assistant["reasoning_content"] = r1.reasoning_content
    elif echo_reasoning is not None:
        assistant["reasoning_content"] = echo_reasoning
    messages.append(assistant)
    for index, spec in enumerate(requests):
        clock(2 + index)
        result = tools.execute(ToolRequest(step_id=step_id, tool_index=index, **spec))
        if seen is not None:
            seen.append(result)
        content = (
            canonical({"stub": "provenance", "operation": index})
            if stub
            else canonical(dict(result.model_view))
        )
        message = {
            "role": "tool",
            "tool_call_id": r1.tool_calls[index]["id"],
            "content": content,
        }
        deliver(result.model_view, message)
        messages.append(message)
    clock(8)
    final = replace(first, messages=tuple(messages))
    model.complete(final)
    if tail_delivery and seen:
        deliver(
            seen[-1].model_view,
            {"role": "tool", "tool_call_id": "late", "content": "x"},
        )
    return first, final


def default_outcomes(count: int) -> list[Any]:
    return [
        (lambda req, i=i: tool_outcome(req, evidence_id=f"ev-{i + 1}"))
        for i in range(count)
    ]


def source_driver(
    requests: list[dict[str, Any]] | None = None,
    *,
    outcomes: list[Any] | None = None,
    stub: bool = False,
    tail_delivery: bool = False,
    result: IncidentOutcome | None = None,
    log: dict[str, Any] | None = None,
):
    """A synthetic source Run driven through CaptureHooks (I:50-56)."""
    requests = [REQ_A] if requests is None else requests

    def driver(hooks: Any) -> IncidentOutcome:
        calls = tuple(f"src_call_{i}" for i in range(len(requests)))
        fake = FakeModel(
            [
                reply(calls=calls, reasoning=SRC_REASONING, response_id="src-1"),
                reply(content='{"summary":"done"}', response_id="src-2"),
            ]
        )
        executor = FakeExecutor(
            default_outcomes(len(requests)) if outcomes is None else outcomes
        )
        seen: list[ToolOutcome] = []
        first, final = investigate(
            model=hooks.model(fake),
            tools=hooks.tools(executor),
            deliver=hooks.delivery,
            requests=requests,
            step_id="src-step-1",
            tick=hooks.clock,
            stub=stub,
            tail_delivery=tail_delivery,
            seen=seen,
        )
        if log is not None:
            log.update(first=first, final=final, seen=seen, model=fake)
        ids = tuple(o.evidence.evidence_id for o in seen if o.evidence is not None)
        return outcome(ids) if result is None else result

    return driver


# --- package inspection and tampering ------------------------------------------


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def package_json(pkg: Path) -> dict[str, Any]:
    return read_json(pkg / "recording.json")


def blob_bytes(pkg: Path, ref: dict[str, Any]) -> bytes:
    return (pkg / ref["path"]).read_bytes()


def blob_json(pkg: Path, ref: dict[str, Any]) -> Any:
    return json.loads(blob_bytes(pkg, ref))


def events_of(doc: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [event for event in doc["events"] if event["kind"] == kind]


def write_recording(pkg: Path, doc: dict[str, Any]) -> str:
    """Rewrite recording.json canonically; return its new SHA (expected_hash)."""
    path = pkg / "recording.json"
    data = canonical_bytes(doc)
    path.unlink()
    path.write_bytes(data)
    return sha(data)


def put_blob(pkg: Path, ref: dict[str, Any], data: bytes) -> None:
    """Store ``data`` as a new content-addressed blob and repoint ``ref``."""
    digest = sha(data)
    target = pkg / "blobs" / digest
    if not target.exists():
        target.write_bytes(data)
    ref.update(path=f"blobs/{digest}", sha256=digest, bytes=len(data))


def all_package_bytes(pkg: Path) -> bytes:
    chunks = []
    for root, _dirs, files in os.walk(pkg):
        for name in files:
            chunks.append(Path(root, name).read_bytes())
    return b"\n".join(chunks)


def structural(rec: Any, **changes: Any):
    mode_attempt = changes.pop("attempt", None) or attempt("structural")
    return replay(
        rec,
        mode="structural",
        attempt=mode_attempt,
        manifest=changes.pop("manifest", None) or manifest(),
        investigator=changes.pop("investigator", None),
        **changes,
    )


def verify_code(pkg: Path, expected_hash: str) -> str | None:
    """Load + structural verification; return the fixed error code or None.

    The interface lets either step detect damage (I:69 reader checks, I:75
    structural re-load), so the observable contract is the combined code.
    """
    try:
        rec = load_recording(pkg / "recording.json", expected_hash=expected_hash)
        if not rec.complete:
            # I:41/I:80: an incomplete package may be loaded for inspection
            # but must never be used as a complete one.
            try:
                structural(rec)
            except RecordingError as exc:
                return exc.code
            return "ACCEPTED_INCOMPLETE"
        capture = structural(rec)
    except RecordingError as exc:
        assert str(exc) == exc.code
        return exc.code
    assert capture.structural_verified is True
    return None
