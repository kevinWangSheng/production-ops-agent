"""F7 observer redaction contracts against the real PostgreSQL roles."""

# Imported pytest fixtures are intentionally re-bound by test parameters.
# ruff: noqa: F401,F811

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest

from opspilot.domain.evidence import QueryWindow
from opspilot.domain.intake import Target
from opspilot.knowledge.generation import build_input
from opspilot.knowledge.jobs import GenerationStore
from opspilot.observation.release_policy import canonical_json, sha256
from opspilot.observation.release_types import (
    ReleaseQueryResult,
    ReleaseSampleEvidence,
    ReleaseSampleLease,
)
from opspilot.observation.store import ObservationStore
from opspilot.observer.health_profile import SessionParameters, canonical_content
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.observer.release_replay import replay_release_history
from opspilot.observer.release_sampling import take_release_sample
from opspilot.observer.replay import main as replay_main
from opspilot.observer.sampler import take_sample
from opspilot.persistence import DurableStore
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
    hash_password,
    token_digest,
)
from tests.integration.test_f11_pr2b_impl_postgres import (
    NEW_DIGEST,
    authorized_due,
    raw_evidence,
    release_harness,
)
from tests.integration.test_f13_step4_contract_postgres import (
    _seed_incident,
    _target,
)
from tests.integration.test_m1_02_observer_postgres import (
    SHIPPED,
    _backdate_authorization,
    _incident,
    controller,
    observer,
    observer_dsn,
    owner,
    scratch_dsn,
    telemetry,
)
from tests.m1_web_support import basic, call

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

SECRET = "-".join(("fake", "observer", "credential", "42"))
AT = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _web_app(store: DurableStore):
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    incidents = DurableIncidentStore(store)
    workbench = Workbench(
        incidents=incidents,
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions={"state": "v1"},
        run_seconds=600,
    )
    config = AuthConfig(
        ui_users={"alice": hash_password("correct horse battery staple")},
        event_tokens={token_digest("event-token-for-tests-only"): "alertmanager-prod"},
        auth_revision="f7-auth",
        allowed_origins=frozenset({"https://ops.example.test"}),
    )
    return create_app(workbench, Authenticator(config), DurableClock(store)), workbench


def _inject_secret(document: dict[str, object]) -> dict[str, object]:
    data = document.get("data")
    if isinstance(data, dict):
        rows = data.get("result")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("metric"), dict):
                    row["metric"].update(
                        {
                            "session": SECRET,
                            "api_key": SECRET,
                            "url": f"https://user:{SECRET}@example.invalid/x?token={SECRET}",
                        }
                    )
    return document


def _blob(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        text = value[2:] if value.startswith("\\x") else value
        return bytes.fromhex(text)
    return bytes(value)  # type: ignore[arg-type]


def _authorize_for_replay(
    controller: ObservationStore,
    incident: object,
    target: Target,
    *,
    sustained: int = 1,
    max_samples: int = 40,
) -> tuple[object, object]:
    """Freeze the same session parameters that replay validates."""
    profile = SHIPPED.model_copy(
        update={
            "session": SessionParameters(
                deadline_seconds=3600,
                max_samples=max_samples,
                sample_interval_seconds=60,
                sustained_window_seconds=sustained,
            )
        }
    )
    session = controller.authorize_session(
        incident,
        revision=target.revision,
        actor="tester",
        deadline_at=controller.current_time() + timedelta(seconds=3600),
        max_samples=max_samples,
        sample_interval_seconds=60,
        sustained_window_seconds=sustained,
        health_profile_revision=profile.revision,
        health_profile=canonical_content(profile),
        first_sample_due_at=controller.current_time(),
    )
    return session, profile


def test_recovery_persists_redacted_bytes_replays_and_web_verifies(
    owner: DurableStore,
    controller: ObservationStore,
    observer: ObservationStore,
    observer_dsn: str,
    scratch_dsn: str,
    telemetry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident, _, target = _incident(owner)
    session, profile = _authorize_for_replay(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)
    leases = observer.claim_due_samples(uuid4(), limit=10, lease_seconds=120)
    lease = next(item for item in leases if item.session_id == session)

    state, url = telemetry
    original_answer = state.answer

    def answer(expr: str, at: float) -> dict[str, object]:
        return _inject_secret(original_answer(expr, at))

    monkeypatch.setattr(state, "answer", answer)
    result = take_sample(lease, profile, PrometheusReadOnlySource(url), observer)
    assert result.receipt.accepted

    with observer.transaction() as conn:
        current_user = conn.execute("SELECT current_user").fetchone()["current_user"]
        rows = conn.execute(
            "SELECT sample_id,signal_name,raw,raw_sha256 FROM opspilot_observation_signal_readings "
            "WHERE sample_id=%s ORDER BY signal_name",
            (result.receipt.sample_id,),
        ).fetchall()
    assert current_user != "m0_lab"  # the write ran through the Observer login
    assert rows
    for row in rows:
        raw = bytes(row["raw"])
        assert SECRET not in raw.decode("utf-8")
        assert hashlib.sha256(raw).hexdigest() == row["raw_sha256"]
        bundle = json.loads(raw)
        for kind in ("query", "coverage", "freshness"):
            body = base64.b64decode(bundle[kind]["body_b64"])
            assert SECRET not in body.decode("utf-8")
            assert hashlib.sha256(body).hexdigest() == bundle[kind]["body_sha256"]

    output = io.StringIO()
    assert (
        replay_main(["--dsn", observer_dsn, "--session", str(session)], stdout=output)
        == 0
    )
    assert all(item["consistent"] for item in json.loads(output.getvalue()))

    web_store = DurableStore(scratch_dsn)
    try:
        app, _ = _web_app(web_store)
        signal = rows[0]["signal_name"]
        path = f"/incidents/{incident}/evidence/{result.receipt.sample_id}:{signal}"
        page = call(app, "GET", path, headers=basic())
        assert page.status == 200
        body = page.json()
        assert body["hashes_verified"] is True
        assert "display_redacted" not in body

        # Simulate a legacy row written before persistence redaction.  The
        # stored bytes stay secret-bearing; only this web read may redact it.
        row = rows[0]
        old_raw = json.loads(bytes(row["raw"]))
        old_body = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"session": SECRET},
                            "value": [AT.timestamp(), "1.0"],
                        }
                    ],
                },
            },
            separators=(",", ":"),
        ).encode()
        old_raw["query"]["body_b64"] = base64.b64encode(old_body).decode()
        old_raw["query"]["body_sha256"] = sha256(old_body)
        old_bytes = canonical_json(old_raw).encode()
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_observation_signal_readings SET raw=%s,raw_sha256=%s "
                "WHERE sample_id=%s AND signal_name=%s",
                (old_bytes, sha256(old_bytes), result.receipt.sample_id, signal),
            )
        legacy = call(app, "GET", path, headers=basic()).json()
        assert legacy["display_redacted"] is True
        assert SECRET not in json.dumps(legacy)
    finally:
        web_store.close()


class _ReleaseHTTP:
    def __init__(self, context):
        self.context = context
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                query = parse_qs(urlsplit(self.path).query)["query"][0]
                at = datetime.fromtimestamp(
                    float(parse_qs(urlsplit(self.path).query)["time"][0]), timezone.utc
                )
                answer = next(
                    item
                    for item in raw_evidence(outer.context, at).queries
                    if item.query == query
                )
                body = canonical_json(_inject_secret(json.loads(answer.raw))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        return Handler

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def test_release_storage_replay_and_same_sample_redelivery(
    release_harness,
) -> None:
    web, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    # The CI database keeps earlier tests' due sessions, so claim widely and
    # pick this test's own session (shared_claim's limit=20 was not enough).
    lease = next(
        item
        for item in observer.claim_due_samples(uuid4(), limit=1000, lease_seconds=120)
        if item.session_id == auth.session_id
    )
    context = observer.release_context(lease.session_id)
    server = _ReleaseHTTP(context)
    try:
        result = take_release_sample(
            lease, PrometheusReadOnlySource(server.url), observer
        )
    finally:
        server.close()
    assert result.receipt.accepted
    assert result.receipt.evaluation.verdict == "healthy"
    history = observer.release_history(oid)
    images = history["samples"][0]["release_evaluation"]["instances"][0]["images"]
    assert images["app"] == NEW_DIGEST
    for query in history["samples"][0]["queries"]:
        raw = _blob(query["raw"])
        assert SECRET.encode() not in raw
        assert sha256(raw) == query["raw_sha256"]
    replay = replay_release_history(history)
    assert replay.consistent
    assert not any(
        code in item
        for item in replay.integrity
        for code in ("RAW_SHA256_MISMATCH", "SAMPLE_DECISION_MISMATCH")
    )
    saved = history["samples"][0]
    replay_lease = ReleaseSampleLease.model_validate_json(
        canonical_json(saved["release_receipt"]["_lease"])
    )
    evidence = ReleaseSampleEvidence(
        format_version=1,
        sample_id=saved["sample_id"],
        window=QueryWindow(start=saved["window_start"], end=saved["window_end"]),
        queries=tuple(
            ReleaseQueryResult.model_validate(
                {
                    **{key: value for key, value in row.items() if key != "sample_id"},
                    "raw": _blob(row["raw"]),
                }
            )
            for row in saved["queries"]
        ),
    )
    again = observer.submit_release_sample(replay_lease, evidence)
    assert again.replayed is True


def test_f13_read_input_and_generation_payload_exclude_raw_secret(
    scratch_dsn: str,
) -> None:
    target_id, _ = _target(scratch_dsn)
    incident, watermark = _seed_incident(scratch_dsn, target_id)
    sample_id = uuid4()
    now = datetime.now(timezone.utc)
    raw = json.dumps({"session": SECRET}).encode()
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_observation_samples(sample_id,session_id,job_id,sequence,epoch,window_start,window_end,outcome,required_signals_present,subject_control_generation,observation_generation,health_profile_revision,disposition,reason,confirms_health,health_basis,subject_lifecycle,incident_control_generation,incident_observation_generation,scope_suspended,global_generation,target_generation,within_deadline,lease_valid,lease_stamps_match,readings_consistent) "
            "VALUES (%s,%s,%s,1,1,%s,%s,'healthy',true,0,1,'f7-test','adopted','adopted',true,'confirmed','observing_recovery',0,1,false,0,0,true,true,true,true)",
            (
                sample_id,
                watermark.observation_session_id,
                uuid4(),
                now - timedelta(minutes=1),
                now,
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_signal_readings(sample_id,signal_name,status,value,sample_count,query,window_start,window_end,source,raw_sha256,raw) VALUES (%s,'request_rate_per_second','ok',1.0,5,'up',%s,%s,'prometheus',%s,%s)",
            (sample_id, now - timedelta(minutes=1), now, sha256(raw), raw),
        )
    store = GenerationStore(scratch_dsn)
    try:
        source = store.read_input(incident)
    finally:
        store.close()
    assert all("raw" not in reading for reading in source["observation"]["readings"])
    payload = build_input(source, secrets=[SECRET]).payload_text
    assert SECRET not in payload
