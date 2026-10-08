"""Offline replay against real stored observations on PostgreSQL (M1-02
step 5, #87; F6 step 5).

The Observer loop samples a scripted Prometheus (the stand-in of the step 4
suite) for the four F6 outcomes -- recovery, withdrawn traffic, a missing
required signal, continued degradation -- then the replay recomputes every
verdict from the stored rows only: it opens no telemetry connection, the
session's frozen profile content comes from ``opspilot_health_profiles``,
and the result agrees with what the store decided. Tampering with a stored
verdict, a raw bundle or the profile content is reported as an integrity
mismatch with an ``unknown`` verdict. The CLI runs under the Observer login
(read-only on these tables) and its exit code follows the verdict.
"""

# ruff: noqa: F811 - the step 4 suite's fixtures are imported by name and
# then named again as test parameters, which is how pytest binds them

import io
import json
import os

import pytest

from opspilot.observation import ObservationStore
from opspilot.observer.replay import (
    INTEGRITY_MISMATCH,
    PROFILE_SENTINEL,
    main,
    replay_stored_session,
)
from opspilot.persistence import DurableStore
from opspilot.persistence.base import PersistenceError
from tests.integration.test_m1_02_observer_postgres import (  # noqa: F401 - fixtures
    SHIPPED,
    Telemetry,
    _authorize,
    _backdate_authorization,
    _due_now,
    _incident,
    _lifecycle,
    _only,
    controller,
    loop,
    observer,
    observer_dsn,
    owner,
    scratch_dsn,
    telemetry,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _run(loop_state, owner_store, session, times):
    observer_loop, _ = loop_state
    receipts = []
    for index in range(times):
        if index:
            _due_now(owner_store, session)
        receipts.append(_only(observer_loop.poll_once(), session))
    return receipts


def test_recovery_replays_consistently_from_stored_rows(
    loop: tuple, owner: DurableStore, controller: ObservationStore, observer_dsn: str
) -> None:
    observer_loop, state = loop
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)
    (receipt,) = _run(loop, owner, session, 1)
    assert receipt.transition == "recovery_confirmed"
    requests_before = len(state.requests)

    result = replay_stored_session(controller, session)

    assert result.consistent, result.integrity
    assert result.recovery_verdict == "healthy" and result.recovery_confirmed
    assert result.expected_lifecycle == result.recorded_lifecycle == "resolved"
    assert result.health_profile_revision == SHIPPED.revision
    assert {r.signal_name for r in result.samples[0].readings} == {
        s.name for s in SHIPPED.signals
    }
    assert all(r.raw_verified for r in result.samples[0].readings)
    assert all(r.stored == r.replayed for r in result.samples[0].readings)
    # nothing was queried: the replay reads rows, never the source
    assert len(state.requests) == requests_before
    assert result.external_queries == () and result.model_requests == ()

    out = io.StringIO()
    code = main(["--dsn", observer_dsn, "--session", str(session)], stdout=out)
    assert code == 0
    document = json.loads(out.getvalue())
    assert document[0]["consistent"] and document[0]["recovery_verdict"] == "healthy"
    assert len(state.requests) == requests_before


@pytest.mark.parametrize(
    "scenario,outcome,verdict",
    [
        ("no-traffic", "no_data", "unknown"),
        ("missing", "no_data", "unknown"),
        ("degraded", "degraded", "degraded"),
    ],
)
def test_unknown_and_degraded_outcomes_replay_consistently(
    loop: tuple,
    owner: DurableStore,
    controller: ObservationStore,
    scenario: str,
    outcome: str,
    verdict: str,
) -> None:
    observer_loop, state = loop
    if scenario == "no-traffic":
        state.values["request_rate_per_second"] = 0.0
    elif scenario == "missing":
        state.values["deployment_ready_replicas"] = None
    else:
        state.values["error_ratio"] = 0.5
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1, max_samples=2)
    _backdate_authorization(owner, session)
    receipts = _run(loop, owner, session, 2)
    assert all(r.accepted and not r.confirms_health for r in receipts)
    assert receipts[-1].transition == "observation_ended_unconfirmed"
    assert _lifecycle(owner, incident) == "open"

    result = replay_stored_session(controller, session)

    assert result.consistent, result.integrity
    assert [s.replayed_outcome for s in result.samples] == [outcome, outcome]
    assert [s.stored_outcome for s in result.samples] == [outcome, outcome]
    assert result.recovery_verdict == verdict and not result.recovery_confirmed
    assert result.healthy_window_seconds == 0
    assert result.expected_lifecycle == result.recorded_lifecycle == "open"
    assert result.stored_session_state == result.replayed_session_state == "expired"
    if scenario == "missing":
        reading = next(
            r
            for r in result.samples[0].readings
            if r.signal_name == "deployment_ready_replicas"
        )
        assert reading.replayed is not None and reading.replayed[0] == "no_data"
        assert reading.verdict == "no_data"


def test_tampered_verdict_bundle_or_profile_is_an_integrity_mismatch(
    loop: tuple, owner: DurableStore, controller: ObservationStore, observer_dsn: str
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)
    (receipt,) = _run(loop, owner, session, 1)
    assert receipt.transition == "recovery_confirmed"
    assert replay_stored_session(controller, session).consistent

    # 1. only the stored judgment is altered; readings and bundles stay
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_samples SET outcome='degraded' WHERE sample_id=%s",
            (receipt.sample_id,),
        )
    result = replay_stored_session(controller, session)
    assert not result.consistent and INTEGRITY_MISMATCH in result.integrity
    assert result.recovery_verdict == "unknown" and not result.recovery_confirmed
    assert result.expected_lifecycle is None
    assert result.recomputed_verdict == "healthy"
    assert result.samples[0].stored_outcome == "degraded"
    assert result.samples[0].replayed_outcome == "healthy"
    out = io.StringIO()
    assert main(["--dsn", observer_dsn, "--session", str(session)], stdout=out) == 1
    assert json.loads(out.getvalue())[0]["recovery_verdict"] == "unknown"
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_samples SET outcome='healthy' WHERE sample_id=%s",
            (receipt.sample_id,),
        )
    assert replay_stored_session(controller, session).consistent

    # 2. a bundle no longer hashing to its row
    with owner.transaction() as conn:
        kept = conn.execute(
            "SELECT raw FROM opspilot_observation_signal_readings WHERE sample_id=%s AND signal_name='error_ratio'",
            (receipt.sample_id,),
        ).fetchone()
        assert kept is not None
        conn.execute(
            "UPDATE opspilot_observation_signal_readings SET raw=%s WHERE sample_id=%s AND signal_name='error_ratio'",
            (b"{}", receipt.sample_id),
        )
    result = replay_stored_session(controller, session)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "RAW_HASH_MISMATCH:error_ratio" in result.samples[0].integrity
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_signal_readings SET raw=%s WHERE sample_id=%s AND signal_name='error_ratio'",
            (bytes(kept["raw"]), receipt.sample_id),
        )
    assert replay_stored_session(controller, session).consistent

    # 3. the frozen profile content behind the revision was edited
    with owner.transaction() as conn:
        original = conn.execute(
            "SELECT content FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (SHIPPED.revision,),
        ).fetchone()
        assert original is not None
        conn.execute(
            "UPDATE opspilot_health_profiles SET content=%s WHERE health_profile_revision=%s",
            (original["content"] + " ", SHIPPED.revision),
        )
    try:
        result = replay_stored_session(controller, session)
        assert not result.consistent
        assert "HEALTH_PROFILE_UNREADABLE" in result.integrity
        assert result.recovery_verdict == "unknown"
        assert all(s.replayed_outcome is None for s in result.samples)
    finally:
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_health_profiles SET content=%s WHERE health_profile_revision=%s",
                (original["content"], SHIPPED.revision),
            )


def test_deleted_rows_and_rewritten_watermarks_are_integrity_mismatches(
    loop: tuple, owner: DurableStore, controller: ObservationStore
) -> None:
    """Codex review of PR #139, P2-1/P2-2/P2-3 on real rows: a reading row
    deleted with the verdict rewritten as not healthy, a sample row deleted,
    a session watermark rewritten, or a reading's query rewritten -- each
    is reported, none is repeated as the verdict."""
    observer_loop, state = loop
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1, max_samples=3)
    _backdate_authorization(owner, session)
    (receipt,) = _run(loop, owner, session, 1)
    assert receipt.transition == "recovery_confirmed"
    assert replay_stored_session(controller, session).consistent
    first = receipt.sample_id

    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_signal_readings SET query='up' WHERE sample_id=%s AND signal_name='error_ratio'",
            (first,),
        )
    result = replay_stored_session(controller, session)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert any(
        c.startswith("READING_BASIS_MISMATCH:error_ratio")
        for c in result.samples[0].integrity
    )

    # a rewritten watermark (the streak start the fold recomputes)
    with owner.transaction() as conn:
        kept = conn.execute(
            "SELECT healthy_since FROM opspilot_observation_sessions WHERE session_id=%s",
            (session,),
        ).fetchone()
        assert kept is not None and kept["healthy_since"] is not None
        conn.execute(
            "UPDATE opspilot_observation_sessions SET healthy_since=NULL WHERE session_id=%s",
            (session,),
        )
    result = replay_stored_session(controller, session)
    assert "WATERMARK_MISMATCH" in result.integrity
    assert result.recovery_verdict == "unknown"
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET healthy_since=%s WHERE session_id=%s",
            (kept["healthy_since"], session),
        )

    with owner.transaction() as conn:
        conn.execute(
            "DELETE FROM opspilot_observation_signal_readings WHERE sample_id=%s AND signal_name='error_ratio'",
            (first,),
        )
        conn.execute(
            "UPDATE opspilot_observation_samples SET outcome='no_data',required_signals_present=false,confirms_health=false,health_basis='outcome_not_healthy' WHERE sample_id=%s",
            (first,),
        )
    result = replay_stored_session(controller, session)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "READING_MISSING:error_ratio" in result.samples[0].integrity


def test_a_transient_profile_read_failure_is_filed_with_its_reason_and_replays(
    loop: tuple, owner: DurableStore, controller: ObservationStore, monkeypatch
) -> None:
    """Codex recheck of PR #139, P2-4: one poll cannot read the profile row
    (store error) and files ``failed`` with a sentinel reading; the next
    poll confirms recovery; the replay accepts the recorded reason."""
    observer_loop, state = loop
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1, max_samples=3)
    _backdate_authorization(owner, session)
    original = observer_loop.store.health_profile

    def unavailable(revision):
        raise PersistenceError("STORAGE_UNAVAILABLE")

    monkeypatch.setattr(observer_loop.store, "health_profile", unavailable)
    (first,) = _run(loop, owner, session, 1)
    assert first.accepted and not first.confirms_health
    monkeypatch.setattr(observer_loop.store, "health_profile", original)
    _due_now(owner, session)
    (second,) = _run(loop, owner, session, 1)
    assert second.transition == "recovery_confirmed"

    result = replay_stored_session(controller, session)
    assert result.consistent, result.integrity
    assert result.recovery_verdict == "healthy"
    assert result.samples[0].stored_outcome == "failed"
    assert result.samples[0].recompute_skipped == "HEALTH_PROFILE_UNAVAILABLE"
    (sentinel,) = result.samples[0].readings
    assert sentinel.signal_name == PROFILE_SENTINEL and sentinel.raw_verified
    with owner.transaction() as conn:
        conn.execute(
            "DELETE FROM opspilot_observation_signal_readings WHERE sample_id=%s",
            (first.sample_id,),
        )
    result = replay_stored_session(controller, session)
    assert not result.consistent and "NO_READINGS" in result.samples[0].integrity
