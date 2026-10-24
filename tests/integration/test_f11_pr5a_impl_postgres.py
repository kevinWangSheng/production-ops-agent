"""Implementation tests for F11 PR-5a: release pages, controls, projection.

The independent acceptance tests live on their own branch; these pin the
implementer's own edges against a real PostgreSQL through the web role:
the list and detail views (D4-D6), pause/resume/cancel over HTTP with the
D2 status mapping and D3 policy resolution, the incident back-reference
(D9) and the acceptance projection (D8) agreeing with the page.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode
from uuid import uuid4

import psycopg
import pytest

from opspilot.acceptance import IncidentScenario
from opspilot.acceptance_release import release_outcome_from_durable
from opspilot.observation import release_policy
from opspilot.persistence import DurableStore
from opspilot.tools.fixture import fixture_face, fixture_versions
from opspilot.web import Authenticator, create_app
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.service import Workbench
from opspilot.web.store import DurableIncidentStore, DurableWebLedger
from scripts.m0.postgres_lab import DSN
from tests.f11_pr3_support import RELEASE_TOKEN, bearer_for, config
from tests.integration import test_f11_pr2b_impl_postgres as pr2b
from tests.integration import test_f11_pr4b_impl_postgres as pr4b
from tests.integration.test_f11_pr4a_comparison_impl_postgres import (
    _anomalous,
    _drain,
)
from tests.m1_tool_support import NOW, FakeClock
from tests.m1_web_support import basic, call, same_origin

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
release_harness = pr2b.release_harness
HTML = {"accept": "text/html"}


@pytest.fixture
def workbench(release_harness):
    web = release_harness[0]
    store = DurableStore(web.dsn)
    yield Workbench(
        incidents=DurableIncidentStore(store),
        events=DurableEventLog(store),
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face(),
        releases=web,
    )
    store.close()


@pytest.fixture
def app(workbench):
    return create_app(workbench, Authenticator(config()), FakeClock(start=NOW))


def _get(app, path, *, html=False, headers=None):
    merged = {**basic(), **(HTML if html else {}), **(headers or {})}
    return call(app, "GET", path, headers=merged)


def _control(app, oid, action, generation, key=None, *, headers=None):
    fields = {
        "action": action,
        "expected_control_generation": str(generation),
        "idempotency_key": key or uuid4().hex,
    }
    merged = {
        "content-type": "application/x-www-form-urlencoded",
        **basic(),
        **same_origin(),
        **(headers or {}),
    }
    return call(
        app,
        "POST",
        f"/version-changes/{oid}/control",
        headers=merged,
        body=urlencode(fields).encode(),
    )


def _audit_count(oid):
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT count(*) FROM opspilot_release_audit WHERE observation_id=%s",
            (oid,),
        ).fetchone()
    assert row is not None
    return row[0]


def test_list_and_detail_show_the_frozen_versions(release_harness, app):
    web, _, oid, _, _ = release_harness
    release = web.get_release(oid)
    listed = _get(app, "/version-changes")
    assert listed.status == 200
    row = next(r for r in listed.json()["releases"] if r["observation_id"] == str(oid))
    assert row["verdict"] == "pending" and row["concluded"] is False
    page = _get(app, f"/version-changes/{oid}")
    assert page.status == 200
    body = page.json()
    assert body["before"] == release.before_revision.model_dump(mode="json")
    assert body["after"] == release.after_revision.model_dump(mode="json")
    assert body["target"] == release.target_snapshot
    assert body["source_release_id"] == release.source_release_id
    assert body["released_at"] == release.released_at.isoformat()
    assert body["actions"] == ["pause", "cancel"]
    assert body["sessions"] == [] and body["link"] is None
    assert [d["outcome"] for d in body["deliveries"]] == ["created"]
    html = _get(app, f"/version-changes/{oid}", html=True)
    assert html.status == 200
    for digest in (pr2b.DIGEST, pr2b.NEW_DIGEST):
        assert digest in html.text
    assert "does not approve, block or perform a release" in html.text
    assert str(oid) in _get(app, "/version-changes", html=True).text


def test_unknown_and_unauthenticated_reads_are_refused(release_harness, app):
    _, _, oid, _, _ = release_harness
    assert _get(app, f"/version-changes/{uuid4()}").status == 404
    assert _get(app, "/version-changes/not-a-uuid").status == 404
    for path in ("/version-changes", f"/version-changes/{oid}"):
        assert call(app, "GET", path).status == 401
        assert call(app, "GET", path, headers=bearer_for(RELEASE_TOKEN)).status == 401
    before = _audit_count(oid)
    refused = call(
        app,
        "POST",
        f"/version-changes/{oid}/control",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            **bearer_for(RELEASE_TOKEN),
        },
        body=b"action=pause&expected_control_generation=0&idempotency_key=k",
    )
    assert refused.status == 401
    assert _audit_count(oid) == before


def test_pause_resume_cancel_over_http(release_harness, app):
    web, _, oid, policy, _ = release_harness
    paused = _control(app, oid, "pause", 0, "pause-1")
    assert paused.status == 200, paused.text
    assert paused.json()["paused"] is True
    assert paused.json()["control_generation"] == 1
    # Same key, same request: the stored answer.
    again = _control(app, oid, "pause", 0, "pause-1")
    assert again.status == 200 and again.json()["replayed"] is True
    # Same key, another request.
    clash = _control(app, oid, "cancel", 1, "pause-1")
    assert clash.status == 409 and clash.json()["code"] == "IDEMPOTENCY_CONFLICT"
    # A stale form: refused, audited, state unchanged.
    stale = _control(app, oid, "resume", 0)
    assert stale.status == 409
    assert stale.json() == {"code": "CONTROL_CONFLICT", "current_generation": 1}
    assert web.get_release(oid).paused is True
    assert _get(app, f"/version-changes/{oid}").json()["actions"] == [
        "resume",
        "cancel",
    ]
    # Never authorized: resume names the catalog's one matching entry (D3).
    resumed = _control(app, oid, "resume", 1)
    assert resumed.status == 200, resumed.text
    assert resumed.json()["status"] == "observing"
    assert resumed.json()["session_id"] is not None
    assert web.get_release(oid).calibrated_policy_revision == policy.revision
    page = _get(app, f"/version-changes/{oid}").json()
    assert page["calibrated_policy_revision"] == policy.revision
    assert [s["state"] for s in page["sessions"]] == ["authorized"]
    principals = {a["action"]: a["principal_kind"] for a in page["audit"]}
    assert principals["pause"] == "basic_auth" and principals["resume"] == "basic_auth"
    # HTML form posts redirect back to the page.
    cancelled = _control(app, oid, "cancel", 2, headers=HTML)
    assert cancelled.status == 303
    assert cancelled.headers["location"] == f"/version-changes/{oid}"
    assert web.get_release(oid).status == "cancelled"
    terminal = _control(app, oid, "pause", 3)
    assert terminal.status == 409 and terminal.json()["code"] == "RELEASE_TERMINAL"
    assert _get(app, f"/version-changes/{oid}").json()["verdict"] == "cancelled"


@pytest.mark.parametrize(
    "action", ["approve", "block", "rollback", "promote", "deploy", "authorize"]
)
def test_actions_outside_pause_resume_cancel_write_nothing(
    release_harness, app, action
):
    _, _, oid, _, _ = release_harness
    before = _audit_count(oid)
    refused = _control(app, oid, action, 0)
    assert refused.status == 400 and refused.json()["code"] == "INVALID_INPUT"
    assert _audit_count(oid) == before


def test_cross_site_and_malformed_controls_write_nothing(release_harness, app):
    web, _, oid, _, _ = release_harness
    before = _audit_count(oid)
    foreign = _control(app, oid, "pause", 0, headers={"origin": "https://evil.test"})
    assert foreign.status == 403
    assert _control(app, oid, "pause", -1).status == 400
    assert _control(app, oid, "pause", 0, key="system:forged").status == 400
    assert _control(app, uuid4(), "pause", 0).status == 404
    assert _audit_count(oid) == before
    assert web.get_release(oid).paused is False


def test_resume_without_a_calibrated_entry_is_refused_unwritten(
    release_harness, app, tmp_path, monkeypatch
):
    web, _, oid, _, _ = release_harness
    assert _control(app, oid, "pause", 0).status == 200
    empty = tmp_path / "empty.json"
    empty.write_text('{"format_version":1,"policies":[]}')
    monkeypatch.setattr(release_policy, "RELEASE_POLICY_CATALOG_PATH", empty)
    before = _audit_count(oid)
    refused = _control(app, oid, "resume", 1)
    assert refused.status == 422
    assert refused.json()["code"] == "RELEASE_POLICY_UNCALIBRATED"
    assert _audit_count(oid) == before
    assert web.get_release(oid).status == "pending"


def test_degraded_release_links_both_pages_and_the_projection(
    release_harness, workbench, app
):
    web, observer, oid, _, _ = release_harness
    _anomalous(release_harness)
    _drain(observer)
    receipt = pr4b._consume(workbench, oid)
    assert receipt is not None and receipt.disposition == "created"
    page = _get(app, f"/version-changes/{oid}").json()
    assert page["verdict"] == "degraded" and page["concluded"] is True
    assert page["status"] == "anomalous" and page["actions"] == []
    assert page["link"]["incident_id"] == str(receipt.incident_id)
    assert page["incident_id"] == str(receipt.incident_id)
    assert len(page["comparisons"]) == 1
    incident = _get(app, f"/incidents/{receipt.incident_id}", html=True)
    assert incident.status == 200
    assert f"/version-changes/{oid}" in incident.text
    scenario = IncidentScenario(
        scenario_id="pr5a-impl",
        feature_id="F11",
        acceptance_step="4",
        kind="release",
        subject_id=str(oid),
    )
    outcome = release_outcome_from_durable(scenario, web, oid)
    assert outcome.verdict == page["verdict"] == "degraded"
    assert outcome.incident_id == page["incident_id"]
    assert outcome.link_disposition == "created"
    assert outcome.before_revision == page["before"]
    assert outcome.after_revision == page["after"]
    assert outcome.actions == ("authorize", "link_created")
    assert outcome.human_controls == () and outcome.human_interaction is None
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        release_outcome_from_durable(
            IncidentScenario("x", "F11", "4", "release", str(uuid4())), web, oid
        )
