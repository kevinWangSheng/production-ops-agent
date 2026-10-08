"""The incident page shows the recovery verdict apart from the investigation
conclusion, with the sampling basis behind it (M1-02 step 5, #87).

No PostgreSQL: the in-memory ``IncidentStore`` holds the stored samples the
way ``ObservationStore.session_history`` returns them (built from genuine
Observer samples by ``tests.m1_02_replay_support``). The page is read-only:
it renders what is stored and the offline replay's agreement with it, and
adds no action.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from markupsafe import escape

from opspilot.web.service import CONTROL_ACTIONS
from opspilot.web.store import TargetIdentity, TargetRegistry
from tests.m1_02_replay_support import PROFILE, stored_history, take
from tests.m1_web_support import (
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
    submit_incident,
)


class _UnitTargets(TargetRegistry):
    """Every target id resolves to the unit profile's subject (ns / svc)."""

    def resolve(self, target_id: str) -> TargetIdentity | None:
        return TargetIdentity(
            integration_id="test-integration",
            cluster_uid="test-cluster",
            namespace="ns",
            resource_uid=target_id,
            workload="svc",
            health_profile_id=PROFILE.profile_id,
        )


def _observing_incident(kind: str = "healthy", count: int = 6):
    """An incident whose remediation was registered and whose session holds
    ``count`` stored samples of ``kind`` (healthy ones confirm at the sixth)."""
    app, workbench, clock = build_workbench(
        health_profile=PROFILE, targets=_UnitTargets()
    )
    incident = submit_incident(app).json()["incident_id"]
    registered = post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": "register_remediation",
            "expected_generation": "0",
            "idempotency_key": "rem-1",
            "revision": "svc:v2",
        },
        headers={**basic(), **same_origin()},
    )
    assert registered.status == 200, registered.text
    store = workbench.incidents
    session = store.sessions[0]
    session_id = session["session_id"]
    assert isinstance(session_id, UUID)
    authorized_at = session["authorized_at"]
    taken = [
        take(
            kind,
            sequence=index + 1,
            window_end=authorized_at + timedelta(seconds=300 + 60 * index),
            session_id=session_id,
        )
        for index in range(count)
    ]
    built = stored_history(
        taken,
        session_id=session_id,
        incident_id=UUID(incident),
        authorized_at=authorized_at,
        max_samples=session["max_samples"],
        sustained_window_seconds=session["sustained_window_seconds"],
    )
    for key in (
        "state",
        "ended_reason",
        "adopted_count",
        "adopted_sequence",
        "adopted_window_end",
        "healthy_since",
    ):
        session[key] = built["session"][key]
    session["samples"] = built["samples"]
    session["endings"] = built["endings"]
    store.incidents[UUID(incident)]["lifecycle"] = built["incident_lifecycle"]
    return app, workbench, incident, built


def test_the_page_separates_the_recovery_verdict_from_the_report_and_shows_the_basis():
    app, workbench, incident, built = _observing_incident()
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    text = page.text
    # two distinct sections: the investigation's causal conclusion and the
    # independent recovery verdict
    assert 'id="recovery-verdict"' in text
    assert 'id="investigation-report"' in text
    assert text.index('id="recovery-verdict"') < text.index('id="investigation-report"')
    assert "recovery confirmed" in text
    assert "replay consistent" in text
    # the basis: every sample with its window and verdict, every reading
    # with query, window, source, value and hash
    assert 'id="recovery-basis"' in text
    sample = built["samples"][-1]
    reading = sample["readings"][0]
    assert sample["window_end"].isoformat() in text
    assert str(escape(reading["query"])) in text
    assert reading["raw_sha256"] in text
    assert reading["source"] in text
    assert "recovery_confirmed" in text
    assert PROFILE.revision in text
    # the structured snapshot behind the page
    snapshot = workbench.snapshot(UUID(incident))
    (recovery,) = snapshot["recovery"]
    assert recovery["session_id"] == str(built["session"]["session_id"])
    assert recovery["replay"]["consistent"] is True
    assert recovery["replay"]["recovery_verdict"] == "healthy"
    assert recovery["replay"]["recovery_confirmed"] is True
    assert recovery["replay"]["healthy_window_seconds"] == 600
    assert len(recovery["samples"]) == 6
    last = recovery["samples"][-1]
    assert last["stored_outcome"] == last["replayed_outcome"] == "healthy"
    assert last["transition"] == "recovery_confirmed"
    assert {r["signal_name"] for r in last["readings"]} == {"rate", "errors"}
    errors = next(r for r in last["readings"] if r["signal_name"] == "errors")
    assert errors["stored"] == errors["replayed"] == ("ok", 0.0, 5)
    assert errors["verdict"] == "healthy"
    assert "raw" not in errors
    # read-only: the only forms on the page are the existing control actions
    assert text.count("<form") == len(CONTROL_ACTIONS)


def test_a_tampered_stored_verdict_shows_as_an_integrity_mismatch():
    app, workbench, incident, built = _observing_incident()
    session = workbench.incidents.sessions[0]
    for row in session["samples"]:
        row["outcome"] = "degraded"
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert "integrity mismatch" in page.text
    assert "STORED_OBSERVATION_INTEGRITY_MISMATCH" in page.text
    assert "recovery confirmed" not in page.text
    snapshot = workbench.snapshot(UUID(incident))
    (recovery,) = snapshot["recovery"]
    assert recovery["replay"]["consistent"] is False
    assert recovery["replay"]["recovery_verdict"] == "unknown"
    assert recovery["samples"][0]["stored_outcome"] == "degraded"
    assert recovery["samples"][0]["replayed_outcome"] == "healthy"


def test_a_degraded_session_shows_its_verdict_and_reasons():
    app, workbench, incident, built = _observing_incident("degraded", count=3)
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert "outside healthy bound" in page.text
    snapshot = workbench.snapshot(UUID(incident))
    (recovery,) = snapshot["recovery"]
    assert recovery["replay"]["recovery_verdict"] == "degraded"
    assert recovery["replay"]["recovery_confirmed"] is False
    assert recovery["samples"][0]["replayed_outcome"] == "degraded"


def test_a_session_without_samples_renders_without_a_basis():
    app, workbench, _ = build_workbench(health_profile=PROFILE, targets=_UnitTargets())
    incident = submit_incident(app).json()["incident_id"]
    post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": "register_remediation",
            "expected_generation": "0",
            "idempotency_key": "rem-1",
            "revision": "svc:v2",
        },
        headers={**basic(), **same_origin()},
    )
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert 'id="recovery-verdict"' in page.text
    assert "no samples stored yet" in page.text
    (recovery,) = workbench.snapshot(UUID(incident))["recovery"]
    assert recovery["samples"] == []
    assert recovery["replay"]["recovery_verdict"] == "unknown"


def test_a_malformed_stored_reading_does_not_take_the_page_down():
    """Codex review of PR #139, P2-4: a value the column accepts but the
    domain rejects renders as an integrity mismatch, verdict unknown."""
    app, workbench, incident, _ = _observing_incident()
    session = workbench.incidents.sessions[0]
    session["samples"][0]["readings"][0]["source"] = "BAD SOURCE"
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert "integrity mismatch" in page.text
    (recovery,) = workbench.snapshot(UUID(incident))["recovery"]
    assert recovery["replay"]["recovery_verdict"] == "unknown"
    assert recovery["replay"]["available"] is True
