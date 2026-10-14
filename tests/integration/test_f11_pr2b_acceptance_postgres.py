"""Independent PostgreSQL acceptance tests for F11 PR-2b: a release lease
claimed from the real store is sampled by ``take_release_sample`` and adopted
atomically; the shipped (empty) catalog still refuses authorization; one
Observer tick sweeps expired releases and dispatches both lease kinds.

Written from ``docs/tasks/2026-10-13-f11-pr2-contract.md`` (「拆分」, r4, r5
B02–B04, 「5. PR-2b 遥测接口」) without reading the PR-2b implementation. The
Prometheus side is a fake ``ReleaseSource`` answering the public query plan
with raw responses built by ``tests.f11_release_support``; everything else is
the real store under the real runtime roles (fixtures and helpers reused from
the PR-2a acceptance module). A test policy catalog is swapped in through
``RELEASE_POLICY_CATALOG_PATH``; it is not a formal calibration.
"""

# ruff: noqa: F811 - pytest fixtures imported from the PR-2a module
from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from opspilot.observation import ObservationStore, release_policy
from opspilot.observation.release_types import ReleaseSampleLease
from opspilot.observation.releases import ReleaseStore
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import InstantResult
from opspilot.observer.release_evaluation import build_release_query_plan
from opspilot.persistence import DurableStore
from tests.f11_release_support import (
    INTERVAL,
    WINDOW,
    pods_for,
    sha256_hex,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
    AFTER_IMAGES,
    QUERY_RESULTS,
    SAMPLES,
    Catalog,
    _authorize,
    _claim,
    _db_now,
    _expiring,
    _incident_session,
    _make_due,
    _observing,
    _record_release,
    _release_sessions,
    _rows,
    _sample,
    _set_authorized_at,
    _wait_past,
    catalog,
    history,
    observer,
    observer_dsn,
    owner,
    reader,
    web,
    web_dsn,
)
from tests.test_f11_pr2b_acceptance import (
    _PR2B_MISSING,
    SENT_AT,
    PlannedSource,
    normalize_query,
)

try:
    from opspilot.observer.release_sampling import take_release_sample
except ImportError:  # implementation not merged yet; ``pr2b`` fails the tests
    take_release_sample = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(
    __import__("os").environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)


def pr2b(test: Callable[..., None]) -> Callable[..., None]:
    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        if _PR2B_MISSING is not None:
            pytest.fail(f"PR2B_INTERFACE_MISSING: {_PR2B_MISSING}", pytrace=False)
        test(*args, **kwargs)

    return run


class AnySource(PlannedSource):
    """The tick's source: the planned answers for our release; an empty
    scalar answer for any incident lease another test left due."""

    def instant(self, expr: str, *, at: Any, timeout_seconds: int) -> InstantResult:
        self.instant_calls += 1
        body = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
        return InstantResult(expr, "no_data", None, body, 200)


def _backdated_observing(
    owner: DurableStore, web: ReleaseStore, catalog: Catalog
) -> Any:
    """An observing release whose session was authorized long enough ago for
    a window entirely after it ([T-W, T] with T-W >= A)."""
    observing = _observing(owner, web, catalog)
    _set_authorized_at(observing.session_id, _db_now() - WINDOW - timedelta(seconds=60))
    return observing


def _query_rows(sample_id: UUID) -> dict[str, dict[str, Any]]:
    return {
        row["query_key"]: row
        for row in _rows(
            f"SELECT * FROM {QUERY_RESULTS} WHERE sample_id=%s", (sample_id,)
        )
    }


# ===========================================================================
# 1. claim -> take_release_sample -> atomic adoption with planned query rows
# ===========================================================================


@pr2b
def test_a_claimed_release_lease_is_sampled_adopted_and_stores_the_planned_queries(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _backdated_observing(owner, web, catalog)
    sid = observing.session_id
    lease = _claim(observer, sid)
    context = observer.release_context(sid)
    pods = pods_for(AFTER_IMAGES)
    source = PlannedSource(context, pods)

    taken = take_release_sample(lease, source, observer)

    assert (taken.scope_interrupted, taken.budget_exhausted) == (False, False)
    assert taken.requests_issued == len(source.calls) == 49
    assert source.instant_calls == 0
    receipt = taken.receipt
    assert (receipt.accepted, receipt.disposition, receipt.reason) == (
        True,
        "adopted",
        "adopted",
    )
    assert (receipt.session_id, receipt.observation_id) == (sid, observing.oid)
    assert receipt.evaluation.evidence_sufficient is True
    assert receipt.evaluation.verdict == "healthy"
    assert {i.pod_uid for i in receipt.evaluation.instances} == {p.uid for p in pods}
    assert receipt.release_status == "observing"
    assert receipt.used_sample_count == 1
    assert reader.get_release(observing.oid).used_sample_count == 1

    sample = _sample(receipt.sample_id)
    t = sample["window_end"]
    assert sample["window_start"] == t - WINDOW
    assert t <= sample["decision_at"]
    assert receipt.next_sample_due_at == sample["decision_at"] + INTERVAL
    # every request at the same Prometheus time T, the DB window end
    assert {at for _, at, _ in source.calls} == {t}
    plan = build_release_query_plan(
        context, window_end=t, instance_uids=[p.uid for p in pods]
    )
    rows = _query_rows(receipt.sample_id)
    assert set(rows) == set(plan)
    for key, row in rows.items():
        assert normalize_query(row["query"]) == normalize_query(plan[key])
        assert bytes(row["raw"]) == source.returned[row["query"]]
        assert row["raw_sha256"] == sha256_hex(bytes(row["raw"]))
        assert row["source"] == "prometheus"
        assert row["evaluated_at"] == t
        assert row["sent_at"] == SENT_AT
        assert (row["status"], row["http_status"], row["body_complete"]) == (
            "ok",
            200,
            True,
        )


@pr2b
def test_a_scope_interruption_before_any_request_is_an_adopted_zero_query_sample(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    lease = _claim(observer, observing.session_id)
    source = PlannedSource(
        observer.release_context(observing.session_id), pods_for(AFTER_IMAGES)
    )

    taken = take_release_sample(lease, source, observer, scope_current=lambda _: False)

    assert (taken.requests_issued, taken.scope_interrupted) == (0, True)
    assert source.calls == []
    receipt = taken.receipt
    # r4 (user decision 2026-10-14): zero-query evidence is adopted, counted
    assert (receipt.accepted, receipt.disposition) == (True, "adopted")
    assert (receipt.evaluation.verdict, receipt.evaluation.outcome) == (
        "unknown",
        "no_data",
    )
    assert receipt.used_sample_count == 1
    assert reader.get_release(observing.oid).used_sample_count == 1
    assert _query_rows(receipt.sample_id) == {}


# ===========================================================================
# 2. the shipped empty catalog still refuses authorization (no sampling job)
# ===========================================================================


def test_with_the_shipped_empty_catalog_authorization_is_refused_and_nothing_is_claimable(
    owner: DurableStore, web: ReleaseStore, observer: ObservationStore
) -> None:
    shipped = (
        Path(release_policy.__file__).resolve().parent
        / "policies"
        / "release-calibrations.json"
    )
    assert Path(release_policy.RELEASE_POLICY_CATALOG_PATH).resolve() == shipped
    assert (
        shipped.read_text(encoding="utf-8").strip()
        == '{"format_version":1,"policies":[]}'
    )
    release = _record_release(owner, web)

    receipt = _authorize(web, release.oid, "checkout-release@0123456789ab")

    assert (receipt.outcome, receipt.reason) == (
        "rejected",
        "RELEASE_POLICY_UNCALIBRATED",
    )
    assert receipt.session_id is None
    assert _release_sessions(release.oid) == []
    assert web.get_release(release.oid).status == "pending"
    assert not any(
        isinstance(lease, ReleaseSampleLease) and lease.observation_id == release.oid
        for lease in observer.claim_due_samples(__import__("uuid").uuid4(), limit=50)
    )


# ===========================================================================
# 3. one Observer tick (B04): release sweep + shared claim, both lease kinds
# ===========================================================================


@pr2b
def test_one_observer_tick_sweeps_expired_releases_and_samples_both_lease_kinds(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    history: ObservationStore,
    catalog: Catalog,
) -> None:
    expiring = _expiring(owner, web, seconds=3.0)  # pending, no session
    observing = _backdated_observing(owner, web, catalog)
    sid = observing.session_id
    _, incident_session = _incident_session(owner, history)
    context = observer.release_context(sid)
    pods = pods_for(AFTER_IMAGES)
    _make_due(sid)
    _make_due(incident_session)
    _wait_past(expiring.record.deadline_at)

    loop = ObserverLoop(store=observer, source=AnySource(context, pods), batch=500)
    loop.poll_once()

    # the release sweep ran in the tick
    swept = reader.get_release(expiring.oid)
    assert (swept.status, swept.ended_reason) == ("unknown", "deadline_expired")
    # the release lease was dispatched to the release sampler and adopted
    samples = _rows(
        f"SELECT * FROM {SAMPLES} WHERE session_id=%s ORDER BY decision_at", (sid,)
    )
    assert len(samples) == 1, samples
    (sample,) = samples
    assert sample["purpose"] == "release_observation"
    assert sample["disposition"] == "adopted"
    plan = build_release_query_plan(
        context, window_end=sample["window_end"], instance_uids=[p.uid for p in pods]
    )
    assert set(_query_rows(sample["sample_id"])) == set(plan)
    assert reader.get_release(observing.oid).used_sample_count == 1
    # the incident lease claimed in the same tick went to the incident sampler
    incident_samples = _rows(
        f"SELECT * FROM {SAMPLES} WHERE session_id=%s", (incident_session,)
    )
    assert len(incident_samples) == 1
    assert incident_samples[0]["purpose"] == "incident_recovery"
