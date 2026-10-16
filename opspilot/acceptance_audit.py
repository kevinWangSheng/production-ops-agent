"""Read-only audit projection, ordered solely by persisted database clocks.

Rows without their own clock use an explicitly labelled related record's
database timestamp. Unlinkable rows are omitted, never dated with source,
model, process, transaction, or synthetic times. Call in a stable snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from psycopg import Connection, Cursor
from psycopg.rows import dict_row

AUDIT_CATEGORIES = (
    "input",
    "query",
    "evidence",
    "state_change",
    "human_correction",
    "recovery_observation",
    "postmortem_review",
)
# Stable source order, following the contract's source table.
SOURCES = (
    "opspilot_inputs",
    "opspilot_input_rounds",
    "opspilot_alert_deliveries",
    "opspilot_tool_charges",
    "opspilot_evidence",
    "opspilot_subject_events",
    "opspilot_runs",
    "opspilot_suspension_audit",
    "opspilot_controls",
    "opspilot_observation_sessions",
    "opspilot_observation_samples",
    "opspilot_observation_endings",
    "opspilot_self_heal_authorizations",
    "opspilot_postmortems",
    "opspilot_postmortem_versions",
    "opspilot_f13_audit",
    "opspilot_knowledge_entries",
)


# 0002_state_checks Run states: these cannot continue investigation in place
# (domain.runs.RUN_EXECUTION); waiting_human and paused remain resumable.
TERMINAL_RUN_STATES = frozenset(
    {"completed", "cancelled", "failed", "blocked", "budget_exhausted"}
)


@dataclass(frozen=True)
class AuditEntry:
    recorded_at: datetime
    sequence: int
    category: str
    source: str
    record_id: str
    run_id: UUID | None
    actor: str | None
    detail: Mapping[str, object]


@dataclass(frozen=True)
class AuditTimeline:
    incident_id: UUID
    entries: tuple[AuditEntry, ...]
    counts: Mapping[str, int]
    missing: tuple[str, ...]


def audit_timeline(conn: Connection[Any], incident_id: UUID) -> AuditTimeline:
    return audit_records(conn, incident_id)[0]


def audit_records(
    conn: Connection[Any],
    incident_id: UUID,
) -> tuple[AuditTimeline, Mapping[str, int]]:
    """The contracted timeline plus counts of records lacking a clock anchor.

    Kept separate because timeline.missing is contractually zero-count
    categories, not a claim that every row in a populated category is dated.
    """
    with conn.cursor(row_factory=dict_row) as cursor:
        return _audit_records(cursor, incident_id)


def _audit_records(
    cursor: Cursor[dict[str, Any]],
    incident_id: UUID,
) -> tuple[AuditTimeline, Mapping[str, int]]:
    incident = cursor.execute(
        "SELECT incident_id,target_id,created_at FROM opspilot_incidents WHERE incident_id=%s",
        (incident_id,),
    ).fetchone()
    if incident is None:
        raise LookupError(str(incident_id))
    entries: list[AuditEntry] = []
    undated: dict[str, int] = {}

    def add(
        row: Mapping[str, Any],
        category: str,
        source: str,
        identity: object,
        time_column: str,
        fields: tuple[str, ...] = (),
        *,
        run_id: UUID | None = None,
        actor: str | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        at = row.get(time_column)
        if at is None:
            undated[source] = undated.get(source, 0) + 1
            return
        detail = {
            key: str(row[key]) if isinstance(row[key], UUID) else row[key]
            for key in fields
        }
        detail.update(extra or {})
        entries.append(
            AuditEntry(
                at,
                0,
                category,
                source,
                str(identity),
                run_id,
                actor,
                MappingProxyType(detail),
            )
        )

    def rows(sql: str) -> list[dict[str, Any]]:
        return cursor.execute(sql, (incident_id,)).fetchall()

    for (
        row
    ) in rows("""SELECT input_id,kind,sequence,control_generation,received_at,actor
                      FROM opspilot_inputs WHERE incident_id=%s"""):
        add(
            row,
            "input",
            "opspilot_inputs",
            row["input_id"],
            "received_at",
            ("kind", "sequence", "control_generation"),
            actor=row["actor"],
        )
    for row in rows("""SELECT d.delivery_id,d.status,d.outcome,d.annotation_revision,
                             d.received_at,d.actor
                      FROM opspilot_alert_deliveries d WHERE d.incident_id=%s"""):
        add(
            row,
            "input",
            "opspilot_alert_deliveries",
            row["delivery_id"],
            "received_at",
            ("status", "outcome", "annotation_revision"),
            actor=row["actor"],
        )

    # A round's committed step is the first durable database-clock anchor
    # available. It is not claimed to be the round's creation time.
    for row in rows("""SELECT f.run_id,s.step_id,s.observed_at,f.control_generation,
                             f.input_watermark,f.committed
                      FROM opspilot_input_rounds f
                      JOIN opspilot_runs r USING(run_id)
                      LEFT JOIN opspilot_steps s ON s.run_id=f.run_id
                        AND s.logical_key=f.logical_key
                      WHERE r.incident_id=%s"""):
        add(
            row,
            "input",
            "opspilot_input_rounds",
            f"{row['run_id']}:{row['step_id']}",
            "observed_at",
            ("control_generation", "input_watermark", "committed"),
            run_id=row["run_id"],
            extra={"time_basis": "step_recorded"},
        )

    # operation_id is canonically "<step UUID>-t<ordinal>". The timestamp is
    # explicitly the preceding plan commit, never a guessed dispatch time.
    for row in rows("""SELECT c.dispatch_id,c.run_id,c.operation_id,c.epoch,
                             COALESCE(ev.recorded_at,s.observed_at) AS recorded_at,
                             CASE WHEN ev.recorded_at IS NULL THEN 'step_recorded'
                                  ELSE 'tool_event' END AS time_basis,
                             COALESCE(e.status,tr.result->>'status') AS status,
                             COALESCE(e.view->>'tool',tr.result->>'tool') AS tool,
                             COALESCE(e.view->>'target_id',tr.result->>'target_id') AS target_id,
                             tr.result->>'reason' AS reason,
                             tr.result->>'source_contact' AS source_contact,
                             e.evidence_id IS NOT NULL AS has_evidence
                      FROM opspilot_tool_charges c
                      JOIN opspilot_runs r USING(run_id)
                      LEFT JOIN opspilot_steps s ON s.run_id=c.run_id
                        AND s.step_id::text=split_part(c.operation_id,'-t',1)
                      LEFT JOIN opspilot_evidence e
                        ON e.evidence_id=c.operation_id || ':' || c.dispatch_id::text
                        AND e.run_id=c.run_id::text AND e.subject_id=r.incident_id::text
                      LEFT JOIN LATERAL (
                        SELECT item->'result' AS result
                        FROM jsonb_array_elements(s.tool_results) item
                        WHERE item->'result'->>'operation_id'=c.operation_id
                          AND (item->'result'->>'evidence_id'=c.operation_id || ':' || c.dispatch_id::text
                            OR (NOT (item->'result' ? 'evidence_id') AND
                              (SELECT count(*) FROM opspilot_tool_charges other
                               WHERE other.run_id=c.run_id
                                 AND other.operation_id=c.operation_id)=1))
                        LIMIT 1
                      ) tr ON true
                      LEFT JOIN LATERAL (
                        SELECT recorded_at FROM opspilot_subject_events
                        WHERE subject_id=r.incident_id AND kind='tool_committed'
                          AND payload->>'evidence_id'=e.evidence_id
                        ORDER BY recorded_at,sequence LIMIT 1
                      ) ev ON true
                      WHERE r.incident_id=%s"""):
        contact = row["source_contact"]
        outbound = (
            True
            if row["has_evidence"] or contact == "confirmed"
            else (False if contact == "none" else None)
        )
        add(
            row,
            "query",
            "opspilot_tool_charges",
            row["dispatch_id"],
            "recorded_at",
            ("operation_id", "epoch", "tool", "target_id"),
            run_id=row["run_id"],
            extra={
                "result_code": row["status"],
                "reason": row["reason"],
                "outbound": outbound,
                "source_contact": contact,
                "time_basis": row["time_basis"],
            },
        )

    for row in rows("""SELECT e.evidence_id,e.run_id,e.status,e.adopted,e.committed,
                             COALESCE(ev.recorded_at,s.observed_at) AS observed_at,
                             CASE WHEN ev.recorded_at IS NULL THEN 'step_recorded'
                                  ELSE 'tool_event' END AS time_basis
                      FROM opspilot_evidence e
                      LEFT JOIN opspilot_steps s ON s.run_id::text=e.run_id
                        AND s.step_id::text=split_part(split_part(e.evidence_id,':',1),'-t',1)
                      LEFT JOIN LATERAL (
                        SELECT recorded_at FROM opspilot_subject_events
                        WHERE subject_id::text=e.subject_id AND kind='tool_committed'
                          AND payload->>'evidence_id'=e.evidence_id
                        ORDER BY recorded_at,sequence LIMIT 1
                      ) ev ON true
                      WHERE e.subject_id=%s::text"""):
        add(
            row,
            "evidence",
            "opspilot_evidence",
            row["evidence_id"],
            "observed_at",
            ("status", "adopted", "committed"),
            run_id=UUID(row["run_id"]),
            extra={"time_basis": row["time_basis"]},
        )

    run_events: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows("""SELECT sequence,kind,recorded_at,payload->>'run_id' AS run_id,
                             payload->>'code' AS code,payload->>'execution' AS execution
                      FROM opspilot_subject_events WHERE subject_id=%s"""):
        run_id = None
        if row["run_id"]:
            try:
                run_id = UUID(row["run_id"])
            except ValueError:
                pass
        add(
            row,
            "state_change",
            "opspilot_subject_events",
            row["sequence"],
            "recorded_at",
            ("kind", "sequence"),
            run_id=run_id,
        )
        if run_id is not None:
            run_events.setdefault(run_id, []).append(row)
    for (
        row
    ) in rows("""SELECT r.run_id,r.state,c.audit_id,c.created_at AS ended_at,c.actor
                       FROM opspilot_runs r
                       LEFT JOIN LATERAL (
                         SELECT audit_id,created_at,actor FROM opspilot_controls c
                         WHERE c.incident_id=r.incident_id
                           AND c.resulting_generation=r.control_generation
                           AND c.action IN ('cancel','close','follow_up','correct')
                         ORDER BY created_at,audit_id LIMIT 1
                       ) c ON r.state='cancelled'
                       WHERE r.incident_id=%s"""):
        events = run_events.get(row["run_id"], [])
        # Only explicit lifecycle announcements; arbitrary related events
        # cannot prove when a Run began or ended.
        seen_actions: set[str] = set()
        for event in sorted(events, key=lambda e: (e["recorded_at"], e["sequence"])):
            action = {
                "intake_accepted": "created",
                "control_applied": "created",
                "run_completed": "ended",
            }.get(event["kind"])
            # Both claim's version mismatch and Runner._blocked persist the
            # blocked state before announcing it. Require that Run state as
            # well: an arbitrary handoff never proves a terminal transition.
            if row["state"] == "blocked" and (
                (
                    event["kind"] == "run_claim_refused"
                    and event["code"] == "INCOMPATIBLE_STATE"
                )
                or (event["kind"] == "run_handoff" and event["execution"] == "blocked")
            ):
                action = "ended"
            if action and action not in seen_actions:
                seen_actions.add(action)
                add(
                    event,
                    "state_change",
                    "opspilot_runs",
                    f"{row['run_id']}:{event['sequence']}",
                    "recorded_at",
                    run_id=row["run_id"],
                    extra={
                        "action": action,
                        "time_basis": "subject_event",
                        "event_sequence": event["sequence"],
                    },
                )

        if (
            "ended" not in seen_actions
            and row["state"] == "cancelled"
            and row["ended_at"] is not None
        ):
            add(
                row,
                "state_change",
                "opspilot_runs",
                f"{row['run_id']}:ended:{row['audit_id']}",
                "ended_at",
                run_id=row["run_id"],
                actor=row["actor"],
                extra={
                    "action": "ended",
                    "state": row["state"],
                    "time_basis": "control_recorded",
                    "control_audit_id": str(row["audit_id"]),
                },
            )
            seen_actions.add("ended")

        for action in ("created", "ended"):
            if action not in seen_actions and (
                action == "created" or row["state"] in TERMINAL_RUN_STATES
            ):
                key = f"opspilot_runs.{action}"
                undated[key] = undated.get(key, 0) + 1

    for row in cursor.execute(
        """WITH scoped AS (
             SELECT audit_id,target_id,suspended,generation,actor,created_at
             FROM opspilot_suspension_audit
             WHERE target_id IS NULL OR target_id=%s
           )
           SELECT *,false AS baseline FROM scoped WHERE created_at >= %s
           UNION ALL
           SELECT *,true AS baseline FROM (
             SELECT DISTINCT ON (target_id) * FROM scoped WHERE created_at < %s
             ORDER BY target_id,created_at DESC,audit_id DESC
           ) prior""",
        (incident["target_id"], incident["created_at"], incident["created_at"]),
    ).fetchall():
        add(
            row,
            "state_change",
            "opspilot_suspension_audit",
            row["audit_id"],
            "created_at",
            ("target_id", "suspended", "generation"),
            actor=row["actor"],
            extra={"baseline": True} if row["baseline"] else None,
        )
    for row in rows("""SELECT audit_id,action,expected_generation,resulting_generation,
                             actor,created_at FROM opspilot_controls WHERE incident_id=%s"""):
        add(
            row,
            "human_correction",
            "opspilot_controls",
            row["audit_id"],
            "created_at",
            ("action", "expected_generation", "resulting_generation"),
            actor=row["actor"],
        )

    for row in rows("""SELECT session_id,state,purpose,authorization_source,created_at
                      FROM opspilot_observation_sessions WHERE incident_id=%s
                        AND purpose='incident_recovery'"""):
        add(
            row,
            "recovery_observation",
            "opspilot_observation_sessions",
            row["session_id"],
            "created_at",
            ("state", "purpose", "authorization_source"),
        )
    for row in rows("""SELECT m.sample_id,m.sequence,m.outcome,m.disposition,m.reason,
                             m.submitted_at
                      FROM opspilot_observation_samples m
                      JOIN opspilot_observation_sessions s USING(session_id)
                      WHERE s.incident_id=%s AND s.purpose='incident_recovery'"""):
        add(
            row,
            "recovery_observation",
            "opspilot_observation_samples",
            row["sample_id"],
            "submitted_at",
            ("sequence", "outcome", "disposition", "reason"),
        )
    for row in rows("""SELECT ending_id,ended_reason,transition,recorded_at
                      FROM opspilot_observation_endings WHERE incident_id=%s
                        AND purpose='incident_recovery'"""):
        add(
            row,
            "recovery_observation",
            "opspilot_observation_endings",
            row["ending_id"],
            "recorded_at",
            ("ended_reason", "transition"),
        )
    for row in rows("""SELECT control_generation,state,attempts,session_id,created_at
                      FROM opspilot_self_heal_authorizations WHERE incident_id=%s"""):
        add(
            row,
            "recovery_observation",
            "opspilot_self_heal_authorizations",
            f"{incident_id}:{row['control_generation']}",
            "created_at",
            ("control_generation", "state", "attempts", "session_id"),
        )

    for row in rows("""SELECT postmortem_id,created_at FROM opspilot_postmortems
                      WHERE incident_id=%s"""):
        add(
            row,
            "postmortem_review",
            "opspilot_postmortems",
            row["postmortem_id"],
            "created_at",
        )
    for row in rows("""SELECT v.postmortem_id,v.version,v.state,v.created_at
                      FROM opspilot_postmortem_versions v
                      JOIN opspilot_postmortems p USING(postmortem_id)
                      WHERE p.incident_id=%s"""):
        add(
            row,
            "postmortem_review",
            "opspilot_postmortem_versions",
            f"{row['postmortem_id']}:{row['version']}",
            "created_at",
            ("version", "state"),
        )
    for row in rows("""SELECT DISTINCT e.entry_id,e.created_at
                      FROM opspilot_knowledge_entries e
                      JOIN opspilot_knowledge_revisions k USING(entry_id)
                      JOIN opspilot_postmortems p ON p.postmortem_id=k.source_postmortem_id
                      WHERE p.incident_id=%s"""):
        add(
            row,
            "postmortem_review",
            "opspilot_knowledge_entries",
            row["entry_id"],
            "created_at",
        )
    # Knowledge publish/supersede audit the NEW revision; revoke audits the
    # revoked revision (knowledge.store._publish/revoke). Entry identity alone
    # spans incidents. A NULL revision cannot prove incident provenance.
    for row in rows("""SELECT a.event_id,a.action,a.version,a.revision,a.actor_id,a.recorded_at
                      FROM opspilot_f13_audit a
                      WHERE EXISTS (
                        SELECT 1 FROM opspilot_postmortems p WHERE p.incident_id=%s
                        AND ((a.object_kind='postmortem' AND a.object_id=p.postmortem_id)
                          OR (a.object_kind='knowledge_entry' AND a.revision IS NOT NULL AND EXISTS (
                            SELECT 1 FROM opspilot_knowledge_revisions k
                            WHERE k.source_postmortem_id=p.postmortem_id
                              AND k.entry_id=a.object_id AND k.revision=a.revision))))"""):
        add(
            row,
            "postmortem_review",
            "opspilot_f13_audit",
            row["event_id"],
            "recorded_at",
            ("action", "version", "revision"),
            actor=row["actor_id"],
        )

    entries.sort(key=lambda e: (e.recorded_at, SOURCES.index(e.source), e.record_id))
    ordered = tuple(replace(entry, sequence=i) for i, entry in enumerate(entries))
    counts = {
        category: sum(e.category == category for e in ordered)
        for category in AUDIT_CATEGORIES
    }
    return (
        AuditTimeline(
            incident_id,
            ordered,
            MappingProxyType(counts),
            tuple(c for c in AUDIT_CATEGORIES if not counts[c]),
        ),
        MappingProxyType(undated),
    )
