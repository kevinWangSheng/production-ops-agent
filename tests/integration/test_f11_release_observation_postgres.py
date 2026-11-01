"""Independent contract tests for F11 PR-2a: calibrated release-observation
authorization and control, atomic sample adoption and endings, the expiry
sweep, release history and offline replay, migration 0015 and the runtime
role guards.

Written from ``docs/tasks/2026-10-13-f11-pr2-contract.md`` (「拆分」 and
「接口定稿（r1）」) without reading the implementation. Behaviour is driven
through the public ``ReleaseStore`` / ``ObservationStore`` / replay entry
points under the real runtime login roles (web authorizes and controls, the
Observer claims, submits and sweeps). SQL is used only to read the tables
and columns the contract names, to construct fixtures the established F6
tests also construct (backdated ``authorized_at``, a job made due now, an
expired lease) and to attempt writes a role or constraint must refuse.

Tests that need a *sufficient* sample adopted go through the query-plan
seam in ``tests.f11_release_support`` (contract gap; see that module).
"""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import BaseModel

from opspilot import schema
from opspilot.domain.evidence import QueryWindow
from opspilot.domain.observation import HealthSample
from opspilot.observation import (
    ObservationStore,
    ReplayReport,
    SampleLease,
    release_policy,
)
from opspilot.observation.release_types import (
    ReleaseSampleEvidence,
    ReleaseSampleLease,
    ReleaseSampleReceipt,
)
from opspilot.observation.releases import (
    ReleaseAuxiliary,
    ReleaseCommandReceipt,
    ReleaseEvent,
    ReleaseRecord,
    ReleaseRevision,
    ReleaseStore,
)
from opspilot.observer import replay as observer_replay
from opspilot.observer.release_replay import ReleaseReplay, replay_release_history
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig, close_pools
from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE
from scripts.m0.postgres_lab import DSN
from tests.f11_release_support import (
    AFTER_HASH,
    BEFORE_HASH,
    EMPTY_CATALOG,
    INTERVAL,
    PROFILE,
    PROFILE_CONTENT,
    PROFILE_REVISION,
    PROFILE_SHA256,
    WINDOW,
    EvidenceBuilder,
    Pod,
    assert_shipped_catalog_is_the_approved_policy,
    canonical_json,
    catalog_text,
    digest,
    empty_evidence,
    planned_texts,
    pods_for,
    policy_content,
    policy_entry,
    sha256_hex,
)
from tests.runtime_roles_support import catalog_grants, login
from tests.target_support import IDENTITY, WORKLOAD

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=1, max_size=6, timeout=5.0)
OBSERVER_ROLE = "opspilot_observer"
RELEASES = "opspilot_release_observations"
SESSIONS = "opspilot_observation_sessions"
SAMPLES = "opspilot_observation_samples"
ENDINGS = "opspilot_observation_endings"
AUDIT = "opspilot_release_audit"
QUERY_RESULTS = "opspilot_release_query_results"
BEFORE_0015 = "0014_release_observations"
REVISION_0015 = "0015_release_observation_sessions"
HEAD = "0024_trace_outbox"

SESSION = PROFILE.session  # deadline 3600, max 40, cadence 60, window 600
MIN_TRACKING = 600  # M: explicit test value, not a calibration
AFTER_IMAGES = {"checkout": digest("checkout-after")}
BEFORE_IMAGES = {"checkout": digest("checkout-before")}
CATALOG_CODES = (
    "RELEASE_POLICY_CATALOG_INVALID",
    "RELEASE_POLICY_UNCALIBRATED",
    "RELEASE_POLICY_MISMATCH",
    "RELEASE_POLICY_UNSUPPORTED",
)
FENCE_REASONS = {
    "lease_revoked",
    "control_generation_stale",
    "observation_generation_stale",
    "subject_state_not_adoptable",
}
HISTORY_KEYS = {
    "format_version",
    "release",
    "health_profile",
    "sessions",
    "samples",
    "endings",
    "audit",
}


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
def worker_dsn() -> Iterator[str]:
    with login(DSN, WORKER_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def web(web_dsn: str) -> ReleaseStore:
    """The Controller side: records, authorizes and controls releases."""
    return ReleaseStore(web_dsn, pool=POOL)


@pytest.fixture(scope="module")
def observer(observer_dsn: str) -> ObservationStore:
    """The Observer process: claims, submits and sweeps."""
    return ObservationStore(observer_dsn, pool=POOL)


@pytest.fixture(scope="module")
def reader() -> ReleaseStore:
    """Owner-side reads of release records and audits."""
    return ReleaseStore(DSN, pool=POOL)


@pytest.fixture(scope="module")
def history() -> ObservationStore:
    """Owner-side reads of release history (one snapshot)."""
    return ObservationStore(DSN, pool=POOL)


class Catalog:
    """A test catalog swapped in through the public path (公开接口 1: tests may
    replace ``RELEASE_POLICY_CATALOG_PATH``); never the product file."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
        self.monkeypatch = monkeypatch
        self.path = directory / f"release-calibrations-{uuid4().hex}.json"
        self.entries: list[dict[str, Any]] = []

    def add(self, *entries: dict[str, Any]) -> None:
        self.entries.extend(entries)
        self.write(catalog_text(*self.entries))

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")
        self.point()

    def empty(self) -> None:
        self.entries.clear()
        self.write(EMPTY_CATALOG)

    def remove(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self.point()

    def point(self) -> None:
        self.monkeypatch.setattr(
            release_policy, "RELEASE_POLICY_CATALOG_PATH", self.path
        )


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Catalog:
    return Catalog(monkeypatch, tmp_path)


# --- SQL helpers ------------------------------------------------------------


def _db_now(dsn: str = DSN) -> datetime:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return row[0]


def _rows(
    statement: str, params: Sequence[Any] = (), dsn: str = DSN
) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return list(conn.execute(statement, params).fetchall())


def _row(statement: str, params: Sequence[Any] = (), dsn: str = DSN) -> dict[str, Any]:
    rows = _rows(statement, params, dsn)
    assert len(rows) == 1, rows
    return rows[0]


def _exec(statement: str, params: Sequence[Any] = (), dsn: str = DSN) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(statement, params)


def _refused(
    dsn: str,
    statement: str,
    params: Sequence[Any] = (),
    errors: type[BaseException] | tuple[type[BaseException], ...] = (
        psycopg.errors.InsufficientPrivilege
    ),
    *,
    commit: bool = True,
) -> None:
    """The write fails at the statement or, for deferred evidence guards, at
    COMMIT. ``commit=False`` never commits (destructive owner statements):
    whatever happens, the transaction is rolled back."""
    with psycopg.connect(dsn) as conn:
        try:
            with pytest.raises(errors):
                conn.execute(statement, params)
                if commit:
                    conn.commit()
        finally:
            conn.rollback()


def _release_sessions(oid: UUID) -> list[dict[str, Any]]:
    return _rows(
        f"SELECT * FROM {SESSIONS} WHERE release_observation_id=%s "
        "ORDER BY observation_generation,session_id",
        (oid,),
    )


def _session(session_id: UUID) -> dict[str, Any]:
    return _row(f"SELECT * FROM {SESSIONS} WHERE session_id=%s", (session_id,))


def _endings(oid: UUID) -> list[dict[str, Any]]:
    return _rows(
        f"SELECT * FROM {ENDINGS} WHERE release_observation_id=%s "
        "ORDER BY recorded_at,ending_id",
        (oid,),
    )


def _sample(sample_id: UUID) -> dict[str, Any]:
    return _row(f"SELECT * FROM {SAMPLES} WHERE sample_id=%s", (sample_id,))


def _audit_count(oid: UUID) -> int:
    return int(
        _row(f"SELECT count(*) AS n FROM {AUDIT} WHERE observation_id=%s", (oid,))["n"]
    )


def _ending_id(oid: UUID, session_id: UUID) -> UUID:
    return uuid5(oid, "session-ending:" + str(session_id))


def _key() -> str:
    return f"key-{uuid4().hex}"


# --- targets, releases, policies -------------------------------------------


def _target(store: DurableStore) -> tuple[UUID, dict[str, str]]:
    resource_uid = f"deployment/checkout-f11pr2-{uuid4().hex[:10]}"
    target_id = store.register_target(resource_uid)
    identity = {**IDENTITY, "resource_uid": resource_uid, "workload": WORKLOAD}
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_targets SET integration_id=%s,cluster_uid=%s,"
            "namespace=%s,workload=%s WHERE target_id=%s",
            (
                identity["integration_id"],
                identity["cluster_uid"],
                identity["namespace"],
                WORKLOAD,
                target_id,
            ),
        )
    return target_id, identity


@dataclass
class Release:
    record: ReleaseRecord
    identity: dict[str, str]

    @property
    def oid(self) -> UUID:
        return self.record.observation_id

    @property
    def target_id(self) -> UUID:
        return self.record.target_id


def _record_release(
    owner: DurableStore,
    store: ReleaseStore,
    *,
    released_ago: float = 2000.0,
    min_tracking: int = MIN_TRACKING,
    before_images: Mapping[str, str] = BEFORE_IMAGES,
    dsn: str = DSN,
) -> Release:
    """A pending release recorded through the PR-1 entry (unchanged)."""
    target_id, identity = _target(owner)
    event = ReleaseEvent(
        format_version=1,
        source="argo-rollouts",
        source_release_id=f"rel-{uuid4().hex}",
        released_at=_db_now(dsn) - timedelta(seconds=released_ago),
        before=ReleaseRevision(
            images=dict(before_images), pod_template_digest=digest("tmpl-before")
        ),
        after=ReleaseRevision(
            images=dict(AFTER_IMAGES), pod_template_digest=digest("tmpl-after")
        ),
        auxiliary=ReleaseAuxiliary(
            before_pod_template_hash=BEFORE_HASH, after_pod_template_hash=AFTER_HASH
        ),
    )
    receipt = store.record_release(
        event=event,
        target_id=target_id,
        identity=identity,
        health_profile_revision=PROFILE_REVISION,
        health_profile=PROFILE_CONTENT,
        min_tracking_seconds=min_tracking,
        actor="release-source:test",
        raw_body=event.model_dump_json().encode(),
    )
    assert (receipt.outcome, receipt.status) == ("created", "pending"), receipt
    assert receipt.observation_id is not None
    return Release(store.get_release(receipt.observation_id), identity)


def _entry(
    release: Release, *, min_tracking: int | None = None, **overrides: Any
) -> dict[str, Any]:
    tracking = (
        release.record.min_tracking_seconds if min_tracking is None else min_tracking
    )
    return policy_entry(
        policy_content(release.identity, min_tracking_seconds=tracking, **overrides)
    )


def _authorize(
    store: ReleaseStore,
    oid: UUID,
    revision: str,
    *,
    expected: int = 0,
    key: str | None = None,
    actor: str = "controller:test",
) -> ReleaseCommandReceipt:
    return store.authorize_release(
        oid,
        actor=actor,
        expected_control_generation=expected,
        idempotency_key=key or _key(),
        calibrated_policy_revision=revision,
    )


def _control(
    store: ReleaseStore,
    oid: UUID,
    action: str,
    expected: int,
    *,
    revision: str | None = None,
    key: str | None = None,
    actor: str = "operator:test",
) -> ReleaseCommandReceipt:
    return store.control_release(
        oid,
        action=action,  # type: ignore[arg-type]
        actor=actor,
        expected_control_generation=expected,
        idempotency_key=key or _key(),
        calibrated_policy_revision=revision,
    )


@dataclass
class Observing:
    release: Release
    entry: dict[str, Any]
    receipt: ReleaseCommandReceipt

    @property
    def oid(self) -> UUID:
        return self.release.oid

    @property
    def revision(self) -> str:
        return str(self.entry["revision"])

    @property
    def session_id(self) -> UUID:
        assert self.receipt.session_id is not None
        return self.receipt.session_id


def _observing(
    owner: DurableStore, web: ReleaseStore, catalog: Catalog, **release: Any
) -> Observing:
    recorded = _record_release(owner, web, **release)
    entry = _entry(recorded)
    catalog.add(entry)
    receipt = _authorize(web, recorded.oid, entry["revision"])
    assert receipt.outcome == "applied", receipt
    return Observing(recorded, entry, receipt)


def _state(record: ReleaseRecord) -> tuple[Any, ...]:
    return (
        record.status,
        record.paused,
        record.control_generation,
        record.observation_generation,
        record.used_sample_count,
        record.deadline_at,
        record.max_samples,
        record.ended_reason,
        record.ended_at,
        record.calibrated_policy_revision,
        record.calibrated_policy_content,
        record.calibrated_policy_sha256,
    )


def _assert_rejected(
    reader: ReleaseStore,
    receipt: ReleaseCommandReceipt,
    reason: str,
    before: ReleaseRecord,
    audit_before: int,
) -> None:
    """A first-time business rejection: one audit row, subject unchanged."""
    assert (receipt.outcome, receipt.reason, receipt.replayed) == (
        "rejected",
        reason,
        False,
    ), receipt
    after = reader.get_release(before.observation_id)
    assert _state(after) == _state(before)
    assert receipt.status == before.status
    assert receipt.paused == before.paused
    assert receipt.control_generation == before.control_generation
    assert receipt.observation_generation == before.observation_generation
    assert receipt.used_sample_count == before.used_sample_count
    assert receipt.deadline_at == before.deadline_at
    audit = reader.release_audit(before.observation_id)
    assert len(audit) == audit_before + 1
    row = audit[-1]
    assert row["audit_id"] == receipt.audit_id
    assert row["sequence"] == receipt.sequence
    assert (row["outcome"], row["reason"]) == ("rejected", reason)
    assert row["control_generation_before"] == row["control_generation_after"]
    assert row["observation_generation_before"] == row["observation_generation_after"]
    assert row["status_before"] == row["status_after"]
    assert row["paused_before"] == row["paused_after"]


# --- sampling helpers -------------------------------------------------------


def _make_due(session_id: UUID, *, expire_lease: bool = False) -> None:
    """Skip the sampling interval (the established F6 test construction)."""
    lease = ",active_sample_lease_until=clock_timestamp()-interval '1 second'"
    _exec(
        f"UPDATE {SESSIONS} SET active_sample_due_at=clock_timestamp()-interval '1 day'"
        + (lease if expire_lease else "")
        + " WHERE session_id=%s AND active_sample_job_id IS NOT NULL",
        (session_id,),
    )


def _claims(observer: ObservationStore, session_id: UUID) -> list[ReleaseSampleLease]:
    return [
        lease
        for lease in observer.claim_due_samples(uuid4(), limit=50)
        if isinstance(lease, ReleaseSampleLease) and lease.session_id == session_id
    ]


def _claim(observer: ObservationStore, session_id: UUID) -> ReleaseSampleLease:
    _make_due(session_id)
    leases = _claims(observer, session_id)
    assert len(leases) == 1, leases
    return leases[0]


def _window_end() -> datetime:
    """T: a DB instant just before the submission's ``decision_at``."""
    return _db_now() - timedelta(milliseconds=5)


def _submit_empty(
    observer: ObservationStore, session_id: UUID
) -> tuple[ReleaseSampleLease, ReleaseSampleReceipt]:
    lease = _claim(observer, session_id)
    return lease, observer.submit_release_sample(lease, empty_evidence(_window_end()))


def _set_authorized_at(session_id: UUID, at: datetime) -> None:
    """Backdate the authorization (as the F6 store tests do) so that windows
    entirely after it can end in the past. The product guard freezes
    ``authorized_at`` for every role (contract r3), so the test bypasses
    triggers itself as superuser, scoped to this one transaction."""
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(
            f"UPDATE {SESSIONS} SET authorized_at=%s WHERE session_id=%s",
            (at, session_id),
        )


def _submit_sufficient(
    observer: ObservationStore,
    session_id: UUID,
    window_end: datetime,
    pods: tuple[Pod, ...],
    *,
    after: Mapping[str, float] | None = None,
) -> tuple[ReleaseSampleLease, ReleaseSampleReceipt, ReleaseSampleEvidence]:
    lease = _claim(observer, session_id)
    context = observer.release_context(session_id)
    texts = planned_texts(context, window_end, [p.uid for p in pods])
    evidence = (
        EvidenceBuilder(window_end=window_end, texts=texts)
        .sufficient(pods, after=after)
        .evidence()
    )
    return lease, observer.submit_release_sample(lease, evidence), evidence


def _wait_past(deadline: datetime) -> None:
    for _ in range(300):
        if _db_now() >= deadline:
            return
        time.sleep(0.1)
    raise AssertionError("deadline did not pass")


def _expiring(owner: DurableStore, web: ReleaseStore, seconds: float = 6.0) -> Release:
    """A pending release whose frozen deadline is ``seconds`` away."""
    return _record_release(
        owner, web, released_ago=SESSION.deadline_seconds - seconds, min_tracking=300
    )


# ===========================================================================
# 1. calibration gate (固定语义 1)
# ===========================================================================


def test_the_shipped_catalog_is_exactly_the_approved_policy() -> None:
    assert_shipped_catalog_is_the_approved_policy()


def test_authorizing_an_unknown_revision_under_the_shipped_catalog_is_uncalibrated(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore
) -> None:
    release = _record_release(owner, web)
    before = reader.get_release(release.oid)
    assert before.used_sample_count == 0
    assert before.calibrated_policy_revision is None
    assert before.calibrated_policy_content is None
    assert before.calibrated_policy_sha256 is None
    audit_before = _audit_count(release.oid)
    key = _key()

    receipt = _authorize(web, release.oid, "checkout-release@0123456789ab", key=key)

    _assert_rejected(
        reader, receipt, "RELEASE_POLICY_UNCALIBRATED", before, audit_before
    )
    assert receipt.action == "authorize"
    assert receipt.session_id is None and receipt.ending_id is None
    assert _release_sessions(release.oid) == []
    (row,) = reader.release_audit(release.oid)
    assert (row["action"], row["principal_kind"], row["actor"]) == (
        "authorize",
        "system",
        "controller:test",
    )
    assert row["idempotency_key"] == key
    assert row["session_id"] is None
    # the PR-1 delivery audit is untouched by the control audit
    assert len(reader.release_events(release.oid)) == 1

    # same key, same fingerprint: the first receipt, nothing re-executed
    again = _authorize(web, release.oid, "checkout-release@0123456789ab", key=key)
    assert again.replayed is True
    assert again.model_copy(update={"replayed": False}) == receipt
    assert _audit_count(release.oid) == audit_before + 1


def test_a_calibrated_policy_authorizes_and_freezes_its_snapshot(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore, catalog: Catalog
) -> None:
    release = _record_release(owner, web)
    entry = _entry(release)
    catalog.add(entry)
    t0 = _db_now()

    receipt = _authorize(web, release.oid, entry["revision"])
    t1 = _db_now()

    assert (receipt.outcome, receipt.reason, receipt.replayed) == (
        "applied",
        None,
        False,
    )
    assert receipt.action == "authorize"
    assert (receipt.status, receipt.paused) == ("observing", False)
    assert receipt.control_generation == 0, "authorize keeps the control generation"
    assert receipt.observation_generation == 1
    assert receipt.used_sample_count == 0
    assert receipt.deadline_at == release.record.deadline_at
    record = reader.get_release(release.oid)
    assert record.status == "observing"
    assert (record.control_generation, record.observation_generation) == (0, 1)
    assert record.calibrated_policy_revision == entry["revision"]
    assert record.calibrated_policy_content == canonical_json(entry["content"])
    assert record.calibrated_policy_sha256 == entry["content_sha256"]
    assert record.calibrated_policy_sha256 == sha256_hex(
        record.calibrated_policy_content
    )
    assert record.deadline_at == release.record.deadline_at
    assert record.max_samples == release.record.max_samples

    (session,) = _release_sessions(release.oid)
    assert session["session_id"] == receipt.session_id
    assert session["state"] == "authorized"
    assert session["purpose"] == "release_observation"
    assert (session["subject_kind"], session["subject_id"]) == (
        "release_observation",
        release.oid,
    )
    assert session["incident_id"] is None
    assert session["authorization_source"] is None
    assert session["release_policy_revision"] == entry["revision"]
    assert session["release_budget_used_at_authorization"] == 0
    assert session["observation_generation"] == 1
    assert session["subject_control_generation"] == 0
    assert session["health_profile_revision"] == PROFILE_REVISION
    assert session["tracking_since"] is None and session["tracking_until"] is None
    assert t0 <= session["authorized_at"] <= t1, "authorized_at is the DB time"
    assert session["active_sample_job_id"] is not None
    assert session["active_sample_due_at"] == session["authorized_at"] + INTERVAL

    (row,) = reader.release_audit(release.oid)
    assert (row["action"], row["outcome"], row["principal_kind"]) == (
        "authorize",
        "applied",
        "system",
    )
    assert (row["status_before"], row["status_after"]) == ("pending", "observing")
    assert (
        row["observation_generation_before"],
        row["observation_generation_after"],
    ) == (
        0,
        1,
    )
    assert row["session_id"] == receipt.session_id


@pytest.mark.parametrize(
    "change",
    [
        "workload",
        "resource_uid",
        "cluster_uid",
        "profile_revision",
        "profile_sha256",
        "min_tracking",
    ],
)
def test_a_policy_that_does_not_match_the_release_is_a_mismatch(
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
    change: str,
) -> None:
    release = _record_release(owner, web)
    target = dict(release.identity)
    overrides: dict[str, Any] = {}
    tracking = release.record.min_tracking_seconds
    if change in ("workload", "resource_uid", "cluster_uid"):
        target[change] = target[change] + "-other"
    elif change == "profile_revision":
        overrides["health_profile_revision"] = "otel-demo-checkout@000000000000"
    elif change == "profile_sha256":
        overrides["health_profile_sha256"] = sha256_hex("another profile")
    else:
        tracking += 60
    entry = policy_entry(
        policy_content(target, min_tracking_seconds=tracking, **overrides)
    )
    catalog.add(entry)
    before = reader.get_release(release.oid)

    receipt = _authorize(web, release.oid, entry["revision"])

    _assert_rejected(reader, receipt, "RELEASE_POLICY_MISMATCH", before, 0)
    assert _release_sessions(release.oid) == []


def test_a_revision_absent_from_a_valid_catalog_is_uncalibrated(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore, catalog: Catalog
) -> None:
    release = _record_release(owner, web)
    other = _record_release(owner, web)
    catalog.add(_entry(other))
    before = reader.get_release(release.oid)

    receipt = _authorize(web, release.oid, _entry(release)["revision"])

    _assert_rejected(reader, receipt, "RELEASE_POLICY_UNCALIBRATED", before, 0)


def _corrupt(kind: str, release: Release) -> str:
    good = _entry(release)
    content = good["content"]
    if kind == "not-json":
        return '{"format_version":1,"policies":['
    if kind == "format-version":
        return canonical_json({"format_version": 2, "policies": [good]})
    if kind == "content-hash":
        return catalog_text(policy_entry(content, content_sha256=sha256_hex("x")))
    if kind == "hash-prefix-only":
        # revision agrees with the first 12 hex; the full hash does not
        true_sha = good["content_sha256"]
        forged = true_sha[:12] + ("0" if true_sha[12] != "0" else "1") + true_sha[13:]
        return catalog_text(
            policy_entry(content, policy_id=good["policy_id"], content_sha256=forged)
        )
    if kind == "revision-formula":
        return catalog_text(
            policy_entry(content, revision=good["policy_id"] + "@abcdefabcdef")
        )
    if kind == "duplicate-revision":
        return catalog_text(good, good)
    if kind == "evidence-path":
        bad = policy_content(
            release.identity,
            min_tracking_seconds=release.record.min_tracking_seconds,
            evidence_path="docs/../../etc/passwd",
        )
        return catalog_text(policy_entry(bad))
    if kind == "target-keys":
        bad = dict(content)
        bad["target"] = {**content["target"], "extra": "x"}
        return catalog_text(policy_entry(bad))
    if kind == "unknown-field":
        return catalog_text({**good, "allow_uncalibrated": True})
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "not-json",
        "format-version",
        "content-hash",
        "hash-prefix-only",
        "revision-formula",
        "duplicate-revision",
        "evidence-path",
        "target-keys",
        "unknown-field",
    ],
)
def test_a_missing_or_corrupt_catalog_is_catalog_invalid(
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
    kind: str,
) -> None:
    release = _record_release(owner, web)
    if kind == "missing":
        catalog.remove()
    else:
        catalog.write(_corrupt(kind, release))
    before = reader.get_release(release.oid)

    receipt = _authorize(web, release.oid, _entry(release)["revision"])

    _assert_rejected(reader, receipt, "RELEASE_POLICY_CATALOG_INVALID", before, 0)
    assert _release_sessions(release.oid) == []


@pytest.mark.parametrize(
    "field", [("evaluator", "release-after-v2"), ("mapping", "pod-name-prefix-v1")]
)
def test_an_unsupported_evaluator_or_mapping_is_refused(
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
    field: tuple[str, str],
) -> None:
    # r1 types the two fields as Literal, so such an entry cannot load: the
    # contract leaves CATALOG_INVALID vs UNSUPPORTED open (reported).
    release = _record_release(owner, web)
    entry = _entry(release, **{field[0]: field[1]})
    catalog.add(entry)
    before = reader.get_release(release.oid)

    receipt = _authorize(web, release.oid, entry["revision"])

    assert receipt.outcome == "rejected"
    assert receipt.reason in (
        "RELEASE_POLICY_CATALOG_INVALID",
        "RELEASE_POLICY_UNSUPPORTED",
    )
    assert _state(reader.get_release(release.oid)) == _state(before)
    assert _release_sessions(release.oid) == []


def test_resume_rechecks_the_catalog_while_sampling_uses_the_snapshot(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    snapshot = _state(reader.get_release(observing.oid))[-3:]

    catalog.empty()  # the policy disappears from the catalog
    _, receipt = _submit_empty(observer, observing.session_id)
    assert receipt.accepted and receipt.disposition == "adopted"
    assert _control(web, observing.oid, "pause", 0).outcome == "applied"

    paused = reader.get_release(observing.oid)
    audit_before = _audit_count(observing.oid)
    refused = _control(web, observing.oid, "resume", 1, revision=observing.revision)
    _assert_rejected(
        reader, refused, "RELEASE_POLICY_UNCALIBRATED", paused, audit_before
    )

    catalog.add(observing.entry)
    resumed = _control(web, observing.oid, "resume", 1, revision=observing.revision)
    assert resumed.outcome == "applied"
    record = reader.get_release(observing.oid)
    assert _state(record)[-3:] == snapshot, "policy snapshot never changes"
    assert _session(resumed.session_id)["release_policy_revision"] == observing.revision


def test_resume_must_name_the_snapshot_revision(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore, catalog: Catalog
) -> None:
    observing = _observing(owner, web, catalog)
    assert _control(web, observing.oid, "pause", 0).outcome == "applied"
    # another valid calibration for the same release still is not the snapshot
    other = _entry(observing.release, approval_ref="test-only: second approval")
    catalog.add(other)
    before = reader.get_release(observing.oid)

    receipt = _control(web, observing.oid, "resume", 1, revision=other["revision"])

    assert receipt.outcome == "rejected"
    assert receipt.reason in CATALOG_CODES
    assert _state(reader.get_release(observing.oid)) == _state(before)


def test_web_cannot_rewrite_the_policy_snapshot_or_frozen_fields(
    owner: DurableStore,
    web: ReleaseStore,
    web_dsn: str,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    before = reader.get_release(observing.oid)
    for assignment in (
        "calibrated_policy_revision='x@000000000000'",
        "calibrated_policy_sha256=%s",
        "calibrated_policy_content='{}'",
        "calibrated_policy_revision=NULL,calibrated_policy_content=NULL,"
        "calibrated_policy_sha256=NULL",
        "deadline_at=deadline_at+interval '1 hour'",
        "max_samples=max_samples+1",
        "used_sample_count=0",
        "min_tracking_seconds=min_tracking_seconds+1",
    ):
        params = (
            (sha256_hex("x"), observing.oid) if "%s" in assignment else (observing.oid,)
        )
        _refused(
            web_dsn,
            f"UPDATE {RELEASES} SET {assignment} WHERE observation_id=%s",
            params,
        )
    assert _state(reader.get_release(observing.oid)) == _state(before)


# ===========================================================================
# 2. command semantics (固定语义 2/3)
# ===========================================================================


@pytest.mark.parametrize(
    "call",
    [
        {"action": "authorize", "key": ""},
        {"action": "authorize", "key": "k" * 257},
        {"action": "authorize", "key": "system:deadline"},
        {"action": "authorize", "actor": ""},
        {"action": "authorize", "expected": -1},
        {"action": "pause", "revision": "checkout-release@0123456789ab"},
        {"action": "cancel", "revision": "checkout-release@0123456789ab"},
        {"action": "resume", "revision": None},
        {"action": "stop", "revision": None},
    ],
    ids=[
        "empty-key",
        "key-257",
        "system-prefix",
        "empty-actor",
        "negative-generation",
        "pause-with-revision",
        "cancel-with-revision",
        "resume-without-revision",
        "unknown-action",
    ],
)
def test_structurally_invalid_commands_raise_invalid_input_without_audit(
    owner: DurableStore, web: ReleaseStore, call: dict[str, Any]
) -> None:
    release = _record_release(owner, web)
    key = call.get("key", _key())
    with pytest.raises(PersistenceError) as raised:
        if call["action"] == "authorize":
            web.authorize_release(
                release.oid,
                actor=call.get("actor", "controller:test"),
                expected_control_generation=call.get("expected", 0),
                idempotency_key=key,
                calibrated_policy_revision="checkout-release@0123456789ab",
            )
        else:
            web.control_release(
                release.oid,
                action=call["action"],
                actor="operator:test",
                expected_control_generation=0,
                idempotency_key=key,
                calibrated_policy_revision=call["revision"],
            )
    assert raised.value.args[0] == "INVALID_INPUT"
    assert _audit_count(release.oid) == 0


def test_a_key_of_256_characters_is_accepted(
    owner: DurableStore, web: ReleaseStore
) -> None:
    release = _record_release(owner, web)
    receipt = _control(web, release.oid, "pause", 0, key="k" * 256)
    assert receipt.outcome == "applied"


def test_an_unknown_release_is_not_found_without_audit(web: ReleaseStore) -> None:
    oid = uuid4()
    for call in (
        lambda: _authorize(web, oid, "checkout-release@0123456789ab"),
        lambda: _control(web, oid, "pause", 0),
    ):
        with pytest.raises(PersistenceError) as raised:
            call()
        assert raised.value.args[0] == "NOT_FOUND"
    assert _audit_count(oid) == 0


def test_a_replayed_command_returns_its_first_receipt_after_later_changes(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore, catalog: Catalog
) -> None:
    observing = _observing(owner, web, catalog)
    first = observing.receipt
    (audit,) = reader.release_audit(observing.oid)
    first_key = audit["idempotency_key"]
    assert first.sequence == audit["sequence"]
    pause_key = _key()
    pause = _control(web, observing.oid, "pause", 0, key=pause_key)
    assert pause.outcome == "applied"
    state = _state(reader.get_release(observing.oid))
    count = _audit_count(observing.oid)

    replay_auth = _authorize(web, observing.oid, observing.revision, key=first_key)
    replay_pause = _control(web, observing.oid, "pause", 0, key=pause_key)

    assert replay_auth.replayed and replay_pause.replayed
    assert replay_auth.model_copy(update={"replayed": False}) == first
    assert replay_pause.model_copy(update={"replayed": False}) == pause
    assert _state(reader.get_release(observing.oid)) == state, "not executed again"
    assert _audit_count(observing.oid) == count
    assert len(_release_sessions(observing.oid)) == 1


@pytest.mark.parametrize("variant", ["actor", "expected", "revision", "action"])
def test_a_reused_key_with_another_fingerprint_is_an_idempotency_conflict(
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
    variant: str,
) -> None:
    observing = _observing(owner, web, catalog)
    key = _key()
    assert _control(web, observing.oid, "pause", 0, key=key).outcome == "applied"
    resume_key = _key()
    assert (
        _control(
            web, observing.oid, "resume", 1, revision=observing.revision, key=resume_key
        ).outcome
        == "applied"
    )
    state = _state(reader.get_release(observing.oid))
    audit = reader.release_audit(observing.oid)

    with pytest.raises(PersistenceError) as raised:
        if variant == "actor":
            _control(web, observing.oid, "pause", 0, key=key, actor="operator:other")
        elif variant == "expected":
            _control(web, observing.oid, "pause", 1, key=key)
        elif variant == "revision":
            other = _entry(observing.release, approval_ref="test-only: other")
            catalog.add(other)
            _control(
                web,
                observing.oid,
                "resume",
                1,
                revision=other["revision"],
                key=resume_key,
            )
        else:
            _control(web, observing.oid, "cancel", 0, key=key)
    assert raised.value.args[0] == "IDEMPOTENCY_CONFLICT"
    assert _state(reader.get_release(observing.oid)) == state
    assert reader.release_audit(observing.oid) == audit


def test_pause_resume_and_cancel_move_generations_sessions_and_audit(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore, catalog: Catalog
) -> None:
    observing = _observing(owner, web, catalog)
    oid, first_session = observing.oid, observing.session_id
    fixed = reader.get_release(oid)

    # pause: paused, both generations +1, session revoked, status kept
    pause = _control(web, oid, "pause", 0)
    assert pause.outcome == "applied"
    assert (pause.status, pause.paused) == ("observing", True)
    assert (pause.control_generation, pause.observation_generation) == (1, 2)
    revoked = _session(first_session)
    assert revoked["state"] == "revoked"
    assert revoked["active_sample_job_id"] is None
    (ending,) = _endings(oid)
    assert ending["ending_id"] == _ending_id(oid, first_session)
    assert pause.ending_id == ending["ending_id"]
    assert (ending["ended_reason"], ending["transition"]) == ("authority_revoked", None)
    assert ending["session_id"] == first_session

    # resume: new session, unpaused, both generations +1, budget start kept
    resume = _control(web, oid, "resume", 1, revision=observing.revision)
    assert resume.outcome == "applied"
    assert (resume.status, resume.paused) == ("observing", False)
    assert (resume.control_generation, resume.observation_generation) == (2, 3)
    assert resume.session_id not in (None, first_session)
    second = _session(resume.session_id)
    assert second["state"] == "authorized"
    assert second["observation_generation"] == 3
    assert second["subject_control_generation"] == 2
    assert second["release_budget_used_at_authorization"] == 0
    assert second["tracking_since"] is None and second["healthy_since"] is None
    assert second["active_sample_due_at"] == second["authorized_at"] + INTERVAL

    # cancel: terminal, DB end time, the session revoked with a cancel ending
    cancel = _control(web, oid, "cancel", 2)
    assert cancel.outcome == "applied"
    assert (cancel.status, cancel.paused) == ("cancelled", False)
    assert (cancel.control_generation, cancel.observation_generation) == (3, 4)
    record = reader.get_release(oid)
    assert record.status == "cancelled"
    assert record.ended_reason == "cancelled"
    assert record.ended_at is not None
    assert _session(resume.session_id)["state"] == "revoked"
    cancel_ending = _endings(oid)[-1]
    assert cancel_ending["ending_id"] == _ending_id(oid, resume.session_id)
    assert (cancel_ending["ended_reason"], cancel_ending["transition"]) == (
        "cancelled",
        None,
    )

    # never reset by control
    for receipt in (pause, resume, cancel):
        assert receipt.deadline_at == fixed.deadline_at
        assert receipt.used_sample_count == 0
    assert (record.deadline_at, record.max_samples) == (
        fixed.deadline_at,
        fixed.max_samples,
    )

    # audit: consecutive sequences, human controls as basic_auth
    audit = reader.release_audit(oid)
    assert [row["sequence"] for row in audit] == list(range(1, len(audit) + 1))
    assert [row["action"] for row in audit] == [
        "authorize",
        "pause",
        "resume",
        "cancel",
    ]
    assert [row["principal_kind"] for row in audit] == [
        "system",
        "basic_auth",
        "basic_auth",
        "basic_auth",
    ]
    assert [r.sequence for r in (pause, resume, cancel)] == [2, 3, 4]

    # terminal: every new control is refused (replays excepted)
    for action, revision in (
        ("pause", None),
        ("resume", observing.revision),
        ("cancel", None),
    ):
        before = reader.get_release(oid)
        audit_before = _audit_count(oid)  # the rejection itself is audited
        refused = _control(web, oid, action, 3, revision=revision)
        _assert_rejected(reader, refused, "RELEASE_TERMINAL", before, audit_before)
    refused = _authorize(web, oid, observing.revision, expected=3)
    assert (refused.outcome, refused.reason) == ("rejected", "RELEASE_TERMINAL")
    assert len(_release_sessions(oid)) == 2


def test_a_pending_release_can_be_paused_and_then_not_authorized(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore, catalog: Catalog
) -> None:
    release = _record_release(owner, web)
    entry = _entry(release)
    catalog.add(entry)

    pause = _control(web, release.oid, "pause", 0)
    assert pause.outcome == "applied"
    assert (pause.status, pause.paused) == ("pending", True)
    assert (pause.control_generation, pause.observation_generation) == (1, 1)
    assert pause.session_id is None and pause.ending_id is None
    assert _release_sessions(release.oid) == []

    before = reader.get_release(release.oid)
    refused = _authorize(web, release.oid, entry["revision"], expected=1)
    _assert_rejected(reader, refused, "RELEASE_PAUSED", before, 1)

    again = _control(web, release.oid, "pause", 1)
    _assert_rejected(reader, again, "ALREADY_PAUSED", before, 2)


def test_authorizing_twice_is_already_authorized(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore, catalog: Catalog
) -> None:
    observing = _observing(owner, web, catalog)
    before = reader.get_release(observing.oid)

    receipt = _authorize(web, observing.oid, observing.revision)

    _assert_rejected(reader, receipt, "OBSERVATION_ALREADY_AUTHORIZED", before, 1)
    assert len(_release_sessions(observing.oid)) == 1


def test_resume_is_refused_while_a_valid_session_is_active(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore, catalog: Catalog
) -> None:
    # r1 names no code for this refusal (reported); assert only its effect
    observing = _observing(owner, web, catalog)
    before = reader.get_release(observing.oid)

    receipt = _control(web, observing.oid, "resume", 0, revision=observing.revision)

    assert receipt.outcome == "rejected" and receipt.reason
    assert _state(reader.get_release(observing.oid)) == _state(before)
    assert len(_release_sessions(observing.oid)) == 1


def test_a_cancelled_pending_release_has_no_session(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore
) -> None:
    release = _record_release(owner, web)
    cancel = _control(web, release.oid, "cancel", 0)
    assert (cancel.outcome, cancel.status, cancel.paused) == (
        "applied",
        "cancelled",
        False,
    )
    assert (cancel.control_generation, cancel.observation_generation) == (1, 1)
    record = reader.get_release(release.oid)
    assert (record.status, record.ended_reason) == ("cancelled", "cancelled")
    assert record.ended_at is not None
    assert _release_sessions(release.oid) == [] and _endings(release.oid) == []


def test_rejection_order_around_the_deadline_and_terminal_state(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore
) -> None:
    release = _expiring(owner, web)
    _wait_past(release.record.deadline_at)
    oid = release.oid

    # 4 before 8: the shipped (empty) catalog is never reached
    before = reader.get_release(oid)
    assert before.status == "pending", "rejections never expire the subject"
    refused = _authorize(web, oid, "checkout-release@0123456789ab")
    _assert_rejected(reader, refused, "RELEASE_DEADLINE_EXPIRED", before, 0)
    refused = _control(web, oid, "pause", 0)
    _assert_rejected(reader, refused, "RELEASE_DEADLINE_EXPIRED", before, 1)

    # cancel is not subject to the deadline rule while not yet swept
    cancel_key = _key()
    cancel = _control(web, oid, "cancel", 0, key=cancel_key)
    assert (cancel.outcome, cancel.status) == ("applied", "cancelled")

    # 3 before 4, 2 before 3, 1 before 2
    after = reader.get_release(oid)
    refused = _control(web, oid, "pause", 1)
    _assert_rejected(reader, refused, "RELEASE_TERMINAL", after, 3)
    refused = _control(web, oid, "pause", 0)
    _assert_rejected(reader, refused, "CONTROL_CONFLICT", after, 4)
    replay = _control(web, oid, "cancel", 0, key=cancel_key)
    assert replay.replayed and replay.model_copy(update={"replayed": False}) == cancel


def test_control_conflict_on_a_live_release(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore
) -> None:
    release = _record_release(owner, web)
    before = reader.get_release(release.oid)
    refused = _authorize(web, release.oid, "checkout-release@0123456789ab", expected=5)
    _assert_rejected(reader, refused, "CONTROL_CONFLICT", before, 0)


def test_scope_suspension_order_against_pause_and_calibration(
    owner: DurableStore, web: ReleaseStore, reader: ReleaseStore
) -> None:
    release = _record_release(owner, web)
    oid = release.oid
    generation = owner.set_target_suspension(
        release.target_id, True, expected_generation=0, actor="tester"
    )
    try:
        # 6 before 8 (shipped empty catalog)
        before = reader.get_release(oid)
        refused = _authorize(web, oid, "checkout-release@0123456789ab")
        _assert_rejected(reader, refused, "SCOPE_SUSPENDED", before, 0)
        # pause is not scope-gated
        assert _control(web, oid, "pause", 0).outcome == "applied"
        paused = reader.get_release(oid)
        # 5 before 6
        refused = _authorize(web, oid, "checkout-release@0123456789ab", expected=1)
        _assert_rejected(reader, refused, "RELEASE_PAUSED", paused, 2)
        # resume is scope-gated: 6 before 8
        refused = _control(
            web, oid, "resume", 1, revision="checkout-release@0123456789ab"
        )
        _assert_rejected(reader, refused, "SCOPE_SUSPENDED", paused, 3)
    finally:
        owner.set_target_suspension(
            release.target_id, False, expected_generation=generation, actor="tester"
        )
    # unsuspended: the calibration gate is next
    refused = _control(web, oid, "resume", 1, revision="checkout-release@0123456789ab")
    _assert_rejected(reader, refused, "RELEASE_POLICY_UNCALIBRATED", paused, 4)


def test_unsuspending_does_not_resume_and_authorize_requires_resume(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    oid, old = observing.oid, observing.session_id
    target = observing.release.target_id
    g1 = owner.set_target_suspension(
        target, True, expected_generation=0, actor="tester"
    )
    g2 = owner.set_target_suspension(
        target, False, expected_generation=g1, actor="tester"
    )
    assert g2 > g1

    record = reader.get_release(oid)
    assert (record.status, record.paused) == ("observing", False)
    assert [s["session_id"] for s in _release_sessions(oid)] == [old], "no new session"
    # the old authorization never resumes: no lease, the session is ended
    _make_due(old)
    assert _claims(observer, old) == []
    assert _session(old)["state"] != "authorized"

    before = reader.get_release(oid)
    refused = _authorize(web, oid, observing.revision)
    _assert_rejected(reader, refused, "REAUTHORIZATION_REQUIRED", before, 1)

    resumed = _control(web, oid, "resume", 0, revision=observing.revision)
    assert resumed.outcome == "applied"
    assert (resumed.control_generation, resumed.observation_generation) == (1, 2)
    sessions = _release_sessions(oid)
    assert [s["state"] for s in sessions] == ["revoked", "authorized"]
    assert sessions[1]["session_id"] == resumed.session_id


# ===========================================================================
# 4. adoption, windows, fences (固定语义 4/7/8)
# ===========================================================================


def test_a_window_before_authorization_is_adopted_as_unknown_and_counted(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    oid, sid = observing.oid, observing.session_id
    session = _session(sid)

    lease = _claim(observer, sid)
    assert lease.purpose == "release_observation"
    assert (lease.subject.kind, lease.subject.id) == ("release_observation", str(oid))
    assert (lease.observation_id, lease.session_id) == (oid, sid)
    assert lease.sequence == 1
    assert lease.job_id == session["active_sample_job_id"]
    assert lease.calibrated_policy_revision == observing.revision
    assert lease.health_profile_revision == PROFILE_REVISION
    assert lease.deadline_at == observing.release.record.deadline_at
    assert lease.sample_interval_seconds == SESSION.sample_interval_seconds
    assert (lease.subject_control_generation, lease.observation_generation) == (0, 1)
    assert lease.adopted_window_end is None
    assert observer.lease_scope_current(lease) is True

    context = observer.release_context(sid)
    assert context.release.observation_id == oid
    assert context.session_id == sid
    assert context.authorized_at == session["authorized_at"]
    assert context.policy.revision == observing.revision
    assert context.health_profile == PROFILE

    window_end = _window_end()
    evidence = empty_evidence(window_end)
    receipt = observer.submit_release_sample(lease, evidence)

    assert (receipt.accepted, receipt.disposition, receipt.reason) == (
        True,
        "adopted",
        "adopted",
    )
    assert receipt.replayed is False
    assert (receipt.sample_id, receipt.session_id, receipt.observation_id) == (
        evidence.sample_id,
        sid,
        oid,
    )
    assert receipt.evaluation.verdict == "unknown"
    assert receipt.evaluation.evidence_sufficient is False
    assert "WINDOW_BEFORE_AUTHORIZATION" in receipt.evaluation.reason_codes
    assert receipt.evaluation.outcome == "no_data"
    assert (receipt.session_state, receipt.release_status) == (
        "authorized",
        "observing",
    )
    assert receipt.used_sample_count == 1
    assert (receipt.tracking_seconds, receipt.healthy_window_seconds) == (0.0, 0.0)
    assert receipt.ending_id is None

    row = _sample(evidence.sample_id)
    assert row["window_end"] <= row["decision_at"]
    assert receipt.next_sample_due_at == row["decision_at"] + INTERVAL
    assert row["purpose"] == "release_observation"
    assert (row["subject_kind"], row["subject_id"]) == ("release_observation", oid)
    assert row["current_subject_state"] == "observing"
    assert row["current_subject_control_generation"] == 0
    assert row["current_subject_observation_generation"] == 1
    assert row["subject_lifecycle"] is None
    assert row["incident_control_generation"] is None
    assert row["incident_observation_generation"] is None
    assert row["release_evidence_sha256"] is not None
    assert row["release_evaluation"] is not None and row["release_receipt"] is not None

    assert reader.get_release(oid).used_sample_count == 1
    after = _session(sid)
    assert after["adopted_count"] == 1
    assert after["tracking_since"] is None and after["tracking_until"] is None
    assert after["active_sample_sequence"] == 2
    assert after["active_sample_due_at"] == receipt.next_sample_due_at


@pytest.fixture(scope="module")
def confirmed(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    """A release confirmed healthy by three contiguous sufficient samples.

    R = released_at, A = R+500 s (backdated), W = 300 s, M = H = 600 s:
    T1 = A+300 (tracking 300, healthy 0), T2 = A+600 (600, 300),
    T3 = A+900 (900, 600): T3 >= R+M, coverage >= M, T3-healthy_since >= H.
    """
    with pytest.MonkeyPatch.context() as patch:
        catalog = Catalog(patch, tmp_path_factory.mktemp("confirmed"))
        observing = _observing(owner, web, catalog, released_ago=2000)
    released_at = observing.release.record.released_at
    authorized_at = released_at + timedelta(seconds=500)
    _set_authorized_at(observing.session_id, authorized_at)
    pods = pods_for(AFTER_IMAGES)
    steps = []
    for offset in (300, 600, 900):
        steps.append(
            _submit_sufficient(
                observer,
                observing.session_id,
                authorized_at + timedelta(seconds=offset),
                pods,
            )
        )
    return {
        "observing": observing,
        "authorized_at": authorized_at,
        "pods": pods,
        "steps": steps,
    }


def test_healthy_requires_coverage_and_a_sustained_healthy_window(
    confirmed: dict[str, Any], reader: ReleaseStore
) -> None:
    observing: Observing = confirmed["observing"]
    oid, sid = observing.oid, observing.session_id
    (l1, r1, e1), (l2, r2, e2), (l3, r3, e3) = confirmed["steps"]

    for receipt in (r1, r2, r3):
        assert receipt.accepted and receipt.disposition == "adopted"
        assert receipt.evaluation.verdict == "healthy"
        assert receipt.evaluation.evidence_sufficient is True
    assert (r1.tracking_seconds, r1.healthy_window_seconds) == (300.0, 0.0)
    assert (r2.tracking_seconds, r2.healthy_window_seconds) == (600.0, 300.0)
    assert (r1.release_status, r2.release_status) == ("observing", "observing")
    assert r1.next_sample_due_at is not None and r2.next_sample_due_at is not None

    assert (r3.tracking_seconds, r3.healthy_window_seconds) == (900.0, 600.0)
    assert (r3.release_status, r3.session_state) == ("healthy", "completed")
    assert r3.used_sample_count == 3
    assert r3.next_sample_due_at is None
    assert r3.ending_id == _ending_id(oid, sid)

    record = reader.get_release(oid)
    assert (record.status, record.used_sample_count) == ("healthy", 3)
    assert record.ended_at is not None and record.ended_reason is not None
    assert record.incident_id is None
    (ending,) = _endings(oid)
    assert ending["ending_id"] == r3.ending_id
    assert (ending["ended_reason"], ending["transition"]) == (
        "healthy_confirmed",
        "release_healthy",
    )
    assert (ending["sample_id"], ending["job_id"], ending["job_sequence"]) == (
        e3.sample_id,
        l3.job_id,
        l3.sequence,
    )
    assert (ending["subject_state_before"], ending["subject_state_after"]) == (
        "observing",
        "healthy",
    )
    assert ending["anomaly_key"] is None
    assert ending["incident_id"] is None
    session = _session(sid)
    assert session["state"] == "completed"
    assert session["active_sample_job_id"] is None
    authorized_at = confirmed["authorized_at"]
    assert session["tracking_since"] == authorized_at
    assert session["tracking_until"] == authorized_at + timedelta(seconds=900)
    assert [l1.sequence, l2.sequence, l3.sequence] == [1, 2, 3]


def test_a_gap_or_an_adopted_unknown_resets_tracking_and_health(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog, released_ago=2500)
    authorized_at = observing.release.record.released_at + timedelta(seconds=300)
    _set_authorized_at(observing.session_id, authorized_at)
    pods = pods_for(AFTER_IMAGES)
    sid = observing.session_id

    def at(seconds: int) -> datetime:
        return authorized_at + timedelta(seconds=seconds)

    _, first, _ = _submit_sufficient(observer, sid, at(300), pods)
    assert (first.tracking_seconds, first.healthy_window_seconds) == (300.0, 0.0)
    # window [A+400, A+700] starts after tracking_until A+300: a gap
    _, gap, _ = _submit_sufficient(observer, sid, at(700), pods)
    assert gap.evaluation.verdict == "healthy"
    assert (gap.tracking_seconds, gap.healthy_window_seconds) == (300.0, 0.0)
    assert _session(sid)["tracking_since"] == at(400)
    # an adopted unknown clears tracking
    lease = _claim(observer, sid)
    unknown = observer.submit_release_sample(lease, empty_evidence(at(1000)))
    assert unknown.disposition == "adopted" and unknown.evaluation.verdict == "unknown"
    assert (unknown.tracking_seconds, unknown.healthy_window_seconds) == (0.0, 0.0)
    session = _session(sid)
    assert session["tracking_since"] is None and session["tracking_until"] is None
    # tracking restarts at the next sufficient window
    _, restart, _ = _submit_sufficient(observer, sid, at(1300), pods)
    assert (restart.tracking_seconds, restart.healthy_window_seconds) == (300.0, 0.0)
    assert restart.release_status == "observing"


def test_one_sufficient_degraded_sample_is_anomalous_with_a_unique_ending(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog, released_ago=1000)
    oid, sid = observing.oid, observing.session_id
    authorized_at = observing.release.record.released_at + timedelta(seconds=100)
    _set_authorized_at(sid, authorized_at)
    pods = pods_for(AFTER_IMAGES)

    lease, receipt, evidence = _submit_sufficient(
        observer, sid, authorized_at + WINDOW, pods, after={"error_ratio": 0.5}
    )

    # no wait for M or H
    assert receipt.evaluation.verdict == "degraded"
    assert receipt.evaluation.outcome == "degraded"
    assert (receipt.release_status, receipt.session_state) == ("anomalous", "completed")
    assert receipt.ending_id == _ending_id(oid, sid)
    assert receipt.next_sample_due_at is None
    record = reader.get_release(oid)
    assert (record.status, record.incident_id, record.used_sample_count) == (
        "anomalous",
        None,
        1,
    )
    (ending,) = _endings(oid)
    assert (ending["ended_reason"], ending["transition"]) == (
        "anomaly_detected",
        "release_anomalous",
    )
    assert ending["anomaly_key"] == f"release-anomaly:{oid}"
    assert (ending["sample_id"], ending["job_id"], ending["job_sequence"]) == (
        evidence.sample_id,
        lease.job_id,
        lease.sequence,
    )
    assert ending["incident_id"] is None

    first = reader.pending_anomaly_endings(limit=10_000)
    second = reader.pending_anomaly_endings(limit=10_000)
    assert first == second, "re-reading returns the same facts"
    mine = [e for e in first if e.ending_id == ending["ending_id"]]
    assert len(mine) == 1
    assert mine[0].observation_id == oid
    assert _claims(observer, sid) == []


def test_the_sample_budget_accumulates_across_sessions_until_exhausted(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog, released_ago=600)
    oid, first_session = observing.oid, observing.session_id
    max_samples = observing.release.record.max_samples
    first_count = 15
    for n in range(1, first_count + 1):
        _, receipt = _submit_empty(observer, first_session)
        assert receipt.disposition == "adopted" and receipt.used_sample_count == n
    assert _control(web, oid, "pause", 0).outcome == "applied"
    resumed = _control(web, oid, "resume", 1, revision=observing.revision)
    assert resumed.used_sample_count == first_count
    second_session = resumed.session_id
    assert (
        _session(second_session)["release_budget_used_at_authorization"] == first_count
    )

    receipts = []
    for _ in range(max_samples - first_count):
        receipts.append(_submit_empty(observer, second_session))
    lease, last = receipts[-1]
    for n, (_, receipt) in enumerate(receipts[:-1], start=first_count + 1):
        assert receipt.used_sample_count == n
        assert receipt.release_status == "observing"

    assert last.disposition == "adopted"
    assert last.used_sample_count == max_samples
    assert (last.release_status, last.session_state) == ("unknown", "expired")
    assert last.next_sample_due_at is None
    assert last.ending_id == _ending_id(oid, second_session)
    record = reader.get_release(oid)
    assert (record.status, record.ended_reason) == ("unknown", "max_samples_exhausted")
    assert record.used_sample_count == max_samples
    ending = _endings(oid)[-1]
    assert (ending["ended_reason"], ending["transition"]) == (
        "max_samples_exhausted",
        "release_unknown",
    )
    assert (ending["job_id"], ending["job_sequence"]) == (lease.job_id, lease.sequence)
    assert _session(first_session)["adopted_count"] == first_count
    assert _session(second_session)["adopted_count"] == max_samples - first_count
    assert _session(second_session)["active_sample_job_id"] is None
    refused = _control(web, oid, "pause", 2)
    assert (refused.outcome, refused.reason) == ("rejected", "RELEASE_TERMINAL")


@pytest.mark.parametrize("change", ["window", "queries"])
def test_a_sample_id_is_idempotent_and_conflicts_on_other_content(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
    change: str,
) -> None:
    observing = _observing(owner, web, catalog)
    lease = _claim(observer, observing.session_id)
    window_end = _window_end()
    evidence = empty_evidence(window_end)
    first = observer.submit_release_sample(lease, evidence)
    assert first.disposition == "adopted"

    again = observer.submit_release_sample(lease, evidence)
    assert again.replayed is True
    assert again.model_copy(update={"replayed": False}) == first
    assert reader.get_release(observing.oid).used_sample_count == 1

    if change == "window":
        other = empty_evidence(window_end - timedelta(seconds=1), evidence.sample_id)
    else:
        builder = EvidenceBuilder(window_end=window_end)
        builder.results["context:error_ratio:query"] = builder.scalar(
            "context:error_ratio:query", 0.0
        )
        other = builder.evidence(evidence.sample_id)
    with pytest.raises(PersistenceError) as raised:
        observer.submit_release_sample(lease, other)
    assert raised.value.args[0] == "SAMPLE_IDENTITY_CONFLICT"
    assert reader.get_release(observing.oid).used_sample_count == 1


def test_a_new_sample_id_for_an_adopted_sequence_is_history_only(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    lease, first = _submit_empty(observer, observing.session_id)
    assert first.disposition == "adopted"

    late = observer.submit_release_sample(lease, empty_evidence(_window_end()))

    assert (late.accepted, late.disposition) == (False, "history_only")
    assert late.used_sample_count == 1
    assert reader.get_release(observing.oid).used_sample_count == 1
    assert _sample(late.sample_id)["disposition"] == "history_only"


def test_a_reclaimed_lease_keeps_job_and_sequence_and_fences_the_old_one(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    sid = observing.session_id
    old = _claim(observer, sid)
    _make_due(sid, expire_lease=True)
    (new,) = _claims(observer, sid)
    assert (new.job_id, new.sequence) == (old.job_id, old.sequence)
    assert new.epoch == old.epoch + 1

    stale = observer.submit_release_sample(old, empty_evidence(_window_end()))
    assert (stale.accepted, stale.disposition, stale.reason) == (
        False,
        "history_only",
        "lease_revoked",
    )
    assert reader.get_release(observing.oid).used_sample_count == 0

    fresh = observer.submit_release_sample(new, empty_evidence(_window_end()))
    assert fresh.disposition == "adopted" and fresh.used_sample_count == 1


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_a_sample_after_a_control_is_history_only(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
    action: str,
) -> None:
    observing = _observing(owner, web, catalog)
    lease = _claim(observer, observing.session_id)
    assert _control(web, observing.oid, action, 0).outcome == "applied"
    assert observer.lease_scope_current(lease) is False
    state = _state(reader.get_release(observing.oid))

    receipt = observer.submit_release_sample(lease, empty_evidence(_window_end()))

    assert (receipt.accepted, receipt.disposition) == (False, "history_only")
    assert receipt.reason in FENCE_REASONS
    assert receipt.next_sample_due_at is None
    assert receipt.ending_id is None or receipt.ending_id == _ending_id(
        observing.oid, observing.session_id
    )
    assert _state(reader.get_release(observing.oid)) == state
    assert _session(observing.session_id)["state"] == "revoked"


def test_a_sample_after_a_target_suspension_is_history_only_and_stops_the_job(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    target = observing.release.target_id
    lease = _claim(observer, observing.session_id)
    assert observer.lease_scope_current(lease) is True
    generation = owner.set_target_suspension(
        target, True, expected_generation=0, actor="t"
    )
    try:
        assert observer.lease_scope_current(lease) is False
        receipt = observer.submit_release_sample(lease, empty_evidence(_window_end()))
    finally:
        owner.set_target_suspension(
            target, False, expected_generation=generation, actor="t"
        )

    assert (receipt.accepted, receipt.disposition, receipt.reason) == (
        False,
        "history_only",
        "suspended",
    )
    assert receipt.next_sample_due_at is None
    record = reader.get_release(observing.oid)
    assert (record.status, record.used_sample_count, record.paused) == (
        "observing",
        0,
        False,
    )
    session = _session(observing.session_id)
    assert session["state"] != "authorized"
    assert session["active_sample_job_id"] is None


def test_adoption_at_or_after_the_deadline_is_history_only(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    release = _expiring(owner, web, seconds=8.0)
    entry = _entry(release)
    catalog.add(entry)
    receipt = _authorize(web, release.oid, entry["revision"])
    assert receipt.outcome == "applied"
    lease = _claim(observer, receipt.session_id)
    _wait_past(release.record.deadline_at)

    late = observer.submit_release_sample(
        lease, empty_evidence(release.record.deadline_at - timedelta(seconds=1))
    )

    assert (late.accepted, late.disposition, late.reason) == (
        False,
        "history_only",
        "deadline_expired",
    )
    assert reader.get_release(release.oid).used_sample_count == 0


def test_a_window_ending_after_the_decision_time_is_not_adopted(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    # T <= decision_at; no future-window tolerance on the release path
    observing = _observing(owner, web, catalog)
    lease = _claim(observer, observing.session_id)
    future = _db_now() + timedelta(seconds=20)

    try:
        receipt = observer.submit_release_sample(lease, empty_evidence(future))
    except PersistenceError:
        receipt = None
    if receipt is not None:
        assert receipt.disposition == "history_only"
    assert reader.get_release(observing.oid).used_sample_count == 0


# ===========================================================================
# 5. sweep (固定语义 9)
# ===========================================================================


def _sweep_all(observer: ObservationStore) -> list[UUID]:
    swept: list[UUID] = []
    for _ in range(200):
        batch = observer.sweep_expired_releases(limit=100)
        if not batch:
            return swept
        swept.extend(batch)
    raise AssertionError("sweep did not converge")


def test_the_sweep_ends_every_expired_active_release_once(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    pending = _expiring(owner, web, seconds=8.0)
    paused = _expiring(owner, web, seconds=8.0)
    observing = _expiring(owner, web, seconds=8.0)
    assert _control(web, paused.oid, "pause", 0).outcome == "applied"
    entry = _entry(observing)
    catalog.add(entry)
    authorized = _authorize(web, observing.oid, entry["revision"])
    assert authorized.outcome == "applied"
    cancelled = _expiring(owner, web, seconds=8.0)
    assert _control(web, cancelled.oid, "cancel", 0).outcome == "applied"
    _wait_past(
        max(r.record.deadline_at for r in (pending, paused, observing, cancelled))
    )

    swept = _sweep_all(observer)

    for release in (pending, paused, observing):
        assert swept.count(release.oid) == 1
        record = reader.get_release(release.oid)
        assert (record.status, record.ended_reason) == ("unknown", "deadline_expired")
        assert record.ended_at is not None
        row = reader.release_audit(release.oid)[-1]
        assert (row["action"], row["outcome"]) == ("deadline_expired", "applied")
        assert row["idempotency_key"] == "system:deadline"
        assert (row["actor"], row["principal_kind"]) == ("observer", "observer")
    assert cancelled.oid not in swept, "terminal subjects are not touched"
    assert reader.get_release(cancelled.oid).status == "cancelled"

    # no session is invented for subjects that had none
    assert _release_sessions(pending.oid) == [] and _endings(pending.oid) == []
    assert _release_sessions(paused.oid) == []
    # the authorized session gets its expiry ending
    session = _session(authorized.session_id)
    assert session["state"] == "expired"
    assert session["active_sample_job_id"] is None
    (ending,) = _endings(observing.oid)
    assert ending["ending_id"] == _ending_id(observing.oid, authorized.session_id)
    assert (ending["ended_reason"], ending["transition"]) == (
        "deadline_expired",
        "release_unknown",
    )

    states = {
        r.oid: _state(reader.get_release(r.oid)) for r in (pending, paused, observing)
    }
    assert observer.sweep_expired_releases(limit=100) == []
    assert {
        r.oid: _state(reader.get_release(r.oid)) for r in (pending, paused, observing)
    } == states
    refused = _control(web, pending.oid, "cancel", 0)
    assert (refused.outcome, refused.reason) == ("rejected", "RELEASE_TERMINAL")


# ===========================================================================
# 7. guards and grants under the runtime login roles (固定语义 11)
# ===========================================================================


def test_release_grants_match_the_contract_table() -> None:
    with psycopg.connect(DSN) as conn:
        web = catalog_grants(conn, WEB_ROLE)
        worker = catalog_grants(conn, WORKER_ROLE)
        observer = catalog_grants(conn, OBSERVER_ROLE)
    # Observer on the release main table: exactly this
    assert observer[RELEASES] == frozenset(
        {"SELECT", "UPDATE(ended_at,ended_reason,status,updated_at,used_sample_count)"}
    )
    # web: original SELECT/INSERT plus exactly these UPDATE columns
    assert "SELECT" in web[RELEASES]
    update = sorted(
        "status,paused,control_generation,observation_generation,ended_reason,"
        "ended_at,updated_at,calibrated_policy_revision,calibrated_policy_content,"
        "calibrated_policy_sha256,incident_id".split(
            ","
        )  # incident_id: 0021, F11 PR-4b
    )
    assert f"UPDATE({','.join(update)})" in web[RELEASES]
    assert not {"DELETE", "TRUNCATE", "UPDATE"} & web[RELEASES]
    assert web[QUERY_RESULTS] == frozenset({"SELECT"})
    assert "SELECT" in web[AUDIT] and "SELECT" in observer[AUDIT]
    assert "SELECT" in observer[QUERY_RESULTS]
    for table in (RELEASES, AUDIT, QUERY_RESULTS):
        assert worker[table] == frozenset(), table
        assert RUNTIME_GRANTS[WORKER_ROLE][table] == frozenset(), table
        assert RUNTIME_GRANTS[WEB_ROLE][table] == web[table], table
    for table in (SESSIONS, SAMPLES, ENDINGS):
        assert RUNTIME_GRANTS[WEB_ROLE][table] == web[table], table
        assert RUNTIME_GRANTS[WORKER_ROLE][table] == worker[table], table
    for role, grants in ((WEB_ROLE, web), (OBSERVER_ROLE, observer)):
        for table in (AUDIT, QUERY_RESULTS):
            assert not {"UPDATE", "DELETE", "TRUNCATE"} & grants[table], (role, table)
            assert not any(p.startswith("UPDATE(") for p in grants[table]), (
                role,
                table,
            )


def _has_column(role: str, table: str, column: str, privilege: str) -> bool:
    return bool(
        _row(
            "SELECT has_column_privilege(%s,%s,%s,%s) AS ok",
            (role, table, column, privilege),
        )["ok"]
    )


def test_session_and_evidence_column_grants() -> None:
    for column in (
        "release_observation_id",
        "release_policy_revision",
        "release_budget_used_at_authorization",
    ):
        assert _has_column(WEB_ROLE, SESSIONS, column, "INSERT"), column
    for column in ("tracking_since", "tracking_until"):
        assert _has_column(OBSERVER_ROLE, SESSIONS, column, "UPDATE"), column
    assert not _row(
        "SELECT has_table_privilege(%s,%s,'INSERT') AS ok", (OBSERVER_ROLE, SESSIONS)
    )["ok"]
    for column in (
        "sample_id",
        "query_key",
        "query",
        "source",
        "evaluated_at",
        "sent_at",
        "status",
        "http_status",
        "detail",
        "body_complete",
        "raw",
        "raw_sha256",
    ):
        assert _has_column(OBSERVER_ROLE, QUERY_RESULTS, column, "INSERT"), column
        assert not _has_column(WEB_ROLE, QUERY_RESULTS, column, "INSERT"), column
    for role in (WEB_ROLE, OBSERVER_ROLE):
        for column in (
            "observation_id",
            "action",
            "actor",
            "outcome",
            "idempotency_key",
        ):
            assert _has_column(role, AUDIT, column, "INSERT"), (role, column)
    for column in ("release_evidence_sha256", "release_evaluation", "release_receipt"):
        assert _has_column(OBSERVER_ROLE, SAMPLES, column, "INSERT"), column
    for column in ("release_observation_id", "anomaly_key"):
        assert _has_column(WEB_ROLE, ENDINGS, column, "INSERT"), column
        assert _has_column(OBSERVER_ROLE, ENDINGS, column, "INSERT"), column


@pytest.mark.parametrize(
    "assignment",
    [
        "paused=true",
        "control_generation=control_generation+1",
        "observation_generation=observation_generation+1",
        "incident_id=NULL",
        "calibrated_policy_revision=calibrated_policy_revision",
        "status='cancelled'",
        "status='pending'",
        # terminal fields kept consistent (CHECK release_terminal_fields) so the
        # deferred evidence guard, not the CHECK, is what refuses the write
        "status='healthy',ended_reason='healthy_confirmed',ended_at=clock_timestamp()",
        "status='anomalous',ended_reason='anomaly_detected',ended_at=clock_timestamp()",
        "status='unknown',ended_reason='deadline_expired',ended_at=clock_timestamp()",
        "status='unknown',ended_reason='max_samples_exhausted',ended_at=clock_timestamp()",
    ],
)
def test_the_observer_cannot_write_controls_or_bare_transitions(
    owner: DurableStore,
    web: ReleaseStore,
    observer_dsn: str,
    reader: ReleaseStore,
    catalog: Catalog,
    assignment: str,
) -> None:
    observing = _observing(owner, web, catalog)
    before = reader.get_release(observing.oid)
    _refused(
        observer_dsn,
        f"UPDATE {RELEASES} SET {assignment},updated_at=clock_timestamp() "
        "WHERE observation_id=%s",
        (observing.oid,),
    )
    assert _state(reader.get_release(observing.oid)) == _state(before)


def test_the_observer_cannot_insert_sessions_or_control_audit(
    owner: DurableStore,
    web: ReleaseStore,
    observer_dsn: str,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    record = reader.get_release(observing.oid)
    _refused(
        observer_dsn,
        f"INSERT INTO {SESSIONS} (session_id,release_observation_id) VALUES (%s,%s)",
        (uuid4(), observing.oid),
    )
    for action, principal in (
        ("authorize", "system"),
        ("cancel", "basic_auth"),
        ("pause", "observer"),
    ):
        _refused(
            observer_dsn,
            f"INSERT INTO {AUDIT} (audit_id,observation_id,sequence,idempotency_key,"
            "request_sha256,action,principal_kind,actor,outcome,reason,"
            "expected_control_generation,control_generation_before,control_generation_after,"
            "observation_generation_before,observation_generation_after,status_before,"
            "status_after,paused_before,paused_after,session_id,ending_id,result) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'rejected','FORGED',0,0,0,1,1,'observing',"
            "'observing',false,false,NULL,NULL,'{}'::jsonb)",
            (
                uuid4(),
                observing.oid,
                100 + len(action),
                _key(),
                sha256_hex(action),
                action,
                principal,
                "observer",
            ),
            errors=(
                psycopg.errors.InsufficientPrivilege,
                psycopg.errors.IntegrityError,
            ),
        )
    assert _state(reader.get_release(observing.oid)) == _state(record)


@pytest.mark.parametrize(
    "role", [OBSERVER_ROLE, WORKER_ROLE], ids=["observer", "worker"]
)
def test_only_the_web_role_runs_authorization_and_control(
    owner: DurableStore,
    reader: ReleaseStore,
    observer_dsn: str,
    worker_dsn: str,
    web: ReleaseStore,
    catalog: Catalog,
    role: str,
) -> None:
    store = ReleaseStore(
        observer_dsn if role == OBSERVER_ROLE else worker_dsn, pool=POOL
    )
    observing = _observing(owner, web, catalog)
    release = _record_release(owner, web)
    catalog.add(_entry(release))
    states = {o: _state(reader.get_release(o)) for o in (observing.oid, release.oid)}
    audits = {o: _audit_count(o) for o in states}

    for call in (
        lambda: _authorize(store, release.oid, _entry(release)["revision"]),
        lambda: _control(store, observing.oid, "cancel", 0),
        lambda: _control(store, observing.oid, "pause", 0),
    ):
        with pytest.raises(PersistenceError) as raised:
            call()
        assert raised.value.args[0] == "INTEGRITY_REFUSED"
    assert {o: _state(reader.get_release(o)) for o in states} == states
    assert {o: _audit_count(o) for o in states} == audits


@pytest.mark.parametrize(
    "assignment",
    [
        "status='healthy',ended_reason='healthy_confirmed',ended_at=clock_timestamp()",
        "status='anomalous',ended_reason='anomaly_detected',ended_at=clock_timestamp()",
        "status='unknown',ended_reason='deadline_expired',ended_at=clock_timestamp()",
        "paused=true,control_generation=control_generation+1,"
        "observation_generation=observation_generation+1",
        "status='cancelled',ended_reason='cancelled',ended_at=clock_timestamp(),"
        "control_generation=control_generation+1,observation_generation=observation_generation+1",
        "used_sample_count=used_sample_count+1",
    ],
)
def test_the_web_role_cannot_write_verdicts_or_unaudited_controls(
    owner: DurableStore,
    web: ReleaseStore,
    web_dsn: str,
    reader: ReleaseStore,
    catalog: Catalog,
    assignment: str,
) -> None:
    observing = _observing(owner, web, catalog)
    before = reader.get_release(observing.oid)
    _refused(
        web_dsn,
        f"UPDATE {RELEASES} SET {assignment},updated_at=clock_timestamp() "
        "WHERE observation_id=%s",
        (observing.oid,),
    )
    assert _state(reader.get_release(observing.oid)) == _state(before)


def test_the_web_role_cannot_start_observing_without_authorization(
    owner: DurableStore, web: ReleaseStore, web_dsn: str, reader: ReleaseStore
) -> None:
    release = _record_release(owner, web)
    before = reader.get_release(release.oid)
    _refused(
        web_dsn,
        f"UPDATE {RELEASES} SET status='observing',"
        "observation_generation=observation_generation+1,updated_at=clock_timestamp() "
        "WHERE observation_id=%s",
        (release.oid,),
    )
    assert _state(reader.get_release(release.oid)) == _state(before)


def test_the_web_role_cannot_submit_release_samples(
    owner: DurableStore,
    web: ReleaseStore,
    web_dsn: str,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    lease = _claim(observer, observing.session_id)
    with pytest.raises(PersistenceError) as raised:
        ObservationStore(web_dsn, pool=POOL).submit_release_sample(
            lease, empty_evidence(_window_end())
        )
    assert raised.value.args[0] == "INTEGRITY_REFUSED"
    assert reader.get_release(observing.oid).used_sample_count == 0


def test_the_worker_role_has_no_access_to_release_tables(
    owner: DurableStore, web: ReleaseStore, worker_dsn: str
) -> None:
    release = _record_release(owner, web)
    for statement in (
        f"SELECT count(*) FROM {RELEASES}",
        f"SELECT count(*) FROM {AUDIT}",
        f"SELECT count(*) FROM {QUERY_RESULTS}",
    ):
        _refused(worker_dsn, statement)
    with pytest.raises(PersistenceError):
        ReleaseStore(worker_dsn, pool=POOL).release_audit(release.oid)


def test_audit_and_query_results_are_append_only(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    lease = _claim(observer, observing.session_id)
    builder = EvidenceBuilder(window_end=_window_end())
    builder.results["context:error_ratio:query"] = builder.scalar(
        "context:error_ratio:query", 0.0
    )
    evidence = builder.evidence()
    observer.submit_release_sample(lease, evidence)
    stored = _rows(
        f"SELECT * FROM {QUERY_RESULTS} WHERE sample_id=%s", (evidence.sample_id,)
    )
    assert len(stored) == 1, "raw query results are stored with every sample"
    for statement, params in (
        (
            f"UPDATE {AUDIT} SET reason='forged' WHERE observation_id=%s",
            (observing.oid,),
        ),
        (f"DELETE FROM {AUDIT} WHERE observation_id=%s", (observing.oid,)),
        (
            f"UPDATE {QUERY_RESULTS} SET raw='\\x00' WHERE sample_id=%s",
            (evidence.sample_id,),
        ),
        (f"DELETE FROM {QUERY_RESULTS} WHERE sample_id=%s", (evidence.sample_id,)),
        (f"TRUNCATE {AUDIT}", ()),
    ):
        # even the owner: the tables are append-only, not merely ungranted
        _refused(DSN, statement, params, errors=psycopg.Error, commit=False)
    assert _audit_count(observing.oid) == 1
    assert (
        _rows(
            f"SELECT * FROM {QUERY_RESULTS} WHERE sample_id=%s", (evidence.sample_id,)
        )
        == stored
    )


# ===========================================================================
# 6. migration 0015: purpose-specific constraints (固定语义 10)
# ===========================================================================


def _incident(owner: DurableStore) -> tuple[UUID, UUID]:
    incident, run = uuid4(), uuid4()
    target_id, _ = _target(owner)
    owner.accept(
        incident,
        run,
        f"f11-pr2-{incident}",
        deadline=_db_now() + timedelta(hours=1),
        budget_limit=10,
        versions={"v": "1"},
        target_id=target_id,
    )
    return incident, target_id


def _incident_session(
    owner: DurableStore, store: ObservationStore
) -> tuple[UUID, UUID]:
    incident, _ = _incident(owner)
    session = store.authorize_session(
        incident,
        revision="r1",
        actor="tester",
        deadline_at=_db_now() + timedelta(hours=1),
        max_samples=10,
        sample_interval_seconds=15,
        sustained_window_seconds=60,
        first_sample_due_at=_db_now() - timedelta(minutes=1),
    )
    return incident, session


def test_release_and_incident_rows_obey_purpose_specific_constraints(
    owner: DurableStore, web: ReleaseStore, history: ObservationStore, catalog: Catalog
) -> None:
    observing = _observing(owner, web, catalog)
    incident, incident_session = _incident_session(owner, history)
    sid = observing.session_id
    for statement, params in (
        (f"UPDATE {SESSIONS} SET incident_id=%s WHERE session_id=%s", (incident, sid)),
        (
            f"UPDATE {SESSIONS} SET release_observation_id=NULL WHERE session_id=%s",
            (sid,),
        ),
        (
            f"UPDATE {SESSIONS} SET authorization_source='post_remediation' WHERE session_id=%s",
            (sid,),
        ),
        (f"UPDATE {SESSIONS} SET subject_kind='incident' WHERE session_id=%s", (sid,)),
        (f"UPDATE {SESSIONS} SET subject_id=%s WHERE session_id=%s", (uuid4(), sid)),
        (
            f"UPDATE {SESSIONS} SET release_policy_revision=NULL WHERE session_id=%s",
            (sid,),
        ),
        (
            f"UPDATE {SESSIONS} SET release_budget_used_at_authorization=NULL WHERE session_id=%s",
            (sid,),
        ),
        (
            f"UPDATE {SESSIONS} SET tracking_since=clock_timestamp(),"
            "tracking_until=clock_timestamp()-interval '1 hour' WHERE session_id=%s",
            (sid,),
        ),
        (
            f"UPDATE {SESSIONS} SET tracking_since=clock_timestamp() WHERE session_id=%s",
            (sid,),
        ),
        (
            f"UPDATE {SESSIONS} SET release_observation_id=%s WHERE session_id=%s",
            (observing.oid, incident_session),
        ),
        (
            f"UPDATE {SESSIONS} SET release_policy_revision='x@000000000000' WHERE session_id=%s",
            (incident_session,),
        ),
        (
            f"UPDATE {SESSIONS} SET subject_id=%s WHERE session_id=%s",
            (uuid4(), incident_session),
        ),
        (
            f"UPDATE {SESSIONS} SET authorization_source=NULL WHERE session_id=%s",
            (incident_session,),
        ),
        (
            f"UPDATE {RELEASES} SET calibrated_policy_sha256=NULL WHERE observation_id=%s",
            (observing.oid,),
        ),
        (
            f"UPDATE {RELEASES} SET used_sample_count=max_samples+1 WHERE observation_id=%s",
            (observing.oid,),
        ),
    ):
        _refused(DSN, statement, params, errors=psycopg.Error)
    session = _session(sid)
    assert (session["incident_id"], session["release_observation_id"]) == (
        None,
        observing.oid,
    )
    assert session["subject_kind"] == "release_observation"
    legacy = _session(incident_session)
    assert (legacy["purpose"], legacy["subject_kind"], legacy["subject_id"]) == (
        "incident_recovery",
        "incident",
        incident,
    )
    assert legacy["release_observation_id"] is None
    assert legacy["authorization_source"] == "post_remediation"
    assert legacy["release_policy_revision"] is None
    assert legacy["release_budget_used_at_authorization"] is None


# ===========================================================================
# 8. history and offline replay (公开接口 4, 固定语义 12)
# ===========================================================================


@pytest.fixture(scope="module")
def exhausted(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    tmp_path_factory: pytest.TempPathFactory,
) -> Observing:
    """A release that adopted unknown samples over two sessions (pause and
    resume in between) until ``max_samples`` ended it as unknown. Needs no
    plan-conforming text: every sample is an empty, legal unknown."""
    with pytest.MonkeyPatch.context() as patch:
        catalog = Catalog(patch, tmp_path_factory.mktemp("exhausted"))
        observing = _observing(owner, web, catalog, released_ago=600)
        for _ in range(5):
            _submit_empty(observer, observing.session_id)
        assert _control(web, observing.oid, "pause", 0).outcome == "applied"
        resumed = _control(web, observing.oid, "resume", 1, revision=observing.revision)
        assert resumed.outcome == "applied" and resumed.session_id is not None
    for _ in range(observing.release.record.max_samples - 5):
        _, receipt = _submit_empty(observer, resumed.session_id)
    assert receipt.release_status == "unknown"
    return observing


def _thaw(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_thaw(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_thaw(v) for v in value)
    return copy.deepcopy(value)


def _get(item: Any, name: str) -> Any:
    return getattr(item, name) if isinstance(item, BaseModel) else item[name]


def _set_release(history: dict[str, Any], **changes: Any) -> None:
    release = history["release"]
    if isinstance(release, BaseModel):
        history["release"] = release.model_copy(update=changes)
    else:
        release.update(changes)


def test_release_history_has_the_fixed_shape_and_order(
    exhausted: Observing, history: ObservationStore
) -> None:
    stored = history.release_history(exhausted.oid)
    assert set(stored) == HISTORY_KEYS
    assert stored["format_version"] == 1
    assert _get(stored["release"], "observation_id") == exhausted.oid
    profile = stored["health_profile"]
    assert _get(profile, "revision") == PROFILE_REVISION
    assert _get(profile, "content") == PROFILE_CONTENT
    assert _get(profile, "content_sha256") == PROFILE_SHA256
    sessions = list(stored["sessions"])
    assert len(sessions) == 2
    assert [
        (_get(s, "observation_generation"), _get(s, "session_id")) for s in sessions
    ] == sorted(
        (_get(s, "observation_generation"), _get(s, "session_id")) for s in sessions
    )
    samples = list(stored["samples"])
    assert len(samples) == exhausted.release.record.max_samples
    keys = [(_get(s, "decision_at"), _get(s, "sample_id")) for s in samples]
    assert keys == sorted(keys)
    endings = list(stored["endings"])
    assert [_get(e, "ended_reason") for e in endings] == [
        "authority_revoked",
        "max_samples_exhausted",
    ]
    audit = list(stored["audit"])
    assert [_get(a, "action") for a in audit] == ["authorize", "pause", "resume"]
    assert [_get(a, "sequence") for a in audit] == [1, 2, 3]

    selected = history.session_history(_get(sessions[0], "session_id"))
    assert selected["selected_session_id"] == _get(sessions[0], "session_id")
    assert HISTORY_KEYS <= set(selected)


def test_replay_of_a_stored_history_is_consistent_and_offline(
    exhausted: Observing, history: ObservationStore
) -> None:
    result = replay_release_history(history.release_history(exhausted.oid))

    assert isinstance(result, ReleaseReplay)
    assert result.observation_id == exhausted.oid
    assert result.consistent is True, result.integrity
    assert result.integrity == ()
    assert (result.stored_status, result.replayed_status) == ("unknown", "unknown")
    assert result.verdict == "unknown"
    assert result.used_sample_count == exhausted.release.record.max_samples
    assert result.external_queries == () and result.model_requests == ()


def _tamper(stored: Mapping[str, Any], code: str) -> dict[str, Any]:
    tampered = _thaw(stored)
    samples = tampered["samples"]
    if code == "PROFILE_MISMATCH":
        profile = tampered["health_profile"]
        content = json.loads(_get(profile, "content"))
        content["signals"][2]["healthy"]["max"] = 0.5
        profile["content"] = canonical_json(content)
    elif code == "POLICY_MISMATCH":
        content = json.loads(_get(tampered["release"], "calibrated_policy_content"))
        content["min_tracking_seconds"] += 1
        _set_release(tampered, calibrated_policy_content=canonical_json(content))
    elif code == "SUBJECT_BINDING_MISMATCH":
        samples[0]["subject_id"] = uuid4()
    elif code == "SAMPLE_DECISION_MISMATCH":
        samples[0]["outcome"] = "healthy"
    elif code == "WATERMARK_MISMATCH":
        last = tampered["sessions"][-1]
        last["adopted_window_end"] = _get(last, "adopted_window_end") + timedelta(
            minutes=1
        )
    elif code == "BUDGET_MISMATCH":
        _set_release(
            tampered,
            used_sample_count=_get(tampered["release"], "used_sample_count") - 1,
        )
    elif code == "ENDING_MISMATCH":
        tampered["endings"][-1]["ended_reason"] = "deadline_expired"
    elif code == "STATUS_MISMATCH":
        _set_release(tampered, status="healthy")
    else:
        raise AssertionError(code)
    return tampered


@pytest.mark.parametrize(
    "code",
    [
        "PROFILE_MISMATCH",
        "POLICY_MISMATCH",
        "SUBJECT_BINDING_MISMATCH",
        "SAMPLE_DECISION_MISMATCH",
        "WATERMARK_MISMATCH",
        "BUDGET_MISMATCH",
        "ENDING_MISMATCH",
        "STATUS_MISMATCH",
    ],
)
def test_replay_reports_each_integrity_failure(
    exhausted: Observing, history: ObservationStore, code: str
) -> None:
    result = replay_release_history(
        _tamper(history.release_history(exhausted.oid), code)
    )

    assert code in result.integrity, result.integrity
    assert result.consistent is False
    assert result.verdict == "unknown"
    assert result.external_queries == () and result.model_requests == ()


def test_replay_treats_a_missing_release_field_as_an_integrity_failure(
    exhausted: Observing, history: ObservationStore
) -> None:
    tampered = _thaw(history.release_history(exhausted.oid))
    for session in tampered["sessions"]:
        session.pop("release_budget_used_at_authorization")

    result = replay_release_history(tampered)

    assert result.consistent is False and result.verdict == "unknown"
    assert result.integrity != ()


def test_replay_of_a_confirmed_history_is_healthy_and_detects_raw_and_query_tampering(
    confirmed: dict[str, Any], history: ObservationStore
) -> None:
    oid = confirmed["observing"].oid
    stored = history.release_history(oid)
    result = replay_release_history(stored)
    assert result.consistent is True, result.integrity
    assert (result.stored_status, result.replayed_status, result.verdict) == (
        "healthy",
        "healthy",
        "healthy",
    )
    assert len(result.sample_evaluations) == 3
    assert all(e.verdict == "healthy" for e in result.sample_evaluations)
    queries = list(stored["samples"][-1]["queries"])
    assert [_get(q, "query_key") for q in queries] == sorted(
        _get(q, "query_key") for q in queries
    )

    raw = _thaw(stored)
    raw["samples"][-1]["queries"][0]["raw"] = bytes(_get(queries[0], "raw")) + b" "
    replayed = replay_release_history(raw)
    assert "RAW_SHA256_MISMATCH" in replayed.integrity
    assert (replayed.consistent, replayed.verdict) == (False, "unknown")

    query = _thaw(stored)
    query["samples"][-1]["queries"][0]["query"] = 'up{namespace="kube-system"}'
    replayed = replay_release_history(query)
    assert "QUERY_MISMATCH" in replayed.integrity
    assert (replayed.consistent, replayed.verdict) == (False, "unknown")


def test_session_replay_dispatches_by_purpose_and_legacy_incidents_are_unchanged(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    history: ObservationStore,
    exhausted: Observing,
    catalog: Catalog,
) -> None:
    # release: dispatched to ReleaseReplay
    release_session = _release_sessions(exhausted.oid)[0]["session_id"]
    assert isinstance(history.replay_session(release_session), ReleaseReplay)
    assert isinstance(
        observer_replay.replay_history(history.session_history(release_session)),
        ReleaseReplay,
    )
    # incident: the original shapes and types; both subjects claimable together
    incident, incident_session = _incident_session(owner, history)
    observing = _observing(owner, web, catalog)
    _make_due(incident_session)
    _make_due(observing.session_id)
    leases = observer.claim_due_samples(uuid4(), limit=50)
    assert any(
        isinstance(item, SampleLease) and item.session_id == incident_session
        for item in leases
    )
    assert any(
        isinstance(item, ReleaseSampleLease) and item.session_id == observing.session_id
        for item in leases
    )
    assert history.revoke_sessions(incident) == [incident_session]
    legacy = history.session_history(incident_session)
    assert "selected_session_id" not in legacy
    assert isinstance(history.replay_session(incident_session), ReplayReport)
    assert not isinstance(observer_replay.replay_history(legacy), ReleaseReplay)


# ===========================================================================
# 6. migration 0015 on scratch databases (固定语义 10)
# ===========================================================================


def _scratch_database() -> tuple[str, str]:
    name = f"opspilot_f11pr2_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    return name, make_conninfo(DSN, dbname=name)


def _drop_database(name: str) -> None:
    close_pools()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


def _downgrade(dsn: str, revision: str = BEFORE_0015) -> None:
    close_pools()
    schema.command.downgrade(schema._config(dsn), revision)


def _upgrade(dsn: str) -> None:
    close_pools()
    schema.command.upgrade(schema._config(dsn), "head")


def _revision(dsn: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        return schema.current_revision(conn)


def _legacy_rows(dsn: str, sessions: list[UUID]) -> dict[str, list[dict[str, Any]]]:
    return {
        table: _rows(
            f"SELECT * FROM {table} WHERE session_id = ANY(%s) ORDER BY {key}",
            (sessions,),
            dsn,
        )
        for table, key in (
            (SESSIONS, "session_id"),
            (SAMPLES, "sample_id"),
            (ENDINGS, "ending_id"),
        )
    }


def _legacy_incident_data(dsn: str) -> tuple[dict[UUID, UUID], UUID]:
    """Two incident sessions through the unchanged incident entry points: one
    still authorized with an adopted-or-filed sample, one revoked."""
    owner = DurableStore(dsn, pool=POOL)
    store = ObservationStore(dsn, pool=POOL)
    active_incident, active = _incident_session(owner, store)
    _exec(
        f"UPDATE {SESSIONS} SET authorized_at=authorized_at-interval '1 hour' "
        "WHERE session_id=%s",
        (active,),
        dsn,
    )
    (lease,) = [
        lease
        for lease in store.claim_due_samples(uuid4(), limit=50)
        if isinstance(lease, SampleLease) and lease.session_id == active
    ]
    end = _db_now(dsn) - timedelta(minutes=5)
    store.submit_sample(
        lease,
        HealthSample(
            sample_id=str(uuid4()),
            session_id=str(active),
            sequence=lease.sequence,
            window=QueryWindow(start=end - timedelta(minutes=5), end=end),
            outcome="no_data",
            subject_control_generation=lease.subject_control_generation,
            observation_generation=lease.observation_generation,
            health_profile_revision=None,
            required_signals_present=False,
        ),
        [],
    )
    revoked_incident, revoked = _incident_session(owner, store)
    assert store.revoke_sessions(revoked_incident) == [revoked]
    return {active: active_incident, revoked: revoked_incident}, active


def test_0015_backfills_legacy_incident_rows_and_round_trips_losslessly() -> None:
    name, scratch = _scratch_database()
    try:
        schema.migrate(scratch, pg_dump=PG_DUMP)
        assert _revision(scratch) == HEAD == schema.head_revision()
        head_dump = schema.schema_dump(scratch, pg_dump=PG_DUMP)
        incidents, active = _legacy_incident_data(scratch)
        sessions = list(incidents)
        # a PR-1-only release (no session/audit/policy) is lossless as well
        scratch_owner = DurableStore(scratch, pool=POOL)
        pr1 = _record_release(
            scratch_owner, ReleaseStore(scratch, pool=POOL), dsn=scratch
        )
        at_head = _legacy_rows(scratch, sessions)
        assert at_head[SAMPLES] and at_head[ENDINGS]

        _downgrade(scratch)
        assert _revision(scratch) == BEFORE_0015
        at_0014 = _legacy_rows(scratch, sessions)
        for table, rows in at_0014.items():
            assert len(rows) == len(at_head[table]), table
            for old, new in zip(rows, at_head[table], strict=True):
                assert {c: new[c] for c in old} == old, (table, "old fields unchanged")
        assert _row(
            f"SELECT status FROM {RELEASES} WHERE observation_id=%s",
            (pr1.oid,),
            scratch,
        )

        _upgrade(scratch)
        assert _revision(scratch) == HEAD
        assert schema.schema_dump(scratch, pg_dump=PG_DUMP) == head_dump
        again = _legacy_rows(scratch, sessions)
        assert again == at_head, "backfill reproduces the same neutral fields"

        for row in again[SESSIONS]:
            incident = incidents[row["session_id"]]
            assert (row["purpose"], row["subject_kind"], row["subject_id"]) == (
                "incident_recovery",
                "incident",
                incident,
            )
            assert row["release_observation_id"] is None
            assert row["authorization_source"] == "post_remediation"
        assert (
            _row(
                f"SELECT state FROM {SESSIONS} WHERE session_id=%s", (active,), scratch
            )["state"]
            == "authorized"
        ), "a valid incident session is not revoked"
        for row in again[SAMPLES]:
            assert row["purpose"] == "incident_recovery"
            assert (row["subject_kind"], row["subject_id"]) == (
                "incident",
                incidents[row["session_id"]],
            )
            assert row["current_subject_state"] == row["subject_lifecycle"]
            assert (
                row["current_subject_control_generation"]
                == row["incident_control_generation"]
            )
            assert (
                row["current_subject_observation_generation"]
                == row["incident_observation_generation"]
            )
        for row in again[ENDINGS]:
            assert row["purpose"] == "incident_recovery"
            assert (row["subject_kind"], row["subject_id"]) == (
                "incident",
                incidents[row["session_id"]],
            )
            assert row["release_observation_id"] is None
            assert row["subject_state_before"] == row["lifecycle_before"]
            assert row["subject_state_after"] == row["lifecycle_after"]
            assert row["anomaly_key"] is None
        record = ReleaseStore(scratch, pool=POOL).get_release(pr1.oid)
        assert (record.status, record.used_sample_count) == ("pending", 0)
        assert record.calibrated_policy_revision is None
    finally:
        _drop_database(name)


def test_0015_refuses_a_legacy_release_session_bound_to_an_incident() -> None:
    name, scratch = _scratch_database()
    try:
        schema.migrate(scratch, pg_dump=PG_DUMP)
        incidents, active = _legacy_incident_data(scratch)
        _downgrade(scratch)
        forged = uuid4()
        _exec(
            f"INSERT INTO {SESSIONS} (session_id,incident_id,purpose,target_id,target,"
            "subject_control_generation,observation_generation,authorized_by,"
            "authorized_global_generation,authorized_target_generation,deadline_at,"
            "max_samples,sample_interval_seconds,sustained_window_seconds,state,"
            "ended_reason,authorization_source) "
            "SELECT %s,incident_id,'release_observation',target_id,target,"
            "subject_control_generation,observation_generation,authorized_by,"
            "authorized_global_generation,authorized_target_generation,deadline_at,"
            "max_samples,sample_interval_seconds,sustained_window_seconds,'revoked',"
            "'authority_revoked',NULL FROM " + SESSIONS + " WHERE session_id=%s",
            (forged, active),
            scratch,
        )
        close_pools()
        with pytest.raises(Exception) as raised:
            schema.command.upgrade(schema._config(scratch), "head")
        assert "LEGACY_RELEASE_SESSION_UNBOUND" in str(raised.value)
        assert _revision(scratch) == BEFORE_0015
        assert _row(
            f"SELECT purpose FROM {SESSIONS} WHERE session_id=%s", (forged,), scratch
        )
    finally:
        _drop_database(name)


@pytest.mark.parametrize("data", ["rejected-audit", "paused", "authorized-session"])
def test_0015_downgrade_is_refused_when_release_observation_data_exists(
    catalog: Catalog, data: str
) -> None:
    name, scratch = _scratch_database()
    try:
        schema.migrate(scratch, pg_dump=PG_DUMP)
        store = ReleaseStore(scratch, pool=POOL)
        release = _record_release(DurableStore(scratch, pool=POOL), store, dsn=scratch)
        if data == "rejected-audit":
            receipt = _authorize(store, release.oid, "checkout-release@0123456789ab")
            assert receipt.reason == "RELEASE_POLICY_UNCALIBRATED"
        elif data == "paused":
            assert _control(store, release.oid, "pause", 0).outcome == "applied"
        else:
            entry = _entry(release)
            catalog.add(entry)
            assert (
                _authorize(store, release.oid, entry["revision"]).outcome == "applied"
            )
        audit = _rows(f"SELECT * FROM {AUDIT} ORDER BY sequence", (), scratch)
        sessions = _rows(f"SELECT * FROM {SESSIONS}", (), scratch)

        with pytest.raises(Exception) as raised:
            _downgrade(scratch)

        assert "RELEASE_OBSERVATION_DATA_WOULD_BE_LOST" in str(raised.value)
        # Each revision commits separately: 0016 restores the 0015 function;
        # the data-loss refusal rolls back only the attempted 0015 downgrade.
        assert _revision(scratch) == REVISION_0015
        assert _rows(f"SELECT * FROM {AUDIT} ORDER BY sequence", (), scratch) == audit
        assert _rows(f"SELECT * FROM {SESSIONS}", (), scratch) == sessions
    finally:
        _drop_database(name)
