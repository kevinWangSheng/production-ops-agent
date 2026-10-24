"""Implementation tests for F11 PR-5b: the automatic release authorization.

The independent acceptance tests live on their own branch; these pin the
implementer's own edges of ``Workbench.release_authorize_pass`` (D7)
against a real PostgreSQL through the web role: one ``system``
authorization per newly registered release, refusals audited and final
(suspension, no calibrated entry, a racing pause), nothing tried after the
deadline or after a human acted, and a storage failure retried.

The lab database is shared, so every pass here sees only the release its
test registered (``_Only``); the harness cancels it afterwards.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.observation import release_policy
from opspilot.observation.releases import ReleaseEvent, calibrated_revision_for
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools.fixture import fixture_face, fixture_versions
from opspilot.web import Authenticator, create_app
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.service import Workbench
from opspilot.web.store import DurableIncidentStore, DurableWebLedger
from scripts.m0.postgres_lab import DSN
from tests.f11_pr3_support import config
from tests.integration import test_f11_pr2b_impl_postgres as pr2b
from tests.m1_tool_support import NOW, FakeClock
from tests.m1_web_support import basic, call

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
release_harness = pr2b.release_harness


class _Only:
    """The web role's ``ReleaseStore`` with its candidates narrowed to the
    releases a test registered, so a pass never touches another test's
    rows."""

    def __init__(self, store: Any, oid: UUID) -> None:
        self._store = store
        self._oids = {oid}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def authorizable_releases(self, *, limit: int) -> tuple[Any, ...]:
        # The whole eligible set, then this test's release: narrowing must
        # not reintroduce a limit before the filter.
        return tuple(
            r
            for r in self._store.authorizable_releases(limit=100_000)
            if r.observation_id in self._oids
        )


@pytest.fixture
def workbench(release_harness):
    web, _, oid, _, _ = release_harness
    store = DurableStore(web.dsn)
    yield Workbench(
        incidents=DurableIncidentStore(store),
        events=DurableEventLog(store),
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face(),
        releases=_Only(web, oid),
    )
    store.close()


def _audit(oid: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(DSN) as conn:
        conn.row_factory = psycopg.rows.dict_row
        return conn.execute(
            "SELECT action,principal_kind,actor,outcome,reason,idempotency_key"
            " FROM opspilot_release_audit WHERE observation_id=%s ORDER BY sequence",
            (oid,),
        ).fetchall()


def _sessions(oid: UUID) -> list[str]:
    with psycopg.connect(DSN) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT state FROM opspilot_observation_sessions"
                " WHERE release_observation_id=%s ORDER BY authorized_at",
                (oid,),
            )
        ]


def test_a_new_release_is_authorized_once_as_system(release_harness, workbench):
    web, _, oid, policy, _ = release_harness
    receipts = workbench.release_authorize_pass()
    assert [(r.observation_id, r.outcome, r.replayed) for r in receipts] == [
        (oid, "applied", False)
    ]
    release = web.get_release(oid)
    assert release.status == "observing" and release.paused is False
    assert release.calibrated_policy_revision == policy.revision
    assert release.control_generation == 0 and release.observation_generation == 1
    assert _sessions(oid) == ["authorized"]
    assert _audit(oid) == [
        {
            "action": "authorize",
            "principal_kind": "system",
            "actor": "system",
            "outcome": "applied",
            "reason": None,
            "idempotency_key": f"auto-authorize:{oid}",
        }
    ]
    # Repeated passes find nothing to do.
    assert workbench.release_authorize_pass() == []
    assert workbench.release_authorize_pass() == []
    assert len(_audit(oid)) == 1 and _sessions(oid) == ["authorized"]


def test_concurrent_passes_authorize_once(release_harness, workbench):
    _, _, oid, _, _ = release_harness
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def run() -> None:
        barrier.wait()
        results.append(workbench.release_authorize_pass())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(results) == 2
    assert [a["outcome"] for a in _audit(oid)] == ["applied"]
    assert _sessions(oid) == ["authorized"]


def test_a_suspended_target_refuses_and_lifting_starts_nothing(
    release_harness, workbench
):
    web, _, oid, _, _ = release_harness
    owner = DurableStore(DSN)
    target = web.get_release(oid).target_id
    generation = owner.set_target_suspension(
        target, True, expected_generation=0, actor="pr5b-impl"
    )
    try:
        receipts = workbench.release_authorize_pass()
        assert [(r.outcome, r.reason) for r in receipts] == [
            ("rejected", "SCOPE_SUSPENDED")
        ]
    finally:
        owner.set_target_suspension(
            target, False, expected_generation=generation, actor="pr5b-impl"
        )
        owner.close()
    # C3 "lifting does not resume": never authorized, never tried again.
    assert workbench.release_authorize_pass() == []
    release = web.get_release(oid)
    assert release.status == "pending" and release.observation_generation == 0
    assert _sessions(oid) == []
    assert [(a["action"], a["outcome"], a["reason"]) for a in _audit(oid)] == [
        ("authorize", "rejected", "SCOPE_SUSPENDED")
    ]


def test_no_calibrated_entry_is_an_audited_final_refusal(
    release_harness, workbench, tmp_path, monkeypatch
):
    web, _, oid, _, _ = release_harness
    empty = tmp_path / "empty.json"
    empty.write_text('{"format_version":1,"policies":[]}')
    monkeypatch.setattr(release_policy, "RELEASE_POLICY_CATALOG_PATH", empty)
    receipts = workbench.release_authorize_pass()
    assert [(r.outcome, r.reason) for r in receipts] == [
        ("rejected", "RELEASE_POLICY_UNCALIBRATED")
    ]
    assert workbench.release_authorize_pass() == []
    assert web.get_release(oid).status == "pending"
    assert _sessions(oid) == []
    assert len(_audit(oid)) == 1


def test_an_ambiguous_catalog_is_never_guessed_through(
    release_harness, workbench, tmp_path, monkeypatch
):
    web, _, oid, policy, catalog = release_harness
    twin = policy.model_dump(mode="json")
    content = dict(twin["content"], approval_ref="test-only-twin")
    digest = release_policy.sha256(release_policy.canonical_json(content).encode())
    twin.update(
        policy_id="twin",
        revision="twin@" + digest[:12],
        content_sha256=digest,
        content=content,
    )
    both = tmp_path / "both.json"
    both.write_text(
        release_policy.canonical_json(
            {"format_version": 1, "policies": [policy.model_dump(mode="json"), twin]}
        )
    )
    monkeypatch.setattr(release_policy, "RELEASE_POLICY_CATALOG_PATH", both)
    with pytest.raises(PersistenceError, match="RELEASE_POLICY_AMBIGUOUS"):
        calibrated_revision_for(web.get_release(oid))
    assert workbench.release_authorize_pass() == []
    assert _audit(oid) == [] and web.get_release(oid).status == "pending"
    # Fixed catalog: the next pass authorizes.
    monkeypatch.setattr(release_policy, "RELEASE_POLICY_CATALOG_PATH", catalog)
    assert [r.outcome for r in workbench.release_authorize_pass()] == ["applied"]


def test_a_human_pause_first_is_left_alone(release_harness, workbench):
    web, _, oid, _, _ = release_harness
    paused = web.control_release(
        oid,
        action="pause",
        actor="operator",
        expected_control_generation=0,
        idempotency_key="pr5b-pause",
        calibrated_policy_revision=None,
    )
    assert paused.outcome == "applied"
    assert workbench.release_authorize_pass() == []
    assert [a["action"] for a in _audit(oid)] == ["pause"]
    assert web.get_release(oid).status == "pending"


def test_a_pause_racing_the_pass_is_a_final_refusal(release_harness, workbench):
    web, _, oid, _, _ = release_harness
    real = workbench.releases.authorizable_releases

    def read_then_pause(**kwargs: Any) -> Any:
        # The human's pause lands between the pass's read and its command.
        candidates = real(**kwargs)
        web.control_release(
            oid,
            action="pause",
            actor="operator",
            expected_control_generation=0,
            idempotency_key="pr5b-race",
            calibrated_policy_revision=None,
        )
        return candidates

    workbench.releases.authorizable_releases = read_then_pause
    receipts = workbench.release_authorize_pass()
    assert [(r.outcome, r.reason) for r in receipts] == [
        ("rejected", "CONTROL_CONFLICT")
    ]
    workbench.releases.authorizable_releases = real
    assert workbench.release_authorize_pass() == []
    assert [(a["action"], a["outcome"]) for a in _audit(oid)] == [
        ("pause", "applied"),
        ("authorize", "rejected"),
    ]


def test_after_the_deadline_nothing_is_tried(release_harness, workbench):
    web, _, oid, _, _ = release_harness
    model = web.get_release(oid)
    (late,) = _register(web, model, 1, released_ago=model.deadline_seconds - 1.5)
    try:
        assert web.get_release(late).status == "pending"
        time.sleep(2)
        workbench.releases._oids = {late}
        assert workbench.release_authorize_pass() == []
        assert _audit(late) == []
    finally:
        _cancel_all(web, [late])


def test_a_storage_failure_writes_nothing_and_is_retried(release_harness, workbench):
    web, _, oid, _, _ = release_harness
    calls = 0
    real = web.authorize_release

    def failing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise PersistenceError("STORAGE_UNAVAILABLE")

    workbench.releases.authorize_release = failing
    assert workbench.release_authorize_pass() == []
    assert calls == 1 and _audit(oid) == []
    workbench.releases.authorize_release = real
    assert [r.outcome for r in workbench.release_authorize_pass()] == ["applied"]


def test_the_limit_bounds_one_pass(release_harness, workbench):
    _, _, oid, _, _ = release_harness
    assert workbench.release_authorize_pass(limit=0) == []
    assert _audit(oid) == []


def _register(
    web: Any,
    model: Any,
    count: int,
    *,
    released_ago: float = 10,
    resource_uid: str | None = None,
) -> list[UUID]:
    """``count`` more pending releases on ``model``'s target (or on the
    target ``resource_uid`` names, same namespace and workload), profile
    and policy, newest last."""
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT content FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (model.health_profile_revision,),
        ).fetchone()
    assert row is not None
    source = "pr5b-" + uuid4().hex
    oids = []
    for _ in range(count):
        receipt = web.record_release(
            event=ReleaseEvent(
                format_version=1,
                source=source,
                source_release_id=uuid4().hex,
                released_at=datetime.now(timezone.utc)
                - timedelta(seconds=released_ago),
                before=model.before_revision,
                after=model.after_revision,
                auxiliary=model.auxiliary,
            ),
            target_id=None if resource_uid else model.target_id,
            resource_uid=resource_uid,
            identity=model.target_snapshot
            if resource_uid is None
            else {**model.target_snapshot, "resource_uid": resource_uid},
            health_profile_revision=model.health_profile_revision,
            health_profile=row[0],
            min_tracking_seconds=model.min_tracking_seconds,
            actor="pr5b-impl",
            raw_body=b"{}",
        )
        assert receipt.outcome == "created", receipt
        oids.append(receipt.observation_id)
    return oids


def _cancel_all(web: Any, oids: list[UUID]) -> None:
    for oid in oids:
        row = web.get_release(oid)
        if row.status in ("pending", "observing"):
            web.control_release(
                oid,
                action="cancel",
                actor="cleanup",
                expected_control_generation=row.control_generation,
                idempotency_key="cleanup",
                calibrated_policy_revision=None,
            )


def _authorized_by_pass(oids: list[UUID]) -> set[UUID]:
    with psycopg.connect(DSN) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT observation_id FROM opspilot_release_audit"
                " WHERE observation_id=ANY(%s) AND idempotency_key LIKE 'auto-authorize:%%'",
                (oids,),
            )
        }


def test_decided_releases_never_take_a_candidate_place(release_harness):
    """Codex P2 on #349: more decided releases than one read holds, older
    than the one still awaiting authorization, never crowd it out."""
    web, _, oid, _, _ = release_harness
    model = web.get_release(oid)
    occupants: list[UUID] = []
    waiting: list[UUID] = []
    try:
        occupants = _register(web, model, 250, resource_uid="pr5b-" + uuid4().hex)
        _cancel_all(web, occupants)
        # The newest release of the calibrated target (the harness's own
        # release is now superseded by it).
        waiting = _register(web, model, 1)
        store = DurableStore(web.dsn)
        try:
            bench = Workbench(
                incidents=DurableIncidentStore(store),
                events=DurableEventLog(store),
                evidence=DurableEvidenceStore(store),
                ledger=DurableWebLedger(store),
                run_versions=fixture_versions(),
                run_seconds=600,
                tool_face=fixture_face(),
                releases=web,
            )
            # The shared lab may hold other tests' pending releases too; each
            # pass decides up to 20, so the bound leaves room for them.
            for _ in range(30):
                if _authorized_by_pass(waiting) == set(waiting):
                    break
                bench.release_authorize_pass()
        finally:
            store.close()
        assert _authorized_by_pass(waiting) == set(waiting)
        assert web.get_release(waiting[0]).status == "observing"
        assert not _authorized_by_pass(occupants + [oid])
    finally:
        _cancel_all(web, waiting + occupants)


def _active_release_sessions(target_id: UUID) -> int:
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT count(*) FROM opspilot_observation_sessions WHERE target_id=%s"
            " AND release_observation_id IS NOT NULL AND state='authorized'",
            (target_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_one_target_observes_one_release_at_a_time(release_harness, workbench):
    """Codex security P2 on #349 (r2, option B): repeated deliveries on one
    calibrated target never run more than one release session there. The
    newest release waits for the running observation to end; older ones
    are superseded, never observed, and end unknown at their deadline."""
    web, observer, first, _, _ = release_harness
    model = web.get_release(first)
    target = model.target_id
    app = create_app(workbench, Authenticator(config()), FakeClock(start=NOW))

    def hold(oid: UUID) -> Any:
        page = call(app, "GET", f"/version-changes/{oid}", headers=basic())
        assert page.status == 200
        return page.json()["authorization_hold"]

    assert [r.observation_id for r in workbench.release_authorize_pass()] == [first]
    assert _active_release_sessions(target) == 1
    older = _register(web, model, 2, released_ago=model.deadline_seconds - 4)
    (newest,) = _register(web, model, 1)
    try:
        workbench.releases._oids |= {*older, newest}
        assert workbench.release_authorize_pass() == []
        assert _active_release_sessions(target) == 1
        assert [hold(o) for o in older] == ["superseded", "superseded"]
        assert hold(newest) == "waiting_for_target"
        assert hold(first) is None
        listed = call(app, "GET", "/version-changes", headers=basic()).json()
        holds = {
            r["observation_id"]: r["authorization_hold"] for r in listed["releases"]
        }
        assert holds[str(newest)] == "waiting_for_target"
        assert holds[str(older[0])] == "superseded"
        html = call(
            app,
            "GET",
            f"/version-changes/{newest}",
            headers={**basic(), "accept": "text/html"},
        )
        assert "waiting for the observation of another release" in html.text
        # Nothing was refused: the waiting releases carry no audit.
        assert all(_audit(o) == [] for o in (*older, newest))
        # The running observation ends; the newest release is next.
        cancelled = web.control_release(
            first,
            action="cancel",
            actor="operator",
            expected_control_generation=0,
            idempotency_key="pr5b-end",
            calibrated_policy_revision=None,
        )
        assert cancelled.outcome == "applied"
        assert _active_release_sessions(target) == 0
        assert [r.observation_id for r in workbench.release_authorize_pass()] == [
            newest
        ]
        assert _active_release_sessions(target) == 1
        assert workbench.release_authorize_pass() == []
        assert _active_release_sessions(target) == 1
        assert [hold(o) for o in older] == ["superseded", "superseded"]
        # The superseded ones are never observed and end at their deadline.
        time.sleep(
            max(
                0.0,
                (
                    web.get_release(older[-1]).deadline_at - datetime.now(timezone.utc)
                ).total_seconds(),
            )
            + 0.5
        )
        observer.sweep_expired_releases(limit=1000)
        for oid in older:
            release = web.get_release(oid)
            assert (release.status, release.ended_reason) == (
                "unknown",
                "deadline_expired",
            )
            assert [a["action"] for a in _audit(oid)] == ["deadline_expired"]
            assert hold(oid) is None
    finally:
        _cancel_all(web, [*older, newest])
