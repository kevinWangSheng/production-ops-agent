"""Credentials never reach model inputs (PRODUCT-CONSTRAINTS "Data flow
contract": credentials remain outside model inputs and traces).

Every operator- or alert-supplied text a Run sends to the model, driven
through the real web app, store and runner on PostgreSQL with a scripted
model (``tests/workbench_driver.py``): the intake question, Alertmanager
labels and annotations, and workbench messages of every kind (``message``
and its ``follow_up`` / ``correct`` aliases). Each carries the same
name-context credential shapes; none may appear in any model call, and the
non-secret marker next to them must (so the check is not vacuous).
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.acceptance import IncidentScenario
from tests.m1_investigation_support import report_from_transcript
from tests.target_support import AnyTargetRegistry
from tests.workbench_driver import (  # noqa: F401 -- shared pytest fixtures
    TARGET,
    basic,
    bearer,
    call,
    post_json,
    tool_round,
    workbench_database_fixture,
    workbench_fixture,
)

MARKER = "checkout-latency-marker-7731"
#: Name-context credential shapes (a bare secret is out of scope by contract).
SHAPES = {
    "userinfo": "dsn postgres://admin:sk-userinfo-4410@db.internal:5432/app",
    "assignment": "password=sk-assign-4411",
    "bearer": "Authorization: Bearer sk-bearer-4412",
    "scheme": "retried with Bearer sk-scheme-4413 after 401",
}
SECRETS = ("sk-userinfo-4410", "sk-assign-4411", "sk-bearer-4412", "sk-scheme-4413")
TEXT = f"{MARKER}: " + "; ".join(SHAPES.values())


def _sent(model) -> str:
    return json.dumps([call.messages for call in model.calls], default=str)


def _assert_clean(model) -> None:
    sent = _sent(model)
    assert model.calls, "the Run never called the model"
    assert MARKER in sent, "the text never reached the model; the check is vacuous"
    for secret in SECRETS:
        assert secret not in sent, secret


def _run(workbench, scenario):
    _, model = workbench.run(scenario, [tool_round(), report_from_transcript])
    return model


def test_the_intake_question_reaches_the_model_without_credentials(workbench):
    response = post_json(
        workbench.app,
        "/intake/events",
        {
            "source": "alertmanager",
            "external_event_id": f"safety-{uuid4()}",
            "target_id": TARGET,
            "question": TEXT,
        },
        headers=bearer(),
    )
    assert response.status == 201, response.text
    scenario = IncidentScenario(
        scenario_id="safety-question",
        feature_id="F7",
        acceptance_step="data flow: question",
        kind="incident",
        subject_id=response.json()["incident_id"],
    )
    _assert_clean(_run(workbench, scenario))


class _AlertTargets(AnyTargetRegistry):
    """Every alert labelled ``service=checkout`` binds the test target."""

    def match_alert(self, labels):
        return (self.resolve(TARGET),) if labels.get("service") == "checkout" else ()


def test_alert_labels_and_annotations_reach_the_model_without_credentials(
    workbench, workbench_database
):
    workbench.workbench.targets = _AlertTargets()
    response = post_json(
        workbench.app,
        "/intake/alertmanager",
        {
            "version": "4",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": uuid4().hex,
                    "startsAt": workbench.clock.now().isoformat(),
                    "labels": {
                        "alertname": "CheckoutLatencyHigh",
                        "service": "checkout",
                        "note": f"{MARKER} password=sk-assign-4411",
                        "token": "sk-bearer-4412",
                    },
                    "annotations": {
                        "summary": TEXT,
                        "runbook": SHAPES["userinfo"],
                        # a credential name whose value is a JSON container
                        "config": '{"PASSWORD": {"value": "sk-nested-4414"}}',
                    },
                }
            ],
        },
        headers=bearer(),
    )
    assert response.status == 200, response.text
    (result,) = response.json()["results"]
    assert result["run_id"] is not None, result
    scenario = IncidentScenario(
        scenario_id="safety-alert",
        feature_id="F7",
        acceptance_step="data flow: alert",
        kind="incident",
        subject_id=result["incident_id"],
    )
    _assert_clean(_run(workbench, scenario))
    # The stored delivery audit record is redacted too, not only the prompt.
    with psycopg.connect(workbench_database) as conn:
        stored = json.dumps(
            [
                row[0]
                for row in conn.execute(
                    "SELECT alert_json FROM opspilot_alert_deliveries "
                    "WHERE incident_id=%s",
                    (UUID(result["incident_id"]),),
                )
            ],
            default=str,
        )
    assert MARKER in stored
    for secret in (*SECRETS, "sk-nested-4414"):
        assert secret not in stored, secret


@pytest.mark.parametrize("kind", ["message", "follow_up", "correct"])
def test_workbench_messages_reach_the_model_without_credentials(workbench, kind):
    scenario = workbench.from_alert(f"safety-message-{kind}")
    incident = UUID(scenario.subject_id)
    response = workbench.message(incident, TEXT, action=kind)
    assert response.status in {200, 202, 303}, response.text
    _assert_clean(_run(workbench, scenario))


def test_the_evidence_page_never_shows_a_stored_credential(
    workbench, workbench_database
):
    """A stored raw that still carries a credential (a row written before a
    redaction rule existed) is shown redacted; the page is display, not the
    store, so the bytes stay as they are."""
    scenario = workbench.published("safety-evidence-page")
    incident = UUID(scenario.subject_id)
    stale = "sk-legacy-" + "4415"
    legacy = json.dumps({"note": TEXT, "api" + "_key": stale}).encode()
    with psycopg.connect(workbench_database) as conn:
        (evidence_id,) = conn.execute(
            "UPDATE opspilot_evidence SET raw=%s WHERE evidence_id = ("
            "SELECT evidence_id FROM opspilot_evidence WHERE subject_id=%s "
            "AND raw IS NOT NULL LIMIT 1) RETURNING evidence_id",
            (legacy, str(incident)),
        ).fetchone()
    response = call(
        workbench.app,
        "GET",
        f"/incidents/{incident}/evidence/{evidence_id}",
        headers={**basic(), "accept": "application/json"},
    )
    assert response.status == 200, response.text
    assert MARKER in response.text
    for secret in (*SECRETS, stale):
        assert secret not in response.text, secret
