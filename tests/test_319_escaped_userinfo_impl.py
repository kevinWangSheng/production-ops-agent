"""Implementer self-checks for #319: the shared userinfo rule also covers a URL
authority still spelled with JSON escapes (``\\/`` and ``\\u002f``/``\\u002F``,
mixed freely), and the Observer's cut-body path recognises the same forms.

The independent acceptance tests live elsewhere; these pin the
implementation's own edges: exact output bytes, the literal ``//`` rule left
byte-identical, no false positives on the brief's samples, idempotence,
linear time, and one sample per call surface the brief lists.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

import pytest

from opspilot.alertmanager import alert_context, parse_alert, record_of
from opspilot.redaction import (
    REDACTED_CREDENTIAL,
    redact_credentials,
    redact_json_bytes,
)

R = REDACTED_CREDENTIAL
SECRET = "synthetic-pw-three-one-nine"
# JSON spellings of one ``/``; a raw string keeps each backslash literal
S, U, UU = "\\/", "\\u002f", "\\u002F"


# -- the shared rule: exact bytes ---------------------------------------------

REDACTED = [
    # scheme + \/\/
    (
        rf"dial https:\/\/alice:{SECRET}@db.internal:5432\/app failed",
        rf"dial https:\/\/{R}@db.internal:5432\/app failed",
    ),
    # no scheme
    (
        rf"upstream \/\/alice:{SECRET}@db.internal\/x",
        rf"upstream \/\/{R}@db.internal\/x",
    ),
    # a JSON fragment inside <pre>
    (
        rf'<pre>{{"dsn":"postgres:\/\/alice:{SECRET}@db:5432\/app"}}</pre>',
        rf'<pre>{{"dsn":"postgres:\/\/{R}@db:5432\/app"}}</pre>',
    ),
    # cut JSON, redacted as prose
    (
        rf'{{"dsn":"postgres:\/\/alice:{SECRET}@db:5432\/app","x":',
        rf'{{"dsn":"postgres:\/\/{R}@db:5432\/app","x":',
    ),
    # / form, both cases
    (
        rf"https:{U}{U}alice:{SECRET}@db.internal{U}x",
        rf"https:{U}{U}{R}@db.internal{U}x",
    ),
    (rf"{UU}{UU}alice:{SECRET}@db.internal", rf"{UU}{UU}{R}@db.internal"),
    # mixed spellings keep their own prefix
    (rf"https:{S}{UU}alice:{SECRET}@h?q=1", rf"https:{S}{UU}{R}@h?q=1"),
    (rf"https:{U}{S}alice:{SECRET}@h#f", rf"https:{U}{S}{R}@h#f"),
    # a credential-named user no longer swallows host and path (assignment
    # rule used to run first on this text)
    (r"x \/\/token:abc123@host\/p", rf"x \/\/{R}@host\/p"),
    # a quote or backslash ends the host
    (rf'"u":"\/\/a:{SECRET}@h"', rf'"u":"\/\/{R}@h"'),
]


@pytest.mark.parametrize(("text", "expected"), REDACTED)
def test_escaped_authority_userinfo_is_replaced_and_the_rest_kept(text, expected):
    assert redact_credentials(text) == expected


# Outputs of the literal ``//`` rule on main before #319 (observed, not
# recomputed): they stay byte-identical.
LITERAL = [
    (
        f"dial https://alice:{SECRET}@db.internal/x",
        f"dial https://{R}@db.internal/x",
    ),
    (f"//alice:{SECRET}@db:5432/app", f"//{R}@db:5432/app"),
    # the literal rule's known over-reach is unchanged too
    ('{"a":"//x","b":"me@corp.com"}', f'{{"a":"//{R}@corp.com"}}'),
]


@pytest.mark.parametrize(("text", "expected"), LITERAL)
def test_literal_double_slash_output_is_unchanged(text, expected):
    assert redact_credentials(text) == expected


UNTOUCHED = [
    r'{"a":"\/\/x","b":"me@corp.com"}',
    r"\/\/registry.npmjs.org\/@scope\/pkg",
    r"see \/\/wiki and mail ops@corp.com",
    rf'{{"a":"{U}{U}x","b":"me@corp.com"}}',
    rf"{U}{U}registry.npmjs.org{U}@scope{U}pkg",
    rf"see {U}{U}wiki and mail ops@corp.com",
    # one escaped slash is not an authority
    rf"a {S}b:c@d and {U}b:c@d",
]


@pytest.mark.parametrize("text", UNTOUCHED)
def test_text_that_is_not_an_escaped_userinfo_is_left_alone(text):
    assert redact_credentials(text) == text


@pytest.mark.parametrize("text", [t for t, _ in REDACTED + LITERAL] + UNTOUCHED)
def test_redaction_is_idempotent(text):
    once = redact_credentials(text)
    assert redact_credentials(once) == once


@pytest.mark.parametrize(
    "chunk",
    [S, U, UU, S + S + "a", U + U + "a", S + UU + "a", S + S + "a@", U + S + "a@"],
)
def test_pathological_input_stays_linear(chunk):
    text = chunk * (512 * 1024 // len(chunk))
    started = time.monotonic()
    redact_credentials(text)
    assert time.monotonic() - started < 5, chunk


# -- redact_json_bytes ----------------------------------------------------------


def test_json_inside_a_json_string_is_redacted():
    inner = json.dumps({"dsn": f"https://alice:{SECRET}@db/x"}).replace("/", S)
    # JSON text as a producer writes it (one level of escapes), then nested
    mixed = f'{{"dsn":"https:{U}{S}alice:{SECRET}@db"}}'
    body = json.dumps({"log": inner, "note": mixed}).encode()
    assert SECRET.encode() in body
    kept = redact_json_bytes(body)
    assert SECRET.encode() not in kept
    decoded = json.loads(kept)
    assert decoded["log"] == rf'{{"dsn": "https:\/\/{R}@db\/x"}}'
    assert decoded["note"] == rf'{{"dsn":"https:{U}{S}{R}@db"}}'


def test_an_html_body_is_redacted_as_prose():
    body = f"<html><pre>postgres:{S}{S}alice:{SECRET}@db{S}x</pre></html>".encode()
    assert redact_json_bytes(body) == (
        f"<html><pre>postgres:{S}{S}{R}@db{S}x</pre></html>".encode()
    )


# -- Observer: complete and cut bodies -----------------------------------------

AT = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def _serve(status: int, body: bytes, *, cut: bool = False):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body) + (100 if cut else 0)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def query():
    from opspilot.observer.prometheus import PrometheusReadOnlySource

    servers = []

    def run(status: int, body: bytes, *, cut: bool = False):
        server = _serve(status, body, cut=cut)
        servers.append(server)
        src = PrometheusReadOnlySource(f"http://127.0.0.1:{server.server_port}")
        return src.instant("up", at=AT, timeout_seconds=3)

    yield run
    for server in servers:
        server.shutdown()
        server.server_close()


def test_a_complete_non_json_error_body_is_stored_redacted(query):
    page = f'<pre>{{"dsn":"postgres:{U}{U}alice:{SECRET}@db{U}app"}}</pre>'
    result = query(502, page.encode())
    assert result.http_status == 502 and result.body_complete
    assert result.body == (
        f'<pre>{{"dsn":"postgres:{U}{U}{R}@db{U}app"}}</pre>'.encode()
    )


LONG = "Zq7Wx9Kp3Lm5Nv8Rt2Yb"
CUT_FORMS = {
    "u002f": '{"url":"https:\\u002f\\u002fuser:%s@host.example\\u002fx"}',
    "u002F": "proxy error at \\u002F\\u002Fuser:%s@host.example end",
    "mixed": '{"url":"https:\\/\\u002fuser:%s@host.example\\/x"}',
    "mixed_rev": '{"url":"\\u002F\\/user:%s@host.example\\/x"}',
}


def _fragments(text: bytes) -> list[str]:
    return [
        LONG[i : i + 8]
        for i in range(len(LONG) - 7)
        if LONG[i : i + 8].encode() in text
    ]


@pytest.mark.parametrize("form", sorted(CUT_FORMS))
def test_an_escaped_authority_cut_at_any_point_leaves_no_fragment(form):
    from opspilot.observer.prometheus import _kept_prefix

    whole = (CUT_FORMS[form] % LONG).encode()
    for cut in range(len(whole) + 1):
        kept = _kept_prefix(whole[:cut], complete=False)
        assert not _fragments(kept), (form, cut, kept)


def test_a_cut_error_body_over_the_wire_leaves_no_fragment(query):
    body = (CUT_FORMS["u002f"] % LONG).encode()
    result = query(200, body[: body.index(b"@") + 3], cut=True)
    assert result.status == "failed" and result.body_complete is False
    assert not _fragments(result.body)


# -- call surfaces: one each ----------------------------------------------------

DSN_TEXT = rf"connect failed: postgres:\/\/svc:{SECRET}@db.internal:5432\/orders"


def test_alert_annotation_is_clean_in_context_and_audit_record():
    annotations = {"description": DSN_TEXT, "runbook": f"https:{U}{U}a:{SECRET}@w"}
    assert SECRET not in json.dumps(alert_context({"alertname": "A"}, annotations))
    alert = parse_alert(
        {
            "status": "firing",
            "labels": {"alertname": "A"},
            "annotations": annotations,
            "startsAt": "2026-10-26T11:41:54Z",
            "fingerprint": "f319",
        }
    )
    assert SECRET not in record_of(alert).alert_json


def test_the_question_reaches_the_model_redacted():
    from opspilot.investigation.context import initial_messages
    from tests.m1_investigation_support import assemble

    _, request, _, _, _, _ = assemble(replies=[])
    question = f"why? {DSN_TEXT} and https:{UU}{S}ops:{SECRET}@prom:9090"
    messages, _ = initial_messages(
        replace(request.as_input(), question=question), evidence_context=None
    )
    content = messages[1]["content"]
    assert SECRET not in content
    assert rf"postgres:\/\/{R}@db.internal:5432\/orders" in content
    assert rf"https:{UU}{S}{R}@prom:9090" in content


def test_prometheus_400_envelope_error_with_nested_escapes_is_redacted():
    from tests.test_311_source_errors_impl import _metrics

    # ``error`` holds a JSON document: in the body its slashes are \\\/
    inner = json.dumps({"dsn": f"https://u:{SECRET}@prom:9090/api"}).replace("/", S)
    envelope = json.dumps(
        {"status": "error", "errorType": "bad_data", "error": f"bad: {inner}"}
    ).encode()
    assert SECRET.encode() in envelope
    server = _serve(400, envelope)
    try:
        outcome = _metrics(f"http://127.0.0.1:{server.server_port}", "up")
    finally:
        server.shutdown()
        server.server_close()
    error = outcome.model_view["source_error"]["error"]
    assert SECRET not in json.dumps(outcome.model_view)
    assert rf"https:\/\/{R}@prom:9090\/api" in error


def test_a_span_log_value_holding_json_is_redacted_in_view_and_evidence(
    monkeypatch,
):
    from opspilot.investigation.inputs import ToolFace
    from opspilot.investigation.loop import DISCIPLINE_VARIANT
    from opspilot.persistence import Lease
    from opspilot.tools import ToolRequest
    from opspilot.tools.otel_demo import (
        TARGET_ID,
        TOOL_SCHEMAS,
        TRACES_TOOL,
        OtelDemoConfig,
        otel_demo_executor_factory,
    )
    from tests.acceptance.f7_hostile_support import jaeger_payload
    from tests.m1_tool_support import FakeClock, RecordingSink
    from tests.test_m1_otel_demo_contract import (
        JAEGER_URL,
        NOW,
        PROMETHEUS_URL,
        WINDOW,
        FakeOpener,
        FakeStore,
    )

    log_value = (
        f'{{"dsn":"postgres:{S}{S}svc:{SECRET}@db{S}x","alt":"{U}{U}a:{SECRET}@h"}}'
    )
    body = jaeger_payload({"key": "exception.message", "value": log_value})
    assert SECRET.encode() in body
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=1,
        global_suspension_generation=1,
        target_suspension_generation=1,
    )
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: FakeOpener(routes={"/api/traces": body}),
    )
    sink = RecordingSink()
    factory = otel_demo_executor_factory(
        store,
        evidence=sink,
        clock=FakeClock(start=NOW),
        config=OtelDemoConfig(prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL),
    )
    face = ToolFace(
        tool_schemas=TOOL_SCHEMAS,
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=lambda run_id: {
            "type": "opspilot-evidence-context-v4",
            "run_id": run_id,
            "time_policies": [
                {
                    "id": "policy-window-1",
                    "mode": "historical_window",
                    "all_authorized_targets": True,
                    "window": WINDOW.as_json(),
                    "reference_rule": "response_received_at",
                }
            ],
        },
    )
    run_input = face.input_for(
        run_id=str(lease.run_id),
        question="Why is checkout erroring?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )
    outcome = factory(lease, run_input).execute(
        ToolRequest(
            step_id="step-1",
            tool_index=0,
            tool_name=TRACES_TOOL,
            target_ref=TARGET_ID,
            params={"service": "checkout"},
            window=WINDOW.as_json(),
        )
    )
    assert outcome.status == "ok", outcome.model_view
    record = outcome.evidence
    assert record is not None
    for text in (
        json.dumps(outcome.model_view),
        record.raw.decode(),
        json.dumps(record.view),
        *(r.raw.decode() for r in sink.records),
    ):
        assert SECRET not in text
    assert R in json.dumps(outcome.model_view)


# -- revisions -------------------------------------------------------------------


def test_projection_and_fixture_revisions_are_bumped():
    from opspilot.tools import fixture
    from opspilot.tools.outcomes import PROJECTION_REVISION

    assert PROJECTION_REVISION == "m1-01-tool-view-v11"
    assert fixture.TOOL_SCHEMA_REVISION == "fixture-5"


# -- compatibility: rows stored before #319 --------------------------------------


def test_old_tool_evidence_is_display_redacted_and_keeps_its_bytes():
    from opspilot.web.app import _raw_fields

    inner = f'{{"dsn":"postgres:{S}{S}svc:{SECRET}@db{S}x"}}'
    raw = json.dumps({"logs": [{"value": inner}]}).encode()
    shown, changed = _raw_fields(raw)
    assert changed is True
    assert SECRET not in shown["raw_utf8"] and R in shown["raw_utf8"]


def test_old_observer_reading_is_display_redacted_and_hash_verified():
    import base64
    from hashlib import sha256

    from opspilot.web.app import _recovery_reading

    page = f"<pre>postgres:{U}{U}alice:{SECRET}@db</pre>".encode()
    raw = json.dumps(
        {"latency": {"body_b64": base64.b64encode(page).decode()}}
    ).encode()
    found = {
        "session_id": "session",
        "target": {},
        "sample": {
            "sample_id": "sample",
            "sequence": 1,
            "disposition": "healthy",
            "readings_consistent": True,
        },
        "reading": {
            "raw": raw,
            "raw_sha256": sha256(raw).hexdigest(),
            "signal_name": "latency",
            "status": "failed",
            "value": None,
            "sample_count": 0,
            "query": "up",
            "window_start": AT,
            "window_end": AT,
            "source": "fixture",
        },
    }
    displayed = _recovery_reading("evidence", found)
    assert displayed["hashes_verified"] is True
    assert displayed["display_redacted"] is True
    shown = json.loads(displayed["raw_utf8"])["latency"]["body_b64"]
    assert SECRET.encode() not in base64.b64decode(shown)
    assert found["reading"]["raw"] is raw
