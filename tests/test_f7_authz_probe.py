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


def _read_status(method: str) -> int:
    # Prometheus' API answers HEAD with 405 after the proxy passes it.
    return 405 if method == "HEAD" else 200


def _proxy_status(role: str, method: str, path: str, body: str | None) -> int:
    """The proxy's intended answer to an authenticated request (#396 rules)."""
    if role == "holmes" and method == "POST" and body is not None:
        if path not in probe.HOLMES_FORM_PATHS:
            return 403
        return 413 if body == "form-oversize" else 200
    return _read_status(method) if (method, path) in probe.READ_REQUESTS else 403


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
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            form = self.headers.get("Content-Type") == (
                "application/x-www-form-urlencoded"
            )
            role = next(
                (
                    r
                    for r in accounts
                    if auth == kind_lab.basic_auth_header(accounts, r)
                ),
                "anonymous",
            )
            form_kind = (
                None
                if not form
                else "form-oversize"
                if len(raw) > probe.FORM_LIMIT_BYTES
                else "form"
            )
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
                    else _proxy_status(role, self.command, parsed.path, form_kind)
                )
                if wrong_status and self.command == "POST" and auth:
                    status = 200
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        do_GET = handle_request
        do_POST = handle_request
        do_HEAD = handle_request
        do_OPTIONS = handle_request

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
        assert len(report["requests"]) == 4 * (
            len(probe.READ_REQUESTS)
            + len(probe.DENIED_REQUESTS)
            + len(probe.VARIANT_REQUESTS)
        ) + len(probe.HOLMES_ROWS)
        assert {r["account"] for r in report["requests"]} == {
            "investigator",
            "observer",
            "lab",
            "anonymous",
            "holmes",
        }
        assert all(
            r["expected"] == 401
            for r in report["requests"]
            if r["account"] == "anonymous" and r["expected"] != "4xx"
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


@pytest.mark.parametrize(
    "account", ("investigator", "observer", "lab", "anonymous", "holmes")
)
@pytest.mark.parametrize("method,path", probe.DENIED_REQUESTS + probe.VARIANT_REQUESTS)
@pytest.mark.parametrize("bad_status", (200, 204))
def test_any_account_getting_2xx_on_a_forbidden_request_fails(
    monkeypatch: pytest.MonkeyPatch,
    account: str,
    method: str,
    path: str,
    bad_status: int,
) -> None:
    monkeypatch.setattr(probe, "authorization", lambda role, directory: role)
    monkeypatch.setattr(probe, "latest_samples", lambda base, auth: (200, {}))

    def request(
        base: str, verb: str, url_path: str, auth: str | None, body: str | None = None
    ) -> tuple[int, bytes]:
        requested_path = url_path.split("?", 1)[0]
        role = auth or "anonymous"
        if (role, verb, requested_path, body) == (account, method, path, None):
            return bad_status, b""
        if (verb, requested_path) in probe.VARIANT_REQUESTS:
            return 400, b""
        if role == "anonymous":
            return 401, b""
        return _proxy_status(role, verb, requested_path, body), b""

    monkeypatch.setattr(probe, "request", request)
    report = probe.probe()
    assert report["ok"] is False
    [failed] = [r for r in report["requests"] if not r["matches"]]
    assert (failed["account"], failed["method"], failed["path"], failed["status"]) == (
        account,
        method,
        path,
        bad_status,
    )


@pytest.mark.parametrize(
    "status,expected_ok",
    [
        (0, False),
        (200, False),
        (299, False),
        (301, False),
        (400, True),
        (403, True),
        (500, False),
        (503, False),
    ],
)
def test_variant_status_is_recorded_and_only_a_4xx_is_a_denial(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_ok: bool,
) -> None:
    monkeypatch.setattr(probe, "authorization", lambda role, directory: role)
    monkeypatch.setattr(probe, "latest_samples", lambda base, auth: (200, {}))

    def request(
        base: str, method: str, path: str, auth: str | None, body: str | None = None
    ) -> tuple[int, bytes]:
        bare_path = path.split("?", 1)[0]
        if (method, bare_path) in probe.VARIANT_REQUESTS:
            return status, b""
        if auth is None:
            return 401, b""
        return _proxy_status(auth, method, bare_path, body), b""

    monkeypatch.setattr(probe, "request", request)
    report = probe.probe()
    assert report["ok"] is expected_ok
    variants = [r for r in report["requests"] if r["expected"] == "4xx"]
    # four frozen accounts plus holmes, and holmes' form POST to /API/V1/QUERY
    assert len(variants) == 5 * 2 * 5 + 1
    assert {r["status"] for r in variants} == {status}


def test_holmes_rows_extend_the_frozen_matrix_without_changing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "authorization", lambda role, directory: role)
    monkeypatch.setattr(probe, "latest_samples", lambda base, auth: (200, {}))
    sent: list[tuple[str, str, str, str | None]] = []

    def request(
        base: str, method: str, path: str, auth: str | None, body: str | None = None
    ) -> tuple[int, bytes]:
        sent.append((auth or "anonymous", method, path, body))
        bare = path.split("?", 1)[0]
        if (method, bare) in probe.VARIANT_REQUESTS:
            return 400, b""
        if auth is None:
            return 401, b""
        return _proxy_status(auth, method, bare, body), b""

    monkeypatch.setattr(probe, "request", request)
    report = probe.probe()
    assert report["ok"] is True
    frozen = [r for r in report["requests"] if r["account"] != "holmes"]
    holmes = [r for r in report["requests"] if r["account"] == "holmes"]
    # the frozen accounts never send a body and keep POST /api/v1/query at 403
    assert all(body is None for role, *_, body in sent if role != "holmes")
    assert {
        r["expected"]
        for r in frozen
        if (r["method"], r["path"]) == ("POST", "/api/v1/query")
        and r["account"] != "anonymous"
    } == {403}
    allowed = {
        (r["method"], r["path"])
        for r in holmes
        if r.get("body") == "form" and r["expected"] == 200
    }
    assert allowed == {("POST", "/api/v1/query"), ("POST", "/api/v1/query_range")}
    denied = {r["path"] for r in holmes if r["expected"] == 403}
    assert {"/api/v1/metadata", "/api/v1/rules", "/api/v1/series"} <= denied
    assert any(
        r.get("body") == "form-oversize" and r["expected"] == 413 for r in holmes
    )
