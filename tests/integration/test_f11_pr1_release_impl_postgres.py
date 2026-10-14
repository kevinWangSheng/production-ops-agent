"""Implementation checks for the r2 public release store on the supplied lab."""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import ValidationError

from opspilot.observation.releases import (
    ReleaseAuxiliary,
    ReleaseEvent,
    ReleaseRevision,
    ReleaseStore,
)
from opspilot.observer.health_profile import canonical_content, load_health_profile
from opspilot.persistence import DurableStore, PersistenceError
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
PROFILE = load_health_profile(
    Path("opspilot/observer/profiles/otel-demo-checkout.json")
)
DIGEST = "sha256:" + "a" * 64


@pytest.fixture(scope="module")
def store():
    with login(DSN, "opspilot_web") as dsn:
        result = ReleaseStore(dsn)
        try:
            yield result
        finally:
            result.close()


@pytest.fixture
def request_data():
    uid = "f11-" + uuid4().hex
    target_id = DurableStore(DSN).register_target(uid)
    return dict(
        event=ReleaseEvent(
            format_version=1,
            source="impl-" + uuid4().hex,
            source_release_id=uuid4().hex,
            released_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            before=ReleaseRevision(images={"app": DIGEST}, pod_template_digest=DIGEST),
            after=ReleaseRevision(
                images={"app": "sha256:" + "b" * 64}, pod_template_digest=DIGEST
            ),
        ),
        target_id=target_id,
        identity=dict(
            integration_id="impl",
            cluster_uid="lab",
            resource_uid=uid,
            namespace=PROFILE.subject.kubernetes_namespace,
            workload=PROFILE.subject.service,
        ),
        health_profile_revision=PROFILE.revision,
        health_profile=canonical_content(PROFILE),
        min_tracking_seconds=1200,
        actor="impl-test",
        raw_body=b'{"password":"private-value","note":"delivery"}',
    )


def test_create_replay_conflict_and_frozen_policy(store, request_data):
    first = store.record_release(**request_data)
    assert (first.outcome, first.status, first.reason) == ("created", "pending", None)
    record = store.get_release(first.observation_id)
    assert record.deadline_at == request_data["event"].released_at + timedelta(
        seconds=3600
    )
    assert record.target_snapshot == request_data["identity"]
    assert record.health_profile_revision == PROFILE.revision
    assert (
        record.sample_interval_seconds,
        record.sustained_window_seconds,
        record.max_samples,
    ) == (60, 600, 40)
    replay = store.record_release(
        **{
            **request_data,
            "min_tracking_seconds": 9999,
            "health_profile_revision": "obsolete",
            "health_profile": "obsolete",
            "raw_body": b"different transmission",
        }
    )
    assert (replay.outcome, replay.observation_id) == ("replayed", first.observation_id)
    changed = request_data["event"].model_copy(
        update={"auxiliary": ReleaseAuxiliary(deployment_generation=2)}
    )
    conflict = store.record_release(**{**request_data, "event": changed})
    assert (conflict.outcome, conflict.reason, conflict.observation_id) == (
        "rejected",
        "RELEASE_IDENTITY_CONFLICT",
        first.observation_id,
    )
    assert store.get_release(first.observation_id) == record
    events = store.release_events(first.observation_id)
    assert [e.sequence for e in events] == [1, 2, 3]
    assert [e.outcome for e in events] == ["created", "replayed", "rejected"]
    assert events[0].raw_sha256 == hashlib.sha256(request_data["raw_body"]).hexdigest()
    assert "private-value" not in events[0].audit_text
    assert store.release_events(first.observation_id, after=1) == events[1:]
    assert store.find_release(record.source, record.source_release_id) == record
    assert store.find_release("missing", "missing") is None
    with pytest.raises(PersistenceError, match="NOT_FOUND"):
        store.get_release(uuid4())


@pytest.mark.parametrize(
    "reason",
    [
        "RELEASE_TIME_IN_FUTURE",
        "INVALID_RELEASE_POLICY",
        "TARGET_MISMATCH",
        "TARGET_IDENTITY_MISSING",
        "HEALTH_PROFILE_TARGET_MISMATCH",
    ],
)
def test_business_refusal_commits_only_one_audit(store, request_data, reason):
    event = request_data["event"]
    if reason == "RELEASE_TIME_IN_FUTURE":
        request_data["event"] = event.model_copy(
            update={"released_at": datetime.now(timezone.utc) + timedelta(hours=1)}
        )
    elif reason == "INVALID_RELEASE_POLICY":
        request_data["min_tracking_seconds"] = 3600
    elif reason == "TARGET_MISMATCH":
        request_data["identity"]["resource_uid"] = "different"
    elif reason == "TARGET_IDENTITY_MISSING":
        request_data["identity"] = None
    else:
        request_data["identity"]["workload"] = "payment"
    receipt = store.record_release(**request_data)
    assert (
        receipt.outcome,
        receipt.reason,
        receipt.observation_id,
        receipt.status,
    ) == ("rejected", reason, None, None)
    assert store.find_release(event.source, event.source_release_id) is None
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT outcome,reason,observation_id,sequence FROM opspilot_release_events WHERE source=%s",
            (event.source,),
        ).fetchall()
        assert rows == [
            dict(outcome="rejected", reason=reason, observation_id=None, sequence=None)
        ]
        assert (
            conn.execute(
                "SELECT integration_id FROM opspilot_targets WHERE target_id=%s",
                (request_data["target_id"],),
            ).fetchone()["integration_id"]
            is None
        )


def test_late_delivery_and_utf8_bounded_redaction(store, request_data):
    request_data["event"] = request_data["event"].model_copy(
        update={"released_at": datetime.now(timezone.utc) - timedelta(hours=2)}
    )
    request_data["raw_body"] = b"token=hidden " + ("界" * 10000).encode() + b"\xff"
    receipt = store.record_release(**request_data)
    assert (receipt.outcome, receipt.status, receipt.reason) == (
        "created",
        "unknown",
        "late_delivery",
    )
    record = store.get_release(receipt.observation_id)
    assert record.received_at >= record.deadline_at
    assert record.ended_at == record.received_at
    [audit] = store.release_events(receipt.observation_id)
    assert audit.truncated and len(audit.audit_text.encode()) <= 16384
    assert "hidden" not in audit.audit_text


def test_nul_in_raw_body_is_replaced_in_audit_text(store, request_data):
    request_data["raw_body"] = b"before\x00after"
    receipt = store.record_release(**request_data)
    assert receipt.outcome == "created"
    [audit] = store.release_events(receipt.observation_id)
    assert audit.audit_text == "before\ufffdafter"
    assert audit.raw_sha256.endswith(hashlib.sha256(b"before\x00after").hexdigest())


def test_caller_transaction_rolls_back_subject_binding_and_audit(store, request_data):
    with pytest.raises(RuntimeError, match="abort composition"):
        with store.transaction() as conn:
            receipt = store.record_release_in(conn, **request_data)
            assert receipt.outcome == "created"
            raise RuntimeError("abort composition")
    event = request_data["event"]
    assert store.find_release(event.source, event.source_release_id) is None
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_release_events WHERE source=%s",
            (event.source,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT integration_id FROM opspilot_targets WHERE target_id=%s",
            (request_data["target_id"],),
        ).fetchone() == (None,)


def test_concurrent_first_delivery_and_sequence(store, request_data):
    barrier = Barrier(3)

    def deliver():
        barrier.wait()
        return store.record_release(**request_data)

    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(lambda _: deliver(), range(3)))
    assert sorted(r.outcome for r in receipts) == ["created", "replayed", "replayed"]
    assert len({r.observation_id for r in receipts}) == 1
    assert [e.sequence for e in store.release_events(receipts[0].observation_id)] == [
        1,
        2,
        3,
    ]


@pytest.mark.parametrize(
    "status,ended_reason",
    [
        ("healthy", None),
        ("observing", None),
        ("unknown", "late_delivery"),
        ("unknown", None),
    ],
)
def test_initial_state_guard_refuses_direct_sql(
    store, request_data, status, ended_reason
):
    receipt = store.record_release(**request_data)
    with store.transaction() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO opspilot_release_observations(observation_id,format_version,source,source_release_id,target_id,target_snapshot,before_revision,after_revision,auxiliary,semantic_sha256,health_profile_revision,timing_policy_version,released_at,deadline_at,deadline_seconds,min_tracking_seconds,sample_interval_seconds,sustained_window_seconds,max_samples,status,ended_reason) "
                    "SELECT %s,format_version,source,%s,target_id,target_snapshot,before_revision,after_revision,auxiliary,semantic_sha256,health_profile_revision,timing_policy_version,released_at,deadline_at,deadline_seconds,min_tracking_seconds,sample_interval_seconds,sustained_window_seconds,max_samples,%s,%s FROM opspilot_release_observations WHERE observation_id=%s",
                    (
                        uuid4(),
                        uuid4().hex,
                        status,
                        ended_reason,
                        receipt.observation_id,
                    ),
                )


@pytest.mark.parametrize(
    "group", ["opspilot_web", "opspilot_worker", "opspilot_observer"]
)
def test_role_and_immutable_boundaries(store, request_data, group):
    receipt = store.record_release(**request_data)
    with login(DSN, group) as dsn:
        with psycopg.connect(dsn) as conn:
            statements = [
                "UPDATE opspilot_release_observations SET status='healthy'",
                "UPDATE opspilot_release_events SET actor='forged'",
                "DELETE FROM opspilot_release_events",
                "TRUNCATE opspilot_release_observations",
            ]
            if group == "opspilot_worker":
                # P2-09 (F11 PR-2 contract): Observer now reads the release table
                statements.append(
                    "SELECT observation_id FROM opspilot_release_observations"
                )
            if group != "opspilot_web":
                statements += [
                    "SELECT event_id FROM opspilot_release_events",
                    "INSERT INTO opspilot_release_events(actor) VALUES('forged')",
                ]
            for statement in statements:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn.transaction():
                        conn.execute(statement)
    assert store.get_release(receipt.observation_id).status == "pending"


def test_storage_refusal_maps_42501_and_rolls_back(request_data):
    with login(DSN, "opspilot_worker") as dsn:
        worker = ReleaseStore(dsn)
        try:
            with pytest.raises(PersistenceError, match="INTEGRITY_REFUSED"):
                worker.record_release(**request_data)
        finally:
            worker.close()
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_release_events WHERE source=%s",
            (request_data["event"].source,),
        ).fetchone() == (0,)


def test_nonempty_downgrade_is_refused_without_changing_schema(
    store, request_data, monkeypatch
):
    store.record_release(**request_data)
    path = Path("opspilot/migrations/versions/0014_release_observations.py")
    spec = spec_from_file_location("release_migration", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    with psycopg.connect(DSN) as conn:
        with pytest.raises(
            psycopg.errors.RaiseException, match="ReleaseRecordsWouldBeLost"
        ):
            with conn.transaction():
                monkeypatch.setattr(module.op, "execute", conn.execute)
                module.downgrade()
        assert conn.execute(
            "SELECT to_regclass('opspilot_release_events')"
        ).fetchone() == ("opspilot_release_events",)


def test_invalid_dto_never_reaches_storage(store, request_data):
    event = request_data["event"]
    with pytest.raises(ValidationError):
        ReleaseRevision(images={}, pod_template_digest=DIGEST)
    with pytest.raises(ValidationError):
        ReleaseRevision(images={"app": "tag:latest"}, pod_template_digest=DIGEST)
    with pytest.raises(ValidationError):
        ReleaseAuxiliary(deployment_generation=-1)
    event.after.images["app"] = "tag:latest"
    with pytest.raises(ValidationError):
        store.record_release(**request_data)
    assert store.find_release(event.source, event.source_release_id) is None


def test_deadline_crossed_while_waiting_for_identity_is_late(store, request_data):
    request_data["event"] = request_data["event"].model_copy(
        update={"released_at": datetime.now(timezone.utc) - timedelta(seconds=3599.8)}
    )
    with psycopg.connect(DSN) as owner:
        owner.execute(
            "SELECT target_id FROM opspilot_targets WHERE target_id=%s FOR UPDATE",
            (request_data["target_id"],),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(store.record_release, **request_data)
            time.sleep(0.3)
            owner.commit()
            receipt = future.result(timeout=5)
    assert (receipt.outcome, receipt.status, receipt.reason) == (
        "created",
        "unknown",
        "late_delivery",
    )


def test_deadline_crossed_between_clock_read_and_insert_retries_late(
    store, request_data, monkeypatch
):
    request_data["event"] = request_data["event"].model_copy(
        update={"released_at": datetime.now(timezone.utc) - timedelta(seconds=3599)}
    )
    original = store._db_now
    reads = 0

    def delayed_clock(conn):
        nonlocal reads
        reads += 1
        now = original(conn)
        if reads == 2:
            # Force the actual INSERT trigger's clock across the deadline,
            # after the store has selected the pending initialization.
            conn.execute("SELECT pg_sleep(1.1)")
        return now

    monkeypatch.setattr(store, "_db_now", delayed_clock)
    receipt = store.record_release(**request_data)
    assert reads >= 4
    assert (receipt.outcome, receipt.status, receipt.reason) == (
        "created",
        "unknown",
        "late_delivery",
    )
    [audit] = store.release_events(receipt.observation_id)
    assert audit.outcome == "created" and audit.sequence == 1


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing_none", "HEALTH_PROFILE_UNAVAILABLE"),
        ("missing_empty", "HEALTH_PROFILE_UNAVAILABLE"),
        ("content_not_text", "INVALID_RELEASE_POLICY"),
        ("malformed_json", "INVALID_RELEASE_POLICY"),
        ("non_object", "INVALID_RELEASE_POLICY"),
        ("missing_subject", "INVALID_RELEASE_POLICY"),
        ("invalid_session", "INVALID_RELEASE_POLICY"),
        ("revision_none", "INVALID_RELEASE_POLICY"),
        ("revision_empty", "INVALID_RELEASE_POLICY"),
        ("revision_hash_mismatch", "INVALID_RELEASE_POLICY"),
        ("target_mismatch", "HEALTH_PROFILE_TARGET_MISMATCH"),
    ],
)
def test_profile_refusal_codes_leave_one_audit_and_no_binding(
    store, request_data, case, reason
):
    if case == "missing_none":
        request_data["health_profile"] = None
    elif case == "missing_empty":
        request_data["health_profile"] = ""
    elif case == "content_not_text":
        request_data["health_profile"] = 42
    elif case == "malformed_json":
        request_data["health_profile"] = "{"
    elif case == "non_object":
        request_data["health_profile"] = "[]"
    elif case == "missing_subject":
        request_data["health_profile"] = "{}"
    elif case == "invalid_session":
        content = json.loads(request_data["health_profile"])
        content["session"]["max_samples"] = 0
        request_data["health_profile"] = json.dumps(content)
    elif case == "revision_none":
        request_data["health_profile_revision"] = None
    elif case == "revision_empty":
        request_data["health_profile_revision"] = ""
    elif case == "revision_hash_mismatch":
        request_data["health_profile_revision"] = "otel-demo-checkout@000000000000"
    else:
        request_data["identity"]["workload"] = "payment"
    receipt = store.record_release(**request_data)
    assert (
        receipt.outcome,
        receipt.reason,
        receipt.observation_id,
        receipt.status,
    ) == ("rejected", reason, None, None)
    event = request_data["event"]
    assert store.find_release(event.source, event.source_release_id) is None
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        assert conn.execute(
            "SELECT event_id,outcome,reason,observation_id,sequence FROM opspilot_release_events WHERE source=%s AND source_release_id=%s",
            (event.source, event.source_release_id),
        ).fetchall() == [
            dict(
                event_id=receipt.event_id,
                outcome="rejected",
                reason=reason,
                observation_id=None,
                sequence=None,
            )
        ]
        assert conn.execute(
            "SELECT integration_id,cluster_uid,namespace,workload FROM opspilot_targets WHERE target_id=%s",
            (request_data["target_id"],),
        ).fetchone() == dict(
            integration_id=None, cluster_uid=None, namespace=None, workload=None
        )


@pytest.mark.parametrize("first_identity_is_none", [True, False])
def test_registered_identity_replays_with_none_or_dictionary(
    store, request_data, first_identity_is_none
):
    # Both forms refer to this same, already complete registry entry.
    identity = request_data["identity"]
    with store.transaction() as conn:
        store._complete_identity(conn, request_data["target_id"], identity)
    first = store.record_release(
        **{**request_data, "identity": None if first_identity_is_none else identity}
    )
    frozen = store.get_release(first.observation_id)
    replay = store.record_release(
        **{**request_data, "identity": identity if first_identity_is_none else None}
    )
    assert (first.outcome, replay.outcome, replay.reason) == (
        "created",
        "replayed",
        None,
    )
    assert replay.observation_id == first.observation_id
    assert store.get_release(first.observation_id) == frozen
    assert frozen.target_snapshot == identity
    events = store.release_events(first.observation_id)
    assert [event.sequence for event in events] == [1, 2]
    assert [event.outcome for event in events] == ["created", "replayed"]
    assert [event.semantic_sha256 for event in events] == [frozen.semantic_sha256] * 2


@pytest.mark.parametrize("revision", ["before", "after"])
@pytest.mark.parametrize(
    "invalid",
    ["\x00", "\ud800", "\udfff"],
    ids=["nul", "high-surrogate", "low-surrogate"],
)
def test_mutated_image_keys_are_validation_errors_without_writes(
    store, request_data, revision, invalid
):
    event = request_data["event"]
    getattr(event, revision).images["app" + invalid] = DIGEST
    with pytest.raises(ValidationError):
        store.record_release(**request_data)
    assert store.find_release(event.source, event.source_release_id) is None
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_release_events WHERE source=%s AND source_release_id=%s",
            (event.source, event.source_release_id),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT integration_id,cluster_uid,namespace,workload FROM opspilot_targets WHERE target_id=%s",
            (request_data["target_id"],),
        ).fetchone() == (None, None, None, None)
