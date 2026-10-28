"""PR-2b bounded non-owner PG chain with test-only calibration and telemetry."""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.domain.evidence import QueryWindow
from opspilot.observation import release_policy
from opspilot.observation.release_policy import ReleasePolicy, canonical_json, sha256
from opspilot.observation.release_types import ReleaseQueryResult, ReleaseSampleEvidence
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
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.observer.release_evaluation import build_release_query_plan
from opspilot.observer.release_replay import replay_release_history
from opspilot.observer.release_sampling import take_release_sample
from opspilot.persistence import DurableStore
from scripts.m0.postgres_lab import DSN
from tests.f11_release_support import assert_shipped_catalog_is_the_approved_policy
from tests.runtime_roles_support import login
from tests.test_f11_pr2b_impl import Opener

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
DIGEST = "sha256:" + "a" * 64
NEW_DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def release_harness(tmp_path, monkeypatch, request):
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
    resource_uid = "pr2b-" + uuid4().hex
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
                source="pr2b-" + uuid4().hex,
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
            receipt = web.control_release(
                oid,
                action="cancel",
                actor="cleanup",
                expected_control_generation=row.control_generation,
                idempotency_key="cleanup",
                calibrated_policy_revision=None,
            )
        web.close()
        observer.close()


def raw_evidence(context, at, *, degraded=False, missing=False):
    instance = SimpleNamespace(
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


class Telemetry:
    """Fake HTTP bodies passed through the product vector parser at DB T."""

    def __init__(self, context, *, degraded=False, mapping_missing=False, on_call=None):
        self.context = context
        self.degraded = degraded
        self.mapping_missing = mapping_missing
        self.on_call = on_call
        self.calls = []

    def instant(self, *args, **kwargs):
        pytest.fail("release telemetry must use vector")

    def instant_vector(self, expr, *, at, timeout_seconds):
        evidence = raw_evidence(self.context, at, degraded=self.degraded)
        answer = next(q for q in evidence.queries if q.query == expr)
        raw = answer.raw
        if self.mapping_missing and answer.query_key.startswith("mapping:"):
            raw = canonical_json(
                {"status": "success", "data": {"resultType": "vector", "result": []}}
            ).encode()
        self.calls.append((expr, at, timeout_seconds))
        if self.on_call:
            self.on_call(len(self.calls))
        return PrometheusReadOnlySource(
            "http://synthetic.example", opener=Opener(raw), clock=lambda: at
        ).instant_vector(expr, at=at, timeout_seconds=timeout_seconds)


def authorized_due(h):
    web, observer, oid, policy, _ = h
    auth = web.authorize_release(
        oid,
        actor="tester",
        expected_control_generation=0,
        idempotency_key="authorize",
        calibrated_policy_revision=policy.revision,
    )
    assert auth.outcome == "applied"
    # The fixture's two-second window is bounded; synthetic historical A is
    # set only as superuser with triggers bypassed, never via a product role.
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role=replica")
        conn.execute(
            "UPDATE opspilot_observation_sessions SET authorized_at=clock_timestamp()-interval '3 seconds', active_sample_due_at=clock_timestamp() WHERE session_id=%s",
            (auth.session_id,),
        )
    return auth


def shared_claim(observer, sid):
    leases = observer.claim_due_samples(uuid4(), limit=20, lease_seconds=120)
    return next(lease for lease in leases if lease.session_id == sid)


@pytest.mark.parametrize(
    "degraded,mapping_missing,requests,verdict,status",
    [
        (False, False, 49, "healthy", "observing"),
        (True, False, 49, "degraded", "anomalous"),
        (False, True, 33, "unknown", "observing"),
    ],
)
def test_authorize_claim_collect_submit_replay(
    release_harness, degraded, mapping_missing, requests, verdict, status
):
    web, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    lease = shared_claim(observer, auth.session_id)
    context = observer.release_context(lease.session_id)
    source = Telemetry(context, degraded=degraded, mapping_missing=mapping_missing)
    result = take_release_sample(lease, source, observer)
    assert result.requests_issued == requests and len(source.calls) == requests
    assert not result.scope_interrupted and not result.budget_exhausted
    receipt = result.receipt
    assert receipt.accepted and receipt.disposition == "adopted"
    assert receipt.evaluation.verdict == verdict and receipt.release_status == status
    assert (
        receipt.used_sample_count == 1 and web.get_release(oid).used_sample_count == 1
    )
    history = observer.release_history(oid)
    assert len(history["samples"]) == 1
    replay = replay_release_history(history)
    assert replay.consistent and replay.used_sample_count == 1
    assert replay.external_queries == () and replay.model_requests == ()


@pytest.mark.parametrize("allowed", [0, 5, 35])
def test_cancel_during_collection_stops_requests_and_retains_history(
    release_harness, allowed
):
    web, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    lease = shared_claim(observer, auth.session_id)

    def cancel(n):
        if n == allowed:
            receipt = web.control_release(
                oid,
                action="cancel",
                actor="tester",
                expected_control_generation=web.get_release(oid).control_generation,
                idempotency_key="cancel",
                calibrated_policy_revision=None,
            )
            assert receipt.outcome == "applied"

    if allowed == 0:
        cancel(0)
    source = Telemetry(observer.release_context(lease.session_id), on_call=cancel)
    result = take_release_sample(lease, source, observer)
    assert result.requests_issued == allowed and result.scope_interrupted
    assert not result.receipt.accepted and result.receipt.disposition == "history_only"
    assert (
        web.get_release(oid).status == "cancelled"
        and web.get_release(oid).used_sample_count == 0
    )
    history = observer.release_history(oid)
    assert len(history["samples"]) == 1
    assert len(history["samples"][0]["queries"]) == allowed


def test_observer_tick_dispatches_release_in_real_store(release_harness):
    web, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    source = Telemetry(observer.release_context(auth.session_id))
    result = ObserverLoop(store=observer, source=source, batch=1).poll_once()
    assert len(result) == 1 and result[0][0] == auth.session_id
    assert result[0][1].disposition == "adopted" and result[0][1].used_sample_count == 1
    assert web.get_release(oid).status == "observing" and len(source.calls) == 49


def test_shipped_catalog_is_exactly_the_approved_policy():
    assert_shipped_catalog_is_the_approved_policy()


@pytest.mark.parametrize("after_mapping", [False, True])
def test_query_budget_timeout_evidence_is_adopted_and_replays(
    release_harness, monkeypatch, after_mapping
):
    web, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    lease = shared_claim(observer, auth.session_id)
    clock = [0.0]

    def used(n):
        if after_mapping and n == 9:
            clock[0] = 1_000.0

    source = Telemetry(observer.release_context(lease.session_id), on_call=used)
    ticks = iter([0.0])

    def monotonic():
        first = next(ticks, None)
        return first if first is not None else (clock[0] if after_mapping else 1_000.0)

    # Patch only the sampler's monotonic seam, leaving HTTP's real clock alone.
    monkeypatch.setattr(
        "opspilot.observer.release_sampling.time", SimpleNamespace(monotonic=monotonic)
    )
    result = take_release_sample(lease, source, observer)
    assert result.requests_issued == (9 if after_mapping else 0)
    assert result.budget_exhausted and not result.scope_interrupted
    assert result.receipt.accepted and result.receipt.evaluation.outcome == "timeout"
    assert web.get_release(oid).used_sample_count == 1
    history = observer.release_history(oid)
    queries = history["samples"][0]["queries"]
    assert len(queries) == (49 if after_mapping else 33)
    fillers = [q for q in queries if q["detail"] == "LEASE_BUDGET"]
    assert len(fillers) == (40 if after_mapping else 33)
    assert all(q["sent_at"] is None and not q["body_complete"] for q in fillers)
    assert replay_release_history(history).consistent


def test_zero_query_scope_interruption_is_counted_unknown(release_harness):
    web, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    lease = shared_claim(observer, auth.session_id)
    source = Telemetry(observer.release_context(lease.session_id))
    result = take_release_sample(lease, source, observer, scope_current=lambda _: False)
    assert result.requests_issued == 0 and result.scope_interrupted
    assert result.receipt.accepted and result.receipt.evaluation.outcome == "no_data"
    assert (
        result.receipt.used_sample_count == 1
        and web.get_release(oid).used_sample_count == 1
    )
    history = observer.release_history(oid)
    assert (
        history["samples"][0]["queries"] == () or history["samples"][0]["queries"] == []
    )
    assert replay_release_history(history).consistent
