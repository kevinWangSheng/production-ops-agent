"""F11 step 3: "Introduce a delayed regression and verify scheduled follow-up
observes it without endlessly re-investigating healthy releases".

Independent acceptance tests for ``docs/tasks/2026-10-24-f11-pr5-contract.md``
section 4, step 3, items 1-3 (C3:370 pausing does not extend the deadline;
P2-06 the sample budget accumulates per release). Harness:
``tests/acceptance/f11_steps_support.py``.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from tests.acceptance import f11_steps_support as s
from tests.f11_release_support import pods_for

pytestmark = s.PG


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Any:
    with s.module_env(Path(tmp_path_factory.mktemp("f11-step3"))) as built:
        yield built


def _snapshot(env: s.Env, case: s.Delivered) -> dict[str, Any]:
    return {
        "record": env.record(case.oid).model_dump(),
        "samples": env.samples(case.oid),
        "sessions": env.sessions(case.oid),
        "audit": env.audit(case.oid),
        "links": env.links(case.oid),
        "incidents": env.incidents_on(case.target_id),
    }


def _rounds(env: s.Env, world: s.ReleaseWorld, case: s.Delivered, n: int) -> None:
    """``n`` rounds of everything scheduled: the Observer tick (every
    session of the release made due first) and both web passes."""
    for _ in range(n):
        env.make_all_due(case.oid)
        env.tick(world)
        env.authorize_pass()
        env.anomaly_pass()


# ===========================================================================
# item 1: healthy first, degraded later, before the deadline -> one incident
# ===========================================================================


def test_item1_a_regression_after_healthy_samples_is_anomalous_with_one_incident(
    env: s.Env,
) -> None:
    regression = s.regress(env, healthy_offsets=(300, 600, 900))
    case = regression.case
    assert [r.evaluation.verdict for r in regression.healthy] == ["healthy"] * 3

    record = env.record(case.oid)
    assert record.status == "anomalous"
    assert record.ended_at is not None and record.ended_at < record.deadline_at
    for _ in range(3):
        env.anomaly_pass()

    (incident,) = env.incidents_on(case.target_id)
    assert len(env.runs_of(incident["incident_id"])) == 1
    assert len(env.links(case.oid)) == 1


def test_item1_two_concurrent_anomaly_passes_open_one_incident_and_one_run(
    env: s.Env,
) -> None:
    regression = s.regress(env)
    case = regression.case
    benches = [env.bench(), env.bench()]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(index: int) -> None:
        try:
            barrier.wait(5)
            benches[index].release_anomaly_pass()
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert errors == []
    (incident,) = env.incidents_on(case.target_id)
    assert len(env.runs_of(incident["incident_id"])) == 1
    assert len(env.links(case.oid)) == 1
    env.anomaly_pass()
    assert env.incidents_on(case.target_id) == [incident]


# ===========================================================================
# item 2: a healthy release is not observed again, by anything scheduled
# ===========================================================================


def _healthy(env: s.Env) -> tuple[s.Delivered, UUID, s.ReleaseWorld]:
    case, sid = s.observing(env, released_ago=2500.0)
    authorized_at = case.record.released_at + timedelta(seconds=100)
    env.set_authorized_at(sid, authorized_at)
    pods = pods_for(s.AFTER_IMAGES)
    status = "observing"
    for offset in range(300, 2101, 300):
        receipt = s.submit(env, sid, authorized_at + timedelta(seconds=offset), pods)
        status = receipt.release_status
        if status != "observing":
            break
    assert status == "healthy"
    world = s.ReleaseWorld()
    # were the Observer to sample it again, it would read a regression
    world.answer(env, sid, pods, after={"error_ratio": 0.5})
    return case, sid, world


def test_item2_after_healthy_nothing_scheduled_samples_authorizes_or_investigates(
    env: s.Env,
) -> None:
    case, _, world = _healthy(env)
    before = _snapshot(env, case)

    _rounds(env, world, case, 4)

    assert _snapshot(env, case) == before
    assert world.sample_calls == []
    assert env.record(case.oid).status == "healthy"
    assert env.record(case.oid).used_sample_count <= s.MAX_SAMPLES


def test_item2_redelivering_a_healthy_release_does_not_reopen_it(env: s.Env) -> None:
    case, _, world = _healthy(env)
    before = _snapshot(env, case)

    again = s.intake(env.app(), case.body)
    _rounds(env, world, case, 2)

    assert again.status == 200, again.text
    assert UUID(again.json()["observation_id"]) == case.oid
    after = _snapshot(env, case)
    assert after["record"] == before["record"]
    for key in ("samples", "sessions", "audit", "links", "incidents"):
        assert after[key] == before[key], key


def test_item2_the_sample_budget_bounds_a_release_that_never_concludes(
    env: s.Env,
) -> None:
    """Endless unknown evidence: the release stops at ``max_samples`` (40)
    and nothing scheduled samples it afterwards."""
    case, sid = s.observing(env, released_ago=3000.0)
    authorized_at = case.record.released_at + timedelta(seconds=100)
    env.set_authorized_at(sid, authorized_at)
    pods = pods_for(s.AFTER_IMAGES)
    status = "observing"
    for index in range(s.MAX_SAMPLES):
        receipt = s.submit(
            env,
            sid,
            authorized_at + timedelta(seconds=300 + 60 * index),
            pods,
            mapping_ok=False,
        )
        status = receipt.release_status
        if status != "observing":
            break
    record = env.record(case.oid)
    assert (record.status, record.ended_reason) == ("unknown", "max_samples_exhausted")
    assert record.used_sample_count == s.MAX_SAMPLES == env.samples(case.oid)
    world = s.ReleaseWorld()
    world.answer(env, sid, pods)
    before = _snapshot(env, case)

    _rounds(env, world, case, 2)

    assert _snapshot(env, case) == before
    assert world.sample_calls == []


# ===========================================================================
# item 3: pause stops sampling; resume opens a fresh session; deadline holds
# ===========================================================================


def test_item3_pause_resume_restarts_tracking_keeps_the_deadline_then_unknown(
    env: s.Env,
) -> None:
    case, first = s.observing(env, released_ago=s.DEADLINE - 25.0)
    deadline = case.record.deadline_at
    authorized_at = case.record.released_at + timedelta(seconds=100)
    env.set_authorized_at(first, authorized_at)
    pods = pods_for(s.AFTER_IMAGES)
    tracked = s.healthy_until(env, first, authorized_at, pods, (300, 600, 900))
    assert tracked[-1].tracking_seconds == 900.0
    assert tracked[-1].healthy_window_seconds == 600.0
    app = env.app()

    paused = s.post_control(
        app, case.oid, "pause", env.record(case.oid).control_generation
    )
    assert paused.status == 200, paused.text
    world = s.ReleaseWorld()
    world.answer(env, first, pods)
    samples = env.samples(case.oid)
    _rounds(env, world, case, 2)
    assert env.samples(case.oid) == samples
    assert world.sample_calls == []
    assert env.record(case.oid).paused is True

    resumed = s.post_control(
        app, case.oid, "resume", env.record(case.oid).control_generation
    )
    assert resumed.status == 200, resumed.text
    record = env.record(case.oid)
    assert (record.status, record.paused) == ("observing", False)
    assert record.deadline_at == deadline
    (second,) = env.authorized(case.oid)
    assert second["session_id"] != first
    assert second["deadline_at"] == deadline
    assert len(env.sessions(case.oid)) == 2
    # tracking and the healthy window start over in the new session
    resumed_at = authorized_at + timedelta(seconds=1000)
    env.set_authorized_at(second["session_id"], resumed_at)
    fresh = s.submit(
        env, second["session_id"], resumed_at + timedelta(seconds=300), pods
    )
    assert fresh.evaluation.verdict == "healthy"
    assert fresh.tracking_seconds == 300.0, fresh
    assert fresh.healthy_window_seconds == 0.0, fresh
    assert fresh.release_status == "observing"

    env.wait_past(deadline)
    env.tick(s.ReleaseWorld())

    record = env.record(case.oid)
    assert (record.status, record.ended_reason) == ("unknown", "deadline_expired")
    assert record.deadline_at == deadline
    assert env.links(case.oid) == []
