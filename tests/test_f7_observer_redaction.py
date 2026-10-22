"""Independent F7 contract tests for Observer response redaction.

These tests exercise the public Observer source over a real loopback HTTP
socket.  They deliberately assert stored-facing bytes and fixed result fields;
they do not inspect implementation call order.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from opspilot.domain.evidence import QueryWindow
from opspilot.domain.subjects import SubjectRef
from opspilot.observation.release_policy import sha256
from opspilot.observation.release_samples import sample_fingerprint
from opspilot.observation.release_types import (
    ReleaseQueryResult,
    ReleaseSampleEvidence,
    ReleaseSampleLease,
)
from opspilot.observer.prometheus import RESPONSE_LIMIT_BYTES, PrometheusReadOnlySource
from opspilot.observer.release_evaluation import parse_vector
from opspilot.observer.sampler import replay_sample, take_sample
from tests.test_m1_observer import PROFILE, FakeStore, lease

SECRET = "-".join(("fake", "observer", "credential", "42"))
AT = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _vector(*, value: float = 0.5, secret: bool = False, rows: int = 1) -> bytes:
    labels = {"service": "checkout"}
    if secret:
        labels.update(
            {
                "url": f"https://user:{SECRET}@example.invalid/x?token={SECRET}",
                "session": SECRET,
                "api_key": SECRET,
            }
        )
    result = [
        {"metric": dict(labels), "value": [AT.timestamp(), str(value)]}
        for _ in range(rows)
    ]
    return json.dumps(
        {"status": "success", "data": {"resultType": "vector", "result": result}},
        separators=(",", ":"),
    ).encode()


def _collision_vector() -> bytes:
    """Two series collapse to one identity after credential redaction."""
    result = [
        {"metric": {"session": SECRET + "-a"}, "value": [AT.timestamp(), "0.5"]},
        {"metric": {"session": SECRET + "-b"}, "value": [AT.timestamp(), "0.6"]},
    ]
    return json.dumps(
        {"status": "success", "data": {"resultType": "vector", "result": result}},
        separators=(",", ":"),
    ).encode()


def _large_body() -> bytes:
    """A valid body below 30 KiB whose credential field expands on redaction."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"api_key": "x", "filler": ""},
                    "value": [AT.timestamp(), "0.5"],
                }
            ],
        },
    }
    filler = ""
    while True:
        payload["data"]["result"][0]["metric"]["filler"] = filler
        body = json.dumps(payload, separators=(",", ":")).encode()
        if len(body) >= RESPONSE_LIMIT_BYTES - 2:
            # The short ``api_key`` value is still inside the transport limit;
            # only replacing it with the fixed marker crosses the boundary.
            return body
        filler += "x"


class _ResponseServer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                request_query = parse_qs(urlsplit(self.path).query).get("query", [""])[
                    0
                ]
                case = parse_qs(request_query).get("case", ["clean"])[0]
                if case in ("error", "clean_error"):
                    body = (
                        "provider rejected Authorization: Bearer "
                        + (SECRET if case == "error" else "clean-placeholder")
                    ).encode()
                    self.send_response(401)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if case in ("nonutf8", "nonutf8_clean"):
                    body = b"\xff\xfe" + (
                        SECRET.encode() if case == "nonutf8" else b"clean-body"
                    )
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if case in ("partial", "partial_clean"):
                    body = _vector(secret=case == "partial")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body) + 17))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                    try:
                        self.connection.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                if case in ("timeout", "timeout_clean"):
                    body = (
                        f"token={SECRET};timeout"
                        if case == "timeout"
                        else "token=clean-placeholder;timeout"
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body) + 17))
                    self.end_headers()
                    # Send the complete recognizable prefix, then leave the
                    # declared tail missing so the client still takes the
                    # timeout/partial-body path.
                    self.wfile.write(body)
                    self.wfile.flush()
                    time.sleep(2.0)
                    return
                if case == "collision":
                    body = _collision_vector()
                elif case == "large":
                    body = _large_body()
                else:
                    body = _vector(secret=case == "secret")
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


@pytest.fixture
def source_server():
    server = _ResponseServer()
    try:
        yield server
    finally:
        server.close()


def _source(server: _ResponseServer) -> PrometheusReadOnlySource:
    return PrometheusReadOnlySource(server.url)


def _no_secret(value: object) -> None:
    rendered = repr(value)
    assert SECRET not in rendered


def test_instant_and_vector_redact_response_bytes_without_changing_verdict_fields(
    source_server: _ResponseServer,
) -> None:
    source = _source(source_server)
    clean = source.instant("case=clean", at=AT, timeout_seconds=1)
    secret = source.instant("case=secret", at=AT, timeout_seconds=1)
    assert (
        secret.status,
        secret.value,
        secret.http_status,
        secret.detail,
        secret.body_complete,
    ) == (
        clean.status,
        clean.value,
        clean.http_status,
        clean.detail,
        clean.body_complete,
    )
    _no_secret(secret.body)

    vector = source.instant_vector("case=secret", at=AT, timeout_seconds=1)
    clean_vector = source.instant_vector("case=clean", at=AT, timeout_seconds=1)
    assert (vector.status, vector.http_status, vector.detail, vector.body_complete) == (
        clean_vector.status,
        clean_vector.http_status,
        clean_vector.detail,
        clean_vector.body_complete,
    )
    assert len(vector.series) == len(clean_vector.series) == 1
    assert [(s.timestamp, s.value) for s in vector.series] == [
        (s.timestamp, s.value) for s in clean_vector.series
    ]
    _no_secret(vector.body)
    _no_secret(vector.series)


@pytest.mark.parametrize(
    ("secret_case", "clean_case"),
    [
        ("error", "clean_error"),
        ("partial", "partial_clean"),
        ("timeout", "timeout_clean"),
        ("nonutf8", "nonutf8_clean"),
    ],
)
def test_error_partial_timeout_and_non_utf8_bodies_close_without_secret_leaks(
    source_server: _ResponseServer, secret_case: str, clean_case: str
) -> None:
    source = _source(source_server)
    result = source.instant(f"case={secret_case}", at=AT, timeout_seconds=1)
    clean = source.instant(f"case={clean_case}", at=AT, timeout_seconds=1)
    assert (result.status, result.http_status, result.detail, result.body_complete) == (
        clean.status,
        clean.http_status,
        clean.detail,
        clean.body_complete,
    )
    _no_secret(result.body)
    if secret_case == "nonutf8":
        assert result.status == "failed" and result.body == b""


def test_clean_bytes_are_byte_for_byte_unchanged(
    source_server: _ResponseServer,
) -> None:
    source = _source(source_server)
    expected = _vector(secret=False)
    result = source.instant("case=clean", at=AT, timeout_seconds=1)
    assert result.body == expected


def test_redaction_growth_over_limit_is_failed_without_throwing(
    source_server: _ResponseServer,
) -> None:
    result = _source(source_server).instant("case=large", at=AT, timeout_seconds=1)
    assert (result.status, result.detail, result.body_complete) == (
        "failed",
        "TOO_LARGE",
        False,
    )
    _no_secret(result.body)


def test_recovery_judgment_and_offline_replay_match_for_redacted_input(
    source_server: _ResponseServer,
) -> None:
    # The fake store is the public sampler seam; the HTTP source is still the
    # real socket path used above.  The two answer sets differ only in labels.
    source = _source(source_server)
    answers = {}
    for signal in PROFILE.signals:
        for kind, expr in (
            ("query", signal.query),
            ("coverage", signal.coverage_query),
            ("freshness", signal.freshness_query),
        ):
            case = "secret" if kind == "query" else "clean"
            # The server selects the case from the expression query string.
            answers[expr] = case

    class RoutedSource:
        def instant(self, expr, *, at, timeout_seconds):
            return source.instant(
                f"case={answers[expr]}", at=at, timeout_seconds=timeout_seconds
            )

    store = FakeStore()
    taken = take_sample(lease(), PROFILE, RoutedSource(), store)
    assert taken.evaluation is not None
    _, sample, readings = store.submitted[0]
    rows = [reading.model_dump(mode="python") for reading in readings]
    replayed, evaluation = replay_sample(PROFILE, rows)
    assert evaluation.outcome == taken.evaluation.outcome
    assert [(r.status, r.value, r.sample_count) for r in replayed] == [
        (r.status, r.value, r.sample_count) for r in readings
    ]
    for reading in readings:
        _no_secret(reading.raw)
        bundle = json.loads(reading.raw)
        for kind in ("query", "coverage", "freshness"):
            _no_secret(base64.b64decode(bundle[kind]["body_b64"]))
        assert hashlib.sha256(reading.raw).hexdigest() == reading.raw_sha256


def test_release_parser_closes_credential_label_collision_as_query_failed(
    source_server: _ResponseServer,
) -> None:
    result = _source(source_server).instant_vector(
        "case=collision", at=AT, timeout_seconds=1
    )
    evidence = ReleaseQueryResult(
        query_key="mapping:pod_owner:query",
        query="vector(1)",
        source="prometheus",
        evaluated_at=AT,
        sent_at=AT,
        status=result.status,
        http_status=result.http_status,
        detail=result.detail,
        body_complete=result.body_complete,
        raw=result.body,
        raw_sha256=hashlib.sha256(result.body).hexdigest(),
    )
    reasons: set[str] = set()
    parse_vector(evidence, reasons)
    _no_secret(result.body)
    assert "QUERY_FAILED:mapping:pod_owner:query" in reasons


def test_release_fingerprint_is_stable_for_same_evidence() -> None:
    raw = _vector(secret=False)
    query = ReleaseQueryResult(
        query_key="context:error_ratio:query",
        query="vector(1)",
        source="prometheus",
        evaluated_at=AT,
        sent_at=AT,
        status="ok",
        http_status=200,
        detail="",
        body_complete=True,
        raw=raw,
        raw_sha256=sha256(raw),
    )
    observation_id = uuid4()
    lease_value = ReleaseSampleLease(
        session_id=uuid4(),
        observation_id=observation_id,
        purpose="release_observation",
        subject=SubjectRef(kind="release_observation", id=str(observation_id)),
        job_id=uuid4(),
        sequence=1,
        owner=uuid4(),
        epoch=1,
        lease_until=AT,
        subject_control_generation=0,
        observation_generation=1,
        health_profile_revision="r",
        calibrated_policy_revision="p",
        deadline_at=AT,
        sample_interval_seconds=1,
        adopted_window_end=None,
        global_suspension_generation=0,
        target_suspension_generation=0,
    )
    ev = ReleaseSampleEvidence(
        format_version=1,
        sample_id=uuid4(),
        window=QueryWindow(start=AT, end=AT.replace(microsecond=1)),
        queries=(query,),
    )
    assert sample_fingerprint(lease_value, ev) == sample_fingerprint(lease_value, ev)
