"""Separate read-only Prometheus credentials per role (M1-02 step 4, D3).

C3 §3: isolation must reach credentials, not only processes. The lab
Prometheus authenticates every request (``scripts/kind_lab.py`` provisions
one basic-auth account per role); the Observer reads only
``OPSPILOT_OBSERVER_PROMETHEUS_*`` and the investigation side only
``OPSPILOT_OTEL_PROMETHEUS_*`` -- each ignores the other's variables even
when present, and an anonymous or wrong credential is refused by the source.
Prometheus' built-in basic auth cannot grant different rights per account;
the contract asks for separate credentials, and that is what is tested.
"""

import base64
import json
import os
import stat
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from opspilot.observer.__main__ import build_loop
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    TRACES_TOOL,
    OtelDemoConfig,
    OtelDemoTransport,
)
from opspilot.tools.outcomes import Window
from scripts import kind_lab

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
OBSERVER_VARS = {
    "OPSPILOT_OBSERVER_PROMETHEUS_USERNAME": "observer",
    "OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD": "observer-pw",
}
INVESTIGATOR_VARS = {
    "OPSPILOT_OTEL_PROMETHEUS_USERNAME": "investigator",
    "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": "investigator-pw",
}
EMPTY_VECTOR = json.dumps(
    {"status": "success", "data": {"resultType": "vector", "result": []}}
).encode()


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.fixture(scope="module")
def prometheus_stub():
    """An HTTP server that answers ``/api/v1/query`` only for two accounts."""
    accepted = {
        _basic("observer", "observer-pw"),
        _basic("investigator", "investigator-pw"),
    }
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            auth = self.headers.get("Authorization")
            seen.append(auth)
            if auth not in accepted:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="prometheus"')
                self.end_headers()
                self.wfile.write(b"Unauthorized\n")
                return
            if self.path.startswith("/api/v1/query_range"):
                body = json.dumps(
                    {
                        "status": "success",
                        "data": {"resultType": "matrix", "result": []},
                    }
                ).encode()
            else:
                body = EMPTY_VECTOR
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()


# --- Observer side


def test_observer_reads_only_its_own_credential_and_ignores_the_investigators(
    prometheus_stub,
):
    url, seen = prometheus_stub
    env = {
        "OPSPILOT_OBSERVER_DSN": "host=db dbname=opspilot user=observer",
        "OPSPILOT_OBSERVER_PROMETHEUS_URL": url,
        **INVESTIGATOR_VARS,
        "OPSPILOT_OTEL_TOKEN": "investigator-token",
    }
    # the investigator's credential is present but is not the Observer's:
    # the request goes out anonymous and the source refuses it
    loop = build_loop(env, stop=threading.Event())
    seen.clear()
    result = loop.source.instant("up", at=NOW, timeout_seconds=5)
    loop.store.close()
    assert seen == [None]
    assert (result.status, result.http_status, result.detail) == ("failed", 401, "HTTP")
    # with its own account the same query is answered
    loop = build_loop({**env, **OBSERVER_VARS}, stop=threading.Event())
    seen.clear()
    result = loop.source.instant("up", at=NOW, timeout_seconds=5)
    loop.store.close()
    assert seen == [_basic("observer", "observer-pw")]
    assert result.status == "no_data" and result.http_status == 200


def test_observer_credential_may_come_from_its_private_env_file(
    tmp_path, prometheus_stub
):
    url, seen = prometheus_stub
    env_file = tmp_path / "observer.env"
    env_file.write_text(
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME=investigator\n"
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD=investigator-pw\n"
        "OPSPILOT_OBSERVER_PROMETHEUS_USERNAME=observer\n"
        "OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD='observer-pw'\n"
    )
    loop = build_loop(
        {
            "OPSPILOT_OBSERVER_DSN": "host=db dbname=opspilot user=observer",
            "OPSPILOT_OBSERVER_PROMETHEUS_URL": url,
            "OPSPILOT_OBSERVER_ENV_FILE": str(env_file),
        },
        stop=threading.Event(),
    )
    seen.clear()
    assert loop.source.instant("up", at=NOW, timeout_seconds=5).status == "no_data"
    loop.store.close()
    assert seen == [_basic("observer", "observer-pw")]
    with pytest.raises(SystemExit, match="go together"):
        build_loop(
            {
                "OPSPILOT_OBSERVER_DSN": "host=db dbname=opspilot user=observer",
                "OPSPILOT_OBSERVER_PROMETHEUS_URL": url,
                "OPSPILOT_OBSERVER_PROMETHEUS_USERNAME": "observer",
            },
            stop=threading.Event(),
        )


def test_a_wrong_observer_password_is_refused_by_the_source(prometheus_stub):
    url, seen = prometheus_stub
    source = PrometheusReadOnlySource(url, basic_auth=("observer", "wrong"))
    seen.clear()
    result = source.instant("up", at=NOW, timeout_seconds=5)
    assert result.http_status == 401 and result.status == "failed"
    assert seen == [_basic("observer", "wrong")]


# --- investigation side


def _metrics_request(url: str) -> object:
    from opspilot.tools.executor import TransportRequest

    window = Window(start=NOW - timedelta(minutes=5), end=NOW)
    return TransportRequest(
        operation_id="op-1",
        source="otel-demo",
        verb="query",
        endpoint=url,
        selector={"traces_endpoint": f"{url}/jaeger/ui"},
        params={"expr": "up", "step_seconds": 15},
        window=window,
        timeout_seconds=5.0,
        max_result_bytes=65536,
        credential_ref=CREDENTIAL_REF,
        tool=METRICS_TOOL,
    )


def test_investigation_config_reads_only_its_own_variables():
    env = {**OBSERVER_VARS, "OPSPILOT_OBSERVER_PROMETHEUS_TOKEN": "obs-token"}
    config = OtelDemoConfig.from_env(env)
    assert config.prometheus_basic_auth is None and config.token is None
    config = OtelDemoConfig.from_env({**env, **INVESTIGATOR_VARS})
    assert config.prometheus_basic_auth == ("investigator", "investigator-pw")
    assert config.token is None
    # the password never renders
    assert "investigator-pw" not in repr(config)
    with pytest.raises(Exception, match="INVALID_CREDENTIAL"):
        OtelDemoConfig(prometheus_username="investigator")


def test_investigation_transport_sends_its_basic_auth_to_prometheus_only(
    prometheus_stub,
):
    url, seen = prometheus_stub
    transport = OtelDemoTransport(
        credentials={CREDENTIAL_REF: None},
        basic_auth={CREDENTIAL_REF: ("investigator", "investigator-pw")},
    )
    seen.clear()
    response = transport.fetch(_metrics_request(url))
    assert response.source_status in (None, "200")
    assert seen == [_basic("investigator", "investigator-pw")]
    # an anonymous transport is refused: the view records the source status
    anonymous = OtelDemoTransport(credentials={CREDENTIAL_REF: None})
    seen.clear()
    response = anonymous.fetch(_metrics_request(url))
    assert response.source_status == "401" and seen == [None]
    # the Observer's account is not something the investigation side holds:
    # it is only wired through its own config, which never read those variables
    config = OtelDemoConfig.from_env(
        {**OBSERVER_VARS, "OPSPILOT_OTEL_PROMETHEUS_URL": url}
    )
    assert config.prometheus_basic_auth is None


def test_traces_requests_stay_anonymous_even_with_a_prometheus_credential(
    prometheus_stub,
):
    url, seen = prometheus_stub
    transport = OtelDemoTransport(
        credentials={CREDENTIAL_REF: None},
        basic_auth={CREDENTIAL_REF: ("investigator", "investigator-pw")},
    )
    from dataclasses import replace

    request = replace(
        _metrics_request(url),
        tool=TRACES_TOOL,
        params={"service": "checkout", "limit": 5},
    )
    seen.clear()
    try:
        transport.fetch(request)
    except Exception:  # the stub is not Jaeger; only the header matters
        pass
    assert seen and seen[0] is None


# --- lab provisioning (engineer side)


def test_lab_web_config_lists_every_account_with_a_bcrypt_hash():
    accounts = {name: f"pw-{name}" for name in kind_lab.PROMETHEUS_ACCOUNTS}
    rendered = kind_lab.web_config_yaml(accounts, hasher=lambda p: f"$2y$10$HASH({p})")
    assert rendered.startswith("basic_auth_users:\n")
    for name in ("observer", "investigator", "collector", "lab"):
        assert f'  {name}: "$2y$10$HASH(pw-{name})"' in rendered
    assert kind_lab.bcrypt_hash("example").startswith("$2y$10$")


def test_lab_env_files_carry_one_role_and_are_private(tmp_path):
    accounts = {name: f"pw-{name}" for name in kind_lab.PROMETHEUS_ACCOUNTS}
    observer = kind_lab.role_env_file("observer", accounts, tmp_path / "o.env")
    investigator = kind_lab.role_env_file("investigator", accounts, tmp_path / "i.env")
    assert stat.S_IMODE(observer.stat().st_mode) == 0o600
    assert observer.read_text() == (
        "OPSPILOT_OBSERVER_PROMETHEUS_USERNAME=observer\n"
        "OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD=pw-observer\n"
    )
    assert investigator.read_text() == (
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME=investigator\n"
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD=pw-investigator\n"
    )
    assert "pw-investigator" not in observer.read_text()
    assert "pw-observer" not in investigator.read_text()


def test_lab_auth_file_is_created_private_and_a_loose_one_is_refused(tmp_path):
    path = tmp_path / "prometheus-auth.json"
    accounts = kind_lab.prometheus_accounts(path)
    assert set(accounts) == set(kind_lab.PROMETHEUS_ACCOUNTS)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(set(accounts.values())) == 4 and all(
        len(v) >= 24 for v in accounts.values()
    )
    assert kind_lab.prometheus_accounts(path) == accounts
    os.chmod(path, 0o644)
    with pytest.raises(SystemExit, match="group/world readable"):
        kind_lab.prometheus_accounts(path)


def test_lab_values_mount_the_web_config_and_authenticate_the_collector():
    values = (Path(kind_lab.__file__).parent / "kind_lab" / "values.yaml").read_text()
    assert "web.config.file=/etc/prometheus-web/web.yml" in values
    assert "secretName: prometheus-web-config" in values
    assert "tcpSocketProbeEnabled: true" in values
    assert (
        "basicauth/prometheus" in values
        and "${env:PROMETHEUS_COLLECTOR_PASSWORD}" in values
    )
    assert "name: prometheus-web-auth" in values
    # no password-looking literal in the committed values
    assert "password:" in values and "password: ${env:" in values
    for line in values.splitlines():
        if "password" in line.lower() and not line.strip().startswith("#"):
            assert "${env:" in line, line
    assert sys.version_info >= (3, 12)


# --- engineer scripts use the lab account (PR #119 follow-up)


def _engineer_stub():
    """A Prometheus stand-in that accepts only the ``lab`` account."""
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            auth = self.headers.get("Authorization")
            seen.append(auth)
            if auth != _basic("lab", "lab-pw"):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"Unauthorized\n")
                return
            body = json.dumps(
                {
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{"metric": {}, "value": [1.0, "1"]}],
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_lab_authorization_prefers_the_env_then_the_auth_file(tmp_path):
    assert kind_lab.lab_authorization({}, path=tmp_path / "missing.json") is None
    env = {
        "OPSPILOT_LAB_PROMETHEUS_USERNAME": "lab",
        "OPSPILOT_LAB_PROMETHEUS_PASSWORD": "lab-pw",
    }
    assert kind_lab.lab_authorization(env, path=tmp_path / "missing.json") == _basic(
        "lab", "lab-pw"
    )
    path = tmp_path / "prometheus-auth.json"
    accounts = kind_lab.prometheus_accounts(path)
    assert kind_lab.lab_authorization({}, path=path) == _basic("lab", accounts["lab"])
    # the lab role's env file carries only the lab variables
    env_file = kind_lab.role_env_file("lab", accounts, tmp_path / "lab.env")
    assert env_file.read_text() == (
        f"OPSPILOT_LAB_PROMETHEUS_USERNAME=lab\nOPSPILOT_LAB_PROMETHEUS_PASSWORD={accounts['lab']}\n"
    )


def test_otel_demo_observe_and_collect_baseline_authenticate_as_lab(
    monkeypatch, tmp_path
):
    """Both engineer scripts read the lab account through ``kind_lab``;
    without it the authenticated Prometheus refuses them (401 surfaces, it
    is not hidden), with it they get their data."""
    import importlib.util
    import urllib.error

    from scripts import otel_demo_observe

    spec = importlib.util.spec_from_file_location(
        "collect_baseline",
        Path(kind_lab.ROOT) / "docs/evidence/m1-02-health-profile/collect_baseline.py",
    )
    assert spec is not None and spec.loader is not None
    collect_baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collect_baseline)

    server, seen = _engineer_stub()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        # distinct prefixes on one stub host: the script tells Prometheus
        # from Jaeger by URL prefix
        monkeypatch.setattr(otel_demo_observe, "PROM", url + "/prometheus")
        monkeypatch.setattr(kind_lab, "AUTH_FILE", tmp_path / "missing.json")
        for name in (
            "OPSPILOT_LAB_PROMETHEUS_USERNAME",
            "OPSPILOT_LAB_PROMETHEUS_PASSWORD",
        ):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(urllib.error.HTTPError) as refused:
            otel_demo_observe.prom_instant("up", NOW)
        assert refused.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as refused:
            collect_baseline.get(url, "/api/v1/query", {"query": "up"})
        assert refused.value.code == 401
        assert seen == [None, None]
        seen.clear()
        monkeypatch.setenv("OPSPILOT_LAB_PROMETHEUS_USERNAME", "lab")
        monkeypatch.setenv("OPSPILOT_LAB_PROMETHEUS_PASSWORD", "lab-pw")
        assert otel_demo_observe.prom_instant("up", NOW) == [
            {"metric": {}, "value": [1.0, "1"]}
        ]
        assert (
            collect_baseline.get(url, "/api/v1/query", {"query": "up"})["status"]
            == "success"
        )
        assert seen == [_basic("lab", "lab-pw")] * 2
        # Jaeger requests from the observe script carry no Prometheus credential
        seen.clear()
        monkeypatch.setattr(otel_demo_observe, "JAEGER", url + "/jaeger")
        try:
            otel_demo_observe.get(url + "/jaeger/api/services")
        except urllib.error.HTTPError:
            pass
        assert seen == [None]
    finally:
        server.shutdown()


def test_htpasswd_never_sees_the_password_on_argv_or_in_errors(monkeypatch):
    """PR #119 recheck: the password goes to htpasswd on stdin; a timeout or
    failure is reported without it (TimeoutExpired quotes the command)."""
    import subprocess

    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **kwargs})
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(kind_lab.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as failure:
        kind_lab.bcrypt_hash("example-only-no-real-secret")
    assert "example-only-no-real-secret" not in str(failure.value)
    assert failure.value.__cause__ is None and failure.value.__suppress_context__
    assert calls and "example-only-no-real-secret" not in " ".join(calls[0]["cmd"])
    assert calls[0]["input"] == "example-only-no-real-secret\n"
    flags = calls[0]["cmd"][1]  # "-niBC": stdin (i), display (n), bcrypt (B)
    assert flags.startswith("-") and "i" in flags and "b" not in flags

    def failing_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(kind_lab.subprocess, "run", failing_run)
    with pytest.raises(SystemExit) as failure:
        kind_lab.bcrypt_hash("example-only-no-real-secret")
    assert "example-only-no-real-secret" not in str(failure.value)
