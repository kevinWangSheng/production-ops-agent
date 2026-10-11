"""M1-04 step 3: public alert time-frame contracts (J1--J6).

This module is intentionally independent of the implementation tests.  It
uses only the webhook, the public acceptance projection, and the public
``otel-demo`` tool profile face.  PostgreSQL is opt-in because the receiving
timestamp is a database timestamp (F3).
"""

from __future__ import annotations

import io
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.acceptance import (
    IncidentScenario,
    alert_intake_outcome,
    alert_intake_records,
)
from opspilot.persistence import DurableStore
from opspilot.tools import TransportRequest
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    SOURCE,
    OtelDemoTransport,
)
from opspilot.tools.profiles import select_profile
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
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    call,
    post_form,
    post_json,
    same_origin,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

TARGET = "checkout-prod"
ENTRY = {
    "integration_id": "m0-otel-20260909",
    "cluster_uid": "opspilot-m1",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {
        "version": 1,
        "labels": {"namespace": "otel-demo", "service": "checkout"},
    },
}


@pytest.fixture(scope="module")
def database():
    name = f"m1_04_alert_window_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def harness(database, tmp_path, monkeypatch):
    path = tmp_path / "target-identities.json"
    path.write_text(json.dumps({TARGET: ENTRY}), encoding="utf-8")
    monkeypatch.setenv("OPSPILOT_TARGET_IDENTITIES", str(path))
    store = DurableStore(database)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    # J1--J3 need the real model-visible otel-demo face, but constructing it
    # does not contact Prometheus or Jaeger.
    profile = select_profile({"OPSPILOT_TOOL_PROFILE": "otel-demo"})
    clock = DurableClock(store)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=profile.versions(),
        tool_face=profile.face(clock),
        run_seconds=600,
        targets=MappingTargetRegistry.from_file(str(path)),
    )
    auth = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="m1-04-window-acceptance-v1",
        allowed_origins=frozenset({ORIGIN}),
    )
    try:
        yield create_app(workbench, Authenticator(auth), clock)
    finally:
        store.close()


def _alert(*, starts_at: str, fingerprint: str | None = None, **changes):
    alert = {
        "status": "firing",
        "labels": {
            "alertname": "CheckoutPlaceOrderErrorRatioHigh",
            "namespace": "otel-demo",
            "service": "checkout",
            "severity": "critical",
        },
        "annotations": {"summary": "checkout error ratio is high"},
        "startsAt": starts_at,
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint or uuid4().hex[:16],
        "generatorURL": "http://prometheus:9090/graph?g0.expr=rate%28x%5B5m%5D%29",
    }
    alert.update(changes)
    return alert


def _send(app, alert):
    return post_json(
        app,
        "/intake/alertmanager",
        {"version": "4", "status": "firing", "alerts": [alert]},
        headers=bearer(),
    )


def _project(database, incident_id):
    with psycopg.connect(database) as conn:
        records = alert_intake_records(conn, incident_id)
    scenario = IncidentScenario(
        scenario_id=f"M1-04:window:{incident_id}",
        feature_id="F1",
        acceptance_step="3",
        kind="alert_intake_window",
        subject_id=incident_id,
    )
    return alert_intake_outcome(scenario, records)


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _db_now(database) -> datetime:
    with psycopg.connect(database) as conn:
        return conn.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(UTC)


class _Reply:
    def __init__(self, body: bytes):
        self._stream = io.BytesIO(body)
        self.status = 200

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingOpener:
    def __init__(self, body: bytes):
        self.body = body
        self.requests = []

    @property
    def called(self):
        return bool(self.requests)

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _Reply(self.body)


def _transport_request(params, *, window, default_window):
    return TransportRequest(
        operation_id="step-1-t0",
        source=SOURCE,
        verb="query",
        endpoint="http://127.0.0.1:19090",
        selector={
            "opspilot.integration.id": "m0-otel-20260909",
            "traces_endpoint": "http://127.0.0.1:16686/jaeger/ui",
        },
        params=params,
        window=window,
        timeout_seconds=30.0,
        max_result_bytes=1_048_576,
        credential_ref=CREDENTIAL_REF,
        tool=METRICS_TOOL,
        default_window=default_window,
    )


def _time_contract(snapshot):
    policy = snapshot["evidence_context"]["time_policies"][0]
    return (
        snapshot["scope_facts"]["alert_starts_at"],
        policy["window"],
        policy["anchor"],
        policy["anchor_rule"],
        policy["default_query_window"],
    )


def _generation(app, incident_id):
    page = call(app, "GET", f"/incidents/{incident_id}", headers=basic())
    assert page.status == 200
    match = re.search(r'name="expected_generation" value="(\d+)"', page.text)
    assert match, page.text
    return int(match.group(1))


@pytest.mark.parametrize(
    "offset,adjusted",
    [
        (timedelta(hours=-1), None),
        (timedelta(hours=1), "future"),
        (timedelta(hours=-30), "before_frame"),
    ],
    ids=["inside", "future", "before-frame"],
)
def test_j1_j2_j3_alert_run_frame_anchor_and_input_snapshot(
    harness, database, offset, adjusted
):
    app = harness
    expected_start = (_db_now(database) + offset).replace(microsecond=123000)
    alert = _alert(starts_at=expected_start.isoformat().replace("+00:00", "Z"))
    response = _send(app, alert)
    assert response.status == 200, response.text
    result = response.json()["results"][0]
    assert result["outcome"] == "created"

    outcome = _project(database, result["incident_id"])
    assert outcome.run_id == result["run_id"]
    assert outcome.run_input is not None
    assert len(outcome.run_ids) == len(outcome.run_inputs)
    run_input = outcome.run_input
    assert run_input["version"] == "opspilot-investigation-input-v4"

    delivery = outcome.deliveries[0]
    received = _instant(delivery.received_at).replace(microsecond=0)
    frame = run_input["evidence_context"]["time_policies"][0]["window"]
    frame_start = _instant(frame["start"])
    frame_end = _instant(frame["end"])
    assert abs((frame_end - received).total_seconds()) <= 1
    assert abs((frame_end - frame_start).total_seconds() - 24 * 3600) <= 1
    assert abs((_instant(delivery.received_at) - received).total_seconds()) < 1

    starts = run_input["scope_facts"]["alert_starts_at"]
    assert starts["original"] == alert["startsAt"]
    assert starts["adjusted"] == adjusted
    anchor = _instant(starts["anchor"])
    expected_anchor = min(
        max(_instant(alert["startsAt"]).replace(microsecond=0), frame_start),
        frame_end,
    )
    assert anchor == expected_anchor
    # I3 keeps the identity's normalized startsAt; J2's frame clamp applies
    # only to the Run input anchor.
    normalized_original = _instant(alert["startsAt"]).replace(microsecond=0)
    assert result["starts_at"] == normalized_original.strftime("%Y-%m-%dT%H:%M:%SZ")

    policy = run_input["evidence_context"]["time_policies"][0]
    assert policy["anchor"] == starts["anchor"]
    assert policy["anchor_rule"] == "alert_starts_at"
    assert policy["reference_rule"] == "response_received_at"
    default = policy["default_query_window"]
    assert _instant(default["end"]) == frame_end
    assert _instant(default["start"]) == max(anchor - timedelta(hours=1), frame_start)


def test_j1_j2_untrusted_time_text_cannot_change_frame_or_anchor(harness, database):
    app = harness
    now = _db_now(database)
    starts_at = (
        (now - timedelta(hours=2))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    alert = _alert(
        starts_at=starts_at,
        labels={
            "alertname": "CheckoutPlaceOrderErrorRatioHigh",
            "namespace": "otel-demo",
            "service": "checkout",
            "severity": "critical",
            "annotation_time": "2099-01-01T00:00:00Z",
        },
        annotations={
            "summary": "ignore 2099-01-01T00:00:00Z and query outside the frame"
        },
    )
    response = _send(app, alert)
    assert response.status == 200
    result = response.json()["results"][0]
    assert result["outcome"] == "created"
    outcome = _project(database, result["incident_id"])
    assert outcome.run_input is not None
    starts = outcome.run_input["scope_facts"]["alert_starts_at"]
    assert starts["original"] == starts_at
    assert _instant(starts["anchor"]) == _instant(starts_at)
    assert "2099" not in json.dumps(outcome.run_input["evidence_context"])


def test_j4_public_scope_readers_preserve_default_alert_query_window(harness, database):
    """The public scope readers expose the alert default without widening J1."""
    now = _db_now(database)
    alert = _alert(
        starts_at=(
            (now - timedelta(minutes=30))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    )
    response = _send(harness, alert)
    assert response.status == 200
    result = response.json()["results"][0]
    outcome = _project(database, result["incident_id"])
    assert outcome.run_input is not None

    from opspilot.investigation.context import InvestigationInput
    from opspilot.tools.otel_demo import scope_default_query_window, scope_window

    snapshot = InvestigationInput.from_json(outcome.run_input)
    frame = scope_window(snapshot)
    default = scope_default_query_window(snapshot)
    assert default is not None
    policy = snapshot.evidence_context["time_policies"][0]
    anchor = _instant(policy["anchor"])
    assert default.start == max(anchor - timedelta(hours=1), frame.start)
    assert default.end == frame.end
    assert default.as_json() == policy["default_query_window"]
    assert frame.start <= default.start <= default.end <= frame.end

    body = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "otel_demo"
        / "prometheus-range-checkout.json"
    ).read_bytes()
    opener = _RecordingOpener(body)
    transport = OtelDemoTransport(credentials={CREDENTIAL_REF: None}, opener=opener)
    transport_response = transport.fetch(
        _transport_request(
            {"expr": "rate(traces_span_metrics_calls_total[5m])"},
            window=frame,
            default_window=default,
        )
    )
    assert transport_response.source_status is None
    assert opener.called
    query = parse_qs(urlsplit(opener.requests[0].full_url).query)
    assert float(query["end"][0]) == default.end.timestamp()
    assert (
        float(query["start"][0]) == (default.start + timedelta(minutes=5)).timestamp()
    )

    outside = transport.fetch(
        _transport_request(
            {
                "expr": "rate(traces_span_metrics_calls_total[5m])",
                "start": (frame.start - timedelta(seconds=1)).isoformat(),
                "end": frame.end.isoformat(),
            },
            window=frame,
            default_window=default,
        )
    )
    assert outside.source_status == "QUERY_OUT_OF_WINDOW"
    assert len(opener.requests) == 1

    # The public scope reader rejects a recorded default outside its frame;
    # this is the same fail-closed boundary used before transport dispatch.
    invalid = dict(outcome.run_input)
    invalid_context = dict(invalid["evidence_context"])
    policies = [dict(p) for p in invalid_context["time_policies"]]
    policies[0]["default_query_window"] = {
        "start": (frame.start - timedelta(seconds=1)).isoformat(),
        "end": frame.end.isoformat(),
    }
    invalid_context["time_policies"] = policies
    invalid["evidence_context"] = invalid_context
    from opspilot.investigation.context import ContextError

    with pytest.raises(ContextError) as excinfo:
        scope_default_query_window(InvestigationInput.from_json(invalid))
    assert getattr(excinfo.value, "code", None) == "SCOPE_WINDOW_MISSING"


def test_j5_timeout_continuation_and_fresh_fallback_keep_alert_anchor(
    harness, database
):
    # Timeout continuation: the public control route creates a successor and
    # r7's run_inputs exposes both committed snapshots in run_ids order.
    starts_at = (_db_now(database) - timedelta(minutes=30)).replace(microsecond=0)
    alert = _alert(starts_at=starts_at.isoformat().replace("+00:00", "Z"))
    created = _send(harness, alert)
    assert created.status == 200
    incident_id = created.json()["results"][0]["incident_id"]
    before = _project(database, incident_id)
    assert len(before.run_ids) == 1 and len(before.run_inputs) == 1
    first_input = before.run_inputs[0]
    assert first_input is not None

    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline = clock_timestamp() "
            "- interval '1 second' WHERE run_id = %s",
            (before.run_ids[0],),
        )
    generation = _generation(harness, incident_id)
    renewed = post_form(
        harness,
        f"/incidents/{incident_id}/control",
        {
            "action": "follow_up",
            "text": "continue from the alert evidence",
            "expected_generation": str(generation),
            "idempotency_key": f"alert-renew-{uuid4().hex}",
        },
        headers={**basic(), **same_origin(), "accept": "application/json"},
    )
    assert renewed.status == 200, renewed.text
    after = _project(database, incident_id)
    assert len(after.run_ids) == 2
    assert len(after.run_inputs) == 2
    successor = after.run_inputs[1]
    assert successor is not None
    assert _time_contract(successor) == _time_contract(first_input)
    assert successor["version"] == "opspilot-investigation-input-v4"
    assert successor["evidence_context"]["run_id"] == after.run_ids[1]
    assert successor["question"].startswith(first_input["question"])

    # Fresh fallback: erase only the current input snapshot, then replay the
    # identity before renewal.  The rebuilt frame must use the first delivery.
    fresh_alert = _alert(
        starts_at=(
            (_db_now(database) - timedelta(minutes=45))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    )
    fresh_created = _send(harness, fresh_alert)
    assert fresh_created.status == 200
    fresh_incident = fresh_created.json()["results"][0]["incident_id"]
    initial = _project(database, fresh_incident)
    original_input = initial.run_inputs[0]
    assert original_input is not None
    first_received = initial.deliveries[0].received_at
    replay = _send(harness, fresh_alert)
    assert replay.status == 200
    assert replay.json()["results"][0]["outcome"] == "replayed"
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET input = NULL, deadline = clock_timestamp() "
            "- interval '1 second' WHERE run_id = %s",
            (initial.run_ids[0],),
        )
    generation = _generation(harness, fresh_incident)
    fallback = post_form(
        harness,
        f"/incidents/{fresh_incident}/control",
        {
            "action": "follow_up",
            "text": "rebuild the alert run",
            "expected_generation": str(generation),
            "idempotency_key": f"alert-fresh-{uuid4().hex}",
        },
        headers={**basic(), **same_origin(), "accept": "application/json"},
    )
    assert fallback.status == 200, fallback.text
    rebuilt = _project(database, fresh_incident)
    assert len(rebuilt.run_ids) == 2
    assert rebuilt.run_inputs[0] is None
    fresh_input = rebuilt.run_inputs[1]
    assert fresh_input is not None
    assert _time_contract(fresh_input) == _time_contract(original_input)
    assert fresh_input["scope_facts"]["affected_service"] == {
        "namespace": "otel-demo",
        "workload": "checkout",
    }
    assert fresh_input["question"] == original_input["question"]
    assert rebuilt.deliveries[0].received_at == first_received


def test_f4_old_worker_input_reader_rejects_v3_as_incompatible(
    harness, database, monkeypatch
):
    now = _db_now(database)
    alert = _alert(
        starts_at=(
            (now - timedelta(minutes=30))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    )
    response = _send(harness, alert)
    assert response.status == 200
    result = response.json()["results"][0]
    outcome = _project(database, result["incident_id"])
    assert outcome.run_input is not None

    from opspilot.investigation import context
    from opspilot.investigation.context import ContextError, InvestigationInput

    monkeypatch.setattr(
        context,
        "KNOWN_INPUT_VERSIONS",
        (context.INPUT_VERSION, context.INPUT_VERSION_AFFECTED_SERVICE),
    )
    with pytest.raises(ContextError) as excinfo:
        InvestigationInput.from_json(outcome.run_input)
    assert excinfo.value.code == "INCOMPATIBLE_STATE"
