"""#350 implementation checks: Observer claim order between recovery and release.

Recovery sessions sort by ``active_sample_due_at - sample_interval_seconds``,
release sessions by ``active_sample_due_at``; eligibility is unchanged. The
harness gives release sessions a 1 s interval and recovery sessions 15 s.
Due times sit in 1800 so other tests' due sessions in the shared database
sort after ours.
"""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest

from opspilot.observation.releases import ReleaseEvent
from opspilot.observation.store import ObservationStore
from opspilot.observer.health_profile import canonical_content
from opspilot.persistence import DurableStore
from scripts.m0.postgres_lab import DSN
from tests.integration.test_f11_pr2a_impl_postgres import harness  # noqa: F401
from tests.integration.test_m1_02_observation_store_postgres import (
    _authorize,
    _incident,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
T = datetime(1800, 1, 1, tzinfo=timezone.utc)
RECOVERY_INTERVAL = 15


@pytest.fixture
def world(harness):  # noqa: F811
    web, observer, first_oid, policy, _ = harness
    owner = DurableStore(DSN)
    controller = ObservationStore(DSN)
    incidents = []
    extra_releases = []
    template = web.get_release(first_oid)
    world_profile: list[str] = []
    state = {"first": True}

    def release(due):
        if state["first"]:
            oid = first_oid
            state["first"] = False
        else:
            oid = web.record_release(
                event=ReleaseEvent(
                    format_version=1,
                    source="claim-order",
                    source_release_id=uuid4().hex,
                    released_at=template.released_at,
                    before=template.before_revision,
                    after=template.after_revision,
                    auxiliary=template.auxiliary,
                ),
                target_id=template.target_id,
                identity=template.target_snapshot,
                health_profile_revision=template.health_profile_revision,
                health_profile=world_profile[0],
                min_tracking_seconds=template.min_tracking_seconds,
                actor="tester",
                raw_body=b"{}",
            ).observation_id
            extra_releases.append(oid)
        sid = web.authorize_release(
            oid,
            actor="controller",
            expected_control_generation=0,
            idempotency_key="claim-order",
            calibrated_policy_revision=policy.revision,
        ).session_id
        if not world_profile:
            world_profile.append(
                canonical_content(observer.release_context(sid).health_profile)
            )
        set_due(sid, due)
        return sid

    def recovery(due):
        incident, _, target = _incident(owner)
        incidents.append(incident)
        sid = _authorize(controller, incident, target)
        set_due(sid, due)
        return sid

    try:
        yield {"observer": observer, "release": release, "recovery": recovery}
    finally:
        for incident in incidents:
            controller.revoke_sessions(incident)
        # The harness cancels only its own release.
        for oid in extra_releases:
            row = web.get_release(oid)
            if row.status in ("pending", "observing"):
                web.control_release(
                    oid,
                    action="cancel",
                    actor="cleanup",
                    expected_control_generation=row.control_generation,
                    idempotency_key="cleanup",
                    calibrated_policy_revision=None,
                )
        controller.close()
        owner.close()


def set_due(sid, due):
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at=%s WHERE session_id=%s",
            (due, sid),
        )


def claimed(world, limit):
    return [
        lease.session_id
        for lease in world["observer"].claim_due_samples(uuid4(), limit=limit)
    ]


def test_same_due_claims_recovery_first_even_when_release_wins_tiebreak(world):
    rel = world["release"](T)
    rec = world["recovery"](T)
    # The old order fell back to session_id; make sure that tiebreak favours
    # the release so only the recovery lead can explain the result.
    # Bounded: both ids are random, so redrawing both wins with probability
    # 1/2 per attempt; redrawing only the recovery id would leave a release
    # id near the top of the range winning almost never (#412).
    for _ in range(20):
        if rec > rel:
            break
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        set_due(rel, later)
        set_due(rec, later)
        rel = world["release"](T)
        rec = world["recovery"](T)
    else:
        pytest.fail("no recovery session_id above the release in 20 attempts")
    assert claimed(world, 1) == [rec]


@pytest.mark.parametrize(
    ("release_earlier_by", "first"), [(10, "recovery"), (20, "release")]
)
def test_release_earlier_by_less_than_one_interval_still_follows_recovery(
    world, release_earlier_by, first
):
    rel = world["release"](T)
    rec = world["recovery"](T + timedelta(seconds=release_earlier_by))
    assert claimed(world, 1) == [rec if first == "recovery" else rel]


def test_release_is_claimed_while_recoveries_keep_arriving(world):
    # Each round a new recovery becomes due 5 s later, as when time advances.
    rel = world["release"](T)
    order = []
    for round_ in range(1, 6):
        world["recovery"](T + timedelta(seconds=5 * round_))
        order.extend(claimed(world, 1))
        if rel in order:
            break
    assert rel in order
    # Recoveries due more than one interval after the release follow it.
    assert len(order) <= RECOVERY_INTERVAL // 5 + 1


def test_recovery_due_in_the_future_is_not_claimed_even_inside_the_lead(world):
    rel = world["release"](T)
    future = world["recovery"](datetime.now(timezone.utc) + timedelta(seconds=5))
    leases = claimed(world, 5)
    assert rel in leases and future not in leases


@pytest.mark.parametrize("locked_count", [3, 20])
def test_locked_mixed_candidates_page_without_gaps_or_repeats(
    world, monkeypatch, locked_count
):
    observer = world["observer"]
    limit = 2
    # Interleave: effective due of recovery k is T + 10k - 15, release k T + 10k.
    sessions = []
    for k in range(locked_count + 4):
        if k % 2:
            sid = world["release"](T + timedelta(seconds=10 * k))
            sessions.append((10 * k, sid, "release"))
        else:
            sid = world["recovery"](T + timedelta(seconds=10 * k))
            sessions.append((10 * k - RECOVERY_INTERVAL, sid, "recovery"))
    sessions.sort()
    ordered = [sid for _, sid, _ in sessions]
    locked, free = ordered[:locked_count], ordered[locked_count:]
    probes = []
    incident_claim = observer._claim_incident_due_samples
    release_claim = observer._claim_release_due

    def claim_incident(*args, **kwargs):
        probes.append(kwargs["selected_session_id"])
        return incident_claim(*args, **kwargs)

    def claim_release(owner, sid, lease_seconds):
        probes.append(sid)
        return release_claim(owner, sid, lease_seconds)

    monkeypatch.setattr(observer, "_claim_incident_due_samples", claim_incident)
    monkeypatch.setattr(observer, "_claim_release_due", claim_release)
    with psycopg.connect(DSN) as blocker:
        kinds = {sid: kind for _, sid, kind in sessions}
        rel_locked = [s for s in locked if kinds[s] == "release"]
        rec_locked = [s for s in locked if kinds[s] == "recovery"]
        # One release is held by its observation row, one recovery by its
        # incident row; the rest by their session rows.
        blocker.execute(
            "SELECT 1 FROM opspilot_release_observations WHERE observation_id=(SELECT release_observation_id FROM opspilot_observation_sessions WHERE session_id=%s) FOR UPDATE",
            (rel_locked[0],),
        )
        blocker.execute(
            "SELECT 1 FROM opspilot_incidents WHERE incident_id=(SELECT incident_id FROM opspilot_observation_sessions WHERE session_id=%s) FOR UPDATE",
            (rec_locked[0],),
        )
        blocker.execute(
            "SELECT 1 FROM opspilot_observation_sessions WHERE session_id=ANY(%s) FOR UPDATE",
            (rel_locked[1:] + rec_locked[1:],),
        )
        leases = claimed(world, limit)
    assert len(probes) <= 8 * limit
    assert len(probes) == len(set(probes))
    assert probes == ordered[: len(probes)]
    if locked_count == 3:
        assert leases == free[:limit]
    else:
        assert leases == []
        assert len(probes) == 8 * limit
