"""Feedback on an investigation's report (migration 0027, workbench P5, contract r12).

One row per incident in ``opspilot_investigation_feedback``, replaced as a
whole by the newest submission (O3/F6). Runs as ``opspilot_web`` only: no
worker, Chat or postmortem-drafting path reads this table (F4, F11 A), and
saving it is not a control action -- the incident row, its inputs, Runs,
controls and observations are never written here (F3).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from opspilot.persistence.base import PersistenceError, _StoreBase

VERDICTS = ("correct", "incorrect")
NEXT_STEPS_VERDICTS = ("correct", "incorrect", "not_applicable")
#: F13: the export is bounded; a larger table is cut at this many rows.
EXPORT_LIMIT = 10_000

_COLUMNS = (
    "f.incident_id,f.verdict,f.actual_root_cause,f.steering_needed,"
    "f.next_steps_verdict,f.correct_next_steps,f.notes,f.rated_run_id,"
    "f.rated_report_sha256,f.updated_by,f.updated_at,f.created_at"
)


class FeedbackStore(_StoreBase):
    def save(
        self,
        incident_id: UUID,
        *,
        fields: Mapping[str, Any],
        rated_run_id: UUID,
        rated_report_sha256: str | None,
        updated_by: str,
    ) -> dict[str, Any]:
        """Insert or replace the incident's one row (last write wins). A
        merged incident is refused in the same statement (``MERGED``), so a
        merge committed after the page check still wins."""
        with self.transaction() as conn:
            row = conn.execute(
                "INSERT INTO opspilot_investigation_feedback (incident_id,verdict,"
                "actual_root_cause,steering_needed,next_steps_verdict,"
                "correct_next_steps,notes,rated_run_id,rated_report_sha256,updated_by) "
                "SELECT i.incident_id,%s,%s,%s,%s,%s,%s,%s,%s,%s "
                "FROM opspilot_incidents i "
                "WHERE i.incident_id=%s AND i.merged_into_incident_id IS NULL "
                "ON CONFLICT (incident_id) DO UPDATE SET "
                "verdict=EXCLUDED.verdict,actual_root_cause=EXCLUDED.actual_root_cause,"
                "steering_needed=EXCLUDED.steering_needed,"
                "next_steps_verdict=EXCLUDED.next_steps_verdict,"
                "correct_next_steps=EXCLUDED.correct_next_steps,notes=EXCLUDED.notes,"
                "rated_run_id=EXCLUDED.rated_run_id,"
                "rated_report_sha256=EXCLUDED.rated_report_sha256,"
                "updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp() "
                "RETURNING incident_id",
                (
                    fields["verdict"],
                    fields.get("actual_root_cause"),
                    fields.get("steering_needed"),
                    fields.get("next_steps_verdict"),
                    fields.get("correct_next_steps"),
                    fields.get("notes"),
                    rated_run_id,
                    rated_report_sha256,
                    updated_by,
                    incident_id,
                ),
            ).fetchone()
            if row is None:
                raise PersistenceError("MERGED")
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM opspilot_investigation_feedback f "
                "WHERE f.incident_id=%s",
                (incident_id,),
            ).fetchone()
            assert found is not None
            return dict(found)

    def get(self, incident_id: UUID) -> dict[str, Any] | None:
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM opspilot_investigation_feedback f "
                "WHERE f.incident_id=%s",
                (incident_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def export(self, *, limit: int = EXPORT_LIMIT) -> list[dict[str, Any]]:
        """Every row, oldest change first, with the rated Run's prompt
        revision (``versions``) and the model its newest model step answered
        with (F13)."""
        with self.transaction(snapshot=True) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"SELECT {_COLUMNS},r.versions->>'prompt_revision' AS prompt_revision,"
                    "(SELECT s.response->>'response_model' FROM opspilot_steps s "
                    "WHERE s.run_id=f.rated_run_id "
                    "AND s.response->>'response_model' IS NOT NULL "
                    "ORDER BY s.sequence DESC, s.observed_at DESC LIMIT 1) AS model "
                    "FROM opspilot_investigation_feedback f "
                    "LEFT JOIN opspilot_runs r ON r.run_id=f.rated_run_id "
                    "ORDER BY f.updated_at, f.incident_id LIMIT %s",
                    (limit,),
                ).fetchall()
            ]
