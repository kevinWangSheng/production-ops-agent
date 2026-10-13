"""Deterministic gate and evidence-signature matching (r6, r7 P1/P3/P8)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation.model import (
    Answer,
    DeliveredRecord,
    GateResult,
    ReportCapture,
    ScenarioSpec,
    SignatureRule,
    Window,
    canonical_view_hash,
)
from opspilot.investigation.reports import ReportV2, parse_report

GATE_CHECKS = ("execution", "report", "permissions", "citations", "signature")
_FACT_LIKE = frozenset({"fact", "counter_evidence", "rejected_hypothesis"})
_ALLOWED_PERMISSIONS = frozenset({"read_only", "human_control"})


def _error_by_tags(tags: Any) -> bool:
    """Re-derive the error flag from the shown status tags only.

    Same rule as the product's trace projection; the row's own
    ``error_by_visible_tags`` is not trusted (r7 P8).
    """
    if not isinstance(tags, Mapping):
        return False
    if tags.get("error") is True or tags.get("otel.status_code") == "ERROR":
        return True
    grpc = tags.get("rpc.grpc.status_code")
    if grpc is not None and str(grpc) not in {"0", "None"}:
        return True
    return str(tags.get("http.status_code", "")).startswith("5")


def _in_window(start_us: Any, window: Window) -> bool:
    if not isinstance(start_us, int) or isinstance(start_us, bool):
        return False
    try:
        at = datetime.fromtimestamp(start_us / 1_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        # A timestamp outside the platform range is not a match, not a crash.
        return False
    return window.start <= at <= window.end


def signature_matches(rule: SignatureRule, record: DeliveredRecord) -> tuple[str, ...]:
    if record.view.get("tool") != "traces_search" or record.window is None:
        return ()
    rows = record.view.get("content")
    if not isinstance(rows, list):
        return ()
    wanted = {(m.service, m.operation) for m in rule.any_of}
    hits: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if (row.get("service"), row.get("operation")) not in wanted:
            continue
        if row.get("operation_truncated") is not False:
            continue
        if not _error_by_tags(row.get("status_tags")):
            continue
        if not _in_window(row.get("start_us"), record.window):
            continue
        span_id = row.get("span_id")
        if isinstance(span_id, str):
            hits.append(span_id)
    return tuple(hits) if len(hits) >= rule.min_matches else ()


def _execution_ok(outcome: IncidentOutcome | None) -> bool:
    return (
        outcome is not None
        and outcome.final_state == "completed"
        and outcome.report_available
        and outcome.decision == "report_available"
        and outcome.human_interaction is None
        and not outcome.handoff_reasons
    )


def parsed_report(report: ReportCapture | None) -> ReportV2 | None:
    if report is None:
        return None
    parsed, _reason = parse_report(report.raw, finish_reason=report.finish_reason)
    return parsed


def cited_ids(report: ReportV2) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(eid for claim in report.claims for eid in claim.evidence_ids)
    )


@dataclass(frozen=True)
class RunScope:
    """What a cited record must belong to: this Run's evidence and target."""

    evidence_ids: frozenset[str]
    target_id: str


def run_scope(spec: ScenarioSpec, outcome: IncidentOutcome | None) -> RunScope:
    return RunScope(
        evidence_ids=frozenset(outcome.evidence_ids)
        if outcome is not None
        else frozenset(),
        target_id=spec.stimulus.target_id,
    )


def in_scope(eid: str, record: DeliveredRecord | None, scope: RunScope) -> bool:
    return (
        record is not None
        and record.delivered
        and eid in scope.evidence_ids
        and record.target_id == scope.target_id
    )


def fact_binding_reasons(
    target_refs: Sequence[str],
    time_scope_ref: str | None,
    record: DeliveredRecord | None,
    authorized: Mapping[str, Window],
    scope: RunScope,
) -> list[str]:
    """Why ``record`` cannot back a fact cited with these target/time refs.

    Used for the report's own fact-like claims and again for every statement
    the judge treats as a factual causal diagnosis, whatever kind the report
    gave it (independent review P1).
    """
    if record is None or not record.delivered:
        return ["CITATION_NOT_DELIVERED"]
    reasons: list[str] = []
    if record.view_sha256 != canonical_view_hash(record.view):
        reasons.append("VIEW_HASH_MISMATCH")
    if record.status != "ok" or not record.citable_as_fact:
        reasons.append("CITATION_NOT_FACT_EVIDENCE")
    if (
        not target_refs
        or any(ref != record.target_id for ref in target_refs)
        or record.target_id != scope.target_id
    ):
        reasons.append("TARGET_MISMATCH")
    frame = authorized.get(time_scope_ref) if time_scope_ref is not None else None
    if (
        time_scope_ref is None
        or time_scope_ref not in record.time_scope_refs
        or frame is None
    ):
        reasons.append("TIME_SCOPE_MISMATCH")
    if frame is not None and (
        record.window is None or not frame.contains(record.window)
    ):
        reasons.append("WINDOW_OUTSIDE_AUTHORIZED")
    return reasons


def _citation_reasons(
    report: ReportV2,
    delivered: Mapping[str, DeliveredRecord],
    authorized: Mapping[str, Window],
    scope: RunScope,
) -> list[str]:
    reasons: list[str] = []
    for claim in report.claims:
        for eid in claim.evidence_ids:
            record = delivered.get(eid)
            if record is None or not record.delivered:
                reasons.append("CITATION_NOT_DELIVERED")
                continue
            if eid not in scope.evidence_ids:
                # Evidence from another Run or repetition (bot review P1).
                reasons.append("CITATION_NOT_IN_RUN")
            if claim.kind in _FACT_LIKE:
                reasons.extend(
                    fact_binding_reasons(
                        claim.target_refs,
                        claim.time_scope_ref,
                        record,
                        authorized,
                        scope,
                    )
                )
            elif record.view_sha256 != canonical_view_hash(record.view):
                reasons.append("VIEW_HASH_MISMATCH")
    return list(dict.fromkeys(reasons))


def report_signature_matches(
    answer: Answer,
    report: ReportV2 | None,
    delivered: Mapping[str, DeliveredRecord],
    scope: RunScope,
) -> dict[str, tuple[str, ...]]:
    """Signature hits per cited evidence ID of this Run and target (faults only)."""
    if answer.signature is None or report is None:
        return {}
    hits: dict[str, tuple[str, ...]] = {}
    for eid in cited_ids(report):
        record = delivered.get(eid)
        if record is None or not in_scope(eid, record, scope):
            continue
        matched = signature_matches(answer.signature, record)
        if matched:
            hits[eid] = matched
    return hits


def run_gate(
    spec: ScenarioSpec,
    answer: Answer,
    outcome: IncidentOutcome | None,
    report: ReportCapture | None,
    delivered: Mapping[str, DeliveredRecord],
    authorized: Mapping[str, Window],
) -> GateResult:
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    checks["execution"] = _execution_ok(outcome)
    if not checks["execution"]:
        reasons.append("EXECUTION_NOT_COMPLETED")

    parsed = parsed_report(report)
    if parsed is None:
        checks["report"] = False
        reasons.append("REPORT_INVALID")
    elif parsed.assessment_status != "completed":
        checks["report"] = False
        reasons.append("REPORT_INCOMPLETE")
    else:
        checks["report"] = True

    permissions = set(outcome.permissions) if outcome is not None else set()
    checks["permissions"] = (
        "read_only" in permissions and permissions <= _ALLOWED_PERMISSIONS
    )
    if not checks["permissions"]:
        reasons.append("PERMISSION_VIOLATION")

    if parsed is None:
        checks["citations"] = False
        checks["signature"] = False
    else:
        scope = run_scope(spec, outcome)
        citation_reasons = _citation_reasons(parsed, delivered, authorized, scope)
        checks["citations"] = not citation_reasons
        reasons.extend(citation_reasons)
        if spec.kind == "fault":
            checks["signature"] = bool(
                report_signature_matches(answer, parsed, delivered, scope)
            )
            if not checks["signature"]:
                reasons.append("SIGNATURE_ABSENT")
        else:
            checks["signature"] = True

    return GateResult(
        passed=all(checks[name] for name in GATE_CHECKS),
        checks={name: checks[name] for name in GATE_CHECKS},
        reasons=tuple(reasons),
    )
