"""Implementer tests for #305 on PostgreSQL: a successor Run's frame after
an attached episode.

Contract r1 (W1-B, W2-A, R1, R5): when an attached firing episode was
received after the previous Run's frame ended, the successor's frame is
the 24 h ending at its creation (the control's database instant) and is
anchored at that episode's ``startsAt``; otherwise J5 holds. A Run
re-queued in place keeps its input. The independent acceptance tests live
elsewhere.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.investigation.context import InvestigationInput
from opspilot.tools.otel_demo import otel_demo_versions
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_04_alert_window_impl_postgres import (
    _build,
    _policy,
    _post_alert,
    _records,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

RUN_SECONDS = 600


def _now(store) -> datetime:
    with store.transaction() as conn:
        return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _second(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def _generation(workbench, incident_id) -> int:
    return workbench.incidents.find_incident(UUID(incident_id)).control_generation


def _control(workbench, incident_id, action, text=None):
    return workbench.control(
        UUID(incident_id),
        actor_id="operator:test",
        action=action,
        expected_generation=_generation(workbench, incident_id),
        idempotency_key=f"{action}-{uuid4()}",
        **({} if text is None else {"text": text}),
    )


def _runs(incident_id):
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT run_id,state,input,deadline FROM opspilot_runs WHERE incident_id=%s ORDER BY deadline,run_id",
            (UUID(incident_id),),
        ).fetchall()


def _attached_case(store, app):
    """First firing, then a new episode of the same alert attached to it."""
    now = _now(store)
    fingerprint = uuid4().hex[:16]
    first = _post_alert(app, _z(now - timedelta(minutes=40)), fingerprint)
    assert first["outcome"] == "created"
    later = now - timedelta(minutes=2)
    attached = _post_alert(app, _z(later), fingerprint)
    assert attached["outcome"] == "attached"
    assert attached["incident_id"] == first["incident_id"]
    return first, _second(later)


def _assert_moved(raw, *, end, anchor, original):
    policy = _policy(raw)
    assert policy["window"] == {
        "start": (end - timedelta(hours=24)).isoformat(),
        "end": end.isoformat(),
    }
    assert policy["anchor"] == anchor.isoformat()
    assert policy["anchor_rule"] == "alert_starts_at"
    assert policy["default_query_window"] == {
        "start": max(
            anchor - timedelta(hours=1), end - timedelta(hours=24)
        ).isoformat(),
        "end": end.isoformat(),
    }
    assert raw["scope_facts"]["alert_starts_at"] == {
        "anchor": anchor.isoformat(),
        "original": original,
        "adjusted": None,
    }


def test_new_run_after_an_attached_episode_is_framed_at_its_creation():
    app, workbench, store = _build()
    first, anchor = _attached_case(store, app)
    previous = _records(first["incident_id"]).run["input"]
    _control(workbench, first["incident_id"], "cancel")
    _control(workbench, first["incident_id"], "new_run")
    rows = _runs(first["incident_id"])
    assert len(rows) == 2
    successor = rows[1]
    created = _second(successor["deadline"] - timedelta(seconds=RUN_SECONDS))
    raw = successor["input"]
    _assert_moved(raw, end=created, anchor=anchor, original=_z(anchor))
    # R2, R3: nothing else of the input changes; the version stays v4 and
    # the alert context stays the first delivery's.
    assert raw["version"] == previous["version"] == "opspilot-investigation-input-v4"
    assert (
        raw["scope_facts"]["alert_context"] == previous["scope_facts"]["alert_context"]
    )
    assert set(raw["scope_facts"]) == set(previous["scope_facts"])
    assert set(_policy(raw)) == set(_policy(previous))
    assert (
        raw["evidence_context"]["time_policies"][1:]
        == previous["evidence_context"]["time_policies"][1:]
    )
    InvestigationInput.from_json(raw)
    # The attached episode's first five minutes are inside the new frame and
    # beyond the first one's end.
    start = datetime.fromisoformat(_policy(raw)["window"]["start"])
    old_end = datetime.fromisoformat(_policy(previous)["window"]["end"])
    assert start <= anchor and anchor + timedelta(minutes=5) > old_end
    # The Run's versions (tool schema, prompt) are the workbench's, unchanged.
    with psycopg.connect(DSN) as conn:
        versions = conn.execute(
            "SELECT versions FROM opspilot_runs WHERE run_id=%s",
            (successor["run_id"],),
        ).fetchone()[0]
    assert versions == otel_demo_versions()


def test_renewal_after_an_attached_episode_moves_once_then_keeps_the_frame():
    app, workbench, store = _build()
    first, anchor = _attached_case(store, app)
    incident_id = first["incident_id"]
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline = clock_timestamp() - interval '1 second' WHERE run_id=%s",
            (UUID(first["run_id"]),),
        )
    _control(workbench, incident_id, "follow_up", "still failing?")
    rows = _runs(incident_id)
    assert len(rows) == 2
    renewed = rows[1]
    created = _second(renewed["deadline"] - timedelta(seconds=RUN_SECONDS))
    _assert_moved(renewed["input"], end=created, anchor=anchor, original=_z(anchor))

    # After the move: a repeat of the attached episode and a replay of the
    # first add no event (R-C3), so the next successor keeps the moved frame
    # (J5). The episode itself was received before the renewal; the test
    # runs within one second, so its receipt is set a second before the
    # moved frame's end to make that strict.
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_alert_deliveries SET received_at=%s WHERE incident_id=%s AND outcome='attached'",
            (created - timedelta(seconds=1), UUID(incident_id)),
        )
    fingerprint = _records(incident_id).deliveries[0]["fingerprint"]
    assert _post_alert(app, _z(anchor), fingerprint)["outcome"] == "attached"
    first_start = _records(incident_id).deliveries[0]["starts_at_raw"]
    assert _post_alert(app, first_start, fingerprint)["outcome"] == "replayed"
    summary = workbench.incidents.find_incident(UUID(incident_id))
    deadline = workbench.incidents.now() + timedelta(hours=2)
    successor = workbench._successor_input(summary, uuid4(), deadline)
    assert (
        successor["evidence_context"]["time_policies"]
        == renewed["input"]["evidence_context"]["time_policies"]
    )
    assert (
        successor["scope_facts"]["alert_starts_at"]
        == renewed["input"]["scope_facts"]["alert_starts_at"]
    )


def test_a_new_episode_after_the_moved_frame_moves_it_again():
    app, workbench, store = _build()
    first, _ = _attached_case(store, app)
    incident_id = first["incident_id"]
    _control(workbench, incident_id, "cancel")
    _control(workbench, incident_id, "new_run")
    fingerprint = _records(incident_id).deliveries[0]["fingerprint"]
    newest = _second(_now(store) - timedelta(seconds=30))
    assert _post_alert(app, _z(newest), fingerprint)["outcome"] == "attached"
    summary = workbench.incidents.find_incident(UUID(incident_id))
    deadline = workbench.incidents.now() + timedelta(hours=2)
    successor = workbench._successor_input(summary, uuid4(), deadline)
    _assert_moved(
        successor,
        end=_second(deadline - timedelta(seconds=RUN_SECONDS)),
        anchor=newest,
        original=_z(newest),
    )


def test_a_run_requeued_in_place_keeps_its_input():
    """W2-A: notes and resume on a paused, unconcluded Run re-queue the same
    Run with the same input, attached episode or not."""
    app, workbench, store = _build()
    first, _ = _attached_case(store, app)
    incident_id = first["incident_id"]
    before = _runs(incident_id)
    _control(workbench, incident_id, "pause")
    _control(workbench, incident_id, "follow_up", "look again")
    _control(workbench, incident_id, "resume")
    after = _runs(incident_id)
    assert [row["run_id"] for row in after] == [before[0]["run_id"]]
    assert after[0]["state"] == "queued"
    assert after[0]["input"] == before[0]["input"]


def test_fresh_fallback_takes_the_same_anchor():
    app, workbench, store = _build()
    first, anchor = _attached_case(store, app)
    incident_id = first["incident_id"]
    previous = _records(incident_id).run["input"]
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET input=NULL WHERE run_id=%s",
            (UUID(first["run_id"]),),
        )
    summary = workbench.incidents.find_incident(UUID(incident_id))
    deadline = workbench.incidents.now() + timedelta(hours=2)
    successor = workbench._successor_input(summary, uuid4(), deadline)
    _assert_moved(
        successor,
        end=_second(deadline - timedelta(seconds=RUN_SECONDS)),
        anchor=anchor,
        original=_z(anchor),
    )
    assert successor["version"] == previous["version"]
    assert (
        successor["scope_facts"]["alert_context"]
        == previous["scope_facts"]["alert_context"]
    )
    assert successor["question"] == previous["question"]
