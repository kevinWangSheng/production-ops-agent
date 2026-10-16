"""External F13 step 4 seam: current Run records -> ``KnowledgeUseOutcome``.

Field sources (R8/R9 and the PR-C public interface): subject/scenario ids
come from the scenario; Run id/state and ``scope_facts.knowledge`` come from
``IncidentStore.rebuild``'s current Run. Policy metadata and entry provenance
are copied from that input snapshot, without content or freshness inference.
``knowledge_checks`` supplies verdicts/kinds, exactly as on the incident page.
Tagged claims come from ``parse_report`` of the published conclusion, whose
inner ``conclusion.evidence_ids`` supplies the evidence membership for
``handled``. Unknown tags retain their complete bracketed spelling and order.
Subject mismatch or an invalid knowledge fact suppresses the entire projection.
The reader calls rebuild once; this module has no SQL, model, telemetry or
settings reads. ``model_requests`` is always empty.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from opspilot.investigation.reports import ReportV2, parse_report
from opspilot.knowledge.hints import knowledge_checks, valid_knowledge

if TYPE_CHECKING:
    from opspilot.acceptance import IncidentScenario

__all__ = [
    "KnowledgeHintOutcome",
    "KnowledgeUseOutcome",
    "KnowledgeUseRecords",
    "knowledge_use_outcome",
    "knowledge_use_records",
]


class IncidentReads(Protocol):
    def rebuild(self, incident_id: UUID) -> dict[str, Any]: ...


@dataclass(frozen=True)
class KnowledgeUseRecords:
    incident_id: UUID
    rebuilt: Mapping[str, Any]


@dataclass(frozen=True)
class KnowledgeHintOutcome:
    entry_id: str
    revision: int
    name: str
    tags: tuple[str, ...]
    content_sha256: str
    source_incident_id: str
    source_postmortem_id: str
    source_version: int
    approved_by: str
    approved_at: str
    observation_ended_at: str
    age_seconds: int
    freshness: str
    truncated: bool
    verdict: str
    claim_kinds: tuple[str, ...]
    tagged_claims: tuple[Mapping[str, Any], ...]
    handled: bool


@dataclass(frozen=True)
class KnowledgeUseOutcome:
    scenario_id: str
    subject_id: str
    run_id: str | None
    run_state: str | None
    status: str
    unknown_reasons: tuple[str, ...]
    policy_version: str | None
    retrieved_at: str | None
    max_age_seconds: int | None
    omitted_for_size: int | None
    entries: tuple[KnowledgeHintOutcome, ...]
    report_available: bool
    run_evidence_ids: tuple[str, ...]
    unprovided_tags: tuple[str, ...]
    model_requests: tuple[Any, ...] = ()


def knowledge_use_records(
    incidents: IncidentReads, incident_id: UUID
) -> KnowledgeUseRecords:
    """Keep the single rebuild result unchanged, as read by the workbench."""
    return KnowledgeUseRecords(incident_id, incidents.rebuild(incident_id))


class _Mismatch(Exception):
    pass


def _same(left: object, right: object) -> None:
    if str(left) != str(right):
        raise _Mismatch


def _committed_report(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    # Same reading rule as opspilot/web/service.py::_committed_report;
    # keep acceptance independent of the web layer.
    if payload.get("kind") == "conclusion":
        inner = payload.get("conclusion")
        if not isinstance(inner, Mapping):
            return None, None
        content = inner.get("report_content")
        reasons = inner.get("handoff_reasons")
        truncated = isinstance(reasons, list) and "OUTPUT_LENGTH" in reasons
        return content if isinstance(content, str) else None, (
            "length" if truncated else "stop"
        )
    assistant = payload.get("assistant")
    content = assistant.get("content") if isinstance(assistant, Mapping) else None
    finish = payload.get("finish_reason")
    return (
        content if isinstance(content, str) else None,
        finish if isinstance(finish, str) else None,
    )


def _hint(
    entry: Mapping[str, Any],
    check: Mapping[str, Any],
    report: ReportV2 | None,
    evidence_ids: tuple[str, ...],
) -> KnowledgeHintOutcome:
    tag = f"[knowledge:{entry['entry_id']}@r{entry['revision']}]"
    tagged = tuple(
        dict(kind=c.kind, text=c.text, evidence_ids=tuple(c.evidence_ids))
        for c in (() if report is None else report.claims)
        if tag in c.text
    )
    # All tagged fact/rejection claims must have current evidence, including
    # a claim with no evidence that contributed no verdict in knowledge_checks.
    deciding = [c for c in tagged if c["kind"] in {"fact", "rejected_hypothesis"}]
    handled = check["verdict"] in {"consistent", "contradicted"} and all(
        c["evidence_ids"] and all(e in evidence_ids for e in c["evidence_ids"])
        for c in deciding
    )
    return KnowledgeHintOutcome(
        entry_id=entry["entry_id"],
        revision=entry["revision"],
        name=entry["name"],
        tags=tuple(entry["tags"]),
        content_sha256=entry["content_sha256"],
        source_incident_id=entry["source_incident_id"],
        source_postmortem_id=entry["source_postmortem_id"],
        source_version=entry["source_version"],
        approved_by=entry["approved_by"],
        approved_at=entry["approved_at"],
        observation_ended_at=entry["observation_ended_at"],
        age_seconds=entry["age_seconds"],
        freshness=entry["freshness"],
        truncated=entry["truncated"],
        verdict=check["verdict"],
        claim_kinds=tuple(check["claim_kinds"]),
        tagged_claims=tagged,
        handled=handled,
    )


def knowledge_use_outcome(
    scenario: IncidentScenario, records: KnowledgeUseRecords
) -> KnowledgeUseOutcome:
    """Pure projection of the current Run; never reinterpret runtime policy."""
    rebuilt = records.rebuilt
    run = rebuilt.get("run") or {}
    reason = None
    try:
        _same(records.incident_id, scenario.subject_id)
        _same(rebuilt.get("incident_id"), scenario.subject_id)
        _same(run.get("incident_id"), scenario.subject_id)
    except _Mismatch:
        reason = "SUBJECT_MISMATCH"
    scope = (run.get("input") or {}).get("scope_facts") or {}
    fact = scope.get("knowledge")
    if reason is None and "knowledge" in scope and not valid_knowledge(fact):
        reason = "INVALID_KNOWLEDGE_FACT"
    if reason is not None:
        return KnowledgeUseOutcome(
            scenario_id=scenario.scenario_id,
            subject_id=scenario.subject_id,
            run_id=None,
            run_state=None,
            status="unknown",
            unknown_reasons=(reason,),
            policy_version=None,
            retrieved_at=None,
            max_age_seconds=None,
            omitted_for_size=None,
            entries=(),
            report_available=False,
            run_evidence_ids=(),
            unprovided_tags=(),
        )
    committed = rebuilt.get("conclusion")
    report = None
    evidence_ids: tuple[str, ...] = ()
    if isinstance(committed, Mapping) and committed:
        content, finish = _committed_report(committed)
        report, _ = parse_report(content, finish_reason=finish or "unknown")
        # Same inner payload field as acceptance._published_conclusion.
        inner = committed.get("conclusion")
        if isinstance(inner, Mapping):
            evidence_ids = tuple(inner.get("evidence_ids") or ())
    entries = (
        tuple(
            _hint(entry, check, report, evidence_ids)
            for entry, check in zip(fact["entries"], knowledge_checks(fact, report))
        )
        if fact is not None
        else ()
    )
    provided = {f"[knowledge:{e.entry_id}@r{e.revision}]" for e in entries}
    unprovided = tuple(
        dict.fromkeys(
            tag
            for claim in (() if report is None else report.claims)
            for tag in re.findall(r"\[knowledge:[^\]\r\n]*\]", claim.text)
            if tag not in provided
        )
    )
    return KnowledgeUseOutcome(
        scenario_id=scenario.scenario_id,
        subject_id=scenario.subject_id,
        run_id=str(run["run_id"]) if run.get("run_id") is not None else None,
        run_state=run.get("state"),
        status="absent" if fact is None else fact["status"],
        unknown_reasons=(),
        policy_version=None if fact is None else fact["policy_version"],
        retrieved_at=None if fact is None else fact["retrieved_at"],
        max_age_seconds=None if fact is None else fact["max_age_seconds"],
        omitted_for_size=None if fact is None else fact["omitted_for_size"],
        entries=entries,
        report_available=report is not None,
        run_evidence_ids=evidence_ids,
        unprovided_tags=unprovided,
    )
