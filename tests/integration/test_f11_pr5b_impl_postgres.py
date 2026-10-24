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
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from opspilot.observation import release_policy
from opspilot.observation.releases import calibrated_revision_for
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools.fixture import fixture_face, fixture_versions
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.service import Workbench
from opspilot.web.store import DurableIncidentStore, DurableWebLedger
from scripts.m0.postgres_lab import DSN
from tests.integration import test_f11_pr2b_impl_postgres as pr2b

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
release_harness = pr2b.release_harness


class _Only:
    """The web role's ``ReleaseStore`` with its listing narrowed to one
    release, so a pass never touches another test's rows."""

    def __init__(self, store: Any, oid: UUID) -> None:
        self._store = store
        self._oid = oid

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def list_releases(self, *, limit: int) -> tuple[Any, ...]:
        return tuple(
            r
            for r in self._store.list_releases(limit=limit)
            if r.observation_id == self._oid
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
    real = web.release_audit

    def pause_then_read(observation_id: UUID, **kwargs: Any) -> Any:
        # The human's pause lands between the pass's read and its command.
        web.control_release(
            oid,
            action="pause",
            actor="operator",
            expected_control_generation=0,
            idempotency_key="pr5b-race",
            calibrated_policy_revision=None,
        )
        return real(observation_id, **kwargs)

    workbench.releases.release_audit = pause_then_read
    receipts = workbench.release_authorize_pass()
    assert [(r.outcome, r.reason) for r in receipts] == [
        ("rejected", "CONTROL_CONFLICT")
    ]
    del workbench.releases.release_audit
    assert workbench.release_authorize_pass() == []
    assert [(a["action"], a["outcome"]) for a in _audit(oid)] == [
        ("pause", "applied"),
        ("authorize", "rejected"),
    ]


def test_after_the_deadline_nothing_is_tried(release_harness, workbench, monkeypatch):
    web, _, oid, _, _ = release_harness
    deadline = web.get_release(oid).deadline_at
    monkeypatch.setattr(
        workbench.incidents, "now", lambda: deadline + timedelta(seconds=1)
    )
    assert workbench.release_authorize_pass() == []
    assert _audit(oid) == []


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
