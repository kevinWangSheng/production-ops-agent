"""Implementer checks for the PR-3 independent review fixes.

P2-1: a ``released_at`` that cannot be normalized to UTC is a payload error.
(a): a blank ``source_release_id`` is not an identity (P3-01).
(b): the harness never follows a redirect with the bearer token.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from opspilot.release_intake import ReleaseIntakeError, parse_release
from scripts import f11_u2_harness as harness
from tests.test_f11_pr3_impl_harness import REGISTRY, TOKEN, UID, _release

A = "sha256:" + "a" * 64


def _body(**overrides: object) -> bytes:
    payload = {
        "format_version": 1,
        "source": "review",
        "source_release_id": "r-1",
        "released_at": "2026-10-21T12:00:00Z",
        "target": {
            "resource_uid": "u",
            "integration_id": "i",
            "cluster_uid": "c",
            "namespace": "n",
            "workload": "w",
        },
        "before": {"images": {"c": A}, "pod_template_digest": A},
        "after": {"images": {"c": A}, "pod_template_digest": A},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_baseline_payload_parses() -> None:
    assert parse_release(_body()).event.source_release_id == "r-1"


@pytest.mark.parametrize(
    "released_at",
    ["9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+05:00"],
)
def test_released_at_outside_utc_range_is_400(released_at: str) -> None:
    with pytest.raises(ReleaseIntakeError) as caught:
        parse_release(_body(released_at=released_at))
    assert (caught.value.status, caught.value.code) == (400, "INVALID_RELEASE_PAYLOAD")


@pytest.mark.parametrize("value", [" ", "\t\n", "   "])
def test_blank_source_release_id_is_400(value: str) -> None:
    with pytest.raises(ReleaseIntakeError) as caught:
        parse_release(_body(source_release_id=value))
    assert (caught.value.status, caught.value.code) == (400, "INVALID_RELEASE_PAYLOAD")


class _Recorder(BaseHTTPRequestHandler):
    seen: list[dict[str, str]]
    location: str = ""
    code: int = 302

    def _answer(self) -> None:
        type(self).seen.append(dict(self.headers.items()))
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if type(self).location:
            self.send_response(type(self).code)
            self.send_header("Location", type(self).location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(201)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = _answer  # noqa: N815
    do_GET = _answer  # noqa: N815

    def log_message(self, *args: object) -> None:
        pass


def _serve(
    location: str = "", code: int = 302
) -> tuple[ThreadingHTTPServer, list[dict[str, str]]]:
    seen: list[dict[str, str]] = []
    handler = type(
        "H", (_Recorder,), {"seen": seen, "location": location, "code": code}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_harness_does_not_follow_redirects(tmp_path: Path, code: int) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "release.json").write_text(json.dumps(_release()))
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(REGISTRY))
    target, target_seen = _serve()
    elsewhere = f"http://127.0.0.1:{target.server_port}/stolen"
    origin, origin_seen = _serve(location=elsewhere, code=code)
    try:
        sent = harness.send_release_webhook(
            run,
            registry,
            UID,
            f"http://127.0.0.1:{origin.server_port}/intake/version-changes",
            TOKEN,
            sleep=lambda _: None,
        )
    finally:
        for server in (origin, target):
            server.shutdown()
            server.server_close()
    assert target_seen == []
    assert len(origin_seen) == 1
    assert [a["status"] for a in sent["attempts"]] == [code]
    assert sent["status"] == code
