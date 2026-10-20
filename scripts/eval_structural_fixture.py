"""Deterministic synthetic CANARY fixtures for the CI structural replay (PR-D1).

Interface: docs/tasks/2026-10-18-eval-pr-d-interface.md section 2. A fixed
clock, fixed IDs, a scripted model and a scripted executor drive B2's
``record()``; nothing reads the environment, a network or a credential.
Running the same code twice yields byte-identical directories.

    python scripts/eval_structural_fixture.py generate --name canary-fault-v1 \\
        --out tests/fixtures/eval_structural
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation import (
    REQUIRED_DIMENSIONS,
    AttemptRecord,
    CaptureHooks,
    DimensionValue,
    Manifest,
    RecordingContext,
    ToolBinding,
    load_recording,
    manifest_hash,
    record,
)
from opspilot.evaluation.codec import canonical_bytes
from opspilot.evaluation.recording import wire
from opspilot.investigation.loop import (
    ACCEPTED_RESPONSE_MODEL,
    ModelCall,
    ModelReply,
    serialized_request,
)
from opspilot.tools.executor import ToolRequest
from opspilot.tools.outcomes import EvidenceRecord, ToolOperation, ToolOutcome
from opspilot.tools.outcomes import Window as ToolWindow
from opspilot.tools.registry import canonical, canonical_hash

FIXTURE_SCHEMA = "opspilot-eval-structural-fixture-1"
KINDS = ("model", "http", "tool", "delivery", "refusal")

# Every name below is synthetic (interface section 1, D02).
START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
END = START + timedelta(minutes=10)
SCENARIO_ID = "canary-fault"
SCENARIO_VERSION = "canary-v1"
VERSION = "canary-v1"
SOURCE_RUN = "canary-source-run-1"
TARGET = "canary-target"
SUBJECT = "canary-svc"
TOOL = "canary_trace_search"
SOURCE = "canary-source"
STEP = "canary-step-1"
CALL_ID = "canary-call-1"
EVIDENCE_ID = "canary-ev-1"
SYSTEM = "CANARY system prompt: read-only synthetic investigator."
QUESTION = "CANARY question: why does canary-svc fail canary-op?"
PARAMS = {"service": SUBJECT, "operation": "canary-op", "limit": 5}
TOOL_SCHEMA = {
    "type": "function",
    "function": {"name": TOOL, "parameters": {"type": "object"}},
}
COMPAT = ("tools_schema", "registry", "profile", "projection", "permissions")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class _ScriptedModel:
    """Returns scripted replies and reports each one's provider HTTP pair."""

    def __init__(self, hooks: CaptureHooks, replies: list[ModelReply]) -> None:
        self.hooks = hooks
        self.replies = list(replies)

    def complete(self, call: ModelCall) -> ModelReply:
        reply = self.replies.pop(0)
        payload = {
            "id": reply.raw["id"],
            "object": "chat.completion",
            "model": reply.response_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply.content,
                        "reasoning_content": reply.reasoning_content,
                        "tool_calls": [dict(item) for item in reply.tool_calls],
                    },
                    "finish_reason": reply.finish_reason,
                }
            ],
            "usage": dict(reply.usage),
        }
        self.hooks.http(serialized_request(call), canonical_bytes(payload), 200)
        return reply


class _ScriptedExecutor:
    def execute(self, request: ToolRequest) -> ToolOutcome:
        window = ToolWindow(START + timedelta(minutes=5), END)
        operation = ToolOperation(
            operation_id=request.operation_id,
            scope_id="canary-scope",
            subject_kind="service",
            subject_id=SUBJECT,
            run_id=SOURCE_RUN,
            requested_tool=request.tool_name,
            requested_target=request.target_ref,
            registry_revision=VERSION,
            tool_registry_revision=VERSION,
            tool=TOOL,
            tool_version=VERSION,
            source=SOURCE,
            target_id=TARGET,
            query=canonical(request.params),
            window=window,
            started_at=START + timedelta(minutes=2),
            dispatched=True,
            finished_at=START + timedelta(minutes=2, seconds=1),
        )
        view = {
            "tool": TOOL,
            "status": "ok",
            "evidence_id": EVIDENCE_ID,
            "operation_id": request.operation_id,
            "content": [{"service": SUBJECT, "operation": "canary-op", "error": True}],
        }
        raw = b'{"spans":[{"service":"canary-svc","operation":"canary-op"}]}'
        evidence = EvidenceRecord(
            evidence_id=EVIDENCE_ID,
            operation=operation,
            status="ok",
            raw=raw,
            raw_sha256=_sha(raw),
            _view=view,
            view_sha256=canonical_hash(view),
            projection_revision=VERSION,
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
            operation=operation,
            status="ok",
            reason=None,
            source_contact="confirmed",
            model_view=view,
            evidence=evidence,
        )


def _reply(response_id: str, *, calls: Sequence[dict[str, Any]] = ()) -> ModelReply:
    usage = {"prompt_tokens": 10, "completion_tokens": 5}
    return ModelReply(
        content=None if calls else '{"summary":"CANARY synthetic report"}',
        reasoning_content=None,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        response_model=ACCEPTED_RESPONSE_MODEL,
        usage=usage,
        raw={"id": response_id, "usage": usage},
    )


def _canary_fault_driver(hooks: CaptureHooks) -> IncidentOutcome:
    """model -> tool -> delivery -> model, each model call with one HTTP pair."""
    call = {
        "id": CALL_ID,
        "type": "function",
        "function": {"name": TOOL, "arguments": json.dumps(PARAMS)},
    }
    model = hooks.model(
        _ScriptedModel(
            hooks, [_reply("canary-resp-1", calls=[call]), _reply("canary-resp-2")]
        )
    )
    tools = hooks.tools(_ScriptedExecutor())
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUESTION},
    ]
    first = ModelCall(
        messages=tuple(messages),
        tools=(TOOL_SCHEMA,),
        json_mode=False,
        max_tokens=1000,
        timeout_seconds=60.0,
    )
    hooks.clock(START + timedelta(minutes=1))
    reply = model.complete(first)
    messages.append(
        {"role": "assistant", "content": None, "tool_calls": list(reply.tool_calls)}
    )
    hooks.clock(START + timedelta(minutes=2))
    outcome = tools.execute(
        ToolRequest(
            step_id=STEP, tool_index=0, tool_name=TOOL, target_ref=TARGET, params=PARAMS
        )
    )
    message = {
        "role": "tool",
        "tool_call_id": CALL_ID,
        "content": canonical(dict(outcome.model_view)),
    }
    hooks.delivery(outcome.model_view, message)
    messages.append(message)
    hooks.clock(START + timedelta(minutes=8))
    model.complete(
        ModelCall(
            messages=tuple(messages),
            tools=(TOOL_SCHEMA,),
            json_mode=False,
            max_tokens=1000,
            timeout_seconds=60.0,
        )
    )
    return IncidentOutcome(
        scenario_id=SCENARIO_ID,
        final_state="completed",
        evidence_ids=(EVIDENCE_ID,),
        decision="report_available",
        actions=("read_only_query",),
        permissions=("read_only", "human_control"),
        human_interaction=None,
        handoff_reasons=(),
        report_available=True,
    )


def _canary_fault_context(name: str) -> RecordingContext:
    return RecordingContext(
        recording_id=name,
        source_run_id=SOURCE_RUN,
        source_manifest_hash=_sha(b"canary-source-manifest-v1"),
        scenario_id=SCENARIO_ID,
        scenario_version=SCENARIO_VERSION,
        synthetic=True,
        versions={key: VERSION for key in COMPAT}
        | {"prompt": VERSION, "model_request": VERSION, "code": VERSION},
        stimulus={"question": QUESTION, "subject_id": SUBJECT, "target_id": TARGET},
        initial_state={"evidence_views": {}},
        input_watermark="canary-input-0",
        control_watermark="canary-control-0",
        started_at=START,
        ended_at=END,
        bindings=(
            ToolBinding(
                tool=TOOL,
                version=VERSION,
                target=TARGET,
                source=SOURCE,
                authorized_window={"start": START.isoformat(), "end": END.isoformat()},
                default_window={
                    "start": (START + timedelta(minutes=5)).isoformat(),
                    "end": END.isoformat(),
                },
                permissions_hash=_sha(b"canary-permissions-v1"),
                registry_hash=_sha(b"canary-registry-v1"),
                profile_hash=_sha(b"canary-profile-v1"),
                projection_hash=_sha(b"canary-projection-v1"),
            ),
        ),
    )


@dataclass(frozen=True)
class _Spec:
    context: Callable[[str], RecordingContext]
    driver: Callable[[CaptureHooks], IncidentOutcome]


# The supported names are fixed in code (interface section 2).
FIXTURES: dict[str, _Spec] = {
    "canary-fault-v1": _Spec(_canary_fault_context, _canary_fault_driver),
}


def _manifest(recording_sha256: str) -> Manifest:
    dimensions = {
        name: DimensionValue("version", VERSION, None) for name in REQUIRED_DIMENSIONS
    }
    dimensions["replay_fixture"] = DimensionValue("hash", recording_sha256, None)
    return Manifest(round_id="canary-structural", dimensions=dimensions)


def generate(name: str, out: Path) -> int:
    spec = FIXTURES.get(name)
    if spec is None:
        print(f"unknown fixture name: {name!r}", file=sys.stderr)
        return 2
    target = out / name
    if os.path.lexists(target):
        print(f"refusing to overwrite {target}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    ref = record(
        spec.driver, destination=target / "recording", context=spec.context(name)
    )
    recording = load_recording(
        target / "recording" / ref.path, expected_hash=ref.sha256
    )
    manifest = _manifest(ref.sha256)
    attempt = AttemptRecord(
        attempt_id=f"{name}-structural",
        round_id=manifest.round_id,
        manifest_hash=manifest_hash(manifest),
        scenario_id=recording.context.scenario_id,
        scenario_version=recording.context.scenario_version,
        mode="structural",
        repetition=1,
        started=True,
        status="ran",
        replay_miss=False,
        product_run_id=None,
    )
    counts = Counter(str(event.kind) for event in recording.events)
    document = {
        "schema": FIXTURE_SCHEMA,
        "name": name,
        "recording_sha256": ref.sha256,
        "attempt": wire(attempt),
        "manifest": wire(manifest),
        "expected_event_counts": {kind: counts.get(kind, 0) for kind in KINDS},
    }
    (target / "fixture.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("generate")
    command.add_argument("--name", required=True)
    command.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return generate(args.name, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
