"""Pure helpers for the live scenario driver (eval PR-C, A5/A8, Q1).

Interface: docs/tasks/2026-10-18-eval-pr-c-interface.md. Nothing here talks to
the lab, a database or a model: it reads an already verified recording.

The round manifest is frozen before any attempt starts (P10), so its
``registry``/``permissions`` dimensions describe the static registrations and
the per-Run scope *policy*; each recording keeps the per-Run bound manifest
it was recorded under (B2). :func:`check_recording_binding` ties the two
together so a recording cannot be filed under another attempt or Run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation.codec import StorageError, canonical_bytes, encode, parse
from opspilot.evaluation.model import (
    AttemptRecord,
    DeliveredRecord,
    Window,
    canonical_view_hash,
)
from opspilot.evaluation.recording import Recording, blob, json_blob, sha
from opspilot.evaluation.rounds import Manifest
from opspilot.investigation.context import delivered_view
from opspilot.tools.executor import QueryScope
from opspilot.tools.registry import RegisteredTarget, ToolRegistration, canonical_hash

# Run identity inside a QueryScope; everything else is the round's policy.
_RUN_IDENTITY = frozenset(
    {
        "scope_id",
        "subject_id",
        "run_id",
        "control_generation",
        "global_suspension_generation",
        "target_suspension_generation",
        "window",
        "default_query_window",
        "deadline",
    }
)


@dataclass(frozen=True)
class LiveEvidence:
    delivered: Mapping[str, DeliveredRecord]
    authorized: Mapping[str, Window]
    delivery_records: Mapping[str, bytes]


def registry_hash(
    tools: tuple[ToolRegistration, ...], targets: tuple[RegisteredTarget, ...]
) -> str:
    """The round's ``registry`` dimension: the static registration DTOs."""
    return canonical_hash(
        {
            "tools": sorted(
                (encode(tool) for tool in tools), key=lambda item: item["name"]
            ),
            "targets": sorted(
                (encode(target) for target in targets),
                key=lambda item: item["target_id"],
            ),
        }
    )


def scope_policy_hash(scope: QueryScope) -> str:
    """The round's ``permissions`` dimension: a scope without Run identity."""
    encoded = encode(scope)
    policy = {key: value for key, value in encoded.items() if key not in _RUN_IDENTITY}
    window = scope.window
    policy["window_seconds"] = (window.end - window.start).total_seconds()
    policy["default_query_window"] = scope.default_query_window is not None
    return canonical_hash(policy)


def _mismatch() -> None:
    raise StorageError("IDENTITY_MISMATCH")


def check_recording_binding(
    recording: Recording,
    attempt: AttemptRecord,
    *,
    round_id: str,
    epoch: int,
    manifest: Manifest,
) -> None:
    """Refuse a recording that is not this attempt's own Run (Q1 guarantee)."""
    context = recording.context
    run_id = attempt.product_run_id
    if (
        run_id is None
        or attempt.round_id != round_id
        or manifest.round_id != round_id
        or context.recording_id != f"eval-live-{round_id}-{attempt.attempt_id}"
        or context.source_run_id != run_id
        or (context.scenario_id, context.scenario_version)
        != (attempt.scenario_id, attempt.scenario_version)
        or not context.tool_snapshots
        or context.source_manifest is None
    ):
        _mismatch()
    assert context.source_manifest is not None
    bound = dict(context.source_manifest.dimensions)
    frozen = dict(manifest.dimensions)
    for name in ("registry", "permissions"):
        bound.pop(name, None)
        frozen.pop(name, None)
    if bound != frozen:
        _mismatch()
    # source_manifest_hash only proves the recording is self-consistent; the
    # snapshots must also recompute to the round's own registry and scope
    # policy, so another tool set or permission policy never joins the round.
    registry = manifest.dimensions.get("registry")
    permissions = manifest.dimensions.get("permissions")
    for snapshot in context.tool_snapshots:
        scope = snapshot.scope
        if (
            scope.run_id != run_id
            or scope.scope_id != f"otel-demo:{run_id}:{epoch}"
            or registry is None
            or registry.value != registry_hash(snapshot.tools, snapshot.targets)
            or permissions is None
            or permissions.value != scope_policy_hash(scope)
        ):
            _mismatch()


def _window(value: Any) -> Window | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return Window(
            datetime.fromisoformat(str(value["start"])),
            datetime.fromisoformat(str(value["end"])),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _evidence_context(recording: Recording) -> Mapping[str, Any] | None:
    for ref in recording.context.input_refs:
        value = parse(blob(recording, ref)).get("evidence_context")
        if isinstance(value, Mapping):
            return value
    return None


def evidence_from_recording(
    recording: Recording, *, outcome: IncidentOutcome | None
) -> LiveEvidence:
    """The B1 visible-evidence inputs of one verified live recording.

    A record is ``delivered`` only when its exact tool message reached a later
    physical model request (the same rule ``validate_recording`` enforces for
    the outcome's evidence). ``outcome`` is accepted for symmetry with the
    loader; every recorded evidence ID is returned, cited or not.
    """
    del outcome
    evidence_context = _evidence_context(recording)
    authorized: dict[str, Window] = {}
    policies = (evidence_context or {}).get("time_policies") or ()
    for policy in policies:
        if isinstance(policy, Mapping) and isinstance(policy.get("id"), str):
            window = _window(policy.get("window"))
            if window is not None:
                authorized[policy["id"]] = window
    targets = frozenset(binding.target for binding in recording.context.bindings)
    tool_evidence: dict[int, str] = {}
    sent: list[tuple[int, str]] = []
    for event in recording.events:
        if event.kind == "tool" and event.response_ref is not None:
            payload = json_blob(recording, event.response_ref)
            evidence = payload.get("evidence")
            if isinstance(evidence, Mapping) and isinstance(
                evidence.get("evidence_id"), str
            ):
                tool_evidence[event.seq] = evidence["evidence_id"]
        elif event.kind == "model":
            call = json_blob(recording, event.request_ref)["call"]
            for message in call.get("messages", ()):
                if isinstance(message, Mapping) and message.get("role") == "tool":
                    sent.append((event.seq, sha(canonical_bytes(message))))
    delivered: dict[str, DeliveredRecord] = {}
    for event in recording.events:
        if event.kind != "delivery" or event.response_ref is None:
            continue
        view = json_blob(recording, event.request_ref)
        message = json_blob(recording, event.response_ref)
        if event.parent is not None:
            evidence_id = tool_evidence.get(event.parent)
        else:
            claimed = view.get("evidence_id")
            evidence_id = claimed if claimed in tool_evidence.values() else None
        if evidence_id is None:
            continue
        message_sha = sha(canonical_bytes(message))
        reached = any(seq > event.seq and digest == message_sha for seq, digest in sent)
        citation = delivered_view(
            view, evidence_context=evidence_context, authorized_targets=targets
        )
        target = view.get("target_id")
        record = DeliveredRecord(
            evidence_id=evidence_id,
            target_id=target if isinstance(target, str) else "",
            status=str(view.get("status", "unknown")),
            citable_as_fact=citation is not None and citation.citable_as_fact,
            time_scope_refs=frozenset()
            if citation is None
            else frozenset(citation.time_scope_refs),
            window=_window(view.get("window")),
            view=view,
            view_sha256=canonical_view_hash(view),
            delivered=reached,
        )
        previous = delivered.get(evidence_id)
        if previous is None or (reached and not previous.delivered):
            delivered[evidence_id] = record
    return LiveEvidence(
        delivered=delivered,
        authorized=authorized,
        delivery_records={
            eid: canonical_bytes(record) for eid, record in sorted(delivered.items())
        },
    )
