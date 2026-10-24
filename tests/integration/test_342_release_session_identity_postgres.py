"""#342: a release observation session on a target does not block the
recovery observations of incidents on the same target.

``authorize_session_in`` checks that every earlier session on the target
recorded the registry's identity. A release session stores the target
snapshot without ``revision`` (F11 PR-2a); the check compares the four
identity fields only (user decision 2026-10-07), so both the human
registration and the #184 automatic authorization succeed after a release
session, and an earlier session that recorded another identity is still
``TARGET_MISMATCH``.
"""

# ruff: noqa: F811 - pytest fixtures imported from the PR-2a module
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.observer.health_profile import (
    PROFILE_DIRECTORY,
    canonical_content,
    load_health_profile,
)
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.web import (
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
)
from scripts.m0.postgres_lab import DSN
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
    POOL,
    Catalog,
    Observing,
    _observing,
    catalog,
    owner,
    web,
    web_dsn,
)
from tests.integration.test_m1_184_self_heal_impl_postgres import ui_envelope
from tests.target_support import ANY_TARGETS, identity_of

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PROFILE = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
VERSIONS = {"state": "v1"}


@pytest.fixture(scope="module")
def controller() -> Iterator[ObservationStore]:
    store = ObservationStore(DSN, pool=POOL)
    yield store
    store.close()


def _released(owner: DurableStore, web: ReleaseStore, catalog: Catalog) -> Observing:
    observing = _observing(owner, web, catalog)
    with owner.transaction(snapshot=True) as conn:
        (stored,) = conn.execute(
            "SELECT target FROM opspilot_observation_sessions WHERE target_id=%s AND purpose='release_observation'",
            (observing.release.target_id,),
        ).fetchall()
    assert "revision" not in stored["target"], "the shape #342 is about"
    return observing


def _incident_on(owner: DurableStore, target_id: UUID) -> UUID:
    incident = uuid4()
    owner.accept(
        incident,
        uuid4(),
        f"r342-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(hours=1),
        budget_limit=10,
        versions=VERSIONS,
        target_id=target_id,
    )
    return incident


def _register(
    controller: ObservationStore,
    incident: UUID,
    resource_uid: str,
    **identity: str,
) -> int:
    parameters = PROFILE.session
    return controller.register_remediation(
        incident,
        expected_generation=0,
        actor="operator",
        revision="checkout:v2",
        deadline_at=datetime.now(timezone.utc)
        + timedelta(seconds=parameters.deadline_seconds),
        max_samples=parameters.max_samples,
        sample_interval_seconds=parameters.sample_interval_seconds,
        sustained_window_seconds=parameters.sustained_window_seconds,
        health_profile_revision=PROFILE.revision,
        health_profile=canonical_content(PROFILE),
        identity={**identity_of(resource_uid), **identity},
        payload={"channel": "test"},
    )


def _incident_sessions(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return list(
            conn.execute(
                "SELECT state,authorization_source,target FROM opspilot_observation_sessions WHERE incident_id=%s",
                (incident,),
            ).fetchall()
        )


def _workbench(owner: DurableStore) -> Workbench:
    return Workbench(
        incidents=DurableIncidentStore(owner),
        events=DurableEventLog(owner),
        evidence=DurableEvidenceStore(owner),
        ledger=DurableWebLedger(owner),
        run_versions=VERSIONS,
        targets=ANY_TARGETS,
        run_seconds=600,
        health_profile=PROFILE,
    )


def _published_on(workbench: Workbench, owner: DurableStore, resource_uid: str) -> UUID:
    incident = workbench.submit(
        ui_envelope(target_id=resource_uid, idempotency_key=f"r342-{uuid4()}")
    ).incident_id
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET conclusion=%s::jsonb WHERE incident_id=%s",
            ('{"kind": "test"}', incident),
        )
        conn.execute(
            "UPDATE opspilot_runs r SET state='completed',control_generation=i.control_generation FROM opspilot_incidents i WHERE i.incident_id=%s AND r.run_id=i.current_run_id",
            (incident,),
        )
    return incident


def test_a_human_registration_after_a_release_session_is_authorized(
    owner: DurableStore,
    web: ReleaseStore,
    catalog: Catalog,
    controller: ObservationStore,
) -> None:
    release = _released(owner, web, catalog).release
    incident = _incident_on(owner, release.target_id)

    assert _register(controller, incident, release.identity["resource_uid"]) == 1

    (session,) = _incident_sessions(owner, incident)
    assert (session["state"], session["authorization_source"]) == (
        "authorized",
        "post_remediation",
    )
    assert session["target"]["revision"] == "checkout:v2"


def test_the_automatic_authorization_after_a_release_session_is_authorized(
    owner: DurableStore, web: ReleaseStore, catalog: Catalog
) -> None:
    release = _released(owner, web, catalog).release
    workbench = _workbench(owner)
    incident = _published_on(workbench, owner, release.identity["resource_uid"])

    outcomes = [
        o for o in workbench.self_heal_pass(limit=50) if o.incident_id == incident
    ]

    assert [(o.state, o.reason) for o in outcomes] == [("authorized", None)]
    (session,) = _incident_sessions(owner, incident)
    assert (session["state"], session["authorization_source"]) == (
        "authorized",
        "automatic",
    )


def test_a_release_session_recorded_with_another_identity_still_mismatches(
    owner: DurableStore,
    web: ReleaseStore,
    catalog: Catalog,
    controller: ObservationStore,
) -> None:
    """The registry row now names another cluster (the configured identity
    agrees with it); the release session recorded the old one."""
    release = _released(owner, web, catalog).release
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_targets SET cluster_uid='another-cluster' WHERE target_id=%s",
            (release.target_id,),
        )
    incident = _incident_on(owner, release.target_id)

    with pytest.raises(PersistenceError, match="TARGET_MISMATCH"):
        _register(
            controller,
            incident,
            release.identity["resource_uid"],
            cluster_uid="another-cluster",
        )

    assert _incident_sessions(owner, incident) == []
