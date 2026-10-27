"""Independent #347 contract tests, against main before implementation.

Only public intake/control/store operations, committed PG rows and the public
tool gateway are observed. Existing F11/#343 fixtures provide real web-role
release consumption and alert intake. Owner writes below establish clock/state
preconditions or deliberately damage a stored snapshot, never patch decisions.
Acceptance criterion numbers refer to the ordered list in issue #347's brief.
"""

# ruff: noqa: F811 - imported pytest fixtures
from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from uuid import uuid4

import pytest

from opspilot.investigation.context import InvestigationInput
from opspilot.tools import Window
from opspilot.tools.otel_demo import otel_demo_face, otel_demo_versions
from opspilot.web import DurableClock
from opspilot.web.service import WorkbenchError
from tests.acceptance.test_343_alert_after_release_incident import (
    assert_attached,
    control,
    fire,
    new_alert,
    release_first,
    resolve,
    scene,
)
from tests.integration.test_305_successor_window_postgres import (
    _db_now,
    _frame,
    _instant,
    _query,
    _text,
)
from tests.integration.test_f11_pr4b_consume_postgres import (  # noqa: F401
    _cancel_and_new_run,
    _case,
    _compare,
    _expect_created,
    _fact,
    _incident,
    _link,
    _ready,
    _tamper,
    bench,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401
    _rows,
    catalog,
    observer,
    observer_dsn,
    owner,
    web,
    web_dsn,
)
from tests.m1_web_support import basic, call

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
FRAME = timedelta(hours=24)
RUN_WALL = timedelta(seconds=600)


def _policy(raw):
    return raw["evidence_context"]["time_policies"][0]


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _run(run_id):
    (row,) = _rows(
        "SELECT run_id,input,input::text AS raw,deadline,versions,state "
        "FROM opspilot_runs WHERE run_id=%s",
        (run_id,),
    )
    return row


def _current(incident):
    return _run(_incident(incident)["current_run_id"])


def _after_second(moment):
    """Wait only to separate real receipt/creation seconds (no frozen web clock)."""
    remaining = (
        moment.replace(microsecond=0) + timedelta(seconds=1.05) - _db_now()
    ).total_seconds()
    if remaining > 0:
        time.sleep(remaining)
    assert _db_now().replace(microsecond=0) > moment.replace(microsecond=0)


def _attach(current, incident, previous, alert=None):
    _after_second(_instant(_policy(previous["input"])["window"]["end"]))
    alert = alert or new_alert()
    starts = _text(_db_now() - timedelta(seconds=1))
    assert_attached(fire(current.app, alert, starts), incident)
    return alert, starts


def _renew(current, incident, action="follow_up"):
    return current.workbench.control(
        incident,
        actor_id="operator:test",
        action=action,
        expected_generation=int(_incident(incident)["control_generation"]),
        idempotency_key=f"347-{uuid4()}",
        text="Investigate the attached alert episode",
    )


def _assert_reframed(previous, successor, *, clamped=False):
    """AC1/7/10: creation frame; release fact stays on its consumption anchor."""
    old, raw = previous["input"], successor["input"]
    before, after = _policy(old), _policy(raw)
    end = _instant(after["window"]["end"])
    created = _instant(successor["deadline"]) - RUN_WALL
    assert end == created.replace(microsecond=0), (end, created)
    assert end > _instant(before["window"]["end"])
    start = _instant(after["window"]["start"])
    assert end - start == FRAME
    anchor = start if clamped else _instant(before["anchor"])
    assert _instant(after["anchor"]) == anchor
    assert after["anchor_rule"] == before["anchor_rule"] == "release_released_at"
    assert _instant(after["default_query_window"]["start"]) == max(
        anchor - timedelta(hours=1), start
    )
    assert _instant(after["default_query_window"]["end"]) == end
    # Exactly the three allowed policy fields change, including future policies.
    changed = {"window", "anchor", "default_query_window"}
    assert {k: v for k, v in after.items() if k not in changed} == {
        k: v for k, v in before.items() if k not in changed
    }
    assert (
        raw["evidence_context"]["time_policies"][1:]
        == old["evidence_context"]["time_policies"][1:]
    )
    assert _bytes(_fact(raw)) == _bytes(_fact(old))
    assert set(raw["scope_facts"]) == set(old["scope_facts"])
    assert "alert_starts_at" not in raw["scope_facts"]
    for key in old["scope_facts"]:
        if key != "deadline":
            assert raw["scope_facts"][key] == old["scope_facts"][key], key
    assert raw["version"] == old["version"] == "opspilot-investigation-input-v6"
    assert successor["versions"] == previous["versions"]
    InvestigationInput.from_json(raw)


@pytest.fixture
def released(owner, web, observer, catalog, bench):
    current, incident, run = release_first(owner, web, observer, catalog, bench)
    return current, incident, _run(run)


def test_ac1_cancel_new_run_reframes_but_preserves_release_fact(released):
    current, incident, first = released
    _attach(current, incident, first)
    _cancel_and_new_run(current.workbench, incident)
    _assert_reframed(first, _current(incident))


@pytest.mark.parametrize(
    "state,action",
    [("concluded", "follow_up"), ("overdue", "follow_up"), ("overdue", "correct")],
)
def test_ac2_concluded_or_overdue_notes_reframe(released, owner, state, action):
    current, incident, first = released
    if state == "concluded":
        # The public store publication seam; no live model is needed to
        # establish the conclusion that gates this web-side renewal.
        lease = owner.claim(incident, first["run_id"], uuid4(), first["versions"])
        conclusion = {"summary": "release investigated"}
        step = owner.commit_step(lease, "347-conclusion", conclusion)
        assert owner.publish(lease, conclusion, step_id=step)
        assert owner.rebuild(incident)["conclusion"] == conclusion
    else:
        _tamper(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (first["run_id"],),
        )
    _attach(current, incident, first)
    _renew(current, incident, action)
    assert _current(incident)["run_id"] != first["run_id"]
    _assert_reframed(first, _current(incident))


def test_ac3_without_attached_alert_time_policies_are_byte_identical(released):
    current, incident, first = released
    _after_second(_instant(_policy(first["input"])["window"]["end"]))
    _cancel_and_new_run(current.workbench, incident)
    assert _bytes(
        _current(incident)["input"]["evidence_context"]["time_policies"]
    ) == _bytes(first["input"]["evidence_context"]["time_policies"])


def test_ac4_second_release_attachment_alone_does_not_reframe(
    released, owner, web, observer, catalog, monkeypatch
):
    current, incident, first = released
    # Same real target, distinct source release/anomaly. Reuse the PR-4b
    # public release registration precondition rather than forging a link.
    from tests.integration import test_f11_pr4a_comparison_postgres as support
    from tests.target_support import IDENTITY, WORKLOAD

    identity = {
        **IDENTITY,
        "resource_uid": current.case.resource_uid,
        "workload": WORKLOAD,
    }
    monkeypatch.setattr(support, "_target", lambda _: (current.target_id, identity))
    second = _case(owner, web, observer, catalog)
    _compare(observer, second)
    current.workbench.release_anomaly_pass()
    link = _link(second.oid)
    assert (link["disposition"], link["incident_id"]) == ("attached", incident)
    _after_second(_instant(_policy(first["input"])["window"]["end"]))
    _cancel_and_new_run(current.workbench, incident)
    assert _policy(_current(incident)["input"]) == _policy(first["input"])


@pytest.mark.parametrize("offset_us,should_move", [(900000, False), (1000000, True)])
def test_ac5_receipt_comparison_uses_strict_utc_seconds(
    released, offset_us, should_move
):
    current, incident, first = released
    alert = new_alert()
    assert_attached(
        fire(current.app, alert, _text(_db_now() - timedelta(minutes=2))), incident
    )
    end = _instant(_policy(first["input"])["window"]["end"])
    # Deterministic boundary precondition, confined to this delivery row.
    _tamper(
        "UPDATE opspilot_alert_deliveries SET received_at=%s WHERE fingerprint=%s",
        (end + timedelta(microseconds=offset_us), alert["fingerprint"]),
    )
    _after_second(end)
    _cancel_and_new_run(current.workbench, incident)
    if should_move:
        _assert_reframed(first, _current(incident))
    else:
        assert _policy(_current(incident)["input"]) == _policy(first["input"])


def test_ac5_repeat_and_resolved_after_covered_episode_do_not_reframe(released):
    current, incident, first = released
    alert, starts = _attach(current, incident, first)
    # Establish an episode already covered by the previous frame. Later
    # duplicates must not be mistaken for a fresh first firing.
    end = _instant(_policy(first["input"])["window"]["end"])
    _tamper(
        "UPDATE opspilot_alert_deliveries SET received_at=%s WHERE fingerprint=%s",
        (end - timedelta(seconds=1), alert["fingerprint"]),
    )
    _after_second(end)
    assert_attached(
        fire(current.app, alert, starts, annotations={"summary": "repeat"}), incident
    )
    assert resolve(current.app, alert, starts)["outcome"] == "resolved_attached"
    _cancel_and_new_run(current.workbench, incident)
    assert _policy(_current(incident)["input"]) == _policy(first["input"])


def test_ac5_successors_compare_against_immediately_previous_frame(released):
    current, incident, first = released
    alert, starts = _attach(current, incident, first)
    _cancel_and_new_run(current.workbench, incident)
    second = _current(incident)
    _assert_reframed(first, second)
    _attach(current, incident, second, alert)
    _cancel_and_new_run(current.workbench, incident)
    third = _current(incident)
    _assert_reframed(second, third)
    # No further episode: even later creation must keep the third frame.
    _after_second(_instant(_policy(third["input"])["window"]["end"]))
    _cancel_and_new_run(current.workbench, incident)
    assert _policy(_current(incident)["input"]) == _policy(third["input"])


def test_ac6_gateway_allows_post_consumption_episode_only_on_successor(
    owner, web, observer, catalog, bench, monkeypatch
):
    def metrics_bench(entries):
        workbench = bench(entries)
        workbench.tool_face = otel_demo_face(DurableClock(owner))
        workbench.run_versions = otel_demo_versions()
        return workbench

    current, incident, run_id = release_first(
        owner, web, observer, catalog, metrics_bench
    )
    first = _run(run_id)
    _after_second(_instant(_policy(first["input"])["window"]["end"]))
    episode = _db_now().replace(microsecond=0)
    _after_second(episode)
    assert_attached(fire(current.app, new_alert(), _text(episode)), incident)
    _cancel_and_new_run(current.workbench, incident)
    successor = _current(incident)
    query = Window(episode - timedelta(minutes=5), episode + timedelta(seconds=1))
    assert query.end > _frame(first["input"]).end
    inside = Window(query.start - timedelta(minutes=5), query.start)
    allowed, sent = _query(monkeypatch, incident, first, inside)
    assert allowed.status != "denied" and len(sent) == 1, allowed
    refused, sent = _query(monkeypatch, incident, first, query)
    assert (refused.status, refused.reason) == ("denied", "WINDOW_OUT_OF_SCOPE")
    assert sent == []
    allowed, sent = _query(monkeypatch, incident, successor, query)
    assert allowed.reason != "WINDOW_OUT_OF_SCOPE", allowed
    assert allowed.status != "denied", allowed
    assert len(sent) == 1


def test_ac7_old_release_clamps_anchor_but_keeps_consumption_fact(
    owner, web, observer, catalog, bench
):
    case = _ready(owner, web, observer, catalog, bench)
    # Model a timely accepted release whose anomaly is consumed after a
    # backlog. Sending a 25h-old webhook instead would correctly be rejected
    # as late_delivery and could never establish this incident precondition.
    _tamper(
        "UPDATE opspilot_release_observations "
        "SET released_at=released_at-interval '25 hours', "
        "deadline_at=deadline_at-interval '25 hours', "
        "received_at=received_at-interval '25 hours', "
        "ended_at=ended_at-interval '25 hours' WHERE observation_id=%s",
        (case.oid,),
    )
    current = scene(owner, bench, case)
    current.workbench.release_anomaly_pass()
    incident_row, run = _expect_created(case)
    incident, first = incident_row["incident_id"], _run(run["run_id"])
    assert _fact(first["input"])["anchor"]["adjusted"] == "before_frame"
    _attach(current, incident, first)
    _cancel_and_new_run(current.workbench, incident)
    second = _current(incident)
    _assert_reframed(first, second, clamped=True)
    # Checks the stored release fact again on continuation of the moved Run.
    _cancel_and_new_run(current.workbench, incident)
    assert _fact(_current(incident)["input"]) == _fact(first["input"])


def test_ac8_waiting_human_follow_up_keeps_same_run_and_snapshot(released, owner):
    current, incident, first = released
    lease = owner.claim(incident, first["run_id"], uuid4(), first["versions"])
    owner.hand_off(lease)
    assert _current(incident)["state"] == "waiting_human"
    _attach(current, incident, first)
    _renew(current, incident)
    after = _current(incident)
    assert after["run_id"] == first["run_id"]
    assert after["state"] == "queued"
    assert after["raw"] == first["raw"]


def test_ac9_missing_snapshot_fresh_fallback_uses_creation_frame(released):
    current, incident, first = released
    _attach(current, incident, first)
    _tamper("UPDATE opspilot_runs SET input=NULL WHERE run_id=%s", (first["run_id"],))
    _cancel_and_new_run(current.workbench, incident)
    _assert_reframed(first, _current(incident))


def test_ac10_release_page_and_alert_projection_remain_release_only(released):
    current, incident, first = released
    _attach(current, incident, first)
    assert current.workbench.incidents.alert_records(incident) is None
    assert current.workbench.snapshot(incident)["alert"] is None
    page = call(current.app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200, page.text
    assert 'id="alert-intake"' not in page.text


def test_invalid_previous_frame_end_refuses_new_run_without_writing_successor(released):
    """Desired behavior: unparseable predecessor frame -> INPUT_INVALID."""
    current, incident, first = released
    _attach(current, incident, first)
    damaged = first["input"]
    _policy(damaged)["window"]["end"] = "not-a-time"
    _tamper(
        "UPDATE opspilot_runs SET input=%s::jsonb WHERE run_id=%s",
        (json.dumps(damaged), first["run_id"]),
    )
    control(current.workbench, incident, "cancel")
    before = _incident(incident)
    with pytest.raises(WorkbenchError) as caught:
        control(current.workbench, incident, "new_run")
    assert caught.value.code == "INPUT_INVALID"
    assert _incident(incident) == before
