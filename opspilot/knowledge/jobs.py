"""Worker-side schedule of postmortem generation (M1-03 step 2, F13).

Contract r3: D17 (candidate scan and a dedicated generation lease, never an
investigation Run lease), D18 (the worker's compensation scan for stale
marks a business transaction missed), D19 (regenerate a returned version),
D22 (generation attempts, bounded backoff), D23 (each attempt's own model
request budget), D27 (incidents whose observation ended before this code
shipped are candidates like any other, oldest ending first).

A candidate is an incident whose current recovery observation has ended
(its session for the incident's observation generation is no longer
``authorized`` and has an ending record; no other session is authorized)
and that has no postmortem version at its current watermark other than a
stale one -- one generation per watermark: a citation-failed draft, a
rejection or an approval at the same watermark are not retried -- or whose
returned version waits for regeneration (D19). Not while a version is under
review, a lease is held, the backoff has not elapsed, or the retry cap at
this watermark is reached. The watermark here is the control/observation
generation, Run count and current Run, and the input watermark; evidence
alone is compared when the version is written (``record_draft``) and by the
compensation scan.

Tables: ``opspilot_postmortem_generation_jobs`` / ``_attempts`` (0009).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from opspilot.knowledge.contract import RETRYABLE_ATTEMPT_ERRORS
from opspilot.knowledge.store import KnowledgeStore, Watermark, canonical_json
from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

# D17: one generation per worker at a time; the lease covers two model
# calls at the frozen 360 s request timeout plus the writes around them.
GENERATION_LEASE_SECONDS = 900
# D22: retries at one watermark (provider unavailable, output invalid),
# with exponential backoff; MODEL_REJECTED / INPUT_TOO_LARGE do not retry.
MAX_ATTEMPTS_PER_WATERMARK = 5
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600

# The watermark key compared with ``failure_watermark`` (SQL and Python
# must agree): control generation, observation generation, Run count,
# current Run, input watermark.
_KEY_SQL = (
    "concat_ws(':', i.control_generation, i.observation_generation, "
    "(SELECT count(*) FROM opspilot_runs r WHERE r.incident_id = i.incident_id), "
    "COALESCE(i.current_run_id::text, ''), "
    "(SELECT COALESCE(max(n.sequence), 0) FROM opspilot_inputs n "
    "WHERE n.incident_id = i.incident_id))"
)

_CANDIDATE_SQL = f"""
SELECT i.incident_id, e.recorded_at
FROM opspilot_incidents i
JOIN opspilot_observation_sessions s
  ON s.incident_id = i.incident_id
 AND s.observation_generation = i.observation_generation
 AND s.state <> 'authorized'
JOIN opspilot_observation_endings e ON e.session_id = s.session_id
LEFT JOIN opspilot_postmortem_generation_jobs j ON j.incident_id = i.incident_id
LEFT JOIN opspilot_postmortems p ON p.incident_id = i.incident_id
WHERE NOT EXISTS (
    SELECT 1 FROM opspilot_observation_sessions a
    WHERE a.incident_id = i.incident_id AND a.state = 'authorized')
  AND (j.lease_until IS NULL OR j.lease_until <= clock_timestamp())
  AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= clock_timestamp())
  AND NOT (COALESCE(j.consecutive_failures, 0) >= %(max_attempts)s
           AND j.failure_watermark IS NOT DISTINCT FROM {_KEY_SQL})
  AND NOT EXISTS (
    SELECT 1 FROM opspilot_postmortem_versions u
    WHERE u.postmortem_id = p.postmortem_id AND u.state = 'under_review')
  AND (
    j.pending_regeneration_version IS NOT NULL
    OR NOT EXISTS (
      SELECT 1 FROM opspilot_postmortem_versions v
      WHERE v.postmortem_id = p.postmortem_id AND v.state <> 'stale'
        AND v.incident_control_generation = i.control_generation
        AND v.observation_generation = i.observation_generation
        AND v.last_run_id IS NOT DISTINCT FROM i.current_run_id
        AND v.run_count = (SELECT count(*) FROM opspilot_runs r
                           WHERE r.incident_id = i.incident_id)
        AND v.input_watermark = (SELECT COALESCE(max(n.sequence), 0)
                                 FROM opspilot_inputs n
                                 WHERE n.incident_id = i.incident_id)))
  {{incident_filter}}
ORDER BY e.recorded_at, i.incident_id
LIMIT %(limit)s
"""


_JOB_COLUMNS = (
    "SELECT incident_id, lease_owner, lease_epoch, lease_until, "
    "pending_regeneration_version, failure_watermark, consecutive_failures, "
    "next_attempt_at, last_error_code FROM opspilot_postmortem_generation_jobs "
)


def watermark_key(watermark: Watermark) -> str:
    return ":".join(
        str(part)
        for part in (
            watermark.incident_control_generation,
            watermark.observation_generation,
            watermark.run_count,
            "" if watermark.last_run_id is None else watermark.last_run_id,
            watermark.input_watermark,
        )
    )


@dataclass(frozen=True)
class Claim:
    """A held generation lease and the attempt it runs (D17, D22)."""

    attempt_id: UUID
    incident_id: UUID
    owner: UUID
    epoch: int
    max_model_requests: int
    # D19: the returned version this attempt regenerates, if any
    revises_version: int | None


class GenerationStore(_StoreBase):
    """Candidate scan, lease, attempt budget and input reads (worker only)."""

    def candidates(self, *, limit: int) -> tuple[UUID, ...]:
        """Incidents due for generation, oldest observation ending first."""
        with self.transaction(snapshot=True) as conn:
            rows = self._candidate_rows(conn, limit=limit)
        return tuple(row["incident_id"] for row in rows)

    @staticmethod
    def _candidate_rows(
        conn: Connection, *, limit: int, incident_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        statement = _CANDIDATE_SQL.replace(
            "{incident_filter}",
            "" if incident_id is None else "AND i.incident_id = %(incident_id)s",
        )
        return conn.execute(
            statement,
            {
                "limit": limit,
                "incident_id": incident_id,
                "max_attempts": MAX_ATTEMPTS_PER_WATERMARK,
            },
        ).fetchall()

    def claim(
        self,
        incident_id: UUID,
        *,
        owner: UUID,
        versions: dict[str, str],
        max_model_requests: int,
        lease_seconds: int = GENERATION_LEASE_SECONDS,
    ) -> Claim | None:
        """Take the incident's generation lease and open an attempt, or
        ``None`` when it is not (or no longer) a candidate. An attempt left
        ``running`` under a lapsed lease is closed first as ``abandoned``
        and counts as a failure at its watermark (a crash loop is bounded
        by the same cap)."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO opspilot_postmortem_generation_jobs(incident_id) "
                "VALUES (%s) ON CONFLICT (incident_id) DO NOTHING",
                (incident_id,),
            )
            job = self._require_row(
                conn.execute(
                    _JOB_COLUMNS + "WHERE incident_id=%s FOR UPDATE",
                    (incident_id,),
                )
            )
            now = self._db_now(conn)
            if job["lease_until"] is not None and job["lease_until"] > now:
                return None
            for stale in conn.execute(
                "SELECT attempt_id, watermark FROM opspilot_postmortem_generation_attempts "
                "WHERE incident_id=%s AND status='running'",
                (incident_id,),
            ).fetchall():
                conn.execute(
                    "UPDATE opspilot_postmortem_generation_attempts SET status='abandoned', "
                    "error_code='LEASE_LOST', finished_at=clock_timestamp() "
                    "WHERE attempt_id=%s",
                    (stale["attempt_id"],),
                )
                self._count_failure(
                    conn, job, _key_of(stale["watermark"]), "LEASE_LOST", now
                )
                job = self._require_row(
                    conn.execute(
                        _JOB_COLUMNS + "WHERE incident_id=%s",
                        (incident_id,),
                    )
                )
            if not self._candidate_rows(conn, limit=1, incident_id=incident_id):
                conn.execute(
                    "UPDATE opspilot_postmortem_generation_jobs SET lease_owner=NULL, "
                    "lease_until=NULL WHERE incident_id=%s",
                    (incident_id,),
                )
                return None
            watermark = current_watermark(conn, incident_id)
            epoch = job["lease_epoch"] + 1
            conn.execute(
                "UPDATE opspilot_postmortem_generation_jobs SET lease_owner=%s, "
                "lease_epoch=%s, lease_until=clock_timestamp() + make_interval(secs=>%s), "
                "updated_at=clock_timestamp() WHERE incident_id=%s",
                (owner, epoch, lease_seconds, incident_id),
            )
            attempt_id = uuid4()
            conn.execute(
                "INSERT INTO opspilot_postmortem_generation_attempts(attempt_id, "
                "incident_id, lease_owner, lease_epoch, watermark, revises_version, "
                "model, model_profile, prompt_version, output_schema_version, "
                "input_policy_version, max_model_requests) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    attempt_id,
                    incident_id,
                    owner,
                    epoch,
                    Jsonb(_watermark_json(watermark)),
                    job["pending_regeneration_version"],
                    versions["model"],
                    versions["model_profile"],
                    versions["prompt_version"],
                    versions["output_schema_version"],
                    versions["input_policy_version"],
                    max_model_requests,
                ),
            )
            return Claim(
                attempt_id=attempt_id,
                incident_id=incident_id,
                owner=owner,
                epoch=epoch,
                max_model_requests=max_model_requests,
                revises_version=job["pending_regeneration_version"],
            )

    def reserve_model_request(self, claim: Claim) -> int:
        """D23: count one model request against the attempt's own budget
        before it is sent, fenced on the lease. Returns the number used;
        ``LEASE_LOST`` or ``BUDGET_EXHAUSTED`` otherwise."""
        with self.transaction() as conn:
            row = conn.execute(
                "UPDATE opspilot_postmortem_generation_attempts a "
                "SET model_requests = a.model_requests + 1 "
                "FROM opspilot_postmortem_generation_jobs j "
                "WHERE a.attempt_id=%s AND a.status='running' "
                "AND j.incident_id = a.incident_id AND j.lease_owner=%s "
                "AND j.lease_epoch=%s AND j.lease_until > clock_timestamp() "
                "AND a.model_requests < a.max_model_requests "
                "RETURNING a.model_requests",
                (claim.attempt_id, claim.owner, claim.epoch),
            ).fetchone()
            if row is not None:
                return int(row["model_requests"])
            held = conn.execute(
                "SELECT 1 FROM opspilot_postmortem_generation_jobs WHERE incident_id=%s "
                "AND lease_owner=%s AND lease_epoch=%s AND lease_until > clock_timestamp()",
                (claim.incident_id, claim.owner, claim.epoch),
            ).fetchone()
            raise PersistenceError("BUDGET_EXHAUSTED" if held else "LEASE_LOST")

    def finish(
        self,
        claim: Claim,
        *,
        status: str,
        watermark: Watermark | None,
        error_code: str | None = None,
        version: int | None = None,
        response_model: str | None = None,
        input_sha256: str | None = None,
        input_bytes: int | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Close the attempt and release the lease (D22). A retryable
        failure schedules the next attempt with exponential backoff; a
        non-retryable one stops retries at this watermark; success clears
        the failure count. A lease that is no longer ours changes nothing
        but the attempt row (``LEASE_LOST`` is raised after it is closed)."""
        with self.transaction() as conn:
            job = self._require_row(
                conn.execute(
                    _JOB_COLUMNS + "WHERE incident_id=%s FOR UPDATE",
                    (claim.incident_id,),
                )
            )
            closed = conn.execute(
                "UPDATE opspilot_postmortem_generation_attempts SET status=%s, "
                "error_code=%s, version=%s, response_model=%s, input_sha256=%s, "
                "input_bytes=%s, prompt_tokens=%s, completion_tokens=%s, "
                "finished_at=clock_timestamp() WHERE attempt_id=%s AND status='running' "
                "RETURNING watermark",
                (
                    status,
                    error_code,
                    version,
                    response_model,
                    input_sha256,
                    input_bytes,
                    prompt_tokens,
                    completion_tokens,
                    claim.attempt_id,
                ),
            ).fetchone()
            ours = (
                job["lease_owner"] == claim.owner and job["lease_epoch"] == claim.epoch
            )
            if closed is None or not ours:
                raise PersistenceError("LEASE_LOST")
            key = (
                watermark_key(watermark)
                if watermark is not None
                else _key_of(closed["watermark"])
            )
            now = self._db_now(conn)
            if status == "failed":
                assert error_code is not None
                self._count_failure(conn, job, key, error_code, now)
            elif status == "succeeded":
                conn.execute(
                    "UPDATE opspilot_postmortem_generation_jobs SET consecutive_failures=0, "
                    "failure_watermark=NULL, next_attempt_at=NULL, last_error_code=NULL "
                    "WHERE incident_id=%s",
                    (claim.incident_id,),
                )
            else:
                conn.execute(
                    "UPDATE opspilot_postmortem_generation_jobs SET last_error_code=%s "
                    "WHERE incident_id=%s",
                    (error_code, claim.incident_id),
                )
            conn.execute(
                "UPDATE opspilot_postmortem_generation_jobs SET lease_owner=NULL, "
                "lease_until=NULL, updated_at=clock_timestamp() WHERE incident_id=%s",
                (claim.incident_id,),
            )

    @staticmethod
    def _count_failure(
        conn: Connection, job: dict[str, Any], key: str, error_code: str, now: Any
    ) -> None:
        failures = (
            job["consecutive_failures"] + 1 if job["failure_watermark"] == key else 1
        )
        if error_code not in RETRYABLE_ATTEMPT_ERRORS and error_code != "LEASE_LOST":
            failures = MAX_ATTEMPTS_PER_WATERMARK
        delay = min(BACKOFF_BASE_SECONDS * 2 ** (failures - 1), BACKOFF_MAX_SECONDS)
        conn.execute(
            "UPDATE opspilot_postmortem_generation_jobs SET consecutive_failures=%s, "
            "failure_watermark=%s, last_error_code=%s, next_attempt_at=%s, "
            "updated_at=clock_timestamp() WHERE incident_id=%s",
            (
                failures,
                key,
                error_code,
                now + timedelta(seconds=delay),
                job["incident_id"],
            ),
        )

    # --- reads

    def read_input(self, incident_id: UUID) -> dict[str, Any]:
        """Everything a generation reads from PostgreSQL, in one snapshot:
        the watermark (D1), the postmortem head (expected generation), the
        incident, its Runs, human controls and inputs, the committed
        evidence of its Runs, the ended observation with its samples and
        readings (raw bytes excluded), the return reason of the version to
        regenerate (D19), and the entries this postmortem already published
        by proposal key (for supersede proposals). Unfiltered: the input
        policy (``opspilot.knowledge.generation``) decides what reaches the
        model."""
        with self.transaction(snapshot=True) as conn:
            incident = conn.execute(
                "SELECT i.incident_id, i.intake_key, i.state, i.lifecycle, i.mode, "
                "i.control_generation, i.observation_generation, i.current_run_id, "
                "i.conclusion, i.created_at, i.target_id, t.resource_uid "
                "FROM opspilot_incidents i LEFT JOIN opspilot_targets t "
                "ON t.target_id = i.target_id WHERE i.incident_id=%s",
                (incident_id,),
            ).fetchone()
            if incident is None:
                raise PersistenceError("NOT_FOUND")
            watermark = current_watermark(conn, incident_id)
            head = conn.execute(
                "SELECT postmortem_id, generation, latest_version FROM opspilot_postmortems "
                "WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()
            job = conn.execute(
                "SELECT pending_regeneration_version FROM opspilot_postmortem_generation_jobs "
                "WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()
            pending = None if job is None else job["pending_regeneration_version"]
            return_reason = None
            published: dict[str, UUID] = {}
            if head is not None:
                if pending is not None:
                    reason = conn.execute(
                        "SELECT reason FROM opspilot_f13_audit WHERE object_kind='postmortem' "
                        "AND object_id=%s AND action='return' AND version=%s",
                        (head["postmortem_id"], pending),
                    ).fetchone()
                    return_reason = None if reason is None else reason["reason"]
                for row in conn.execute(
                    "SELECT r.source_proposal_key, r.entry_id FROM opspilot_knowledge_revisions r "
                    "JOIN opspilot_knowledge_revision_states s USING (entry_id, revision) "
                    "WHERE r.source_postmortem_id=%s AND s.state='active' "
                    "ORDER BY r.approved_at",
                    (head["postmortem_id"],),
                ).fetchall():
                    published[row["source_proposal_key"]] = row["entry_id"]
            runs = conn.execute(
                "SELECT run_id, state, control_generation, budget_limit, budget_spent, "
                "deadline, input_watermark FROM opspilot_runs WHERE incident_id=%s "
                "ORDER BY control_generation, run_id",
                (incident_id,),
            ).fetchall()
            controls = conn.execute(
                "SELECT action, actor, expected_generation, resulting_generation, created_at, "
                "payload FROM opspilot_controls WHERE incident_id=%s "
                "ORDER BY created_at, resulting_generation",
                (incident_id,),
            ).fetchall()
            inputs = conn.execute(
                "SELECT sequence, kind, content, actor, received_at FROM opspilot_inputs "
                "WHERE incident_id=%s ORDER BY sequence",
                (incident_id,),
            ).fetchall()
            evidence = conn.execute(
                "SELECT e.evidence_id, e.run_id, e.status, e.adopted, e.view, e.view_sha256, "
                "e.observed_at, e.data_as_of FROM opspilot_evidence e "
                "WHERE e.committed AND e.run_id IN (SELECT r.run_id::text FROM opspilot_runs r "
                "WHERE r.incident_id=%s) ORDER BY e.evidence_id",
                (incident_id,),
            ).fetchall()
            session = conn.execute(
                "SELECT session_id, state, ended_reason, health_profile_revision, "
                "authorized_by, authorized_at, deadline_at, max_samples, "
                "sample_interval_seconds, sustained_window_seconds, adopted_count, "
                "healthy_since, target FROM opspilot_observation_sessions "
                "WHERE session_id=%s",
                (watermark.observation_session_id,),
            ).fetchone()
            ending = conn.execute(
                "SELECT ending_id, ended_reason, transition, lifecycle_before, "
                "lifecycle_after, recorded_at FROM opspilot_observation_endings "
                "WHERE ending_id=%s",
                (watermark.observation_ending_id,),
            ).fetchone()
            samples = conn.execute(
                "SELECT sample_id, sequence, window_start, window_end, outcome, "
                "disposition, reason, confirms_health, health_basis, readings_consistent, "
                "transition, submitted_at FROM opspilot_observation_samples "
                "WHERE session_id=%s ORDER BY sequence, submitted_at",
                (watermark.observation_session_id,),
            ).fetchall()
            readings = conn.execute(
                "SELECT g.sample_id, g.signal_name, g.status, g.value, g.sample_count, "
                "g.window_start, g.window_end, g.source, g.raw_sha256 "
                "FROM opspilot_observation_signal_readings g "
                "JOIN opspilot_observation_samples s ON s.sample_id = g.sample_id "
                "WHERE s.session_id=%s ORDER BY s.sequence, g.signal_name",
                (watermark.observation_session_id,),
            ).fetchall()
        return {
            "incident": incident,
            "watermark": watermark,
            "postmortem": head,
            "pending_regeneration_version": pending,
            "return_reason": return_reason,
            "published_entries": published,
            "runs": runs,
            "controls": controls,
            "inputs": inputs,
            "evidence": evidence,
            "observation": {
                "session": session,
                "ending": ending,
                "samples": samples,
                "readings": readings,
            },
        }

    def stale_versions(self, *, limit: int) -> list[dict[str, Any]]:
        """Compensation scan (D18): draft/under_review versions whose
        incident has moved past their watermark, with the reason to record.
        A business transaction marks these synchronously; this finds what a
        crash or a race left behind."""
        found: list[dict[str, Any]] = []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT p.incident_id, p.postmortem_id, p.generation, v.version, "
                "v.incident_control_generation, v.observation_generation, v.run_count, "
                "v.last_run_id, v.input_watermark, v.evidence_snapshot_sha256 "
                "FROM opspilot_postmortem_versions v JOIN opspilot_postmortems p "
                "USING (postmortem_id) WHERE v.state IN ('draft', 'under_review') "
                "ORDER BY v.created_at LIMIT %s",
                (limit,),
            ).fetchall()
            from opspilot.knowledge.store import moved_components, stale_reason_for

            for row in rows:
                reason = stale_reason_for(
                    moved_components(conn, row["incident_id"], row)
                )
                if reason is not None:
                    found.append({**row, "reason": reason})
        return found


def current_watermark(conn: Connection, incident_id: UUID) -> Watermark:
    """The incident's watermark now (D1), from the session of its current
    observation generation and that session's ending record."""
    row = conn.execute(
        "SELECT i.control_generation, i.observation_generation, i.current_run_id, "
        "(SELECT count(*) FROM opspilot_runs r WHERE r.incident_id = i.incident_id) AS run_count, "
        "(SELECT COALESCE(max(n.sequence), 0) FROM opspilot_inputs n "
        "WHERE n.incident_id = i.incident_id) AS input_watermark, "
        "s.session_id, e.ending_id FROM opspilot_incidents i "
        "JOIN opspilot_observation_sessions s ON s.incident_id = i.incident_id "
        "AND s.observation_generation = i.observation_generation AND s.state <> 'authorized' "
        "JOIN opspilot_observation_endings e ON e.session_id = s.session_id "
        "WHERE i.incident_id=%s",
        (incident_id,),
    ).fetchone()
    if row is None:
        raise PersistenceError("NOT_ELIGIBLE")
    return Watermark(
        incident_control_generation=row["control_generation"],
        observation_generation=row["observation_generation"],
        observation_session_id=row["session_id"],
        observation_ending_id=row["ending_id"],
        run_count=int(row["run_count"]),
        last_run_id=row["current_run_id"],
        input_watermark=int(row["input_watermark"]),
        evidence_snapshot_sha256=KnowledgeStore._evidence_snapshot(conn, incident_id),
    )


def _watermark_json(watermark: Watermark) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, UUID) else value
        for key, value in watermark.__dict__.items()
    }


def _key_of(stored: dict[str, Any]) -> str:
    return ":".join(
        str(part)
        for part in (
            stored["incident_control_generation"],
            stored["observation_generation"],
            stored["run_count"],
            stored["last_run_id"] or "",
            stored["input_watermark"],
        )
    )


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_MAX_SECONDS",
    "GENERATION_LEASE_SECONDS",
    "MAX_ATTEMPTS_PER_WATERMARK",
    "Claim",
    "GenerationStore",
    "canonical_json",
    "current_watermark",
    "watermark_key",
]
