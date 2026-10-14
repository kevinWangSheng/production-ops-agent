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
    status: Any = "unknown" if release.ended_reason == "late_delivery" else "pending"
    used = 0
    control_generation = observation_generation = 0
    paused = False
    audit_sequence = 0
    evaluations = []
    ended_samples: dict[UUID, tuple[str, str, UUID]] = {}
    for _, _, kind, item in events:
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
            elif (
                a["control_generation_after"] != control_generation
                or a["observation_generation_after"] != observation_generation
                or a["paused_after"] != paused
                or a["status_after"] != status
            ):
                issues.add("SAMPLE_DECISION_MISMATCH")
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
                data = {k: v for k, v in q.items() if k != "sample_id"}
                raw = data["raw"]
                if not isinstance(raw, bytes):
                    raw = bytes(raw)
                data["raw"] = raw
                if sha256(raw) != data["raw_sha256"]:
                    issues.add("RAW_SHA256_MISMATCH")
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
            if not consistent:
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
            from opspilot.observation.release_samples import sample_fingerprint

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
        reason = "adopted"
        if m["current_subject_control_generation"] != m["subject_control_generation"]:
            reason = "control_generation_stale"
        elif m["current_subject_observation_generation"] != m["observation_generation"]:
            reason = "observation_generation_stale"
        elif m["current_subject_state"] != "observing":
            reason = "subject_state_not_adoptable"
        elif (
            m["scope_suspended"]
            or m["global_generation"] != s["authorized_global_generation"]
            or m["target_generation"] != s["authorized_target_generation"]
        ):
            reason = "suspended"
        elif m["decision_at"] >= release.deadline_at:
            reason = "deadline_expired"
        elif not m["lease_valid"]:
            reason = "lease_revoked"
        elif not m["lease_stamps_match"]:
            reason = "lease_stamp_mismatch"
        elif m["sequence"] <= fold["adopted_sequence"]:
            reason = "sequence_not_advancing"
        elif (
            fold["adopted_window_end"] is not None
            and m["window_end"] < fold["adopted_window_end"]
        ):
            reason = "window_regressed"
        elif not consistent:
            reason = "readings_inconsistent"
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
        if reason == "adopted" and prior_end is not None:
            reason = "session_not_authorized"
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
        try:
            receipt = ReleaseSampleReceipt.model_validate_json(
                canonical_json(
                    {k: v for k, v in m["release_receipt"].items() if k != "_lease"}
                )
            )
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
    for sid, terminal_sample in ended_samples.items():
        matches = [
            e
            for e in endings
            if e["session_id"] == sid and e.get("sample_id") == terminal_sample[2]
        ]
        if len(matches) != 1:
            issues.add("ENDING_MISMATCH")
    for e in endings:
        if (
            e.get("ending_id")
            != uuid5(
                release.observation_id, "session-ending:" + str(e.get("session_id"))
            )
            or e.get("purpose") != "release_observation"
            or e.get("subject_id") != release.observation_id
            or e.get("release_observation_id") != release.observation_id
        ):
            issues.add("ENDING_MISMATCH")
        s = by_session.get(e["session_id"])
        if s is None or s["ended_reason"] != e["ended_reason"]:
            issues.add("ENDING_MISMATCH")
        else:
            expected_state = (
                "completed"
                if e["ended_reason"] in ("healthy_confirmed", "anomaly_detected")
                else "expired"
                if e["ended_reason"] in ("deadline_expired", "max_samples_exhausted")
                else "revoked"
            )
            if s["state"] != expected_state:
                issues.add("ENDING_MISMATCH")
        if (
            e["ended_reason"] in ("healthy_confirmed", "anomaly_detected")
            and e["session_id"] not in ended_samples
        ):
            issues.add("ENDING_MISMATCH")
        ending_terminal = ended_samples.get(e["session_id"])
        if ending_terminal and (
            e["transition"] != "release_" + ending_terminal[0]
            or e["ended_reason"] != ending_terminal[1]
            or e["sample_id"] != ending_terminal[2]
            or e["subject_state_after"] != ending_terminal[0]
        ):
            issues.add("ENDING_MISMATCH")
        if e["ended_reason"] == "anomaly_detected" and e.get(
            "anomaly_key"
        ) != "release-anomaly:" + str(release.observation_id):
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
