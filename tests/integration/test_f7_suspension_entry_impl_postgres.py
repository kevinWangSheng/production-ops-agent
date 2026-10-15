"""F7 implementation checks through the web runtime login and HTTP entry."""

import os
import re
from uuid import uuid4

import psycopg
import pytest

from opspilot import schema
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.persistence.grants import WEB_ROLE
from scripts.m0.postgres_lab import DSN
from tests.integration.test_runtime_roles_postgres import _form, _workbench
from tests.m1_web_support import basic, call, post_form, post_json, same_origin
from tests.runtime_roles_support import catalog_grants, login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


@pytest.fixture
def entry():
    with login(DSN, WEB_ROLE) as dsn:
        app, workbench, store, _ = _workbench(dsn)
        # Shared gates must be restored even when an assertion fails.
        initial = store.suspension_state()
        try:
            yield app, workbench, store
        finally:
            owner = DurableStore(DSN)
            current = owner.suspension_state()
            if current["global"]["suspended"] != initial["global"]["suspended"]:
                owner.set_global_suspension(
                    initial["global"]["suspended"],
                    expected_generation=current["global"]["generation"],
                    actor="test-cleanup",
                )
            for uid, gate in current["targets"].items():
                was_suspended = initial["targets"].get(uid, {"suspended": False})[
                    "suspended"
                ]
                if gate["suspended"] != was_suspended:
                    owner.set_target_suspension(
                        owner.register_target(uid),
                        was_suspended,
                        expected_generation=gate["generation"],
                        actor="test-cleanup",
                    )


def state(app):
    response = call(app, "GET", "/controls", headers=basic())
    assert response.status == 200, response.text
    return response.json()


def audit_count():
    with psycopg.connect(DSN) as conn:
        return conn.execute(
            "SELECT count(*) FROM opspilot_suspension_audit"
        ).fetchone()[0]


@pytest.mark.parametrize("scope", ["global", "target"])
def test_suspend_release_same_value_replay_and_conflicts(entry, scope):
    app, _, _ = entry
    snapshot = state(app)
    target = snapshot["targets"][0]
    gate = snapshot["global"] if scope == "global" else target
    fields = {
        "scope": scope,
        "suspended": "true",
        "expected_generation": str(gate["generation"]),
        "idempotency_key": uuid4().hex,
    }
    if scope == "target":
        fields["target_id"] = target["target_id"]
    before = audit_count()
    response = _form(app, "/controls/suspension", fields)
    assert response.status == 200, response.text
    result = response.json()
    assert result == {
        "scope": scope,
        "target_id": fields.get("target_id"),
        "suspended": True,
        "generation": gate["generation"] + (not gate["suspended"]),
        "replayed": False,
    }
    assert audit_count() == before + 1
    replay = _form(app, "/controls/suspension", fields)
    assert replay.status == 200
    assert replay.json() == {**result, "replayed": True}
    assert audit_count() == before + 1
    conflict = _form(app, "/controls/suspension", {**fields, "suspended": "false"})
    assert conflict.status == 409 and conflict.json()["code"] == "CONTROL_KEY_CONFLICT"
    stale = _form(
        app,
        "/controls/suspension",
        {
            **fields,
            "expected_generation": str(result["generation"] + 1),
            "idempotency_key": uuid4().hex,
        },
    )
    assert stale.status == 409
    assert stale.json() == {
        "code": "CONTROL_CONFLICT",
        "current_generation": result["generation"],
    }
    same = _form(
        app,
        "/controls/suspension",
        {
            **fields,
            "expected_generation": str(result["generation"]),
            "idempotency_key": uuid4().hex,
        },
    )
    assert same.status == 200 and same.json()["generation"] == result["generation"]
    assert audit_count() == before + 2
    released = _form(
        app,
        "/controls/suspension",
        {
            **fields,
            "suspended": "false",
            "expected_generation": str(result["generation"]),
            "idempotency_key": uuid4().hex,
        },
    )
    assert released.status == 200, released.text
    assert released.json()["generation"] == result["generation"] + 1
    assert audit_count() == before + 3
    # An old success replays even after the live scope moved on.
    assert _form(app, "/controls/suspension", fields).json() == {
        **result,
        "replayed": True,
    }
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT actor FROM opspilot_suspension_audit ORDER BY audit_id DESC LIMIT 1"
        ).fetchone() == ("alice",)
    final = state(app)
    observed = (
        final["global"]
        if scope == "global"
        else next(t for t in final["targets"] if t["target_id"] == fields["target_id"])
    )
    assert observed["suspended"] is False


def test_auth_validation_html_and_registry_resolution(entry):
    app, workbench, store = entry
    assert call(app, "GET", "/controls").status == 401
    fields = {
        "scope": "global",
        "suspended": "false",
        "expected_generation": str(state(app)["global"]["generation"]),
        "idempotency_key": uuid4().hex,
    }
    assert post_form(app, "/controls/suspension", fields, headers={}).status == 401
    assert (
        post_form(
            app,
            "/controls/suspension",
            fields,
            headers={**basic(), "origin": "https://evil.test"},
        ).status
        == 403
    )
    assert (
        post_json(
            app, "/controls/suspension", fields, headers={**basic(), **same_origin()}
        ).status
        == 415
    )
    before = audit_count()
    for patch in (
        {"scope": "other"},
        {"suspended": "1"},
        {"expected_generation": "-1"},
        {"expected_generation": ""},
        {"expected_generation": "9" * 5000},
        {"idempotency_key": ""},
        {"target_id": "extra"},
        {"scope": "target"},
    ):
        response = _form(app, "/controls/suspension", {**fields, **patch})
        assert response.status == 400, response.text
    unknown = _form(
        app,
        "/controls/suspension",
        {**fields, "scope": "target", "target_id": "missing"},
    )
    assert unknown.status == 404 and unknown.json()["code"] == "UNKNOWN_TARGET"
    assert audit_count() == before
    before_state = store.suspension_state()
    page = call(
        app,
        "GET",
        "/controls",
        headers={**basic(), "accept": "text/html,application/json"},
    )
    assert page.status == 200
    keys = re.findall(r'name="idempotency_key" value="([^"]+)"', page.text)
    assert len(keys) == len(workbench.targets.keys()) + 1
    assert len(set(keys)) == len(keys)
    assert 'name="expected_generation"' in page.text
    assert "in-flight results are kept only as history" in page.text
    second = call(app, "GET", "/controls", headers={**basic(), "accept": "text/html"})
    assert not set(keys) & set(
        re.findall(r'name="idempotency_key" value="([^"]+)"', second.text)
    )
    assert store.suspension_state() == before_state
    redirected = post_form(
        app,
        "/controls/suspension",
        fields,
        headers={**basic(), **same_origin(), "accept": "text/html"},
    )
    assert redirected.status == 303 and redirected.headers["location"] == "/controls"


def test_pending_same_value_intent_never_writes_twice(entry):
    app, workbench, _ = entry
    gate = state(app)["global"]
    key = uuid4().hex
    workbench.ledger.put(
        "suspension_intent",
        key,
        {
            "scope": "global",
            "target_id": None,
            "suspended": gate["suspended"],
            "expected_generation": gate["generation"],
            "actor_id": "alice",
        },
    )
    before = audit_count()
    response = _form(
        app,
        "/controls/suspension",
        {
            "scope": "global",
            "suspended": str(gate["suspended"]).lower(),
            "expected_generation": str(gate["generation"]),
            "idempotency_key": key,
        },
    )
    assert (
        response.status == 409
        and response.json()["current_generation"] == gate["generation"]
    )
    assert audit_count() == before


def test_0017_round_trip_preserves_old_grants():
    # Isolated database avoids changing the shared lab's runtime privileges.
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = "opspilot_f7_" + uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        config = schema._config(dsn)
        schema.command.upgrade(config, "0016_release_deadline_evidence")
        with psycopg.connect(dsn) as conn:
            before = catalog_grants(conn, WEB_ROLE)
        schema.command.upgrade(config, "head")
        with psycopg.connect(dsn) as conn:
            after = catalog_grants(conn, WEB_ROLE)
            assert after["opspilot_suspension_audit"] == frozenset(
                {"INSERT(actor,generation,suspended,target_id)"}
            )
        schema.command.downgrade(config, "0016_release_deadline_evidence")
        with psycopg.connect(dsn) as conn:
            assert catalog_grants(conn, WEB_ROLE) == before
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.mark.parametrize("scope", ["global", "target"])
def test_suspension_revokes_run_and_release_keeps_it_paused(entry, scope):
    from tests.integration.test_runtime_roles_postgres import _submit

    app, workbench, _ = entry
    incident = _submit(app)
    owner = DurableStore(DSN)
    rebuilt = owner.rebuild(incident)
    owner.claim(incident, rebuilt["run"]["run_id"], uuid4(), workbench.run_versions)
    snapshot = state(app)
    gate = (
        snapshot["global"]
        if scope == "global"
        else next(t for t in snapshot["targets"] if t["target_id"] == "checkout-prod")
    )
    fields = {
        "scope": scope,
        "suspended": "true",
        "expected_generation": str(gate["generation"]),
        "idempotency_key": uuid4().hex,
    }
    if scope == "target":
        fields["target_id"] = "checkout-prod"
    suspended = _form(app, "/controls/suspension", fields)
    assert suspended.status == 200, suspended.text
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT i.state,r.state,r.owner,r.lease_until FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id WHERE i.incident_id=%s",
            (incident,),
        ).fetchone() == ("paused", "paused", None, None)
        if scope == "target":
            row = conn.execute(
                "SELECT t.target_id,s.suspended FROM opspilot_targets t JOIN opspilot_target_suspensions s USING(target_id) WHERE resource_uid='checkout-prod'"
            ).fetchone()
            target_uuid = conn.execute(
                "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            ).fetchone()[0]
            assert row == (target_uuid, True)
    released = _form(
        app,
        "/controls/suspension",
        {
            **fields,
            "suspended": "false",
            "expected_generation": str(suspended.json()["generation"]),
            "idempotency_key": uuid4().hex,
        },
    )
    assert released.status == 200, released.text
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT i.state,r.state,r.owner,r.lease_until FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id WHERE i.incident_id=%s",
            (incident,),
        ).fetchone() == ("paused", "paused", None, None)


def test_unregistered_registry_target_is_read_only_until_mutation(entry):
    from opspilot.web.store import MappingTargetRegistry

    app, workbench, store = entry
    key = "f7-new-" + uuid4().hex
    workbench.targets = MappingTargetRegistry(
        {
            key: {
                "integration_id": "fixture",
                "cluster_uid": "cluster",
                "namespace": "test",
            }
        }
    )
    assert state(app)["targets"] == [
        {"target_id": key, "suspended": False, "generation": 0}
    ]
    assert key not in store.suspension_state()["targets"]
    response = _form(
        app,
        "/controls/suspension",
        {
            "scope": "target",
            "target_id": key,
            "suspended": "true",
            "expected_generation": "0",
            "idempotency_key": uuid4().hex,
        },
    )
    assert response.status == 200, response.text
    assert store.suspension_state()["targets"][key] == {
        "suspended": True,
        "generation": 1,
    }
    assert (
        _form(
            app,
            "/controls/suspension",
            {
                "scope": "target",
                "target_id": key,
                "suspended": "false",
                "expected_generation": "1",
                "idempotency_key": uuid4().hex,
            },
        ).status
        == 200
    )


def test_refused_first_target_request_registers_nothing(entry):
    from opspilot.web.store import MappingTargetRegistry

    app, workbench, _ = entry
    key = "f7-stale-" + uuid4().hex
    workbench.targets = MappingTargetRegistry(
        {
            key: {
                "integration_id": "fixture",
                "cluster_uid": "cluster",
                "namespace": "test",
            }
        }
    )
    before = audit_count()
    response = _form(
        app,
        "/controls/suspension",
        {
            "scope": "target",
            "target_id": key,
            "suspended": "true",
            "expected_generation": "3",
            "idempotency_key": uuid4().hex,
        },
    )
    assert response.status == 409
    assert response.json() == {"code": "CONTROL_CONFLICT", "current_generation": 0}
    with psycopg.connect(DSN) as conn:
        assert not conn.execute(
            "SELECT 1 FROM opspilot_targets WHERE resource_uid=%s", (key,)
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM opspilot_target_suspensions s JOIN opspilot_targets t"
            " USING (target_id) WHERE t.resource_uid=%s",
            (key,),
        ).fetchone()
    assert audit_count() == before


@pytest.mark.parametrize("scope", ["global", "target"])
@pytest.mark.parametrize("error", ["STORAGE_UNAVAILABLE", "RETRY", "TIMEOUT"])
def test_same_value_retry_after_lost_commit_ack(entry, monkeypatch, scope, error):
    app, workbench, _ = entry
    snapshot = state(app)
    gate = snapshot["global"] if scope == "global" else snapshot["targets"][0]
    key = uuid4().hex
    fields = {
        "scope": scope,
        "suspended": str(gate["suspended"]).lower(),
        "expected_generation": str(gate["generation"]),
        "idempotency_key": key,
    }
    if scope == "target":
        fields["target_id"] = gate["target_id"]
    method = f"set_{scope}_suspension"
    original = getattr(workbench.incidents, method)

    def committed_but_ack_lost(*args, **kwargs):
        # The adapter commits the real audit INSERT through the web login.
        original(*args, **kwargs)
        raise PersistenceError(error)

    before = audit_count()
    with monkeypatch.context() as patch:
        patch.setattr(workbench.incidents, method, committed_but_ack_lost)
        first = _form(app, "/controls/suspension", fields)
    assert first.status == 503 and first.json() == {"code": error}
    assert audit_count() == before + 1
    retry = _form(app, "/controls/suspension", fields)
    assert audit_count() == before + 1, "identical retry appended a second audit row"
    assert retry.status == 409
    assert retry.json() == {
        "code": "CONTROL_CONFLICT",
        "current_generation": gate["generation"],
    }
    assert workbench.ledger.get("suspension_intent", key) is not None
    assert workbench.ledger.get("suspension", key) is None


@pytest.mark.parametrize("scope", ["global", "target"])
def test_definitive_generation_refusal_allows_corrected_retry(entry, scope):
    app, workbench, _ = entry
    snapshot = state(app)
    gate = snapshot["global"] if scope == "global" else snapshot["targets"][0]
    key = uuid4().hex
    fields = {
        "scope": scope,
        "suspended": str(gate["suspended"]).lower(),
        "expected_generation": str(gate["generation"] + 1),
        "idempotency_key": key,
    }
    if scope == "target":
        fields["target_id"] = gate["target_id"]
    before = audit_count()
    refused = _form(app, "/controls/suspension", fields)
    assert refused.status == 409
    assert refused.json() == {
        "code": "CONTROL_CONFLICT",
        "current_generation": gate["generation"],
    }
    assert audit_count() == before
    assert workbench.ledger.get("suspension_intent", key) is None
    corrected = _form(
        app,
        "/controls/suspension",
        {**fields, "expected_generation": str(gate["generation"])},
    )
    assert corrected.status == 200, corrected.text
    assert corrected.json()["replayed"] is False
    assert audit_count() == before + 1
