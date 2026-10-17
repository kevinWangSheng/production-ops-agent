"""PR-2a implementation checks using raw fixtures and non-owner roles."""

import copy
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import ValidationError

from opspilot.domain.evidence import QueryWindow
from opspilot.observation import release_policy
from opspilot.observation.release_policy import (
    ReleasePolicy,
    canonical_json,
    sha256,
)
from opspilot.observation.release_types import (
    ReleaseInstance,
    ReleaseQueryResult,
    ReleaseSampleEvidence,
)
from opspilot.observation.releases import (
    ReleaseAuxiliary,
    ReleaseEvent,
    ReleaseRevision,
    ReleaseStore,
)
from opspilot.observation.store import ObservationStore
from opspilot.observer.health_profile import (
    HealthProfile,
    canonical_content,
    load_health_profile,
)
from opspilot.observer.release_evaluation import (
    build_release_query_plan,
    evaluate_release_sample,
)
from opspilot.observer.release_replay import replay_release_history
from opspilot.persistence import DurableStore, PersistenceError
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
DIGEST = "sha256:" + "a" * 64
NEW_DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def harness(tmp_path, monkeypatch, request):
    profile = load_health_profile(
        release_policy.Path("opspilot/observer/profiles/otel-demo-checkout.json")
    )
    content = profile.model_dump(mode="json")
    content.update(evaluation_window_seconds=2, query_timeout_seconds=1)
    content["session"].update(sample_interval_seconds=1, sustained_window_seconds=1)
    if hasattr(request, "param"):
        content["session"]["max_samples"] = request.param
    for sig in content["signals"]:
        for key in ("query", "coverage_query", "freshness_query"):
            sig[key] = re.sub(r"\[5m\]", "[2s]", sig[key])
    profile = HealthProfile.model_validate_json(json.dumps(content))
    resource_uid = "pr2a-" + uuid4().hex
    target = DurableStore(DSN).register_target(resource_uid)
    identity = {
        "integration_id": "impl",
        "cluster_uid": "lab",
        "namespace": profile.subject.kubernetes_namespace,
        "resource_uid": resource_uid,
        "workload": profile.subject.service,
    }
    policy_content = {
        "format_version": 1,
        "target": identity,
        "health_profile_revision": profile.revision,
        "health_profile_sha256": sha256(canonical_content(profile).encode()),
        "min_tracking_seconds": 2,
        "evaluator": "release-after-v1",
        "mapping": "pod-uid-replicaset-image-v1",
        "evidence_path": "docs/evidence/test-only/run.md",
        "evidence_sha256": "c" * 64,
        "approval_ref": "test-only",
        "approved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    digest = sha256(canonical_json(policy_content).encode())
    policy = ReleasePolicy.model_validate_json(
        canonical_json(
            {
                "policy_id": "impl",
                "revision": "impl@" + digest[:12],
                "content_sha256": digest,
                "content": policy_content,
            }
        )
    )
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        canonical_json(
            {"format_version": 1, "policies": [policy.model_dump(mode="json")]}
        )
    )
    monkeypatch.setattr(release_policy, "RELEASE_POLICY_CATALOG_PATH", catalog)
    with (
        login(DSN, "opspilot_web") as web_dsn,
        login(DSN, "opspilot_observer") as observer_dsn,
    ):
        web = ReleaseStore(web_dsn)
        observer = ObservationStore(observer_dsn)
        receipt = web.record_release(
            event=ReleaseEvent(
                format_version=1,
                source="pr2a-" + uuid4().hex,
                source_release_id=uuid4().hex,
                released_at=datetime.now(timezone.utc) - timedelta(seconds=10),
                before=ReleaseRevision(
                    images={"app": DIGEST}, pod_template_digest=DIGEST
                ),
                after=ReleaseRevision(
                    images={"app": NEW_DIGEST}, pod_template_digest=NEW_DIGEST
                ),
                auxiliary=ReleaseAuxiliary(
                    before_pod_template_hash="aaa", after_pod_template_hash="bbb"
                ),
            ),
            target_id=target,
            identity=identity,
            health_profile_revision=profile.revision,
            health_profile=canonical_content(profile),
            min_tracking_seconds=2,
            actor="tester",
            raw_body=b"{}",
        )
        assert receipt.outcome == "created", receipt
        oid = receipt.observation_id
        yield web, observer, oid, policy, catalog
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
        web.close()
        observer.close()


def authorize(h):
    web, _, oid, policy, _ = h
    return web.authorize_release(
        oid,
        actor="controller",
        expected_control_generation=0,
        idempotency_key="authorize",
        calibrated_policy_revision=policy.revision,
    )


def force_due(sid):
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() WHERE session_id=%s",
            (sid,),
        )


def claim(h, sid):
    force_due(sid)
    return h[1]._claim_release_due(uuid4(), sid, 120)[0]


def raw_evidence(context, at, *, degraded=False, missing=False):
    instance = ReleaseInstance(
        pod_uid=UUID("12345678-1234-1234-1234-123456789012"),
        pod_name="checkout-bbb-pod",
        replicaset_name="checkout-bbb",
        images={"app": NEW_DIGEST},
    )
    plan = build_release_query_plan(
        context, window_end=at, instance_uids=(instance.pod_uid,)
    )
    results = []
    for key, query in plan.items():
        labels = {}
        value = 1.0
        name = key.split(":")[1]
        kind = key.split(":")[2]
        if key.startswith("mapping:"):
            labels = {"namespace": "otel-demo"}
            if name == "replicaset_owner":
                labels.update(
                    replicaset="checkout-bbb",
                    owner_kind="Deployment",
                    owner_name="checkout",
                )
            else:
                labels.update(uid=str(instance.pod_uid), pod=instance.pod_name)
                if name == "pod_owner":
                    labels.update(owner_kind="ReplicaSet", owner_name="checkout-bbb")
                else:
                    labels.update(container="app", image_id="repo@" + NEW_DIGEST)
        elif kind == "instances":
            labels = {"service_instance_id": str(instance.pod_uid)}
        elif kind == "query":
            signal = context.health_profile.signal(name)
            value = (
                0.1
                if name == "request_rate_per_second"
                else 0.0
                if name
                in (
                    "error_ratio",
                    "dependency_error_ratio",
                    "deployment_unavailable_replicas",
                )
                else 100.0
                if name == "latency_p95_milliseconds"
                else 1.0
            )
            if degraded and name == "error_ratio" and key.startswith("after:"):
                value = 0.5
            assert signal is not None
            if signal.healthy.min is not None and value < signal.healthy.min:
                value = signal.healthy.min
        if kind == "coverage":
            value = 4.0
        if kind == "freshness":
            value = at.timestamp()
        rows = (
            []
            if missing and key == "after:request_rate_per_second:query"
            else [{"metric": labels, "value": [at.timestamp(), str(value)]}]
        )
        raw = canonical_json(
            {"status": "success", "data": {"resultType": "vector", "result": rows}}
        ).encode()
        results.append(
            ReleaseQueryResult(
                query_key=key,
                query=query,
                source="prometheus",
                evaluated_at=at,
                sent_at=at,
                status="ok",
                http_status=200,
                detail="",
                body_complete=True,
                raw=raw,
                raw_sha256=sha256(raw),
            )
        )
    return ReleaseSampleEvidence(
        format_version=1,
        sample_id=uuid4(),
        window=QueryWindow(
            start=at
            - timedelta(seconds=context.health_profile.evaluation_window_seconds),
            end=at,
        ),
        queries=tuple(results),
    )


def test_empty_catalog_rejects_and_replays_first_receipt(harness):
    h = harness
    h[4].write_text('{"format_version":1,"policies":[]}')
    first = authorize(h)
    assert (first.outcome, first.reason, first.session_id) == (
        "rejected",
        "RELEASE_POLICY_UNCALIBRATED",
        None,
    )
    row = h[0].get_release(h[2])
    assert (
        row.status,
        row.control_generation,
        row.observation_generation,
        row.used_sample_count,
    ) == ("pending", 0, 0, 0)
    second = authorize(h)
    assert second.model_copy(update={"replayed": False}) == first
    assert len(h[0].release_audit(h[2])) == 1
    with pytest.raises(PersistenceError, match="IDEMPOTENCY_CONFLICT"):
        h[0].authorize_release(
            h[2],
            actor="other",
            expected_control_generation=0,
            idempotency_key="authorize",
            calibrated_policy_revision=h[3].revision,
        )


def test_commands_pause_resume_cancel_and_late_result(harness):
    h = harness
    a = authorize(h)
    assert a.outcome == "applied"
    lease = claim(h, a.session_id)
    assert h[1].lease_scope_current(lease)
    ctx = h[1].release_context(a.session_id)
    evidence = raw_evidence(ctx, h[1].current_time())
    pause = h[0].control_release(
        h[2],
        action="pause",
        actor="human",
        expected_control_generation=0,
        idempotency_key="pause",
        calibrated_policy_revision=None,
    )
    assert (pause.control_generation, pause.observation_generation, pause.paused) == (
        1,
        2,
        True,
    )
    assert not h[1].lease_scope_current(lease)
    late = h[1].submit_release_sample(lease, evidence)
    assert not late.accepted and late.reason == "control_generation_stale"
    resume = h[0].control_release(
        h[2],
        action="resume",
        actor="human",
        expected_control_generation=1,
        idempotency_key="resume",
        calibrated_policy_revision=h[3].revision,
    )
    assert resume.session_id != a.session_id and resume.control_generation == 2
    cancel = h[0].control_release(
        h[2],
        action="cancel",
        actor="human",
        expected_control_generation=2,
        idempotency_key="cancel",
        calibrated_policy_revision=None,
    )
    assert cancel.status == "cancelled"
    assert (
        h[0]
        .control_release(
            h[2],
            action="pause",
            actor="human",
            expected_control_generation=3,
            idempotency_key="terminal",
            calibrated_policy_revision=None,
        )
        .reason
        == "RELEASE_TERMINAL"
    )
    assert authorize(h).model_copy(update={"replayed": False}) == a
    replay = replay_release_history(h[1].release_history(h[2]))
    assert replay.consistent, replay.integrity


@pytest.mark.parametrize(
    "degraded,missing,expected",
    [(False, False, "healthy"), (True, False, "degraded"), (False, True, "unknown")],
)
def test_raw_mapping_and_evaluation(harness, degraded, missing, expected):
    a = authorize(harness)
    ctx = harness[1].release_context(a.session_id)
    at = ctx.authorized_at + timedelta(seconds=3)
    evaluation = evaluate_release_sample(
        ctx, raw_evidence(ctx, at, degraded=degraded, missing=missing), decision_at=at
    )
    assert evaluation.verdict == expected, evaluation.reason_codes
    assert len(evaluation.instances) == 1


def test_anomaly_atomic_receipt_replay_and_tampering(harness):
    h = harness
    a = authorize(h)
    time.sleep(2.05)
    lease = claim(h, a.session_id)
    ctx = h[1].release_context(a.session_id)
    evidence = raw_evidence(ctx, h[1].current_time(), degraded=True)
    receipt = h[1].submit_release_sample(lease, evidence)
    assert (receipt.accepted, receipt.release_status, receipt.used_sample_count) == (
        True,
        "anomalous",
        1,
    ), receipt
    assert receipt.ending_id == uuid5(h[2], "session-ending:" + str(a.session_id))
    assert h[0].get_release(h[2]).incident_id is None
    assert h[0].pending_anomaly_endings() == h[0].pending_anomaly_endings()
    assert any(
        e.observation_id == h[2] for e in h[0].pending_anomaly_endings(limit=1000)
    )
    assert (
        h[1]
        .submit_release_sample(lease, evidence)
        .model_copy(update={"replayed": False})
        == receipt
    )
    changed = evidence.model_copy(
        update={
            "window": evidence.window.model_copy(
                update={"end": evidence.window.end + timedelta(microseconds=1)}
            )
        }
    )
    with pytest.raises(PersistenceError, match="SAMPLE_IDENTITY_CONFLICT"):
        h[1].submit_release_sample(lease, changed)
    history = h[1].release_history(h[2])
    replay = replay_release_history(history)
    assert replay.consistent, replay.integrity
    assert (
        replay.verdict == "degraded"
        and replay.external_queries == replay.model_requests == ()
    )
    corrupted = copy.deepcopy(history)
    corrupted["samples"][0]["queries"][0]["raw"] = b"changed"
    assert "RAW_SHA256_MISMATCH" in replay_release_history(corrupted).integrity
    assert h[1].session_history(a.session_id)["selected_session_id"] == a.session_id
    assert h[1].replay_session(a.session_id).consistent


def test_unknown_counts_and_query_mismatch_history_only(harness):
    h = harness
    a = authorize(h)
    lease = claim(h, a.session_id)
    ctx = h[1].release_context(a.session_id)
    evidence = raw_evidence(ctx, h[1].current_time())
    receipt = h[1].submit_release_sample(lease, evidence)
    assert (
        receipt.accepted
        and receipt.evaluation.verdict == "unknown"
        and receipt.used_sample_count == 1
    )
    assert receipt.tracking_seconds == receipt.healthy_window_seconds == 0
    lease = claim(h, a.session_id)
    evidence = raw_evidence(ctx, h[1].current_time())
    query = evidence.queries[0].model_copy(update={"query": "vector(1)"})
    evidence = evidence.model_copy(update={"queries": (query, *evidence.queries[1:])})
    receipt = h[1].submit_release_sample(lease, evidence)
    assert (
        not receipt.accepted
        and receipt.reason == "readings_inconsistent"
        and receipt.used_sample_count == 1
    )


def test_authorization_transaction_rollback(harness):
    h = harness
    with pytest.raises(RuntimeError):
        with h[0].transaction() as conn:
            h[0].authorize_release_in(
                conn,
                h[2],
                actor="controller",
                expected_control_generation=0,
                idempotency_key="rollback",
                calibrated_policy_revision=h[3].revision,
            )
            raise RuntimeError("rollback")
    assert h[0].get_release(h[2]).status == "pending"
    assert h[0].release_audit(h[2]) == ()


def test_bare_sql_role_guards(harness):
    h = harness
    with h[1].transaction() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "UPDATE opspilot_release_observations SET status='observing' WHERE observation_id=%s",
                    (h[2],),
                )
    with pytest.raises(PersistenceError, match="INTEGRITY_REFUSED"):
        with h[0].transaction() as conn:
            conn.execute(
                "UPDATE opspilot_release_observations SET paused=true,control_generation=control_generation+1,observation_generation=observation_generation+1 WHERE observation_id=%s",
                (h[2],),
            )
    assert h[0].get_release(h[2]).status == "pending"


def test_command_concurrency_same_receipt(harness):
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: authorize(harness), range(2)))
    assert {r.audit_id for r in receipts} == {receipts[0].audit_id}
    assert sum(r.replayed for r in receipts) == 1


def test_dto_bounds_and_raw_hash(harness):
    a = authorize(harness)
    ctx = harness[1].release_context(a.session_id)
    evidence = raw_evidence(ctx, ctx.authorized_at)
    query = evidence.queries[0]
    with pytest.raises(ValidationError):
        ReleaseSampleEvidence.model_validate(
            {**evidence.model_dump(), "format_version": True}
        )
    with pytest.raises(ValidationError):
        release_policy.ReleasePolicyCatalog.model_validate_json(
            '{"format_version":true,"policies":[]}'
        )
    with pytest.raises(ValidationError):
        ReleaseQueryResult.model_validate(
            {**query.model_dump(), "raw_sha256": "0" * 64}
        )
    with pytest.raises(ValidationError):
        ReleaseSampleEvidence.model_validate(
            {**evidence.model_dump(), "queries": (query, query)}
        )
    with pytest.raises(ValidationError):
        ReleaseSampleEvidence.model_validate(
            {
                **evidence.model_dump(),
                "queries": tuple(
                    query.model_copy(update={"query_key": str(i)}) for i in range(65)
                ),
            }
        )


def test_healthy_requires_a_sustained_window_and_offline_replays(harness):
    h = harness
    a = authorize(h)
    ctx = h[1].release_context(a.session_id)
    time.sleep(2.05)
    lease = claim(h, a.session_id)
    first = h[1].submit_release_sample(lease, raw_evidence(ctx, h[1].current_time()))
    assert (
        first.accepted
        and first.release_status == "observing"
        and first.healthy_window_seconds == 0
    )
    time.sleep(1.05)
    lease = claim(h, a.session_id)
    second = h[1].submit_release_sample(lease, raw_evidence(ctx, h[1].current_time()))
    assert second.release_status == "healthy" and second.used_sample_count == 2
    assert second.tracking_seconds >= 2 and second.healthy_window_seconds >= 1
    replay = replay_release_history(h[1].release_history(h[2]))
    assert replay.consistent, replay.integrity
    assert replay.verdict == "healthy"
    corrupted = copy.deepcopy(h[1].release_history(h[2]))
    corrupted["samples"][0]["release_receipt"]["tracking_seconds"] = 12345.0
    corrupted["samples"][0]["release_receipt"]["healthy_window_seconds"] = 12345.0
    assert "SAMPLE_DECISION_MISMATCH" in replay_release_history(corrupted).integrity


def test_scope_change_revokes_session_without_resetting_budget(harness):
    h = harness
    a = authorize(h)
    lease = claim(h, a.session_id)
    ctx = h[1].release_context(a.session_id)
    evidence = raw_evidence(ctx, h[1].current_time())
    row = h[0].get_release(h[2])
    owner = DurableStore(DSN)
    owner.set_target_suspension(
        row.target_id, True, expected_generation=0, actor="tester"
    )
    owner.set_target_suspension(
        row.target_id, False, expected_generation=1, actor="tester"
    )
    assert not h[1].lease_scope_current(lease)
    receipt = h[1].submit_release_sample(lease, evidence)
    assert receipt.reason == "suspended" and receipt.session_state == "revoked"
    resumed = h[0].control_release(
        h[2],
        action="resume",
        actor="tester",
        expected_control_generation=0,
        idempotency_key="scope-resume",
        calibrated_policy_revision=h[3].revision,
    )
    assert (
        resumed.outcome == "applied"
        and resumed.session_id != a.session_id
        and resumed.used_sample_count == 0
    )
    assert resumed.deadline_at == row.deadline_at


def test_pending_deadline_sweeper_without_session(harness):
    h = harness
    row = h[0].get_release(h[2])
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        profile = conn.execute(
            "SELECT content FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (row.health_profile_revision,),
        ).fetchone()["content"]
    receipt = h[0].record_release(
        event=ReleaseEvent(
            format_version=1,
            source="deadline-" + uuid4().hex,
            source_release_id=uuid4().hex,
            released_at=h[1].current_time()
            - timedelta(seconds=row.deadline_seconds - 0.2),
            before=row.before_revision,
            after=row.after_revision,
            auxiliary=row.auxiliary,
        ),
        target_id=row.target_id,
        identity=row.target_snapshot,
        health_profile_revision=row.health_profile_revision,
        health_profile=profile,
        min_tracking_seconds=row.min_tracking_seconds,
        actor="tester",
        raw_body=b"{}",
    )
    assert receipt.status == "pending"
    time.sleep(0.25)
    assert receipt.observation_id in h[1].sweep_expired_releases(limit=10000)
    assert receipt.observation_id not in h[1].sweep_expired_releases(limit=10000)
    record = h[0].get_release(receipt.observation_id)
    assert record.status == "unknown" and record.ended_reason == "deadline_expired"
    audit = h[0].release_audit(receipt.observation_id)
    assert (
        audit[0]["idempotency_key"] == "system:deadline"
        and audit[0]["session_id"] is None
    )
    replay = replay_release_history(h[1].release_history(receipt.observation_id))
    assert replay.consistent, replay.integrity


@pytest.mark.parametrize("harness", [2], indirect=True)
def test_budget_survives_pause_resume_and_unknown_ends_once(harness):
    h = harness
    a = authorize(h)
    ctx = h[1].release_context(a.session_id)
    lease = claim(h, a.session_id)
    first = h[1].submit_release_sample(lease, raw_evidence(ctx, h[1].current_time()))
    assert first.used_sample_count == 1 and first.release_status == "observing"
    h[0].control_release(
        h[2],
        action="pause",
        actor="human",
        expected_control_generation=0,
        idempotency_key="budget-pause",
        calibrated_policy_revision=None,
    )
    resumed = h[0].control_release(
        h[2],
        action="resume",
        actor="human",
        expected_control_generation=1,
        idempotency_key="budget-resume",
        calibrated_policy_revision=h[3].revision,
    )
    lease = claim(h, resumed.session_id)
    ctx = h[1].release_context(resumed.session_id)
    second = h[1].submit_release_sample(lease, raw_evidence(ctx, h[1].current_time()))
    assert (
        second.used_sample_count == 2
        and second.release_status == "unknown"
        and second.session_state == "expired"
    )
    assert h[0].get_release(h[2]).ended_reason == "max_samples_exhausted"
    replay = replay_release_history(h[1].release_history(h[2]))
    assert replay.consistent, replay.integrity


def test_submit_failure_rolls_back_evidence_watermark_and_ending(harness, monkeypatch):
    h = harness
    a = authorize(h)
    time.sleep(2.05)
    lease = claim(h, a.session_id)
    ctx = h[1].release_context(a.session_id)
    evidence = raw_evidence(ctx, h[1].current_time(), degraded=True)

    def fail(*args, **kwargs):
        raise RuntimeError("injected-ending-failure")

    monkeypatch.setattr(
        "opspilot.observation.release_samples.end_release_session", fail
    )
    with pytest.raises(RuntimeError, match="injected-ending-failure"):
        h[1].submit_release_sample(lease, evidence)
    history = h[1].release_history(h[2])
    assert history["samples"] == [] and history["endings"] == []
    assert (
        history["release"].used_sample_count == 0
        and history["release"].status == "observing"
    )
    assert history["sessions"][0]["adopted_count"] == 0
    assert h[1].lease_scope_current(lease)


def test_migration_roundtrip_backfills_legacy_subject_and_refuses_unbound():
    from alembic import command
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from opspilot import schema

    name = "opspilot_pr2a_impl_" + uuid4().hex[:12]
    dsn = make_conninfo(DSN, dbname=name)
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
        try:
            schema.upgrade_to(dsn, "0014_release_observations")
            target, incident, sid = uuid4(), uuid4(), uuid4()
            with psycopg.connect(dsn) as conn:
                conn.execute(
                    "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,%s)",
                    (target, "migration-" + str(target)),
                )
                conn.execute(
                    "INSERT INTO opspilot_incidents(incident_id,intake_key,state,target_id) VALUES(%s,%s,'running',%s)",
                    (incident, str(incident), target),
                )
                conn.execute(
                    "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,subject_control_generation,observation_generation,authorized_by,authorized_global_generation,authorized_target_generation,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds) VALUES(%s,%s,'incident_recovery',%s,'{}',0,1,'legacy',0,0,clock_timestamp()+interval '1 hour',40,60,600)",
                    (sid, incident, target),
                )
            pg_dump = os.environ["OPSPILOT_PG_DUMP"]
            before = schema.schema_dump(dsn, pg_dump=pg_dump)
            schema.upgrade_head(dsn)
            with psycopg.connect(dsn) as conn:
                row = conn.execute(
                    "SELECT incident_id,subject_kind,subject_id,state,authorization_source FROM opspilot_observation_sessions WHERE session_id=%s",
                    (sid,),
                ).fetchone()
                assert row == (
                    incident,
                    "incident",
                    incident,
                    "authorized",
                    "post_remediation",
                )
            command.downgrade(schema._config(dsn), "0014_release_observations")
            assert schema.schema_dump(dsn, pg_dump=pg_dump) == before
            with psycopg.connect(dsn) as conn:
                conn.execute(
                    "UPDATE opspilot_observation_sessions SET purpose='release_observation',authorization_source=NULL WHERE session_id=%s",
                    (sid,),
                )
            with pytest.raises(Exception, match="LEGACY_RELEASE_SESSION_UNBOUND"):
                schema.upgrade_head(dsn)
            with psycopg.connect(dsn) as conn:
                assert schema.current_revision(conn) == "0014_release_observations"
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def test_web_sample_submission_refusal_is_integrity_and_rolls_back(harness):
    h = harness
    a = authorize(h)
    lease = claim(h, a.session_id)
    ctx = h[1].release_context(a.session_id)
    unauthorized = ObservationStore(h[0].dsn)
    try:
        with pytest.raises(PersistenceError, match="INTEGRITY_REFUSED"):
            unauthorized.submit_release_sample(
                lease, raw_evidence(ctx, h[1].current_time())
            )
    finally:
        unauthorized.close()
    assert h[1].release_history(h[2])["samples"] == []
    assert h[1].lease_scope_current(lease)


def test_concurrent_sample_id_collision_across_subjects_is_identity_conflict(harness):
    h = harness
    first = authorize(h)
    original = h[0].get_release(h[2])
    profile = canonical_content(h[1].release_context(first.session_id).health_profile)
    registered = h[0].record_release(
        event=ReleaseEvent(
            format_version=1,
            source=original.source,
            source_release_id=uuid4().hex,
            released_at=original.released_at,
            before=original.before_revision,
            after=original.after_revision,
            auxiliary=original.auxiliary,
        ),
        target_id=original.target_id,
        identity=original.target_snapshot,
        health_profile_revision=original.health_profile_revision,
        health_profile=profile,
        min_tracking_seconds=original.min_tracking_seconds,
        actor="tester",
        raw_body=b"{}",
    )
    second = h[0].authorize_release(
        registered.observation_id,
        actor="controller",
        expected_control_generation=0,
        idempotency_key="second-authorize",
        calibrated_policy_revision=h[3].revision,
    )
    leases = (claim(h, first.session_id), claim(h, second.session_id))
    at = h[1].current_time()
    sample_id = uuid4()
    evidence = tuple(
        raw_evidence(h[1].release_context(a.session_id), at).model_copy(
            update={"sample_id": sample_id}
        )
        for a in (first, second)
    )

    def submit(index):
        try:
            return h[1].submit_release_sample(leases[index], evidence[index])
        except PersistenceError as exc:
            return str(exc)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (0, 1)))
        assert sum(result == "SAMPLE_IDENTITY_CONFLICT" for result in results) == 1
        assert sum(getattr(result, "accepted", False) for result in results) == 1
    finally:
        row = h[0].get_release(registered.observation_id)
        h[0].control_release(
            registered.observation_id,
            action="cancel",
            actor="cleanup",
            expected_control_generation=row.control_generation,
            idempotency_key="collision-cleanup",
            calibrated_policy_revision=None,
        )


def test_public_claim_shares_limit_and_skips_a_locked_release(harness):
    from opspilot.observation.release_types import ReleaseSampleLease
    from opspilot.observation.store import SampleLease
    from tests.integration.test_m1_02_observation_store_postgres import (
        _authorize,
        _incident,
    )

    h = harness
    authorized = authorize(h)
    owner = DurableStore(DSN)
    controller = ObservationStore(DSN)
    incident, _, target = _incident(owner)
    incident_session = _authorize(controller, incident, target)
    try:
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET active_sample_due_at='2000-01-01'::timestamptz WHERE session_id IN (%s,%s)",
                (authorized.session_id, incident_session),
            )
        leases = h[1].claim_due_samples(uuid4(), limit=2)
        assert {lease.session_id for lease in leases} == {
            authorized.session_id,
            incident_session,
        }
        assert {type(lease) for lease in leases} == {SampleLease, ReleaseSampleLease}
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET active_sample_owner=NULL,active_sample_lease_until=NULL WHERE session_id IN (%s,%s)",
                (authorized.session_id, incident_session),
            )
        with psycopg.connect(DSN) as blocker:
            blocker.execute(
                "SELECT observation_id FROM opspilot_release_observations WHERE observation_id=%s FOR UPDATE",
                (h[2],),
            )
            leases = h[1].claim_due_samples(uuid4(), limit=1)
            assert len(leases) == 1 and leases[0].session_id == incident_session
    finally:
        controller.revoke_sessions(incident)
        controller.close()
        owner.close()


def test_impl_non_owner_cannot_forge_healthy_window_origins(
    _impl_fresh_budget_database, request
):
    h = request.getfixturevalue("harness")
    authorization = authorize(h)
    context = h[1].release_context(authorization.session_id)
    refused = False
    try:
        with h[1]._release_transaction() as conn:
            identity = conn.execute(
                "SELECT pg_has_role(current_user,'opspilot_observer','MEMBER') AS observer, current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='opspilot_observation_sessions'::regclass) AS non_owner"
            ).fetchone()
            assert identity["observer"] and identity["non_owner"]
            conn.execute(
                "UPDATE opspilot_observation_sessions SET tracking_since=authorized_at,tracking_until=authorized_at,healthy_since=authorized_at WHERE session_id=%s",
                (authorization.session_id,),
            )
    except PersistenceError as error:
        assert isinstance(error.__cause__, psycopg.errors.InsufficientPrivilege)
        assert error.__cause__.sqlstate == "42501"
        refused = True
    time.sleep(2.05)
    lease = claim(h, authorization.session_id)
    receipt = h[1].submit_release_sample(
        lease,
        raw_evidence(context, context.authorized_at + timedelta(seconds=2)),
    )
    assert receipt.accepted and receipt.release_status == "observing", receipt
    assert refused, "watermark forgery committed without an adopted sample"


@pytest.mark.parametrize(
    "assignment",
    [
        "adopted_sequence=1",
        "adopted_count=1",
        "adopted_window_end=clock_timestamp()+interval '1 day'",
        "adopted_sequence=1,adopted_count=1,adopted_window_end=clock_timestamp()+interval '1 day'",
    ],
)
def test_impl_non_owner_cannot_forge_adopted_watermarks(
    _impl_fresh_budget_database, request, assignment
):
    h = request.getfixturevalue("harness")
    authorization = authorize(h)
    with pytest.raises(PersistenceError, match="INTEGRITY_REFUSED") as error:
        with h[1]._release_transaction() as conn:
            identity = conn.execute(
                "SELECT pg_has_role(current_user,'opspilot_observer','MEMBER') AS observer, current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='opspilot_observation_sessions'::regclass) AS non_owner"
            ).fetchone()
            assert identity["observer"] and identity["non_owner"]
            conn.execute(
                "UPDATE opspilot_observation_sessions SET "
                + assignment
                + " WHERE session_id=%s",
                (authorization.session_id,),
            )
    assert isinstance(error.value.__cause__, psycopg.errors.InsufficientPrivilege)
    assert error.value.__cause__.sqlstate == "42501"
    session = h[1].release_history(h[2])["sessions"][0]
    assert session["adopted_sequence"] == session["adopted_count"] == 0
    assert session["adopted_window_end"] is None
    context = h[1].release_context(authorization.session_id)
    receipt = h[1].submit_release_sample(
        claim(h, authorization.session_id),
        raw_evidence(context, h[1].current_time(), missing=True),
    )
    assert receipt.accepted and receipt.used_sample_count == 1


def test_impl_watermark_guard_allows_gap_and_unknown_resets(
    _impl_fresh_budget_database, request
):
    h = request.getfixturevalue("harness")
    authorization = authorize(h)
    context = h[1].release_context(authorization.session_id)
    time.sleep(2.05)
    first_at = context.authorized_at + timedelta(seconds=2)
    first = h[1].submit_release_sample(
        claim(h, authorization.session_id), raw_evidence(context, first_at)
    )
    assert first.accepted and first.release_status == "observing"
    time.sleep(2.05)
    gap_at = context.authorized_at + timedelta(seconds=4, milliseconds=1)
    for missing in (False, True, False):
        receipt = h[1].submit_release_sample(
            claim(h, authorization.session_id),
            raw_evidence(context, gap_at, missing=missing),
        )
        assert receipt.accepted and receipt.release_status == "observing"
        session = h[1].release_history(h[2])["sessions"][0]
        if missing:
            assert session["tracking_since"] is None
            assert session["tracking_until"] is None
            assert session["healthy_since"] is None
        else:
            assert session["tracking_since"] == gap_at - timedelta(seconds=2)
            assert session["tracking_until"] == gap_at
            assert session["healthy_since"] == gap_at
    time.sleep(1.05)
    confirmed = h[1].submit_release_sample(
        claim(h, authorization.session_id),
        raw_evidence(context, gap_at + timedelta(seconds=1)),
    )
    assert confirmed.accepted and confirmed.release_status == "healthy"
    assert replay_release_history(h[1].release_history(h[2])).consistent


@pytest.mark.parametrize("field", ["ended_reason", "ended_at", "ending_time"])
def test_impl_replay_rejects_terminal_metadata_tampering(harness, field):
    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    time.sleep(2.05)
    first = harness[1].submit_release_sample(
        claim(harness, authorization.session_id),
        raw_evidence(context, harness[1].current_time()),
    )
    assert first.release_status == "observing"
    time.sleep(1.05)
    second = harness[1].submit_release_sample(
        claim(harness, authorization.session_id),
        raw_evidence(context, harness[1].current_time()),
    )
    assert second.release_status == "healthy"
    history = harness[1].release_history(harness[2])
    assert replay_release_history(history).consistent
    corrupted = copy.deepcopy(history)
    if field == "ending_time":
        corrupted["endings"][0]["recorded_at"] += timedelta(days=1)
    else:
        value = (
            "max_samples_exhausted"
            if field == "ended_reason"
            else history["release"].ended_at + timedelta(seconds=1)
        )
        corrupted["release"] = history["release"].model_copy(update={field: value})
    replay = replay_release_history(corrupted)
    assert not replay.consistent, replay
    assert replay.verdict == "unknown"


@pytest.mark.parametrize("binding", ["other_sample", "missing"])
def test_impl_replay_rejects_query_sample_binding(harness, binding):
    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    time.sleep(2.05)
    for index in range(2):
        if index:
            time.sleep(1.05)
        receipt = harness[1].submit_release_sample(
            claim(harness, authorization.session_id),
            raw_evidence(context, harness[1].current_time()),
        )
    assert receipt.release_status == "healthy"
    history = harness[1].release_history(harness[2])
    baseline = replay_release_history(history)
    assert baseline.consistent and baseline.verdict == "healthy"
    corrupted = copy.deepcopy(history)
    query = corrupted["samples"][0]["queries"][0]
    if binding == "other_sample":
        query["sample_id"] = corrupted["samples"][1]["sample_id"]
    else:
        del query["sample_id"]
    replay = replay_release_history(corrupted)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "SUBJECT_BINDING_MISMATCH" in replay.integrity


@pytest.mark.parametrize("authorized", [False, True])
def test_impl_replay_cancel_time_is_bound_to_frozen_evidence(harness, authorized):
    if authorized:
        authorize(harness)
    time.sleep(0.025)
    harness[0].control_release(
        harness[2],
        action="cancel",
        actor="tester",
        expected_control_generation=0,
        idempotency_key="terminal-time",
        calibrated_policy_revision=None,
    )
    history = harness[1].release_history(harness[2])
    assert replay_release_history(history).consistent
    previous = max(
        [history["release"].received_at]
        + [audit["recorded_at"] for audit in history["audit"][:-1]]
    )
    original = history["release"].ended_at
    assert original > previous
    corrupted = copy.deepcopy(history)
    corrupted["release"] = history["release"].model_copy(
        update={"ended_at": previous + (original - previous) / 2}
    )
    replay = replay_release_history(corrupted)
    assert not replay.consistent
    assert replay.verdict == "unknown"


@pytest.mark.parametrize("owner_group", ["replicaset_owner", "pod_owner"])
def test_impl_owner_queries_expose_conflicting_owners(harness, owner_group):
    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    at = context.authorized_at + timedelta(seconds=3)
    evidence = raw_evidence(context, at)
    replacements = {}
    for query in evidence.queries:
        if query.query_key.startswith(f"mapping:{owner_group}:"):
            rows = json.loads(query.raw)["data"]["result"]
            rows.append(copy.deepcopy(rows[0]))
            rows[-1]["metric"]["owner_name"] = "conflicting-owner"
            # Model the owner-name filtering on the left side of the plan's
            # set intersection: right-side membership must not hide owners.
            left = query.query.split(" and on(", 1)[0]
            expected = re.search(r'owner_name=("(?:\\.|[^"\\])*")', left)
            if expected is not None:
                owner = json.loads(expected.group(1))
                rows = [row for row in rows if row["metric"]["owner_name"] == owner]
            replacements[query.query_key] = rows
    evaluation = evaluate_release_sample(
        context, _impl_replace_query_vectors(evidence, replacements), decision_at=at
    )
    assert evaluation.verdict == "unknown", evaluation
    assert not evaluation.evidence_sufficient
    assert "VERSION_MAPPING_AMBIGUOUS" in evaluation.reason_codes


@pytest.mark.parametrize(
    "missing",
    [
        "all",
        "context:request_rate_per_second:query",
        "mapping:pod_owner:query",
        "after:request_rate_per_second:query",
    ],
)
def test_impl_query_completeness_follows_r4(harness, missing):
    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    lease = claim(harness, authorization.session_id)
    evidence = raw_evidence(context, harness[1].current_time())
    evidence = evidence.model_copy(
        update={
            "queries": ()
            if missing == "all"
            else tuple(
                query
                for query in evidence.queries
                if query.query_key != missing
                and not (
                    missing.startswith("mapping:")
                    and query.query_key.startswith("after:")
                )
            )
        }
    )
    if missing == "all":
        pure = evaluate_release_sample(
            context, evidence, decision_at=evidence.window.end
        )
        assert pure.verdict == "unknown" and pure.outcome == "no_data"
    receipt = harness[1].submit_release_sample(lease, evidence)
    empty = missing == "all"
    assert receipt.accepted == empty, receipt
    assert receipt.disposition == ("adopted" if empty else "history_only")
    assert receipt.reason == ("adopted" if empty else "readings_inconsistent")
    assert receipt.used_sample_count == int(empty)
    if empty:
        assert receipt.evaluation.verdict == "unknown"
        assert receipt.evaluation.outcome == "no_data"
    history = harness[1].release_history(harness[2])
    assert history["sessions"][0]["adopted_count"] == int(empty)
    assert history["sessions"][0]["adopted_window_end"] == (
        evidence.window.end if empty else None
    )
    assert history["release"].status == "observing"
    assert not history["endings"]
    assert replay_release_history(history).used_sample_count == int(empty)


def test_catalog_hash_covers_the_original_valid_utc_timestamp_text(harness):
    h = harness
    content = h[3].content.model_dump(mode="json")
    content["approved_at"] = content["approved_at"].replace("Z", "+00:00")
    digest = sha256(canonical_json(content).encode())
    entry = {
        "policy_id": "timestamp",
        "revision": "timestamp@" + digest[:12],
        "content_sha256": digest,
        "content": content,
    }
    h[4].write_text(canonical_json({"format_version": 1, "policies": [entry]}))
    policy = release_policy.load_release_policy_catalog().policies[0]
    assert policy.content.model_dump(mode="json") == content
    assert policy.content_sha256 == digest
    receipt = h[0].authorize_release(
        h[2],
        actor="controller",
        expected_control_generation=0,
        idempotency_key="timestamp-authorize",
        calibrated_policy_revision=policy.revision,
    )
    assert receipt.outcome == "applied"
    row = h[0].get_release(h[2])
    assert row.calibrated_policy_content == canonical_json(content)
    assert row.calibrated_policy_sha256 == digest
    assert h[1].release_context(receipt.session_id).policy.content_sha256 == digest


def test_invalid_version_name_and_raw_label_text_cannot_confirm_health(harness):
    h = harness
    a = authorize(h)
    ctx = h[1].release_context(a.session_id)
    at = ctx.authorized_at + timedelta(seconds=3)
    invalid = ctx.model_copy(
        update={
            "release": ctx.release.model_copy(
                update={
                    "auxiliary": ctx.release.auxiliary.model_copy(
                        update={"after_pod_template_hash": "bad..hash"}
                    )
                }
            )
        }
    )
    result = evaluate_release_sample(invalid, raw_evidence(invalid, at), decision_at=at)
    assert (
        result.verdict == "unknown"
        and "VERSION_IDENTITY_MISSING" in result.reason_codes
    )
    evidence = raw_evidence(ctx, at)
    queries = []
    for q in evidence.queries:
        if q.query_key == "mapping:container_info:query":
            body = json.loads(q.raw)
            body["data"]["result"][0]["metric"]["container"] = "app\x00"
            raw = canonical_json(body).encode()
            q = q.model_copy(update={"raw": raw, "raw_sha256": sha256(raw)})
        queries.append(q)
    result = evaluate_release_sample(
        ctx, evidence.model_copy(update={"queries": tuple(queries)}), decision_at=at
    )
    assert (
        result.verdict == "unknown"
        and "QUERY_FAILED:mapping:container_info:query" in result.reason_codes
    )


def test_deadline_crossed_during_catalog_validation_rejects_without_authorizing(
    harness, monkeypatch
):
    h = harness
    record = h[0].get_release(h[2])
    with psycopg.connect(DSN) as conn:
        profile = conn.execute(
            "SELECT content FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (record.health_profile_revision,),
        ).fetchone()[0]
    receipt = h[0].record_release(
        event=ReleaseEvent(
            format_version=1,
            source=record.source,
            source_release_id=uuid4().hex,
            released_at=h[1].current_time()
            - timedelta(seconds=record.deadline_seconds - 0.3),
            before=record.before_revision,
            after=record.after_revision,
            auxiliary=record.auxiliary,
        ),
        target_id=record.target_id,
        identity=record.target_snapshot,
        health_profile_revision=record.health_profile_revision,
        health_profile=profile,
        min_tracking_seconds=record.min_tracking_seconds,
        actor="tester",
        raw_body=b"{}",
    )
    assert receipt.status == "pending"
    from opspilot.observation import release_commands

    original = release_commands.load_release_policy_catalog

    def slow_catalog():
        time.sleep(0.4)
        return original()

    monkeypatch.setattr(release_commands, "load_release_policy_catalog", slow_catalog)
    result = h[0].authorize_release(
        receipt.observation_id,
        actor="controller",
        expected_control_generation=0,
        idempotency_key="late-authorization",
        calibrated_policy_revision=h[3].revision,
    )
    assert (result.outcome, result.reason, result.session_id) == (
        "rejected",
        "RELEASE_DEADLINE_EXPIRED",
        None,
    )
    assert h[0].get_release(receipt.observation_id).status == "pending"
    assert h[1].release_history(receipt.observation_id)["sessions"] == []
    h[0].control_release(
        receipt.observation_id,
        action="cancel",
        actor="cleanup",
        expected_control_generation=0,
        idempotency_key="late-cleanup",
        calibrated_policy_revision=None,
    )


@pytest.mark.parametrize(
    "timestamp", ["2026-10-13\x0000:00:00+00:00", "2026-10-13X00:00:00+00:00"]
)
def test_catalog_rejects_invalid_timestamp_lexemes(harness, timestamp):
    h = harness
    content = h[3].content.model_dump(mode="json")
    content["approved_at"] = timestamp
    digest = sha256(canonical_json(content).encode())
    entry = {
        "policy_id": "bad-time",
        "revision": "bad-time@" + digest[:12],
        "content_sha256": digest,
        "content": content,
    }
    h[4].write_text(canonical_json({"format_version": 1, "policies": [entry]}))
    with pytest.raises(ValidationError):
        release_policy.load_release_policy_catalog()
    receipt = h[0].authorize_release(
        h[2],
        actor="controller",
        expected_control_generation=0,
        idempotency_key="invalid-time",
        calibrated_policy_revision=entry["revision"],
    )
    assert (receipt.outcome, receipt.reason) == (
        "rejected",
        "RELEASE_POLICY_CATALOG_INVALID",
    )
    assert h[1].release_history(h[2])["sessions"] == []


def test_sample_fingerprint_normalizes_offsets_and_replays_postgres_timestamps(harness):
    h = harness
    a = authorize(h)
    lease = claim(h, a.session_id)
    ctx = h[1].release_context(a.session_id)
    at = h[1].current_time().astimezone(timezone.utc)
    evidence = raw_evidence(ctx, at)
    first = h[1].submit_release_sample(lease, evidence)
    shifted = lease.model_copy(
        update={
            "lease_until": lease.lease_until.astimezone(timezone.utc),
            "deadline_at": lease.deadline_at.astimezone(timezone.utc),
        }
    )
    assert (
        h[1]
        .submit_release_sample(shifted, evidence)
        .model_copy(update={"replayed": False})
        == first
    )
    replay = replay_release_history(h[1].release_history(h[2]))
    assert replay.consistent, replay.integrity


def test_structurally_valid_unsupported_profile_returns_unknown(harness):
    h = harness
    a = authorize(h)
    ctx = h[1].release_context(a.session_id)
    evidence = raw_evidence(ctx, ctx.authorized_at + timedelta(seconds=3))
    content = ctx.health_profile.model_dump(mode="json")
    for signal in content["signals"]:
        if signal["name"] == "request_rate_per_second":
            signal["coverage_query"] = signal["query"]
    profile = HealthProfile.model_validate_json(canonical_json(content))
    badctx = ctx.model_copy(update={"health_profile": profile})
    evaluation = evaluate_release_sample(
        badctx, evidence, decision_at=evidence.window.end
    )
    assert (
        evaluation.verdict == "unknown"
        and "HEALTH_PROFILE_INVALID" in evaluation.reason_codes
    )
    assert not evaluation.evidence_sufficient


@pytest.mark.parametrize("spelling", ["max_space", "leading_space", "unexpected"])
def test_impl_after_coverage_normalizes_or_refuses(harness, spelling):
    from opspilot.observer.release_evaluation import supported_profile

    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    content = context.health_profile.model_dump(mode="json")
    for signal in content["signals"]:
        if signal["name"] == "request_rate_per_second":
            original = signal["coverage_query"]
            signal["coverage_query"] = (
                original.replace("max(", "max (")
                if spelling == "max_space"
                else "  " + original
                if spelling == "leading_space"
                else original.replace("max(", "sum(")
            )
    profile = HealthProfile.model_validate_json(canonical_json(content))
    context = context.model_copy(update={"health_profile": profile})
    at = context.authorized_at + timedelta(seconds=3)
    if spelling == "unexpected":
        with pytest.raises(ValueError, match="HEALTH_PROFILE_INVALID"):
            build_release_query_plan(context, window_end=at, instance_uids=(uuid4(),))
        evidence = ReleaseSampleEvidence(
            format_version=1,
            sample_id=uuid4(),
            window=QueryWindow(start=at - timedelta(seconds=2), end=at),
            queries=(),
        )
        assert (
            evaluate_release_sample(context, evidence, decision_at=at).verdict
            == "unknown"
        )
    else:
        assert supported_profile(profile)
        plan = build_release_query_plan(
            context, window_end=at, instance_uids=(uuid4(),)
        )
        coverage = plan["after:request_rate_per_second:coverage"]
        # r8: per-instance max inside, min across instances; the profile's own
        # spelling of max(...) must not leak into the sliced expression.
        assert coverage.startswith("min(max by (service_instance_id,")
        assert coverage.count("max") == 1 and "count_over_time(" in coverage


@pytest.mark.parametrize("purpose", ["incident", "release"])
@pytest.mark.parametrize(
    "blocked", ["target_suspended", "target_generation", "global_generation"]
)
def test_impl_suspended_backlog_does_not_use_probe_budget(
    _impl_fresh_budget_database, request, monkeypatch, purpose, blocked
):
    from tests.integration.test_m1_02_observation_store_postgres import (
        _authorize,
        _incident,
    )

    h = request.getfixturevalue("harness")
    owner = DurableStore(DSN)
    controller = ObservationStore(DSN)
    incidents, releases, heads, targets = [], [], [], set()
    try:
        for index in range(9):
            if purpose == "incident":
                incident, target_id, target = _incident(owner)
                incidents.append(incident)
                heads.append(_authorize(controller, incident, target))
                targets.add(target_id)
            else:
                template = h[0].get_release(h[2])
                if index == 0:
                    oid = h[2]
                else:
                    recorded = h[0].record_release(
                        event=ReleaseEvent(
                            format_version=1,
                            source="blocked-backlog",
                            source_release_id=uuid4().hex,
                            released_at=template.released_at,
                            before=template.before_revision,
                            after=template.after_revision,
                            auxiliary=template.auxiliary,
                        ),
                        target_id=template.target_id,
                        identity=template.target_snapshot,
                        health_profile_revision=template.health_profile_revision,
                        health_profile=canonical_content(
                            h[1].release_context(heads[0]).health_profile
                        ),
                        min_tracking_seconds=template.min_tracking_seconds,
                        actor="tester",
                        raw_body=b"{}",
                    )
                    oid = recorded.observation_id
                releases.append(oid)
                authorization = h[0].authorize_release(
                    oid,
                    actor="controller",
                    expected_control_generation=0,
                    idempotency_key="backlog-authorize",
                    calibrated_policy_revision=h[3].revision,
                )
                heads.append(authorization.session_id)
                targets.add(template.target_id)
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET active_sample_due_at='1900-01-01'::timestamptz WHERE session_id=ANY(%s)",
                (heads,),
            )
            if blocked == "global_generation":
                conn.execute(
                    "UPDATE opspilot_scope_controls SET global_generation=global_generation+1 WHERE scope_id=1"
                )
            else:
                conn.execute(
                    "UPDATE opspilot_target_suspensions SET suspended=true WHERE target_id=ANY(%s)"
                    if blocked == "target_suspended"
                    else "UPDATE opspilot_target_suspensions SET generation=generation+1 WHERE target_id=ANY(%s)",
                    (list(targets),),
                )
        if purpose == "incident":
            ready = authorize(h).session_id
        else:
            incident, _, target = _incident(owner)
            incidents.append(incident)
            ready = _authorize(controller, incident, target)
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET active_sample_due_at='1901-01-01'::timestamptz WHERE session_id=%s",
                (ready,),
            )
        probes = []
        incident_claim = h[1]._claim_incident_due_samples
        release_claim = h[1]._claim_release_due

        def claim_incident(*args, **kwargs):
            probes.append(kwargs["selected_session_id"])
            return incident_claim(*args, **kwargs)

        def claim_release(owner, sid, lease_seconds):
            probes.append(sid)
            return release_claim(owner, sid, lease_seconds)

        monkeypatch.setattr(h[1], "_claim_incident_due_samples", claim_incident)
        monkeypatch.setattr(h[1], "_claim_release_due", claim_release)
        leases = h[1].claim_due_samples(uuid4(), limit=1)
        if blocked != "target_suspended":
            assert leases == []
            assert len(probes) == 8
            with psycopg.connect(DSN, row_factory=dict_row) as conn:
                retired = conn.execute(
                    "SELECT state,ended_reason FROM opspilot_observation_sessions WHERE session_id=ANY(%s)",
                    (heads,),
                ).fetchall()
            assert sum(row["state"] == "revoked" for row in retired) == 8
            probes.clear()
            leases = h[1].claim_due_samples(uuid4(), limit=1)
            assert len(probes) == 2
            assert probes[0] in heads and probes[1] == ready
            with psycopg.connect(DSN) as conn:
                assert conn.execute(
                    "SELECT count(*) FROM opspilot_observation_sessions WHERE session_id=ANY(%s) AND state='revoked' AND ended_reason='scope_suspended'",
                    (heads,),
                ).fetchone()[0] == len(heads)
        else:
            assert probes == [ready]
        assert [lease.session_id for lease in leases] == [ready]
    finally:
        for incident in incidents:
            controller.revoke_sessions(incident)
        for oid in releases:
            row = h[0].get_release(oid)
            if row.status in ("pending", "observing"):
                h[0].control_release(
                    oid,
                    action="cancel",
                    actor="cleanup",
                    expected_control_generation=row.control_generation,
                    idempotency_key="backlog-cleanup",
                    calibrated_policy_revision=None,
                )
        controller.close()
        owner.close()


def test_release_coverage_uses_elapsed_utc_time_across_dst_fold(harness):
    from zoneinfo import ZoneInfo

    from opspilot.observer.release_evaluation import (
        ReleaseEvaluationContext,
        advance_release_window,
    )

    h = harness
    a = authorize(h)
    original = h[1].release_context(a.session_id)
    local = ZoneInfo("America/Los_Angeles")
    authorized = datetime(2026, 11, 1, 1, 59, 57, tzinfo=local, fold=0)
    release = original.release.model_copy(
        update={
            "released_at": authorized,
            "received_at": authorized,
            "deadline_at": authorized.astimezone(timezone.utc)
            + timedelta(seconds=3600),
        }
    )
    context = ReleaseEvaluationContext(
        release=release,
        session_id=original.session_id,
        authorized_at=authorized,
        policy=original.policy,
        health_profile=original.health_profile,
    )
    first = datetime(2026, 11, 1, 1, 59, 59, tzinfo=local, fold=0).astimezone(
        timezone.utc
    )
    second = datetime(2026, 11, 1, 1, 0, 0, tzinfo=local, fold=1).astimezone(
        timezone.utc
    )
    state = {}
    for used, at in enumerate((first, second), 1):
        evidence = raw_evidence(context, at)
        evaluation = evaluate_release_sample(context, evidence, decision_at=at)
        assert evaluation.verdict == "healthy", evaluation.reason_codes
        state, status, reason = advance_release_window(
            context,
            state,
            evaluation,
            window_start=evidence.window.start,
            window_end=evidence.window.end,
            decision_at=at,
            used_sample_count=used,
        )
    assert (status, reason) == ("healthy", "healthy_confirmed")
    assert (state["tracking_until"] - state["tracking_since"]).total_seconds() == 3
    assert (second - state["healthy_since"]).total_seconds() == 1


def _impl_replace_query_vectors(evidence, replacements):
    queries = []
    for query in evidence.queries:
        if query.query_key in replacements:
            body = json.loads(query.raw)
            body["data"]["result"] = replacements[query.query_key]
            raw = canonical_json(body).encode()
            query = query.model_copy(update={"raw": raw, "raw_sha256": sha256(raw)})
        queries.append(query)
    return evidence.model_copy(update={"queries": tuple(queries)})


def test_impl_instances_plan_preserves_span_binding_labels(harness):
    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    at = context.authorized_at + timedelta(seconds=3)
    plan = build_release_query_plan(
        context,
        window_end=at,
        instance_uids=(UUID("12345678-1234-1234-1234-123456789012"),),
    )
    instance_queries = [
        query for key, query in plan.items() if key.endswith(":instances")
    ]
    assert len(instance_queries) == 4
    for query in instance_queries:
        grouping = re.match(r"count by \(([^)]+)\)", query)
        assert grouping is not None
        assert {label.strip() for label in grouping.group(1).split(",")} == {
            "service_instance_id",
            "k8s_pod_name",
            "k8s_pod_uid",
            "k8s_namespace_name",
            "service_name",
        }


@pytest.mark.parametrize(
    "conflict", ["k8s_pod_name", "k8s_namespace_name", "service_name", "duplicate_uid"]
)
def test_impl_instances_span_binding_conflicts_are_unknown(harness, conflict):
    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    at = context.authorized_at + timedelta(seconds=3)
    evidence = raw_evidence(context, at)
    key = "after:request_rate_per_second:instances"
    query = next(query for query in evidence.queries if query.query_key == key)
    rows = json.loads(query.raw)["data"]["result"]
    labels = rows[0]["metric"]
    labels.update(
        k8s_pod_name="checkout-bbb-pod",
        k8s_namespace_name=context.health_profile.subject.kubernetes_namespace,
        service_name=context.health_profile.subject.service,
    )
    if conflict == "duplicate_uid":
        rows.append(copy.deepcopy(rows[0]))
        rows[-1]["metric"]["k8s_pod_name"] = "other-pod"
    else:
        labels[conflict] = "other-target"
    evidence = _impl_replace_query_vectors(evidence, {key: rows})
    evaluation = evaluate_release_sample(context, evidence, decision_at=at)
    assert not evaluation.evidence_sufficient
    assert evaluation.verdict == "unknown"
    assert "VERSION_INSTANCE_MISMATCH" in evaluation.reason_codes


@pytest.mark.parametrize("scope", ["context", "after"])
@pytest.mark.parametrize("missing_value", [False, True])
def test_impl_insufficient_and_stale_reasons_are_both_retained(
    harness, scope, missing_value
):
    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    at = context.authorized_at + timedelta(seconds=3)
    evidence = raw_evidence(context, at)
    prefix = f"{scope}:request_rate_per_second"
    evidence = _impl_replace_query_vectors(
        evidence,
        {
            prefix + ":coverage": [{"metric": {}, "value": [at.timestamp(), "0"]}],
            prefix + ":freshness": [
                {
                    "metric": {},
                    "value": [
                        at.timestamp(),
                        str(
                            at.timestamp()
                            - context.health_profile.freshness_seconds
                            - 1
                        ),
                    ],
                }
            ],
        },
    )
    if missing_value:
        evidence = _impl_replace_query_vectors(evidence, {prefix + ":query": []})
    evaluation = evaluate_release_sample(context, evidence, decision_at=at)
    assert evaluation.verdict == "unknown"
    assert evaluation.outcome == "stale"
    assert f"INSUFFICIENT_SAMPLES:{prefix}" in evaluation.reason_codes
    assert f"STALE_TELEMETRY:{prefix}" in evaluation.reason_codes
    if missing_value:
        assert f"REQUIRED_TELEMETRY_MISSING:{prefix}" in evaluation.reason_codes


@pytest.mark.parametrize("field", ["subject_kind", "job_id", "job_sequence"])
def test_impl_replay_rejects_tampered_healthy_ending_binding(harness, field):
    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    time.sleep(2.05)
    for index in range(2):
        if index:
            time.sleep(1.05)
        lease = claim(harness, authorization.session_id)
        receipt = harness[1].submit_release_sample(
            lease, raw_evidence(context, harness[1].current_time())
        )
    assert receipt.release_status == "healthy"
    history = harness[1].release_history(harness[2])
    baseline = replay_release_history(history)
    assert baseline.consistent and baseline.verdict == "healthy"
    corrupted = copy.deepcopy(history)
    ending = corrupted["endings"][0]
    ending[field] = {
        "subject_kind": "incident",
        "job_id": uuid4(),
        "job_sequence": ending["job_sequence"] + 1,
    }[field]
    replay = replay_release_history(corrupted)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


@pytest.mark.parametrize("pod_uid", [None, "mapped", "wrong"])
def test_impl_instances_grouping_keeps_pod_uid_identity(harness, pod_uid):
    authorization = authorize(harness)
    context = harness[1].release_context(authorization.session_id)
    at = context.authorized_at + timedelta(seconds=3)
    evidence = raw_evidence(context, at)
    key = "after:request_rate_per_second:instances"
    query = next(query for query in evidence.queries if query.query_key == key)
    rows = json.loads(query.raw)["data"]["result"]
    labels = rows[0]["metric"]
    if pod_uid is not None:
        labels["k8s_pod_uid"] = (
            labels["service_instance_id"] if pod_uid == "mapped" else str(uuid4())
        )
    grouping = re.match(r"count by \(([^)]+)\)", query.query)
    assert grouping is not None
    kept = {label.strip() for label in grouping.group(1).split(",")}
    # Prometheus aggregation retains only grouping labels in the response.
    rows[0]["metric"] = {
        label: value for label, value in labels.items() if label in kept
    }
    evidence = _impl_replace_query_vectors(evidence, {key: rows})
    evaluation = evaluate_release_sample(context, evidence, decision_at=at)
    if pod_uid == "wrong":
        assert evaluation.verdict == "unknown", evaluation
        assert not evaluation.evidence_sufficient
        assert "VERSION_INSTANCE_MISMATCH" in evaluation.reason_codes
    else:
        assert evaluation.verdict == "healthy", evaluation.reason_codes


@pytest.fixture
def _impl_fresh_budget_database(monkeypatch):
    import sys

    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from opspilot import schema

    name = "opspilot_budget_guard_" + uuid4().hex[:12]
    dsn = make_conninfo(DSN, dbname=name)
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
        try:
            schema.upgrade_head(dsn)
            monkeypatch.setattr(sys.modules[__name__], "DSN", dsn)
            yield dsn
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _impl_prepare_last_budget_sample(h):
    authorization = authorize(h)
    context = h[1].release_context(authorization.session_id)
    for _ in range(context.release.max_samples - 1):
        lease = claim(h, authorization.session_id)
        receipt = h[1].submit_release_sample(
            lease, raw_evidence(context, h[1].current_time(), missing=True)
        )
        assert receipt.accepted and receipt.release_status == "observing"
    lease = claim(h, authorization.session_id)
    return context, lease, raw_evidence(context, h[1].current_time(), missing=True)


def test_impl_unknown_budget_requires_session_to_end(
    _impl_fresh_budget_database, request, monkeypatch
):
    from opspilot.observation import release_samples

    h = request.getfixturevalue("harness")
    _, lease, evidence = _impl_prepare_last_budget_sample(h)
    original = release_samples.end_release_session

    def leave_session_authorized(conn, *args, **kwargs):
        identity = conn.execute(
            "SELECT pg_has_role(current_user,'opspilot_observer','MEMBER') AS observer, current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='opspilot_release_observations'::regclass) AS non_owner"
        ).fetchone()
        assert identity["observer"] and identity["non_owner"]

        class KeepSession:
            def execute(self, query, parameters=None):
                if query.startswith("UPDATE opspilot_observation_sessions SET state="):
                    return None
                return conn.execute(query, parameters)

        return original(KeepSession(), *args, **kwargs)

    monkeypatch.setattr(
        release_samples, "end_release_session", leave_session_authorized
    )
    with pytest.raises(PersistenceError, match="INTEGRITY_REFUSED") as error:
        h[1].submit_release_sample(lease, evidence)
    assert isinstance(error.value.__cause__, psycopg.errors.InsufficientPrivilege)
    assert error.value.__cause__.sqlstate == "42501"
    history = h[1].release_history(h[2])
    assert history["release"].status == "observing"
    assert history["release"].used_sample_count == lease.sequence - 1
    assert history["sessions"][0]["state"] == "authorized"
    assert history["sessions"][0]["active_sample_job_id"] == lease.job_id
    assert not history["endings"]


def test_impl_unknown_budget_rejects_another_release_sample(
    _impl_fresh_budget_database, request, monkeypatch
):
    from contextlib import contextmanager

    from opspilot.observation import release_samples

    h = request.getfixturevalue("harness")
    context, lease, evidence = _impl_prepare_last_budget_sample(h)
    other = h[0].record_release(
        event=ReleaseEvent(
            format_version=1,
            source="guard-foreign",
            source_release_id=uuid4().hex,
            released_at=context.release.released_at,
            before=context.release.before_revision,
            after=context.release.after_revision,
            auxiliary=context.release.auxiliary,
        ),
        target_id=context.release.target_id,
        identity=context.release.target_snapshot,
        health_profile_revision=context.health_profile.revision,
        health_profile=canonical_content(context.health_profile),
        min_tracking_seconds=context.release.min_tracking_seconds,
        actor="tester",
        raw_body=b"{}",
    )
    _, other_lease, other_evidence = _impl_prepare_last_budget_sample(
        (h[0], h[1], other.observation_id, h[3], h[4])
    )
    original = release_samples.end_release_session

    def bind_foreign_sample(conn, *args, **kwargs):
        identity = conn.execute(
            "SELECT pg_has_role(current_user,'opspilot_observer','MEMBER') AS observer, current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='opspilot_release_observations'::regclass) AS non_owner"
        ).fetchone()
        assert identity["observer"] and identity["non_owner"]

        @contextmanager
        def same_transaction(*, snapshot=False):
            yield conn

        with monkeypatch.context() as nested:
            nested.setattr(h[1], "_release_transaction", same_transaction)
            nested.setattr(release_samples, "end_release_session", original)
            receipt = h[1].submit_release_sample(other_lease, other_evidence)
            assert receipt.accepted and receipt.release_status == "unknown"
        kwargs["sample_id"] = other_evidence.sample_id
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(release_samples, "end_release_session", bind_foreign_sample)
    with pytest.raises(PersistenceError, match="INTEGRITY_REFUSED") as error:
        h[1].submit_release_sample(lease, evidence)
    assert isinstance(error.value.__cause__, psycopg.errors.InsufficientPrivilege)
    assert error.value.__cause__.sqlstate == "42501"
    history = h[1].release_history(h[2])
    assert history["release"].status == "observing"
    assert history["sessions"][0]["state"] == "authorized"
    assert not history["endings"]
    foreign_history = h[1].release_history(other.observation_id)
    assert (
        foreign_history["release"].used_sample_count == context.release.max_samples - 1
    )
    assert all(
        sample["sample_id"] != other_evidence.sample_id
        for sample in foreign_history["samples"]
    )
    h[0].control_release(
        other.observation_id,
        action="cancel",
        actor="cleanup",
        expected_control_generation=0,
        idempotency_key="foreign-cleanup",
        calibrated_policy_revision=None,
    )


def test_impl_unknown_budget_normal_submit_expires_session(
    _impl_fresh_budget_database, request
):
    h = request.getfixturevalue("harness")
    context, lease, evidence = _impl_prepare_last_budget_sample(h)
    receipt = h[1].submit_release_sample(lease, evidence)
    assert receipt.accepted and receipt.release_status == "unknown"
    assert receipt.session_state == "expired"
    assert receipt.used_sample_count == context.release.max_samples
    history = h[1].release_history(h[2])
    assert history["sessions"][0]["state"] == "expired"
    assert history["sessions"][0]["active_sample_job_id"] is None
    assert history["endings"][0]["sample_id"] == evidence.sample_id
    assert replay_release_history(history).consistent


@pytest.mark.parametrize("locked_count", [3, 20])
def test_impl_public_claim_pages_and_bounds_locked_backlog(
    harness, monkeypatch, locked_count
):
    from contextlib import contextmanager

    from tests.integration.test_m1_02_observation_store_postgres import (
        _authorize,
        _incident,
    )

    observer = harness[1]
    owner = DurableStore(DSN)
    controller = ObservationStore(DSN)
    incidents = []
    sessions = []
    pages = []
    probes = []
    limit = 2
    original_transaction = observer.transaction
    original_claim = observer._claim_incident_due_samples

    class RecordingConnection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, query, parameters=None):
            cursor = self.conn.execute(query, parameters)
            if isinstance(query, str) and query.startswith("SELECT session_id,purpose"):
                rows = cursor.fetchall()
                pages.append((query, rows))

                class Result:
                    def fetchall(self):
                        return rows

                return Result()
            return cursor

    @contextmanager
    def record_transaction(*, snapshot=False):
        with original_transaction(snapshot=snapshot) as conn:
            yield RecordingConnection(conn) if snapshot else conn

    def record_probe(*args, **kwargs):
        probes.append(kwargs["selected_session_id"])
        return original_claim(*args, **kwargs)

    try:
        for _ in range(locked_count + 6):
            incident, _, target = _incident(owner)
            incidents.append(incident)
            session = _authorize(controller, incident, target)
            sessions.append(session)
            with psycopg.connect(DSN) as conn:
                conn.execute(
                    "UPDATE opspilot_observation_sessions SET active_sample_due_at=%s WHERE session_id=%s",
                    (datetime(1900, 1, 1, tzinfo=timezone.utc), session),
                )
        sessions.sort()
        monkeypatch.setattr(observer, "transaction", record_transaction)
        monkeypatch.setattr(observer, "_claim_incident_due_samples", record_probe)
        with psycopg.connect(DSN) as blocker:
            blocker.execute(
                "SELECT session_id FROM opspilot_observation_sessions WHERE session_id=ANY(%s) FOR UPDATE",
                (sessions[:locked_count],),
            )
            leases = observer.claim_due_samples(uuid4(), limit=limit)
        assert len(leases) <= limit
        assert pages and all(" LIMIT " in query for query, _ in pages)
        assert all(len(rows) <= limit for _, rows in pages)
        assert len(probes) <= 8 * limit
        if locked_count == 3:
            assert [lease.session_id for lease in leases] == sessions[
                locked_count : locked_count + limit
            ]
            assert len(pages) >= 3
        else:
            assert leases == []
            assert len(probes) == 8 * limit
    finally:
        for incident in incidents:
            controller.revoke_sessions(incident)
        controller.close()
        owner.close()
