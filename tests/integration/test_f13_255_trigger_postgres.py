"""#255: no postmortem generation while the incident still has an unfinished Run.

Independent regression and contract tests, written from the issue, the F12
live evidence (docs/evidence/f12-live/run.md, scenario A: six generations,
the first five voided by ``WATERMARK_MOVED``) and the user's decision of
2026-10-17:

* U1 -- while any Run of the incident is not terminal (queued, running,
  paused, waiting_human, blocked: the human handoff and the parked Run
  included) the incident is not a generation candidate and cannot be
  claimed, however its observation ended;
* U2 -- once that Run reaches a terminal state (completed, failed,
  cancelled, budget_exhausted) the incident is a candidate again at once,
  and the watermark yields one generation;
* U3 -- generation after ``close`` keeps its current behaviour.

Only observable results are asserted: the candidate list, the claim, the
model calls, and the public postmortem view (versions, their states, the
generation attempts). Upstream state the product reaches through the
investigation worker (a Run finishing, being parked, a new automatic
observation) is written directly, the way the F13 implementer tests seed
it; human controls go through ``DurableStore.control``. The model is a
scripted double; nothing here calls a model or a telemetry backend.
"""

import json
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from opspilot import schema
from opspilot.knowledge import KnowledgeStore
from opspilot.knowledge.jobs import GenerationStore
from opspilot.persistence import DurableStore, PoolConfig
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_03_draft_generation_postgres import (
    T0,
    ScriptedModel,
    _reply,
    _seed,
    _worker,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=0, max_size=3, timeout=5.0)
# opspilot/domain/runs.py RUN_EXECUTION: states with outgoing edges / without
UNFINISHED = ("queued", "running", "paused", "waiting_human", "blocked")
TERMINAL = ("completed", "failed", "cancelled", "budget_exhausted")
CONCLUSION = {
    "summary": "checkout errors after deploy",
    "claims": [{"kind": "fact", "text": "5xx rose", "evidence_ids": ["ev"]}],
}


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    name = f"opspilot_f13t_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    schema.migrate(scratch, pg_dump=PG_DUMP)
    try:
        yield scratch
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture(scope="module")
def stores(dsn: str) -> Iterator[tuple[KnowledgeStore, GenerationStore, DurableStore]]:
    knowledge = KnowledgeStore(dsn, pool=POOL)
    jobs = GenerationStore(dsn, pool=POOL)
    durable = DurableStore(dsn, pool=POOL)
    for store in (knowledge, jobs, durable):
        store.install()
    yield knowledge, jobs, durable
    for store in (knowledge, jobs, durable):
        store.close()


@pytest.fixture(autouse=True)
def _isolated(dsn: str) -> None:
    """Earlier tests' incidents are parked behind a day-long lease, so each
    test's ``poll_once`` sees only its own incident."""
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_postmortem_generation_jobs(incident_id, lease_owner, "
            "lease_until) SELECT incident_id, gen_random_uuid(), clock_timestamp() "
            "+ interval '1 day' FROM opspilot_incidents ON CONFLICT (incident_id) DO UPDATE "
            "SET lease_owner=EXCLUDED.lease_owner, lease_until=EXCLUDED.lease_until"
        )


# --- helpers


def _one(dsn: str, statement: str, params: tuple) -> dict:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(statement, params).fetchone()


def _exec(dsn: str, statement: str, params: tuple) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(statement, params)


def _incident(dsn: str, incident_id: UUID) -> dict:
    return _one(
        dsn,
        "SELECT control_generation, lifecycle, current_run_id, observation_generation "
        "FROM opspilot_incidents WHERE incident_id=%s",
        (incident_id,),
    )


def _current_run_state(dsn: str, incident_id: UUID) -> str:
    return _one(
        dsn,
        "SELECT r.state FROM opspilot_runs r JOIN opspilot_incidents i "
        "ON r.run_id = i.current_run_id WHERE i.incident_id=%s",
        (incident_id,),
    )["state"]


def _set_current_run(dsn: str, incident_id: UUID, state: str) -> None:
    """What the investigation worker writes when the Run moves (finishes,
    fails, is parked as blocked, waits for a human ...)."""
    _exec(
        dsn,
        "UPDATE opspilot_runs SET state=%s, owner=NULL, lease_until=NULL "
        "WHERE run_id=(SELECT current_run_id FROM opspilot_incidents "
        "WHERE incident_id=%s)",
        (state, incident_id),
    )


def _concluded_and_observing(dsn: str) -> UUID:
    """A published conclusion, its Run completed, and the automatic recovery
    observation authorized (lifecycle observing_recovery): scenario A at
    08:16, the moment before the follow-up."""
    incident_id = _seed(dsn, authorized=True)
    _exec(
        dsn,
        "UPDATE opspilot_incidents SET lifecycle='observing_recovery' "
        "WHERE incident_id=%s",
        (incident_id,),
    )
    return incident_id


def _follow_up(durable: DurableStore, dsn: str, incident_id: UUID) -> UUID:
    """A follow-up after the conclusion: the product renews the Run and, in
    the same transaction, revokes the observation (C3 §10). Returns the new
    Run; checks the preconditions that make this the #255 path."""
    before = _incident(dsn, incident_id)
    run_id = uuid4()
    durable.control(
        incident_id,
        before["control_generation"],
        "follow_up",
        "operator",
        {"text": "Also check the payments dependency"},
        renew_run_id=run_id,
        renew_deadline=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    after = _incident(dsn, incident_id)
    assert after["current_run_id"] == run_id
    assert _current_run_state(dsn, incident_id) == "queued"
    # the observation ended by revocation: the trigger the old scan fired on
    sessions = _one(
        dsn,
        "SELECT count(*) FILTER (WHERE state='authorized') AS authorized, "
        "count(*) FILTER (WHERE ended_reason='authority_revoked') AS revoked "
        "FROM opspilot_observation_sessions WHERE incident_id=%s",
        (incident_id,),
    )
    assert sessions["authorized"] == 0 and sessions["revoked"] >= 1
    return run_id


def _publish_and_observe(dsn: str, incident_id: UUID) -> None:
    """The renewed Run publishes a new conclusion and the product authorizes
    a new automatic observation (``authorize_session``: next observation
    generation, lifecycle observing_recovery, one authorized session)."""
    _set_current_run(dsn, incident_id, "completed")
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "UPDATE opspilot_incidents SET state='completed', conclusion=%s, "
            "lifecycle='observing_recovery', "
            "observation_generation=observation_generation + 1 "
            "WHERE incident_id=%s RETURNING control_generation, "
            "observation_generation, target_id",
            (json.dumps(CONCLUSION), incident_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id, incident_id, purpose, "
            "target_id, target, subject_control_generation, observation_generation, "
            "authorized_by, state, authorized_global_generation, "
            "authorized_target_generation, deadline_at, max_samples, "
            "sample_interval_seconds, sustained_window_seconds) "
            "VALUES (%s, %s, 'incident_recovery', %s, '{}', %s, %s, 'op', 'authorized', "
            "0, 0, %s, 5, 60, 60)",
            (uuid4(), incident_id, row[2], row[0], row[1], T0 + timedelta(hours=1)),
        )


def _assert_nothing_generated(stores, incident_id: UUID, model: ScriptedModel) -> None:
    knowledge, jobs, _ = stores
    assert incident_id not in jobs.candidates(limit=1000)
    worker = _worker(stores, model)
    assert worker.generate(incident_id) is None
    assert all(o.incident_id != incident_id for o in worker.poll_once())
    assert model.calls == []
    view = knowledge.incident_postmortem(incident_id)
    assert view["status"] == "not_generated"
    assert view["postmortem"] is None and view["attempts"] == []


def _generate_once(stores, incident_id: UUID) -> dict:
    """The incident is due; exactly one generation follows, and a second
    poll at the same watermark generates nothing."""
    knowledge, jobs, _ = stores
    assert incident_id in jobs.candidates(limit=1000)
    model = ScriptedModel(_reply(incident_id))
    outcomes = [
        o for o in _worker(stores, model).poll_once() if o.incident_id == incident_id
    ]
    assert [(o.status, o.version, o.state) for o in outcomes] == [
        ("succeeded", 1, "under_review")
    ]
    idle = ScriptedModel()
    assert all(o.incident_id != incident_id for o in _worker(stores, idle).poll_once())
    assert idle.calls == [] and len(model.calls) == 1
    assert incident_id not in jobs.candidates(limit=1000)
    view = knowledge.incident_postmortem(incident_id)
    assert view["status"] == "under_review"
    assert [v["state"] for v in view["postmortem"]["versions"]] == ["under_review"]
    assert [(a["status"], a["version"]) for a in view["attempts"]] == [("succeeded", 1)]
    return view


# --- U1: an unfinished Run holds generation back


def test_follow_up_after_conclusion_does_not_trigger_generation(stores, dsn) -> None:
    """#255 minimal reproduction: the follow-up revokes the observation and
    queues a new Run; the revocation is not a generation trigger."""
    _, jobs, durable = stores
    incident_id = _concluded_and_observing(dsn)
    assert incident_id not in jobs.candidates(limit=1000)  # observation running
    _follow_up(durable, dsn, incident_id)
    _assert_nothing_generated(stores, incident_id, ScriptedModel())


@pytest.mark.parametrize("state", UNFINISHED)
def test_no_candidate_while_the_renewed_run_is_unfinished(stores, dsn, state) -> None:
    _, _, durable = stores
    incident_id = _concluded_and_observing(dsn)
    _follow_up(durable, dsn, incident_id)
    _set_current_run(dsn, incident_id, state)
    _assert_nothing_generated(stores, incident_id, ScriptedModel())


def test_takeover_hands_the_run_to_a_human_and_still_no_candidate(stores, dsn) -> None:
    """The human handoff through the public control: the Run waits for the
    human (waiting_human), which is not terminal."""
    _, _, durable = stores
    incident_id = _concluded_and_observing(dsn)
    _follow_up(durable, dsn, incident_id)
    generation = _incident(dsn, incident_id)["control_generation"]
    durable.control(incident_id, generation, "takeover", "operator")
    assert _current_run_state(dsn, incident_id) == "waiting_human"
    _assert_nothing_generated(stores, incident_id, ScriptedModel())


@pytest.mark.parametrize("state", UNFINISHED)
def test_an_observation_that_ended_normally_waits_for_the_run_too(
    stores, dsn, state
) -> None:
    """U1 is not limited to revocation: a recovery confirmed while a Run is
    still unfinished does not trigger generation either."""
    incident_id = _seed(dsn)  # ended recovery_confirmed, Run completed
    _set_current_run(dsn, incident_id, state)
    _assert_nothing_generated(stores, incident_id, ScriptedModel())


# --- U2: a terminal Run makes the incident a candidate again, once


@pytest.mark.parametrize("state", TERMINAL)
def test_a_terminal_run_restores_the_candidate_and_generates_once(
    stores, dsn, state
) -> None:
    _, _, durable = stores
    incident_id = _concluded_and_observing(dsn)
    _follow_up(durable, dsn, incident_id)
    _assert_nothing_generated(stores, incident_id, ScriptedModel())
    _set_current_run(dsn, incident_id, state)
    _generate_once(stores, incident_id)


def test_cancel_through_the_control_restores_the_candidate_at_once(stores, dsn) -> None:
    _, _, durable = stores
    incident_id = _concluded_and_observing(dsn)
    _follow_up(durable, dsn, incident_id)
    generation = _incident(dsn, incident_id)["control_generation"]
    durable.control(incident_id, generation, "cancel", "operator")
    assert _current_run_state(dsn, incident_id) == "cancelled"
    _generate_once(stores, incident_id)


def test_repeated_follow_ups_generate_one_draft_after_the_last_run(stores, dsn) -> None:
    """Scenario A shape: three follow-ups, each renewing the Run and revoking
    the automatic observation, each Run publishing a conclusion that
    re-authorizes observation. Nothing is generated or voided on the way;
    the last Run finishing (no new observation) yields one version, and no
    attempt anywhere failed with WATERMARK_MOVED."""
    knowledge, _, durable = stores
    incident_id = _concluded_and_observing(dsn)
    idle = ScriptedModel()
    for _ in range(3):
        _follow_up(durable, dsn, incident_id)
        _assert_nothing_generated(stores, incident_id, idle)
        _publish_and_observe(dsn, incident_id)
        _assert_nothing_generated(stores, incident_id, idle)
    _follow_up(durable, dsn, incident_id)
    _assert_nothing_generated(stores, incident_id, idle)
    _set_current_run(dsn, incident_id, "completed")
    view = _generate_once(stores, incident_id)
    assert all(a["error_code"] != "WATERMARK_MOVED" for a in view["attempts"])
    runs = _one(
        dsn,
        "SELECT count(*) AS n FROM opspilot_runs WHERE incident_id=%s",
        (incident_id,),
    )["n"]
    assert runs == 5


# --- U3: generation after close keeps its current behaviour


def test_close_during_a_handed_over_run_generates_once(stores, dsn) -> None:
    """Scenario A's ending: follow-up, takeover (waiting_human), close. Close
    cancels the Run and the incident becomes a candidate (G5), once."""
    _, _, durable = stores
    incident_id = _concluded_and_observing(dsn)
    _follow_up(durable, dsn, incident_id)
    generation = _incident(dsn, incident_id)["control_generation"]
    generation = durable.control(incident_id, generation, "takeover", "operator")
    _assert_nothing_generated(stores, incident_id, ScriptedModel())
    durable.control(incident_id, generation, "close", "operator")
    assert _incident(dsn, incident_id)["lifecycle"] == "closed"
    assert _current_run_state(dsn, incident_id) == "cancelled"
    _generate_once(stores, incident_id)


def test_close_after_a_generated_version_regenerates_once(stores, dsn) -> None:
    """Unchanged: close moves the control generation, so the version under
    review goes stale in the close transaction and one new version is
    generated at the new watermark."""
    knowledge, _, durable = stores
    incident_id = _concluded_and_observing(dsn)
    _follow_up(durable, dsn, incident_id)
    _set_current_run(dsn, incident_id, "completed")
    _generate_once(stores, incident_id)
    generation = _incident(dsn, incident_id)["control_generation"]
    durable.control(incident_id, generation, "close", "operator")
    stale = knowledge.incident_postmortem(incident_id)["postmortem"]["versions"]
    assert [(v["version"], v["state"]) for v in stale] == [(1, "stale")]

    model = ScriptedModel(_reply(incident_id))
    outcomes = [
        o for o in _worker(stores, model).poll_once() if o.incident_id == incident_id
    ]
    assert [(o.status, o.version, o.state) for o in outcomes] == [
        ("succeeded", 2, "under_review")
    ]
    idle = ScriptedModel()
    assert all(o.incident_id != incident_id for o in _worker(stores, idle).poll_once())
    assert idle.calls == []
    versions = knowledge.incident_postmortem(incident_id)["postmortem"]["versions"]
    assert [(v["version"], v["state"]) for v in versions] == [
        (1, "stale"),
        (2, "under_review"),
    ]
