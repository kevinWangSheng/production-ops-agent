"""#343: alert after a release-anomaly incident -- concurrency, lifecycle and
control variants, handoffs, suspension, successor Runs and invariants, opt-in
real PostgreSQL.

Independent tests written from the #343 contract pre-review
(``~/.agent/handoffs/opspilot-prereview-343-2026-10-24.md``: U1 = A, U2 = A,
U3 = A, R1-R9, section 4 items 5, 6, 8, 11-14). No implementation branch was
read. The ordinary release-first behaviour is in
``tests/acceptance/test_343_alert_after_release_incident.py``, whose helpers
this module reuses.

Public surface only: ``Workbench.release_anomaly_pass``, ``/intake/alertmanager``,
``Workbench.control`` / suspension, ``DurableStore.rebuild`` and committed rows.

Assumptions (also marked ``ASSUMPTION`` where used):

* S5 the alembic head stays ``0021_release_incident_links`` (head of ``main``
  at 1904345, no migration in #343); a rebase onto a newer migration updates
  this constant, not the assertion.
* S6 the declared grants of the five tables the attach path touches are
  exactly those on ``main`` at 1904345 (no new role privilege, section 1.4).
* S7 a concluded incident is prepared by writing ``conclusion`` as the owner
  (no public path concludes without a model Run).
"""

# ruff: noqa: F811 - pytest fixtures imported from the F11 modules
from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot import schema
from opspilot.investigation.context import INPUT_VERSION_RELEASE
from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore
from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE
from opspilot.web import Workbench
from scripts.m0.postgres_lab import DSN
from tests.acceptance.test_343_alert_after_release_incident import (
    CONTROL_COLUMNS,
    RELEASE_PRIORITY,
    T1,
    T2,
    assert_alert_event,
    assert_attached,
    control,
    deliveries,
    fire,
    new_alert,
    related,
    release_first,
    scene,
)
from tests.acceptance.test_m1_04_alert_intake import results, send
from tests.integration.test_f11_pr4b_consume_postgres import (  # noqa: F401 - fixtures
    _app,
    _cancel_and_new_run,
    _damage_target_row,
    _events,
    _expect_created,
    _fact,
    _incident,
    _link,
    _on_target,
    _ready,
    _run_input,
    _runs,
    _tamper,
    bench,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
    Catalog,
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

ROUNDS = 3
HEAD = "0021_release_incident_links"  # ASSUMPTION S5


def _send_one(app: Any, alert: dict[str, Any], starts_at: str) -> dict[str, Any]:
    (item,) = results(send(app, {**alert, "startsAt": starts_at}))
    return item


# ===========================================================================
# 5. two fingerprints race for one unclaimed release incident
# ===========================================================================


def test_concurrent_first_firings_of_two_fingerprints_claim_once(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    for _round in range(ROUNDS):
        current, release_incident, _ = release_first(
            owner, web, observer, catalog, bench
        )
        x, y = new_alert(), new_alert()
        with ThreadPoolExecutor(max_workers=2) as pool:
            items = list(pool.map(lambda a: _send_one(current.app, a, T1), (x, y)))

        outcomes = sorted(i["outcome"] for i in items)
        assert outcomes == ["attached", "created"], items
        (attached,) = [i for i in items if i["outcome"] == "attached"]
        (created,) = [i for i in items if i["outcome"] == "created"]
        assert attached["incident_id"] == str(release_incident)
        assert created["incident_id"] != str(release_incident)
        assert related(created["incident_id"]) is None
        assert len(_runs(release_incident)) == 1
        alert_events = [
            e for e in _events(release_incident) if e["actor"] == "alertmanager"
        ]
        assert len(alert_events) == 1
        assert alert_events[0]["content"]["fingerprint"] == attached["fingerprint"]


# ===========================================================================
# 6. lifecycle and control variants of the release incident: attach only
# ===========================================================================


def _prepare(variant: str, workbench: Workbench, release_incident: UUID) -> None:
    if variant in ("observing_recovery", "resolved"):
        _tamper(
            "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
            (variant, release_incident),
        )
    elif variant == "human_owned":
        control(workbench, release_incident, "takeover")
        assert _incident(release_incident)["mode"] == "human_owned"
    elif variant == "concluded":  # ASSUMPTION S7
        _tamper(
            "UPDATE opspilot_incidents SET conclusion=%s::jsonb WHERE incident_id=%s",
            ('{"summary": "343 test conclusion"}', release_incident),
        )
    else:  # pragma: no cover
        raise AssertionError(variant)


@pytest.mark.parametrize(
    "variant", ["observing_recovery", "resolved", "human_owned", "concluded"]
)
def test_variants_of_the_release_incident_only_take_the_attachment(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    variant: str,
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    _prepare(variant, current.workbench, release_incident)
    before = _incident(release_incident)
    runs_before = _runs(release_incident)
    events_before = _events(release_incident)
    alert = new_alert("critical")

    item = fire(current.app, alert, T1)

    assert_attached(item, release_incident)
    after = _incident(release_incident)
    for column in CONTROL_COLUMNS:
        assert after[column] == before[column], (variant, column)
    assert after["priority"] == min(before["priority"], 0)
    assert _runs(release_incident) == runs_before
    events = _events(release_incident)
    assert len(events) == len(events_before) + 1
    assert_alert_event(events[-1], alert, T1)


# ===========================================================================
# 8. handoffs are never candidates in either direction
# ===========================================================================


def test_a_release_handoff_incident_is_not_a_candidate(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case = _ready(owner, web, observer, catalog, bench)
    _damage_target_row(case, "missing")
    current = scene(owner, bench, case)
    current.workbench.release_anomaly_pass()
    link = _link(case.oid)
    assert link["disposition"] == "handoff_created", link
    handoff = link["incident_id"]
    before = _incident(handoff)

    created = fire(current.app, new_alert(), T1)

    assert created["outcome"] == "created", created
    assert created["incident_id"] != str(handoff)
    assert related(created["incident_id"]) is None
    assert _incident(handoff) == before
    assert [e for e in _events(handoff) if e["actor"] == "alertmanager"] == []


def test_an_alert_handoff_never_joins_a_release_incident(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    unmatched = bench({})  # the alert's target cannot be resolved here
    app = _app(unmatched, owner)
    before = _incident(release_incident)
    events_before = _events(release_incident)

    item = fire(app, new_alert(), T1)

    assert item["outcome"] == "handoff_created", item
    assert item["incident_id"] != str(release_incident)
    assert related(item["incident_id"]) is None
    assert _incident(release_incident) == before
    assert _events(release_incident) == events_before


# ===========================================================================
# 11. an attachment racing a human close of the release incident
# ===========================================================================


def test_attach_racing_close_is_either_attached_first_or_related_after(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    seen: set[str] = set()
    for _round in range(ROUNDS):
        current, release_incident, _ = release_first(
            owner, web, observer, catalog, bench
        )
        generation = int(_incident(release_incident)["control_generation"])
        alert = new_alert()

        def close() -> Any:
            return current.workbench.control(
                release_incident,
                actor_id="operator:test",
                action="close",
                expected_generation=generation,
                idempotency_key=f"close-{uuid4()}",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            closing = pool.submit(close)
            firing = pool.submit(_send_one, current.app, alert, T1)
            closing.result()
            item = firing.result()

        assert _incident(release_incident)["lifecycle"] == "closed"
        alert_events = [
            e for e in _events(release_incident) if e["actor"] == "alertmanager"
        ]
        if item["outcome"] == "attached":
            assert item["incident_id"] == str(release_incident)
            assert len(alert_events) == 1
        else:
            assert item["outcome"] == "created", item
            assert item["incident_id"] != str(release_incident)
            assert related(item["incident_id"]) == release_incident
            assert alert_events == []
        seen.add(item["outcome"])
    assert seen <= {"attached", "created"}


# ===========================================================================
# 12. suspension does not stop an attachment and starts nothing
# ===========================================================================


def test_a_suspended_target_still_attaches_without_a_run(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    generation = owner.suspension_state()["targets"][current.case.resource_uid][
        "generation"
    ]
    lifted = owner.set_target_suspension(
        current.target_id, True, expected_generation=generation, actor="test:343"
    )
    try:
        runs_before = _runs(release_incident)
        before = _incident(release_incident)

        item = fire(current.app, new_alert(), T1)

        assert_attached(item, release_incident)
        assert _runs(release_incident) == runs_before
        after = _incident(release_incident)
        for column in CONTROL_COLUMNS:
            assert after[column] == before[column], column
        assert release_incident not in owner.claimable_incidents(limit=1000)
    finally:
        owner.set_target_suspension(
            current.target_id, False, expected_generation=lifted, actor="test:343"
        )


def test_global_suspension_still_attaches_without_a_run(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    generation = owner.suspension_state()["global"]["generation"]
    lifted = owner.set_global_suspension(
        True, expected_generation=generation, actor="test:343"
    )
    try:
        runs_before = _runs(release_incident)

        item = fire(current.app, new_alert(), T1)

        assert_attached(item, release_incident)
        assert _runs(release_incident) == runs_before
        assert owner.claimable_incidents(limit=1000) == ()
    finally:
        owner.set_global_suspension(False, expected_generation=lifted, actor="test:343")


# ===========================================================================
# 13. successor Runs keep the release frame and see the attached event
# ===========================================================================


def _policy(run_input: dict[str, Any]) -> dict[str, Any]:
    policies = (run_input.get("evidence_context") or {}).get("time_policies")
    assert policies, run_input.get("evidence_context")
    return policies[0]


def test_a_new_run_after_an_attachment_keeps_the_release_frame_and_sees_the_event(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, release_incident, release_run = release_first(
        owner, web, observer, catalog, bench
    )
    original = _run_input(release_run)
    alert = new_alert()
    assert_attached(fire(current.app, alert, T1), release_incident)
    (event,) = [e for e in _events(release_incident) if e["actor"] == "alertmanager"]

    _cancel_and_new_run(current.workbench, release_incident)

    successor_id = _incident(release_incident)["current_run_id"]
    assert successor_id is not None and successor_id != release_run
    successor = _run_input(successor_id)
    assert successor["version"] == INPUT_VERSION_RELEASE
    assert _fact(successor) == _fact(original)
    # R6: no alert reframe -- still anchored at the release, not at startsAt.
    before, after = _policy(original), _policy(successor)
    assert after["anchor"] == before["anchor"]
    assert (
        after["default_query_window"]["start"]
        == (before["default_query_window"]["start"])
    )
    assert datetime.fromisoformat(after["anchor"]) != datetime.fromisoformat(
        T1.replace("Z", "+00:00")
    )
    # The attached event reaches the successor at its next safe boundary.
    state = owner.rebuild(release_incident)
    assert state["run"]["run_id"] == successor_id
    assert event["input_id"] in {i["input_id"] for i in state["pending_inputs"]}
    # Version strings of the Run are unchanged by the attachment.
    rows = {
        r["run_id"]: r
        for r in _rows(
            "SELECT run_id,versions FROM opspilot_runs WHERE incident_id=%s",
            (release_incident,),
        )
    }
    assert rows[successor_id]["versions"] == rows[release_run]["versions"]


# ===========================================================================
# 14. no migration, no new privilege
# ===========================================================================


def test_no_migration_and_the_lab_is_at_the_same_head() -> None:
    assert schema.head_revision() == HEAD  # ASSUMPTION S5
    with psycopg.connect(DSN) as conn:
        assert schema.current_revision(conn) == HEAD


EXPECTED_GRANTS = {  # ASSUMPTION S6
    WEB_ROLE: {
        "opspilot_alert_deliveries": frozenset(
            {
                "SELECT",
                "INSERT(actor,alert_json,annotation_revision,annotations_sha256,"
                "delivery_key,fingerprint,incident_id,outcome,raw_sha256,received_at,"
                "starts_at,starts_at_raw,status,truncated)",
            }
        ),
        "opspilot_alert_identities": frozenset(
            {
                "SELECT",
                "INSERT(delivery_key,fingerprint,handoff_reason,incident_id,namespace,"
                "run_id,starts_at,starts_at_raw,target_id,workload)",
            }
        ),
        "opspilot_inputs": frozenset(
            {
                "SELECT",
                "INSERT(actor,content,control_generation,incident_id,input_id,kind,"
                "sequence)",
            }
        ),
        "opspilot_incidents": frozenset(
            {
                "SELECT",
                "UPDATE(conclusion,control_generation,current_run_id,lifecycle,mode,"
                "observation_generation,priority,state)",
                "INSERT(current_run_id,incident_id,intake_key,lifecycle,priority,"
                "related_incident_id,state,target_id)",
            }
        ),
        "opspilot_release_incident_links": frozenset(
            {
                "SELECT",
                "INSERT(anomaly_key,comparison_id,disposition,ending_id,"
                "handoff_reason,incident_id,input_id,observation_id,"
                "related_incident_id,run_id)",
            }
        ),
    },
    WORKER_ROLE: {
        "opspilot_alert_deliveries": frozenset(),
        "opspilot_alert_identities": frozenset(),
        "opspilot_inputs": frozenset({"SELECT"}),
        "opspilot_incidents": frozenset({"SELECT", "UPDATE(conclusion)"}),
        "opspilot_release_incident_links": frozenset(),
    },
}


def test_no_new_privilege_on_the_attach_path_tables() -> None:
    for role, tables in EXPECTED_GRANTS.items():
        for table, grants in tables.items():
            assert RUNTIME_GRANTS[role][table] == grants, (role, table)


def test_the_release_priority_constant_is_the_one_assumed() -> None:
    from opspilot.observation.release_commands import RELEASE_INCIDENT_PRIORITY

    assert RELEASE_INCIDENT_PRIORITY == RELEASE_PRIORITY


def test_the_attached_delivery_points_at_the_release_incident_under_the_web_role(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    """The bench runs as ``opspilot_web``: the whole attach path works with
    the existing grants (section 1.4)."""
    current, release_incident, _ = release_first(owner, web, observer, catalog, bench)
    alert = new_alert()

    assert_attached(fire(current.app, alert, T2), release_incident)

    assert [d["incident_id"] for d in deliveries(alert["fingerprint"])] == [
        release_incident
    ]
    assert {r["incident_id"] for r in _on_target(current.target_id)} == {
        release_incident
    }
    assert _expect_created(current.case)[0]["incident_id"] == release_incident
