"""Offline structural verification and current-model frozen-evidence replay."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation.codec import (
    StorageError,
    canonical_bytes,
    validated,
)
from opspilot.evaluation.model import AttemptRecord
from opspilot.evaluation.recording import (
    MAX_METADATA_BYTES,
    CaptureHooks,
    Recording,
    RecordingEvent,
    ToolFacade,
    _read,
    binding_for,
    blob,
    json_blob,
    load_recording,
    record,
    sha,
    tool_key,
    unwire,
)
from opspilot.evaluation.rounds import (
    Manifest,
    ManifestError,
    manifest_hash,
    validate_manifest,
)
from opspilot.evaluation.storage import ArtifactRef
from opspilot.investigation.loop import ModelCall, ModelClient, ModelReply
from opspilot.tools.executor import ToolRequest
from opspilot.tools.outcomes import ToolOutcome


@dataclass(frozen=True)
class ReplayCapture:
    attempt: AttemptRecord
    outcome: IncidentOutcome | None
    recording_ref: ArtifactRef | None
    source_run_id: str
    replay_run_id: str
    business_time: datetime
    structural_verified: bool
    error: str | None
    id_map: Mapping[str, str]


class _ReplayClock:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def monotonic(self) -> float:
        return time.monotonic()


class ReplaySession:
    """Driver-facing, isolated collaborators. No live tool fallback exists."""

    def __init__(
        self,
        recording: Recording,
        hooks: CaptureHooks,
        investigator: ModelClient,
        run_id: str,
    ) -> None:
        self.context = recording.context
        self.clock = _ReplayClock(recording.context.ended_at)
        self.capture_delivery = hooks.delivery
        self.capture_http = hooks.http
        self.id_map: dict[str, str] = {}
        self.error: str | None = None
        self._opened = time.monotonic()
        self._reasoning: set[bytes] = set()
        self._model_name: str | None = None
        self._recording = recording
        self._hooks = hooks
        self._run_id = run_id
        self._index: dict[str, list[RecordingEvent]] = defaultdict(list)
        self._counts: dict[str, int] = defaultdict(int)
        for event in recording.events:
            if event.kind == "tool":
                obj = json_blob(recording, event.request_ref)
                assert event.response_ref is not None
                self._index[obj["key"]].append(event)
        self.tools: ToolFacade = _SessionTools(self, hooks.tools(_FrozenTools(self)))
        self.model: ModelClient = _CurrentModel(self, hooks.model(investigator))

    def _remaining(self) -> float:
        remaining = 7200.0 - (time.monotonic() - self._opened)
        if remaining <= 0:
            self.error = "TIMEOUT"
            raise StorageError("TIMEOUT")
        if self.error is not None:
            raise StorageError(self.error)
        return remaining


class _SessionTools:
    def __init__(self, session: ReplaySession, captured: ToolFacade) -> None:
        self.session = session
        self.captured = captured

    def __getattr__(self, name: str) -> Any:
        return getattr(self.captured, name)

    def execute(self, request: ToolRequest) -> ToolOutcome:
        self.session._remaining()
        try:
            return self.captured.execute(request)
        except StorageError as exc:
            self.session.error = exc.code
            raise


class _FrozenTools:
    def __init__(self, session: ReplaySession) -> None:
        self.session = session

    @property
    def tool_seconds_used(self) -> float:
        # Frozen source costs are observations, never newly spent tool time.
        return 0.0

    @property
    def operations_used(self) -> int:
        return sum(self.session._counts.values())

    def execute(self, request: ToolRequest) -> ToolOutcome:
        session = self.session
        session._remaining()
        try:
            binding = binding_for(session.context, request)
            key = tool_key(session.context, request, binding)
            occurrence = session._counts[key]
            choices = session._index.get(key, [])
            if occurrence >= len(choices):
                raise StorageError("REPLAY_MISS")
            event = choices[occurrence]
            assert event.response_ref is not None
            outcome = cast(
                ToolOutcome,
                unwire(ToolOutcome, json_blob(session._recording, event.response_ref)),
            )
            session._counts[key] += 1
            session.id_map[request.operation_id] = outcome.operation.operation_id
            source = session._hooks.context.initial_state["replay_source"]
            source["id_map"][request.operation_id] = outcome.operation.operation_id
            session._remaining()
            return outcome
        except StorageError as exc:
            session.error = exc.code
            raise


def _protocol_key(message: Mapping[str, Any]) -> bytes:
    return canonical_bytes(
        {
            name: message.get(name)
            for name in ("content", "reasoning_content", "tool_calls")
        }
    )


class _CurrentModel:
    def __init__(self, session: ReplaySession, captured: ModelClient) -> None:
        self.session = session
        self.captured = captured

    def complete(self, call: ModelCall) -> ModelReply:
        session = self.session
        remaining = session._remaining()
        validated(ModelCall, call)
        if session._model_name is None:
            session._model_name = call.model
        elif session._model_name != call.model:
            session.error = "IDENTITY_MISMATCH"
            raise StorageError("IDENTITY_MISMATCH")
        # Do not infer or transform source history. Private reasoning can only
        # be echoed from an actual current-Run response, including its pairing.
        for message in call.messages:
            if set(message) - {
                "role",
                "content",
                "tool_calls",
                "tool_call_id",
                "reasoning_content",
            }:
                session.error = "SCHEMA_INVALID"
                raise StorageError("SCHEMA_INVALID")
            if (
                "reasoning_content" in message
                and _protocol_key(message) not in session._reasoning
            ):
                session.error = "IDENTITY_MISMATCH"
                raise StorageError("IDENTITY_MISMATCH")
        if call.timeout_seconds <= 0:
            session.error = "TIMEOUT"
            raise StorageError("TIMEOUT")
        timeout = min(call.timeout_seconds, 360.0, remaining)
        opened = time.monotonic()
        reply = self.captured.complete(replace(call, timeout_seconds=timeout))
        if time.monotonic() - opened > timeout:
            session.error = "TIMEOUT"
            raise StorageError("TIMEOUT")
        session._remaining()
        if set(reply.raw) - {"id", "usage"}:
            session.error = "SCHEMA_INVALID"
            raise StorageError("SCHEMA_INVALID")
        session._reasoning.add(
            _protocol_key(
                {
                    "content": reply.content,
                    "reasoning_content": reply.reasoning_content,
                    "tool_calls": list(reply.tool_calls),
                }
            )
        )
        return reply


def _identity(
    recording: Recording, mode: str, attempt: AttemptRecord, manifest: Manifest
) -> None:
    validated(AttemptRecord, attempt)
    validated(Manifest, manifest)
    try:
        validate_manifest(manifest)
    except ManifestError:
        raise StorageError("SCHEMA_INVALID") from None
    context = recording.context
    if mode not in ("structural", "replay"):
        raise StorageError("SCHEMA_INVALID")
    if (
        attempt.mode != mode
        or not attempt.started
        or attempt.manifest_hash != manifest_hash(manifest)
        or attempt.round_id != manifest.round_id
        or attempt.scenario_id != context.scenario_id
        or attempt.scenario_version != context.scenario_version
        or not attempt.product_run_id
        or attempt.product_run_id == context.source_run_id
    ):
        raise StorageError("IDENTITY_MISMATCH")
    fixture = manifest.dimensions.get("replay_fixture")
    if (
        fixture is None
        or fixture.kind != "hash"
        or fixture.value != recording.ref.sha256
    ):
        raise StorageError("IDENTITY_MISMATCH")


def _compatible(recording: Recording, manifest: Manifest) -> bool:
    for key in ("tools_schema", "registry", "profile", "projection", "permissions"):
        dimension = manifest.dimensions.get(key)
        if (
            dimension is None
            or dimension.kind
            not in (
                "hash",
                "version",
            )
            or dimension.value != recording.context.versions.get(key)
        ):
            return False
    return True


def replay(
    recording: Recording,
    *,
    mode: Literal["replay", "structural"],
    attempt: AttemptRecord,
    manifest: Manifest,
    investigator: ModelClient | None,
    driver: Callable[[ReplaySession], IncidentOutcome] | None = None,
    destination: Path | None = None,
) -> ReplayCapture:
    # Do not trust a manually constructed Recording or mutable DTO contents.
    source = load_recording(recording.path, expected_hash=recording.ref.sha256)
    _identity(source, mode, attempt, manifest)
    assert attempt.product_run_id is not None
    base = ReplayCapture(
        attempt,
        None,
        None,
        source.context.source_run_id,
        attempt.product_run_id,
        source.context.ended_at,
        True,
        None,
        {},
    )
    if mode == "structural":
        return base
    if investigator is None or driver is None or destination is None:
        raise StorageError("SCHEMA_INVALID")
    if not _compatible(source, manifest):
        return replace(
            base,
            attempt=replace(attempt, status="aborted", replay_miss=True),
            error="REPLAY_MISS",
        )
    initial = dict(source.context.initial_state)
    initial["replay_source"] = {
        "run_id": source.context.source_run_id,
        "recording_hash": source.ref.sha256,
        "id_map": {},
    }
    context = replace(
        source.context,
        recording_id=attempt.attempt_id,
        source_run_id=attempt.product_run_id,
        source_manifest_hash=manifest_hash(manifest),
        initial_state=initial,
        input_refs=(),
        source_versions=source.context.versions,
        started_at=source.context.ended_at,
        ended_at=source.context.ended_at,
        versions={
            **source.context.versions,
            **{
                key: str(value.value)
                for key, value in manifest.dimensions.items()
                if value.kind in ("hash", "version")
            },
        },
    )
    session: ReplaySession | None = None
    outcome: IncidentOutcome | None = None

    def drive(hooks: CaptureHooks) -> IncidentOutcome:
        nonlocal session, outcome
        hooks.clock(source.context.ended_at)
        for ref in source.context.input_refs:
            hooks.input(blob(source, ref))
        session = ReplaySession(
            source, hooks, investigator, attempt.product_run_id or ""
        )
        outcome = driver(session)
        session._remaining()  # Sticky miss/timeout survives a driver catching it.
        hooks.finish(source.context.ended_at)
        return outcome

    try:
        ref = record(drive, destination=destination, context=context)
        assert session is not None
        return replace(
            base, outcome=outcome, recording_ref=ref, id_map=dict(session.id_map)
        )
    except StorageError as exc:
        if exc.code in ("OUTPUT_EXISTS", "EVIDENCE_UNAVAILABLE"):
            raise
        code = (
            session.error
            if session is not None and session.error is not None
            else exc.code
        )
        if code not in {
            "REPLAY_MISS",
            "TIMEOUT",
            "IDENTITY_MISMATCH",
            "SCHEMA_INVALID",
        }:
            code = "DRIVER_ERROR"
        ref = None
        path = destination / "recording.json"
        if path.is_file():
            raw = _read(path, MAX_METADATA_BYTES)
            ref = ArtifactRef(
                "recording.json", sha(raw), len(raw), "application/json", "private"
            )
        return replace(
            base,
            attempt=replace(
                attempt, status="aborted", replay_miss=code == "REPLAY_MISS"
            ),
            outcome=outcome,
            recording_ref=ref,
            error=code,
            id_map={} if session is None else dict(session.id_map),
        )
