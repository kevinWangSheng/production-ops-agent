"""#245 PR-1 implementation checks: ``rebind_target`` on a real PostgreSQL.

The web runs as a throwaway login of ``opspilot_web`` (U6: the rebind is a
write the web role alone may make). Covered: the decision's transaction
(generation, revocation, cancellation, lifecycle, target, audit), refusals
that write nothing, the page, successor inputs over the new target (G6/G7),
alert correlation by the incident's current target (U5 ①), the audit
timeline and the 0022 round trip. No model calls.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.acceptance_audit import audit_records
from opspilot.observation.store import ObservationStore
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.persistence.grants import WEB_ROLE
from opspilot.tools.fixture import fixture_face, fixture_versions
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_02_observation_store_postgres import (
    _authorize,
    _incident,
)
from tests.integration.test_m1_02_register_remediation_postgres import PROFILE
from tests.integration.test_m1_04_alert_intake_impl_postgres import (
    REGISTRY,
    _alert,
    _webhook,
)
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    call,
    post_form,
    post_json,
    same_origin,
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

QUESTION = "Why is checkout erroring?"


@pytest.fixture(scope="module")
def web_dsn():
    with login(DSN, WEB_ROLE) as dsn:
        yield dsn


def _build(dsn: str, *, profile: bool = False, registry=REGISTRY):
    store = DurableStore(dsn)
    store.install()
    events = DurableEventLog(store)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face(),
        targets=MappingTargetRegistry(registry),
        health_profile=PROFILE if profile else None,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="auth-rev-245",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), DurableClock(store))
    return app, workbench, store


@pytest.fixture
def env(web_dsn):
    app, workbench, store = _build(web_dsn)
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": QUESTION,
            "idempotency_key": f"rebind-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    return app, workbench, store, UUID(response.json()["incident_id"])


def _control(app, incident, generation, action, **fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": action,
            "expected_generation": str(generation),
            "idempotency_key": f"{action}-{uuid4()}",
            **fields,
        },
        headers={**basic(), **same_origin()},
    )


def _admin(query: str, *args):
    with psycopg.connect(DSN) as conn:
        conn.execute(query, args)


def _state(incident: UUID):
    """Every row a refused rebind must leave as it was."""
    with psycopg.connect(DSN) as conn:
        conn.row_factory = psycopg.rows.dict_row
        return (
            conn.execute(
                "SELECT * FROM opspilot_incidents WHERE incident_id=%s", (incident,)
            ).fetchone(),
            conn.execute(
                "SELECT run_id,state,control_generation FROM opspilot_runs WHERE incident_id=%s ORDER BY run_id",
                (incident,),
            ).fetchall(),
            conn.execute(
                "SELECT count(*) AS n FROM opspilot_controls WHERE incident_id=%s",
                (incident,),
            ).fetchone()["n"],
            conn.execute(
                "SELECT session_id,state FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY session_id",
                (incident,),
            ).fetchall(),
        )


def _uid(incident: UUID) -> str:
    with psycopg.connect(DSN) as conn:
        return conn.execute(
            "SELECT t.resource_uid FROM opspilot_incidents i JOIN opspilot_targets t USING (target_id) WHERE i.incident_id=%s",
            (incident,),
        ).fetchone()[0]


# -- the decision -------------------------------------------------------


def test_rebind_while_running_cancels_opens_and_audits(env):
    app, workbench, _, incident = env
    _admin("UPDATE opspilot_runs SET state='running' WHERE incident_id=%s", incident)
    response = _control(
        app, incident, 0, "rebind_target", to_target="checkout-canary", text="wrong"
    )
    assert response.status == 200, response.text
    assert response.json()["generation"] == 1
    assert "run_id" not in response.json()  # U2: no Run is started
    row, runs, _, _ = _state(incident)
    assert (row["lifecycle"], row["state"], row["control_generation"]) == (
        "open",
        "cancelled",
        1,
    )
    assert _uid(incident) == "checkout-canary"
    assert [(r["state"], r["control_generation"]) for r in runs] == [("cancelled", 1)]
    audit = workbench.incidents.control_audit(incident)[-1]
    assert (audit.action, audit.actor, audit.expected_generation) == (
        "rebind_target",
        UI_USER,
        0,
    )
    assert audit.payload["from_target"] == "checkout-prod"
    assert audit.payload["to_target"] == "checkout-canary"
    assert audit.payload["text"] == "wrong"
    assert audit.payload["channel"] == "web" and audit.payload["idempotency_key"]
    page = call(app, "GET", f"/incidents/{incident}", headers=basic()).text
    assert '<code id="current-target">checkout-canary</code>' in page
    assert (
        "Rebound from <code>checkout-prod</code> to <code>checkout-canary</code>"
        in page
    )
    # The run row cancelled by the rebind keeps its end time in the audit
    # timeline (G11), not an undated entry.
    with psycopg.connect(DSN) as conn:
        _, undated = audit_records(conn, incident)
    assert "opspilot_runs.ended" not in undated


@pytest.mark.parametrize("run_state", ["queued", "paused", "waiting_human", "blocked"])
def test_rebind_cancels_every_unfinished_run_state(env, run_state):
    app, _, _, incident = env
    _admin(
        "UPDATE opspilot_runs SET state=%s WHERE incident_id=%s", run_state, incident
    )
    assert (
        _control(app, incident, 0, "rebind_target", to_target="payment-prod").status
        == 200
    )
    _, runs, _, _ = _state(incident)
    assert [r["state"] for r in runs] == ["cancelled"]


def test_rebind_from_observing_recovery_revokes_and_returns_to_open(web_dsn):
    owner = DurableStore(DSN)
    incident, _, target = _incident(owner)
    session = _authorize(ObservationStore(DSN), incident, target)
    web = DurableStore(web_dsn)
    web.register_target("payment-prod")
    row, _, _, _ = _state(incident)
    assert row["lifecycle"] == "observing_recovery"
    generation = row["control_generation"]
    payload = {"to_target": "payment-prod", "channel": "web", "idempotency_key": "k"}
    assert (
        web.control(incident, generation, "rebind_target", "operator", payload)
        == generation + 1
    )
    row, runs, _, sessions = _state(incident)
    assert row["lifecycle"] == "open" and _uid(incident) == "payment-prod"
    assert sessions and all(s["state"] != "authorized" for s in sessions)
    with psycopg.connect(DSN) as conn:
        ending = conn.execute(
            "SELECT ended_reason,lifecycle_before FROM opspilot_observation_endings WHERE session_id=%s",
            (session,),
        ).fetchone()
    assert ending == ("authority_revoked", "observing_recovery")
    assert all(r["state"] == "cancelled" for r in runs)


# -- refusals write nothing ---------------------------------------------


@pytest.mark.parametrize("lifecycle", ["resolved", "closed"])
def test_resolved_or_closed_requires_reopen(env, lifecycle):
    app, _, _, incident = env
    _admin(
        "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
        lifecycle,
        incident,
    )
    before = _state(incident)
    response = _control(app, incident, 0, "rebind_target", to_target="payment-prod")
    assert (response.status, response.json()["code"]) == (409, "REOPEN_REQUIRED")
    assert _state(incident) == before


@pytest.mark.parametrize(
    ("to_target", "status", "code"),
    [
        ("checkout-prod", 409, "TARGET_UNCHANGED"),
        ("not-in-the-registry", 404, "UNKNOWN_TARGET"),
        ("", 400, "TARGET_REQUIRED"),
    ],
)
def test_unregistered_or_same_target_is_refused(env, to_target, status, code):
    app, _, _, incident = env
    before = _state(incident)
    response = _control(app, incident, 0, "rebind_target", to_target=to_target)
    assert (response.status, response.json()["code"]) == (status, code)
    assert _state(incident) == before


def test_to_target_is_refused_on_other_actions(env):
    app, _, _, incident = env
    before = _state(incident)
    response = _control(app, incident, 0, "cancel", to_target="payment-prod")
    assert (response.status, response.json()["code"]) == (400, "TARGET_NOT_ALLOWED")
    assert _state(incident) == before


def test_store_refusals_under_the_lock(env, web_dsn):
    _, _, _, incident = env
    web = DurableStore(web_dsn)
    before = _state(incident)
    cases = [
        (0, {"to_target": "checkout-prod"}, "TARGET_UNCHANGED"),
        (0, {"to_target": f"never-registered-{uuid4()}"}, "UNKNOWN_TARGET"),
        (0, {"text": "no target"}, "INVALID_INPUT"),
        (0, {"to_target": "payment-prod", "from_target": "x"}, "INVALID_INPUT"),
        (1, {"to_target": "payment-prod"}, "CONTROL_CONFLICT"),
    ]
    web.register_target("payment-prod")
    for generation, payload, code in cases:
        with pytest.raises(PersistenceError, match=f"^{code}$"):
            web.control(incident, generation, "rebind_target", "operator", payload)
        assert _state(incident) == before


# -- page ---------------------------------------------------------------


def test_page_offers_the_rebind_form_only_where_it_applies(env):
    app, _, _, incident = env
    page = call(app, "GET", f"/incidents/{incident}", headers=basic()).text
    assert 'value="rebind_target"' in page
    assert '<option value="payment-prod">' in page
    assert '<option value="checkout-prod">' not in page
    _admin(
        "UPDATE opspilot_incidents SET lifecycle='resolved' WHERE incident_id=%s",
        incident,
    )
    page = call(app, "GET", f"/incidents/{incident}", headers=basic()).text
    assert 'value="rebind_target"' not in page


# -- successor inputs over the new target (G6, G7) -----------------------


def _current_input(store, incident):
    return store.rebuild(incident)["run"]["input"]


@pytest.mark.parametrize("successor", ["follow_up", "new_run"])
def test_successor_after_rebind_is_fresh_over_the_new_target(env, successor):
    app, _, store, incident = env
    first = _current_input(store, incident)
    assert first["bound_target_id"] == "checkout-prod"
    assert (
        _control(app, incident, 0, "rebind_target", to_target="checkout-canary").status
        == 200
    )
    fields = {"text": "look at the canary"} if successor == "follow_up" else {}
    response = _control(app, incident, 1, successor, **fields)
    assert response.status == 200, response.text
    run_id = response.json()["run_id"]
    new = _current_input(store, incident)
    assert new["bound_target_id"] == "checkout-canary"
    assert new["scope_facts"]["target_ids"] == ["checkout-canary"]
    # Fresh, not a continuation: the intake question without the handoff
    # note, and the face's own context for the new Run id (no carried refs).
    assert new["question"] == QUESTION
    assert new["evidence_context"] == fixture_face().evidence_context(run_id)
    assert new["scope_facts"]["affected_service"] == {
        "namespace": "otel-demo",
        "workload": "checkout-canary",
    }


def test_successor_without_rebind_is_still_a_continuation(env):
    app, _, store, incident = env
    assert _control(app, incident, 0, "cancel").status == 200
    response = _control(app, incident, 1, "follow_up", text="again")
    assert response.status == 200, response.text
    new = _current_input(store, incident)
    assert new["bound_target_id"] == "checkout-prod"
    assert new["question"].startswith(QUESTION) and new["question"] != QUESTION


def test_observation_after_rebind_resolves_the_new_target(web_dsn):
    # A second registry key for the workload the profile observes: only
    # the new target's own identity can authorize it.
    uid = f"checkout-blue-{uuid4().hex[:8]}"
    registry = {
        **REGISTRY,
        uid: {k: v for k, v in REGISTRY["checkout-prod"].items() if k != "match"},
    }
    app, _, _ = _build(web_dsn, profile=True, registry=registry)
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": QUESTION,
            "idempotency_key": f"rebind-obs-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    incident = UUID(response.json()["incident_id"])
    assert _control(app, incident, 0, "rebind_target", to_target=uid).status == 200
    response = _control(app, incident, 1, "register_remediation", revision="v2")
    assert response.status == 200, response.text
    with psycopg.connect(DSN) as conn:
        session = conn.execute(
            "SELECT t.resource_uid,s.target->>'resource_uid' FROM opspilot_observation_sessions s JOIN opspilot_targets t USING (target_id) WHERE s.incident_id=%s",
            (incident,),
        ).fetchone()
    assert session == (uid, uid)


def test_profile_of_another_workload_cannot_observe_the_rebound_target(web_dsn):
    app, _, _ = _build(web_dsn, profile=True)
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": QUESTION,
            "idempotency_key": f"rebind-obs-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    incident = UUID(response.json()["incident_id"])
    assert (
        _control(app, incident, 0, "rebind_target", to_target="payment-prod").status
        == 200
    )
    response = _control(app, incident, 1, "register_remediation", revision="v2")
    assert (response.status, response.json()["code"]) == (
        409,
        "HEALTH_PROFILE_TARGET_MISMATCH",
    )


# -- alert correlation by the current target (U5 ①) ----------------------


def _fire(app, fingerprint, starts_at):
    response = post_json(
        app,
        "/intake/alertmanager",
        _webhook(_alert(fingerprint, startsAt=starts_at)),
        headers=bearer(),
    )
    assert response.status == 200, response.text
    return response.json()["results"][0]


def test_alert_correlation_follows_the_current_target(web_dsn):
    app, _, _ = _build(web_dsn)
    fingerprint = uuid4().hex[:16]
    first = _fire(app, fingerprint, "2026-10-27T10:00:00Z")
    assert first["outcome"] == "created"
    incident = UUID(first["incident_id"])
    # Same target, still open: a re-fired episode attaches (unchanged).
    attached = _fire(app, fingerprint, "2026-10-27T10:05:00Z")
    assert (attached["outcome"], attached["incident_id"]) == ("attached", str(incident))
    generation = _state(incident)[0]["control_generation"]
    assert (
        _control(
            app, incident, generation, "rebind_target", to_target="payment-prod"
        ).status
        == 200
    )
    # The old target fires again: no longer this incident's, a new one opens
    # with no relation to the rebound incident (it is not a candidate at all).
    again = _fire(app, fingerprint, "2026-10-27T10:10:00Z")
    assert again["outcome"] == "created" and again["incident_id"] != str(incident)
    assert again.get("related_incident_id") is None
    _, runs, _, _ = _state(incident)
    assert len(runs) == 1  # the rebound incident took nothing
    # Every candidate closed: the next episode opens a related incident (unchanged).
    other = UUID(again["incident_id"])
    _control(app, other, _state(other)[0]["control_generation"], "close")
    last = _fire(app, fingerprint, "2026-10-27T10:15:00Z")
    assert last["outcome"] == "created"
    with psycopg.connect(DSN) as conn:
        related = conn.execute(
            "SELECT related_incident_id FROM opspilot_incidents WHERE incident_id=%s",
            (UUID(last["incident_id"]),),
        ).fetchone()[0]
    assert related == other


# -- migration 0022 ------------------------------------------------------


@pytest.fixture
def scratch():
    name = f"opspilot_245_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    try:
        yield make_conninfo(DSN, dbname=name)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def test_0022_round_trip_and_refusal_with_a_merge_pointer(scratch):
    pg_dump = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
    schema.migrate(scratch, pg_dump=pg_dump)
    head = schema.schema_dump(scratch, pg_dump=pg_dump)
    schema.command.downgrade(schema._config(scratch), "0021_release_incident_links")
    assert schema.schema_dump(scratch, pg_dump=pg_dump) == schema.fresh_dump(
        scratch, "0021_release_incident_links", pg_dump=pg_dump
    )
    schema.migrate(scratch, pg_dump=pg_dump)
    assert schema.schema_dump(scratch, pg_dump=pg_dump) == head
    with psycopg.connect(scratch) as conn:
        a, b = uuid4(), uuid4()
        for incident in (a, b):
            conn.execute(
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state) VALUES(%s,%s,'queued')",
                (incident, str(incident)),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "UPDATE opspilot_incidents SET merged_into_incident_id=incident_id WHERE incident_id=%s",
                    (a,),
                )
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed',merged_into_incident_id=%s WHERE incident_id=%s",
            (b, a),
        )
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        schema.command.downgrade(schema._config(scratch), "0021_release_incident_links")
    with psycopg.connect(scratch) as conn:
        assert schema.current_revision(conn) == "0022_incident_association"
