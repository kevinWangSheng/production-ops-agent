"""Independent contract tests for F11 PR-1: release events and the durable
``ReleaseObservation`` record (migration 0014, ``opspilot.observation.releases``).

Written from ``docs/tasks/2026-10-12-f11-pr1-contract.md`` (「公开接口 r2」,
「固定语义」 and the ruled decisions P1-01..P1-13) without reading the
implementation. Behaviour is driven through the public ``ReleaseStore``;
SQL is used only to read the two tables the contract names, to construct
explicit race fixtures and to attempt writes a runtime role must be refused.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from opspilot import schema
from opspilot.observation.releases import (
    ReleaseAuxiliary,
    ReleaseEvent,
    ReleaseReceipt,
    ReleaseRevision,
    ReleaseStore,
)
from opspilot.observer import (
    PROFILE_DIRECTORY,
    HealthProfile,
    SessionParameters,
    canonical_content,
    load_health_profile,
    profile_revision,
)
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig, close_pools
from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import catalog_grants, login, relations
from tests.target_support import IDENTITY, WORKLOAD

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=1, max_size=6, timeout=5.0)
OBSERVER_ROLE = "opspilot_observer"
OBSERVATIONS = "opspilot_release_observations"
EVENTS = "opspilot_release_events"
BEFORE_0014 = "0013_self_heal_authorization"

PROFILE = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
PROFILE_CONTENT = canonical_content(PROFILE)
PROFILE_REVISION = profile_revision(PROFILE)
SESSION = PROFILE.session  # deadline 3600, sustained 600, cadence 60, max 40
MIN_TRACKING = 1200  # P1-02 development candidate; explicit, never defaulted
AUDIT_LIMIT = 16 * 1024


# --- fixtures and helpers -------------------------------------------------


@pytest.fixture(scope="module")
def owner() -> Iterator[DurableStore]:
    store = DurableStore(DSN, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture(scope="module")
def releases() -> ReleaseStore:
    return ReleaseStore(DSN, pool=POOL)


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _revision(seed: str) -> ReleaseRevision:
    return ReleaseRevision(
        images={"checkout": _digest(f"image-{seed}")},
        pod_template_digest=_digest(f"template-{seed}"),
    )


def _db_now(dsn: str = DSN) -> datetime:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return row[0]


def _event(
    *,
    released_at: datetime,
    source: str = "argo-rollouts",
    release_id: str | None = None,
    before: str = "v1",
    after: str = "v2",
    auxiliary: ReleaseAuxiliary | None = None,
) -> ReleaseEvent:
    fields: dict[str, Any] = {
        "format_version": 1,
        "source": source,
        "source_release_id": release_id or f"rel-{uuid4().hex}",
        "released_at": released_at,
        "before": _revision(before),
        "after": _revision(after),
    }
    if auxiliary is not None:
        fields["auxiliary"] = auxiliary
    return ReleaseEvent(**fields)


def _target(
    store: DurableStore, *, complete: bool = True, workload: str = WORKLOAD
) -> tuple[UUID, dict[str, str]]:
    """A registry row plus the identity the configured registry holds for it.
    ``complete=False`` leaves the row bare (``resource_uid`` only), as intake
    registers it."""
    resource_uid = f"deployment/checkout-release-{uuid4().hex[:10]}"
    target_id = store.register_target(resource_uid)
    identity = {**IDENTITY, "resource_uid": resource_uid, "workload": workload}
    if complete:
        with store.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_targets SET integration_id=%s,cluster_uid=%s,"
                "namespace=%s,workload=%s WHERE target_id=%s",
                (
                    identity["integration_id"],
                    identity["cluster_uid"],
                    identity["namespace"],
                    workload,
                    target_id,
                ),
            )
    return target_id, identity


def _record(
    store: ReleaseStore,
    event: ReleaseEvent,
    target_id: UUID,
    identity: dict[str, str] | None,
    *,
    min_tracking_seconds: int = MIN_TRACKING,
    revision: str = PROFILE_REVISION,
    content: str = PROFILE_CONTENT,
    raw_body: bytes | None = None,
    actor: str = "release-source:test",
) -> ReleaseReceipt:
    return store.record_release(
        event=event,
        target_id=target_id,
        identity=identity,  # r3: Mapping[str, str] | None
        health_profile_revision=revision,
        health_profile=content,
        min_tracking_seconds=min_tracking_seconds,
        actor=actor,
        raw_body=event.model_dump_json().encode() if raw_body is None else raw_body,
    )


def _event_row(event_id: int) -> dict[str, Any] | None:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        return conn.execute(
            f"SELECT * FROM {EVENTS} WHERE event_id=%s", (event_id,)
        ).fetchone()


def _observation_count(source: str, release_id: str) -> int:
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {OBSERVATIONS} WHERE source=%s AND source_release_id=%s",
            (source, release_id),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _as_revision(value: Any) -> ReleaseRevision:
    return (
        value
        if isinstance(value, ReleaseRevision)
        else ReleaseRevision.model_validate(value)
    )


def _sha256_matches(stored: str, body: bytes) -> bool:
    # The contract fixes the value (sha256 of raw_body), not its spelling.
    hexdigest = hashlib.sha256(body).hexdigest()
    return stored in (hexdigest, f"sha256:{hexdigest}")


def _frozen(record: Any) -> dict[str, Any]:
    names = (
        "observation_id",
        "source",
        "source_release_id",
        "target_id",
        "released_at",
        "received_at",
        "deadline_at",
        "deadline_seconds",
        "min_tracking_seconds",
        "sample_interval_seconds",
        "sustained_window_seconds",
        "max_samples",
        "health_profile_revision",
        "semantic_sha256",
        "status",
        "ended_reason",
    )
    frozen = {name: getattr(record, name) for name in names}
    frozen["before_revision"] = _as_revision(record.before_revision)
    frozen["after_revision"] = _as_revision(record.after_revision)
    return frozen


def _assert_rejected_without_record(
    receipt: ReleaseReceipt, reason: str, event: ReleaseEvent, store: ReleaseStore
) -> None:
    assert receipt.outcome == "rejected"
    assert receipt.reason == reason
    assert receipt.observation_id is None
    assert receipt.status is None
    row = _event_row(receipt.event_id)
    assert row is not None, "a business rejection commits its audit row"
    assert row["outcome"] == "rejected"
    assert row["reason"] == reason
    assert row["observation_id"] is None
    assert row["source"] == event.source
    assert row["source_release_id"] == event.source_release_id
    assert store.find_release(event.source, event.source_release_id) is None
    assert _observation_count(event.source, event.source_release_id) == 0


# --- creation, frozen policy and audit (固定语义: 时间, policy, 审计) ----------


def test_created_release_freezes_time_policy_and_revisions(owner, releases) -> None:
    target_id, identity = _target(owner)
    released_at = _db_now() - timedelta(minutes=5)
    event = _event(
        released_at=released_at,
        auxiliary=ReleaseAuxiliary(deployment_uid="uid-1", deployment_generation=7),
    )
    raw_body = event.model_dump_json().encode()
    t0 = _db_now()
    receipt = _record(
        releases, event, target_id, identity, raw_body=raw_body, actor="argo"
    )
    t1 = _db_now()

    assert receipt.outcome == "created"
    assert receipt.status == "pending"
    assert receipt.reason is None
    assert isinstance(receipt.observation_id, UUID)

    record = releases.get_release(receipt.observation_id)
    assert record.observation_id == receipt.observation_id
    assert record.status == "pending"
    assert record.source == event.source
    assert record.source_release_id == event.source_release_id
    assert record.target_id == target_id
    assert record.released_at == released_at
    assert t0 <= record.received_at <= t1, "received_at comes from the DB clock"
    assert record.deadline_at == released_at + timedelta(
        seconds=SESSION.deadline_seconds
    )
    assert record.deadline_seconds == SESSION.deadline_seconds
    assert record.sample_interval_seconds == SESSION.sample_interval_seconds
    assert record.sustained_window_seconds == SESSION.sustained_window_seconds
    assert record.max_samples == SESSION.max_samples
    assert record.health_profile_revision == PROFILE_REVISION
    assert record.min_tracking_seconds == MIN_TRACKING
    assert _as_revision(record.before_revision) == event.before
    assert _as_revision(record.after_revision) == event.after

    found = releases.find_release(event.source, event.source_release_id)
    assert found is not None and found.observation_id == receipt.observation_id

    # the profile content is kept behind its revision (opspilot_health_profiles)
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT content FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (PROFILE_REVISION,),
        ).fetchone() == (PROFILE_CONTENT,)

    events = releases.release_events(receipt.observation_id)
    assert len(events) == 1
    audit = events[0]
    assert audit.event_id == receipt.event_id
    assert audit.observation_id == receipt.observation_id
    assert audit.outcome == "created"
    assert audit.reason is None
    assert audit.source == event.source
    assert audit.source_release_id == event.source_release_id
    assert audit.actor == "argo"
    assert t0 <= audit.received_at <= t1
    assert _sha256_matches(audit.raw_sha256, raw_body)
    assert audit.semantic_sha256 == record.semantic_sha256
    assert audit.truncated is False
    assert isinstance(audit.sequence, int)


def test_late_first_delivery_is_created_unknown_late_delivery(owner, releases) -> None:
    target_id, identity = _target(owner)
    released_at = _db_now() - timedelta(seconds=SESSION.deadline_seconds + 120)
    event = _event(released_at=released_at)

    receipt = _record(releases, event, target_id, identity)

    assert receipt.outcome == "created"
    assert receipt.status == "unknown"
    assert receipt.reason == "late_delivery"
    record = releases.get_release(receipt.observation_id)
    assert record.status == "unknown"
    assert record.ended_reason == "late_delivery"
    assert record.released_at == released_at, "time is never clamped or rewritten"
    assert record.deadline_at == released_at + timedelta(
        seconds=SESSION.deadline_seconds
    )
    assert record.received_at >= record.deadline_at
    (audit,) = releases.release_events(receipt.observation_id)
    assert audit.outcome == "created"


def test_future_released_at_is_rejected_with_audit_and_no_record(
    owner, releases
) -> None:
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() + timedelta(minutes=10))

    receipt = _record(releases, event, target_id, identity)

    _assert_rejected_without_record(receipt, "RELEASE_TIME_IN_FUTURE", event, releases)


def test_policy_where_min_tracking_reaches_the_deadline_is_rejected(
    owner, releases
) -> None:
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=1))

    receipt = _record(
        releases,
        event,
        target_id,
        identity,
        min_tracking_seconds=SESSION.deadline_seconds,
    )

    _assert_rejected_without_record(receipt, "INVALID_RELEASE_POLICY", event, releases)


def test_policy_one_second_below_the_deadline_is_accepted(owner, releases) -> None:
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=1))

    receipt = _record(
        releases,
        event,
        target_id,
        identity,
        min_tracking_seconds=SESSION.deadline_seconds - 1,
    )

    assert receipt.outcome == "created"
    assert releases.get_release(receipt.observation_id).min_tracking_seconds == (
        SESSION.deadline_seconds - 1
    )


def test_policy_where_the_sustained_window_reaches_the_deadline_is_rejected(
    owner, releases
) -> None:
    # max(min_tracking, sustained_window) >= deadline: here the profile's own
    # sustained window equals its deadline while min_tracking is small.
    tight = HealthProfile.model_validate(
        {
            **PROFILE.model_dump(mode="python"),
            "session": SessionParameters(
                deadline_seconds=600,
                max_samples=11,
                sample_interval_seconds=60,
                sustained_window_seconds=600,
            ),
        }
    )
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=1))

    receipt = _record(
        releases,
        event,
        target_id,
        identity,
        min_tracking_seconds=60,
        revision=profile_revision(tight),
        content=canonical_content(tight),
    )

    _assert_rejected_without_record(receipt, "INVALID_RELEASE_POLICY", event, releases)


def test_min_tracking_seconds_has_no_default(owner, releases) -> None:
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=1))
    with pytest.raises(TypeError):
        releases.record_release(  # type: ignore[call-arg]
            event=event,
            target_id=target_id,
            identity=identity,
            health_profile_revision=PROFILE_REVISION,
            health_profile=PROFILE_CONTENT,
            actor="argo",
            raw_body=b"{}",
        )
    assert releases.find_release(event.source, event.source_release_id) is None


def test_reading_an_unknown_release(releases) -> None:
    with pytest.raises(PersistenceError) as raised:
        releases.get_release(uuid4())
    assert raised.value.args[0] == "NOT_FOUND"
    assert releases.find_release("argo-rollouts", f"missing-{uuid4().hex}") is None


# --- idempotency (P1-03, P1-06, P1-08) --------------------------------------


def test_identical_redelivery_is_replayed_and_changes_nothing(owner, releases) -> None:
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=5))
    first = _record(releases, event, target_id, identity, raw_body=b'{"delivery":1}')
    before = _frozen(releases.get_release(first.observation_id))

    # Same semantic content: different transport bytes and a different
    # min_tracking (policy is frozen at creation, never recomputed).
    second = _record(
        releases,
        event,
        target_id,
        identity,
        raw_body=b'{"delivery":2}',
        min_tracking_seconds=900,
    )

    assert second.outcome == "replayed"
    assert second.observation_id == first.observation_id
    assert second.status == "pending"
    assert second.event_id != first.event_id
    assert _frozen(releases.get_release(first.observation_id)) == before
    assert _observation_count(event.source, event.source_release_id) == 1

    events = releases.release_events(first.observation_id)
    assert [e.event_id for e in events] == [first.event_id, second.event_id]
    assert [e.outcome for e in events] == ["created", "replayed"]
    assert events[0].sequence < events[1].sequence
    assert events[0].semantic_sha256 == events[1].semantic_sha256
    assert _sha256_matches(events[0].raw_sha256, b'{"delivery":1}')
    assert _sha256_matches(events[1].raw_sha256, b'{"delivery":2}')
    assert events[1].received_at >= events[0].received_at

    later = releases.release_events(first.observation_id, after=events[0].sequence)
    assert [e.event_id for e in later] == [second.event_id]


def test_replay_of_a_late_release_does_not_reset_its_state(owner, releases) -> None:
    target_id, identity = _target(owner)
    event = _event(
        released_at=_db_now() - timedelta(seconds=SESSION.deadline_seconds + 60)
    )
    first = _record(releases, event, target_id, identity)
    before = _frozen(releases.get_release(first.observation_id))

    again = _record(releases, event, target_id, identity)

    assert again.outcome == "replayed"
    assert again.observation_id == first.observation_id
    assert again.status == "unknown"
    assert _frozen(releases.get_release(first.observation_id)) == before


def _conflicting_variants(
    event: ReleaseEvent, other_target: tuple[UUID, dict[str, str]]
) -> list[tuple[str, ReleaseEvent, tuple[UUID, dict[str, str]] | None]]:
    same = {
        "released_at": event.released_at,
        "source": event.source,
        "release_id": event.source_release_id,
    }
    return [
        ("target", _event(**same), other_target),
        ("before", _event(**same, before="v0"), None),
        ("after", _event(**same, after="v3"), None),
        (
            "released_at",
            _event(**{**same, "released_at": event.released_at - timedelta(seconds=1)}),
            None,
        ),
        (
            "auxiliary",
            _event(**same, auxiliary=ReleaseAuxiliary(deployment_uid="another-uid")),
            None,
        ),
    ]


def test_same_release_id_with_different_content_is_an_identity_conflict(
    owner, releases
) -> None:
    target = _target(owner)
    other_target = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=5))
    original = _record(releases, event, *target)
    before = _frozen(releases.get_release(original.observation_id))

    conflicts = []
    for label, variant, variant_target in _conflicting_variants(event, other_target):
        receipt = _record(releases, variant, *(variant_target or target))
        assert receipt.outcome == "rejected", label
        assert receipt.reason == "RELEASE_IDENTITY_CONFLICT", label
        assert receipt.observation_id == original.observation_id, label
        conflicts.append(receipt.event_id)

    assert _frozen(releases.get_release(original.observation_id)) == before
    assert _observation_count(event.source, event.source_release_id) == 1
    events = releases.release_events(original.observation_id)
    assert [e.event_id for e in events] == [original.event_id, *conflicts]
    assert [e.outcome for e in events[1:]] == ["rejected"] * len(conflicts)
    assert {e.reason for e in events[1:]} == {"RELEASE_IDENTITY_CONFLICT"}
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)


def test_a_new_release_id_with_identical_digests_is_a_new_release(
    owner, releases
) -> None:
    target_id, identity = _target(owner)
    released_at = _db_now() - timedelta(minutes=5)
    first_event = _event(released_at=released_at)
    first = _record(releases, first_event, target_id, identity)
    second_event = _event(released_at=released_at)  # new source_release_id only

    second = _record(releases, second_event, target_id, identity)

    assert second.outcome == "created"
    assert second.observation_id != first.observation_id
    # the earlier release is not cancelled or otherwise touched
    assert releases.get_release(first.observation_id).status == "pending"


def test_the_identity_key_is_source_and_release_id(owner, releases) -> None:
    target_id, identity = _target(owner)
    released_at = _db_now() - timedelta(minutes=5)
    release_id = f"rel-{uuid4().hex}"
    first = _record(
        releases,
        _event(released_at=released_at, release_id=release_id, source="argo"),
        target_id,
        identity,
    )
    second = _record(
        releases,
        _event(released_at=released_at, release_id=release_id, source="flux"),
        target_id,
        identity,
    )
    assert first.outcome == second.outcome == "created"
    assert first.observation_id != second.observation_id


# --- target identity and profile binding (固定语义: identity) ---------------


def test_identity_disagreeing_with_a_complete_registry_row_is_refused(
    owner, releases
) -> None:
    target_id, identity = _target(owner)
    for field in (
        "cluster_uid",
        "integration_id",
        "namespace",
        "resource_uid",
        "workload",
    ):
        event = _event(released_at=_db_now() - timedelta(minutes=1))
        wrong = {**identity, field: f"{identity[field]}-other"}
        receipt = _record(releases, event, target_id, wrong)
        _assert_rejected_without_record(receipt, "TARGET_MISMATCH", event, releases)
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT integration_id,cluster_uid,namespace,resource_uid,workload "
            "FROM opspilot_targets WHERE target_id=%s",
            (target_id,),
        ).fetchone()
    assert row == identity


def test_a_bare_registry_row_is_completed_from_the_configured_identity(
    owner, releases
) -> None:
    target_id, identity = _target(owner, complete=False)
    event = _event(released_at=_db_now() - timedelta(minutes=1))

    receipt = _record(releases, event, target_id, identity)

    assert receipt.outcome == "created"
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT integration_id,cluster_uid,namespace,resource_uid,workload "
            "FROM opspilot_targets WHERE target_id=%s",
            (target_id,),
        ).fetchone()
    assert row == identity


def test_a_bare_registry_row_without_an_identity_is_refused(owner, releases) -> None:
    target_id, _ = _target(owner, complete=False)
    event = _event(released_at=_db_now() - timedelta(minutes=1))

    receipt = _record(releases, event, target_id, None)

    _assert_rejected_without_record(receipt, "TARGET_IDENTITY_MISSING", event, releases)


def test_a_profile_for_another_workload_is_refused(owner, releases) -> None:
    target_id, identity = _target(owner, workload="payment")
    event = _event(released_at=_db_now() - timedelta(minutes=1))

    receipt = _record(releases, event, target_id, identity)

    _assert_rejected_without_record(
        receipt, "HEALTH_PROFILE_TARGET_MISMATCH", event, releases
    )


@pytest.mark.parametrize(
    "first_with_identity", [False, True], ids=["none-then-full", "full-then-none"]
)
def test_identity_parameter_form_does_not_change_the_fingerprint(
    owner, releases, first_with_identity
) -> None:
    # r3: the fingerprint covers the target, not the call parameter; for a
    # complete registry row ``None`` and the equivalent mapping are the same.
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=2))
    forms = (identity, None) if first_with_identity else (None, identity)

    first = _record(releases, event, target_id, forms[0])
    second = _record(releases, event, target_id, forms[1])

    assert first.outcome == "created"
    assert second.outcome == "replayed"
    assert second.observation_id == first.observation_id
    events = releases.release_events(first.observation_id)
    assert [e.outcome for e in events] == ["created", "replayed"]
    assert events[0].semantic_sha256 == events[1].semantic_sha256


@pytest.mark.parametrize(
    ("label", "revision", "content"),
    [
        (
            "revision not hashing the content",
            f"{PROFILE.profile_id}@000000000000",
            None,
        ),
        ("content that is not a profile", None, '{"not":"a profile"}'),
    ],
)
def test_an_invalid_profile_revision_or_content_is_an_invalid_policy(
    owner, releases, label, revision, content
) -> None:
    # r3: invalid profile content or revision -> INVALID_RELEASE_POLICY.
    if content is None:
        content = PROFILE_CONTENT
    if revision is None:
        digest = hashlib.sha256(content.encode()).hexdigest()
        revision = f"{PROFILE.profile_id}@{digest[:12]}"
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=1))

    receipt = _record(
        releases, event, target_id, identity, revision=revision, content=content
    )

    _assert_rejected_without_record(receipt, "INVALID_RELEASE_POLICY", event, releases)


# --- structural validation (固定语义: pydantic before storage) --------------


def _valid_fields() -> dict[str, Any]:
    return {
        "format_version": 1,
        "source": "argo-rollouts",
        "source_release_id": f"rel-{uuid4().hex}",
        "released_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "before": {
            "images": {"checkout": _digest("a")},
            "pod_template_digest": _digest("b"),
        },
        "after": {
            "images": {"checkout": _digest("c")},
            "pod_template_digest": _digest("d"),
        },
    }


def _with(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    fields = _valid_fields()
    node = fields
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return fields


@pytest.mark.parametrize(
    ("label", "fields"),
    [
        ("format_version 2", _with(("format_version",), 2)),
        ("naive released_at", _with(("released_at",), datetime(2026, 10, 12, 8, 0))),
        ("no images", _with(("after", "images"), {})),
        ("digest without prefix", _with(("after", "pod_template_digest"), "0" * 64)),
        (
            "short digest",
            _with(("after", "images"), {"checkout": "sha256:" + "0" * 63}),
        ),
        (
            "uppercase digest",
            _with(("before", "images"), {"checkout": "sha256:" + "A" * 64}),
        ),
        (
            "other algorithm",
            _with(("before", "pod_template_digest"), "sha512:" + "0" * 64),
        ),
        ("unknown top-level field", {**_valid_fields(), "target_id": str(uuid4())}),
        ("unknown revision field", _with(("after", "config"), "plain text")),
        ("unknown auxiliary field", {**_valid_fields(), "auxiliary": {"secret": "x"}}),
        ("missing after", {k: v for k, v in _valid_fields().items() if k != "after"}),
        ("NUL in source", _with(("source",), "argo\x00rollouts")),
        ("NUL in source_release_id", _with(("source_release_id",), "rel-\x00-1")),
        (
            "unpaired surrogate in a container name",
            _with(("after", "images"), {"check\ud800out": _digest("c")}),
        ),
        (
            "NUL in an auxiliary field",
            {**_valid_fields(), "auxiliary": {"deployment_uid": "uid\x00-1"}},
        ),
    ],
)
def test_structurally_invalid_events_are_refused_by_the_dto(label, fields) -> None:
    with pytest.raises(ValidationError):
        ReleaseEvent.model_validate(fields)


def test_the_valid_baseline_event_is_accepted_by_the_dto() -> None:
    event = ReleaseEvent.model_validate(_valid_fields())
    assert event.auxiliary == ReleaseAuxiliary()


# --- concurrency (P1-11) ----------------------------------------------------


def _wait_for_lock_waiter(timeout: float = 3.0) -> bool:
    """True once some backend of this database waits on a heavyweight lock."""
    deadline = time.monotonic() + timeout
    with psycopg.connect(DSN, autocommit=True) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()
            if row is not None and row[0] > 0:
                return True
            time.sleep(0.02)
    return False


def _held_first_delivery(
    releases: ReleaseStore,
    event: ReleaseEvent,
    target: tuple[UUID, dict[str, str]],
    *,
    commit: bool,
) -> tuple[ReleaseReceipt, ReleaseReceipt]:
    """Delivery A writes inside an open transaction; delivery B starts while
    A is uncommitted; A then commits or rolls back. Returns (A, B)."""
    written, finish = threading.Event(), threading.Event()
    results: dict[str, Any] = {}

    def first() -> None:
        try:
            with releases.transaction() as conn:
                results["a"] = releases.record_release_in(
                    conn,
                    event=event,
                    target_id=target[0],
                    identity=target[1],
                    health_profile_revision=PROFILE_REVISION,
                    health_profile=PROFILE_CONTENT,
                    min_tracking_seconds=MIN_TRACKING,
                    actor="delivery-a",
                    raw_body=b"a",
                )
                written.set()
                finish.wait(10)
                if not commit:
                    raise _Rollback()
        except _Rollback:
            pass
        except BaseException as exc:  # surfaced to the test thread
            results["a_error"] = exc
            written.set()

    def second() -> None:
        try:
            results["b"] = _record(
                releases, event, *target, raw_body=b"b", actor="delivery-b"
            )
        except BaseException as exc:
            results["b_error"] = exc

    a = threading.Thread(target=first)
    a.start()
    assert written.wait(10)
    assert "a_error" not in results, results.get("a_error")
    b = threading.Thread(target=second)
    b.start()
    _wait_for_lock_waiter()  # B must block on A's uncommitted row (or its lock)
    finish.set()
    a.join(10)
    b.join(10)
    assert "b_error" not in results, results.get("b_error")
    return results["a"], results["b"]


class _Rollback(Exception):
    pass


def test_concurrent_first_delivery_waits_and_replays_the_committed_one(
    owner, releases
) -> None:
    target = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=2))

    a, b = _held_first_delivery(releases, event, target, commit=True)

    assert a.outcome == "created"
    assert b.outcome == "replayed"
    assert b.observation_id == a.observation_id
    assert _observation_count(event.source, event.source_release_id) == 1
    outcomes = [e.outcome for e in releases.release_events(a.observation_id)]
    assert outcomes == ["created", "replayed"]


def test_a_rolled_back_first_delivery_leaves_the_concurrent_one_to_create(
    owner, releases
) -> None:
    target = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=2))

    a, b = _held_first_delivery(releases, event, target, commit=False)

    assert b.outcome == "created"
    assert b.observation_id != a.observation_id
    assert _event_row(a.event_id) is None, "the rolled-back audit row is gone"
    with pytest.raises(PersistenceError) as raised:
        releases.get_release(a.observation_id)
    assert raised.value.args[0] == "NOT_FOUND"
    found = releases.find_release(event.source, event.source_release_id)
    assert found is not None and found.observation_id == b.observation_id
    assert _observation_count(event.source, event.source_release_id) == 1


def test_racing_first_deliveries_create_exactly_one_record(owner, releases) -> None:
    target = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=2))
    start = threading.Barrier(4)
    receipts: list[ReleaseReceipt] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def deliver(n: int) -> None:
        start.wait(10)
        try:
            receipt = _record(releases, event, *target, raw_body=f"{n}".encode())
        except BaseException as exc:
            with lock:
                failures.append(exc)
            return
        with lock:
            receipts.append(receipt)

    threads = [threading.Thread(target=deliver, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert failures == []
    assert sorted(r.outcome for r in receipts) == [
        "created",
        "replayed",
        "replayed",
        "replayed",
    ]
    assert len({r.observation_id for r in receipts}) == 1
    assert _observation_count(event.source, event.source_release_id) == 1
    assert len(releases.release_events(receipts[0].observation_id)) == 4


# --- audit text (P1-08) -----------------------------------------------------


def test_audit_text_is_redacted(owner, releases) -> None:
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=1))
    secrets = ("hunter2SecretValue", "abcdefSECRETtoken123", "sk-livesecret999")
    raw_body = (
        '{"note":"password=hunter2SecretValue",'
        '"auth":"Authorization: Bearer abcdefSECRETtoken123",'
        '"api_key":"sk-livesecret999","release":"checkout"}'
    ).encode()

    receipt = _record(releases, event, target_id, identity, raw_body=raw_body)

    (audit,) = releases.release_events(receipt.observation_id)
    assert audit.truncated is False
    for secret in secrets:
        assert secret not in audit.audit_text
    assert "checkout" in audit.audit_text
    assert _sha256_matches(audit.raw_sha256, raw_body)


def test_audit_text_over_16_kib_is_truncated(owner, releases) -> None:
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() - timedelta(minutes=1))
    raw_body = ('{"padding":"' + "x" * (AUDIT_LIMIT + 4000) + '"}').encode()

    receipt = _record(releases, event, target_id, identity, raw_body=raw_body)

    assert receipt.outcome == "created"
    (audit,) = releases.release_events(receipt.observation_id)
    assert audit.truncated is True
    assert 0 < len(audit.audit_text.encode("utf-8")) <= AUDIT_LIMIT
    assert _sha256_matches(audit.raw_sha256, raw_body), "hash is over the full body"


def test_audit_text_of_a_rejection_is_redacted(owner, releases) -> None:
    target_id, identity = _target(owner)
    event = _event(released_at=_db_now() + timedelta(minutes=10))
    raw_body = b'{"token":"password=rejectedSecret42"}'

    receipt = _record(releases, event, target_id, identity, raw_body=raw_body)

    assert receipt.outcome == "rejected"
    row = _event_row(receipt.event_id)
    assert row is not None
    assert "rejectedSecret42" not in row["audit_text"]


# --- runtime roles (P1-07, P1-13) -------------------------------------------


@pytest.fixture(scope="module")
def web_dsn():
    with login(DSN, WEB_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def worker_dsn():
    with login(DSN, WORKER_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def observer_dsn():
    with login(DSN, OBSERVER_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def web_release(owner, web_dsn) -> tuple[ReleaseStore, ReleaseReceipt]:
    """A release recorded by the web role through the store (bare registry
    row, so the identity completion runs under the web role too)."""
    store = ReleaseStore(web_dsn, pool=POOL)
    target_id, identity = _target(owner, complete=False)
    event = _event(released_at=_db_now() - timedelta(minutes=2))
    receipt = _record(store, event, target_id, identity)
    return store, receipt


def test_the_web_role_records_releases_through_the_store(
    owner, web_dsn, web_release
) -> None:
    store, receipt = web_release
    assert receipt.outcome == "created"
    assert receipt.status == "pending"
    assert store.get_release(receipt.observation_id).status == "pending"
    assert len(store.release_events(receipt.observation_id)) == 1

    # the late-delivery initial state is a legitimate web write as well
    target_id, identity = _target(owner)
    late = _event(
        released_at=_db_now() - timedelta(seconds=SESSION.deadline_seconds + 60)
    )
    late_receipt = _record(store, late, target_id, identity)
    assert (late_receipt.outcome, late_receipt.status, late_receipt.reason) == (
        "created",
        "unknown",
        "late_delivery",
    )


def test_the_web_role_holds_only_select_and_column_insert_on_the_new_tables() -> None:
    for table in (OBSERVATIONS, EVENTS):
        declared = RUNTIME_GRANTS[WEB_ROLE][table]
        assert "SELECT" in declared
        assert all(
            p == "SELECT"
            or p.startswith("INSERT(")
            or (
                table == OBSERVATIONS
                and p
                == "UPDATE(calibrated_policy_content,calibrated_policy_revision,calibrated_policy_sha256,control_generation,ended_at,ended_reason,observation_generation,paused,status,updated_at)"
            )
            for p in declared
        ), declared
        assert RUNTIME_GRANTS[WORKER_ROLE][table] == frozenset()
    with psycopg.connect(DSN) as conn:
        web = catalog_grants(conn, WEB_ROLE)
        worker = catalog_grants(conn, WORKER_ROLE)
        observer = catalog_grants(conn, OBSERVER_ROLE)
        for table in (OBSERVATIONS, EVENTS):
            assert web[table] == RUNTIME_GRANTS[WEB_ROLE][table]
            assert worker[table] == frozenset()
            expected_observer = (
                frozenset(
                    {
                        "SELECT",
                        "UPDATE(ended_at,ended_reason,status,updated_at,used_sample_count)",
                    }
                )
                if table == OBSERVATIONS
                else frozenset()
            )
            assert observer[table] == expected_observer


@pytest.mark.parametrize(
    "assignment",
    [
        "status = 'observing'",
        "status = 'healthy'",
        "released_at = released_at - interval '1 hour'",
        "received_at = received_at - interval '1 hour'",
        "deadline_at = deadline_at + interval '1 hour'",
        "deadline_seconds = deadline_seconds + 1",
        "min_tracking_seconds = min_tracking_seconds + 1",
        "sample_interval_seconds = sample_interval_seconds + 1",
        "sustained_window_seconds = sustained_window_seconds + 1",
        "max_samples = max_samples + 1",
        "health_profile_revision = health_profile_revision",
        "source_release_id = source_release_id || '-x'",
        "target_id = target_id",
        "semantic_sha256 = semantic_sha256",
        "before_revision = after_revision",
    ],
)
def test_the_web_role_cannot_update_a_release(web_dsn, web_release, assignment) -> None:
    _, receipt = web_release
    with psycopg.connect(web_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                f"UPDATE {OBSERVATIONS} SET {assignment} WHERE observation_id=%s",
                (receipt.observation_id,),
            )


def test_the_web_role_cannot_delete_or_rewrite_the_audit(web_dsn, web_release) -> None:
    _, receipt = web_release
    for statement in (
        f"DELETE FROM {OBSERVATIONS} WHERE observation_id=%s",
        f"DELETE FROM {EVENTS} WHERE observation_id=%s",
        f"UPDATE {EVENTS} SET audit_text = 'forged' WHERE observation_id=%s",
        f"UPDATE {EVENTS} SET outcome = 'replayed' WHERE observation_id=%s",
        f"UPDATE {EVENTS} SET sequence = sequence + 100 WHERE observation_id=%s",
    ):
        with psycopg.connect(web_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, (receipt.observation_id,))
    with psycopg.connect(web_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"TRUNCATE {OBSERVATIONS}, {EVENTS}")
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            f"SELECT count(*) FROM {OBSERVATIONS} WHERE observation_id=%s",
            (receipt.observation_id,),
        ).fetchone() == (1,)


def _web_insertable_columns() -> list[str]:
    with psycopg.connect(DSN) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.column_privileges "
                "WHERE grantee = %s AND table_schema = 'public' AND table_name = %s "
                "AND privilege_type = 'INSERT' ORDER BY column_name",
                (WEB_ROLE, OBSERVATIONS),
            )
        ]


def _forge_from(
    observation_id: UUID, overrides: dict[str, Any]
) -> tuple[sql.Composed, list[Any]]:
    """An INSERT a web member could issue: a copy of an existing record over
    the columns the web may insert, with a fresh identity and ``overrides``."""
    columns = _web_insertable_columns()
    assert columns, "the web role inserts releases through column-level INSERT"
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            sql.SQL("SELECT {} FROM {} WHERE observation_id=%s").format(
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.Identifier(OBSERVATIONS),
            ),
            (observation_id,),
        ).fetchone()
    assert row is not None
    values = dict(zip(columns, row, strict=True))
    values.update(
        {
            key: value
            for key, value in {
                "observation_id": uuid4(),
                "source_release_id": f"forged-{uuid4().hex}",
            }.items()
            if key in values
        }
    )
    values.update(overrides)
    names = list(values)
    params = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in values.values()]
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(OBSERVATIONS),
        sql.SQL(",").join(map(sql.Identifier, names)),
        sql.SQL(",").join(sql.Placeholder() * len(names)),
    )
    return statement, params


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "observing"},
        {"status": "healthy"},
        {"status": "anomalous"},
        {"status": "cancelled"},
        {"status": "unknown"},
    ],
    ids=["observing", "healthy", "anomalous", "cancelled", "unknown-without-late"],
)
def test_the_web_role_cannot_insert_another_initial_state(
    web_dsn, web_release, overrides
) -> None:
    _, receipt = web_release
    statement, params = _forge_from(receipt.observation_id, overrides)
    with psycopg.connect(web_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement, params)


def test_the_web_role_cannot_insert_late_delivery_before_the_deadline(
    web_dsn, web_release
) -> None:
    # P1-07: unknown is an initial state only when the DB confirms expiry.
    _, receipt = web_release
    statement, params = _forge_from(
        receipt.observation_id, {"status": "unknown", "ended_reason": "late_delivery"}
    )
    with psycopg.connect(web_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement, params)


def test_the_storage_layer_maps_the_refusal_to_integrity_refused(
    web_dsn, web_release
) -> None:
    store, receipt = web_release
    statement, params = _forge_from(receipt.observation_id, {"status": "healthy"})
    with pytest.raises(PersistenceError) as raised:
        with store.transaction() as conn:
            conn.execute(statement, params)
    assert raised.value.args[0] == "INTEGRITY_REFUSED"


@pytest.mark.parametrize("role_fixture", ["worker_dsn", "observer_dsn"])
def test_worker_and_observer_cannot_touch_the_new_tables(
    request, web_release, role_fixture
) -> None:
    dsn = request.getfixturevalue(role_fixture)
    _, receipt = web_release
    # P2-09 (F11 PR-2 contract): Observer now reads the release table
    observer = role_fixture == "observer_dsn"
    statements = (
        *(() if observer else ((f"SELECT count(*) FROM {OBSERVATIONS}", ()),)),
        (f"SELECT count(*) FROM {EVENTS}", ()),
        (
            f"UPDATE {OBSERVATIONS} SET status='healthy' WHERE observation_id=%s",
            (receipt.observation_id,),
        ),
        (f"DELETE FROM {EVENTS} WHERE observation_id=%s", (receipt.observation_id,)),
        (f"INSERT INTO {EVENTS} (outcome) VALUES ('created')", ()),
        (f"INSERT INTO {OBSERVATIONS} (status) VALUES ('pending')", ()),
    )
    for statement, args in statements:
        with psycopg.connect(dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, args)
    if observer:
        record = ReleaseStore(dsn, pool=POOL).get_release(receipt.observation_id)
        assert record.observation_id == receipt.observation_id
    else:
        with pytest.raises(PersistenceError):
            ReleaseStore(dsn, pool=POOL).get_release(receipt.observation_id)


# --- storage failures through the write entry (固定语义: PersistenceError) ---


def _fresh_profile() -> tuple[str, str]:
    """A valid profile revision no earlier test stored (only the description
    differs), so a leftover ``opspilot_health_profiles`` row is attributable."""
    profile = HealthProfile.model_validate(
        {
            **PROFILE.model_dump(mode="python"),
            "description": f"{PROFILE.description} [{uuid4().hex}]",
        }
    )
    return profile_revision(profile), canonical_content(profile)


def _write(
    store: ReleaseStore,
    entry: str,
    event: ReleaseEvent,
    target: tuple[UUID, dict[str, str]],
    revision: str,
    content: str,
) -> ReleaseReceipt:
    """Drive one public write entry: the wrapper, or ``record_release_in``
    inside a caller-owned transaction."""
    kwargs: dict[str, Any] = {
        "event": event,
        "target_id": target[0],
        "identity": target[1],
        "health_profile_revision": revision,
        "health_profile": content,
        "min_tracking_seconds": MIN_TRACKING,
        "actor": "release-source:test",
        "raw_body": event.model_dump_json().encode(),
    }
    if entry == "record_release":
        return store.record_release(**kwargs)
    with store.transaction() as conn:
        return store.record_release_in(conn, **kwargs)


def _assert_nothing_written(
    event: ReleaseEvent, target_id: UUID, revision: str
) -> None:
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            f"SELECT count(*) FROM {OBSERVATIONS} WHERE source=%s AND source_release_id=%s",
            (event.source, event.source_release_id),
        ).fetchone() == (0,)
        assert conn.execute(
            f"SELECT count(*) FROM {EVENTS} WHERE source=%s AND source_release_id=%s",
            (event.source, event.source_release_id),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT integration_id,cluster_uid,namespace,workload "
            "FROM opspilot_targets WHERE target_id=%s",
            (target_id,),
        ).fetchone() == (None, None, None, None), "identity completion rolled back"
        assert conn.execute(
            "SELECT count(*) FROM opspilot_health_profiles "
            "WHERE health_profile_revision=%s",
            (revision,),
        ).fetchone() == (0,), "profile row from the failed call rolled back"


@pytest.mark.parametrize("entry", ["record_release", "record_release_in"])
def test_a_lock_timeout_during_the_write_raises_timeout_and_rolls_back(
    owner, releases, entry
) -> None:
    # The audit table is held ACCESS EXCLUSIVE by another connection, so the
    # write must hit the transaction's lock_timeout (4 s) wherever it touches
    # that table; identity completion and the profile row come with it.
    target = _target(owner, complete=False)
    revision, content = _fresh_profile()
    event = _event(released_at=_db_now() - timedelta(minutes=1))

    with psycopg.connect(DSN) as blocker:
        blocker.execute(f"LOCK TABLE {EVENTS} IN ACCESS EXCLUSIVE MODE")
        try:
            with pytest.raises(PersistenceError) as raised:
                _write(releases, entry, event, target, revision, content)
        finally:
            blocker.rollback()

    assert raised.value.args[0] == "TIMEOUT"
    _assert_nothing_written(event, target[0], revision)


@pytest.mark.parametrize("entry", ["record_release", "record_release_in"])
def test_a_privilege_refusal_during_the_write_raises_integrity_refused(
    owner, worker_dsn, entry
) -> None:
    # A worker member has no privilege on the release tables (P1-07): its
    # write is refused with 42501, which the store reports as
    # INTEGRITY_REFUSED (P1-13), never as a business receipt or another code.
    target = _target(owner, complete=False)
    revision, content = _fresh_profile()
    event = _event(released_at=_db_now() - timedelta(minutes=1))

    with pytest.raises(PersistenceError) as raised:
        _write(
            ReleaseStore(worker_dsn, pool=POOL), entry, event, target, revision, content
        )

    assert raised.value.args[0] == "INTEGRITY_REFUSED"
    _assert_nothing_written(event, target[0], revision)


# --- migration 0014 (P1-09) --------------------------------------------------


def _scratch_database() -> tuple[str, str]:
    name = f"opspilot_f11_{uuid4().hex[:12]}"
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
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
        )


def _count_in(dsn: str, table: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_0014_downgrades_only_when_empty_and_keeps_data_otherwise() -> None:
    name, scratch = _scratch_database()
    try:
        schema.migrate(scratch, pg_dump=PG_DUMP)
        before_dump = schema.schema_dump(scratch, pg_dump=PG_DUMP)
        with psycopg.connect(scratch) as conn:
            assert {OBSERVATIONS, EVENTS} <= relations(conn)
            grants_at_head = {
                role: catalog_grants(conn, role) for role in (WEB_ROLE, WORKER_ROLE)
            }

        # empty tables: the downgrade removes 0014 and nothing of 0013
        schema.command.downgrade(schema._config(scratch), BEFORE_0014)
        with psycopg.connect(scratch) as conn:
            assert schema.current_revision(conn) == BEFORE_0014
            remaining = relations(conn)
            assert OBSERVATIONS not in remaining and EVENTS not in remaining
            assert "opspilot_self_heal_authorizations" in remaining
            for role, at_head in grants_at_head.items():
                expected = {
                    table: held
                    for table, held in at_head.items()
                    if table not in (OBSERVATIONS, EVENTS)
                }
                # 0015 adds two relations and exact columns to shared tables;
                # descending through it must revoke those additions as well.
                expected = {
                    table: held
                    for table, held in expected.items()
                    if table
                    not in ("opspilot_release_audit", "opspilot_release_query_results")
                }
                # 0019 adds the two comparison relations; below it they are gone.
                expected = {
                    table: held
                    for table, held in expected.items()
                    if table
                    not in (
                        "opspilot_release_comparisons",
                        "opspilot_release_comparison_queries",
                    )
                }
                if role == WEB_ROLE:
                    removals = {
                        # 0018 grants the web role INSERT on the related
                        # incident column; below it that grant is revoked.
                        "opspilot_incidents": {"related_incident_id"},
                        "opspilot_observation_sessions": {
                            "release_observation_id",
                            "release_policy_revision",
                            "release_budget_used_at_authorization",
                        },
                        "opspilot_observation_endings": {
                            "release_observation_id",
                            "subject_state_before",
                            "subject_state_after",
                            "anomaly_key",
                        },
                    }
                    for table, columns in removals.items():
                        normalized = set()
                        for privilege in expected[table]:
                            if privilege.startswith("INSERT("):
                                remaining_columns = (
                                    set(privilege[7:-1].split(",")) - columns
                                )
                                privilege = (
                                    "INSERT("
                                    + ",".join(sorted(remaining_columns))
                                    + ")"
                                )
                            normalized.add(privilege)
                        expected[table] = frozenset(normalized)
                    # 0017 grants the operator suspension writes; below it
                    # the web role holds the lock-only privileges again.
                    expected.update(
                        opspilot_scope_controls=frozenset(
                            {"SELECT", "UPDATE(scope_id)"}
                        ),
                        opspilot_suspension_audit=frozenset(),
                        opspilot_target_suspensions=frozenset(
                            {"SELECT", "INSERT(target_id)", "UPDATE(updated_at)"}
                        ),
                    )
                assert catalog_grants(conn, role) == expected, role

        schema.command.upgrade(schema._config(scratch), "head")
        assert schema.schema_dump(scratch, pg_dump=PG_DUMP) == before_dump

        # non-empty tables: the downgrade is refused and the data stays
        scratch_owner = DurableStore(scratch, pool=POOL)
        target_id, identity = _target(scratch_owner)
        receipt = _record(
            ReleaseStore(scratch, pool=POOL),
            _event(released_at=_db_now(scratch) - timedelta(minutes=1)),
            target_id,
            identity,
        )
        assert receipt.outcome == "created"
        close_pools()
        with pytest.raises(Exception):
            schema.command.downgrade(schema._config(scratch), BEFORE_0014)
        with psycopg.connect(scratch) as conn:
            # 0015 commits its lossless downgrade first; the 0014 data guard
            # then refuses removing the release records in its own transaction.
            assert schema.current_revision(conn) == "0014_release_observations"
            assert {OBSERVATIONS, EVENTS} <= relations(conn)
        assert _count_in(scratch, OBSERVATIONS) == 1
        assert _count_in(scratch, EVENTS) == 1
    finally:
        _drop_database(name)
