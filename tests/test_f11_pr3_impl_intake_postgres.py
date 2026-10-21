"""Implementer checks for ``POST /intake/releases`` on PostgreSQL (F11 PR-3).

The independent acceptance tests live elsewhere; these exercise the route
as the web runtime role, through the real store, for the behaviours the
implementation chose: the dedicated actor, fail-closed target resolution,
replay/conflict, tolerated top-level unknown keys and storage failure.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from opspilot.observation.releases import ReleaseStore
from opspilot.observer.health_profile import load_health_profile
from opspilot.persistence import DurableStore
from opspilot.release_intake import RELEASE_MIN_TRACKING_SECONDS
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    token_digest,
)
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.m1_web_support import call
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PROFILE = load_health_profile(
    Path("opspilot/observer/profiles/otel-demo-checkout.json")
)
RELEASE_TOKEN = "release-token-for-tests-only"
ALERT_TOKEN = "alertmanager-token-for-tests-only"
A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64


def _registry(uid: str) -> dict[str, dict[str, str]]:
    base = {
        "integration_id": "pr3-impl",
        "cluster_uid": "cluster-" + uid,
        "namespace": PROFILE.subject.kubernetes_namespace,
        "health_profile_id": PROFILE.profile_id,
    }
    return {
        uid: {**base, "workload": PROFILE.subject.service},
        uid + "-other": {**base, "workload": "payment"},
    }


@pytest.fixture(scope="module")
def web_dsn():
    DurableStore(DSN).install()
    with login(DSN, "opspilot_web") as dsn:
        yield dsn


def _app(dsn: str, registry):
    store = DurableStore(dsn)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=DurableEventLog(store),
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions={},
        targets=MappingTargetRegistry(registry),
        health_profile=PROFILE,
    )
    config = AuthConfig(
        ui_users={},
        event_tokens={
            token_digest(RELEASE_TOKEN): "release-deployer",
            token_digest(ALERT_TOKEN): "alertmanager",
        },
        auth_revision="pr3-impl",
        allowed_origins=frozenset(),
    )
    return create_app(workbench, Authenticator(config), DurableClock(store))


def _payload(uid: str, registry, **overrides):
    entry = registry[uid]
    payload = {
        "format_version": 1,
        "source": "pr3-impl",
        "source_release_id": uuid4().hex,
        "released_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        "target": {
            "resource_uid": uid,
            "integration_id": entry["integration_id"],
            "cluster_uid": entry["cluster_uid"],
            "namespace": entry["namespace"],
            "workload": entry["workload"],
        },
        "before": {"images": {"checkout": A}, "pod_template_digest": A},
        "after": {"images": {"checkout": B}, "pod_template_digest": B},
        "auxiliary": {"deployment_generation": 3},
    }
    payload.update(overrides)
    return payload


def _post(app, payload, token=RELEASE_TOKEN, raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    return call(
        app,
        "POST",
        "/intake/releases",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
        body=body,
    )


def _count(source_release_id: str) -> tuple[int, int]:
    with psycopg.connect(DSN) as conn:
        observations = conn.execute(
            "SELECT count(*) FROM opspilot_release_observations WHERE source_release_id=%s",
            (source_release_id,),
        ).fetchone()[0]
        events = conn.execute(
            "SELECT count(*) FROM opspilot_release_events WHERE source_release_id=%s",
            (source_release_id,),
        ).fetchone()[0]
    return observations, events


def test_created_replayed_and_conflict(web_dsn):
    uid = "pr3-" + uuid4().hex
    registry = _registry(uid)
    app = _app(web_dsn, registry)
    payload = _payload(uid, registry)
    first = _post(app, payload)
    assert first.status == 201, first.body
    created = json.loads(first.body)
    assert (created["outcome"], created["status"]) == ("created", "pending")
    record = ReleaseStore(DSN).get_release(created["observation_id"])
    assert record.min_tracking_seconds == RELEASE_MIN_TRACKING_SECONDS
    assert record.target_snapshot["resource_uid"] == uid

    # A different transmission of the same semantics is a replay; an
    # unknown top-level key reaches only the audit text, not the hash.
    again = {**payload, "note": "Bearer should-not-be-stored-verbatim-0123456789"}
    raw = json.dumps(again, indent=2).encode()
    second = _post(app, None, raw=raw)
    assert second.status == 200, second.body
    assert json.loads(second.body)["observation_id"] == created["observation_id"]

    changed = {**payload, "auxiliary": {"deployment_generation": 4}}
    third = _post(app, changed)
    assert (third.status, json.loads(third.body)["code"]) == (
        409,
        "RELEASE_IDENTITY_CONFLICT",
    )
    events = ReleaseStore(DSN).release_events(created["observation_id"])
    assert [e.outcome for e in events] == ["created", "replayed", "rejected"]
    assert [e.actor for e in events] == ["release-deployer"] * 3
    assert events[0].semantic_sha256 == events[1].semantic_sha256
    assert events[1].raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert "note" in events[1].audit_text
    assert "should-not-be-stored-verbatim" not in events[1].audit_text
    assert ReleaseStore(DSN).get_release(created["observation_id"]) == record


def test_refusals_write_nothing(web_dsn):
    uid = "pr3-" + uuid4().hex
    registry = _registry(uid)
    app = _app(web_dsn, registry)
    cases = []
    p = _payload(uid, registry)
    cases.append((_post(app, p, token=ALERT_TOKEN), 403, "RELEASE_ACTOR_REQUIRED", p))
    p = _payload(uid, registry)
    cases.append((_post(app, p, token="nope"), 401, "INVALID_CREDENTIALS", p))
    p = _payload(uid, registry, released_at="2026-10-21T12:00:00")
    cases.append((_post(app, p), 400, "INVALID_RELEASE_PAYLOAD", p))
    p = _payload(uid, registry)
    p["after"] = {**p["after"], "extra": 1}
    cases.append((_post(app, p), 400, "INVALID_RELEASE_PAYLOAD", p))
    p = _payload(uid, registry)
    p["target"] = {**p["target"], "resource_uid": "missing-" + uid, "cluster_uid": "x"}
    cases.append((_post(app, p), 422, "TARGET_UNRESOLVED", p))
    p = _payload(uid, registry)
    p["target"] = {**p["target"], "integration_id": "other"}
    cases.append((_post(app, p), 422, "TARGET_MISMATCH", p))
    p = _payload(uid, registry)
    cases.append(
        (
            _post(app, None, raw=b"{" + b" " * (128 * 1024) + b"}"),
            413,
            "BODY_TOO_LARGE",
            p,
        )
    )
    for response, status, code, sent in cases:
        assert (response.status, json.loads(response.body)["code"]) == (status, code)
        assert _count(sent["source_release_id"]) == (0, 0)


def test_ambiguous_target(web_dsn):
    uid = "pr3-" + uuid4().hex
    registry = _registry(uid)
    registry[uid + "-twin"] = dict(registry[uid])
    app = _app(web_dsn, registry)
    p = _payload(uid, registry)
    response = _post(app, p)
    assert (response.status, json.loads(response.body)["code"]) == (
        422,
        "TARGET_AMBIGUOUS",
    )
    assert _count(p["source_release_id"]) == (0, 0)


def test_future_and_late(web_dsn):
    uid = "pr3-" + uuid4().hex
    registry = _registry(uid)
    app = _app(web_dsn, registry)
    future = _payload(
        uid,
        registry,
        released_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    response = _post(app, future)
    assert (response.status, json.loads(response.body)["code"]) == (
        422,
        "RELEASE_TIME_IN_FUTURE",
    )
    assert _count(future["source_release_id"]) == (0, 1)
    late = _payload(
        uid,
        registry,
        released_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    )
    response = _post(app, late)
    body = json.loads(response.body)
    assert response.status == 201
    assert (body["status"], body["reason"]) == ("unknown", "late_delivery")


def test_storage_failure_is_503_and_retry_replays(web_dsn):
    uid = "pr3-" + uuid4().hex
    registry = _registry(uid)
    app = _app(web_dsn, registry)
    p = _payload(uid, registry)
    name = "pr3_impl_fail_" + uuid4().hex[:8]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS "
            f"$$BEGIN IF NEW.source_release_id = '{p['source_release_id']}' "
            "THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END$$"
        )
        conn.execute(
            f"CREATE TRIGGER {name} BEFORE INSERT ON opspilot_release_events "
            f"FOR EACH ROW EXECUTE FUNCTION {name}()"
        )
        try:
            response = _post(app, p)
        finally:
            conn.execute(f"DROP TRIGGER {name} ON opspilot_release_events")
            conn.execute(f"DROP FUNCTION {name}()")
    assert (response.status, json.loads(response.body)["code"]) == (
        503,
        "STORAGE_UNAVAILABLE",
    )
    assert _count(p["source_release_id"]) == (0, 0)
    retry = _post(app, p)
    assert retry.status == 201
    assert _count(p["source_release_id"]) == (1, 1)


def test_harness_sends_over_real_http(web_dsn, tmp_path):
    """P3-03 end to end: the harness sender, a real uvicorn server, PG."""
    import socket
    import threading
    import time

    import uvicorn

    from opspilot.observation.release_policy import canonical_json, sha256
    from scripts import f11_u2_harness as harness

    uid = "pr3-" + uuid4().hex
    registry = _registry(uid)
    app = _app(web_dsn, registry)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    before_template = {"spec": {"containers": [{"name": "checkout"}]}}
    after_template = {"spec": {"containers": [{"name": "checkout", "env": []}]}}
    release = dict(
        release_id="pr3-" + uuid4().hex,
        released_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        before_template=before_template,
        after_template=after_template,
        before_revision=dict(
            images={"checkout": A},
            pod_template_digest="sha256:"
            + sha256(canonical_json(before_template).encode()),
        ),
        after_revision=dict(
            images={"checkout": B},
            pod_template_digest="sha256:"
            + sha256(canonical_json(after_template).encode()),
        ),
        deployment_uid="d",
        deployment_generation=2,
        before_replicaset_uid="rs1",
        after_replicaset_uid="rs2",
        before_pod_template_hash="h1",
        after_pod_template_hash="h2",
    )
    run = tmp_path / "run"
    run.mkdir()
    (run / "release.json").write_text(json.dumps(release))
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry))
    url = f"http://127.0.0.1:{port}/intake/releases"
    try:
        first = harness.send_release_webhook(
            run, registry_path, uid, url, RELEASE_TOKEN
        )
        again = harness.send_release_webhook(
            run, registry_path, uid, url, RELEASE_TOKEN
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert (first["status"], first["response"]["outcome"]) == (201, "created")
    assert (again["status"], again["response"]["outcome"]) == (200, "replayed")
    events = ReleaseStore(DSN).release_events(first["response"]["observation_id"])
    assert [e.raw_sha256 for e in events] == [first["request_sha256"]] * 2
    assert {e.actor for e in events} == {"release-deployer"}


def test_registered_release_authorization_is_uncalibrated(web_dsn):
    """P3-02: the webhook only registers; authorization with the empty
    catalog is refused and leaves the release pending without a session."""
    from uuid import UUID

    from opspilot.persistence import PersistenceError

    uid = "pr3-" + uuid4().hex
    registry = _registry(uid)
    app = _app(web_dsn, registry)
    created = json.loads(_post(app, _payload(uid, registry)).body)
    observation_id = UUID(created["observation_id"])
    store = ReleaseStore(web_dsn)
    try:
        outcome = store.authorize_release(
            observation_id,
            actor="operator",
            expected_control_generation=0,
            idempotency_key="pr3-" + uuid4().hex,
            calibrated_policy_revision="none@000000000000",
        )
        refusal = getattr(outcome, "reason", None) or getattr(outcome, "code", None)
    except PersistenceError as exc:
        refusal = str(exc)
    assert refusal == "RELEASE_POLICY_UNCALIBRATED"
    assert store.get_release(observation_id).status == "pending"
    with psycopg.connect(DSN) as conn:
        sessions = conn.execute(
            "SELECT count(*) FROM opspilot_observation_sessions WHERE subject_id=%s",
            (observation_id,),
        ).fetchone()[0]
    assert sessions == 0
