"""Offline reconstruction of release sampling and control history."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import ValidationError

from opspilot.domain.base import DTO, Count
from opspilot.domain.evidence import QueryWindow
from opspilot.domain.subjects import ReleaseStatus
from opspilot.observation.release_commands import utc_values
from opspilot.observation.release_policy import ReleasePolicy, canonical_json, sha256
from opspilot.observation.release_types import (
    ReleaseEvaluation,
    ReleaseSampleEvidence,
    ReleaseSampleLease,
    ReleaseSampleReceipt,
)
from opspilot.observation.releases import ReleaseRecord
from opspilot.observer.health_profile import HealthProfile
from opspilot.observer.release_evaluation import (
    ReleaseEvaluationContext,
    advance_release_window,
    evaluate_release_sample,
    query_evidence_consistent,
)


class ReleaseReplay(DTO):
    observation_id: UUID
    consistent: bool
    stored_status: ReleaseStatus
    replayed_status: ReleaseStatus
    verdict: Literal["healthy", "degraded", "unknown"]
    used_sample_count: Count
    integrity: tuple[str, ...]
    sample_evaluations: tuple[ReleaseEvaluation, ...]
    external_queries: tuple[()] = ()
    model_requests: tuple[()] = ()


#: The retention cleanup removed raw query bodies of this release (#244):
#: the replay cannot redo the comparison and says so instead of reporting a
#: mismatch; its verdict is ``unknown``.
DATA_CLEANED = "REPLAY_DATA_CLEANED"


def _cleaned(rows: Any) -> bool:
    return any(
        isinstance(row, Mapping) and row.get("payload_state") == "cleaned"
        for row in rows or ()
    )


_AUDIT_ENDING_REASONS = {
    "pause": "authority_revoked",
    "resume": "authority_revoked",
    "cancel": "cancelled",
    "deadline_expired": "deadline_expired",
}
_AUDITED_ENDING_REASONS = frozenset(_AUDIT_ENDING_REASONS.values())


def _snapshot_matches(actual: object, expected: Mapping[str, Any]) -> bool:
    """Compare a complete saved JSON result with typed historical facts."""
    if not isinstance(actual, Mapping) or actual.keys() != expected.keys():
        return False
    for key, expected_value in expected.items():
        value = actual[key]
        try:
            if isinstance(expected_value, UUID):
                if not isinstance(value, str) or UUID(value) != expected_value:
                    return False
            elif isinstance(expected_value, datetime):
                if (
                    not isinstance(value, str)
                    or datetime.fromisoformat(value) != expected_value
                ):
                    return False
            elif type(value) is not type(expected_value) or value != expected_value:
                return False
        except ValueError:
            return False
    return True


def _replay_release_history(history: Mapping[str, object]) -> ReleaseReplay:
    issues: set[str] = set()
    release_value = history.get("release")
    release = (
        release_value
        if isinstance(release_value, ReleaseRecord)
        else ReleaseRecord.model_validate_json(canonical_json(release_value))
    )
    release = release.model_copy(
        update={
            key: value.astimezone(timezone.utc)
            for key, value in release.model_dump().items()
            if isinstance(value, datetime)
        }
    )
    samples: Any = history.get("samples", [])
    if any(_cleaned(m.get("queries")) for m in samples):
        # #244: the retention cleanup removed raw query bodies. Nothing can be
        # recomputed, and nothing was altered either: unknown, not a mismatch.
        # The rows (query, key, window, hash) stay in the history.
        return ReleaseReplay(
            observation_id=release.observation_id,
            consistent=False,
            stored_status=release.status,
            replayed_status="unknown",
            verdict="unknown",
            used_sample_count=0,
            integrity=(DATA_CLEANED,),
            sample_evaluations=(),
        )
    sessions: Any = history.get("sessions", [])
    endings: Any = history.get("endings", [])
    audits: Any = history.get("audit", [])
    profile_data: Any = history.get("health_profile")
    profile = None
    policy = None
    if history.get("format_version") != 1 or any(
        k not in history
        for k in ("sessions", "samples", "endings", "audit", "health_profile")
    ):
        issues.add("SUBJECT_BINDING_MISMATCH")
    try:
        profile = HealthProfile.model_validate_json(profile_data["content"])
        if (
            profile_data["revision"] != release.health_profile_revision
            or profile.revision != profile_data["revision"]
            or sha256(profile_data["content"].encode())
            != profile_data["content_sha256"]
        ):
            issues.add("PROFILE_MISMATCH")
    except (ValidationError, ValueError, TypeError, KeyError):
        issues.add("PROFILE_MISMATCH")
    if release.deadline_at != release.released_at + timedelta(
        seconds=release.deadline_seconds
    ):
        issues.add("POLICY_MISMATCH")
    if release.calibrated_policy_revision is not None:
        try:
            policy = ReleasePolicy.model_validate_json(
                canonical_json(
                    {
                        "policy_id": release.calibrated_policy_revision.rsplit("@", 1)[
                            0
                        ],
                        "revision": release.calibrated_policy_revision,
                        "content_sha256": release.calibrated_policy_sha256,
                        "content": json.loads(release.calibrated_policy_content or ""),
                    }
                )
            )
            if (
                policy.content.target != release.target_snapshot
                or policy.content.health_profile_revision
                != release.health_profile_revision
                or policy.content.min_tracking_seconds != release.min_tracking_seconds
                or policy.content.health_profile_sha256
                != profile_data["content_sha256"]
            ):
                issues.add("POLICY_MISMATCH")
        except (ValidationError, ValueError, TypeError, KeyError):
            issues.add("POLICY_MISMATCH")
    elif sessions or samples:
        issues.add("POLICY_MISMATCH")
    by_session = {s["session_id"]: s for s in sessions}
    marks: dict[UUID, dict[str, Any]] = {
        sid: {
            "tracking_since": None,
            "tracking_until": None,
            "healthy_since": None,
            "adopted_count": 0,
            "adopted_sequence": 0,
            "adopted_window_end": None,
        }
        for sid in by_session
    }
    for s in sessions:
        if (
            s.get("purpose") != "release_observation"
            or s.get("subject_kind") != "release_observation"
            or s.get("subject_id") != release.observation_id
            or s.get("release_observation_id") != release.observation_id
            or s.get("incident_id") is not None
            or s.get("authorization_source") is not None
        ):
            issues.add("SUBJECT_BINDING_MISMATCH")
        if (
            s.get("release_policy_revision") != release.calibrated_policy_revision
            or s.get("health_profile_revision") != release.health_profile_revision
            or s.get("deadline_at") != release.deadline_at
            or any(
                s.get(k) != getattr(release, k)
                for k in (
                    "max_samples",
                    "sample_interval_seconds",
                    "sustained_window_seconds",
                )
            )
        ):
            issues.add("POLICY_MISMATCH")
    events: list[tuple[datetime, int, str, Any]] = []
    for a in audits:
        events.append((a["recorded_at"], 0, "audit", a))
    for m in samples:
        if m.get("decision_at") is None:
            issues.add("SAMPLE_DECISION_MISMATCH")
            continue
        events.append((m["decision_at"], 1, "sample", m))
    events.sort(
        key=lambda e: (e[0], e[1], str(e[3].get("sample_id", e[3].get("audit_id", ""))))
    )
    # Audit recorded_at is later than decision time in the same transaction;
    # authorization necessarily commits before any sample can be claimed.
    late_delivery = release.received_at >= release.deadline_at
    status: Any = "unknown" if late_delivery else "pending"
    subject_states = [(release.received_at, status)]
    terminal_reason = "late_delivery" if late_delivery else None
    terminal_sample: Any = None
    terminal_audit: Any = None
    last_event_at = terminal_time_floor = release.received_at
    used = 0
    control_generation = observation_generation = 0
    paused = False
    audit_sequence = 0
    evaluations = []
    ended_samples: dict[UUID, tuple[str, str, UUID]] = {}
    receipt_endings: dict[UUID, UUID | None] = {}
    for event_at, _, kind, item in events:
        prior_event_at = last_event_at
        last_event_at = max(last_event_at, event_at)
        if kind == "audit":
            a = item
            if a.get("observation_id") != release.observation_id:
                issues.add("SUBJECT_BINDING_MISMATCH")
            audit_sequence += 1
            if (
                a["sequence"] != audit_sequence
                or a["control_generation_before"] != control_generation
                or a["observation_generation_before"] != observation_generation
                or a["paused_before"] != paused
            ):
                issues.add("WATERMARK_MISMATCH")
            if a["status_before"] != status:
                issues.add("STATUS_MISMATCH")
            if a["outcome"] == "applied":
                if a["status_before"] != status:
                    issues.add("STATUS_MISMATCH")
                if (
                    a["action"] == "deadline_expired"
                    and a["recorded_at"] < release.deadline_at
                ):
                    issues.add("ENDING_MISMATCH")
                if a["action"] in ("authorize", "resume"):
                    s = by_session.get(a["session_id"])
                    if (
                        s is None
                        or s.get("release_budget_used_at_authorization") != used
                    ):
                        issues.add("BUDGET_MISMATCH")
                expected_control = control_generation + (
                    1 if a["action"] in ("pause", "resume", "cancel") else 0
                )
                expected_observation = observation_generation + (
                    0 if a["action"] == "deadline_expired" else 1
                )
                expected_paused = (
                    True
                    if a["action"] == "pause"
                    else paused
                    if a["action"] == "deadline_expired"
                    else False
                )
                if (
                    a["control_generation_after"] != expected_control
                    or a["observation_generation_after"] != expected_observation
                    or a["paused_after"] != expected_paused
                ):
                    issues.add("WATERMARK_MISMATCH")
                if a["action"] in ("authorize", "resume"):
                    session = by_session.get(a["session_id"])
                    if (
                        session is None
                        or session["subject_control_generation"] != expected_control
                        or session["observation_generation"] != expected_observation
                    ):
                        issues.add("SUBJECT_BINDING_MISMATCH")
                control_generation = expected_control
                observation_generation = expected_observation
                paused = expected_paused
                action = a["action"]
                expected_status = (
                    status
                    if action == "pause"
                    else "observing"
                    if action in ("authorize", "resume")
                    else "cancelled"
                    if action == "cancel"
                    else "unknown"
                    if action == "deadline_expired"
                    else None
                )
                if (
                    status not in ("pending", "observing")
                    or expected_status is None
                    or a["status_after"] != expected_status
                ):
                    issues.add("STATUS_MISMATCH")
                if action == "pause" and paused is not True:
                    issues.add("WATERMARK_MISMATCH")
                status = expected_status or status
                subject_states.append((event_at, status))
                if action in ("cancel", "deadline_expired"):
                    terminal_reason = (
                        "cancelled" if action == "cancel" else "deadline_expired"
                    )
                    terminal_audit = a
                    terminal_time_floor = prior_event_at
            elif (
                a["control_generation_after"] != control_generation
                or a["observation_generation_after"] != observation_generation
                or a["paused_after"] != paused
                or a["status_after"] != status
            ):
                issues.add("SAMPLE_DECISION_MISMATCH")
            expected_result = {
                "observation_id": release.observation_id,
                "audit_id": a["audit_id"],
                "sequence": a["sequence"],
                "action": a["action"],
                "outcome": a["outcome"],
                "reason": a.get("reason"),
                "session_id": a.get("session_id"),
                "ending_id": a.get("ending_id"),
                "status": status,
                "paused": paused,
                "control_generation": control_generation,
                "observation_generation": observation_generation,
                "used_sample_count": used,
                "deadline_at": release.deadline_at,
                "replayed": False,
            }
            result = a.get("result")
            if not _snapshot_matches(result, expected_result):
                issues.add("SAMPLE_DECISION_MISMATCH")
                if not isinstance(result, Mapping) or any(
                    not _snapshot_matches(
                        {key: result[key]} if key in result else {},
                        {key: expected_result[key]},
                    )
                    for key in ("session_id", "ending_id")
                ):
                    issues.add("ENDING_MISMATCH")
            continue
        m = item
        s = by_session.get(m.get("session_id"))
        if s is None or profile is None or policy is None:
            issues.add("SUBJECT_BINDING_MISMATCH")
            continue
        if (
            m.get("purpose") != "release_observation"
            or m.get("subject_kind") != "release_observation"
            or m.get("subject_id") != release.observation_id
            or any(
                m.get(k) is not None
                for k in (
                    "subject_lifecycle",
                    "incident_control_generation",
                    "incident_observation_generation",
                )
            )
        ):
            issues.add("SUBJECT_BINDING_MISMATCH")
        context = ReleaseEvaluationContext(
            release=release,
            session_id=s["session_id"],
            authorized_at=s["authorized_at"],
            policy=policy,
            health_profile=profile,
        )
        try:
            from opspilot.observation.release_types import ReleaseQueryResult

            queries = []
            for q in m["queries"]:
                if q.get("sample_id") != m["sample_id"]:
                    issues.add("SUBJECT_BINDING_MISMATCH")
                    issues.add("QUERY_MISMATCH")
                data = {
                    k: v
                    for k, v in q.items()
                    if k not in ("sample_id", "payload_state")
                }
                raw = data["raw"]
                if not isinstance(raw, bytes):
                    raw = bytes(raw)
                data["raw"] = raw
                if sha256(raw) != data["raw_sha256"]:
                    issues.add("RAW_SHA256_MISMATCH")
                    issues.add("QUERY_MISMATCH")
                queries.append(ReleaseQueryResult.model_validate(data))
            evidence = ReleaseSampleEvidence(
                format_version=1,
                sample_id=m["sample_id"],
                window=QueryWindow(start=m["window_start"], end=m["window_end"]),
                queries=tuple(queries),
            )
            evaluation = evaluate_release_sample(
                context, evidence, decision_at=m["decision_at"]
            )
            evaluations.append(evaluation)
            consistent = query_evidence_consistent(context, evidence, m["decision_at"])
            # A missing key can be legitimate interrupted history. Existing
            # rows must still match the plan even when adoption was fenced.
            if not query_evidence_consistent(
                context, evidence, m["decision_at"], require_complete=False
            ):
                issues.add("QUERY_MISMATCH")
            if (
                evaluation.model_dump(mode="json") != m["release_evaluation"]
                or evaluation.outcome != m["outcome"]
                or evaluation.evidence_sufficient != m["required_signals_present"]
            ):
                issues.add("SAMPLE_DECISION_MISMATCH")
            lease = ReleaseSampleLease.model_validate_json(
                canonical_json(m["release_receipt"]["_lease"])
            )
            from opspilot.observation.release_samples import (
                release_sample_reason,
                sample_fingerprint,
            )

            if sample_fingerprint(lease, evidence) != m["release_evidence_sha256"]:
                issues.add("RAW_SHA256_MISMATCH")
            if (
                lease.job_id != m["job_id"]
                or lease.sequence != m["sequence"]
                or lease.epoch != m["epoch"]
                or lease.session_id != s["session_id"]
            ):
                issues.add("SUBJECT_BINDING_MISMATCH")
        except (ValidationError, KeyError, TypeError, ValueError):
            issues.add("SAMPLE_DECISION_MISMATCH")
            continue
        if (
            lease.observation_id != release.observation_id
            or lease.subject_control_generation != s["subject_control_generation"]
            or lease.observation_generation != s["observation_generation"]
            or lease.health_profile_revision != s["health_profile_revision"]
            or lease.calibrated_policy_revision != s["release_policy_revision"]
            or lease.deadline_at != s["deadline_at"]
            or lease.global_suspension_generation != s["authorized_global_generation"]
            or lease.target_suspension_generation != s["authorized_target_generation"]
            or lease.sample_interval_seconds != s["sample_interval_seconds"]
        ):
            issues.add("SUBJECT_BINDING_MISMATCH")
        if (
            m["subject_control_generation"] != lease.subject_control_generation
            or m["observation_generation"] != lease.observation_generation
            or m["health_profile_revision"] != lease.health_profile_revision
        ):
            issues.add("SUBJECT_BINDING_MISMATCH")
        if (
            m["current_subject_control_generation"] != control_generation
            or m["current_subject_observation_generation"] != observation_generation
            or m["current_subject_state"] != status
            or (m["lease_valid"] and m["decision_at"] >= lease.lease_until)
        ):
            issues.add("SAMPLE_DECISION_MISMATCH")
        fold = marks[s["session_id"]]
        # Ended-session returns cannot be adopted; final state alone cannot date
        # revocation, so use the immutable ending timestamp at this decision.
        prior_end = next(
            (
                e
                for e in endings
                if e["session_id"] == s["session_id"]
                and e["recorded_at"] <= m["decision_at"]
            ),
            None,
        )
        reason = release_sample_reason(
            {
                "control_generation": control_generation,
                "observation_generation": observation_generation,
                "status": status,
                "paused": paused,
                "deadline_at": release.deadline_at,
            },
            {
                **fold,
                "state": "authorized" if prior_end is None else "revoked",
                "authorized": prior_end is None,
            },
            lease,
            evidence,
            decision_at=m["decision_at"],
            blocked=(
                m["scope_suspended"]
                or m["global_generation"] != s["authorized_global_generation"]
                or m["target_generation"] != s["authorized_target_generation"]
            ),
            lease_valid=m["lease_valid"],
            lease_stamps_match=m["lease_stamps_match"],
            readings_consistent=consistent,
        )
        if (
            (reason == "adopted") != (m["disposition"] == "adopted")
            or reason != m["reason"]
            or m["readings_consistent"] != consistent
            or m["within_deadline"] != (m["decision_at"] < release.deadline_at)
        ):
            issues.add("SAMPLE_DECISION_MISMATCH")
        terminal = None
        if reason == "adopted":
            used += 1
            updated, terminal, ending_reason = advance_release_window(
                context,
                fold,
                evaluation,
                window_start=m["window_start"],
                window_end=m["window_end"],
                decision_at=m["decision_at"],
                used_sample_count=used,
            )
            fold.update(
                updated,
                adopted_count=fold["adopted_count"] + 1,
                adopted_sequence=m["sequence"],
                adopted_window_end=m["window_end"],
            )
            if terminal:
                status = terminal
                subject_states.append((event_at, status))
                terminal_reason = ending_reason
                terminal_sample = m
                ended_samples[s["session_id"]] = (
                    terminal,
                    ending_reason or "",
                    m["sample_id"],
                )
        if m["transition"] != ("release_" + terminal if terminal else None):
            issues.add("SAMPLE_DECISION_MISMATCH")
        tracking_seconds = healthy_seconds = 0.0
        if reason == "adopted":
            if (
                fold["tracking_since"] is not None
                and fold["tracking_until"] is not None
            ):
                tracking_seconds = (
                    fold["tracking_until"] - fold["tracking_since"]
                ).total_seconds()
            if (
                fold["healthy_since"] is not None
                and fold["adopted_window_end"] is not None
            ):
                healthy_seconds = (
                    fold["adopted_window_end"] - fold["healthy_since"]
                ).total_seconds()
        # Reconstruct the receipt at this decision, not from the session's
        # final row: a later pause/claim/terminal sample can end it afterwards.
        expected_ending = None
        session_state = "authorized"
        due = None
        if prior_end is not None:
            session_state = (
                "completed"
                if prior_end["ended_reason"]
                in ("healthy_confirmed", "anomaly_detected")
                else "expired"
                if prior_end["ended_reason"]
                in ("deadline_expired", "max_samples_exhausted")
                else "revoked"
            )
        elif terminal or reason in (
            "suspended",
            "control_generation_stale",
            "observation_generation_stale",
            "subject_state_not_adoptable",
        ):
            expected_ending = uuid5(
                release.observation_id, "session-ending:" + str(s["session_id"])
            )
            session_state = (
                "completed"
                if terminal in ("healthy", "anomalous")
                else "expired"
                if terminal
                else "revoked"
            )
            matches = [e for e in endings if e["ending_id"] == expected_ending]
            expected_reason = (
                terminal_reason
                if terminal
                else "scope_suspended"
                if reason == "suspended"
                else "binding_stale"
            )
            if len(matches) != 1 or (
                matches[0]["ended_reason"] != expected_reason
                or matches[0]["session_id"] != s["session_id"]
                or (terminal and matches[0]["job_id"] != m["job_id"])
                or (terminal and matches[0]["job_sequence"] != m["sequence"])
                or matches[0]["recorded_at"] < m["decision_at"]
                or (not terminal and matches[0]["recorded_at"] > m["submitted_at"])
            ):
                issues.add("ENDING_MISMATCH")
        elif reason == "adopted" or m["lease_valid"]:
            due = m["decision_at"] + timedelta(seconds=release.sample_interval_seconds)
        saved_receipt = m["release_receipt"]
        if not isinstance(saved_receipt, Mapping) or not _snapshot_matches(
            {"ending_id": saved_receipt["ending_id"]}
            if "ending_id" in saved_receipt
            else {},
            {"ending_id": expected_ending},
        ):
            issues.add("ENDING_MISMATCH")
        try:
            receipt = ReleaseSampleReceipt.model_validate_json(
                canonical_json(
                    {k: v for k, v in m["release_receipt"].items() if k != "_lease"}
                )
            )
            receipt_endings[m["sample_id"]] = receipt.ending_id
            if (
                receipt.tracking_seconds != tracking_seconds
                or receipt.healthy_window_seconds != healthy_seconds
                or receipt.sample_id != m["sample_id"]
                or receipt.session_id != s["session_id"]
                or receipt.observation_id != release.observation_id
                or receipt.used_sample_count != used
                or receipt.accepted != (reason == "adopted")
                or receipt.reason != reason
                or receipt.release_status != status
                or receipt.evaluation != evaluation
                or receipt.replayed is not False
                or receipt.disposition != m["disposition"]
                or receipt.session_state != session_state
                or receipt.next_sample_due_at != due
                or set(saved_receipt)
                != set(ReleaseSampleReceipt.model_fields) | {"_lease"}
            ):
                issues.add("SAMPLE_DECISION_MISMATCH")
        except (ValueError, TypeError):
            issues.add("SAMPLE_DECISION_MISMATCH")
    for s in sessions:
        fold = marks[s["session_id"]]
        if any(s.get(k) != v for k, v in fold.items()):
            issues.add("WATERMARK_MISMATCH")
        facts = [e for e in endings if e["session_id"] == s["session_id"]]
        if (s["state"] == "authorized" and facts) or (
            s["state"] != "authorized" and len(facts) != 1
        ):
            issues.add("ENDING_MISMATCH")
        if terminal_reason is not None and s["state"] == "authorized":
            issues.add("ENDING_MISMATCH")
    for sid, expected_terminal_sample in ended_samples.items():
        matches = [
            e
            for e in endings
            if e["session_id"] == sid
            and e.get("sample_id") == expected_terminal_sample[2]
        ]
        if len(matches) != 1:
            issues.add("ENDING_MISMATCH")
    by_sample = {m["sample_id"]: m for m in samples}
    by_ending = {e["ending_id"]: e for e in endings}
    ending_audits: dict[UUID, list[Any]] = {}
    if len(by_ending) != len(endings):
        issues.add("ENDING_MISMATCH")
    # Validate audit -> ending independently of the archived reason: renaming
    # an ending cannot opt it out of the control event that references it.
    for a in audits:
        if a.get("outcome") == "applied" and a.get("action") in ("pause", "resume"):
            result = a.get("result")
            recorded_sessions = (
                a.get("session_id"),
                result.get("session_id") if isinstance(result, Mapping) else None,
            )
            for value in recorded_sessions:
                if value is None:
                    continue
                try:
                    recorded_sid = UUID(str(value))
                except (ValueError, TypeError):
                    issues.add("ENDING_MISMATCH")
                    continue
                recorded_session = by_session.get(recorded_sid)
                if recorded_session is None:
                    issues.add("ENDING_MISMATCH")
                    continue
                # Resume normally records its new session (after generations).
                # Only an explicitly recorded before-generation session is old.
                if recorded_session.get("subject_control_generation") == a.get(
                    "control_generation_before"
                ) and recorded_session.get("observation_generation") == a.get(
                    "observation_generation_before"
                ):
                    expected_id = uuid5(
                        release.observation_id, "session-ending:" + str(recorded_sid)
                    )
                    recorded_ending = by_ending.get(expected_id)
                    if (
                        a.get("ending_id") != expected_id
                        or recorded_ending is None
                        or recorded_ending.get("session_id") != recorded_sid
                        or recorded_ending.get("ended_reason") != "authority_revoked"
                        or recorded_session.get("ended_reason") != "authority_revoked"
                    ):
                        issues.add("ENDING_MISMATCH")
        if a.get("ending_id") is None:
            continue
        if a.get("outcome") != "applied":
            # Rejected commands produce no ending, so cannot claim one of
            # the independently produced or previously controlled facts.
            issues.add("ENDING_MISMATCH")
            continue
        ending_audits.setdefault(a["ending_id"], []).append(a)
        referenced = by_ending.get(a["ending_id"])
        session = by_session.get(referenced.get("session_id")) if referenced else None
        expected_reason = _AUDIT_ENDING_REASONS.get(a.get("action"))
        if (
            expected_reason is None
            or referenced is None
            or session is None
            or referenced.get("ended_reason") != expected_reason
            or session.get("ended_reason") != expected_reason
            or a.get("observation_id") != release.observation_id
            or referenced.get("subject_id") != a.get("observation_id")
            or session.get("subject_id") != a.get("observation_id")
        ):
            issues.add("ENDING_MISMATCH")
    for e in endings:
        if any(
            field not in e
            for field in (
                "ended_reason",
                "transition",
                "subject_state_before",
                "subject_state_after",
            )
        ):
            issues.add("ENDING_MISMATCH")
            continue
        reason = e.get("ended_reason")
        before = e.get("subject_state_before")
        after = e.get("subject_state_after")
        transition = e.get("transition")
        references = ending_audits.get(e.get("ending_id"), [])
        # Validate ending -> audit cardinality for every reason, including
        # reasons that must originate outside the control/deadline audit path.
        if reason in _AUDITED_ENDING_REASONS:
            if (
                len(references) != 1
                or _AUDIT_ENDING_REASONS.get(references[0].get("action")) != reason
            ):
                issues.add("ENDING_MISMATCH")
        elif references:
            issues.add("ENDING_MISMATCH")
        # Mirror the reason-specific release edges, including endings that
        # revoke sampling authority without terminating the release subject.
        if reason in ("healthy_confirmed", "anomaly_detected"):
            expected_after = "healthy" if reason == "healthy_confirmed" else "anomalous"
            edge_matches = (
                before == "observing"
                and after == expected_after
                and transition == "release_" + expected_after
                and e.get("sample_id") is not None
            )
        elif reason in ("deadline_expired", "max_samples_exhausted"):
            edge_matches = (
                before in ("pending", "observing")
                and after == "unknown"
                and transition == "release_unknown"
            )
        elif reason in ("authority_revoked", "binding_stale", "scope_suspended"):
            edge_matches = (
                before in ("pending", "observing")
                and after == before
                and transition is None
            )
        elif reason == "cancelled":
            edge_matches = (
                before in ("pending", "observing")
                and after == "cancelled"
                and transition is None
            )
        else:
            edge_matches = False
        if not edge_matches:
            issues.add("ENDING_MISMATCH")
        sample = by_sample.get(e.get("sample_id"))
        recorded_at = e.get("recorded_at")
        if sample is not None:
            expected_before = sample.get("current_subject_state")
        elif isinstance(recorded_at, datetime):
            expected_before = next(
                (state for at, state in reversed(subject_states) if at < recorded_at),
                None,
            )
        else:
            expected_before = None
        if before != expected_before:
            issues.add("ENDING_MISMATCH")
        if (
            e.get("ending_id")
            != uuid5(
                release.observation_id, "session-ending:" + str(e.get("session_id"))
            )
            or e.get("purpose") != "release_observation"
            or e.get("subject_kind") != "release_observation"
            or e.get("subject_id") != release.observation_id
            or e.get("release_observation_id") != release.observation_id
        ):
            issues.add("ENDING_MISMATCH")
        s = by_session.get(e["session_id"])
        if s is None or s["ended_reason"] != e["ended_reason"]:
            issues.add("ENDING_MISMATCH")
        else:
            if (
                not isinstance(recorded_at, datetime)
                or not isinstance(s.get("authorized_at"), datetime)
                or recorded_at < s["authorized_at"]
            ):
                issues.add("ENDING_MISMATCH")
            if (
                e.get("subject_kind") != s.get("subject_kind")
                or not isinstance(e.get("job_id"), UUID)
                or e.get("job_sequence") != s.get("issued_sequence")
            ):
                issues.add("ENDING_MISMATCH")
            expected_state = (
                "completed"
                if e["ended_reason"] in ("healthy_confirmed", "anomaly_detected")
                else "expired"
                if e["ended_reason"] in ("deadline_expired", "max_samples_exhausted")
                else "revoked"
            )
            if s["state"] != expected_state:
                issues.add("ENDING_MISMATCH")
        if reason == "authority_revoked":
            controls = [
                a
                for a in references
                if s is not None
                and a.get("action") in ("pause", "resume")
                and a.get("outcome") == "applied"
                and a.get("observation_id") == release.observation_id
                and a.get("ending_id") == e.get("ending_id")
            ]
            if s is None or len(controls) != 1:
                issues.add("ENDING_MISMATCH")
            else:
                control = controls[0]
                if (
                    control.get("status_before") != before
                    or control.get("status_after") != after
                    or control.get("control_generation_before")
                    != s["subject_control_generation"]
                    or control.get("observation_generation_before")
                    != s["observation_generation"]
                    or control.get("control_generation_after")
                    != s["subject_control_generation"] + 1
                    or control.get("observation_generation_after")
                    != s["observation_generation"] + 1
                    or not isinstance(recorded_at, datetime)
                    or not isinstance(s.get("authorized_at"), datetime)
                    or not s["authorized_at"] <= recorded_at <= control["recorded_at"]
                ):
                    issues.add("ENDING_MISMATCH")
                if control["action"] == "pause":
                    if (
                        control.get("session_id") is not None
                        or control.get("paused_before") is not False
                        or control.get("paused_after") is not True
                    ):
                        issues.add("ENDING_MISMATCH")
                else:
                    replacement = by_session.get(control.get("session_id"))
                    if (
                        replacement is None
                        or replacement["session_id"] == s["session_id"]
                        or replacement.get("subject_id") != s.get("subject_id")
                        or replacement.get("subject_control_generation")
                        != control["control_generation_after"]
                        or replacement.get("observation_generation")
                        != control["observation_generation_after"]
                        or control.get("paused_after") is not False
                        or not isinstance(recorded_at, datetime)
                        or not isinstance(replacement.get("authorized_at"), datetime)
                        or not recorded_at
                        <= replacement["authorized_at"]
                        <= control["recorded_at"]
                    ):
                        issues.add("ENDING_MISMATCH")
        if e.get("sample_id") is not None:
            sample = by_sample.get(e["sample_id"])
            if (
                sample is None
                or sample.get("session_id") != e["session_id"]
                or sample.get("subject_kind") != e.get("subject_kind")
                or sample.get("job_id") != e.get("job_id")
                or sample.get("sequence") != e.get("job_sequence")
            ):
                issues.add("ENDING_MISMATCH")
        if (
            e["ended_reason"]
            in ("healthy_confirmed", "anomaly_detected", "max_samples_exhausted")
            and e["session_id"] not in ended_samples
        ):
            issues.add("ENDING_MISMATCH")
        ending_terminal = ended_samples.get(e["session_id"])
        if ending_terminal and (
            e["transition"] != "release_" + ending_terminal[0]
            or e["ended_reason"] != ending_terminal[1]
            or e["sample_id"] != ending_terminal[2]
            or e["subject_state_after"] != ending_terminal[0]
            or receipt_endings.get(e.get("sample_id")) != e.get("ending_id")
        ):
            issues.add("ENDING_MISMATCH")
        if e["ended_reason"] == "anomaly_detected" and e.get(
            "anomaly_key"
        ) != "release-anomaly:" + str(release.observation_id):
            issues.add("ENDING_MISMATCH")
        if e["ended_reason"] != "anomaly_detected" and e.get("anomaly_key") is not None:
            issues.add("ENDING_MISMATCH")
        if reason in (
            "cancelled",
            "deadline_expired",
            "healthy_confirmed",
            "anomaly_detected",
            "max_samples_exhausted",
        ):
            # A plausible terminal edge must be backed by the event that
            # actually ended this subject, not just a matching session marker.
            if (
                s is None
                or not isinstance(recorded_at, datetime)
                or not isinstance(s.get("authorized_at"), datetime)
                or recorded_at < s["authorized_at"]
                or release.status != after
                or status != after
                or release.ended_reason != reason
                or terminal_reason != reason
                or release.ended_at is None
            ):
                issues.add("ENDING_MISMATCH")
                continue
            if reason in ("cancelled", "deadline_expired"):
                applied = [
                    a
                    for a in references
                    if a.get("action")
                    == ("cancel" if reason == "cancelled" else reason)
                    and a.get("outcome") == "applied"
                    and a.get("observation_id") == release.observation_id
                    and a.get("ending_id") == e.get("ending_id")
                    # Cancel references the old session through ending_id;
                    # its session_id is reserved for new authorizations.
                    and a.get("session_id")
                    == (s["session_id"] if reason == "deadline_expired" else None)
                    and a.get("status_before") == before
                    and a.get("status_after") == after
                    and a.get("control_generation_before")
                    == s.get("subject_control_generation")
                    and a.get("observation_generation_before")
                    == s.get("observation_generation")
                    and a.get("control_generation_after") == release.control_generation
                    and a.get("observation_generation_after")
                    == release.observation_generation
                ]
                if (
                    len(applied) != 1
                    or e.get("sample_id") is not None
                    or release.ended_at != recorded_at
                    or not recorded_at
                    <= applied[0]["recorded_at"]
                    <= release.updated_at
                    or (
                        reason == "deadline_expired"
                        and recorded_at < release.deadline_at
                    )
                ):
                    issues.add("ENDING_MISMATCH")
            elif (
                terminal_sample is None
                or terminal_sample.get("session_id") != s["session_id"]
                or terminal_sample.get("sample_id") != e.get("sample_id")
                or release.ended_at != terminal_sample.get("decision_at")
                or not release.ended_at <= recorded_at <= release.updated_at
            ):
                issues.add("ENDING_MISMATCH")
    if (
        release.control_generation != control_generation
        or release.observation_generation != observation_generation
        or release.paused != paused
    ):
        issues.add("WATERMARK_MISMATCH")
    if used != release.used_sample_count:
        issues.add("BUDGET_MISMATCH")
    if status != release.status:
        issues.add("STATUS_MISMATCH")
    if release.ended_reason != terminal_reason:
        issues.add("STATUS_MISMATCH")
    if terminal_reason is None:
        if release.ended_at is not None:
            issues.add("STATUS_MISMATCH")
    elif release.ended_at is None:
        issues.add("ENDING_MISMATCH")
    elif terminal_sample is not None:
        if release.ended_at != terminal_sample["decision_at"]:
            issues.add("ENDING_MISMATCH")
        terminal_endings = [
            e for e in endings if e.get("sample_id") == terminal_sample["sample_id"]
        ]
        if (
            len(terminal_endings) != 1
            or not isinstance(terminal_endings[0].get("recorded_at"), datetime)
            or not (
                release.ended_at
                <= terminal_endings[0]["recorded_at"]
                <= release.updated_at
            )
        ):
            issues.add("ENDING_MISMATCH")
    elif terminal_audit is not None:
        if not (
            terminal_time_floor
            <= release.ended_at
            <= min(terminal_audit["recorded_at"], release.updated_at)
        ) or (
            terminal_reason == "deadline_expired"
            and release.ended_at < release.deadline_at
        ):
            issues.add("ENDING_MISMATCH")
        if terminal_audit.get("ending_id") is not None:
            terminal_ending = next(
                (e for e in endings if e["ending_id"] == terminal_audit["ending_id"]),
                None,
            )
            if terminal_ending is None or (
                terminal_ending.get("ended_reason") != terminal_reason
                or terminal_ending.get("subject_state_after") != status
                or release.ended_at != terminal_ending.get("recorded_at")
                or not release.ended_at
                <= terminal_ending["recorded_at"]
                <= min(terminal_audit["recorded_at"], release.updated_at)
            ):
                issues.add("ENDING_MISMATCH")
        elif release.ended_at != terminal_audit["recorded_at"]:
            issues.add("ENDING_MISMATCH")
    elif not (late_delivery and release.ended_at == release.received_at):
        issues.add("ENDING_MISMATCH")
    if "comparisons" in history:
        issues |= _comparison_issues(
            release,
            history["comparisons"],
            profile,
            policy,
            by_session,
            samples,
            endings,
        )
    if "link" in history:
        issues |= _link_issues(release, history["link"], endings)
    verdict: Literal["healthy", "degraded", "unknown"] = "unknown"
    if not issues:
        if status == "healthy":
            verdict = "healthy"
        elif status == "anomalous":
            verdict = "degraded"
    return ReleaseReplay(
        observation_id=release.observation_id,
        consistent=not issues,
        stored_status=release.status,
        replayed_status=status,
        verdict=verdict,
        used_sample_count=used,
        integrity=tuple(sorted(issues)),
        sample_evaluations=tuple(evaluations),
    )


def _comparison_issues(
    release: ReleaseRecord,
    comparisons: Any,
    profile: HealthProfile | None,
    policy: ReleasePolicy | None,
    sessions: Mapping[UUID, Any],
    samples: Any,
    endings: Any,
) -> set[str]:
    """PR-4a S8: a stored comparison is recomputed from its stored raw
    before-window evidence and the anomaly sample it is bound to."""
    from opspilot.observation.release_comparison import (
        ReleaseComparisonEvidence,
        compare_evidence,
        comparison_fingerprint,
        comparison_id_for,
        iter_comparison_queries,
    )

    if not comparisons:
        return set()
    if len(comparisons) != 1 or release.status != "anomalous":
        return {"COMPARISON_MISMATCH"}
    c = comparisons[0]
    try:
        ending = next(
            e
            for e in endings
            if e["ending_id"] == c["ending_id"] and e.get("anomaly_key") is not None
        )
        sample = next(m for m in samples if m["sample_id"] == c["sample_id"])
        session = sessions[c["session_id"]]
        if (
            profile is None
            or policy is None
            or c["observation_id"] != release.observation_id
            or c["comparison_id"]
            != comparison_id_for(release.observation_id, c["ending_id"])
            or ending["sample_id"] != c["sample_id"]
            or ending["session_id"] != c["session_id"]
            or c["format_version"] != 1
        ):
            return {"COMPARISON_MISMATCH"}
        window_after = QueryWindow(
            start=sample["window_start"], end=sample["window_end"]
        )
        if (c["window_after_start"], c["window_after_end"]) != (
            window_after.start,
            window_after.end,
        ):
            return {"COMPARISON_MISMATCH"}
        if _cleaned(c["queries"]):
            # #244: the before-window bodies were cleaned; the comparison's
            # bindings checked above still hold, its numbers cannot be redone
            return {DATA_CLEANED}
        queries = list(iter_comparison_queries(c["queries"]))
        if any(q["comparison_id"] != c["comparison_id"] for q in c["queries"]):
            return {"COMPARISON_MISMATCH"}
        evidence = ReleaseComparisonEvidence(
            format_version=1,
            comparison_id=c["comparison_id"],
            attempts=c["attempts"],
            window=QueryWindow(
                start=c["window_before_start"], end=c["window_before_end"]
            ),
            queries=tuple(queries),
        )
        recomputed = compare_evidence(
            ReleaseEvaluationContext(
                release=release,
                session_id=session["session_id"],
                authorized_at=session["authorized_at"],
                policy=policy,
                health_profile=profile,
            ),
            observation_id=release.observation_id,
            ending_id=c["ending_id"],
            sample_id=c["sample_id"],
            window_after=window_after,
            anomaly=ReleaseEvaluation.model_validate_json(
                canonical_json(sample["release_evaluation"])
            ),
            evidence=evidence,
            decision_at=c["decision_at"],
        )
    except (StopIteration, KeyError, TypeError, ValueError, ValidationError):
        return {"COMPARISON_MISMATCH"}
    if (
        recomputed.model_dump(mode="json") != c["comparison"]
        or recomputed.status != c["status"]
        or comparison_fingerprint(evidence) != c["evidence_sha256"]
        or not c["decision_at"] <= c["recorded_at"]
    ):
        return {"COMPARISON_MISMATCH"}
    return set()


_LINK_SHAPES = {
    # disposition -> (related set, run set, input set, handoff reason set)
    "created": (False, True, False, False),
    "attached": (False, False, True, False),
    "created_related": (True, True, False, False),
    "handoff_created": (False, False, False, True),
}


def _link_issues(release: ReleaseRecord, link: Any, endings: Any) -> set[str]:
    """R9: the consumption link agrees with the release's unique anomaly
    ending and with the incident the release names; a release that names an
    incident without a link, or a link on a release that names none or
    another, is a mismatch."""
    if link is None:
        return set() if release.incident_id is None else {"LINK_MISMATCH"}
    try:
        anomalies = [e for e in endings if e.get("anomaly_key") is not None]
        shape = _LINK_SHAPES[link["disposition"]]
        if (
            release.status != "anomalous"
            or len(anomalies) != 1
            or link["observation_id"] != release.observation_id
            or link["anomaly_key"] != "release-anomaly:" + str(release.observation_id)
            or link["ending_id"] != anomalies[0]["ending_id"]
            or link["anomaly_key"] != anomalies[0]["anomaly_key"]
            or anomalies[0].get("ended_reason") != "anomaly_detected"
            or link["incident_id"] is None
            or release.incident_id != link["incident_id"]
            or shape
            != (
                link["related_incident_id"] is not None,
                link["run_id"] is not None,
                link["input_id"] is not None,
                link["handoff_reason"] is not None,
            )
            or not anomalies[0]["recorded_at"] <= link["consumed_at"]
        ):
            return {"LINK_MISMATCH"}
    except (KeyError, TypeError, AttributeError):
        return {"LINK_MISMATCH"}
    return set()


def replay_release_history(history: Mapping[str, object]) -> ReleaseReplay:
    """Malformed release records fail closed; never fall back to incident replay."""
    try:
        return _replay_release_history(utc_values(dict(history)))
    except (
        ValidationError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        OverflowError,
    ):
        value = history.get("release")
        try:
            release = (
                value
                if isinstance(value, ReleaseRecord)
                else ReleaseRecord.model_validate_json(canonical_json(value))
            )
        except (ValidationError, ValueError, TypeError):
            return ReleaseReplay(
                observation_id=UUID(int=0),
                consistent=False,
                stored_status="unknown",
                replayed_status="unknown",
                verdict="unknown",
                used_sample_count=0,
                integrity=("SUBJECT_BINDING_MISMATCH",),
                sample_evaluations=(),
            )
        return ReleaseReplay(
            observation_id=release.observation_id,
            consistent=False,
            stored_status=release.status,
            replayed_status="pending",
            verdict="unknown",
            used_sample_count=0,
            integrity=("SAMPLE_DECISION_MISMATCH",),
            sample_evaluations=(),
        )
