"""Frozen request/registration provenance and bounded gateway audit facts.

This evaluation-only module performs no IO and never obtains credentials.
Snapshots are supplied before recording, independently of tool responses.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, NoReturn, cast

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
from opspilot.tools.evidence_read import EvidenceReadTransport
from opspilot.tools.executor import (
    _GENERIC_VIEW_KEYS,
    _SPILLED_PROVENANCE_KEYS,
    _SPILLED_USAGE,
    _UNSTORED_USAGE,
    EVIDENCE_SOURCE,
    MAX_SOURCE_ERROR_CHARS,
    QueryScope,
    ToolRequest,
    TransportRequest,
    _accept_params,
    _result_size_message,
    _source_error_type_ok,
    _status_for,
    _view_refusal_detail,
    reads_evidence,
)
from opspilot.tools.otel_demo import (
    MAX_SUMMARY_LABEL_CHARS,
    MAX_SUMMARY_LABELS,
    METRICS_TOOL,
    TOOL_NAMES,
    OtelDemoTransport,
    _query_window,
    topk_suggestion,
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


class _WouldSend(BaseException):
    """Raised where the shipped transport would leave the process."""


class _Offline:
    """An opener / evidence store that refuses to be used."""

    def open(self, *args: object, **kwargs: object) -> NoReturn:
        raise _WouldSend

    def get(self, evidence_id: str) -> NoReturn:
        raise _WouldSend


# The transport's verdict on an evidence read whose parameters are well
# formed: it then asks the evidence store, which a recording does not carry,
# so EVIDENCE_NOT_READABLE (``evidence_read.py`` :284-312) can be neither
# proved nor ruled out offline.
_STORE_DEPENDENT = "EVIDENCE_NOT_READABLE?"

# A pre-send ``elapsed_seconds`` is monotonic time measured between
# ``authorized_at`` and ``finished_at`` (``executor.py`` :1043-1080); this much
# wall/monotonic disagreement is tolerated.
_CLOCK_SLACK_SECONDS = 1.0


def _transport_refusal(
    reg: ToolRegistration,
    target: RegisteredTarget,
    scope: QueryScope,
    op: Any,
    window: Window,
    params: Mapping[str, object],
) -> str | None:
    """The code the shipped transport refuses this exact request with before
    sending (#425 bot P1), ``None`` when it would send it, or
    ``_STORE_DEPENDENT``. Runs the transport itself, offline: anything that
    would reach the network or the evidence store stops at ``_Offline``.
    """
    default = scope.default_query_window
    request = TransportRequest(
        operation_id=op.operation_id,
        source=reg.source,
        verb=reg.verb,
        endpoint=target.endpoint,
        selector=target.selector,
        params=dict(params),
        window=window,
        timeout_seconds=op.timeout_seconds or reg.request_timeout_seconds,
        max_result_bytes=reg.max_result_bytes,
        credential_ref=target.credential_ref,
        tool=reg.name,
        target_id=target.target_id,
        default_window=default
        if default is not None and window.contains(default)
        else None,
    )
    offline = _Offline()
    transport: Any
    if reads_evidence(reg):
        transport = EvidenceReadTransport(
            store=cast(Any, offline),
            run_id=scope.run_id,
            target_ids=scope.target_ids,
            result_paths={},
        )
    elif reg.source == OTEL_SOURCE and reg.name in TOOL_NAMES:
        transport = OtelDemoTransport(
            credentials={target.credential_ref: None}, opener=cast(Any, offline)
        )
    else:
        return None  # No other shipped transport answers sent=false.
    try:
        response = transport.fetch(request)
    except _WouldSend:
        return _STORE_DEPENDENT if reads_evidence(reg) else None
    except Exception:
        return None
    if getattr(response, "sent", True) is not False:
        return None
    status = getattr(response, "source_status", None)
    return status if isinstance(status, str) else None


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
    # A transport's own pre-send refusal (``sent=false``, #283/#327): handed
    # to the transport, which answered without sending.
    presend = not op.dispatched and op.finished_at is not None
    # The executor sets the operation's window once the window checks pass,
    # before parameter acceptance, so every later refusal keeps it and every
    # earlier one has none (#327); ``planned`` mirrors that point.
    planned: Window | None = None
    accepted: Mapping[str, object] | None = None
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
        elif target.source != reg.source and not reads_evidence(reg):
            # ``read_evidence`` (#308 R3) reads the gateway's own store for
            # any registered target; the executor waives the same check.
            failure = ("denied", "TARGET_SOURCE_MISMATCH")
        else:
            resolved["target_id"] = target.target_id
            value = request.window
            if (op.dispatched or op.window is not None) and value is None:
                value = binding.default_window or binding.authorized_window
            window = Window.parse(value)
            if window is None:
                failure = ("error", "INVALID_PARAMS")
            elif not scope.window.contains(window):
                failure = ("denied", "WINDOW_OUT_OF_SCOPE")
            elif window.seconds > reg.max_window_seconds:
                failure = ("denied", "WINDOW_TOO_LARGE")
            else:
                planned = window
                params, problem = _accept_params(reg, request.params)
                accepted = params
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
        if reads_evidence(reg):
            # A slice carries its parent's source (#308 U4); the operation
            # header above still names this registration's.
            del identities["source"]
        # Evidence keeps source payload, but its top-level identity projections
        # cannot disagree with the same frozen headers merely by rehashing it.
        for view in (outcome.model_view, outcome.evidence.view):
            for name in set(view) & identities.keys():
                same(view[name], identities[name])
    if op.dispatched:
        if failure is not None:
            mismatch()
        expected_window = Window.parse(
            request.window
            if request.window is not None
            else (binding.default_window or binding.authorized_window)
        )
        same(op.window, expected_window)
        allowed: set[tuple[str, str | None, str]] = {
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
        if outcome.evidence is not None:
            allowed.add(("ok", None, "confirmed"))
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
        # Replay serves these refusals by status and reason; there is no
        # source response whose identity could be compared.
        same(op.window, planned)
        if outcome.source_contact != "none" or outcome.status not in (
            ("denied", "error", "timeout") if presend else ("denied", "error")
        ):
            mismatch()
        if failure is not None:
            same((outcome.status, outcome.reason), failure)
        elif presend:
            # What a ``sent=false`` response can become (#425 reviews r1, r2
            # and bot P1s). Only a request past the window checks reaches the
            # transport, and only one the transport itself refuses gets a
            # sent=false reply: recompute that refusal from the frozen request
            # (``_transport_refusal``) instead of trusting ``finished_at``.
            # Its code maps through the registration (``otel_demo.py``
            # ``_refused`` :1796-1812, classes :791-797, :884-890, :930-938;
            # ``evidence_read.py`` ``_refused`` :240-244, classes :183-188).
            # The executor's post-fetch precedence (``executor.py``
            # ``refuse_after_fetch``) may then replace it with a human decision
            # taken meanwhile or a refused settlement charge, never with the
            # deadline; and a reply after the call's own timeout is
            # GATEWAY_TIMEOUT exactly when elapsed > timeout (:1187-1189).
            if planned is None or accepted is None:
                mismatch()
            code = _transport_refusal(reg, target, scope, op, planned, accepted)
            if code is None:
                mismatch()
            if code == _STORE_DEPENDENT:
                own = ("denied", "EVIDENCE_NOT_READABLE")
            else:
                reason = reg.classify(code)
                own = ("denied" if reason in DENIED_REASONS else "error", reason)
            if (
                op.authorized_at is None
                or op.finished_at is None
                or op.elapsed_seconds is None
                or op.elapsed_seconds
                > (op.finished_at - op.authorized_at).total_seconds()
                + _CLOCK_SLACK_SECONDS
            ):
                mismatch()
            if not (
                (outcome.status, outcome.reason) == own
                or (
                    (outcome.status, outcome.reason) == ("timeout", "GATEWAY_TIMEOUT")
                    and op.timeout_seconds is not None
                    and op.elapsed_seconds > op.timeout_seconds
                )
                or (
                    outcome.status == "denied"
                    and outcome.reason in _CONTROL - {"DEADLINE_EXCEEDED"}
                )
            ):
                mismatch()
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
            or (
                # The re-check just before the transport overwrites
                # ``authorized_at`` and may find the deadline passed; the
                # timeout it then keeps was bounded at the earlier check.
                op.timeout_seconds
                > max(0.0, (scope.deadline - op.authorized_at).total_seconds())
                and not (
                    not op.dispatched
                    and outcome.reason == "DEADLINE_EXCEEDED"
                    and op.authorized_at >= scope.deadline
                )
            )
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
    if outcome.evidence is None:
        _check_view(outcome, reg, target, scope)
    elif outcome.reason == "RESULT_TOO_LARGE":
        _check_kept_view(outcome, reg, target, scope)
    else:
        _check_evidence_view(outcome, reg, scope, event_clock)


def _check_kept_view(
    outcome: ToolOutcome,
    reg: ToolRegistration,
    target: RegisteredTarget,
    scope: QueryScope,
) -> None:
    """An over-limit refusal whose result was kept (#308 PR2): the refusal,
    its row summary, the record identity and the observation's provenance;
    never adopted, never a row."""
    evidence, op, view = outcome.evidence, outcome.operation, outcome.model_view
    assert evidence is not None
    if (
        reg.source == EVIDENCE_SOURCE
        or evidence.adopted
        or outcome.status != "error"
        or evidence.status != "error"
        or not op.dispatched
        or len(evidence.raw) > reg.max_result_bytes
    ):
        mismatch()
    same(view.get("evidence_id"), evidence.evidence_id)
    same(view.get("adopted"), False)
    same(view.get("usage"), _SPILLED_USAGE)
    expected: dict[str, Any] = {
        "source": reg.source,
        "tool_version": reg.version,
        "projection_revision": evidence.projection_revision,
        "observed_at": evidence.observed_at.isoformat(),
        "dispatch_started_at": None
        if op.started_at is None
        else op.started_at.isoformat(),
        "data_as_of": None
        if evidence.data_as_of is None
        else evidence.data_as_of.isoformat(),
        "source_start_at": None
        if evidence.source_start_at is None
        else evidence.source_start_at.isoformat(),
        "source_end_at": None
        if evidence.source_end_at is None
        else evidence.source_end_at.isoformat(),
        "freshness_seconds": evidence.freshness_seconds,
        "result_count": evidence.result_count,
        "incomplete": evidence.incomplete,
    }
    for name in _SPILLED_PROVENANCE_KEYS:
        same(view.get(name), expected[name])
    same(evidence.observed_at, op.finished_at)
    same(view.get("total_rows"), evidence.result_count)
    if (
        evidence.truncated
        or evidence.omitted_rows
        or evidence.omitted_bytes
        or not isinstance(evidence.result_count, int)
    ):
        mismatch()
    refusal = {
        key: value
        for key, value in view.items()
        if key not in {"evidence_id", "adopted", *_SPILLED_PROVENANCE_KEYS}
    }
    _check_view(
        replace(outcome, model_view=refusal, evidence=None),
        reg,
        target,
        scope,
        kept=True,
    )


def _check_evidence_view(
    outcome: ToolOutcome,
    reg: ToolRegistration,
    scope: QueryScope,
    event_clock: datetime,
) -> None:
    evidence, op, view = outcome.evidence, outcome.operation, outcome.model_view
    assert evidence is not None
    if not _GENERIC_VIEW_KEYS <= view.keys():
        mismatch()
    # A ``read_evidence`` slice (#308 U4) states its parent's observation:
    # observed and dispatched before this read, and the rows of the parent it
    # does not show as omitted.
    slice_read = reads_evidence(reg)
    same(evidence.status, outcome.status)
    if slice_read:
        if op.finished_at is None or evidence.observed_at > op.finished_at:
            mismatch()
        _check_slice_raw(evidence.raw, view, adopted=evidence.adopted)
    else:
        same(evidence.observed_at, op.finished_at)
    if evidence.observed_at > event_clock:
        mismatch()
    if evidence.data_as_of is not None and evidence.data_as_of > evidence.observed_at:
        mismatch()
    start, end = evidence.source_start_at, evidence.source_end_at
    if (start is None) != (end is None):
        mismatch()
    if (
        start is not None
        and end is not None
        and not (
            scope.window.start <= start <= end <= scope.window.end
            and end <= evidence.observed_at
        )
    ):
        mismatch()
    for count in (evidence.result_count, evidence.omitted_rows, evidence.omitted_bytes):
        if type(count) is not int or count < 0:
            mismatch()
    if evidence.adopted and outcome.status not in ("ok", "no_data"):
        mismatch()
    content = view["content"]
    if evidence.adopted:
        if not isinstance(content, list):
            mismatch()
        returned = len(content)
    else:
        same(content, None)
        returned = 0
    if slice_read and evidence.adopted:
        slice_header = view["slice"]
        assert isinstance(slice_header, Mapping)  # _check_slice_fields
        same(slice_header["returned"], returned)
        same(evidence.omitted_rows, slice_header["total_rows"] - returned)
    else:
        same(evidence.omitted_rows, evidence.result_count - returned)
    same(evidence.truncated, bool(evidence.omitted_rows))
    expected = {
        "evidence_id": evidence.evidence_id,
        "operation_id": op.operation_id,
        "trust": "untrusted-evidence",
        "status": outcome.status,
        "adopted": evidence.adopted,
        "citable_as_fact": evidence.adopted and outcome.status == "ok",
        "projection_revision": evidence.projection_revision,
        "observed_at": evidence.observed_at.isoformat(),
        "data_as_of": None
        if evidence.data_as_of is None
        else evidence.data_as_of.isoformat(),
        "source_start_at": None if start is None else start.isoformat(),
        "source_end_at": None if end is None else end.isoformat(),
        "freshness_seconds": evidence.freshness_seconds,
        "result_count": evidence.result_count,
        "returned_count": returned,
        "incomplete": evidence.incomplete,
        "truncated": evidence.truncated,
        "omitted_rows": evidence.omitted_rows,
        "omitted_bytes": evidence.omitted_bytes,
    }
    if not slice_read:
        expected["dispatch_started_at"] = (
            None if op.started_at is None else op.started_at.isoformat()
        )
    elif view["dispatch_started_at"] is not None:
        dispatched = view["dispatch_started_at"]
        try:
            moment = datetime.fromisoformat(cast(str, dispatched))
        except (TypeError, ValueError):
            mismatch()
        if not isinstance(dispatched, str) or moment > evidence.observed_at:
            mismatch()
    for name, value in expected.items():
        same(view[name], value)
    window = Window.parse(view["window"])
    if window is None or op.window is None or not op.window.contains(window):
        mismatch()
    lookback = view["lookback_seconds"]
    if lookback is not None and (
        type(lookback) is not int or not 0 <= lookback <= window.seconds
    ):
        mismatch()
    same(
        view["lookback_start_at"],
        None if lookback is None else window.start.isoformat(),
    )


_SLICE_RAW_KEYS = {"parent_evidence_id", "parent_raw_sha256", "offset", "limit", "rows"}


def _check_slice_raw(raw: bytes, view: Mapping[str, Any], *, adopted: bool) -> None:
    """A slice's raw is exactly R5's canonical record and agrees with its view.

    ``{parent_evidence_id, parent_raw_sha256, offset, limit, rows}`` in
    canonical bytes; the parent id, offset and limit are the call's own
    parameters; on an adopted slice the rows are the view's content and the
    ``slice`` header repeats the parent hash, the offset and the row count,
    with ``returned`` the rows ``[offset, offset + limit)`` of ``total_rows``
    can hold. The parent's own bytes are not in the recording, so the rows
    themselves are bound to the parent only through ``parent_raw_sha256``.
    """
    try:
        record = parse(raw)
    except StorageError:
        mismatch()
    if not isinstance(record, Mapping) or set(record) != _SLICE_RAW_KEYS:
        mismatch()
    if canonical(record).encode("utf-8") != raw:
        mismatch()
    query = view.get("query")
    if not isinstance(query, Mapping):
        mismatch()
    assert isinstance(query, Mapping)
    offset, limit, rows = record["offset"], record["limit"], record["rows"]
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or limit < 1
        or not isinstance(rows, list)
        or len(rows) > limit
        or not isinstance(record["parent_raw_sha256"], str)
    ):
        mismatch()
    same(record["parent_evidence_id"], query.get("evidence_id"))
    same(offset, query.get("offset", 0))
    same(limit, query.get("limit"))
    if not adopted:
        return
    _check_slice_fields(view)
    header = view["slice"]
    same(rows, view["content"])
    same(
        (header["parent_evidence_id"], header["parent_raw_sha256"]),
        (record["parent_evidence_id"], record["parent_raw_sha256"]),
    )
    same((header["offset"], header["returned"]), (offset, len(rows)))
    same(len(rows), max(0, min(limit, header["total_rows"] - offset)))


def _check_slice_fields(view: Mapping[str, Any]) -> None:
    """The fixed ``read_evidence`` header (#308 U4): counts and hashes only."""
    header = view.get("slice")
    if not isinstance(view.get("parent_tool"), str) or not isinstance(header, Mapping):
        mismatch()
    assert isinstance(header, Mapping)
    if set(header) != {
        "parent_evidence_id",
        "parent_raw_sha256",
        "offset",
        "returned",
        "total_rows",
    }:
        mismatch()
    for name in ("offset", "returned", "total_rows"):
        if type(header[name]) is not int or header[name] < 0:
            mismatch()
    for name in ("parent_evidence_id", "parent_raw_sha256"):
        if not isinstance(header[name], str) or not header[name]:
            mismatch()
    same(view["query"].get("evidence_id"), header["parent_evidence_id"])


# The over-limit row summary (#308 U5) and the metrics tool's additions.
_SUMMARY = {"total_rows", "row_unit", "suggested_limit"}
_METRICS_SUMMARY = {"series_count", "total_points", "label_cardinality", "suggestion"}


def _summary_expected(
    view: Mapping[str, Any], reg: ToolRegistration, query: object, *, kept: bool
) -> dict[str, Any]:
    """Shape-check the row summary of an over-limit refusal; counts only.

    The rows are not in the recording, so the counts are bounded rather than
    recomputed; label names are the one source-derived text and are bounded
    in number and length (U5).
    """
    keys = set(view) & (_SUMMARY | _METRICS_SUMMARY | {"stored", "usage"})
    if not keys:
        return {}
    metrics = reg.source == OTEL_SOURCE and reg.name == METRICS_TOOL
    expected_keys = _SUMMARY | {"usage"} | (_METRICS_SUMMARY if metrics else set())
    if not kept:
        expected_keys |= {"stored"}
    if keys != expected_keys or reg.source == EVIDENCE_SOURCE:
        mismatch()
    for name in ("total_rows", "suggested_limit"):
        if type(view[name]) is not int or view[name] < 0:
            mismatch()
    if view["suggested_limit"] > view["total_rows"]:
        mismatch()
    if not isinstance(view["row_unit"], str) or not view["row_unit"]:
        mismatch()
    summary: dict[str, Any] = {name: view[name] for name in _SUMMARY}
    summary["usage"] = _SPILLED_USAGE if kept else _UNSTORED_USAGE
    if not kept:
        summary["stored"] = False
    if metrics:
        same(view["series_count"], view["total_rows"])
        if type(view["total_points"]) is not int or view["total_points"] < 0:
            mismatch()
        cardinality = view["label_cardinality"]
        if not isinstance(cardinality, list) or len(cardinality) > MAX_SUMMARY_LABELS:
            mismatch()
        counts = []
        for item in cardinality:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"label", "value_count"}
                or not isinstance(item["label"], str)
                or len(item["label"]) > MAX_SUMMARY_LABEL_CHARS
                or type(item["value_count"]) is not int
                or item["value_count"] < 1
            ):
                mismatch()
            counts.append(item["value_count"])
        if counts != sorted(counts, reverse=True):
            mismatch()
        expr = query.get("expr") if isinstance(query, Mapping) else None
        summary.update(
            series_count=view["series_count"],
            total_points=view["total_points"],
            label_cardinality=cardinality,
            suggestion=topk_suggestion(
                view["series_count"], expr if isinstance(expr, str) else ""
            ),
        )
    return summary


def _check_view(
    outcome: ToolOutcome,
    reg: ToolRegistration,
    target: RegisteredTarget,
    scope: QueryScope,
    *,
    kept: bool = False,
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
    summary_keys = set(view) & (_SUMMARY | _METRICS_SUMMARY | {"stored", "usage"})
    if kept and not summary_keys:
        mismatch()
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
                # Every otel-demo tool picks its window with ``_query_window``
                # (#307 A R10: derived from the registrations).
                and reg.name in TOOL_NAMES
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
            elif reads_evidence(reg):
                # A slice refusal reports its parent's query window, which
                # the store holds and the recording does not (#308 U4).
                window = Window.parse(view["window"])
                if window is None:
                    mismatch()
                assert window is not None
            if not op.window.contains(window) or not scope.window.contains(window):
                mismatch()
            expected.update(
                _view_refusal_detail(
                    tokens, reg.max_view_tokens, query, window.as_json()
                )
            )
            expected.update(_summary_expected(view, reg, query, kept=kept))
        else:
            mismatch()
    elif summary_keys:
        mismatch()
    if "source_error" in view:
        # #311 R6: only a dispatched read the source answered with a 4xx
        # carries one. The error body is not recorded, so its text cannot be
        # recomputed; its shape, bound and trust marking are what is checked.
        if (
            not op.dispatched
            or (outcome.status, outcome.reason, outcome.source_contact)
            not in _SOURCE_ERROR_OUTCOMES
            or not _source_error_ok(view["source_error"])
        ):
            mismatch()
        expected["source_error"] = view["source_error"]
    if set(view) - set(expected):
        mismatch()
    for name, value in view.items():
        same(value, expected[name])


_SOURCE_ERROR_OUTCOMES = frozenset(
    {
        ("error", "INVALID_PARAMS", "confirmed"),
        ("error", "SOURCE_ERROR", "confirmed"),
    }
)


def _source_error_ok(value: object) -> bool:
    """The ``source_error`` shape ``executor._source_error`` builds (#311 U3)."""
    if not isinstance(value, dict) or set(value) != {
        "trust",
        "http_status",
        "error_type",
        "error",
        "error_truncated",
    }:
        return False
    status, error, truncated = (
        value["http_status"],
        value["error"],
        value["error_truncated"],
    )
    return (
        value["trust"] == "untrusted-evidence"
        and type(status) is int
        and 400 <= status <= 499
        and (value["error_type"] is None or _source_error_type_ok(value["error_type"]))
        and type(truncated) is bool
        and (
            (error is None and not truncated)
            or (
                isinstance(error, str)
                and len(error) <= MAX_SOURCE_ERROR_CHARS
                and (not truncated or len(error) == MAX_SOURCE_ERROR_CHARS)
            )
        )
    )
