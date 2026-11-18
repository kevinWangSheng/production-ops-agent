"""#343: an alert arriving after a release anomaly opened an incident, opt-in PG.

Independent acceptance tests written from the #343 contract pre-review
(``~/.agent/handoffs/opspilot-prereview-343-2026-10-24.md``: U1 = A claim,
U2 = A, U3 = A, reversible defaults R1-R9, section 4 items 1-4 and 7-10), the
F2 PR-C contract (``docs/tasks/2026-10-21-f2-c-contract.md`` C1-A, C3, R-C3,
R-C4) and F11 U8 / PR-4 P4-04. No implementation branch was read.

Public surface only: the release consumer ``Workbench.release_anomaly_pass``
(preconditions from the F11 PR-4a/4b acceptance helpers, which drive the real
Observer against a fake Prometheus), the ``/intake/alertmanager`` endpoint,
workbench controls and pages, and committed rows read as the owner. No
internal call order is asserted. Concurrency, lifecycle variants, suspension,
successor Runs and grants are in
``tests/integration/test_343_alert_after_release_incident_postgres.py``.

Assumptions where the contract leaves a name or wording open (each also
marked ``ASSUMPTION`` at the assertion):

* S1 the attached event on a release incident keeps the R-C4 content keys
  (R3: "content 键集合与 R-C4 相同"); ``text`` names the fingerprint, the
  normalized startsAt and ``firing`` (as A2 of the PR-C acceptance tests).
* S2 R3: the first attachment to a release incident is not worded as a
  repeat -- its ``text`` does not contain ``again``.
* S3 the release incident's claim priority is ``1`` (F11
  ``RELEASE_INCIDENT_PRIORITY``, U-B1-2 warning); alert severities map as
  ``critical`` -> 0, ``info`` -> 2 (U5-A). Attachment takes the minimum (R4).
* S4 the new alert incident's page links a closed release incident relation
  with ``id="related-incident"`` exactly as for an alert relation (R-C6).
"""

# ruff: noqa: F811 - pytest fixtures imported from the F11 modules
from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot import schema
from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore
from opspilot.web import Workbench
from scripts.m0.postgres_lab import DSN
from tests.acceptance.m1_04_alert_intake_support import EVIDENCE, results, send
from tests.integration.f11_pr4b_consume_postgres_support import (  # noqa: F401 - fixtures
    Case,
    _app,
    _by_intake,
    _case,
    _compare,
    _events,
    _expect_created,
    _incident,
    _link,
    _on_target,
    _ready,
    _record,
    _registry_entry,
    _runs,
    _seed,
    _settle,
    bench,
)
from tests.integration.f11_release_observation_postgres_support import (  # noqa: F401 - fixtures
    Catalog,
    _db_now,
    _rows,
    catalog,
    observer,
    observer_dsn,
    owner,
    web,
    web_dsn,
    worker_dsn,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

T1 = "2026-10-10T10:41:54Z"
T2 = "2026-10-10T10:41:55Z"
T3 = "2026-10-10T10:41:56Z"
T4 = "2026-10-10T10:41:57Z"
#: R-C4 whitelist of an attached event's content, and the keys it must carry.
EVENT_KEYS = {
    "channel",
    "text",
    "fingerprint",
    "starts_at",
    "status",
    "annotation_revision",
    "delivery_key",
}
REQUIRED_EVENT_KEYS = {"channel", "text", "fingerprint", "starts_at", "status"}
RELEASE_PRIORITY = 1  # ASSUMPTION S3
#: The labels of ``webhook-firing.json`` an entry's ``match`` selects on.
MATCH = {"version": 1, "labels": {"namespace": "otel-demo", "service": "checkout"}}
#: The incident columns an attachment never changes (U-B1-2, C3 section 4).
CONTROL_COLUMNS = (
    "state",
    "lifecycle",
    "mode",
    "control_generation",
    "current_run_id",
    "unconcluded",
    "related_incident_id",
    "target_id",
)


@pytest.fixture(scope="module", autouse=True)
def _lab_schema_at_head() -> None:
    """The F11 fixtures talk to the lab DSN; bring it to head first, as the
    integration conftest does for its own directory."""
    schema.migrate(DSN, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))


# ===========================================================================
# helpers
# ===========================================================================


def entry(match: Mapping[str, Any] = MATCH) -> dict[str, Any]:
    """A registry entry for a release target that also binds alerts."""
    return {**_registry_entry(), "match": dict(match)}


@dataclass
class Scene:
    """One target with a release-anomaly case and an app on its workbench."""

    case: Case
    workbench: Workbench
    app: Any

    @property
    def target_id(self) -> UUID:
        return self.case.target_id


def scene(
    owner: DurableStore,
    bench: Callable[..., Workbench],
    case: Case,
    extra: Mapping[str, Mapping[str, Any]] | None = None,
) -> Scene:
    workbench = bench({case.resource_uid: entry(), **(extra or {})})
    return Scene(case=case, workbench=workbench, app=_app(workbench, owner))


def release_first(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> tuple[Scene, UUID, UUID]:
    """The release anomaly is consumed first: incident R with one Run."""
    case = _ready(owner, web, observer, catalog, bench)
    current = scene(owner, bench, case)
    current.workbench.release_anomaly_pass()
    incident, run = _expect_created(case)
    assert incident["priority"] == RELEASE_PRIORITY  # ASSUMPTION S3
    return current, incident["incident_id"], run["run_id"]


def new_alert(severity: str = "critical") -> dict[str, Any]:
    alert = json.loads((EVIDENCE / "webhook-firing.json").read_text())["alerts"][0]
    alert["fingerprint"] = uuid4().hex[:16]
    alert["labels"] = {**alert["labels"], "severity": severity}
    return alert


def fire(app: Any, alert: Mapping[str, Any], starts_at: str, **changes: Any) -> dict:
    (item,) = results(
        send(app, {**deepcopy(dict(alert)), **changes, "startsAt": starts_at})
    )
    return item


def resolve(app: Any, alert: Mapping[str, Any], starts_at: str) -> dict:
    resolved = json.loads((EVIDENCE / "webhook-resolved.json").read_text())["alerts"][0]
    resolved["fingerprint"] = alert["fingerprint"]
    resolved["labels"] = dict(alert["labels"])
    resolved["startsAt"] = starts_at
    (item,) = results(send(app, resolved))
    return item


def deliveries(fingerprint: str) -> list[dict[str, Any]]:
    return _rows(
        "SELECT outcome,incident_id,starts_at_raw FROM opspilot_alert_deliveries "
        "WHERE fingerprint=%s ORDER BY delivery_id",
        (fingerprint,),
    )


def identities(fingerprint: str) -> list[dict[str, Any]]:
    return _rows(
        "SELECT incident_id FROM opspilot_alert_identities WHERE fingerprint=%s",
        (fingerprint,),
    )


def control(workbench: Workbench, incident_id: UUID, action: str) -> Any:
    return workbench.control(
        incident_id,
        actor_id="operator:test",
        action=action,
        expected_generation=int(_incident(incident_id)["control_generation"]),
        idempotency_key=f"{action}-{uuid4()}",
    )


def related(incident_id: str | UUID) -> UUID | None:
    return _incident(UUID(str(incident_id)))["related_incident_id"]


def assert_attached(item: Mapping[str, Any], incident_id: UUID) -> None:
    assert item["outcome"] == "attached", item
    assert item["incident_id"] == str(incident_id), item
    assert item["run_id"] is None, item


def assert_alert_event(
    event: Mapping[str, Any], alert: Mapping[str, Any], starts_at: str
) -> None:
    assert event["kind"] == "event"
    assert event["actor"] == "alertmanager"
    content = event["content"]
    # ASSUMPTION S1: R-C4 key set; required keys present.
    assert set(content) <= EVENT_KEYS, content
    assert REQUIRED_EVENT_KEYS <= set(content), content
    assert content["channel"] == "alertmanager"
    assert content["fingerprint"] == alert["fingerprint"]
    assert content["starts_at"] == starts_at
    assert content["status"] == "firing"
    for needle in (alert["fingerprint"], starts_at, "firing"):
        assert needle in content["text"], content["text"]


# ===========================================================================
# 1. release first: the first alert fingerprint attaches, no second Run
# ===========================================================================


# ===========================================================================
# 2-3. repeats, resolved and a new episode of the claiming fingerprint
# ===========================================================================


# ===========================================================================
# 4. a claimed release incident takes no other fingerprint
# ===========================================================================


# ===========================================================================
# 7. a closed release incident (U2 = A)
# ===========================================================================


# ===========================================================================
# 8. incidents that never take alerts
# ===========================================================================


# ===========================================================================
# 9. an open alert incident and a claimable release incident: latest wins
# ===========================================================================


# ===========================================================================
# 10. alert first: unchanged (PR-C, F11 U8, S4)
# ===========================================================================


# ===========================================================================
# the release incident page after an attachment (R7)
# ===========================================================================
