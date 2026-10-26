"""Implementer tests for #347 on PostgreSQL: a release-anomaly incident's
successor Run after an alert episode attached to it.

Brief (V1 = B, V2 = A): the #305 W1-B trigger on the incident's attached
alert deliveries moves the successor's frame to the 24 h ending at its
creation (the control's database instant); the anchor stays
``released_at`` (``release_released_at``), clamped to the frame's start once
the release is older; the ``release_observation`` fact is the first Run's
byte for byte. Without an attachment F11 R6 holds unchanged. The
independent acceptance tests live elsewhere.
"""

# ruff: noqa: F811 - pytest fixtures imported from the F11 modules
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.investigation.context import INPUT_VERSION_RELEASE, InvestigationInput
from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.web import Workbench
from scripts.m0.postgres_lab import DSN
from tests.acceptance.test_343_alert_after_release_incident import (
    assert_attached,
    control,
    fire,
    new_alert,
    release_first,
    resolve,
)
from tests.integration.test_f11_pr4b_consume_postgres import (  # noqa: F401 - fixtures
    _app,
    _fact,
    _incident,
    _run_input,
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

RUN_SECONDS = 600


def _second(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(microsecond=0)


def _z(moment: datetime) -> str:
    return _second(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_now() -> datetime:
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT clock_timestamp()").fetchone()[0]


def _policy(run_input: dict[str, Any]) -> dict[str, Any]:
    return run_input["evidence_context"]["time_policies"][0]


def _runs(incident_id: UUID) -> list[dict[str, Any]]:
    return _rows(
        "SELECT run_id,state,input,deadline FROM opspilot_runs WHERE incident_id=%s "
        "ORDER BY deadline,run_id",
        (incident_id,),
    )


def _released_at(incident_id: UUID) -> datetime:
    (row,) = _rows(
        "SELECT released_at FROM opspilot_release_observations WHERE incident_id=%s",
        (incident_id,),
    )
    return row["released_at"]


def _execute(statement: str, params: tuple[Any, ...]) -> int:
    """An owner write that may touch several rows (``_tamper`` takes one)."""
    with psycopg.connect(DSN) as conn:
        return conn.execute(statement, params).rowcount


def _later(incident_id: UUID, fingerprint: str | None = None) -> None:
    """The fire lands within the second of the consumption; an episode counts
    after the first frame only from the next whole second (#305 item 6), so
    its receipt is set two seconds later."""
    extra, params = "", [incident_id]
    if fingerprint is not None:
        extra, params = " AND fingerprint=%s", [incident_id, fingerprint]
    _tamper(
        "UPDATE opspilot_alert_deliveries SET received_at=received_at + interval "
        f"'2 seconds' WHERE incident_id=%s AND outcome='attached'{extra}",
        tuple(params),
    )


def _attached_release(owner, web, observer, catalog, bench):
    current, incident, run = release_first(owner, web, observer, catalog, bench)
    original = _run_input(run)
    alert = new_alert()
    episode = _db_now() - timedelta(minutes=3)
    assert_attached(fire(current.app, alert, _z(episode)), incident)
    _later(incident)
    # The successor's creation lands in a later second than the
    # consumption, so a moved frame differs from the first one.
    time.sleep(2.2)
    return current, incident, run, original, alert


def _assert_moved(raw, original, *, end: datetime, released: datetime) -> None:
    """Only the first policy's window, anchor and default window moved."""
    start = end - timedelta(hours=24)
    anchor = min(max(_second(released), start), end)
    policy = _policy(raw)
    assert policy["window"] == {"start": start.isoformat(), "end": end.isoformat()}
    assert policy["anchor"] == anchor.isoformat()
    assert policy["anchor_rule"] == "release_released_at"
    assert policy["default_query_window"] == {
        "start": max(anchor - timedelta(hours=1), start).isoformat(),
        "end": end.isoformat(),
    }
    before = _policy(original)
    assert set(policy) == set(before)
    for key in before:
        if key not in ("window", "anchor", "default_query_window"):
            assert policy[key] == before[key], key
    assert (
        raw["evidence_context"]["time_policies"][1:]
        == original["evidence_context"]["time_policies"][1:]
    )
    assert raw["version"] == original["version"] == INPUT_VERSION_RELEASE
    assert _fact(raw) == _fact(original)
    assert "alert_starts_at" not in raw["scope_facts"]
    InvestigationInput.from_json(raw)


def _created(row: dict[str, Any]) -> datetime:
    return _second(row["deadline"] - timedelta(seconds=RUN_SECONDS))


def test_new_run_after_an_attached_alert_is_framed_at_its_creation(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, incident, run, original, _ = _attached_release(
        owner, web, observer, catalog, bench
    )
    control(current.workbench, incident, "cancel")
    control(current.workbench, incident, "new_run")
    first, successor = _runs(incident)
    assert first["run_id"] == run
    _assert_moved(
        successor["input"],
        original,
        end=_created(successor),
        released=_released_at(incident),
    )
    # The anchor itself is the first Run's: the release is recent.
    assert _policy(successor["input"])["anchor"] == _policy(original)["anchor"]
    assert _policy(successor["input"])["window"] != _policy(original)["window"]


def test_follow_up_after_conclusion_moves_the_same_way(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, incident, _, original, _ = _attached_release(
        owner, web, observer, catalog, bench
    )
    _tamper(
        "UPDATE opspilot_incidents SET conclusion=%s::jsonb WHERE incident_id=%s",
        ('{"summary": "347 test conclusion"}', incident),
    )
    current.workbench.control(
        incident,
        actor_id="operator:test",
        action="follow_up",
        expected_generation=int(_incident(incident)["control_generation"]),
        idempotency_key=f"follow-{uuid4()}",
        text="Did the alert come from the release?",
    )
    rows = _runs(incident)
    assert len(rows) == 2
    _assert_moved(
        rows[1]["input"],
        original,
        end=_created(rows[1]),
        released=_released_at(incident),
    )


def test_no_attachment_keeps_the_release_frame_byte_for_byte(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, incident, _ = release_first(owner, web, observer, catalog, bench)
    first = _runs(incident)[0]["input"]
    control(current.workbench, incident, "cancel")
    control(current.workbench, incident, "new_run")
    successor = _runs(incident)[1]["input"]
    assert (
        successor["evidence_context"]["time_policies"]
        == first["evidence_context"]["time_policies"]
    )
    assert _fact(successor) == _fact(first)


def test_repeats_and_resolved_do_not_move_a_moved_frame_but_a_new_episode_does(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, incident, _, original, alert = _attached_release(
        owner, web, observer, catalog, bench
    )
    episode = _rows(
        "SELECT starts_at FROM opspilot_alert_deliveries WHERE incident_id=%s",
        (incident,),
    )[0]["starts_at"]
    control(current.workbench, incident, "cancel")
    control(current.workbench, incident, "new_run")
    moved = _runs(incident)[1]
    end = _created(moved)

    # A repeat and a resolved notice of the attached episode, received after
    # the moved frame's end, add no episode event: the frame stays.
    assert fire(current.app, alert, _z(episode))["outcome"] == "attached"
    assert resolve(current.app, alert, _z(episode))["outcome"] == "resolved_attached"
    assert (
        _execute(
            "UPDATE opspilot_alert_deliveries SET received_at=%s WHERE incident_id=%s "
            "AND delivery_id > (SELECT min(delivery_id) FROM opspilot_alert_deliveries "
            "WHERE incident_id=%s)",
            (end + timedelta(seconds=5), incident, incident),
        )
        == 2
    )
    summary = current.workbench.incidents.find_incident(incident)
    deadline = current.workbench.incidents.now() + timedelta(hours=2)
    kept = current.workbench._successor_input(summary, uuid4(), deadline)
    assert (
        kept["evidence_context"]["time_policies"]
        == moved["input"]["evidence_context"]["time_policies"]
    )

    # A new episode received after the moved frame's end moves it again.
    newest = _db_now() - timedelta(seconds=30)
    assert fire(current.app, alert, _z(newest))["outcome"] == "attached"
    _tamper(
        "UPDATE opspilot_alert_deliveries SET received_at=%s WHERE incident_id=%s "
        "AND starts_at=%s AND status='firing'",
        (end + timedelta(seconds=5), incident, _second(newest)),
    )
    again = current.workbench._successor_input(summary, uuid4(), deadline)
    _assert_moved(
        again,
        original,
        end=_second(deadline - timedelta(seconds=RUN_SECONDS)),
        released=_released_at(incident),
    )


def test_a_successor_a_day_after_the_release_clamps_the_anchor(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, incident, _, original, _ = _attached_release(
        owner, web, observer, catalog, bench
    )
    summary = current.workbench.incidents.find_incident(incident)
    deadline = _released_at(incident) + timedelta(hours=30)
    raw = current.workbench._successor_input(summary, uuid4(), deadline)
    end = _second(deadline - timedelta(seconds=RUN_SECONDS))
    _assert_moved(raw, original, end=end, released=_released_at(incident))
    assert _policy(raw)["anchor"] == (end - timedelta(hours=24)).isoformat()


def test_a_waiting_run_follow_up_requeues_the_same_run_and_input(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, incident, run, original, _ = _attached_release(
        owner, web, observer, catalog, bench
    )
    _tamper("UPDATE opspilot_runs SET state='waiting_human' WHERE run_id=%s", (run,))
    _tamper(
        "UPDATE opspilot_incidents SET state='waiting_human' WHERE incident_id=%s",
        (incident,),
    )
    current.workbench.control(
        incident,
        actor_id="operator:test",
        action="follow_up",
        expected_generation=int(_incident(incident)["control_generation"]),
        idempotency_key=f"follow-{uuid4()}",
        text="look again",
    )
    (row,) = _runs(incident)
    assert row["run_id"] == run
    assert row["input"] == original


def test_fresh_fallback_takes_the_same_frame(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, incident, run, original, _ = _attached_release(
        owner, web, observer, catalog, bench
    )
    _tamper("UPDATE opspilot_runs SET input=NULL WHERE run_id=%s", (run,))
    summary = current.workbench.incidents.find_incident(incident)
    deadline = current.workbench.incidents.now() + timedelta(hours=2)
    raw = current.workbench._successor_input(summary, uuid4(), deadline)
    _assert_moved(
        raw,
        original,
        end=_second(deadline - timedelta(seconds=RUN_SECONDS)),
        released=_released_at(incident),
    )
    assert raw["question"] == original["question"]


def test_an_unreadable_previous_frame_end_is_input_invalid(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    current, incident, run, original, _ = _attached_release(
        owner, web, observer, catalog, bench
    )
    damaged = dict(original)
    damaged["evidence_context"] = {
        **original["evidence_context"],
        "time_policies": [
            {**_policy(original), "window": {"start": "x", "end": "not-a-time"}},
            *original["evidence_context"]["time_policies"][1:],
        ],
    }
    _tamper(
        "UPDATE opspilot_runs SET input=%s::jsonb WHERE run_id=%s",
        (json.dumps(damaged), run),
    )
    summary = current.workbench.incidents.find_incident(incident)
    deadline = current.workbench.incidents.now() + timedelta(hours=2)
    with pytest.raises(PersistenceError) as caught:
        current.workbench._successor_input(summary, uuid4(), deadline)
    assert str(caught.value) == "INPUT_INVALID"
