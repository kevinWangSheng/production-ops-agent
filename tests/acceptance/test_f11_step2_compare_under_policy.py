"""F11 step 2: "Compare pre/post-release and old/new-instance evidence under
the reviewed traffic policy, including a healthy release and an unrelated
concurrent fault".

Independent acceptance tests for ``docs/tasks/2026-10-24-f11-pr5-contract.md``
section 4, step 2, items 1-4 (with A5: insufficient version or traffic
evidence is ``unknown``, never a release regression; PR-2: whole-service
signals are context only; PR-4 P4-01: the bounded comparison). Harness:
``tests/acceptance/f11_steps_support.py``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.acceptance import f11_steps_support as s
from tests.f11_comparison_support import OLD_RS, ComparisonWorld
from tests.f11_release_support import pods_for

pytestmark = s.PG

SCOPES = ("before_service", "before_old", "after_service", "after_new")


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Any:
    with s.module_env(Path(tmp_path_factory.mktemp("f11-step2"))) as built:
        yield built


def _healthy_release(env: s.Env, **release: Any) -> tuple[s.Delivered, UUID]:
    """Delivered, authorized by the pass, then sampled healthy every 300 s
    of after-window until the store confirms it healthy (the intake's
    ``min_tracking_seconds`` M=1500 and the 600 s sustained window bound
    how long that takes)."""
    release.setdefault("released_ago", 2500.0)
    case, sid = s.observing(env, **release)
    authorized_at = case.record.released_at + timedelta(seconds=100)
    env.set_authorized_at(sid, authorized_at)
    pods = pods_for(s.AFTER_IMAGES)
    statuses = []
    for offset in range(300, 2101, 300):
        receipt = s.submit(env, sid, authorized_at + timedelta(seconds=offset), pods)
        assert receipt.evaluation.verdict == "healthy", receipt.evaluation
        statuses.append(receipt.release_status)
        if receipt.release_status != "observing":
            break
    assert statuses[-1] == "healthy", statuses
    return case, sid


# ===========================================================================
# item 1: the page shows the frozen policy and the comparison record
# ===========================================================================


def test_item1_the_page_shows_the_frozen_policy_and_the_four_scope_comparison(
    env: s.Env,
) -> None:
    regression = s.regress(env)
    case = regression.case
    record = env.record(case.oid)
    app = env.app()

    document = s.get_json(app, s.detail_path(case.oid))
    page = s.get_html(app, s.detail_path(case.oid))

    # the reviewed traffic policy the release is judged under
    assert record.calibrated_policy_revision == case.entry["revision"]  # type: ignore[index]
    assert record.calibrated_policy_revision in s.strings(document)
    assert record.calibrated_policy_revision in page
    # the before window ends at released_at and is one evaluation window long
    (comparison,) = s.comparisons(env, case.oid)
    assert s.has_instant(document, record.released_at - s.WINDOW), document
    assert s.has_instant(document, record.released_at)
    found = s.strings(document)
    for scope in SCOPES:
        assert scope in found, scope
        assert scope in page, scope
    assert comparison["status"] in found


def test_item1_a_failed_collection_is_unavailable_and_the_release_stays_degraded(
    env: s.Env,
) -> None:
    failing = ComparisonWorld(
        old_pods=pods_for(s.BEFORE_IMAGES, 1, replicaset=OLD_RS), fail_all=True
    )
    regression = s.regress(env, comparison=failing)
    assert failing.calls, "the comparison was collected without Prometheus"
    case = regression.case

    (comparison,) = s.comparisons(env, case.oid)
    assert comparison["status"] == "unavailable", comparison
    record = env.record(case.oid)
    assert record.status == "anomalous", record
    document = s.get_json(env.app(), s.detail_path(case.oid))
    assert "unavailable" in s.strings(document)
    assert document["verdict"] == "degraded"


# ===========================================================================
# item 2: a healthy release produces no incident, Run, link or event
# ===========================================================================


def test_item2_a_healthy_release_creates_no_incident_run_link_or_event(
    env: s.Env,
) -> None:
    env.anomaly_pass()  # settle whatever an earlier test in this module left
    totals = env.totals()

    case, _ = _healthy_release(env)
    for _ in range(3):
        env.anomaly_pass()

    record = env.record(case.oid)
    assert record.status == "healthy", record
    assert record.incident_id is None
    assert env.links(case.oid) == []
    assert env.incidents_on(case.target_id) == []
    assert env.totals() == totals
    assert s.comparisons(env, case.oid) == []
    assert s.get_json(env.app(), s.detail_path(case.oid))["verdict"] == "healthy"


# ===========================================================================
# item 3: an unrelated concurrent fault does not make the release degraded
# ===========================================================================


def test_item3_an_unrelated_concurrent_fault_leaves_the_release_healthy_and_unlinked(
    env: s.Env,
) -> None:
    """Another service (frontend) has an open incident from its own alert
    while checkout's new version is healthy: the release ends healthy and
    is associated with no incident; the frontend incident gets nothing."""
    frontend = env.owner.register_target(f"deployment/frontend-{uuid4().hex[:10]}")
    incident_id, run_id = uuid4(), uuid4()
    env.owner.accept(
        incident_id,
        run_id,
        f"alert:frontend-errors:{incident_id}",
        deadline=env.now() + timedelta(hours=1),
        budget_limit=5,
        versions={"seed": "f11-step2"},
        target_id=frontend,
    )
    env.anomaly_pass()
    events_before = env.events_of(incident_id)
    runs_before = env.runs_of(incident_id)

    case, _ = _healthy_release(env)
    for _ in range(3):
        env.anomaly_pass()

    record = env.record(case.oid)
    assert (record.status, record.incident_id) == ("healthy", None)
    assert env.links(case.oid) == []
    assert env.incidents_on(case.target_id) == []
    assert env.events_of(incident_id) == events_before
    assert env.runs_of(incident_id) == runs_before
    assert str(incident_id) not in s.strings(
        s.get_json(env.app(), s.detail_path(case.oid))
    )


def test_item3_a_concurrent_regression_on_another_release_is_linked_alone(
    env: s.Env,
) -> None:
    """Two releases observed at once: one regresses, the other is healthy.
    Only the regressed one is associated with an incident."""
    healthy, _ = _healthy_release(env)
    regression = s.regress(env)

    env.anomaly_pass()

    assert len(env.links(regression.case.oid)) == 1
    assert env.record(regression.case.oid).incident_id is not None
    assert env.links(healthy.oid) == []
    assert env.record(healthy.oid).incident_id is None
    assert env.incidents_on(healthy.target_id) == []


# ===========================================================================
# item 4: missing version mapping or traffic is unknown, never degraded
# ===========================================================================


@pytest.mark.parametrize(
    "evidence",
    ["mapping_missing", "traffic_insufficient", "context_only_degraded"],
)
def test_item4_insufficient_evidence_is_unknown_never_degraded(
    env: s.Env, evidence: str
) -> None:
    """Every sample is out of bounds where it is read (error ratio 0.5), but
    the evidence cannot attribute it to the new version: the release never
    becomes degraded and ends ``unknown`` at its deadline, with no incident."""
    case, sid = s.observing(env, released_ago=s.DEADLINE - 25.0)
    authorized_at = case.record.released_at + timedelta(seconds=100)
    env.set_authorized_at(sid, authorized_at)
    pods = pods_for(s.AFTER_IMAGES)
    kwargs: dict[str, Any] = {
        "mapping_missing": {"mapping_ok": False, "context": {"error_ratio": 0.5}},
        "traffic_insufficient": {
            "after": {"request_rate_per_second": 0.001, "error_ratio": 0.5}
        },
        "context_only_degraded": {"context": {"error_ratio": 0.5}},
    }[evidence]

    for offset in (300, 600, 900):
        receipt = s.submit(
            env, sid, authorized_at + timedelta(seconds=offset), pods, **kwargs
        )
        assert receipt.disposition == "adopted", receipt
        assert receipt.evaluation.verdict == "unknown", receipt.evaluation
        assert receipt.release_status == "observing", receipt
    env.wait_past(case.record.deadline_at)
    env.tick(s.ReleaseWorld())
    env.anomaly_pass()

    record = env.record(case.oid)
    assert record.status == "unknown", record
    assert record.ended_reason == "deadline_expired"
    assert record.incident_id is None and env.links(case.oid) == []
    document = s.get_json(env.app(), s.detail_path(case.oid))
    assert document["verdict"] == "unknown"
    assert "degraded" not in {document["verdict"], document["status"]}
