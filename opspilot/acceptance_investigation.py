"""Read-only F3 projection of stored observations, refusals and report claims.

Call in a stable snapshot. Missing legacy view fields are labelled, never
backfilled. Evidence integrity uses the workbench's read-back decoder and
hash verification over the bytes read in this transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from opspilot.acceptance_audit import audit_timeline
from opspilot.investigation.reports import CLAIM_KINDS, parse_report
from opspilot.web.evidence import StoredEvidence, _from_row
from opspilot.web.service import _committed_report

_VIEW_FIELDS = (
    "evidence_id",
    "status",
    "adopted",
    "tool",
    "tool_version",
    "registry_revision",
    "tool_registry_revision",
    "source",
    "target_id",
    "query",
    "window",
    "lookback_seconds",
    "lookback_start_at",
    "observed_at",
    "data_as_of",
    "freshness_seconds",
    "projection_revision",
)


@dataclass(frozen=True)
class ObservationEntry:
    evidence_id: str
    run_id: str
    status: str
    adopted: bool
    tool: str | None
    tool_version: str | None
    registry_revision: str | None
    tool_registry_revision: str | None
    source: str | None
    target_id: str | None
    query: Mapping[str, Any] | None
    window: Mapping[str, Any] | None
    lookback_seconds: float | None
    lookback_start_at: str | None
    observed_at: str | None
    data_as_of: str | None
    freshness_seconds: float | None
    projection_revision: str | None
    resolvable: bool
    missing_fields: tuple[str, ...]


@dataclass(frozen=True)
class RefusedOperation:
    operation_id: str
    run_id: UUID | None
    tool: object
    target_id: object
    status: str
    reason: object
    source_contact: object


@dataclass(frozen=True)
class EvidenceReference:
    evidence_id: str
    resolvable: bool
    ok: bool
    citable_as_fact: bool


@dataclass(frozen=True)
class Claim:
    text: str
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class ReportClaims:
    run_id: UUID
    claims: Mapping[str, tuple[Claim, ...]]


@dataclass(frozen=True)
class InvestigationEvidence:
    incident_id: UUID
    observations: tuple[ObservationEntry, ...]
    refused_operations: tuple[RefusedOperation, ...]
    reports: tuple[ReportClaims, ...]


def _resolves(record: StoredEvidence, run_id: str, incident_id: UUID) -> bool:
    return (
        record.run_id == run_id
        and record.subject_id == str(incident_id)
        and record.hashes_verified
    )


def investigation_evidence(
    conn: Connection[Any], incident_id: UUID
) -> InvestigationEvidence:
    """SELECT only; unknown incidents raise LookupError, as audit_timeline."""
    timeline = audit_timeline(conn, incident_id)
    with conn.cursor(row_factory=dict_row) as cursor:
        runs = cursor.execute(
            "SELECT run_id FROM opspilot_runs WHERE incident_id=%s",
            (incident_id,),
        ).fetchall()
        run_ids = {str(row["run_id"]) for row in runs}
        rows = cursor.execute(
            """SELECT e.* FROM opspilot_evidence e
               WHERE e.subject_id=%s OR e.run_id IN
                 (SELECT run_id::text FROM opspilot_runs WHERE incident_id=%s)
               ORDER BY e.observed_at,e.evidence_id""",
            (str(incident_id), incident_id),
        ).fetchall()
        records = {row["evidence_id"]: _from_row(row) for row in rows}
        observations = []
        for record in records.values():
            if record.status not in {"ok", "no_data"}:
                continue
            view = record.view
            observations.append(
                ObservationEntry(
                    evidence_id=record.evidence_id,
                    run_id=record.run_id,
                    status=record.status,
                    adopted=record.adopted,
                    **{key: view.get(key) for key in _VIEW_FIELDS[3:]},
                    resolvable=record.run_id in run_ids
                    and _resolves(record, record.run_id, incident_id),
                    missing_fields=tuple(
                        key for key in _VIEW_FIELDS if key not in view
                    ),
                )
            )

        # Reuse the audit's committed-result correlation rather than inventing
        # a second interpretation of dispatch IDs and pre-dispatch refusals.
        refused = [
            RefusedOperation(
                operation_id=str(entry.detail["operation_id"]),
                run_id=entry.run_id,
                tool=entry.detail.get("tool"),
                target_id=entry.detail.get("target_id"),
                status=str(entry.detail["result_code"]),
                reason=entry.detail.get("reason"),
                source_contact=entry.detail.get("source_contact"),
            )
            for entry in timeline.entries
            if entry.category == "query"
            and entry.detail.get("record_kind") == "result"
            and entry.detail.get("result_code") in {"error", "timeout", "denied"}
        ]
        represented = {
            (item.run_id, item.operation_id, item.status, item.reason)
            for item in refused
        }
        # Schema/scope refusals can commit before a dispatch is charged, so
        # they have no audit query row. Retain those stored results as well.
        result_rows = cursor.execute(
            """SELECT s.run_id,item->'result' AS result
               FROM opspilot_steps s JOIN opspilot_runs r USING(run_id)
               CROSS JOIN LATERAL jsonb_array_elements(s.tool_results) item
               WHERE r.incident_id=%s AND s.status!='late_result'
                 AND item->'result'->>'status' IN ('error','timeout','denied')
               ORDER BY s.observed_at,s.step_id,(item->>'ordinal')::int""",
            (incident_id,),
        ).fetchall()
        for row in result_rows:
            result = row["result"]
            key = (
                row["run_id"],
                result["operation_id"],
                result["status"],
                result.get("reason"),
            )
            if key in represented:
                continue
            refused.append(
                RefusedOperation(
                    operation_id=result["operation_id"],
                    run_id=row["run_id"],
                    tool=result.get("tool"),
                    target_id=result.get("target_id"),
                    status=result["status"],
                    reason=result.get("reason"),
                    source_contact=result.get("source_contact"),
                )
            )
            represented.add(key)

        reports = []
        # Handed-off conclusions remain committed steps (ADR-0005). For a
        # completed Run, the latest conclusion is its published report, as in
        # DurableStore.investigation_history. Exclude late/abandoned reports.
        report_rows = cursor.execute(
            """SELECT s.run_id,s.response FROM opspilot_steps s
               JOIN opspilot_runs r USING(run_id)
               WHERE r.incident_id=%s AND s.status!='late_result'
                 AND s.response->>'kind'='conclusion'
                 AND (s.response->'conclusion'->>'handoff'='true'
                      OR (r.state='completed' AND s.step_id=(
                        SELECT last.step_id FROM opspilot_steps last
                        WHERE last.run_id=r.run_id AND last.status!='late_result'
                          AND last.response->>'kind'='conclusion'
                        ORDER BY last.sequence DESC,last.step_id DESC LIMIT 1)))
               ORDER BY s.observed_at,s.step_id""",
            (incident_id,),
        ).fetchall()
        for row in report_rows:
            content, finish = _committed_report(row["response"])
            report, _ = parse_report(content, finish_reason=finish or "unknown")
            if report is None:
                continue
            grouped: dict[str, list[Claim]] = {kind: [] for kind in CLAIM_KINDS}
            for claim in report.claims:
                refs = []
                for evidence_id in claim.evidence_ids:
                    cited = records.get(evidence_id)
                    refs.append(
                        EvidenceReference(
                            evidence_id,
                            cited is not None
                            and _resolves(cited, str(row["run_id"]), incident_id),
                            cited is not None and cited.status == "ok",
                            cited is not None
                            and cited.view.get("citable_as_fact") is True,
                        )
                    )
                grouped[claim.kind].append(Claim(claim.text, tuple(refs)))
            reports.append(
                ReportClaims(
                    row["run_id"],
                    MappingProxyType(
                        {kind: tuple(items) for kind, items in grouped.items()}
                    ),
                )
            )
    return InvestigationEvidence(
        incident_id, tuple(observations), tuple(refused), tuple(reports)
    )
