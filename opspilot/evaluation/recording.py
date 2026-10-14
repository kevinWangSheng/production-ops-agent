"""Private, injected capture at the physical model and tool facade boundaries.

No runtime imports this module. Artifacts are written individually, never as an
in-memory ledger; only the bounded metadata index lives for the whole Run.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import types
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Literal,
    Protocol,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation.codec import (
    StorageError,
    canonical_bytes,
    decode,
    encode,
    exact,
    invalid,
    parse,
    validated,
)
from opspilot.evaluation.rounds import Manifest
from opspilot.evaluation.storage import (
    ArtifactRef,
    _close,
    _local_path,
    _parent,
    _publish,
    _ref,
)
from opspilot.evaluation.tool_provenance import (
    ToolSnapshot,
    check_result,
    validate_snapshot_context,
)
from opspilot.investigation.loop import (
    ModelCall,
    ModelClient,
    ModelError,
    ModelReply,
    _call_fingerprint,
    _duplicate_call_view,
    serialized_request,
)
from opspilot.investigation.messages import PairingError, validate_tool_calls
from opspilot.tools.executor import ToolRequest
from opspilot.tools.outcomes import ToolOutcome, Window
from opspilot.tools.registry import ToolContractError

RecordingError = StorageError
RecordingRef = ArtifactRef
SCHEMA = "opspilot-replay-1"
MIB = 1024 * 1024
MAX_REQUEST_BYTES = 8 * MIB
MAX_RESPONSE_BYTES = 2 * MIB
MAX_RAW_BYTES = 2 * MIB
MAX_VIEW_BYTES = 8 * MIB
MAX_METADATA_BYTES = 8 * MIB
# JSON escaping and hex-encoded raw bytes enlarge individual DTO envelopes.
MAX_BLOB_BYTES = 32 * MIB
ERROR_CODES = frozenset(
    {
        "REPLAY_MISS",
        "RECORDING_INCOMPLETE",
        "HASH_MISMATCH",
        "IDENTITY_MISMATCH",
        "SCHEMA_INVALID",
        "OUTPUT_EXISTS",
        "EVIDENCE_UNAVAILABLE",
        "DRIVER_ERROR",
        "TIMEOUT",
    }
)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash(value: str) -> None:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        invalid()


@dataclass(frozen=True)
class ToolBinding:
    tool: str
    version: str
    target: str
    source: str
    authorized_window: Mapping[str, Any]
    default_window: Mapping[str, Any] | None
    permissions_hash: str
    registry_hash: str
    profile_hash: str
    projection_hash: str


@dataclass(frozen=True)
class RecordingContext:
    recording_id: str
    source_run_id: str
    source_manifest_hash: str
    scenario_id: str
    scenario_version: str
    synthetic: bool
    versions: Mapping[str, str]
    stimulus: Mapping[str, Any]
    initial_state: Mapping[str, Any]
    input_watermark: str
    control_watermark: str
    started_at: datetime
    ended_at: datetime
    bindings: tuple[ToolBinding, ...]
    input_refs: tuple[ArtifactRef, ...] = ()
    source_versions: Mapping[str, str] | None = None
    clock_policy: Literal["event-clock/frozen-end-real-monotonic"] = (
        "event-clock/frozen-end-real-monotonic"
    )
    tool_snapshots: tuple[ToolSnapshot, ...] = ()
    source_manifest: Manifest | None = None


@dataclass(frozen=True)
class RecordingEvent:
    seq: int
    kind: Literal["model", "tool", "delivery", "http", "refusal"]
    parent: int | None
    request_ref: ArtifactRef
    response_ref: ArtifactRef | None
    error: str | None
    clock: datetime
    identity: str
    hashes: Mapping[str, str]


@dataclass(frozen=True)
class Recording:
    path: Path
    ref: ArtifactRef
    context: RecordingContext
    events: tuple[RecordingEvent, ...]
    outcome: IncidentOutcome | None
    complete: bool
    incomplete_reasons: tuple[str, ...]


class ToolFacade(Protocol):
    def execute(self, request: ToolRequest) -> ToolOutcome: ...


def _context(context: RecordingContext) -> RecordingContext:
    try:
        context = validated(RecordingContext, context)
    except ToolContractError:
        raise StorageError("IDENTITY_MISMATCH") from None
    validate_snapshot_context(context)
    for value in (
        context.recording_id,
        context.source_run_id,
        context.scenario_id,
        context.scenario_version,
        context.input_watermark,
        context.control_watermark,
    ):
        if not value.strip():
            invalid()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", context.recording_id):
        invalid()
    _hash(context.source_manifest_hash)
    if context.started_at > context.ended_at:
        invalid()
    seen: set[tuple[str, str]] = set()
    for binding in context.bindings:
        identity = (binding.tool, binding.target)
        if identity in seen or not all(identity + (binding.version, binding.source)):
            invalid()
        seen.add(identity)
        _window(binding.authorized_window)
        if binding.default_window is not None:
            default = _window(binding.default_window)
            auth = _window(binding.authorized_window)
            if default.start < auth.start or default.end > auth.end:
                invalid()
        for name in ("permissions", "registry", "profile", "projection"):
            value = getattr(binding, name + "_hash")
            _hash(value)
    if not context.synthetic:
        for name in (
            "tools_schema",
            "permissions",
            "registry",
            "profile",
            "projection",
        ):
            if not context.versions.get(name, "").strip():
                invalid()
    _no_credentials(encode(context))
    _no_private(encode(context))
    return context


def _window(value: Mapping[str, Any]) -> Window:
    return cast(Window, decode(Window, value))


def _no_credentials(value: Any) -> None:
    # Structural secret channels are forbidden. Opaque business strings are not
    # scanned/redacted: changing their bytes would destroy exact replay.
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key.lower() in {
                "authorization",
                "proxy-authorization",
                "api_key",
                "api-key",
                "x-api-key",
                "cookie",
                "set-cookie",
                "headers",
            }:
                invalid()
            _no_credentials(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _no_credentials(item)


def _no_private(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {
                "reasoning_content",
                "reasoning_details",
                "provider_specific_fields",
                "thinking",
                "signature",
            }:
                invalid()
            _no_private(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _no_private(item)


def wire(value: Any) -> Any:
    """B1 JSON with bytes represented as hex, only for tool outcome DTOs."""
    if isinstance(value, bytes):
        return value.hex()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return encode({key: wire(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return [wire(item) for item in value]
    return encode(value)


def unwire(annotation: Any, value: Any) -> Any:
    if annotation is object:
        return encode(value)
    if annotation is bytes:
        if type(value) is not str or not re.fullmatch(r"(?:[0-9a-f]{2})*", value):
            invalid()
        return bytes.fromhex(value)
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        for option in args:
            try:
                return unwire(option, value)
            except StorageError:
                continue
        invalid()
    if origin in (Mapping, dict):
        if type(value) is not dict:
            invalid()
        return {
            decode(args[0], key): unwire(args[1], item) for key, item in value.items()
        }
    if isinstance(annotation, type) and is_dataclass(annotation):
        obj = exact(value, {field.name for field in fields(annotation)})
        hints = get_type_hints(annotation)
        try:
            return annotation(
                **{key: unwire(hints[key], item) for key, item in obj.items()}
            )
        except (TypeError, ValueError):
            invalid()
    return decode(annotation, value)


def _read(path: Path, limit: int) -> bytes:
    parent, name = _parent(path)
    raw = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StorageError("EVIDENCE_UNAVAILABLE")
            if info.st_size > limit:
                raise StorageError("RECORDING_INCOMPLETE")
            raw = file.read(limit + 1)
            if len(raw) > limit:
                raise StorageError("RECORDING_INCOMPLETE")
    except StorageError:
        raise
    except (OSError, ValueError):
        raw = None
    finally:
        if not _close(parent):
            raw = None
    if raw is None:
        raise StorageError("EVIDENCE_UNAVAILABLE") from None
    return raw


def blob(recording: Recording, ref: ArtifactRef) -> bytes:
    _ref(ref)
    if ref.visibility != "private" or ref.path != "blobs/" + ref.sha256:
        invalid()
    raw = _read(recording.path.parent / ref.path, MAX_BLOB_BYTES)
    if len(raw) != ref.bytes or sha(raw) != ref.sha256:
        raise StorageError("HASH_MISMATCH")
    return raw


def json_blob(recording: Recording, ref: ArtifactRef) -> dict[str, Any]:
    if ref.media_type != "application/json":
        invalid()
    return parse(blob(recording, ref))


def binding_for(context: RecordingContext, request: ToolRequest) -> ToolBinding:
    for binding in context.bindings:
        if binding.tool == request.tool_name and binding.target == request.target_ref:
            return binding
    raise StorageError("REPLAY_MISS")


def tool_key(
    context: RecordingContext, request: ToolRequest, binding: ToolBinding
) -> str:
    return sha(
        canonical_bytes(
            {
                "fixture_hash": context.source_manifest_hash,
                "binding": binding,
                "tool_name": request.tool_name,
                "target_ref": request.target_ref,
                "params": request.params,
                "window": request.window,
            }
        )
    )


def _create(destination: Path, context: RecordingContext) -> None:
    destination = _local_path(destination)
    if not context.synthetic:
        root = Path(__file__).resolve().parents[2] / "tmp" / "eval"
        if destination.absolute() != root / context.recording_id:
            invalid()
    parent, name = _parent(destination)
    failure = None
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        failure = "OUTPUT_EXISTS"
    except (OSError, ValueError):
        failure = "EVIDENCE_UNAVAILABLE"
    finally:
        if not _close(parent):
            failure = "EVIDENCE_UNAVAILABLE"
    if failure is not None:
        raise StorageError(failure) from None
    _publish(destination / "INCOMPLETE", b"RECORDING_INCOMPLETE", "text/plain")
    try:
        os.mkdir(destination / "blobs", 0o700)
    except (OSError, ValueError):
        pass
    else:
        return
    raise StorageError("EVIDENCE_UNAVAILABLE") from None


class CaptureHooks:
    """Per-Run, synchronous injection. Never attach to a different Run/client."""

    def __init__(self, destination: Path, context: RecordingContext) -> None:
        self.destination = destination
        self.context = context
        self.events: list[RecordingEvent] = []
        self.reasons: list[str] = []
        self._at = context.started_at
        self._sealed = False
        self._model_parent: int | None = None
        self._operations: dict[str, int] = {}
        self._pending_views: dict[str, list[int]] = {}
        self._delivered_tools: set[int] = set()
        self._counts: Counter[str] = Counter()
        self._current_calls: dict[str, Mapping[str, Any]] = {}
        self._claimed_calls: set[str] = set()
        self._tool_fingerprints: dict[int, str] = {}
        self._evidence_events: dict[str, int] = {}

    def clock(self, at: datetime) -> None:
        at = decode(datetime, encode(at))
        if self._sealed or at < self._at:
            raise StorageError("IDENTITY_MISMATCH")
        self._at = at

    def input(self, raw: bytes) -> ArtifactRef:
        """Freeze an original JSON input without exporting it to the model."""
        if self._sealed or type(raw) is not bytes:
            invalid()
        if len(raw) > MAX_REQUEST_BYTES:
            self.reasons.append("RECORDING_INCOMPLETE")
            raise StorageError("RECORDING_INCOMPLETE")
        obj = parse(raw)
        _no_credentials(obj)
        _no_private(obj)
        ref = self._put(raw, limit=MAX_REQUEST_BYTES)
        self.context = replace(self.context, input_refs=(*self.context.input_refs, ref))
        return ref

    def finish(self, at: datetime) -> None:
        """Seal a live recording with the driver's final business-clock read."""
        self.clock(at)
        self.context = replace(self.context, ended_at=at)
        self._sealed = True

    def control(self, event: object) -> None:
        self.reasons.append("SCHEMA_INVALID")
        raise StorageError("SCHEMA_INVALID")

    def _put(
        self,
        raw: bytes,
        media_type: str = "application/json",
        *,
        limit: int = MAX_BLOB_BYTES,
    ) -> ArtifactRef:
        if len(raw) > limit:
            self.reasons.append("RECORDING_INCOMPLETE")
            raise StorageError("RECORDING_INCOMPLETE")
        digest = sha(raw)
        path = self.destination / "blobs" / digest
        try:
            _publish(path, raw, media_type)
        except StorageError as exc:
            if exc.code != "OUTPUT_EXISTS":
                raise
            if _read(path, limit) != raw:
                raise StorageError("HASH_MISMATCH") from None
        return ArtifactRef("blobs/" + digest, digest, len(raw), media_type, "private")

    def _begin(
        self,
        kind: Literal["model", "tool", "delivery", "http", "refusal"],
        request: bytes,
        hashes: Mapping[str, str],
        *,
        parent: int | None = None,
        media_type: str = "application/json",
    ) -> int:
        if self._sealed:
            self.reasons.append("RECORDING_INCOMPLETE")
            raise StorageError("RECORDING_INCOMPLETE")
        seq = len(self.events)
        self.events.append(
            RecordingEvent(
                seq,
                kind,
                parent,
                self._put(request, media_type),
                None,
                None,
                self._at,
                self.context.source_run_id,
                dict(hashes),
            )
        )
        return seq

    def _end(
        self,
        seq: int,
        response: bytes | None,
        *,
        error: str | None = None,
        media_type: str = "application/json",
        limit: int = MAX_BLOB_BYTES,
    ) -> None:
        ref = None if response is None else self._put(response, media_type, limit=limit)
        self.events[seq] = replace(self.events[seq], response_ref=ref, error=error)

    def model(self, client: ModelClient) -> ModelClient:
        hooks = self

        class CapturedModel:
            def complete(self, call: ModelCall) -> ModelReply:
                body = serialized_request(call)
                if len(body) > MAX_REQUEST_BYTES:
                    hooks.reasons.append("RECORDING_INCOMPLETE")
                    raise StorageError("RECORDING_INCOMPLETE")
                _no_credentials(parse(body))
                seq = hooks._begin(
                    "model",
                    canonical_bytes({"call": call, "body": body.decode("utf-8")}),
                    {
                        "request_body_sha256": sha(body),
                        "messages_sha256": sha(canonical_bytes(call.messages)),
                    },
                )
                hooks._model_parent = seq
                try:
                    reply = client.complete(call)
                    validated(ModelReply, reply)
                    calls = validate_tool_calls({"tool_calls": list(reply.tool_calls)})
                    hooks._current_calls = {item["id"]: item for item in calls}
                    hooks._claimed_calls = set()
                    hooks._end(seq, canonical_bytes(reply), limit=MAX_RESPONSE_BYTES)
                    return reply
                except ModelError as exc:
                    # Never persist arbitrary exception codes/provider text.
                    code = (
                        exc.code
                        if exc.code
                        in {
                            "MODEL_UNAVAILABLE",
                            "MODEL_REJECTED",
                            "RESPONSE_TOO_LARGE",
                            "BUDGET_EXHAUSTED",
                        }
                        else "MODEL_ERROR"
                    )
                    if code == "RESPONSE_TOO_LARGE":
                        hooks.reasons.append("RECORDING_INCOMPLETE")
                    hooks._end(seq, None, error=code)
                    raise
                finally:
                    hooks._model_parent = None

        return CapturedModel()

    def http(self, body: bytes, response: bytes | None, status: int | None) -> None:
        if self._model_parent is None:
            raise StorageError("IDENTITY_MISMATCH")
        if type(body) is not bytes or (
            response is not None and type(response) is not bytes
        ):
            invalid()
        if len(body) > MAX_REQUEST_BYTES or (
            response is not None and len(response) > MAX_RESPONSE_BYTES
        ):
            self.reasons.append("RECORDING_INCOMPLETE")
            raise StorageError("RECORDING_INCOMPLETE")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            invalid()
        seq = self._begin(
            "http",
            body,
            {
                "request_body_sha256": sha(body),
                "status": "unknown" if status is None else str(status),
            },
            parent=self._model_parent,
            media_type="application/octet-stream",
        )
        truncated = response is None and status is not None
        if truncated:
            self.reasons.append("HTTP_BODY_INCOMPLETE")
        self._end(
            seq,
            response,
            error="HTTP_BODY_INCOMPLETE"
            if truncated
            else ("MODEL_UNAVAILABLE" if response is None else None),
            media_type="application/octet-stream",
            limit=MAX_RESPONSE_BYTES,
        )

    def tools(self, executor: ToolFacade) -> ToolFacade:
        hooks = self

        class CapturedTools:
            def __getattr__(self, name: str) -> Any:
                return getattr(executor, name)

            def execute(self, request: ToolRequest) -> ToolOutcome:
                binding = binding_for(hooks.context, request)
                key = tool_key(hooks.context, request, binding)
                occurrence = hooks._counts[key]
                hooks._counts[key] += 1
                seq = hooks._begin(
                    "tool",
                    canonical_bytes(
                        {
                            "request": request,
                            "binding": binding,
                            "key": key,
                            "occurrence": occurrence,
                        }
                    ),
                    {},
                )
                hooks._operations[request.operation_id] = seq
                candidates = [
                    item
                    for item in hooks._current_calls.values()
                    if item["function"]["name"] == request.tool_name
                    and item["id"] not in hooks._claimed_calls
                ]
                if candidates:
                    candidate = candidates[0]
                    hooks._claimed_calls.add(candidate["id"])
                    hooks._tool_fingerprints[seq] = call_fingerprint(
                        candidate, binding.target
                    )
                try:
                    outcome = executor.execute(request)
                    hooks._operations[
                        str(
                            outcome.model_view.get("operation_id", request.operation_id)
                        )
                    ] = seq
                    raw = b"" if outcome.evidence is None else outcome.evidence.raw
                    if (
                        len(raw) > MAX_RAW_BYTES
                        or len(canonical_bytes(outcome.model_view)) > MAX_VIEW_BYTES
                    ):
                        hooks.reasons.append("RECORDING_INCOMPLETE")
                        raise StorageError("RECORDING_INCOMPLETE")
                    evidence_id = outcome.model_view.get("evidence_id")
                    if isinstance(evidence_id, str) and evidence_id:
                        hooks._evidence_events[evidence_id] = seq
                    view_hash = sha(canonical_bytes(outcome.model_view))
                    hooks._pending_views.setdefault(view_hash, []).append(seq)
                    hooks.events[seq] = replace(
                        hooks.events[seq],
                        hashes={
                            "view_sha256": sha(canonical_bytes(outcome.model_view)),
                            "raw_sha256": sha(raw),
                        },
                    )
                    hooks.events[seq] = replace(hooks.events[seq], clock=hooks._at)
                    hooks._end(seq, canonical_bytes(wire(outcome)))
                    return outcome
                except Exception:
                    hooks.reasons.append("RECORDING_INCOMPLETE")
                    raise

        return CapturedTools()

    def _refusal_request(
        self, view: Mapping[str, Any], message: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        evidence_id = view.get("duplicate_of_evidence_id")
        operation_id = view.get("operation_id")
        call_id = message.get("tool_call_id")
        parent = (
            self._evidence_events.get(evidence_id)
            if isinstance(evidence_id, str)
            else None
        )
        call = self._current_calls.get(call_id) if isinstance(call_id, str) else None
        if (
            parent is None
            or not isinstance(evidence_id, str)
            or call is None
            or call_id in self._claimed_calls
            or not isinstance(operation_id, str)
            or not operation_id
        ):
            raise StorageError("IDENTITY_MISMATCH")
        request = parse(
            _read(
                self.destination / self.events[parent].request_ref.path, MAX_BLOB_BYTES
            )
        )
        binding = decode(ToolBinding, request["binding"])
        if call_fingerprint(call, binding.target) != self._tool_fingerprints[
            parent
        ] or canonical_bytes(view) != canonical_bytes(
            _duplicate_call_view(operation_id, evidence_id=evidence_id)
        ):
            raise StorageError("IDENTITY_MISMATCH")
        return parent, {
            "operation_id": operation_id,
            "tool_call_id": call_id,
            "duplicate_of_evidence_id": evidence_id,
        }

    def delivery(self, view: Mapping[str, Any], message: Mapping[str, Any]) -> None:
        if (
            len(canonical_bytes(view)) > MAX_VIEW_BYTES
            or len(canonical_bytes(message)) > MAX_REQUEST_BYTES
        ):
            self.reasons.append("RECORDING_INCOMPLETE")
            raise StorageError("RECORDING_INCOMPLETE")
        if view.get("reason") == "DUPLICATE_TOOL_CALL":
            origin, request = self._refusal_request(view, message)
            self._claimed_calls.add(request["tool_call_id"])
            refusal = self._begin(
                "refusal",
                canonical_bytes(request),
                {"view_sha256": sha(canonical_bytes(view))},
                parent=origin,
            )
            self._end(refusal, canonical_bytes(view), limit=MAX_VIEW_BYTES)
            self._operations[request["operation_id"]] = refusal
            self._pending_views.setdefault(sha(canonical_bytes(view)), []).append(
                refusal
            )
        parent = self._operations.pop(str(view.get("operation_id")), None)
        candidates = self._pending_views.get(sha(canonical_bytes(view)), [])
        if parent in self._delivered_tools:
            parent = None
        if parent is None and candidates:
            parent = candidates[0]
        if parent is not None:
            self._delivered_tools.add(parent)
            if parent in candidates:
                candidates.remove(parent)
        seq = self._begin(
            "delivery",
            canonical_bytes(view),
            {
                "view_sha256": sha(canonical_bytes(view)),
                "delivery_sha256": sha(canonical_bytes(message)),
            },
            parent=parent,
        )
        self._end(seq, canonical_bytes(message))


def _save(
    hooks: CaptureHooks, outcome: IncidentOutcome | None, complete: bool
) -> ArtifactRef:
    payload = canonical_bytes(
        {
            "schema": SCHEMA,
            "context": hooks.context,
            "events": hooks.events,
            "outcome": outcome,
            "complete": complete,
            "incomplete_reasons": tuple(dict.fromkeys(hooks.reasons)),
        }
    )
    if len(payload) > MAX_METADATA_BYTES:
        raise StorageError("RECORDING_INCOMPLETE")
    return _publish(hooks.destination / "recording.json", payload, "application/json")


def _configuration_bytes(context: RecordingContext) -> bytes:
    """Detach the configuration baseline from nested mutable DTO mappings."""
    return canonical_bytes(
        {
            "tool_snapshots": context.tool_snapshots,
            "source_manifest": context.source_manifest,
            "source_manifest_hash": context.source_manifest_hash,
            "bindings": context.bindings,
            "versions": context.versions,
        }
    )


def record(
    driver: Callable[[CaptureHooks], IncidentOutcome],
    *,
    destination: Path,
    context: RecordingContext,
) -> ArtifactRef:
    context = _context(context)
    configuration = _configuration_bytes(context)
    _create(destination, context)
    hooks = CaptureHooks(
        destination,
        replace(context, source_versions=context.source_versions or context.versions),
    )
    outcome: IncidentOutcome | None = None
    failure: str | None = None
    try:
        outcome = validated(IncidentOutcome, driver(hooks))
        if context.synthetic and not hooks.context.input_refs:
            # A driver may supply original bytes before sealing. Generate the
            # fallback only after it has had that opportunity; this is metadata,
            # not another post-seal event.
            ref = hooks._put(
                canonical_bytes(hooks.context.stimulus), limit=MAX_REQUEST_BYTES
            )
            hooks.context = replace(hooks.context, input_refs=(ref,))
        if outcome.scenario_id != context.scenario_id:
            raise StorageError("IDENTITY_MISMATCH")
        if outcome.final_state in {
            "paused",
            "cancelled",
        } or outcome.human_interaction in {
            "pause",
            "cancel",
            "resume",
            "follow_up",
            "correct",
        }:
            raise StorageError("SCHEMA_INVALID")
        if hooks.reasons or (not context.synthetic and not hooks._sealed):
            raise StorageError("RECORDING_INCOMPLETE")
        provisional = Recording(
            destination / "recording.json",
            ArtifactRef("recording.json", "0" * 64, 0, "application/json", "private"),
            _context(hooks.context),
            tuple(hooks.events),
            outcome,
            True,
            (),
        )
        validate_recording(provisional)
    except Exception as exc:
        failure = (
            exc.code
            if isinstance(exc, StorageError) and exc.code in ERROR_CODES
            else "RECORDING_INCOMPLETE"
        )
        hooks.reasons.append(failure)
    try:
        configuration_changed = _configuration_bytes(hooks.context) != configuration
    except Exception:
        configuration_changed = True
    if configuration_changed:
        failure = "IDENTITY_MISMATCH"
        hooks.reasons.append(failure)
    try:
        ref = _save(hooks, outcome, failure is None)
    except StorageError:
        if configuration_changed:
            # An unserializable replacement still leaves INCOMPLETE in place.
            raise StorageError("IDENTITY_MISMATCH") from None
        raise
    if failure is not None:
        raise StorageError(failure) from None
    parent, name = _parent(destination / "INCOMPLETE")
    try:
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)
    return ref


def load_recording(path: Path, *, expected_hash: str) -> Recording:
    _hash(expected_hash)
    parent, _ = _parent(path)
    try:
        try:
            os.stat("INCOMPLETE", dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StorageError("RECORDING_INCOMPLETE")
    except OSError:
        raise StorageError("EVIDENCE_UNAVAILABLE") from None
    finally:
        os.close(parent)
    raw = _read(path, MAX_METADATA_BYTES)
    if sha(raw) != expected_hash:
        raise StorageError("HASH_MISMATCH")
    obj = exact(
        parse(raw),
        {"schema", "context", "events", "outcome", "complete", "incomplete_reasons"},
    )
    if obj["schema"] != SCHEMA:
        invalid()
    recording = Recording(
        path,
        ArtifactRef(path.name, sha(raw), len(raw), "application/json", "private"),
        _decode_context(obj["context"]),
        decode(tuple[RecordingEvent, ...], obj["events"]),
        decode(IncidentOutcome | None, obj["outcome"]),
        decode(bool, obj["complete"]),
        decode(tuple[str, ...], obj["incomplete_reasons"]),
    )
    if (
        os.path.lexists(path.parent / "INCOMPLETE")
        or not recording.complete
        or recording.incomplete_reasons
    ):
        raise StorageError("RECORDING_INCOMPLETE")
    validate_recording(recording)
    return recording


def _same_hashes(event: RecordingEvent, expected: Mapping[str, str]) -> None:
    if set(event.hashes) != set(expected):
        invalid()
    if dict(event.hashes) != expected:
        raise StorageError("HASH_MISMATCH")


def validate_recording(recording: Recording) -> None:
    """Recompute every byte/hash and pairing; never accept cached verdicts."""
    context = recording.context
    if not recording.events or recording.outcome is None:
        raise StorageError("RECORDING_INCOMPLETE")
    if recording.outcome.scenario_id != context.scenario_id:
        raise StorageError("IDENTITY_MISMATCH")
    if not context.input_refs or context.source_versions is None:
        raise StorageError("RECORDING_INCOMPLETE")
    for ref in context.input_refs:
        raw_input = blob(recording, ref)
        if len(raw_input) > MAX_REQUEST_BYTES or ref.media_type != "application/json":
            raise StorageError("RECORDING_INCOMPLETE")
        obj = parse(raw_input)
        _no_credentials(obj)
        _no_private(obj)
    previous = context.started_at
    counts: Counter[str] = Counter()
    http_counts: Counter[int] = Counter()
    model_bodies: dict[int, str] = {}
    pending: dict[str, Mapping[str, Any]] = {}
    generated: dict[str, str] = {}
    tool_views: dict[int, str] = {}
    event_views: dict[int, str] = {}
    tools_waiting: list[int] = []
    tool_calls: dict[int, str] = {}
    tool_fingerprints: dict[int, str] = {}
    evidence_events: dict[str, int] = {}
    operation_ids: set[str] = set()
    saw_model = False
    for seq, event in enumerate(recording.events):
        if (
            event.seq != seq
            or event.identity != context.source_run_id
            or not previous <= event.clock <= context.ended_at
        ):
            raise StorageError("IDENTITY_MISMATCH")
        previous = event.clock
        if event.parent is not None and not 0 <= event.parent < seq:
            raise StorageError("IDENTITY_MISMATCH")
        expected_media = (
            "application/octet-stream" if event.kind == "http" else "application/json"
        )
        if event.request_ref.media_type != expected_media or (
            event.response_ref is not None
            and event.response_ref.media_type != expected_media
        ):
            invalid()
        request_raw = blob(recording, event.request_ref)
        response_raw = (
            None if event.response_ref is None else blob(recording, event.response_ref)
        )
        if event.kind == "model":
            if event.parent is not None:
                raise StorageError("IDENTITY_MISMATCH")
            saw_model = True
            obj = exact(parse(request_raw), {"call", "body"})
            call = decode(ModelCall, obj["call"])
            if type(obj["body"]) is not str:
                invalid()
            body = obj["body"].encode("utf-8")
            body_obj = parse(body)
            _no_credentials(body_obj)
            # Canonical DTO metadata sorts message keys; the embedded body
            # retains their original insertion order. Rebuild using that order
            # and compare all logical values before the exact byte assertion.
            messages = body_obj.get("messages")
            tools = body_obj.get("tools")
            if canonical_bytes(messages) != canonical_bytes(
                call.messages
            ) or canonical_bytes(tools) != canonical_bytes(call.tools):
                raise StorageError("HASH_MISMATCH")
            if not isinstance(messages, list):
                invalid()
            exact_call = replace(
                call,
                messages=tuple(messages),
                tools=None if tools is None else tuple(tools),
            )
            if serialized_request(exact_call) != body:
                raise StorageError("HASH_MISMATCH")
            if len(body) > MAX_REQUEST_BYTES or (
                response_raw is not None and len(response_raw) > MAX_RESPONSE_BYTES
            ):
                raise StorageError("RECORDING_INCOMPLETE")
            _same_hashes(
                event,
                {
                    "request_body_sha256": sha(body),
                    "messages_sha256": sha(canonical_bytes(call.messages)),
                },
            )
            model_bodies[seq] = sha(body)
            # A generated message counts as delivered only when its exact
            # bytes occur in an actual subsequent physical model request.
            for message in call.messages:
                if message.get("role") == "tool":
                    call_id = message.get("tool_call_id")
                    if (
                        call_id not in generated
                        or sha(canonical_bytes(message)) != generated[call_id]
                    ):
                        raise StorageError("IDENTITY_MISMATCH")
            if pending:
                raise StorageError("RECORDING_INCOMPLETE")
            if response_raw is not None:
                if event.error is not None:
                    invalid()
                reply = decode(ModelReply, parse(response_raw))
                if set(reply.raw) - {"id", "usage"}:
                    invalid()
                try:
                    calls = validate_tool_calls({"tool_calls": list(reply.tool_calls)})
                except PairingError:
                    raise StorageError("SCHEMA_INVALID") from None
                pending = {item["id"]: item for item in calls}
            elif event.error == "RESPONSE_TOO_LARGE":
                raise StorageError("RECORDING_INCOMPLETE")
            elif event.error not in {
                "MODEL_UNAVAILABLE",
                "MODEL_REJECTED",
                "RESPONSE_TOO_LARGE",
                "BUDGET_EXHAUSTED",
                "MODEL_ERROR",
            }:
                raise StorageError("RECORDING_INCOMPLETE")
        elif event.kind == "http":
            if event.parent is None or event.parent not in model_bodies:
                raise StorageError("IDENTITY_MISMATCH")
            http_counts[event.parent] += 1
            if sha(request_raw) != model_bodies[event.parent]:
                raise StorageError("HASH_MISMATCH")
            status = event.hashes.get("status", "")
            if status != "unknown" and not re.fullmatch(r"[1-5][0-9]{2}", status):
                invalid()
            _same_hashes(
                event, {"request_body_sha256": sha(request_raw), "status": status}
            )
            if response_raw is not None and len(response_raw) > MAX_RESPONSE_BYTES:
                raise StorageError("RECORDING_INCOMPLETE")
            if event.error == "HTTP_BODY_INCOMPLETE" or (
                response_raw is None and status != "unknown"
            ):
                raise StorageError("RECORDING_INCOMPLETE")
            if response_raw is None and event.error != "MODEL_UNAVAILABLE":
                raise StorageError("RECORDING_INCOMPLETE")
            if response_raw is not None and event.error is not None:
                invalid()
            parent_event = recording.events[event.parent]
            if parent_event.response_ref is not None:
                if (
                    response_raw is None
                    or status == "unknown"
                    or not 200 <= int(status) < 300
                ):
                    raise StorageError("RECORDING_INCOMPLETE")
                # Parse only to cross-check the adapter's normalized boundary;
                # never rewrite or export the original private HTTP response.
                from opspilot.investigation.client import _parse_reply

                try:
                    actual_reply = _parse_reply(parse(response_raw))
                except ModelError:
                    raise StorageError("SCHEMA_INVALID") from None
                if canonical_bytes(actual_reply) != blob(
                    recording, parent_event.response_ref
                ):
                    raise StorageError("HASH_MISMATCH")
        elif event.kind == "tool":
            if (
                event.parent is not None
                or response_raw is None
                or event.error is not None
            ):
                raise StorageError("RECORDING_INCOMPLETE")
            obj = exact(parse(request_raw), {"request", "binding", "key", "occurrence"})
            request = unwire(ToolRequest, obj["request"])
            binding = decode(ToolBinding, obj["binding"])
            if binding != binding_for(context, request):
                raise StorageError("IDENTITY_MISMATCH")
            key = tool_key(context, request, binding)
            if obj["key"] != key:
                raise StorageError("HASH_MISMATCH")
            if type(obj["occurrence"]) is not int or obj["occurrence"] != counts[key]:
                raise StorageError("IDENTITY_MISMATCH")
            counts[key] += 1
            payload = parse(response_raw)
            restricted = payload.get("evidence") is None or (
                isinstance(payload.get("operation"), dict)
                and payload["operation"].get("dispatched") is False
            )
            try:
                outcome = unwire(ToolOutcome, payload)
            except (StorageError, ToolContractError):
                if restricted:
                    raise StorageError("IDENTITY_MISMATCH") from None
                raise
            _tool_integrity(context, request, binding, outcome, event.clock)
            raw = b"" if outcome.evidence is None else outcome.evidence.raw
            view_raw = canonical_bytes(outcome.model_view)
            if len(raw) > MAX_RAW_BYTES or len(view_raw) > MAX_VIEW_BYTES:
                raise StorageError("RECORDING_INCOMPLETE")
            _same_hashes(event, {"view_sha256": sha(view_raw), "raw_sha256": sha(raw)})
            # A facade dispatch must have been requested by this model, not
            # smuggled into a fixture as unrelated evidence.
            matching = [
                item
                for item in pending.values()
                if item["function"]["name"] == request.tool_name
            ]
            if not matching:
                raise StorageError("IDENTITY_MISMATCH")
            available = [
                item for item in matching if item["id"] not in tool_calls.values()
            ]
            if not available:
                raise StorageError("IDENTITY_MISMATCH")
            tool_calls[seq] = available[0]["id"]
            tool_fingerprints[seq] = call_fingerprint(available[0], binding.target)
            operation_ids.add(request.operation_id)
            operation_ids.add(outcome.operation.operation_id)
            evidence_id = outcome.model_view.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id:
                evidence_events[evidence_id] = seq
            tool_views[seq] = sha(view_raw)
            event_views[seq] = sha(view_raw)
            tools_waiting.append(seq)
        elif event.kind == "refusal":
            if (
                response_raw is None
                or event.error is not None
                or len(response_raw) > MAX_VIEW_BYTES
            ):
                raise StorageError("RECORDING_INCOMPLETE")
            obj = exact(
                parse(request_raw),
                {"operation_id", "tool_call_id", "duplicate_of_evidence_id"},
            )
            obj = decode(dict[str, str], obj)
            if not all(obj.values()):
                raise StorageError("IDENTITY_MISMATCH")
            view = parse(response_raw)
            _same_hashes(event, {"view_sha256": sha(canonical_bytes(view))})
            origin = evidence_events.get(obj["duplicate_of_evidence_id"])
            call = pending.get(obj["tool_call_id"])
            if (
                event.parent is None
                or event.parent != origin
                or call is None
                or obj["operation_id"] in operation_ids
            ):
                raise StorageError("IDENTITY_MISMATCH")
            request = json_blob(recording, recording.events[event.parent].request_ref)
            binding = decode(ToolBinding, request["binding"])
            if call_fingerprint(call, binding.target) != tool_fingerprints[
                event.parent
            ] or canonical_bytes(view) != canonical_bytes(
                _duplicate_call_view(
                    obj["operation_id"], evidence_id=obj["duplicate_of_evidence_id"]
                )
            ):
                raise StorageError("IDENTITY_MISMATCH")
            operation_ids.add(obj["operation_id"])
            tool_calls[seq] = obj["tool_call_id"]
            event_views[seq] = sha(canonical_bytes(view))
            tools_waiting.append(seq)
        else:
            if response_raw is None or event.error is not None:
                raise StorageError("RECORDING_INCOMPLETE")
            if (
                len(request_raw) > MAX_VIEW_BYTES
                or len(response_raw) > MAX_REQUEST_BYTES
            ):
                raise StorageError("RECORDING_INCOMPLETE")
            view = parse(request_raw)
            message = exact(parse(response_raw), {"role", "tool_call_id", "content"})
            if (
                message["role"] != "tool"
                or type(message["tool_call_id"]) is not str
                or type(message["content"]) is not str
            ):
                invalid()
            _same_hashes(
                event,
                {
                    "view_sha256": sha(canonical_bytes(view)),
                    "delivery_sha256": sha(canonical_bytes(message)),
                },
            )
            call_id = message["tool_call_id"]
            if call_id not in pending:
                raise StorageError("IDENTITY_MISMATCH")
            if view.get("reason") == "DUPLICATE_TOOL_CALL" and (
                event.parent is None or recording.events[event.parent].kind != "refusal"
            ):
                raise StorageError("IDENTITY_MISMATCH")
            if event.parent is not None:
                if (
                    event.parent not in tools_waiting
                    or sha(request_raw) != event_views[event.parent]
                    or tool_calls[event.parent] != call_id
                ):
                    raise StorageError("IDENTITY_MISMATCH")
                tools_waiting.remove(event.parent)
            else:
                known_views = list(tool_views.values()) + [
                    sha(canonical_bytes(view))
                    for view in context.initial_state.get("evidence_views", [])
                ]
                if sha(request_raw) not in known_views:
                    raise StorageError("IDENTITY_MISMATCH")
            pending.pop(call_id)
            generated[call_id] = sha(canonical_bytes(message))
    if not saw_model or tools_waiting or pending:
        raise StorageError("RECORDING_INCOMPLETE")
    for seq in model_bodies:
        if http_counts[seq] > 1 or (not context.synthetic and http_counts[seq] != 1):
            raise StorageError("RECORDING_INCOMPLETE")


def call_fingerprint(call: Mapping[str, Any], target: str) -> str:
    function = call["function"]
    return sha(
        _call_fingerprint(function["name"], function["arguments"], target).encode(
            "utf-8"
        )
    )


def _tool_integrity(
    context: RecordingContext,
    request: ToolRequest,
    binding: ToolBinding,
    outcome: ToolOutcome,
    event_clock: datetime,
) -> None:
    operation = outcome.operation
    source = context.initial_state.get("replay_source")
    if source is None:
        expected_run, expected_operation = context.source_run_id, request.operation_id
    else:
        source = exact(source, {"run_id", "recording_hash", "id_map"})
        _hash(decode(str, source["recording_hash"]))
        expected_run = decode(str, source["run_id"])
        mappings = decode(dict[str, str], source["id_map"])
        expected_operation = mappings.get(request.operation_id)
    if operation.run_id != expected_run or operation.operation_id != expected_operation:
        raise StorageError("IDENTITY_MISMATCH")
    unqueried = not operation.dispatched
    if operation.tool is not None and (
        operation.tool != binding.tool
        or operation.tool_version != binding.version
        or operation.source != binding.source
    ):
        raise StorageError("IDENTITY_MISMATCH")
    if (
        operation.target_id is not None or not unqueried
    ) and operation.target_id != binding.target:
        raise StorageError("IDENTITY_MISMATCH")
    if canonical_bytes(operation.requested_tool) != canonical_bytes(
        request.tool_name
    ) or canonical_bytes(operation.requested_target) != canonical_bytes(
        request.target_ref
    ):
        raise StorageError("IDENTITY_MISMATCH")
    if (
        "operation_id" in outcome.model_view
        and outcome.model_view["operation_id"] != operation.operation_id
    ):
        raise StorageError("IDENTITY_MISMATCH")
    auth = _window(binding.authorized_window)
    evidence = outcome.evidence
    if unqueried or evidence is None:
        check_result(context, request, binding, outcome, event_clock)
    if operation.window is not None and not auth.contains(operation.window):
        raise StorageError("IDENTITY_MISMATCH")
    if not unqueried:
        try:
            value = (
                request.window
                if request.window is not None
                else (binding.default_window or binding.authorized_window)
            )
            if not isinstance(value, Mapping):
                raise StorageError("IDENTITY_MISMATCH")
            expected_window = _window(value)
        except (StorageError, TypeError):
            raise StorageError("IDENTITY_MISMATCH") from None
        if operation.window != expected_window or not auth.contains(expected_window):
            raise StorageError("IDENTITY_MISMATCH")
    if evidence is not None:
        if evidence.operation != operation:
            raise StorageError("IDENTITY_MISMATCH")
        if evidence.raw_sha256 != sha(evidence.raw) or evidence.view_sha256 != sha(
            canonical_bytes(evidence.view)
        ):
            raise StorageError("HASH_MISMATCH")
        if canonical_bytes(evidence.view) != canonical_bytes(outcome.model_view):
            raise StorageError("HASH_MISMATCH")
        if (
            "evidence_id" in outcome.model_view
            and outcome.model_view["evidence_id"] != evidence.evidence_id
        ):
            raise StorageError("IDENTITY_MISMATCH")


def _decode_context(value: Any) -> RecordingContext:
    # New optional provenance fields do not invalidate old complete evidence
    # packets. Restricted branches still require the actual snapshots below.
    if isinstance(value, dict):
        value = {"tool_snapshots": [], "source_manifest": None, **value}
    try:
        return _context(decode(RecordingContext, value))
    except ToolContractError:
        raise StorageError("IDENTITY_MISMATCH") from None
