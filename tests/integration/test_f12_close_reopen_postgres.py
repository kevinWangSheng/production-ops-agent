"""F12 PR-2 implementation integration checks; no model calls."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from opspilot.knowledge.jobs import GenerationStore
from opspilot.observation.store import ObservationStore
from opspilot.persistence import PersistenceError
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_02_observation_store_postgres import (
    _authorize,
    _incident,
)
from tests.integration.test_m1_incident_control_postgres import _rows, _submit
from tests.integration.test_m1_web_postgres import _build
from tests.m1_web_support import basic, call, post_form, same_origin

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


@pytest.fixture
def env():
    app, workbench, store = _build()
    subject = UUID(_submit(app, "checkout-prod"))
    return app, workbench, store, subject


def _set(store, subject, lifecycle, state="queued", mode="automatic"):
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle=%s,state=%s,mode=%s WHERE incident_id=%s",
            (lifecycle, state, mode, subject),
        )
        conn.execute(
            "UPDATE opspilot_runs SET state=%s WHERE incident_id=%s", (state, subject)
        )


@pytest.mark.parametrize("lifecycle", ["open", "observing_recovery", "resolved"])
@pytest.mark.parametrize(
    "state", ["queued", "paused", "running", "waiting_human", "blocked", "completed"]
)
@pytest.mark.parametrize("mode", ["automatic", "human_owned"])
def test_close_legal_starts_and_run_mirror(env, lifecycle, state, mode):
    _, _, store, subject = env
    _set(store, subject, lifecycle, state, mode)
    before, _ = _rows(store, subject)
    assert store.control(subject, 0, "close", "operator", {"note": "audit only"}) == 1
    inc, runs = _rows(store, subject)
    expected = "completed" if state == "completed" else "cancelled"
    assert (inc["lifecycle"], inc["state"], inc["mode"]) == ("closed", expected, mode)
    assert inc["conclusion"] == before["conclusion"]
    assert runs[0]["state"] == expected
    if state != "completed":
        assert runs[0]["owner"] is None and runs[0]["lease_until"] is None
        assert runs[0]["control_generation"] == 1
    with store.transaction() as conn:
        audit = conn.execute(
            "SELECT * FROM opspilot_controls WHERE incident_id=%s", (subject,)
        ).fetchone()
    assert audit["action"] == "close" and audit["payload"] == {"note": "audit only"}


@pytest.mark.parametrize(
    "action,lifecycle",
    [("close", "closed"), ("reopen", "open"), ("reopen", "observing_recovery")],
)
def test_illegal_and_conflict_precedence_leave_rows_unchanged(env, action, lifecycle):
    _, _, store, subject = env
    _set(store, subject, lifecycle)
    before = _rows(store, subject)
    for generation, error in [(1, "CONTROL_CONFLICT"), (0, "ILLEGAL_TRANSITION")]:
        with pytest.raises(PersistenceError, match=error):
            store.control(subject, generation, action, "operator")
        assert _rows(store, subject) == before


@pytest.mark.parametrize("lifecycle", ["resolved", "closed"])
@pytest.mark.parametrize("mode", ["automatic", "human_owned"])
def test_reopen_preserves_runs_and_mirrors(env, lifecycle, mode):
    _, _, store, subject = env
    _set(store, subject, lifecycle, "completed", mode)
    before, runs = _rows(store, subject)
    assert store.control(subject, 0, "reopen", "operator") == 1
    after, later = _rows(store, subject)
    assert later == runs
    assert after["lifecycle"] == "open"
    for key in (
        "state",
        "mode",
        "conclusion",
        "current_run_id",
        "observation_generation",
    ):
        assert after[key] == before[key]


def test_web_close_reopen_then_followup_starts_successor(env):
    app, workbench, store, subject = env
    old_run = _rows(store, subject)[0]["current_run_id"]
    for generation, action, extra in [
        (0, "close", {}),
        (1, "reopen", {}),
        (2, "follow_up", {"text": "Continue checking"}),
    ]:
        response = post_form(
            app,
            f"/incidents/{subject}/control",
            {
                "action": action,
                "expected_generation": str(generation),
                "idempotency_key": str(uuid4()),
                **extra,
            },
            headers={**basic(), **same_origin()},
        )
        assert response.status == 200, response.text
        assert (
            response.json().get("run_id") is None
            if action != "follow_up"
            else response.json().get("run_id") is not None
        )
        if action == "close":
            page = call(app, "GET", f"/incidents/{subject}", headers=basic()).text
            assert 'id="incident-lifecycle">closed' in page
            assert "Automatic work stopped" in page
    inc, runs = _rows(store, subject)
    assert len(runs) == 2 and inc["current_run_id"] != old_run
    assert next(r for r in runs if r["run_id"] == old_run)["state"] == "cancelled"


def test_close_revokes_observation_and_becomes_generation_candidate():
    _, _, store = _build()
    subject, _, target = _incident(store)
    observer = ObservationStore(DSN)
    session = _authorize(observer, subject, target)
    jobs = GenerationStore(DSN)
    assert subject not in jobs.candidates(limit=100000)
    assert store.control(subject, 0, "close", "operator") == 1
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM opspilot_observation_sessions WHERE session_id=%s",
            (session,),
        ).fetchone()
        ending = conn.execute(
            "SELECT * FROM opspilot_observation_endings WHERE session_id=%s", (session,)
        ).fetchone()
    assert row["state"] != "authorized" and row["ended_reason"] == "authority_revoked"
    assert (
        ending["lifecycle_before"] == ending["lifecycle_after"] == "observing_recovery"
    )
    # G5: close uses the normal observation-ending candidate path.
    assert subject in jobs.candidates(limit=100000)
    store.control(subject, 1, "reopen", "operator")
    assert all(s["state"] != "authorized" for s in observer.incident_sessions(subject))


@pytest.mark.parametrize("mode", ["automatic", "human_owned"])
def test_closed_refuses_new_work_after_generation_check(env, mode):
    _, _, store, subject = env
    _set(store, subject, "closed", "cancelled", mode)
    observer = ObservationStore(DSN)
    before = _rows(store, subject)
    for generation, error in [(1, "CONTROL_CONFLICT"), (0, "REOPEN_REQUIRED")]:
        with pytest.raises(PersistenceError, match=error):
            store.new_run(
                subject,
                uuid4(),
                expected_generation=generation,
                deadline=datetime.now(timezone.utc) + timedelta(hours=1),
                budget_limit=10,
                versions={"state": "v1"},
                actor="operator",
            )
        with pytest.raises(PersistenceError, match=error):
            observer.register_remediation(
                subject,
                expected_generation=generation,
                actor="operator",
                revision="v2",
                deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
                max_samples=10,
                sample_interval_seconds=15,
                sustained_window_seconds=60,
            )
    assert _rows(store, subject) == before


@pytest.mark.parametrize("action", ["close", "reopen"])
def test_concurrent_control_has_one_winner(env, action):
    _, _, store, subject = env

    if action == "reopen":
        _set(store, subject, "resolved")

    def decide(_):
        try:
            return store.control(subject, 0, action, "operator")
        except PersistenceError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, range(2)))
    assert sorted(map(str, results)) == ["1", "CONTROL_CONFLICT"]


@pytest.mark.parametrize(
    "action,lifecycle", [("close", "open"), ("reopen", "resolved")]
)
def test_lifecycle_controls_ignore_target_suspension(env, action, lifecycle):
    from tests.integration.test_m1_incident_control_postgres import _Suspension

    _, _, store, subject = env
    _set(store, subject, lifecycle)
    suspension = _Suspension(store, "target", "checkout-prod")
    suspension.suspend()
    try:
        assert store.control(subject, 0, action, "operator") == 1
    finally:
        suspension.release()
