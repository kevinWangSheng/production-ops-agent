"""#245 PR-2 implementation checks: ``merge_into`` on a real PostgreSQL.

The web runs as a throwaway login of ``opspilot_web``. Covered: the
source's transaction (generation, revocation, cancellation, closed +
pointer, audit), the survivor's single event input with nothing else moved
(C3 section 10), refusals that write nothing, a merged incident's refusals,
the pages' links, the audit timeline, and late alerts following the
pointer (U5 ②). No model calls.
"""

from __future__ import annotations

import os
import threading
import time
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.acceptance_audit import audit_records
from opspilot.persistence import DurableStore
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


@pytest.fixture
def app(web_dsn):
    store = DurableStore(web_dsn)
    store.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=DurableEventLog(store),
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face(),
        targets=MappingTargetRegistry(REGISTRY),
        health_profile=PROFILE,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="auth-rev-245-merge",
        allowed_origins=frozenset({ORIGIN}),
    )
    return create_app(workbench, Authenticator(config), DurableClock(store))


def _submit(app, target="checkout-prod") -> UUID:
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": target,
            "question": QUESTION,
            "idempotency_key": f"merge-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    return UUID(response.json()["incident_id"])


def _generation(incident: UUID) -> int:
    return int(
        _one(
            "SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s",
            incident,
        )["control_generation"]
    )


def _control(app, incident, action, *, generation=None, key=None, **fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": action,
            "expected_generation": str(
                _generation(incident) if generation is None else generation
            ),
            "idempotency_key": key or f"{action}-{uuid4()}",
            **fields,
        },
        headers={**basic(), **same_origin()},
    )


def _merge(app, source, survivor, **fields):
    return _control(app, source, "merge_into", into_incident_id=str(survivor), **fields)


def _rows(query: str, *args):
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        return conn.execute(query, args).fetchall()


def _one(query: str, *args):
    (row,) = _rows(query, *args)
    return row


def _admin(query: str, *args):
    with psycopg.connect(DSN) as conn:
        conn.execute(query, args)


def _state(incident: UUID):
    """Every row a refusal must leave as it was."""
    return (
        _one("SELECT * FROM opspilot_incidents WHERE incident_id=%s", incident),
        _rows(
            "SELECT run_id,state,control_generation FROM opspilot_runs WHERE incident_id=%s ORDER BY run_id",
            incident,
        ),
        _rows(
            "SELECT audit_id FROM opspilot_controls WHERE incident_id=%s ORDER BY audit_id",
            incident,
        ),
        _rows(
            "SELECT input_id FROM opspilot_inputs WHERE incident_id=%s ORDER BY input_id",
            incident,
        ),
        _rows(
            "SELECT session_id,state FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY session_id",
            incident,
        ),
    )


def _page(app, incident) -> str:
    response = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert response.status == 200, response.text
    return response.text


# -- the decision -------------------------------------------------------


def test_merge_closes_the_source_and_adds_one_event_to_the_survivor(app):
    source, survivor = _submit(app), _submit(app)
    _admin("UPDATE opspilot_runs SET state='running' WHERE incident_id=%s", source)
    for incident in (source, survivor):
        assert (
            _control(app, incident, "register_remediation", revision="v1").status == 200
        )
    before = _state(survivor)
    response = _merge(app, source, survivor, text="same checkout outage")
    assert response.status == 200, response.text
    assert "run_id" not in response.json()
    row, runs, _, _, sessions = _state(source)
    assert row["lifecycle"] == "closed" and row["state"] == "cancelled"
    assert row["merged_into_incident_id"] == survivor
    assert row["control_generation"] == 2
    assert [r["state"] for r in runs] == ["cancelled"]
    assert sessions and all(s["state"] != "authorized" for s in sessions)
    audit = _one(
        "SELECT action,actor,expected_generation,payload FROM opspilot_controls WHERE incident_id=%s AND action='merge_into'",
        source,
    )
    assert (audit["actor"], audit["expected_generation"]) == (UI_USER, 1)
    assert audit["payload"]["into_incident_id"] == str(survivor)
    assert audit["payload"]["text"] == "same checkout outage"
    # The survivor: generation, Runs and observation untouched; one event.
    after = _state(survivor)
    assert after[0]["control_generation"] == before[0]["control_generation"]
    assert after[0]["lifecycle"] == before[0]["lifecycle"] == "observing_recovery"
    assert after[1] == before[1] and after[2] == before[2] and after[4] == before[4]
    assert all(s["state"] == "authorized" for s in after[4])
    assert len(after[3]) == len(before[3]) + 1
    event = _one(
        "SELECT kind,actor,content,control_generation FROM opspilot_inputs WHERE incident_id=%s ORDER BY sequence DESC LIMIT 1",
        survivor,
    )
    assert event["kind"] == "event" and event["actor"] == UI_USER
    content = event["content"]
    assert content["channel"] == "merge"
    assert content["source_incident_id"] == str(source)
    assert content["source_lifecycle"] == "observing_recovery"
    assert content["source_concluded"] is False
    assert str(source) in content["text"]
    # Pages link both ways.
    assert f'id="merged-into" href="/incidents/{survivor}"' in _page(app, source)
    assert f'href="/incidents/{source}"' in _page(app, survivor)
    assert "Merged from" in _page(app, survivor)
    # The Run the merge cancelled keeps its end time in the audit timeline.
    with psycopg.connect(DSN) as conn:
        _, undated = audit_records(conn, source)
    assert "opspilot_runs.ended" not in undated


def test_the_event_never_copies_the_source_conclusion(app):
    source, survivor = _submit(app), _submit(app)
    _admin(
        "UPDATE opspilot_incidents SET conclusion=%s::jsonb WHERE incident_id=%s",
        '{"summary": "SECRET-CONCLUSION-BODY"}',
        source,
    )
    assert _merge(app, source, survivor).status == 200
    event = _one(
        "SELECT content FROM opspilot_inputs WHERE incident_id=%s ORDER BY sequence DESC LIMIT 1",
        survivor,
    )["content"]
    assert event["source_concluded"] is True
    assert "SECRET-CONCLUSION-BODY" not in str(event)


def test_a_replayed_merge_returns_the_first_result(app):
    source, survivor = _submit(app), _submit(app)
    first = _merge(app, source, survivor, key="merge-once", generation=0)
    again = _control(
        app,
        source,
        "merge_into",
        generation=0,
        key="merge-once",
        into_incident_id=str(survivor),
    )
    assert again.status == 200 and again.json()["replayed"] is True
    assert again.json()["generation"] == first.json()["generation"] == 1
    assert len(_state(survivor)[3]) == 1  # one merge event, not two


# -- refusals write nothing ---------------------------------------------


def _refused(app, source, survivor, status, code, **fields):
    before = (_state(source), _state(survivor) if survivor else None)
    response = _control(
        app, source, "merge_into", into_incident_id=str(survivor), **fields
    )
    assert (response.status, response.json()["code"]) == (status, code), response.text
    assert (_state(source), _state(survivor) if survivor else None) == before


def test_cross_target_is_refused(app):
    _refused(app, _submit(app), _submit(app, "payment-prod"), 409, "TARGET_MISMATCH")


def test_closed_survivor_is_refused(app):
    source, survivor = _submit(app), _submit(app)
    assert _control(app, survivor, "close").status == 200
    _refused(app, source, survivor, 409, "ILLEGAL_TRANSITION")


def test_self_is_refused(app):
    source = _submit(app)
    _refused(app, source, source, 409, "ILLEGAL_TRANSITION")


def test_unknown_survivor_is_refused(app):
    source = _submit(app)
    before = _state(source)
    response = _control(app, source, "merge_into", into_incident_id=str(uuid4()))
    assert (response.status, response.json()["code"]) == (404, "UNKNOWN_IDENTITY")
    assert _state(source) == before


def test_handoff_survivor_is_refused(app):
    source = _submit(app)
    unresolved = _alert(labels={"alertname": "X", "namespace": "nowhere"})
    result = post_json(
        app, "/intake/alertmanager", _webhook(unresolved), headers=bearer()
    ).json()["results"][0]
    assert result["outcome"] == "handoff_created"
    _refused(app, source, UUID(result["incident_id"]), 409, "HANDOFF_ONLY")


@pytest.mark.parametrize(
    ("fields", "status", "code"),
    [
        ({"into_incident_id": ""}, 400, "INTO_INCIDENT_REQUIRED"),
        ({"into_incident_id": "not-a-uuid"}, 400, "INVALID_INPUT"),
    ],
)
def test_malformed_merge_is_refused(app, fields, status, code):
    source = _submit(app)
    before = _state(source)
    response = _control(app, source, "merge_into", **fields)
    assert (response.status, response.json()["code"]) == (status, code)
    assert _state(source) == before


def test_into_incident_is_refused_on_other_actions(app):
    source, survivor = _submit(app), _submit(app)
    before = _state(source)
    response = _control(app, source, "cancel", into_incident_id=str(survivor))
    assert (response.status, response.json()["code"]) == (
        400,
        "INTO_INCIDENT_NOT_ALLOWED",
    )
    assert _state(source) == before


def test_a_merged_incident_refuses_reopen_rebind_and_merge(app):
    source, survivor, third = _submit(app), _submit(app), _submit(app)
    assert _merge(app, source, survivor).status == 200
    page = _page(app, source)
    for action in ("reopen", "rebind_target", "merge_into"):
        assert f'value="{action}"' not in page, action
    before = _state(source)
    for action, fields in (
        ("reopen", {}),
        ("rebind_target", {"to_target": "payment-prod"}),
        ("merge_into", {"into_incident_id": str(third)}),
    ):
        response = _control(app, source, action, **fields)
        assert (response.status, response.json()["code"]) == (
            409,
            "ILLEGAL_TRANSITION",
        ), action
        assert _state(source) == before
    # Nor can anything be merged into it (closed survivor).
    _refused(app, third, source, 409, "ILLEGAL_TRANSITION")


def test_page_offers_the_merge_form(app):
    source = _submit(app)
    page = _page(app, source)
    assert 'value="merge_into"' in page and 'name="into_incident_id"' in page


# -- late alerts follow the pointer (U5 ②) --------------------------------


def _fire(app, fingerprint, starts_at):
    response = post_json(
        app,
        "/intake/alertmanager",
        _webhook(_alert(fingerprint, startsAt=starts_at)),
        headers=bearer(),
    )
    assert response.status == 200, response.text
    return response.json()["results"][0]


def _related(incident) -> UUID | None:
    return _one(
        "SELECT related_incident_id FROM opspilot_incidents WHERE incident_id=%s",
        UUID(incident),
    )["related_incident_id"]


def test_late_alerts_follow_the_merge_pointer(app):
    f1, f2, f3 = (uuid4().hex[:16] for _ in range(3))
    source = UUID(_fire(app, f1, "2026-10-27T10:00:00Z")["incident_id"])
    survivor = UUID(_fire(app, f2, "2026-10-27T10:00:00Z")["incident_id"])
    # Same target, open: an episode of the source's fingerprint attaches
    # to the source (unchanged).
    attached = _fire(app, f1, "2026-10-27T10:01:00Z")
    assert (attached["outcome"], attached["incident_id"]) == ("attached", str(source))
    assert _merge(app, source, survivor).status == 200
    # After the merge the source's fingerprint attaches to the survivor.
    later = _fire(app, f1, "2026-10-27T10:02:00Z")
    assert (later["outcome"], later["incident_id"]) == ("attached", str(survivor))
    # A chain: the survivor is merged on into a third incident.
    third = UUID(_fire(app, f3, "2026-10-27T10:00:00Z")["incident_id"])
    assert _merge(app, survivor, third).status == 200
    chained = _fire(app, f1, "2026-10-27T10:03:00Z")
    assert (chained["outcome"], chained["incident_id"]) == ("attached", str(third))
    # The end of the chain closed: a new incident related to it.
    assert _control(app, third, "close").status == 200
    new = _fire(app, f1, "2026-10-27T10:04:00Z")
    assert new["outcome"] == "created"
    assert _related(new["incident_id"]) == third


def test_a_closed_survivor_opens_a_new_related_incident(app):
    f1, f2 = uuid4().hex[:16], uuid4().hex[:16]
    source = UUID(_fire(app, f1, "2026-10-27T11:00:00Z")["incident_id"])
    survivor = UUID(_fire(app, f2, "2026-10-27T11:00:00Z")["incident_id"])
    assert _merge(app, source, survivor).status == 200
    assert _control(app, survivor, "close").status == 200
    new = _fire(app, f1, "2026-10-27T11:05:00Z")
    assert new["outcome"] == "created"
    assert _related(new["incident_id"]) == survivor


def test_unmerged_correlation_is_unchanged(app):
    f1 = uuid4().hex[:16]
    first = _fire(app, f1, "2026-10-27T12:00:00Z")
    incident = first["incident_id"]
    assert _fire(app, f1, "2026-10-27T12:01:00Z")["incident_id"] == incident
    assert _control(app, UUID(incident), "close").status == 200
    new = _fire(app, f1, "2026-10-27T12:02:00Z")
    assert new["outcome"] == "created" and _related(new["incident_id"]) == UUID(
        incident
    )


# -- review on #380: lock order, release path, chain bound ----------------


def _pair(app):
    """Two alert incidents on one target, ``(source, survivor, source
    fingerprint)`` with the source's id above the survivor's: the order in
    which following a pointer after locking the source used to invert the
    merge's id-ordered locks."""
    f1, f2 = uuid4().hex[:16], uuid4().hex[:16]
    a = UUID(_fire(app, f1, "2026-10-27T13:00:00Z")["incident_id"])
    b = UUID(_fire(app, f2, "2026-10-27T13:00:00Z")["incident_id"])
    return (a, b, f1) if a > b else (b, a, f2)


def test_a_late_alert_locks_incidents_in_id_order(app):
    source, survivor, fingerprint = _pair(app)
    assert _merge(app, source, survivor).status == 200
    outcome: dict = {}
    with psycopg.connect(DSN) as blocker:
        blocker.execute("SET lock_timeout='10s'")
        # Another writer holds the survivor (lower id) and will want the
        # source next, exactly the merge's id order.
        blocker.execute(
            "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
            (survivor,),
        )
        worker = threading.Thread(
            target=lambda: outcome.update(
                result=_fire(app, fingerprint, "2026-10-27T13:05:00Z")
            )
        )
        worker.start()
        time.sleep(1.5)
        # The alert must not hold the source while it waits for the survivor.
        blocker.execute(
            "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
            (source,),
        )
        blocker.commit()
        worker.join(timeout=20)
    assert outcome["result"]["outcome"] == "attached", outcome
    assert outcome["result"]["incident_id"] == str(survivor)


def _chain(app, length: int) -> tuple[str, list[UUID]]:
    """An alert incident followed by ``length`` merges, each into the next
    incident on the same target; returns the head's fingerprint and the
    incidents from head to tail."""
    fingerprint = uuid4().hex[:16]
    incidents = [UUID(_fire(app, fingerprint, "2026-10-27T14:00:00Z")["incident_id"])]
    for _ in range(length):
        incidents.append(
            UUID(_fire(app, uuid4().hex[:16], "2026-10-27T14:00:00Z")["incident_id"])
        )
    for source, survivor in zip(incidents, incidents[1:]):
        assert _merge(app, source, survivor).status == 200
    return fingerprint, incidents


def test_a_chain_past_the_bound_never_relates_to_a_merged_incident(app, monkeypatch):
    from opspilot.persistence import correlation

    fingerprint, incidents = _chain(app, 3)
    # Within the bound the chain reaches its open tail.
    monkeypatch.setattr(correlation, "MERGE_CHAIN_LIMIT", 3)
    tail = _fire(app, fingerprint, "2026-10-27T14:05:00Z")
    assert (tail["outcome"], tail["incident_id"]) == ("attached", str(incidents[-1]))
    # Past the bound the head is dropped: no attachment, and no relation to
    # any merged (closed + pointer) incident on the way.
    monkeypatch.setattr(correlation, "MERGE_CHAIN_LIMIT", 2)
    new = _fire(app, fingerprint, "2026-10-27T14:06:00Z")
    assert new["outcome"] == "created"
    assert _related(new["incident_id"]) is None
