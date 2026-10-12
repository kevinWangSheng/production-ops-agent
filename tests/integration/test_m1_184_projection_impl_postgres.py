"""Implementer's checks for #184 step 2 on a real PostgreSQL: the committed
rows of an automatic observation, read the way the F6 projection and the
workbench read them, project as a system authorization (r6 C6/C7, D2-13).

The acceptance tests are the independent author's; these pin that the
readers return ``authorization_source`` and that a human registration after
an automatic session is paired with its own session.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import UUID

import pytest

from opspilot.acceptance import RecoveryRecords, recovery_outcome
from opspilot.acceptance_recovery import RecoveryOutcome
from opspilot.observation.store import ObservationStore
from opspilot.persistence import DurableStore
from opspilot.web.service import OBSERVING_NO_CLAIM
from tests.integration.test_m1_184_self_heal_impl_postgres import (  # noqa: F401 - fixtures bound by name
    POOL,
    _mine,
    _published,
    _workbench,
    scratch_dsn,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


@pytest.fixture(scope="module")
def owner(scratch_dsn: str) -> Iterator[DurableStore]:  # noqa: F811
    store = DurableStore(scratch_dsn, pool=POOL)
    store.install()
    yield store
    store.close()


def _outcome(owner: DurableStore, incident: UUID) -> RecoveryOutcome:
    store = ObservationStore(owner.dsn, pool=POOL)
    try:
        records = store.incident_records(incident)
        grants = store.table_privileges()
    finally:
        store.close()
    return recovery_outcome(
        SimpleNamespace(scenario_id="F6:184", subject_id=str(incident)),
        RecoveryRecords(
            incident=records["incident"],
            sessions=tuple(records["sessions"]),
            controls=tuple(records["controls"]),
            grants=grants,
        ),
    )


def test_an_automatic_observation_projects_as_a_system_authorization(
    owner: DurableStore,
) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    (decision,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert decision.state == "authorized"

    outcome = _outcome(owner, incident)
    assert outcome.authorization_source == "automatic"
    assert outcome.authorization_sources == ("automatic",)
    assert outcome.observation_authorization is not None
    assert outcome.observation_authorization["authorization_source"] == "automatic"
    assert "human_control" not in outcome.permissions
    assert outcome.human_interaction is None
    assert "record_handling" not in outcome.actions
    assert not [a for a in outcome.actions if a.startswith("human_")]
    (system,) = outcome.system_authorizations
    assert (system["action"], system["actor"]) == (
        "auto_authorize_observation",
        "system",
    )
    assert outcome.handling_audit == outcome.system_authorizations

    snapshot = workbench.snapshot(incident)
    (session,) = snapshot["observation_sessions"]
    assert session["authorization_source"] == "automatic"
    assert session["target_revision"] is None
    (recovery,) = snapshot["recovery"]
    assert recovery["authorization_source"] == "automatic"
    assert recovery["recovery_status"] == "observing"
    assert recovery["recovery_statement"] == OBSERVING_NO_CLAIM


def test_a_registration_after_the_automatic_session_is_paired_with_its_own(
    owner: DurableStore,
) -> None:
    """D2-13: the registration revoked the automatic session while the
    incident was already ``observing_recovery``; it claims no lifecycle
    move (positional pairing took it for the first session, before which
    the incident was ``open``)."""
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    (decision,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert decision.state == "authorized"
    workbench.control(
        incident,
        actor_id="operator",
        action="register_remediation",
        expected_generation=0,
        idempotency_key="rem-184-step2",
        revision="checkout:v2",
    )

    outcome = _outcome(owner, incident)
    assert outcome.authorization_sources == ("automatic", "post_remediation")
    assert outcome.authorization_source == "post_remediation"
    assert "human_control" in outcome.permissions
    controls = [
        e.action for e in outcome.action_events if e.time_source == "created_at"
    ]
    assert controls == ["record_handling"]
    assert len(outcome.system_authorizations) == 1

    snapshot = workbench.snapshot(incident)
    assert [s["authorization_source"] for s in snapshot["observation_sessions"]] == [
        "automatic",
        "post_remediation",
    ]
    assert [r["recovery_status"] for r in snapshot["recovery"]] == [None, None]
