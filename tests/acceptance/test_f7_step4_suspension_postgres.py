"""F7 step 4 acceptance (PR-A): the operator suspension entry on the workbench.

feature_list.json F7 step 4: "Activate global or service-level investigation
suspension and verify new queries stop and in-flight work follows the
reviewed cancellation policy."

Written by a fresh-context author from the task record
(``docs/tasks/2026-10-15-f7.md`` 「接口」), the contract pre-review
(``docs/tasks/2026-10-15-f7-contract.md`` S4a-S4d, U5-a, U9-a, R1, R7) and C3
§4 (l.110-116) / §8 (l.312), without reading the implementation.

The workbench under test runs on a throwaway login role that is a member of
``opspilot_web`` (not the migration owner), so a missing grant shows up as a
refused request. The "worker side" (claim, step/tool commits, a real
read-only executor against a fake opener) uses the owner connection, as the
worker process would with its own role; persisted state is read back
through the owner connection. Observable outcomes only: HTTP responses,
committed rows, whether a claim succeeds and whether the executor contacts
the data source.

Opt-in like the other PG tests: ``M1_DURABLE_POSTGRES=1`` against the lab
database (``scripts/m0/postgres_lab.py``).
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.persistence import DurableStore, PersistenceError
from opspilot.persistence.grants import WEB_ROLE
from opspilot.tools.otel_demo import METRICS_TOOL, otel_demo_versions
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
from tests.integration.test_m1_otel_demo_contract_postgres import (
    CHECKOUT_BODY,
    GOOD_EXPR,
    FakeOpener,
    _call,
    _executor,
)
from tests.m1_web_support import ORIGIN, call, post_form, post_json
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

#: A per-module operator name, so audit rows written by this file are
#: distinguishable from every other test's rows in the shared lab database.
OPERATOR = f"f7-operator-{uuid4().hex[:8]}"
PASSWORD = "f7 suspension acceptance password"
EVENT_TOKEN = "f7-event-token-for-tests-only"
FOREIGN_ORIGIN = "https://evil.example.test"
#: Registry keys (service names) the deployment's target registry lists.
KEY_A = f"f7-svc-a-{uuid4().hex[:8]}"
KEY_B = f"f7-svc-b-{uuid4().hex[:8]}"
KEY_C = f"f7-svc-c-{uuid4().hex[:8]}"
REGISTRY = {
    key: {
        "integration_id": "f7-integration",
        "cluster_uid": "f7-cluster",
        "namespace": "f7-namespace",
    }
    for key in (KEY_A, KEY_B, KEY_C)
}
TWO_TOOLS = {
    "tool_calls": [
        {"name": "query", "arguments": {}},
        {"name": "query", "arguments": {}},
    ]
}


# --- fixtures ----------------------------------------------------------------


def _open_global_gate(owner: DurableStore) -> None:
    row = _global_row()
    if row["global_suspended"]:
        owner.set_global_suspension(
            False, expected_generation=row["global_generation"], actor="f7-cleanup"
        )


@pytest.fixture(scope="module")
def owner() -> Iterator[DurableStore]:
    store = DurableStore(DSN)
    store.install()
    yield store
    _open_global_gate(store)
    store.close()


@pytest.fixture(scope="module")
def web_dsn() -> Iterator[str]:
    with login(DSN, WEB_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def app(web_dsn: str) -> Any:
    store = DurableStore(web_dsn)
    store.install()
    events = DurableEventLog(store)
    events.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=otel_demo_versions(),
        run_seconds=600,
        targets=MappingTargetRegistry(REGISTRY),
    )
    config = AuthConfig(
        ui_users={OPERATOR: hash_password(PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="auth-rev-f7",
        allowed_origins=frozenset({ORIGIN}),
    )
    return create_app(workbench, Authenticator(config), DurableClock(store))


@pytest.fixture(autouse=True)
def _gate_open_around_each_test(owner: DurableStore) -> Iterator[None]:
    _open_global_gate(owner)
    yield
    _open_global_gate(owner)


# --- HTTP helpers --------------------------------------------------------------


def _auth(user: str = OPERATOR, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"authorization": f"Basic {token}"}


def _json_headers(**extra: str) -> dict[str, str]:
    return {**_auth(), "origin": ORIGIN, "accept": "application/json", **extra}


def _controls(app: Any) -> dict[str, Any]:
    response = call(
        app, "GET", "/controls", headers={**_auth(), "accept": "application/json"}
    )
    assert response.status == 200, response.text
    return response.json()


def _target_entry(state: dict[str, Any], key: str) -> dict[str, Any]:
    matches = [t for t in state["targets"] if t["target_id"] == key]
    assert len(matches) == 1, state["targets"]
    return matches[0]


def _suspend(
    app: Any,
    *,
    scope: str,
    suspended: bool,
    expected_generation: int,
    key: str | None = None,
    target_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    fields = {
        "scope": scope,
        "suspended": "true" if suspended else "false",
        "expected_generation": str(expected_generation),
        "idempotency_key": key or f"f7-{uuid4()}",
    }
    if target_id is not None:
        fields["target_id"] = target_id
    return post_form(
        app,
        "/controls/suspension",
        fields,
        headers=_json_headers() if headers is None else headers,
    )


# --- owner-side reads and worker-side actions ----------------------------------


def _query(sql: str, *args: Any) -> list[dict[str, Any]]:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        return list(conn.execute(sql, args).fetchall())


def _global_row() -> dict[str, Any]:
    return _query(
        "SELECT global_suspended,global_generation FROM opspilot_scope_controls WHERE scope_id=1"
    )[0]


def _audit(*, target: UUID | None) -> list[dict[str, Any]]:
    if target is None:
        return _query(
            "SELECT target_id,suspended,generation,actor FROM opspilot_suspension_audit WHERE actor=%s AND target_id IS NULL ORDER BY audit_id",
            OPERATOR,
        )
    return _query(
        "SELECT target_id,suspended,generation,actor FROM opspilot_suspension_audit WHERE actor=%s AND target_id=%s ORDER BY audit_id",
        OPERATOR,
        target,
    )


def _run(run_id: UUID) -> dict[str, Any]:
    return _query(
        "SELECT state,owner,lease_until FROM opspilot_runs WHERE run_id=%s", run_id
    )[0]


def _incident_state(incident_id: UUID) -> str:
    return _query(
        "SELECT state FROM opspilot_incidents WHERE incident_id=%s", incident_id
    )[0]["state"]


def _accept(owner: DurableStore, target: UUID | None) -> tuple[UUID, UUID]:
    incident, run = uuid4(), uuid4()
    owner.accept(
        incident,
        run,
        f"f7-step4-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=5,
        versions=otel_demo_versions(),
        target_id=target,
    )
    return incident, run


def _claim(owner: DurableStore, incident: UUID, run: UUID):
    return owner.claim(incident, run, uuid4(), otel_demo_versions())


def _claim_refused(owner: DurableStore, incident: UUID, run: UUID) -> bool:
    try:
        _claim(owner, incident, run)
    except PersistenceError as exc:
        assert str(exc) == "CONTROL_DENIED", exc
        return True
    return False


# --- 1-3: global suspension, in-flight policy, release -------------------------


def test_global_suspension_stops_new_queries_fences_in_flight_work_and_release_resumes_nothing(
    app: Any, owner: DurableStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_a = owner.register_target(KEY_A)
    incident, run = _accept(owner, target_a)
    lease = _claim(owner, incident, run)
    # In flight before the suspension: a committed step with one adopted tool
    # result, and a read-only executor that has already queried the source.
    step = owner.commit_step(lease, "round-0", TWO_TOOLS)
    owner.commit_tool(lease, step, 0, {"ok": True, "before": "suspension"})
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    executor = _executor(owner, lease, opener, monkeypatch)
    first = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=0))
    assert (first.status, first.reason) == ("ok", None)
    assert len(opener.requests) == 1
    assert _run(run)["state"] == "running" and _run(run)["lease_until"] is not None

    before = _controls(app)
    assert before["global"]["suspended"] is False
    g0 = before["global"]["generation"]
    audit_base = len(_audit(target=None))
    assert g0 == _global_row()["global_generation"]

    suspended = _suspend(app, scope="global", suspended=True, expected_generation=g0)
    assert suspended.status == 200, suspended.text
    body = suspended.json()
    assert body["scope"] == "global"
    assert body.get("target_id") is None
    assert body["suspended"] is True
    assert body["generation"] == g0 + 1
    assert body["replayed"] is False
    assert _global_row() == {"global_suspended": True, "global_generation": g0 + 1}
    assert _controls(app)["global"] == {"suspended": True, "generation": g0 + 1}

    # The active Run is parked by the suspension itself; the lease is gone.
    parked = _run(run)
    assert parked["state"] == "paused"
    assert parked["owner"] is None and parked["lease_until"] is None
    assert _incident_state(incident) == "paused"

    # Audit: one row, the Basic Auth username as actor.
    audit = _audit(target=None)[audit_base:]
    assert [(a["suspended"], a["generation"]) for a in audit] == [(True, g0 + 1)]

    # New work for any target (or none) is refused while suspended; intake
    # itself still records the incident (events keep arriving, C3 §4).
    target_b = owner.register_target(KEY_B)
    for new_target in (target_a, target_b, None):
        new_incident, new_run = _accept(owner, new_target)
        assert _claim_refused(owner, new_incident, new_run)
    assert _claim_refused(owner, incident, run)

    # In flight (C3 §4 l.114, §8 l.312): the pre-suspension lease cannot
    # dispatch a new read, nor adopt a step or tool result; the earlier
    # adopted result stays as history.
    denied = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=1))
    assert (denied.status, denied.reason) == ("denied", "SUSPENDED")
    assert denied.operation.sent is False
    assert len(opener.requests) == 1
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        owner.commit_tool(lease, step, 1, {"ok": True, "after": "suspension"})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        owner.commit_step(lease, "round-1", TWO_TOOLS)
    steps = _query(
        "SELECT step_id,status,tool_results,logical_key FROM opspilot_steps WHERE run_id=%s",
        run,
    )
    adopted = [s for s in steps if s["step_id"] == step]
    assert len(adopted) == 1
    assert [r["ordinal"] for r in adopted[0]["tool_results"]] == [0]
    assert adopted[0]["tool_results"][0]["result"] == {
        "ok": True,
        "before": "suspension",
    }
    assert any(s["status"] == "late_result" for s in steps)
    assert owner.rebuild(incident)["conclusion"] is None

    # Release: the generation moves again; nothing is resumed automatically.
    released = _suspend(
        app, scope="global", suspended=False, expected_generation=g0 + 1
    )
    assert released.status == 200, released.text
    assert released.json()["generation"] == g0 + 2
    assert released.json()["suspended"] is False
    assert released.json()["replayed"] is False
    assert _global_row() == {"global_suspended": False, "global_generation": g0 + 2}
    assert _run(run)["state"] == "paused"
    assert _incident_state(incident) == "paused"
    assert _claim_refused(owner, incident, run)
    after = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=2))
    assert (after.status, after.reason) == ("denied", "CONTROL_GENERATION_CHANGED")
    assert len(opener.requests) == 1
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        owner.commit_tool(lease, step, 1, {"ok": True, "after": "release"})
    assert [
        (a["suspended"], a["generation"]) for a in _audit(target=None)[audit_base:]
    ] == [(True, g0 + 1), (False, g0 + 2)]

    # A new incident after the release is claimable again.
    fresh_incident, fresh_run = _accept(owner, target_a)
    assert _claim(owner, fresh_incident, fresh_run).run_id == fresh_run


# --- 4: target (service-level) suspension -------------------------------------


def test_target_suspension_by_registry_key_pauses_only_that_target(
    app: Any, owner: DurableStore
) -> None:
    target_a = owner.register_target(KEY_A)
    target_b = owner.register_target(KEY_B)
    incident_a, run_a = _accept(owner, target_a)
    incident_b, run_b = _accept(owner, target_b)
    lease_a = _claim(owner, incident_a, run_a)
    _claim(owner, incident_b, run_b)
    state_b_before = _incident_state(incident_b)

    before = _controls(app)
    keys = {t["target_id"] for t in before["targets"]}
    assert {KEY_A, KEY_B, KEY_C} <= keys
    ga = _target_entry(before, KEY_A)["generation"]
    gb = _target_entry(before, KEY_B)["generation"]
    g_global = before["global"]["generation"]

    unknown = _suspend(
        app,
        scope="target",
        suspended=True,
        expected_generation=0,
        target_id=f"f7-not-registered-{uuid4().hex[:8]}",
    )
    assert unknown.status == 404, unknown.text
    assert unknown.json()["code"] == "UNKNOWN_TARGET"

    done = _suspend(
        app,
        scope="target",
        suspended=True,
        expected_generation=ga,
        target_id=KEY_A,
    )
    assert done.status == 200, done.text
    body = done.json()
    assert body["scope"] == "target"
    assert body["target_id"] == KEY_A
    assert body["suspended"] is True
    assert body["generation"] == ga + 1
    assert body["replayed"] is False

    # Only target A is paused.
    parked = _run(run_a)
    assert parked["state"] == "paused"
    assert parked["owner"] is None and parked["lease_until"] is None
    assert _incident_state(incident_a) == "paused"
    untouched = _run(run_b)
    assert untouched["state"] == "running" and untouched["lease_until"] is not None
    assert _incident_state(incident_b) == state_b_before

    state = _controls(app)
    assert _target_entry(state, KEY_A) == {
        "target_id": KEY_A,
        "suspended": True,
        "generation": ga + 1,
    }
    assert _target_entry(state, KEY_B)["suspended"] is False
    assert _target_entry(state, KEY_B)["generation"] == gb
    assert state["global"] == {"suspended": False, "generation": g_global}

    # New work on A is refused, on B it is not; A's old lease is fenced.
    new_a = _accept(owner, target_a)
    assert _claim_refused(owner, *new_a)
    new_b = _accept(owner, target_b)
    assert _claim(owner, *new_b).run_id == new_b[1]
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        owner.commit_step(lease_a, "round-0", TWO_TOOLS)

    assert [
        (a["target_id"], a["suspended"], a["generation"])
        for a in _audit(target=target_a)
    ] == [(target_a, True, ga + 1)]
    assert _audit(target=target_b) == []

    released = _suspend(
        app,
        scope="target",
        suspended=False,
        expected_generation=ga + 1,
        target_id=KEY_A,
    )
    assert released.status == 200, released.text
    assert released.json()["generation"] == ga + 2
    assert _run(run_a)["state"] == "paused"
    assert _incident_state(incident_a) == "paused"
    assert _claim_refused(owner, incident_a, run_a)


# --- 5: conflicts, idempotency, same-value writes ------------------------------


def test_stale_generation_idempotent_replay_key_conflict_and_same_value_write(
    app: Any, owner: DurableStore
) -> None:
    target_c = owner.register_target(KEY_C)
    gc = _target_entry(_controls(app), KEY_C)["generation"]
    global_base = len(_audit(target=None))

    stale = _suspend(
        app, scope="target", suspended=True, expected_generation=gc + 7, target_id=KEY_C
    )
    assert stale.status == 409, stale.text
    assert stale.json()["code"] == "CONTROL_CONFLICT"
    assert stale.json()["current_generation"] == gc
    g_global = _global_row()["global_generation"]
    stale_global = _suspend(
        app, scope="global", suspended=True, expected_generation=g_global + 7
    )
    assert stale_global.status == 409, stale_global.text
    assert stale_global.json()["code"] == "CONTROL_CONFLICT"
    assert stale_global.json()["current_generation"] == g_global
    assert _global_row() == {"global_suspended": False, "global_generation": g_global}
    assert _audit(target=target_c) == []
    assert len(_audit(target=None)) == global_base

    key = f"f7-replay-{uuid4()}"
    first = _suspend(
        app,
        scope="target",
        suspended=True,
        expected_generation=gc,
        target_id=KEY_C,
        key=key,
    )
    assert first.status == 200, first.text
    assert first.json()["generation"] == gc + 1
    assert first.json()["replayed"] is False
    assert len(_audit(target=target_c)) == 1

    replay = _suspend(
        app,
        scope="target",
        suspended=True,
        expected_generation=gc,
        target_id=KEY_C,
        key=key,
    )
    assert replay.status == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["generation"] == gc + 1
    assert replay.json()["suspended"] is True
    assert len(_audit(target=target_c)) == 1

    reused = _suspend(
        app,
        scope="target",
        suspended=False,
        expected_generation=gc + 1,
        target_id=KEY_C,
        key=key,
    )
    assert reused.status == 409, reused.text
    assert reused.json()["code"] == "CONTROL_KEY_CONFLICT"
    assert _target_entry(_controls(app), KEY_C)["suspended"] is True
    assert len(_audit(target=target_c)) == 1

    # R7: the same value again (fresh key, current generation) is audited but
    # does not bump the fence generation.
    again = _suspend(
        app, scope="target", suspended=True, expected_generation=gc + 1, target_id=KEY_C
    )
    assert again.status == 200, again.text
    assert again.json()["generation"] == gc + 1
    assert again.json()["replayed"] is False
    assert _target_entry(_controls(app), KEY_C) == {
        "target_id": KEY_C,
        "suspended": True,
        "generation": gc + 1,
    }
    assert [(a["suspended"], a["generation"]) for a in _audit(target=target_c)] == [
        (True, gc + 1),
        (True, gc + 1),
    ]

    # Same for the global gate: releasing an open gate does not bump.
    same_global = _suspend(
        app, scope="global", suspended=False, expected_generation=g_global
    )
    assert same_global.status == 200, same_global.text
    assert same_global.json()["generation"] == g_global
    assert _global_row() == {"global_suspended": False, "global_generation": g_global}
    assert [
        (a["suspended"], a["generation"]) for a in _audit(target=None)[global_base:]
    ] == [(False, g_global)]

    release = _suspend(
        app,
        scope="target",
        suspended=False,
        expected_generation=gc + 1,
        target_id=KEY_C,
    )
    assert release.status == 200, release.text
    assert release.json()["generation"] == gc + 2


def test_malformed_requests_are_refused_without_a_state_change(app: Any) -> None:
    g = _global_row()
    audit_base = len(_audit(target=None))
    cases = [
        {"scope": "target", "suspended": "true", "expected_generation": "0"},
        {"scope": "galaxy", "suspended": "true", "expected_generation": "0"},
        {
            "scope": "global",
            "suspended": "maybe",
            "expected_generation": str(g["global_generation"]),
        },
        {"scope": "global", "suspended": "true", "expected_generation": "-1"},
        {"scope": "global", "suspended": "true", "expected_generation": "x"},
    ]
    for fields in cases:
        response = post_form(
            app,
            "/controls/suspension",
            {**fields, "idempotency_key": f"f7-bad-{uuid4()}"},
            headers=_json_headers(),
        )
        assert response.status == 400, (fields, response.text)
        assert response.json()["code"] == "INVALID_INPUT"
    assert _global_row() == g
    assert len(_audit(target=None)) == audit_base


# --- 6: authentication, Origin and body format -----------------------------------


def test_auth_origin_and_form_body_are_enforced(app: Any) -> None:
    g = _global_row()
    audit_base = len(_audit(target=None))

    def attempt(headers: dict[str, str]) -> Any:
        return _suspend(
            app,
            scope="global",
            suspended=True,
            expected_generation=g["global_generation"],
            headers=headers,
        )

    no_auth = attempt({"origin": ORIGIN, "accept": "application/json"})
    assert no_auth.status == 401, no_auth.text
    wrong = attempt(
        {**_auth(password="wrong"), "origin": ORIGIN, "accept": "application/json"}
    )
    assert wrong.status == 401, wrong.text
    foreign = attempt(
        {**_auth(), "origin": FOREIGN_ORIGIN, "accept": "application/json"}
    )
    assert foreign.status == 403, foreign.text
    no_origin = attempt({**_auth(), "accept": "application/json"})
    assert no_origin.status == 403, no_origin.text

    as_json = post_json(
        app,
        "/controls/suspension",
        {
            "scope": "global",
            "suspended": "true",
            "expected_generation": str(g["global_generation"]),
            "idempotency_key": f"f7-json-{uuid4()}",
        },
        headers=_json_headers(),
    )
    assert as_json.status == 415, as_json.text
    assert as_json.json()["code"] == "FORM_REQUIRED"

    unauthenticated_read = call(
        app, "GET", "/controls", headers={"accept": "application/json"}
    )
    assert unauthenticated_read.status == 401

    assert _global_row() == g
    assert len(_audit(target=None)) == audit_base


# --- 7: GET /controls and the HTML flow ----------------------------------------


def test_controls_page_shows_current_state_and_html_post_redirects(app: Any) -> None:
    state = _controls(app)
    g = _global_row()
    assert state["global"] == {
        "suspended": g["global_suspended"],
        "generation": g["global_generation"],
    }
    assert {KEY_A, KEY_B, KEY_C} <= {t["target_id"] for t in state["targets"]}
    for entry in state["targets"]:
        assert set(entry) == {"target_id", "suspended", "generation"}
        assert isinstance(entry["suspended"], bool)
        assert isinstance(entry["generation"], int)

    page = call(app, "GET", "/controls", headers={**_auth(), "accept": "text/html"})
    assert page.status == 200
    assert page.headers.get("content-type", "").startswith("text/html")
    assert KEY_A in page.text

    redirected = _suspend(
        app,
        scope="global",
        suspended=True,
        expected_generation=g["global_generation"],
        headers={**_auth(), "origin": ORIGIN, "accept": "text/html"},
    )
    assert redirected.status == 303, redirected.text
    assert redirected.headers.get("location", "").endswith("/controls")
    assert _global_row() == {
        "global_suspended": True,
        "global_generation": g["global_generation"] + 1,
    }
    assert _controls(app)["global"] == {
        "suspended": True,
        "generation": g["global_generation"] + 1,
    }
    # The autouse fixture reopens the gate as the owner afterwards.
