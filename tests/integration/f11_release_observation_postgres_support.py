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
from opspilot.persistence import DurableStore, PoolConfig, close_pools
from opspilot.persistence.grants import WEB_ROLE, WORKER_ROLE
from scripts.m0.postgres_lab import DSN
from tests.f11_release_support import (
    AFTER_HASH,
    BEFORE_HASH,
    EMPTY_CATALOG,
    PROFILE,
    PROFILE_CONTENT,
    PROFILE_REVISION,
    EvidenceBuilder,
    Pod,
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
from tests.runtime_roles_support import login
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
HEAD = "0026_chat_panel"

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


# ===========================================================================
# 2. command semantics (固定语义 2/3)
# ===========================================================================


# ===========================================================================
# 4. adoption, windows, fences (固定语义 4/7/8)
# ===========================================================================


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


# ===========================================================================
# 7. guards and grants under the runtime login roles (固定语义 11)
# ===========================================================================


def _has_column(role: str, table: str, column: str, privilege: str) -> bool:
    return bool(
        _row(
            "SELECT has_column_privilege(%s,%s,%s,%s) AS ok",
            (role, table, column, privilege),
        )["ok"]
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
