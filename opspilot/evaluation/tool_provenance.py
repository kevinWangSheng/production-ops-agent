"""Frozen request/registration provenance and bounded gateway audit facts.

This evaluation-only module performs no IO and never obtains credentials.
Snapshots are supplied before recording, independently of tool responses.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, NoReturn

from opspilot.evaluation.codec import (
    StorageError,
    canonical_bytes,
    encode,
    parse,
    validated,
)
from opspilot.evaluation.rounds import (
    DimensionValue,
    Manifest,
    ManifestError,
    manifest_hash,
    validate_manifest,
)
from opspilot.tools.executor import (
    QueryScope,
    ToolRequest,
    TransportRequest,
    _accept_params,
    _result_size_message,
    _status_for,
    _view_refusal_detail,
)
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TRACES_TOOL,
    _query_window,
)
from opspilot.tools.otel_demo import (
    SOURCE as OTEL_SOURCE,
)
from opspilot.tools.outcomes import DENIED_REASONS, ToolOutcome, Window
from opspilot.tools.registry import (
    RegisteredTarget,
    TargetRegistry,
    ToolContractError,
    ToolRegistration,
    ToolRegistry,
    canonical,
    canonical_hash,
)

if TYPE_CHECKING:
    from opspilot.evaluation.recording import RecordingContext, ToolBinding


def mismatch() -> NoReturn:
    raise StorageError("IDENTITY_MISMATCH")


def same(actual: Any, expected: Any) -> None:
    if canonical_bytes(actual) != canonical_bytes(expected):
        mismatch()


@dataclass(frozen=True)
class ToolSnapshot:
    tools: tuple[ToolRegistration, ...]
    targets: tuple[RegisteredTarget, ...]
    scope: QueryScope


def _registries(snapshot: ToolSnapshot) -> tuple[ToolRegistry, TargetRegistry]:
    try:
        return ToolRegistry(snapshot.tools), TargetRegistry(snapshot.targets)
    except ToolContractError:
        return mismatch()


def snapshot_hashes(snapshots: tuple[ToolSnapshot, ...]) -> tuple[str, str]:
    """Canonical whole-registry and whole-authorization manifest dimensions."""
    ordered = sorted(snapshots, key=lambda item: item.scope.scope_id)
    if len({item.scope.scope_id for item in ordered}) != len(ordered):
        mismatch()
    registries = []
    for item in ordered:
        tools, targets = _registries(item)
        registrations = sorted(item.tools, key=lambda reg: reg.name)
        registered_targets = sorted(item.targets, key=lambda target: target.target_id)
        registries.append(
            {
                "scope_id": item.scope.scope_id,
                "tools": registrations,
                "targets": registered_targets,
                "tool_revision": tools.revision,
                "target_revision": targets.revision,
            }
        )
    return (
        canonical_hash(encode(registries)),
        canonical_hash(encode([item.scope for item in ordered])),
    )


def snapshot_for(context: RecordingContext, binding: ToolBinding) -> ToolSnapshot:
    candidates = [
        item
        for item in context.tool_snapshots
        if any(reg.name == binding.tool for reg in item.tools)
        and any(target.target_id == binding.target for target in item.targets)
        and canonical_bytes(item.scope.window)
        == canonical_bytes(binding.authorized_window)
    ]
    if len(candidates) != 1:
        return mismatch()
    return candidates[0]


def bind_tool_snapshots(
    context: RecordingContext,
    snapshots: tuple[ToolSnapshot, ...],
    manifest: Manifest,
) -> RecordingContext:
    """Build the frozen source envelope before any model/tool call occurs."""
    try:
        snapshots = validated(tuple[ToolSnapshot, ...], snapshots)
    except (StorageError, ToolContractError):
        return mismatch()
    if not snapshots:
        return mismatch()
    registry, permissions = snapshot_hashes(snapshots)
    dimensions = {
        **manifest.dimensions,
        "registry": DimensionValue("hash", registry, None),
        "permissions": DimensionValue("hash", permissions, None),
    }
    manifest = replace(manifest, dimensions=dimensions)
    context = replace(
        context,
        tool_snapshots=snapshots,
        source_manifest=manifest,
        source_manifest_hash=manifest_hash(manifest),
        versions={**context.versions, "registry": registry, "permissions": permissions},
    )
    bindings = tuple(
        replace(
            binding,
            registry_hash=registry,
            permissions_hash=canonical_hash(
                encode(snapshot_for(context, binding).scope)
            ),
        )
        for binding in context.bindings
    )
    context = replace(context, bindings=bindings)
    validate_snapshot_context(context)
    return context


def validate_snapshot_context(context: RecordingContext) -> None:
    if not context.tool_snapshots:
        if context.source_manifest is not None:
            mismatch()
        return
    manifest = context.source_manifest
    if manifest is None:
        mismatch()
    assert manifest is not None
    try:
        validate_manifest(manifest)
    except ManifestError:
        mismatch()
    if manifest_hash(manifest) != context.source_manifest_hash:
        mismatch()
    registry, permissions = snapshot_hashes(context.tool_snapshots)
    for name, value in (("registry", registry), ("permissions", permissions)):
        same(manifest.dimensions.get(name), DimensionValue("hash", value, None))
        same(context.versions.get(name), value)
    replay_source = context.initial_state.get("replay_source", {})
    if not isinstance(replay_source, Mapping):
        mismatch()
    run = replay_source.get("run_id", context.source_run_id)
    for item in context.tool_snapshots:
        same(item.scope.run_id, run)
    for binding in context.bindings:
        item = snapshot_for(context, binding)
        tools, targets = _registries(item)
        reg, target = tools.lookup(binding.tool), targets.resolve(binding.target)
        if reg is None or target is None:
            mismatch()
        assert reg is not None and target is not None
        same((binding.version, binding.source), (reg.version, reg.source))
        same(binding.default_window, item.scope.default_query_window)
        same(binding.registry_hash, registry)
        same(binding.permissions_hash, canonical_hash(encode(item.scope)))


# These decisions can arise both before dispatch and while a read is in flight.
_CONTROL = {
    "CONTROL_GENERATION_CHANGED",
    "CONTROL_UNAVAILABLE",
    "DEADLINE_EXCEEDED",
    "OPERATION_BUDGET_EXHAUSTED",
    "SUSPENDED",
    "TIME_BUDGET_EXHAUSTED",
}


def check_result(
    context: RecordingContext,
    request: ToolRequest,
    binding: ToolBinding,
    outcome: ToolOutcome,
    event_clock: datetime,
) -> None:
    """Bind every result to frozen identity; bound evidenceless audit facts."""
    item = snapshot_for(context, binding)
    tools, targets = _registries(item)
    scope = item.scope
    op = outcome.operation
    same(op.run_id, scope.run_id)
    same(
        (op.scope_id, op.subject_kind, op.subject_id),
        (scope.scope_id, scope.subject_kind, scope.subject_id),
    )
    same(
        (op.registry_revision, op.tool_registry_revision),
        (targets.revision, tools.revision),
    )
    reg = tools.lookup(request.tool_name)
    target = targets.resolve(request.target_ref)
    if reg is None or target is None:
        mismatch()  # Unknown bindings remain outside the representable format.
    assert reg is not None and target is not None
    resolved: dict[str, Any] = {
        "tool": None,
        "tool_version": None,
        "source": None,
        "target_id": None,
        "query": None,
    }
    failure: tuple[str, str] | None = None
    if targets.revision != scope.registry_revision:
        failure = ("denied", "TARGET_REGISTRY_CHANGED")
    elif tools.revision != scope.tool_registry_revision:
        failure = ("denied", "TOOL_REGISTRY_CHANGED")
    elif reg.name not in scope.tool_names:
        failure = ("denied", "TOOL_NOT_IN_SCOPE")
    else:
        resolved.update(tool=reg.name, tool_version=reg.version, source=reg.source)
        if target.target_id not in scope.target_ids:
            failure = ("denied", "TARGET_NOT_AUTHORIZED")
        elif target.source != reg.source:
            failure = ("denied", "TARGET_SOURCE_MISMATCH")
        else:
            resolved["target_id"] = target.target_id
            value = request.window
            if op.dispatched and value is None:
                value = binding.default_window or binding.authorized_window
            window = Window.parse(value)
            if window is None:
                failure = ("error", "INVALID_PARAMS")
            elif not scope.window.contains(window):
                failure = ("denied", "WINDOW_OUT_OF_SCOPE")
            elif window.seconds > reg.max_window_seconds:
                failure = ("denied", "WINDOW_TOO_LARGE")
            else:
                params, problem = _accept_params(reg, request.params)
                if params is None:
                    failure = (_status_for(problem), problem)
                else:
                    try:
                        resolved["query"] = canonical(params)
                    except (ValueError, OverflowError):
                        failure = ("error", "INVALID_PARAMS")
    for name, expected in resolved.items():
        same(getattr(op, name), expected)
    if outcome.evidence is not None:
        if not op.dispatched or failure is not None:
            mismatch()
        same(op.credential_ref, target.credential_ref)
        same(outcome.evidence.operation, op)
        identities = {
            "operation_id": op.operation_id,
            "run_id": scope.run_id,
            "scope_id": scope.scope_id,
            "subject_kind": scope.subject_kind,
            "subject_id": scope.subject_id,
            "registry_revision": targets.revision,
            "tool_registry_revision": tools.revision,
            "requested_tool": request.tool_name,
            "requested_target": request.target_ref,
            "tool": reg.name,
            "tool_version": reg.version,
            "source": reg.source,
            "target_id": target.target_id,
            "credential_ref": target.credential_ref,
            "query": parse(resolved["query"].encode()),
        }
        # Evidence keeps source payload, but its top-level identity projections
        # cannot disagree with the same frozen headers merely by rehashing it.
        for view in (outcome.model_view, outcome.evidence.view):
            for name in set(view) & identities.keys():
                same(view[name], identities[name])
        return
    if op.dispatched:
        if failure is not None:
            mismatch()
        expected_window = Window.parse(
            request.window
            if request.window is not None
            else (binding.default_window or binding.authorized_window)
        )
        same(op.window, expected_window)
        allowed = {
            ("no_data", "NO_DATA", "confirmed"),
            ("error", "INVALID_PARAMS", "confirmed"),
            ("error", "SOURCE_ERROR", "possible"),
            ("error", "SOURCE_ERROR", "confirmed"),
            ("error", "SOURCE_UNAVAILABLE", "possible"),
            ("error", "SOURCE_UNAVAILABLE", "confirmed"),
            ("error", "MALFORMED_RESULT", "confirmed"),
            ("error", "EVIDENCE_NOT_COMMITTED", "confirmed"),
            ("error", "RESULT_TOO_LARGE", "confirmed"),
            ("timeout", "TOOL_TIMEOUT", "possible"),
            ("timeout", "GATEWAY_TIMEOUT", "confirmed"),
            ("denied", "WINDOW_OUT_OF_SCOPE", "confirmed"),
            ("denied", "DEADLINE_EXCEEDED", "confirmed"),
        } | {
            ("denied", reason, contact)
            for reason in _CONTROL - {"DEADLINE_EXCEEDED"}
            for contact in ("possible", "confirmed")
        }
        if (outcome.status, outcome.reason, outcome.source_contact) not in allowed:
            mismatch()
        same(op.credential_ref, target.credential_ref)
        if any(
            value is None
            for value in (
                op.started_at,
                op.authorized_at,
                op.finished_at,
                op.elapsed_seconds,
                op.timeout_seconds,
            )
        ):
            mismatch()
    else:
        same(op.window, None)
        if outcome.source_contact != "none" or outcome.status not in (
            "denied",
            "error",
        ):
            mismatch()
        if failure is not None:
            same((outcome.status, outcome.reason), failure)
        elif not (
            (outcome.status == "denied" and outcome.reason in _CONTROL)
            or (
                outcome.status == "error"
                and outcome.reason
                in {"INVALID_PARAMS", "SOURCE_ERROR", "SOURCE_UNAVAILABLE"}
            )
        ):
            mismatch()
        same(
            op.credential_ref,
            target.credential_ref if op.timeout_seconds is not None else None,
        )
        if failure is not None and any(
            value is not None
            for value in (
                op.authorized_at,
                op.finished_at,
                op.timeout_seconds,
                op.elapsed_seconds,
            )
        ):
            mismatch()
        if failure is None:
            transport_phase = (
                outcome.status == "error"
                or op.finished_at is not None
                or op.elapsed_seconds is not None
            )
            if transport_phase:
                if any(
                    value is None
                    for value in (
                        op.authorized_at,
                        op.finished_at,
                        op.elapsed_seconds,
                        op.timeout_seconds,
                    )
                ):
                    mismatch()
            elif (
                op.authorized_at is not None
                and op.timeout_seconds is None
                and outcome.reason != "DEADLINE_EXCEEDED"
            ):
                mismatch()
    if outcome.status == "denied" and outcome.reason not in DENIED_REASONS:
        mismatch()
    if outcome.reason == "DEADLINE_EXCEEDED":
        if event_clock < scope.deadline or op.authorized_at is None:
            mismatch()
        if op.dispatched:
            if op.finished_at is None or op.finished_at < scope.deadline:
                mismatch()
            if (
                op.elapsed_seconds is None
                or op.timeout_seconds is None
                or op.elapsed_seconds > op.timeout_seconds
            ):
                mismatch()
        elif op.finished_at is not None:
            mismatch()
        elif op.timeout_seconds is None and op.authorized_at < scope.deadline:
            mismatch()
    if outcome.reason == "GATEWAY_TIMEOUT" and not (
        op.elapsed_seconds is not None
        and op.timeout_seconds is not None
        and op.elapsed_seconds > op.timeout_seconds
    ):
        mismatch()
    times = [op.started_at, op.authorized_at, op.finished_at]
    if op.started_at is None:
        mismatch()
    previous = context.started_at
    for at in times:
        if at is not None:
            if not previous <= at <= event_clock <= context.ended_at:
                mismatch()
            previous = at
    for value in (op.elapsed_seconds, op.timeout_seconds):
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or value < 0
        ):
            mismatch()
    if op.timeout_seconds is not None:
        if (
            op.timeout_seconds > reg.request_timeout_seconds
            or op.authorized_at is None
            or op.timeout_seconds
            > max(0.0, (scope.deadline - op.authorized_at).total_seconds())
        ):
            mismatch()
    if op.dispatched or (not op.dispatched and op.finished_at is not None):
        if (
            op.authorized_at is None
            or op.authorized_at >= scope.deadline
            or op.timeout_seconds is None
            or op.timeout_seconds <= 0
        ):
            mismatch()
    _check_view(outcome, reg, target, scope)


def _check_view(
    outcome: ToolOutcome,
    reg: ToolRegistration,
    target: RegisteredTarget,
    scope: QueryScope,
) -> None:
    op, view = outcome.operation, outcome.model_view
    expected = {
        "operation_id": op.operation_id,
        "trust": "gateway",
        "status": outcome.status,
        "citable_as_fact": False,
        "adopted": False,
        "reason": outcome.reason,
        "source_contact": outcome.source_contact,
        "tool": op.tool,
        "target_id": op.target_id,
        "window": None if op.window is None else op.window.as_json(),
        "requested_at": None if op.started_at is None else op.started_at.isoformat(),
        "content": None,
    }
    hints = set(view) & {
        "message",
        "query",
        "max_result_bytes",
        "max_view_tokens",
        "view_tokens",
    }
    if hints:
        if not op.dispatched or outcome.reason != "RESULT_TOO_LARGE":
            mismatch()
        if hints == {"message", "max_result_bytes"}:
            expected.update(
                max_result_bytes=reg.max_result_bytes,
                message=_result_size_message(reg.max_result_bytes),
            )
        elif (
            hints == {"message", "query", "max_view_tokens", "view_tokens"}
            and "window" in view
        ):
            tokens = view["view_tokens"]
            if type(tokens) is not int or tokens <= reg.max_view_tokens:
                mismatch()
            query = parse((op.query or "null").encode())
            window = op.window
            if not isinstance(query, dict) or op.window is None:
                mismatch()
            assert window is not None
            if (
                reg.source == OTEL_SOURCE
                and reg.version == "v1"
                and reg.name in (METRICS_TOOL, TRACES_TOOL)
            ):
                # Replay the same profile selection from frozen, checked
                # parameters. Never use the response's claimed query_window.
                default = scope.default_query_window
                window, refusal = _query_window(
                    TransportRequest(
                        operation_id=op.operation_id,
                        source=reg.source,
                        verb=reg.verb,
                        endpoint=target.endpoint,
                        selector=target.selector,
                        params=query,
                        window=window,
                        timeout_seconds=reg.request_timeout_seconds,
                        max_result_bytes=reg.max_result_bytes,
                        credential_ref=target.credential_ref,
                        tool=reg.name,
                        default_window=default
                        if default is not None and window.contains(default)
                        else None,
                    )
                )
                if refusal is not None or window is None:
                    mismatch()
            if not op.window.contains(window) or not scope.window.contains(window):
                mismatch()
            expected.update(
                _view_refusal_detail(
                    tokens, reg.max_view_tokens, query, window.as_json()
                )
            )
        else:
            mismatch()
    if set(view) - set(expected):
        mismatch()
    for name, value in view.items():
        same(value, expected[name])
