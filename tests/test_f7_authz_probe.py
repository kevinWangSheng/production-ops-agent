"""Exercise the evidence collector against a real local HTTP server."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts import f7_authz_probe as probe
from scripts import kind_lab


@pytest.mark.parametrize(
    "change,wrong_status", [(False, False), (True, False), (False, True)]
)
def test_collector_detects_failures_without_printing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: bool,
    wrong_status: bool,
) -> None:
    accounts = {r: f"canary-{r}-password" for r in kind_lab.ROLE_ENV_VARS}
    for role in accounts:
        kind_lab.role_env_file(role, accounts, tmp_path / f"prometheus-{role}.env")
    reads = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def handle_request(self) -> None:
            nonlocal reads
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query).get("query", [""])[0]
            auth = self.headers.get("Authorization")
            if query == "opspilot_f7_probe[30d]":
                reads += 1
                timestamp = 123 + (1 if change and reads > 1 else 0)
                body = json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "resultType": "matrix",
                            "result": [
                                {
                                    "metric": {"__name__": "opspilot_f7_probe"},
                                    "values": [[timestamp, "1"]],
                                }
                            ],
                        },
                    }
                ).encode()
                status = 200
            else:
                body = b""
                status = (
                    401
                    if not auth
                    else (
                        200
                        if (self.command, parsed.path) in probe.READ_REQUESTS
                        else 403
                    )
                )
                if wrong_status and self.command == "POST" and auth:
                    status = 200
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        original = probe.probe
        monkeypatch.setattr(probe, "probe", lambda **kw: original(base, **kw))
        assert probe.main(["--env-dir", str(tmp_path)]) == int(change or wrong_status)
        output = capsys.readouterr().out
        report = json.loads(output)
        assert report["sample_check"]["matches"] is (not change)
        assert report["ok"] is (not change and not wrong_status)
        assert len(report["requests"]) == 3 * (
            len(probe.READ_REQUESTS) + len(probe.DENIED_REQUESTS)
        )
        assert all(
            r["expected"] == 401
            for r in report["requests"]
            if r["account"] == "anonymous"
        )
        for secret in accounts.values():
            assert secret not in output
        for role in accounts:
            assert kind_lab.basic_auth_header(accounts, role) not in output
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_missing_credentials_are_sanitized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert probe.main(["--env-dir", str(tmp_path)]) == 1
    assert (
        json.loads(capsys.readouterr().out)["error"] == "cannot read role credentials"
    )
