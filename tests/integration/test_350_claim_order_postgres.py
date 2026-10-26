"""#350 independent acceptance contract (Agent Brief, U1=D / U2=A).

AC1-AC7 exercise public claims / ObserverLoop and committed PG rows. The
F6 scratch-database and F11 authorization/catalog fixtures are reused; only
fixture timestamps are backdated as in those suites. No product SQL, private
method or sampler call order is asserted. AC8 pins the main baseline for
the migration head and the complete declared runtime-grant matrix.
"""

# ruff: noqa: F811 - imported pytest fixtures are intentionally dependencies
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.observer.loop import ObserverLoop
from opspilot.persistence import DurableStore
from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE
from tests.f11_release_support import (
    PROFILE,
    PROFILE_CONTENT,
    PROFILE_REVISION,
    pods_for,
)
from tests.integration.test_f11_pr2b_acceptance_postgres import AnySource
from tests.integration.test_f11_release_observation_postgres import (
    AFTER_IMAGES,
    Catalog,
    _incident,
    _observing,
    _wait_past,
    catalog,  # noqa: F401 - fixture
)
from tests.integration.test_m1_02_observation_store_postgres import (
    POOL,
    controller,  # noqa: F401 - fixture
    observer_dsn,  # noqa: F401 - fixture
    owner,  # noqa: F401 - fixture
    scratch_dsn,  # noqa: F401 - fixture
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


@pytest.fixture(scope="module")
def web(scratch_dsn: str) -> Iterator[ReleaseStore]:
    with login(scratch_dsn, WEB_ROLE) as dsn:
        store = ReleaseStore(dsn, pool=POOL)
        try:
            yield store
        finally:
            store.close()


@pytest.fixture(scope="module")
def observer(observer_dsn: str) -> Iterator[ObservationStore]:
    # A broken SKIP LOCKED path must fail instead of hanging this test run.
    dsn = make_conninfo(observer_dsn, options="-c statement_timeout=3000")
    store = ObservationStore(dsn, pool=POOL)
    try:
        yield store
    finally:
        store.close()


@dataclass(frozen=True)
class Case:
    session_id: UUID
    purpose: str
    subject_id: UUID
    target_id: UUID


@dataclass
class Scene:
    owner: DurableStore
    controller: ObservationStore
    web: ReleaseStore
    observer: ObservationStore
    catalog: Catalog
    cases: list[Case] = field(default_factory=list)

    def recovery(
        self, due: datetime, *, interval: int = 60, session_id: UUID | None = None
    ) -> Case:
        incident, target = _incident(self.owner)
        sid = self.controller.authorize_session(
            incident,
            revision="350-test",
            actor="test:350",
            deadline_at=self.observer.current_time() + timedelta(hours=1),
            max_samples=40,
            sample_interval_seconds=interval,
            sustained_window_seconds=600,
            health_profile_revision=PROFILE_REVISION,
            health_profile=PROFILE_CONTENT,
            session_id=session_id,
            first_sample_due_at=due,
        )
        case = Case(sid, "incident_recovery", incident, target)
        self.cases.append(case)
        return case

    def release(self, due: datetime, *, expires_in: float | None = None) -> Case:
        if expires_in is None:
            observing = _observing(self.owner, self.web, self.catalog)
        else:
            observing = _observing(
                self.owner,
                self.web,
                self.catalog,
                released_ago=PROFILE.session.deadline_seconds - expires_in,
                min_tracking=300,
            )
        case = Case(
            observing.session_id,
            "release_observation",
            observing.oid,
            observing.release.target_id,
        )
        self.cases.append(case)
        self.set_due(case, due)
        return case

    def set_due(self, case: Case, due: datetime) -> None:
        with self.owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET active_sample_due_at=%s "
                "WHERE session_id=%s",
                (due, case.session_id),
            )

    def claim(self, limit: int = 1) -> list[UUID]:
        return [
            lease.session_id
            for lease in self.observer.claim_due_samples(uuid4(), limit=limit)
        ]


@pytest.fixture
def scene(
    owner: DurableStore,
    controller: ObservationStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> Iterator[Scene]:
    current = Scene(owner, controller, web, observer, catalog)
    try:
        yield current
    finally:
        # Retire only this test's sessions through public controls, including
        # after expected red assertions, so strict limit=1 checks stay isolated.
        for case in current.cases:
            if case.purpose == "incident_recovery":
                controller.revoke_sessions(case.subject_id)
            else:
                release = web.get_release(case.subject_id)
                if release.status in ("pending", "observing"):
                    receipt = web.control_release(
                        case.subject_id,
                        action="cancel",
                        actor="test:350-cleanup",
                        expected_control_generation=release.control_generation,
                        idempotency_key=f"cleanup-350-{uuid4()}",
                        calibrated_policy_revision=None,
                    )
                    assert receipt.outcome == "applied", receipt


def _pair(scene: Scene, gap: int, interval: int = 60) -> tuple[Case, Case]:
    due = scene.observer.current_time() - timedelta(minutes=10)
    release = scene.release(due - timedelta(seconds=gap))
    # Deliberately put release first under the old (real_due, session_id)
    # ordering at a tie: main must go red deterministically, not by UUID luck.
    recovery = scene.recovery(
        due,
        interval=interval,
        session_id=UUID(int=release.session_id.int + 1),
    )
    return recovery, release


def test_ac1_simultaneously_due_limit_one_claims_recovery(scene: Scene) -> None:
    """AC1: a later UUID cannot cancel the one-interval recovery head start."""
    recovery, _ = _pair(scene, gap=0)
    assert scene.claim() == [recovery.session_id]


@pytest.mark.parametrize("interval", [15, 60, 120])
@pytest.mark.parametrize("within_interval", [True, False], ids=["within", "beyond"])
def test_ac2_release_earlier_by_less_or_more_than_own_interval(
    scene: Scene, interval: int, within_interval: bool
) -> None:
    """AC2: use the recovery row's own interval, including non-profile values."""
    gap = interval // 2 if within_interval else interval + 5
    recovery, release = _pair(scene, gap=gap, interval=interval)
    expected = recovery if within_interval else release
    assert scene.claim() == [expected.session_id]


def test_ac2_equal_effective_due_uses_session_id(scene: Scene) -> None:
    """AC2/keyset contract: exactly one interval makes an effective-key tie."""
    recovery, release = _pair(scene, gap=60)
    assert scene.claim(limit=2) == [release.session_id, recovery.session_id]


def test_ac2_multiple_recoveries_use_each_rows_interval(scene: Scene) -> None:
    """AC2: two recovery rows at the same real due time have different keys."""
    due = scene.observer.current_time() - timedelta(minutes=10)
    short = scene.recovery(due, interval=15)
    long = scene.recovery(
        due, interval=120, session_id=UUID(int=short.session_id.int + 1)
    )
    release = scene.release(due - timedelta(seconds=30))
    assert scene.claim(limit=3) == [
        long.session_id,
        release.session_id,
        short.session_id,
    ]


def test_ac3_release_is_not_starved_by_continuously_arriving_recoveries(
    scene: Scene,
) -> None:
    """AC3: a finite older backlog may precede release; new arrivals cannot.

    Each arrival is already due but beyond release_due + interval. An
    absolute recovery-first implementation would keep choosing arrivals.
    All injected due times also satisfy the brief's > release_due - interval.
    """
    due = scene.observer.current_time() - timedelta(minutes=10)
    release = scene.release(due)
    backlog = [scene.recovery(due + timedelta(seconds=s)) for s in (10, 30, 50)]
    seen: list[UUID] = []
    for index in range(len(backlog) + 1):
        scene.recovery(due + timedelta(seconds=61 + index))
        (claimed,) = scene.claim()
        assert claimed not in seen
        seen.append(claimed)
        if claimed == release.session_id:
            break
    assert release.session_id in seen, "new recovery arrivals starved a due release"


def test_ac4_future_recovery_is_not_eligible_even_inside_head_start(
    scene: Scene,
) -> None:
    """AC4: effective due is in the past, real due is still in the future."""
    now = scene.observer.current_time()
    future = scene.recovery(now + timedelta(seconds=30))
    release = scene.release(now - timedelta(seconds=10))
    assert scene.observer.current_time() < now + timedelta(seconds=30)
    assert scene.claim(limit=2) == [release.session_id]
    assert scene.controller.session(future.session_id)["active_sample_owner"] is None
    assert scene.claim() == []


@contextmanager
def _locked(scene: Scene, cases: Sequence[Case], kind: str) -> Iterator[None]:
    with psycopg.connect(scene.owner.dsn) as holder:
        for case in cases:
            if kind == "session":
                table, column, identity = (
                    "opspilot_observation_sessions",
                    "session_id",
                    case.session_id,
                )
            elif case.purpose == "incident_recovery":
                table, column, identity = (
                    "opspilot_incidents",
                    "incident_id",
                    case.subject_id,
                )
            else:
                table, column, identity = (
                    "opspilot_release_observations",
                    "observation_id",
                    case.subject_id,
                )
            holder.execute(
                sql.SQL("SELECT {} FROM {} WHERE {}=%s FOR UPDATE").format(
                    sql.Identifier(column),
                    sql.Identifier(table),
                    sql.Identifier(column),
                ),
                (identity,),
            )
        yield


def _mixed(scene: Scene, count: int, *, step: int) -> list[Case]:
    base = scene.observer.current_time() - timedelta(hours=2)
    cases = []
    for rank in range(count):
        effective_due = base + timedelta(seconds=rank * step)
        case = (
            scene.recovery(effective_due + timedelta(seconds=60))
            if rank % 2 == 0
            else scene.release(effective_due)
        )
        cases.append(case)
    return cases


@pytest.mark.parametrize("lock_kind", ["subject", "session"])
def test_ac5_mixed_keyset_crosses_locked_pages_without_omissions_or_duplicates(
    scene: Scene, lock_kind: str
) -> None:
    """AC5: skip two blocked pages (release/incident locks or session locks).

    Real due order crosses effective due order. This detects cursors still
    using real due values, including a mismatch only in the selected key.
    """
    cases = _mixed(scene, 8, step=5)
    ids = [case.session_id for case in cases]
    with _locked(scene, cases[:4], lock_kind):
        first = scene.claim(limit=2)
        second = scene.claim(limit=2)
        assert first == ids[4:6]
        assert second == ids[6:8]
        assert scene.claim(limit=2) == []
        for case in cases[:4]:
            assert (
                scene.controller.session(case.session_id)["active_sample_owner"] is None
            )
    after_unlock = scene.claim(limit=2) + scene.claim(limit=2)
    assert after_unlock == ids[:4]
    all_claimed = first + second + after_unlock
    assert len(all_claimed) == len(set(all_claimed)) == len(ids)
    assert set(all_claimed) == set(ids)
    assert scene.claim(limit=2) == []


def test_ac5_equal_effective_keys_across_pages_use_uuid_without_losing_rows(
    scene: Scene,
) -> None:
    """AC5: effective-key ties spanning blocked pages still advance by UUID."""
    effective_due = scene.observer.current_time() - timedelta(minutes=10)
    cases = []
    for _ in range(3):
        release = scene.release(effective_due)
        recovery = scene.recovery(
            effective_due + timedelta(seconds=60),
            session_id=UUID(int=release.session_id.int + 1),
        )
        cases.extend((release, recovery))
    cases.sort(key=lambda case: case.session_id)
    ids = [case.session_id for case in cases]
    with _locked(scene, cases[:2], "subject"):
        first = scene.claim(limit=2)
        second = scene.claim(limit=2)
        assert first == ids[2:4]
        assert second == ids[4:6]
    after_unlock = scene.claim(limit=2)
    assert after_unlock == ids[:2]
    assert len(set(first + second + after_unlock)) == 6
    assert scene.claim(limit=2) == []


@pytest.mark.parametrize("limit", [1, 2])
@pytest.mark.parametrize(
    "free_at_budget", [True, False], ids=["last-probe", "over-budget"]
)
def test_ac5_probe_budget_remains_exactly_eight_times_limit(
    scene: Scene, limit: int, free_at_budget: bool
) -> None:
    """AC5: the 8*limit-th candidate is probed, the next candidate is not.

    Wide spacing makes real/effective orders identical, isolating the
    existing bound from the changed order. Observe returned leases only.
    """
    blocked_count = 8 * limit - int(free_at_budget)
    cases = _mixed(scene, blocked_count + limit, step=120)
    with _locked(scene, cases[:blocked_count], "session"):
        expected = [cases[blocked_count].session_id] if free_at_budget else []
        assert scene.claim(limit=limit) == expected
        for case in cases[blocked_count + len(expected) :]:
            assert (
                scene.controller.session(case.session_id)["active_sample_owner"] is None
            )


@pytest.mark.parametrize("purpose", ["incident_recovery", "release_observation"])
@pytest.mark.parametrize("excluded_by", ["suspension", "deadline", "live_lease"])
def test_ac6_existing_eligibility_filters_are_unchanged(
    scene: Scene, purpose: str, excluded_by: str
) -> None:
    """AC6: a high-ranked but ineligible candidate cannot consume the limit."""
    due = scene.observer.current_time() - timedelta(minutes=10)
    if purpose == "incident_recovery":
        blocked = scene.recovery(due)
    else:
        blocked = scene.release(
            due, expires_in=3.0 if excluded_by == "deadline" else None
        )
    suspension_generation = None
    try:
        if excluded_by == "suspension":
            suspension_generation = scene.owner.set_target_suspension(
                blocked.target_id, True, expected_generation=0, actor="test:350"
            )
        elif excluded_by == "deadline":
            if purpose == "release_observation":
                # A release deadline is immutable even to the owner. Reuse
                # the F11 DB-clock wait instead of forging frozen fields.
                _wait_past(scene.web.get_release(blocked.subject_id).deadline_at)
            else:
                with scene.owner.transaction() as conn:
                    conn.execute(
                        "UPDATE opspilot_observation_sessions SET deadline_at=%s "
                        "WHERE session_id=%s",
                        (due, blocked.session_id),
                    )
        else:
            assert scene.claim() == [blocked.session_id]
        eligible = (
            scene.release(due + timedelta(seconds=120))
            if purpose == "incident_recovery"
            else scene.recovery(due + timedelta(seconds=120))
        )
        assert scene.claim() == [eligible.session_id]
        assert scene.claim() == []
    finally:
        if suspension_generation is not None:
            scene.owner.set_target_suspension(
                blocked.target_id,
                False,
                expected_generation=suspension_generation,
                actor="test:350",
            )


@pytest.mark.parametrize("purpose", ["incident_recovery", "release_observation"])
def test_ac6_lifted_suspension_generation_ends_as_scope_suspended(
    scene: Scene, purpose: str
) -> None:
    """AC6: lifting a target suspension never reuses the old authorization."""
    due = scene.observer.current_time() - timedelta(minutes=10)
    case = scene.recovery(due) if purpose == "incident_recovery" else scene.release(due)
    generation = scene.owner.set_target_suspension(
        case.target_id, True, expected_generation=0, actor="test:350"
    )
    scene.owner.set_target_suspension(
        case.target_id, False, expected_generation=generation, actor="test:350"
    )
    assert scene.claim() == []
    session = scene.controller.session(case.session_id)
    assert session["ended_reason"] == "scope_suspended"
    assert session["active_sample_job_id"] is None
    with scene.owner.transaction(snapshot=True) as conn:
        endings = conn.execute(
            "SELECT ended_reason FROM opspilot_observation_endings WHERE session_id=%s",
            (case.session_id,),
        ).fetchall()
    assert [row["ended_reason"] for row in endings] == ["scope_suspended"]
    assert scene.claim() == []


def test_ac7_one_poll_samples_both_kinds_once_with_their_own_readings(
    scene: Scene,
) -> None:
    """AC7: real samplers and submission paths, only telemetry is scripted."""
    recovery, release = _pair(scene, gap=0)
    context = scene.observer.release_context(release.session_id)
    source = AnySource(context, pods_for(AFTER_IMAGES))
    results = ObserverLoop(store=scene.observer, source=source, batch=4).poll_once(
        compare=False
    )
    assert {sid for sid, _ in results} == {recovery.session_id, release.session_id}
    assert len(results) == 2
    assert all(receipt.accepted for _, receipt in results)
    assert source.instant_calls > 0  # recovery's scalar telemetry interface
    assert source.calls  # release's labelled-vector telemetry interface
    with scene.owner.transaction(snapshot=True) as conn:
        samples = conn.execute(
            "SELECT session_id,sample_id,purpose,disposition "
            "FROM opspilot_observation_samples WHERE session_id=ANY(%s)",
            ([recovery.session_id, release.session_id],),
        ).fetchall()
        recovery_readings = conn.execute(
            "SELECT signal_name FROM opspilot_observation_signal_readings "
            "WHERE sample_id IN (SELECT sample_id FROM opspilot_observation_samples "
            "WHERE session_id=%s)",
            (recovery.session_id,),
        ).fetchall()
        release_queries = conn.execute(
            "SELECT query_key FROM opspilot_release_query_results "
            "WHERE sample_id IN (SELECT sample_id FROM opspilot_observation_samples "
            "WHERE session_id=%s)",
            (release.session_id,),
        ).fetchall()
    assert sorted((r["purpose"], r["disposition"]) for r in samples) == [
        ("incident_recovery", "adopted"),
        ("release_observation", "adopted"),
    ]
    assert recovery_readings and release_queries
    assert scene.claim(limit=2) == []


def test_ac8_alembic_head_and_all_runtime_grants_stay_at_main_baseline(
    scene: Scene,
) -> None:
    """AC8: baseline main 78a9603; canonical full role/table/privilege matrix."""
    head = "0021_release_incident_links"
    assert schema.head_revision() == head
    with scene.owner.transaction(snapshot=True) as conn:
        assert schema.current_revision(conn) == head
    grants = {
        role: {table: sorted(privileges) for table, privileges in tables.items()}
        for role, tables in RUNTIME_GRANTS.items()
    }
    encoded = json.dumps(grants, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == (
        "a1721d08506cd2692b21630bcde220b82771144ce181db608a3b34c73fa8e516"
    )
