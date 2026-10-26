"""PostgreSQL acceptance seams for #319.

The tests exercise the public durable evidence/web and release-audit APIs.  A
database is required because the contract is specifically about old persisted
bytes, hashes and version-gated Run claims.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from opspilot.observation.releases import (
    ReleaseEvent,
    ReleaseRevision,
    ReleaseStore,
)
from opspilot.observer.health_profile import canonical_content, load_health_profile
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.observer.sampler import take_sample
from opspilot.persistence import DurableStore
from opspilot.redaction import REDACTED_CREDENTIAL
from opspilot.tools.otel_demo import TARGET_ID, TRACES_TOOL
from tests.acceptance.f7_hostile_support import jaeger_payload, prometheus_payload
from tests.acceptance.f7_pg_support import (
    ScriptedProvider,
    assistant,
    build_harness,
    evidence_rows,
    final_report,
    fn,
    scratch_database,
)
from tests.integration.test_f7_observer_redaction_postgres import (
    _authorize_for_replay,
    _backdate_authorization,
    _incident,
    _web_app,
)
from tests.m1_web_support import basic, call
from tests.runtime_roles_support import login
from tests.test_m1_otel_demo_contract import GOOD_EXPR, FakeOpener

pytest_plugins = ["tests.integration.test_f7_observer_redaction_postgres"]


pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

SECRET = "alice:s3cr3t-319"
HOST = "db.example.test"
MIXED_PREFIX = r"/\/"
# Publicly observed origin/main value before #319.  The new value is derived
# from public schema inputs, so this test only needs the old value as a
# compatibility fixture.
OLD_TOOL_SCHEMA_REVISION = "otel-demo-68ed2ca8f5d6"


@pytest.fixture(scope="module")
def dsn():
    yield from scratch_database()


def _opener() -> FakeOpener:
    return FakeOpener(
        by_query={GOOD_EXPR: prometheus_payload()},
        routes={
            "/api/traces": jaeger_payload(
                {
                    "key": "exception.message",
                    "value": f"https:\\/\\/{SECRET}@{HOST}/orders",
                }
            )
        },
    )


def test_legacy_tool_evidence_web_redacts_display_and_preserves_hashes(
    dsn, monkeypatch
) -> None:
    harness = build_harness(dsn, monkeypatch, _opener())
    try:
        incident, run = harness.submit("Why is checkout erroring?", target_id=TARGET_ID)
        provider = ScriptedProvider(
            [
                assistant(
                    tool_calls=[
                        fn("call-1", TRACES_TOOL, {"service": "checkout"}),
                    ]
                ),
                final_report(TARGET_ID),
            ]
        )
        harness.run(incident, provider)
        rows = evidence_rows(harness, run)
        assert rows
        row = rows[0]

        # Simulate bytes written by a pre-#319 worker.  Only the web display is
        # allowed to transform them; the persisted raw hash must still verify.
        legacy = f'{{"dsn":"https:\\/\\/{SECRET}@{HOST}/orders"}}'.encode()
        digest = hashlib.sha256(legacy).hexdigest()
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "UPDATE opspilot_evidence SET raw=%s,raw_sha256=%s WHERE evidence_id=%s",
                (legacy, digest, row["evidence_id"]),
            )

        page = harness.get(f"/incidents/{incident}/evidence/{row['evidence_id']}")
        assert page.status == 200, page.text
        payload = page.json()
        assert payload.get("display_redacted") is True
        assert payload.get("hashes_verified") is True
        assert SECRET not in page.text
        assert REDACTED_CREDENTIAL in page.text
    finally:
        for closer in harness.closers:
            closer.close()


def test_claiming_run_recorded_before_tool_schema_bump_blocks_incompatible_state(
    dsn, monkeypatch
) -> None:
    harness = build_harness(dsn, monkeypatch, _opener())
    try:
        current = harness.profile.versions()
        # This is the public current value on origin/main; the implementation
        # must publish a different value after #319.  Keeping the old literal
        # here makes the pre-bump Run observable without importing internals.
        old_app = harness.workbench_app(
            {**current, "tool_schema_revision": OLD_TOOL_SCHEMA_REVISION}
        )
        incident, run = harness.submit(
            "Why is checkout erroring?", target_id=TARGET_ID, app=old_app
        )
        # If the version gate is absent on origin/main, let the run reach one
        # deterministic model response so the assertion below reports a
        # behavioural mismatch instead of exhausting the provider script.
        outcome = harness.run(
            incident, ScriptedProvider([final_report(TARGET_ID) for _ in range(10)])
        )
        assert (outcome.status, outcome.reason) == ("blocked", "INCOMPATIBLE_STATE")
        assert outcome.evidence_ids == ()
        assert evidence_rows(harness, run) == []
    finally:
        for closer in harness.closers:
            closer.close()


@pytest.fixture(scope="module")
def release_store(dsn):
    with login(dsn, "opspilot_web") as web_dsn:
        store = ReleaseStore(web_dsn)
        try:
            yield store
        finally:
            store.close()


@pytest.fixture
def release_request(dsn):
    profile = load_health_profile(Path("opspilot/observer/profiles/otel-demo-checkout.json"))
    uid = "319-release-" + uuid4().hex
    owner = DurableStore(dsn)
    try:
        target_id = owner.register_target(uid)
    finally:
        owner.close()
    digest = "sha256:" + "a" * 64
    return {
        "event": ReleaseEvent(
            format_version=1,
            source="319-source-" + uuid4().hex,
            source_release_id=uuid4().hex,
            released_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            before=ReleaseRevision(images={"app": digest}, pod_template_digest=digest),
            after=ReleaseRevision(
                images={"app": "sha256:" + "b" * 64},
                pod_template_digest=digest,
            ),
        ),
        "target_id": target_id,
        "identity": {
            "integration_id": "kind-lab",
            "cluster_uid": "opspilot-kind",
            "resource_uid": uid,
            "namespace": profile.subject.kubernetes_namespace,
            "workload": profile.subject.service,
        },
        "health_profile_revision": profile.revision,
        "health_profile": canonical_content(profile),
        "min_tracking_seconds": 1200,
        "actor": "319-test",
        "raw_body": f'{{"note":"https:\\/\\/{SECRET}@{HOST}/orders"}}'.encode(),
    }


def test_release_audit_text_redacts_escaped_userinfo(release_store, release_request) -> None:
    receipt = release_store.record_release(**release_request)
    assert receipt.outcome == "created"
    [audit] = release_store.release_events(receipt.observation_id)
    assert SECRET not in audit.audit_text
    assert REDACTED_CREDENTIAL in audit.audit_text
    assert audit.raw_sha256 == hashlib.sha256(release_request["raw_body"]).hexdigest()


def test_legacy_observer_reading_web_display_redacts_and_keeps_hashes(
    owner, controller, observer, observer_dsn, scratch_dsn, telemetry, monkeypatch
) -> None:
    incident, _, target = _incident(owner)
    session, profile = _authorize_for_replay(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)
    lease = next(
        item
        for item in observer.claim_due_samples(uuid4(), limit=10, lease_seconds=120)
        if item.session_id == session
    )
    state, url = telemetry
    original_answer = state.answer

    def answer(expr: str, at: float) -> dict[str, object]:
        document = original_answer(expr, at)
        data = document.get("data")
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            for row in data["result"]:
                if isinstance(row, dict) and isinstance(row.get("metric"), dict):
                    row["metric"]["url"] = (
                        f"https:{MIXED_PREFIX}{SECRET}@{HOST}/orders"
                    )
        return document

    monkeypatch.setattr(state, "answer", answer)
    result = take_sample(lease, profile, PrometheusReadOnlySource(url), observer)
    assert result.receipt.accepted

    with observer.transaction() as conn:
        row = conn.execute(
            "SELECT signal_name,raw,raw_sha256 FROM opspilot_observation_signal_readings "
            "WHERE sample_id=%s ORDER BY signal_name LIMIT 1",
            (result.receipt.sample_id,),
        ).fetchone()
    assert row is not None
    if isinstance(row, dict):
        signal, raw = row["signal_name"], row["raw"]
    else:
        signal, raw = row[0], row[1]
    raw_bytes = raw.encode() if isinstance(raw, str) else bytes(raw)
    bundle = json.loads(raw_bytes)
    body = f"upstream https:{MIXED_PREFIX}{SECRET}@{HOST}/orders".encode()
    bundle["query"]["body_b64"] = base64.b64encode(body).decode()
    bundle["query"]["body_sha256"] = hashlib.sha256(body).hexdigest()
    legacy = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_signal_readings SET raw=%s,raw_sha256=%s "
            "WHERE sample_id=%s AND signal_name=%s",
            (legacy, hashlib.sha256(legacy).hexdigest(), result.receipt.sample_id, signal),
        )

    web_store = DurableStore(scratch_dsn)
    try:
        app, _ = _web_app(web_store)
        path = f"/incidents/{incident}/evidence/{result.receipt.sample_id}:{signal}"
        page = call(app, "GET", path, headers=basic())
        assert page.status == 200, page.text
        payload = page.json()
        assert payload.get("display_redacted") is True
        assert payload.get("hashes_verified") is True
        assert SECRET not in page.text
        shown_bundle = json.loads(payload["raw_utf8"])
        shown_body = base64.b64decode(shown_bundle["query"]["body_b64"])
        assert REDACTED_CREDENTIAL.encode() in shown_body
    finally:
        web_store.close()
