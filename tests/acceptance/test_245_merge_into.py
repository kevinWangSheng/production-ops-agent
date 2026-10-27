"""Issue #245 PR-2 independent acceptance tests for ``merge_into``.

These tests are written from the Agent Brief and the existing public
Workbench/storage interfaces.  They observe HTTP responses and pages plus
committed PostgreSQL rows; they do not assert internal call order.

Signature assumption (the brief leaves this form field implicit):
``POST /incidents/{source_id}/control`` accepts
``action=merge_into``, ``into_incident_id=<survivor id>``, optional ``text``,
``expected_generation`` and ``idempotency_key``.  The audit payload carries
the same target id under ``into_incident_id``.
"""

# ruff: noqa: F811 - pytest fixtures imported from the PR-1 module
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.acceptance.test_245_rebind_target import (  # noqa: F401 - fixtures
    TARGET_A,
    TARGET_B,
    UI,
    _control,
    _remember_dsn,
    _submit,
    app,
    database,
)
from tests.m1_web_support import basic, bearer, call, post_form, post_json

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

ACTOR = "alice"


def _incident_state(database: str, incident_id: str) -> dict:
    """Rows observable by a refusal or by the merge transition."""
    with psycopg.connect(database, row_factory=dict_row) as conn:
        incident = conn.execute(
            "SELECT i.incident_id,i.lifecycle,i.state,i.control_generation,"
            "i.current_run_id,i.target_id,i.merged_into_incident_id,i.mode,"
            "i.conclusion,t.resource_uid "
            "FROM opspilot_incidents i LEFT JOIN opspilot_targets t "
            "ON t.target_id=i.target_id WHERE i.incident_id=%s",
            (UUID(incident_id),),
        ).fetchone()
        assert incident is not None
        runs = conn.execute(
            "SELECT run_id,state,control_generation,input FROM opspilot_runs "
            "WHERE incident_id=%s ORDER BY run_id",
            (UUID(incident_id),),
        ).fetchall()
        controls = conn.execute(
            "SELECT action,actor,expected_generation,resulting_generation,payload "
            "FROM opspilot_controls WHERE incident_id=%s "
            "ORDER BY resulting_generation,audit_id",
            (UUID(incident_id),),
        ).fetchall()
        sessions = conn.execute(
            "SELECT session_id,state,ended_reason,subject_control_generation,"
            "observation_generation FROM opspilot_observation_sessions "
            "WHERE incident_id=%s ORDER BY session_id",
            (UUID(incident_id),),
        ).fetchall()
        inputs = conn.execute(
            "SELECT input_id,sequence,kind,content,actor,control_generation "
            "FROM opspilot_inputs WHERE incident_id=%s ORDER BY sequence",
            (UUID(incident_id),),
        ).fetchall()
    return {
        "incident": dict(incident),
        "runs": [dict(row) for row in runs],
        "controls": [dict(row) for row in controls],
        "sessions": [dict(row) for row in sessions],
        "inputs": [dict(row) for row in inputs],
    }


def _set_open_running(database: str, incident_id: str) -> None:
    """Make a deterministic in-flight fixture after authorizing observation."""
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='open',state='running' "
            "WHERE incident_id=%s",
            (UUID(incident_id),),
        )
        conn.execute(
            "UPDATE opspilot_runs SET state='running' WHERE incident_id=%s",
            (UUID(incident_id),),
        )


def _authorized_open_pair(app, database: str) -> tuple[str, str]:
    source = _submit(app, target=TARGET_A)["incident_id"]
    survivor = _submit(app, target=TARGET_A)["incident_id"]
    for incident in (source, survivor):
        generation = _incident_state(database, incident)["incident"][
            "control_generation"
        ]
        response = _control(
            app,
            incident,
            "register_remediation",
            generation,
            revision=f"merge-{uuid4().hex}",
            key=f"merge-observation-{uuid4().hex}",
        )
        assert response.status == 200, response.text
        _set_open_running(database, incident)
    return source, survivor


def _merge(app, database: str, source: str, survivor: str, *, text: str | None = None):
    generation = _incident_state(database, source)["incident"]["control_generation"]
    fields = {
        "action": "merge_into",
        "into_incident_id": survivor,
        "expected_generation": str(generation),
        "idempotency_key": f"merge-{uuid4().hex}",
    }
    if text is not None:
        fields["text"] = text
    return post_form(
        app,
        f"/incidents/{source}/control",
        fields,
        headers=UI,
    )


def _authorize_and_merge(app, database: str) -> tuple[str, str, dict, dict]:
    source, survivor = _authorized_open_pair(app, database)
    before_source = _incident_state(database, source)
    before_survivor = _incident_state(database, survivor)
    response = _merge(app, database, source, survivor, text="same incident")
    assert response.status == 200, response.text
    return source, survivor, before_source, before_survivor


def _alert() -> dict:
    evidence = Path(__file__).resolve().parents[2] / "docs/evidence/m1-04-lab"
    return json.loads((evidence / "webhook-firing.json").read_text())["alerts"][0]


def _fire(app, alert: dict, *, fingerprint: str, starts_at: str) -> dict:
    item = deepcopy(alert)
    item["fingerprint"] = fingerprint
    item["startsAt"] = starts_at
    response = post_json(
        app,
        "/intake/alertmanager",
        {"version": "4", "status": "firing", "alerts": [item]},
        headers=bearer(),
    )
    assert response.status == 200, response.text
    return response.json()["results"][0]


def test_pr2_same_target_merge_closes_source_and_preserves_survivor_state(
    app, database
):
    """PR-2 AC1: merge is one source transition plus one survivor event."""
    source, survivor, before_source, before_survivor = _authorize_and_merge(
        app, database
    )
    after_source = _incident_state(database, source)
    after_survivor = _incident_state(database, survivor)

    source_row = after_source["incident"]
    assert source_row["lifecycle"] == "closed"
    assert source_row["state"] == "cancelled"
    assert str(source_row["merged_into_incident_id"]) == survivor
    assert source_row["control_generation"] == (
        before_source["incident"]["control_generation"] + 1
    )
    assert after_source["runs"] and all(
        row["state"] == "cancelled" for row in after_source["runs"]
    )
    assert after_source["sessions"] and all(
        row["state"] == "revoked" for row in after_source["sessions"]
    )

    survivor_row = after_survivor["incident"]
    assert (
        survivor_row["control_generation"]
        == before_survivor["incident"]["control_generation"]
    )
    assert after_survivor["runs"] == before_survivor["runs"]
    assert after_survivor["sessions"] == before_survivor["sessions"]
    assert len(after_survivor["inputs"]) == len(before_survivor["inputs"]) + 1

    event = after_survivor["inputs"][-1]
    assert event["kind"] == "event"
    content = json.dumps(event["content"], sort_keys=True, default=str)
    assert source in content

    audit = after_source["controls"][-1]
    assert audit["action"] == "merge_into"
    assert audit["actor"] == ACTOR
    payload = dict(audit["payload"] or {})
    assert payload["into_incident_id"] == survivor
    assert payload["channel"] == "web"
    assert payload["text"] == "same incident"
    assert payload["idempotency_key"]


def test_pr2_merge_fingerprint_refire_follows_pointer_then_related_new_incident(
    app, database
):
    """PR-2 AC2: U5-2 follows merged pointers, then uses related on all-closed."""
    # Alert intake creates the source identity whose fingerprint is followed.
    alert = _alert()
    fingerprint = uuid4().hex[:16]
    first = _fire(app, alert, fingerprint=fingerprint, starts_at="2026-10-27T10:00:00Z")
    source = first["incident_id"]
    survivor = _submit(app, target=TARGET_A)["incident_id"]
    assert first["outcome"] == "created"

    merged = _merge(app, database, source, survivor)
    assert merged.status == 200, merged.text

    attached = _fire(
        app, alert, fingerprint=fingerprint, starts_at="2026-10-27T10:05:00Z"
    )
    assert attached["outcome"] == "attached"
    assert attached["incident_id"] == survivor

    survivor_generation = _incident_state(database, survivor)["incident"][
        "control_generation"
    ]
    closed = _control(app, survivor, "close", survivor_generation)
    assert closed.status == 200, closed.text

    related = _fire(
        app, alert, fingerprint=fingerprint, starts_at="2026-10-27T10:10:00Z"
    )
    assert related["outcome"] == "created"
    assert related["incident_id"] not in {source, survivor}
    with psycopg.connect(database) as conn:
        row = conn.execute(
            "SELECT related_incident_id FROM opspilot_incidents WHERE incident_id=%s",
            (UUID(related["incident_id"]),),
        ).fetchone()
    assert row[0] == UUID(survivor)


@pytest.mark.parametrize(
    "case",
    ["cross_target", "closed_survivor", "self", "already_merged", "handoff_only"],
)
def test_pr2_merge_preconditions_refuse_without_any_write(app, database, case):
    """PR-2 AC3: every illegal merge precondition is atomic and write-free."""
    if case == "cross_target":
        source = _submit(app, target=TARGET_A)["incident_id"]
        survivor = _submit(app, target=TARGET_B)["incident_id"]
        expected = "TARGET_MISMATCH"
    elif case == "closed_survivor":
        source = _submit(app, target=TARGET_A)["incident_id"]
        survivor = _submit(app, target=TARGET_A)["incident_id"]
        closed = _control(app, survivor, "close", 0)
        assert closed.status == 200, closed.text
        expected = "ILLEGAL_TRANSITION"
    elif case == "self":
        source = _submit(app, target=TARGET_A)["incident_id"]
        survivor = source
        expected = "ILLEGAL_TRANSITION"
    elif case == "already_merged":
        source = _submit(app, target=TARGET_A)["incident_id"]
        survivor = _submit(app, target=TARGET_A)["incident_id"]
        with psycopg.connect(database) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle='closed',state='cancelled',"
                "merged_into_incident_id=%s WHERE incident_id=%s",
                (UUID(survivor), UUID(source)),
            )
        expected = "ILLEGAL_TRANSITION"
    else:
        alert = _alert()
        alert["labels"] = {
            **alert["labels"],
            "namespace": "unregistered",
            "service": "unregistered",
            "service_name": "unregistered",
        }
        response = post_json(
            app,
            "/intake/alertmanager",
            {"version": "4", "status": "firing", "alerts": [alert]},
            headers=bearer(),
        )
        assert response.status == 200, response.text
        source = response.json()["results"][0]["incident_id"]
        assert response.json()["results"][0]["outcome"] == "handoff_created"
        survivor = _submit(app, target=TARGET_A)["incident_id"]
        # The handoff has no target by design. Bind the fixture to the same
        # registered target so this test isolates the HANDOFF_ONLY guard.
        with psycopg.connect(database) as conn:
            target_id = conn.execute(
                "SELECT target_id FROM opspilot_targets WHERE resource_uid=%s",
                (TARGET_A,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE opspilot_incidents SET target_id=%s WHERE incident_id=%s",
                (target_id, UUID(source)),
            )
        expected = "HANDOFF_ONLY"

    before_source = _incident_state(database, source)
    before_survivor = _incident_state(database, survivor)
    response = _merge(app, database, source, survivor)
    assert response.status == 409, response.text
    assert response.json()["code"] == expected
    assert _incident_state(database, source) == before_source
    if survivor != source:
        assert _incident_state(database, survivor) == before_survivor


@pytest.mark.parametrize("action", ["reopen", "rebind_target", "merge_into"])
def test_pr2_merged_source_cannot_be_reopened_rebound_or_merged_again(
    app, database, action
):
    """PR-2 AC4: a closed source with a merge pointer has no undo controls."""
    source = _submit(app, target=TARGET_A)["incident_id"]
    survivor = _submit(app, target=TARGET_A)["incident_id"]
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed',state='cancelled',"
            "merged_into_incident_id=%s WHERE incident_id=%s",
            (UUID(survivor), UUID(source)),
        )
    before = _incident_state(database, source)
    generation = before["incident"]["control_generation"]
    fields: dict[str, str] = {}
    if action == "rebind_target":
        fields["to_target"] = TARGET_B
    elif action == "merge_into":
        fields["into_incident_id"] = survivor
    if action == "merge_into":
        response = _merge(app, database, source, survivor)
    else:
        response = _control(app, source, action, generation, **fields)
    assert response.status == 409, response.text
    assert response.json()["code"] in {"ILLEGAL_TRANSITION", "REOPEN_REQUIRED"}
    assert _incident_state(database, source) == before


def test_pr2_pages_link_source_to_survivor_in_both_directions(app, database):
    """PR-2 AC5: both incident pages expose the merge relationship."""
    source, survivor, _, _ = _authorize_and_merge(app, database)
    source_page = call(app, "GET", f"/incidents/{source}", headers=basic())
    survivor_page = call(app, "GET", f"/incidents/{survivor}", headers=basic())
    assert source_page.status == 200, source_page.text
    assert survivor_page.status == 200, survivor_page.text
    assert "Merged into" in source_page.text
    assert f"/incidents/{survivor}" in source_page.text
    assert "Merged from" in survivor_page.text
    assert f"/incidents/{source}" in survivor_page.text
