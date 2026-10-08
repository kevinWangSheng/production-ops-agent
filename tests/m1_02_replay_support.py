"""Stored observation histories built from real Observer samples (M1-02 step 5).

The replay tests need rows shaped like ``ObservationStore.session_history``
returns them -- a session row, its profile row, sample rows carrying the
decision the store took and the conditions it took it under, reading rows
with their raw bundles -- without a database. ``take_sample`` against the
unit fake source produces genuine readings and bundles; ``stored_history``
files them the way the store would have (adopted, healthy streak, ending),
so a replay that recomputes everything from the bundles must agree with
what is "stored" unless a test tampers with it afterwards.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from opspilot.domain.observation import HealthSample
from opspilot.observation.store import SignalReading as StoredReading
from opspilot.observation.store import profile_revision
from opspilot.observer.health_profile import HealthProfile, canonical_content
from opspilot.observer.sampler import take_sample
from tests.test_m1_observer import (
    PROFILE,
    FakeSource,
    FakeStore,
    healthy_answers,
    lease,
    ok,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
TARGET = {
    "integration_id": "test-integration",
    "cluster_uid": "test-cluster",
    "namespace": "ns",
    "resource_uid": "deployment/svc",
    "revision": "svc:v2",
}


def answers(kind: str = "healthy", *, latest: datetime | None = None) -> dict:
    """Instant answers for the unit profile's six expressions."""
    base = healthy_answers(latest=latest)
    if kind == "degraded":
        base["e{namespace='ns',service='svc'} / t{namespace='ns',service='svc'}"] = ok(
            0.5
        )
    elif kind == "no-traffic":
        base["sum(rate(x{namespace='ns',service='svc'}[5m]))"] = ok(0.0)
    return base


def take(
    kind: str,
    *,
    sequence: int,
    window_end: datetime,
    session_id: UUID,
    profile: HealthProfile = PROFILE,
) -> tuple[HealthSample, list[StoredReading]]:
    """One genuine sample (readings with raw bundles) judged at ``window_end``."""
    store = FakeStore(clock=[window_end, window_end + timedelta(seconds=1)])
    sample_lease = replace(
        lease(profile.revision),
        session_id=session_id,
        sequence=sequence,
        lease_until=window_end + timedelta(seconds=120),
    )
    source = FakeSource(answers(kind, latest=window_end - timedelta(seconds=30)))
    take_sample(sample_lease, profile, source, store)
    ((_, sample, readings),) = store.submitted
    return sample, readings


def stored_history(
    taken: Sequence[tuple[HealthSample, Sequence[StoredReading]]],
    *,
    profile: HealthProfile | None = PROFILE,
    session_id: UUID,
    incident_id: UUID | None = None,
    authorized_at: datetime,
    max_samples: int = 20,
    sustained_window_seconds: int = 600,
) -> dict[str, Any]:
    """File the samples as the store would have: every one adopted, the
    healthy streak measured from the first healthy window start, the
    session completed on confirmation or expired on the last budgeted
    sample, with the matching ending record."""
    incident_id = incident_id or uuid4()
    content = None if profile is None else canonical_content(profile)
    revision = (
        None if profile is None else profile_revision(profile.profile_id, content or "")
    )
    samples: list[dict[str, Any]] = []
    endings: list[dict[str, Any]] = []
    healthy_since: datetime | None = None
    state, ended_reason = "authorized", None
    lifecycle = "observing_recovery"
    adopted_count = 0
    for sample, readings in taken:
        adopted_count += 1
        healthy = (
            revision is not None
            and sample.outcome == "healthy"
            and sample.required_signals_present
            and sample.window.start >= authorized_at
        )
        if revision is None:
            basis = "no_health_profile"
        elif sample.outcome != "healthy":
            basis = "outcome_not_healthy"
        elif not sample.required_signals_present:
            basis = "required_signals_missing"
        elif sample.window.start < authorized_at:
            basis = "window_before_authorization"
        else:
            basis = "confirmed"
        healthy_since = None if not healthy else (healthy_since or sample.window.start)
        transition = None
        before = lifecycle
        if (
            healthy
            and healthy_since is not None
            and (sample.window.end - healthy_since).total_seconds()
            >= sustained_window_seconds
        ):
            transition, state, ended_reason = (
                "recovery_confirmed",
                "completed",
                "recovery_confirmed",
            )
            lifecycle = "resolved"
        elif adopted_count >= max_samples:
            transition, state, ended_reason = (
                "observation_ended_unconfirmed",
                "expired",
                "max_samples_exhausted",
            )
            lifecycle = "open"
        sample_id = UUID(sample.sample_id)
        samples.append(
            {
                "sample_id": sample_id,
                "session_id": session_id,
                "job_id": uuid4(),
                "sequence": sample.sequence,
                "epoch": 1,
                "window_start": sample.window.start,
                "window_end": sample.window.end,
                "outcome": sample.outcome,
                "required_signals_present": sample.required_signals_present,
                "subject_control_generation": sample.subject_control_generation,
                "observation_generation": sample.observation_generation,
                "health_profile_revision": revision,
                "disposition": "adopted",
                "reason": "adopted",
                "confirms_health": healthy,
                "health_basis": basis,
                "subject_lifecycle": before,
                "incident_control_generation": sample.subject_control_generation,
                "incident_observation_generation": sample.observation_generation,
                "scope_suspended": False,
                "global_generation": 0,
                "target_generation": 0,
                "within_deadline": True,
                "lease_valid": True,
                "lease_stamps_match": True,
                "readings_consistent": True,
                "transition": transition,
                "submitted_at": sample.window.end + timedelta(seconds=2),
                "readings": [
                    {
                        "sample_id": sample_id,
                        "signal_name": r.signal_name,
                        "status": r.status,
                        "value": r.value,
                        "sample_count": r.sample_count,
                        "query": r.query,
                        "window_start": r.window_start,
                        "window_end": r.window_end,
                        "source": r.source,
                        "raw_sha256": r.raw_sha256,
                        "raw": r.raw,
                    }
                    for r in readings
                ],
            }
        )
        if ended_reason is not None:
            endings.append(
                {
                    "ending_id": uuid4(),
                    "session_id": session_id,
                    "incident_id": incident_id,
                    "ended_reason": ended_reason,
                    "transition": transition,
                    "sample_id": sample_id,
                    "lifecycle_before": before,
                    "lifecycle_after": lifecycle,
                    "recorded_at": sample.window.end + timedelta(seconds=2),
                }
            )
            break
    # the session row reflects the samples actually filed (the history
    # stops at the ending), not everything that was taken
    filed = taken[: len(samples)]
    last_end = filed[-1][0].window.end if filed else authorized_at
    session = {
        "session_id": session_id,
        "incident_id": incident_id,
        "purpose": "incident_recovery",
        "target_id": uuid4(),
        "target": dict(TARGET),
        "subject_control_generation": taken[0][0].subject_control_generation
        if taken
        else 0,
        "observation_generation": taken[0][0].observation_generation if taken else 1,
        "authorized": True,
        "authorized_by": "operator",
        "authorized_at": authorized_at,
        "state": state,
        "ended_reason": ended_reason,
        "authorized_global_generation": 0,
        "authorized_target_generation": 0,
        "health_profile_revision": revision,
        "deadline_at": authorized_at + timedelta(hours=1),
        "max_samples": max_samples,
        "sample_interval_seconds": 60,
        "sustained_window_seconds": sustained_window_seconds,
        "adopted_sequence": filed[-1][0].sequence if filed else 0,
        "adopted_window_end": last_end if filed else None,
        "adopted_count": len(samples),
        "healthy_since": healthy_since,
        "issued_sequence": len(samples),
        "active_sample_job_id": None,
        "active_sample_sequence": None,
        "active_sample_due_at": None if state != "authorized" else last_end,
        "active_sample_owner": None,
        "active_sample_epoch": None,
        "active_sample_lease_until": None,
        "created_at": authorized_at,
        "updated_at": last_end,
    }
    return {
        "session": session,
        "incident_lifecycle": lifecycle,
        "health_profile": None
        if content is None
        else {
            "health_profile_revision": revision,
            "profile_id": profile.profile_id if profile else None,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "content": content,
            "created_at": authorized_at,
        },
        "samples": samples,
        "endings": endings,
    }


def healthy_history(
    count: int = 11,
    *,
    session_id: UUID | None = None,
    profile: HealthProfile = PROFILE,
    sustained_window_seconds: int = 600,
) -> dict[str, Any]:
    """Up to ``count`` contiguous healthy samples a minute apart; the unit
    profile's 300 s window makes the sixth one span 600 s since the first
    window start (NOW-300 .. NOW+300) and confirm recovery, where the
    history ends as the store's would."""
    session_id = session_id or uuid4()
    taken = [
        take(
            "healthy",
            sequence=index + 1,
            window_end=NOW + timedelta(seconds=60 * index),
            session_id=session_id,
            profile=profile,
        )
        for index in range(count)
    ]
    return stored_history(
        taken,
        profile=profile,
        session_id=session_id,
        authorized_at=NOW - timedelta(seconds=600),
        sustained_window_seconds=sustained_window_seconds,
    )


def rehash(reading: Mapping[str, Any], raw: bytes) -> dict[str, Any]:
    """A reading row rewritten with another bundle and a hash that matches it
    (what the store would hold had the Observer stored that bundle)."""
    return {**reading, "raw": raw, "raw_sha256": hashlib.sha256(raw).hexdigest()}


def with_sample_time(reading: Mapping[str, Any], at: datetime) -> dict[str, Any]:
    bundle = json.loads(bytes(reading["raw"]))
    bundle["sample_time"] = at.isoformat()
    return rehash(
        reading, json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    )
