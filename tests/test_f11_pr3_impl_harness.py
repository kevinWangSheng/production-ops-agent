"""Implementer checks for the harness release webhook sender (F11 PR-3 P3-03/D6).

A local ``http.server`` stands in for the workbench so the sender is
exercised over real HTTP: exact request bytes, bearer header, retry only on
503/network failure, and a ledger that never holds the token.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from opspilot.observation.release_policy import canonical_json, sha256
from scripts import f11_u2_harness as harness

TOKEN = "release-token-for-harness-tests"
A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64
UID = "checkout-uid"
REGISTRY = {
    UID: {
        "integration_id": "lab",
        "cluster_uid": "cluster",
        "namespace": "otel-demo",
        "workload": "checkout",
        "health_profile_id": "otel-demo-checkout",
    }
}


def _release(*, watched: bool = True) -> dict:
    before_template = {"spec": {"containers": [{"name": "checkout"}]}}
    after_template = {
        "spec": {"containers": [{"name": "checkout", "env": [{"name": "X"}]}]}
    }
    release = dict(
        release_id="u2-configuration-001",
        released_at="2026-10-21T12:00:00+00:00",
        before_template=before_template,
        after_template=after_template,
        before_revision=dict(
            images={"checkout": A},
            pod_template_digest="sha256:"
            + sha256(canonical_json(before_template).encode()),
        ),
        before_pod_template_hash="aaa",
        before_replicaset_uid="rs-before",
        deployment_uid="deploy-uid",
        deployment_generation=7,
    )
    if watched:
        release.update(
            after_revision=dict(
                images={"checkout": B},
                pod_template_digest="sha256:"
                + sha256(canonical_json(after_template).encode()),
            ),
            after_pod_template_hash="bbb",
            after_replicaset_uid="rs-after",
        )
    return release


@pytest.fixture
def ledger(tmp_path: Path) -> tuple[Path, Path]:
    run = tmp_path / "run"
    run.mkdir()
    (run / "release.json").write_text(json.dumps(_release()))
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(REGISTRY))
    return run, registry


class _Server:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.requests: list[tuple[dict[str, str], bytes]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                owner.requests.append(
                    (dict(self.headers.items()), self.rfile.read(length))
                )
                status = owner.statuses.pop(0)
                body = json.dumps({"code": f"S{status}"}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/intake/version-changes"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_payload_requires_watched_after_identity() -> None:
    with pytest.raises(ValueError, match="RELEASE_IDENTITY_INCOMPLETE_RUN_WATCH_FIRST"):
        harness.release_webhook_payload(_release(watched=False), REGISTRY, UID)
    payload = harness.release_webhook_payload(_release(), REGISTRY, UID)
    assert payload["target"] == {
        "resource_uid": UID,
        "integration_id": "lab",
        "cluster_uid": "cluster",
        "namespace": "otel-demo",
        "workload": "checkout",
    }
    assert payload["auxiliary"]["after_replicaset_uid"] == "rs-after"
    assert payload["auxiliary"]["deployment_generation"] == 7
    assert payload["source_release_id"] == "u2-configuration-001"
    with pytest.raises(ValueError, match="TARGET_NOT_REGISTERED"):
        harness.release_webhook_payload(_release(), REGISTRY, "other")


def test_retries_503_then_records_created(ledger) -> None:
    run, registry = ledger
    server = _Server([503, 201])
    try:
        sent = harness.send_release_webhook(
            run, registry, UID, server.url, TOKEN, sleep=lambda _: None
        )
    finally:
        server.close()
    assert [a["status"] for a in sent["attempts"]] == [503, 201]
    assert sent["status"] == 201 and sent["actor"] == "release-deployer"
    headers, body = server.requests[0]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert server.requests[1][1] == body
    assert sent["request_sha256"] == sha256(body)
    assert (run / sent["request_path"]).read_bytes() == body
    assert sent["response_sha256"] == sha256(json.dumps({"code": "S201"}).encode())
    directory = (run / sent["request_path"]).parent
    assert (
        sha256((directory / "response-2.body").read_bytes()) == sent["response_sha256"]
    )
    timeline = [
        json.loads(line) for line in (run / "timeline.jsonl").read_text().splitlines()
    ]
    assert [e["kind"] for e in timeline] == ["release_webhook"] * 2
    for path in run.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text()


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422])
def test_final_refusals_are_not_retried(ledger, status) -> None:
    run, registry = ledger
    server = _Server([status, 201])
    try:
        sent = harness.send_release_webhook(
            run, registry, UID, server.url, TOKEN, sleep=lambda _: None
        )
    finally:
        server.close()
    assert [a["status"] for a in sent["attempts"]] == [status]
    assert len(server.requests) == 1


def test_network_failure_is_retried_and_bounded(ledger) -> None:
    run, registry = ledger
    server = _Server([])
    url = server.url
    server.close()
    sleeps: list[float] = []
    sent = harness.send_release_webhook(
        run, registry, UID, url, TOKEN, sleep=sleeps.append
    )
    assert [a["status"] for a in sent["attempts"]] == [None] * 3
    assert all(a["error"] for a in sent["attempts"])
    assert sleeps == [1, 2]
    assert sent["status"] is None


def test_unwatched_release_sends_nothing(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "release.json").write_text(json.dumps(_release(watched=False)))
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(REGISTRY))
    server = _Server([201])
    try:
        with pytest.raises(ValueError):
            harness.send_release_webhook(run, registry, UID, server.url, TOKEN)
    finally:
        server.close()
    assert server.requests == []
    assert not (run / "webhook").exists()


def test_token_file_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RELEASE_TOKEN_MISSING"):
        harness.release_deployer_token(tmp_path / "absent")
