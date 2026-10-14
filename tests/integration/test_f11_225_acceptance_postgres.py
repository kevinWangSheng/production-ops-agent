"""Independent acceptance tests for issue #225, item 1: the deadline-expiry
evidence guard of migration 0015 (``opspilot_release_update_evidence``).

Written from ``docs/tasks/2026-10-13-f11-pr2-contract.md`` (固定语义 9
「清扫与锁协议」, 11 「DB 守卫与权限」, the ending table) and the issue text,
without reading any fix. The coordinator's ruling (contract 9 wins over the
narrower issue wording):

* a release that currently has **no** ``authorized`` session (pending and
  never authorized, or observing whose session already ended through
  pause / scope revocation) may go to ``unknown`` on the audit-only path:
  an ``applied`` ``deadline_expired`` audit in the same transaction;
* a release that **has** an ``authorized`` session needs, in the same
  transaction, the matching deadline ending (same subject / session /
  generation, ``deadline_expired`` / ``release_unknown``,
  observing→unknown) and that session ended; otherwise SQLSTATE 42501.

Fault injection runs erroneous SQL directly under the real Observer login
role (as the PR-2a guard tests do); legal paths go through the public sweep.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore
from opspilot.persistence.grants import WEB_ROLE
from scripts.m0.postgres_lab import DSN
from tests.f11_release_support import empty_evidence, sha256_hex
from tests.integration.test_f11_release_observation_postgres import (
    AUDIT,
    ENDINGS,
    OBSERVER_ROLE,
    POOL,
    RELEASES,
    SESSIONS,
    Catalog,
    _authorize,
    _claim,
    _control,
    _ending_id,
    _endings,
    _entry,
    _expiring,
    _release_sessions,
    _session,
    _state,
    _wait_past,
    _window_end,
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def owner() -> Iterator[DurableStore]:
    store = DurableStore(DSN, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture(scope="module")
def web_dsn() -> Iterator[str]:
    with login(DSN, WEB_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def observer_dsn() -> Iterator[str]:
    with login(DSN, OBSERVER_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def web(web_dsn: str) -> ReleaseStore:
    return ReleaseStore(web_dsn, pool=POOL)


@pytest.fixture(scope="module")
def observer(observer_dsn: str) -> ObservationStore:
    return ObservationStore(observer_dsn, pool=POOL)


@pytest.fixture(scope="module")
def reader() -> ReleaseStore:
    return ReleaseStore(DSN, pool=POOL)


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Catalog:
    return Catalog(monkeypatch, tmp_path)


# --- builders ---------------------------------------------------------------


def _expired_observing(
    owner: DurableStore, web: ReleaseStore, catalog: Catalog
) -> tuple[UUID, UUID]:
    """An observing release with a still-authorized session whose frozen
    deadline has passed (nobody swept it yet)."""
    release = _expiring(owner, web, seconds=8.0)
    entry = _entry(release)
    catalog.add(entry)
    receipt = _authorize(web, release.oid, entry["revision"])
    assert receipt.outcome == "applied", receipt
    assert receipt.session_id is not None
    _wait_past(release.record.deadline_at)
    assert _session(receipt.session_id)["state"] == "authorized"
    return release.oid, receipt.session_id


def _forge_expiry(
    observer_dsn: str,
    oid: UUID,
    *,
    ending: dict[str, Any] | None = None,
    end_session: tuple[UUID, str, str] | None = None,
    audit: bool = True,
) -> None:
    """One Observer transaction: optionally an ending and a session end, then
    an applied ``deadline_expired`` audit and release→unknown; COMMIT."""
    with psycopg.connect(observer_dsn, row_factory=dict_row) as conn:
        try:
            release = conn.execute(
                f"SELECT status,paused,control_generation,observation_generation "
                f"FROM {RELEASES} WHERE observation_id=%s",
                (oid,),
            ).fetchone()
            assert release is not None
            sequence = conn.execute(
                f"SELECT coalesce(max(sequence),0)+1 AS n FROM {AUDIT} "
                "WHERE observation_id=%s",
                (oid,),
            ).fetchone()["n"]
            ending_id = None
            session_id = None
            if ending is not None:
                conn.execute(
                    f"INSERT INTO {ENDINGS} (ending_id,session_id,release_observation_id,"
                    "ended_reason,transition,subject_state_before,subject_state_after) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        ending["ending_id"],
                        ending["session_id"],
                        oid,
                        ending["ended_reason"],
                        ending["transition"],
                        ending["before"],
                        ending["after"],
                    ),
                )
                ending_id, session_id = ending["ending_id"], ending["session_id"]
            if end_session is not None:
                sid, state, reason = end_session
                conn.execute(
                    f"UPDATE {SESSIONS} SET state=%s,ended_reason=%s,"
                    "active_sample_job_id=NULL,active_sample_sequence=NULL,"
                    "active_sample_due_at=NULL,active_sample_owner=NULL,"
                    "active_sample_lease_until=NULL,"
                    "updated_at=clock_timestamp() WHERE session_id=%s",
                    (state, reason, sid),
                )
            if audit:
                conn.execute(
                    f"INSERT INTO {AUDIT} (audit_id,observation_id,sequence,idempotency_key,"
                    "request_sha256,action,principal_kind,actor,outcome,reason,"
                    "expected_control_generation,control_generation_before,"
                    "control_generation_after,observation_generation_before,"
                    "observation_generation_after,status_before,status_after,"
                    "paused_before,paused_after,session_id,ending_id,result) "
                    "VALUES (%s,%s,%s,'system:deadline',%s,'deadline_expired','observer',"
                    "'observer','applied',NULL,%s,%s,%s,%s,%s,%s,'unknown',%s,%s,%s,%s,"
                    "'{}'::jsonb)",
                    (
                        uuid4(),
                        oid,
                        sequence,
                        sha256_hex(f"forged-expiry-{oid}"),
                        release["control_generation"],
                        release["control_generation"],
                        release["control_generation"],
                        release["observation_generation"],
                        release["observation_generation"],
                        release["status"],
                        release["paused"],
                        release["paused"],
                        session_id,
                        ending_id,
                    ),
                )
            conn.execute(
                f"UPDATE {RELEASES} SET status='unknown',ended_reason='deadline_expired',"
                "ended_at=clock_timestamp(),updated_at=clock_timestamp() "
                "WHERE observation_id=%s",
                (oid,),
            )
            conn.commit()
        finally:
            conn.rollback()


def _assert_refused_and_untouched(
    observer_dsn: str,
    reader: ReleaseStore,
    oid: UUID,
    session_id: UUID,
    **forge: Any,
) -> None:
    before = _state(reader.get_release(oid))
    endings_before = _endings(oid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _forge_expiry(observer_dsn, oid, **forge)
    assert _state(reader.get_release(oid)) == before
    assert reader.get_release(oid).status == "observing"
    assert _session(session_id)["state"] == "authorized"
    assert _endings(oid) == endings_before


def _sweep_all(observer: ObservationStore) -> list[UUID]:
    swept: list[UUID] = []
    for _ in range(200):
        batch = observer.sweep_expired_releases(limit=100)
        if not batch:
            return swept
        swept.extend(batch)
    raise AssertionError("sweep did not converge")


# ===========================================================================
# (a) audit-only expiry of a release that has an authorized session
# ===========================================================================


def test_audit_only_expiry_with_an_authorized_session_is_refused(
    owner: DurableStore,
    web: ReleaseStore,
    observer_dsn: str,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    oid, session_id = _expired_observing(owner, web, catalog)
    _assert_refused_and_untouched(observer_dsn, reader, oid, session_id)


# ===========================================================================
# (b) an ending that does not match the deadline expiry of that session
# ===========================================================================
#
# A *different* session or an *older generation* cannot be named by a fresh
# ending: every release session has at most one ending (unique index), every
# earlier session of a release already has its ending (pause / resume), and
# the ending's session must belong to the same release (FK). The mismatches
# that remain constructible are the reason / edge / session-end ones below.

MISMATCHES: dict[str, dict[str, Any]] = {
    # the right deadline ending, but the session is left authorized
    "session_not_ended": dict(
        reason="deadline_expired",
        transition="release_unknown",
        before="observing",
        after="unknown",
        end=None,
    ),
    # a pause-style ending (non-terminal) instead of the deadline ending
    "authority_revoked_ending": dict(
        reason="authority_revoked",
        transition=None,
        before="observing",
        after="observing",
        end=("revoked", "authority_revoked"),
    ),
    # a scope-revocation ending instead of the deadline ending
    "scope_suspended_ending": dict(
        reason="scope_suspended",
        transition=None,
        before="observing",
        after="observing",
        end=("revoked", "scope_suspended"),
    ),
    # an expiry with another reason whose own evidence (budget) is absent
    "max_samples_ending_without_budget": dict(
        reason="max_samples_exhausted",
        transition="release_unknown",
        before="observing",
        after="unknown",
        end=("expired", "max_samples_exhausted"),
    ),
    # the deadline ending with the wrong subject-state edge
    "deadline_ending_from_pending": dict(
        reason="deadline_expired",
        transition="release_unknown",
        before="pending",
        after="unknown",
        end=("expired", "deadline_expired"),
    ),
}


@pytest.mark.parametrize("case", sorted(MISMATCHES))
def test_expiry_with_a_mismatched_ending_is_refused(
    owner: DurableStore,
    web: ReleaseStore,
    observer_dsn: str,
    reader: ReleaseStore,
    catalog: Catalog,
    case: str,
) -> None:
    spec = MISMATCHES[case]
    oid, session_id = _expired_observing(owner, web, catalog)
    end = spec["end"]
    _assert_refused_and_untouched(
        observer_dsn,
        reader,
        oid,
        session_id,
        ending={
            "ending_id": _ending_id(oid, session_id),
            "session_id": session_id,
            "ended_reason": spec["reason"],
            "transition": spec["transition"],
            "before": spec["before"],
            "after": spec["after"],
        },
        end_session=None if end is None else (session_id, *end),
    )


# ===========================================================================
# (c) legal paths stay open
# ===========================================================================


def test_audit_only_expiry_of_a_pending_release_without_session_commits(
    owner: DurableStore, web: ReleaseStore, observer_dsn: str, reader: ReleaseStore
) -> None:
    """The guard itself still accepts the audit-only path for a pending
    release that never had a session (contract 9: no session is invented)."""
    release = _expiring(owner, web, seconds=6.0)
    _wait_past(release.record.deadline_at)

    _forge_expiry(observer_dsn, release.oid)

    record = reader.get_release(release.oid)
    assert (record.status, record.ended_reason) == ("unknown", "deadline_expired")
    assert _release_sessions(release.oid) == [] and _endings(release.oid) == []


def test_a_matching_deadline_ending_with_the_session_ended_commits(
    owner: DurableStore,
    web: ReleaseStore,
    observer_dsn: str,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    """Positive control for the injection helper: the same Observer
    transaction with the *matching* deadline ending and the session expired
    is the evidence the guard requires, and commits."""
    oid, session_id = _expired_observing(owner, web, catalog)

    _forge_expiry(
        observer_dsn,
        oid,
        ending={
            "ending_id": _ending_id(oid, session_id),
            "session_id": session_id,
            "ended_reason": "deadline_expired",
            "transition": "release_unknown",
            "before": "observing",
            "after": "unknown",
        },
        end_session=(session_id, "expired", "deadline_expired"),
    )

    record = reader.get_release(oid)
    assert (record.status, record.ended_reason) == ("unknown", "deadline_expired")
    assert _session(session_id)["state"] == "expired"


def _audit_only_sweep_assertions(
    reader: ReleaseStore, oid: UUID, swept: list[UUID], endings_before: list[Any]
) -> None:
    assert swept.count(oid) == 1
    record = reader.get_release(oid)
    assert (record.status, record.ended_reason) == ("unknown", "deadline_expired")
    row = reader.release_audit(oid)[-1]
    assert (row["action"], row["outcome"]) == ("deadline_expired", "applied")
    assert row["status_before"] in ("pending", "observing"), row
    assert row["status_after"] == "unknown"
    assert row["idempotency_key"] == "system:deadline"
    assert (row["actor"], row["principal_kind"]) == ("observer", "observer")
    # audit only: no ending is invented, no session is touched
    assert _endings(oid) == endings_before
    assert not any(s["state"] == "authorized" for s in _release_sessions(oid))


def test_sweep_of_an_expired_pending_release_is_audit_only(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
) -> None:
    release = _expiring(owner, web, seconds=6.0)
    _wait_past(release.record.deadline_at)

    swept = _sweep_all(observer)

    _audit_only_sweep_assertions(reader, release.oid, swept, [])
    assert reader.release_audit(release.oid)[-1]["status_before"] == "pending"
    assert _release_sessions(release.oid) == []


def test_sweep_of_a_paused_observing_release_is_audit_only(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    release = _expiring(owner, web, seconds=8.0)
    entry = _entry(release)
    catalog.add(entry)
    authorized = _authorize(web, release.oid, entry["revision"])
    assert authorized.outcome == "applied"
    assert _control(web, release.oid, "pause", 0).outcome == "applied"
    record = reader.get_release(release.oid)
    assert (record.status, record.paused) == ("observing", True)
    endings_before = _endings(release.oid)
    assert [e["ended_reason"] for e in endings_before] == ["authority_revoked"]
    _wait_past(release.record.deadline_at)

    swept = _sweep_all(observer)

    _audit_only_sweep_assertions(reader, release.oid, swept, endings_before)
    assert reader.release_audit(release.oid)[-1]["status_before"] == "observing"


def test_sweep_of_a_scope_revoked_observing_release_is_audit_only(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    release = _expiring(owner, web, seconds=10.0)
    entry = _entry(release)
    catalog.add(entry)
    authorized = _authorize(web, release.oid, entry["revision"])
    assert authorized.outcome == "applied" and authorized.session_id is not None
    lease = _claim(observer, authorized.session_id)
    generation = owner.set_target_suspension(
        release.target_id, True, expected_generation=0, actor="t"
    )
    try:
        receipt = observer.submit_release_sample(lease, empty_evidence(_window_end()))
    finally:
        owner.set_target_suspension(
            release.target_id, False, expected_generation=generation, actor="t"
        )
    assert (receipt.disposition, receipt.reason) == ("history_only", "suspended")
    assert _session(authorized.session_id)["state"] != "authorized"
    assert reader.get_release(release.oid).status == "observing"
    endings_before = _endings(release.oid)
    assert len(endings_before) == 1
    assert endings_before[0]["transition"] is None
    _wait_past(release.record.deadline_at)

    swept = _sweep_all(observer)

    _audit_only_sweep_assertions(reader, release.oid, swept, endings_before)
    assert reader.release_audit(release.oid)[-1]["status_before"] == "observing"


def test_sweep_of_an_observing_release_with_a_session_writes_the_deadline_ending(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    oid, session_id = _expired_observing(owner, web, catalog)
    record = reader.get_release(oid)

    swept = _sweep_all(observer)

    assert swept.count(oid) == 1
    after = reader.get_release(oid)
    assert (after.status, after.ended_reason) == ("unknown", "deadline_expired")
    session = _session(session_id)
    assert (session["state"], session["ended_reason"]) == (
        "expired",
        "deadline_expired",
    )
    assert session["active_sample_job_id"] is None
    (ending,) = _endings(oid)
    assert ending["ending_id"] == _ending_id(oid, session_id)
    assert ending["session_id"] == session_id
    assert (ending["ended_reason"], ending["transition"]) == (
        "deadline_expired",
        "release_unknown",
    )
    assert (ending["subject_state_before"], ending["subject_state_after"]) == (
        "observing",
        "unknown",
    )
    assert session["subject_control_generation"] == record.control_generation
    assert session["observation_generation"] == record.observation_generation
    row = reader.release_audit(oid)[-1]
    assert (row["action"], row["outcome"]) == ("deadline_expired", "applied")
    assert (row["status_before"], row["status_after"]) == ("observing", "unknown")
