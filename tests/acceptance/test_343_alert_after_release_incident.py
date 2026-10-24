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
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot import schema
from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore
from opspilot.web import Workbench
from scripts.m0.postgres_lab import DSN
from tests.acceptance.test_m1_04_alert_intake import EVIDENCE, results, send
from tests.integration.test_f11_pr4b_consume_postgres import (  # noqa: F401 - fixtures
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
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
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
from tests.m1_web_support import basic, call

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


def test_first_alert_after_release_incident_attaches_without_a_second_run(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, release_run = release_first(
        owner, web, observer, catalog, bench
    )
    before = _incident(release_incident)
    runs_before = _runs(release_incident)
    events_before = _events(release_incident)
    alert = new_alert("critical")
    marker = "ANNOTATION-SECRET-MARKER-343"

    item = fire(
        current.app,
        alert,
        T1,
        annotations={"summary": marker, "description": marker},
    )

    assert_attached(item, release_incident)
    assert item["starts_at"] == T1
    # One incident and one Run for the fault: no alert incident, no identity.
    assert {r["incident_id"] for r in _on_target(current.target_id)} == {
        release_incident
    }
    assert _runs(release_incident) == runs_before
    assert [r["run_id"] for r in runs_before] == [release_run]
    assert identities(alert["fingerprint"]) == []
    assert deliveries(alert["fingerprint"]) == [
        {"outcome": "attached", "incident_id": release_incident, "starts_at_raw": T1}
    ]
    # Exactly one new event input, whitelisted, without the annotations.
    events = _events(release_incident)
    assert len(events) == len(events_before) + 1
    event = events[-1]
    assert_alert_event(event, alert, T1)
    assert marker not in json.dumps(event["content"])
    # ASSUMPTION S2: the first attachment is not worded as a repeat (R3).
    assert "again" not in event["content"]["text"].lower(), event["content"]["text"]
    # No control field changes; priority only rises (critical -> 0 < 1).
    after = _incident(release_incident)
    for column in CONTROL_COLUMNS:
        assert after[column] == before[column], column
    assert after["priority"] == 0
    # The release association itself is untouched.
    link = _link(current.case.oid)
    assert (link["disposition"], link["incident_id"]) == ("created", release_incident)


def test_a_lower_severity_attachment_never_lowers_the_release_priority(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)

    item = fire(current.app, new_alert("info"), T1)  # info -> 2

    assert_attached(item, release_incident)
    assert _incident(release_incident)["priority"] == RELEASE_PRIORITY


# ===========================================================================
# 2-3. repeats, resolved and a new episode of the claiming fingerprint
# ===========================================================================


def test_repeats_and_resolved_of_the_attached_episode_add_no_event(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    alert = new_alert()
    first = fire(current.app, alert, T1)
    assert_attached(first, release_incident)
    count = len(_events(release_incident))
    before = _incident(release_incident)

    again = fire(current.app, alert, T1)
    changed = fire(
        current.app, alert, T1, annotations={"summary": "revised", "description": "x"}
    )
    done = resolve(current.app, alert, T1)

    for item in (again, changed):
        assert_attached(item, release_incident)
        assert item["delivery_key"] == first["delivery_key"]
    assert done["outcome"] == "resolved_attached", done
    assert done["incident_id"] == str(release_incident)
    assert done["run_id"] is None
    assert len(_events(release_incident)) == count
    assert len(_runs(release_incident)) == 1
    assert _incident(release_incident) == before
    assert [d["outcome"] for d in deliveries(alert["fingerprint"])] == [
        "attached",
        "attached",
        "attached",
        "resolved_attached",
    ]


def test_a_new_episode_of_the_claiming_fingerprint_attaches_again(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    alert = new_alert()
    assert_attached(fire(current.app, alert, T1), release_incident)
    count = len(_events(release_incident))

    second = fire(current.app, alert, T2)

    assert_attached(second, release_incident)
    events = _events(release_incident)
    assert len(events) == count + 1
    assert_alert_event(events[-1], alert, T2)
    assert len(_runs(release_incident)) == 1
    assert identities(alert["fingerprint"]) == []


# ===========================================================================
# 4. a claimed release incident takes no other fingerprint
# ===========================================================================


def test_a_second_fingerprint_opens_its_own_incident_once_the_release_is_claimed(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    x, y = new_alert(), new_alert()
    assert_attached(fire(current.app, x, T1), release_incident)
    before = _incident(release_incident)
    events_before = _events(release_incident)

    created = fire(current.app, y, T1)

    assert created["outcome"] == "created", created
    assert created["incident_id"] != str(release_incident)
    assert created["run_id"]
    assert related(created["incident_id"]) is None
    assert len(_runs(UUID(created["incident_id"]))) == 1
    assert _incident(release_incident) == before
    assert _events(release_incident) == events_before
    # Y's own next episode joins Y's incident, never the claimed release one.
    again = fire(current.app, y, T2)
    assert_attached(again, UUID(created["incident_id"]))
    assert _events(release_incident) == events_before


def test_resolved_claimer_still_holds_the_release_incident(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    """Pre-review section 6 (recommended: the claim is not released when the
    claiming episode resolves)."""
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    x, y = new_alert(), new_alert()
    assert_attached(fire(current.app, x, T1), release_incident)
    assert resolve(current.app, x, T1)["outcome"] == "resolved_attached"

    created = fire(current.app, y, T1)

    assert created["outcome"] == "created", created
    assert created["incident_id"] != str(release_incident)


# ===========================================================================
# 7. a closed release incident (U2 = A)
# ===========================================================================


def test_a_closed_unclaimed_release_incident_becomes_the_relation(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    control(current.workbench, release_incident, "close")
    closed = _incident(release_incident)
    events_before = _events(release_incident)

    created = fire(current.app, new_alert(), T1)

    assert created["outcome"] == "created", created
    assert created["incident_id"] != str(release_incident)
    assert created["run_id"]
    assert related(created["incident_id"]) == release_incident
    # Never reopened, nothing appended.
    assert _incident(release_incident) == closed
    assert closed["lifecycle"] == "closed"
    assert _events(release_incident) == events_before
    # ASSUMPTION S4: the new incident's page links the release incident.
    page = call(
        current.app, "GET", f"/incidents/{created['incident_id']}", headers=basic()
    )
    assert page.status == 200, page.text
    link = re.search(
        r'<a[^>]*href="(/incidents/[^"]+)"[^>]*id="related-incident"|'
        r'<a[^>]*id="related-incident"[^>]*href="(/incidents/[^"]+)"|'
        r'id="related-incident"[^>]*>\s*<a[^>]*href="(/incidents/[^"]+)"',
        page.text,
    )
    assert link, "related-incident must link to the release incident"
    assert next(g for g in link.groups() if g) == f"/incidents/{release_incident}"


def test_a_closed_release_incident_claimed_by_another_fingerprint_is_not_related(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    y = new_alert()
    assert_attached(fire(current.app, y, T1), release_incident)
    control(current.workbench, release_incident, "close")

    created = fire(current.app, new_alert(), T1)

    assert created["outcome"] == "created", created
    assert related(created["incident_id"]) is None


def test_the_claiming_fingerprint_relates_to_its_closed_release_incident(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    x = new_alert()
    assert_attached(fire(current.app, x, T1), release_incident)
    control(current.workbench, release_incident, "close")

    created = fire(current.app, x, T2)

    assert created["outcome"] == "created", created
    assert related(created["incident_id"]) == release_incident
    assert _incident(release_incident)["lifecycle"] == "closed"


# ===========================================================================
# 8. incidents that never take alerts
# ===========================================================================


def test_a_manual_incident_on_the_target_is_not_a_candidate(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)  # target only; nothing consumed
    manual = _seed(owner, case.target_id)
    closed_manual = _seed(
        owner,
        case.target_id,
        lifecycle="closed",
        created_at=_db_now() - timedelta(minutes=5),
    )
    current = scene(owner, bench, case)
    before = {i: _incident(i) for i in (manual, closed_manual)}

    created = fire(current.app, new_alert(), T1)

    assert created["outcome"] == "created", created
    assert created["incident_id"] not in {str(manual), str(closed_manual)}
    assert related(created["incident_id"]) is None
    for incident_id, row in before.items():
        assert _incident(incident_id) == row
        assert _events(incident_id) == []


def test_an_incident_a_release_attached_to_is_not_a_release_incident(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    """R1: only ``created``/``created_related`` links make a release incident;
    a manual incident the anomaly attached to stays manual."""
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    manual = _seed(owner, case.target_id)
    _compare(observer, case)
    current = scene(owner, bench, case)
    current.workbench.release_anomaly_pass()
    assert (_link(case.oid)["disposition"], _link(case.oid)["incident_id"]) == (
        "attached",
        manual,
    )
    events_before = _events(manual)

    created = fire(current.app, new_alert(), T1)

    assert created["outcome"] == "created", created
    assert created["incident_id"] != str(manual)
    assert related(created["incident_id"]) is None
    assert _events(manual) == events_before


def test_a_release_incident_on_another_target_is_not_a_candidate(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    elsewhere = _case(owner, web, observer, catalog)
    _compare(observer, elsewhere)
    alert_target = _record(owner, web)  # a target with a pending release only
    resource_uid = _rows(
        "SELECT resource_uid FROM opspilot_targets WHERE target_id=%s",
        (alert_target.target_id,),
    )[0]["resource_uid"]
    other_match = {"version": 1, "labels": {"service": "elsewhere-343"}}
    workbench = bench(
        {elsewhere.resource_uid: entry(other_match), resource_uid: entry()}
    )
    app = _app(workbench, owner)
    workbench.release_anomaly_pass()
    release_incident, _ = _expect_created(elsewhere)
    events_before = _events(release_incident["incident_id"])

    created = fire(app, new_alert(), T1)

    assert created["outcome"] == "created", created
    assert created["incident_id"] != str(release_incident["incident_id"])
    assert _incident(UUID(created["incident_id"]))["target_id"] == (
        alert_target.target_id
    )
    assert related(created["incident_id"]) is None
    assert _events(release_incident["incident_id"]) == events_before


# ===========================================================================
# 9. an open alert incident and a claimable release incident: latest wins
# ===========================================================================


def test_a_newer_claimable_release_incident_wins_over_an_older_alert_incident(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    current = scene(owner, bench, case)
    x = new_alert()
    opened = fire(current.app, x, T1)
    assert opened["outcome"] == "created", opened
    alert_incident = UUID(opened["incident_id"])
    control(current.workbench, alert_incident, "close")
    _compare(observer, case)
    current.workbench.release_anomaly_pass()  # closed candidate only
    link = _link(case.oid)
    assert link["disposition"] == "created_related", link
    release_incident = link["incident_id"]
    control(current.workbench, alert_incident, "reopen")
    assert _incident(alert_incident)["lifecycle"] == "open"
    assert (
        _incident(release_incident)["created_at"]
        > _incident(alert_incident)["created_at"]
    )
    alert_events = _events(alert_incident)

    item = fire(current.app, x, T2)

    assert_attached(item, release_incident)
    assert _events(alert_incident) == alert_events


def test_a_newer_alert_incident_wins_over_an_older_claimed_release_incident(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    x = new_alert()
    assert_attached(fire(current.app, x, T1), release_incident)
    control(current.workbench, release_incident, "close")
    created = fire(current.app, x, T2)
    assert created["outcome"] == "created"
    alert_incident = UUID(created["incident_id"])
    control(current.workbench, release_incident, "reopen")
    assert _incident(release_incident)["lifecycle"] == "open"
    release_events = _events(release_incident)

    item = fire(current.app, x, T3)

    assert_attached(item, alert_incident)
    assert _events(release_incident) == release_events


# ===========================================================================
# 10. alert first: unchanged (PR-C, F11 U8, S4)
# ===========================================================================


def test_alert_first_the_release_attaches_and_other_fingerprints_stay_apart(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    current = scene(owner, bench, case)
    x, y = new_alert(), new_alert()
    opened = fire(current.app, x, T1)
    assert opened["outcome"] == "created", opened
    alert_incident = UUID(opened["incident_id"])
    before = _incident(alert_incident)
    _compare(observer, case)

    current.workbench.release_anomaly_pass()

    # S4: the release joins the alert incident, no release incident or Run.
    link = _link(case.oid)
    assert (link["disposition"], link["incident_id"]) == ("attached", alert_incident)
    assert _by_intake(case.intake_key) == []
    assert len(_runs(alert_incident)) == 1
    assert _incident(alert_incident)["priority"] == before["priority"]
    # X's next episode joins its own incident (PR-C unchanged) ...
    assert_attached(fire(current.app, x, T2), alert_incident)
    # ... and another fingerprint still opens its own incident.
    other = fire(current.app, y, T1)
    assert other["outcome"] == "created", other
    assert other["incident_id"] != str(alert_incident)
    assert related(other["incident_id"]) is None
    assert {r["incident_id"] for r in _on_target(case.target_id)} == {
        alert_incident,
        UUID(other["incident_id"]),
    }


# ===========================================================================
# the release incident page after an attachment (R7)
# ===========================================================================


def test_the_release_incident_page_renders_after_an_attachment(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    assert_attached(fire(current.app, new_alert(), T1), release_incident)

    page = call(current.app, "GET", f"/incidents/{release_incident}", headers=basic())

    assert page.status == 200, page.text
    assert str(release_incident) in page.text
    assert 'id="related-incident"' not in page.text
