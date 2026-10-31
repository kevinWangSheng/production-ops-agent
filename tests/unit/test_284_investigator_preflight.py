"""#284 / V2-R9: the live driver's Runs get the investigation side's
Prometheus account, and the preflight proves it before any setup.

The account comes from the environment or the private env file
``kind_lab.py up`` writes; the preflight refuses (``ENV_UNCONFIRMED``, no
external write) when it is absent or one bounded read-only query with it is
not answered 200, and records the result in ``lifecycle.json`` without the
credential value. The fake Prometheus here authenticates like the lab one.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from opspilot.tools import otel_demo
from scripts import eval_live_investigator as investigator
from scripts.eval_live_lab import LabError
from tests.acceptance import eval_c_support as s

USER, PASSWORD = "test-user", "test-pass"
USER_VAR, PASSWORD_VAR = (
    "OPSPILOT_OTEL_PROMETHEUS_USERNAME",
    "OPSPILOT_OTEL_PROMETHEUS_PASSWORD",
)


class FakePrometheus:
    """Answers ``/api/v1/query`` 200 for the investigator account, else 401."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server hook
                header = self.headers.get("Authorization")
                owner.requests.append(("GET", self.path, header))
                if header != expected:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps(
                    {
                        "status": "success",
                        "data": {"resultType": "vector", "result": []},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - http.server hook
                owner.requests.append(("POST", self.path, None))
                self.send_response(405)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def prometheus():
    fake = FakePrometheus()
    yield fake
    fake.close()


@pytest.fixture
def h(tmp_path):
    return s.Harness(tmp_path)


def config(url: str, password: str | None = PASSWORD) -> otel_demo.OtelDemoConfig:
    if password is None:
        return otel_demo.OtelDemoConfig(prometheus_url=url)
    return otel_demo.OtelDemoConfig(
        prometheus_url=url, prometheus_username=USER, prometheus_password=password
    )


def env_file(tmp_path, mode: int = 0o600):
    path = tmp_path / "prometheus-investigator.env"
    path.write_text(f"{USER_VAR}={USER}\n{PASSWORD_VAR}={PASSWORD}\n")
    path.chmod(mode)
    return path


# -- the Run's credential -------------------------------------------------------


def _prepare(monkeypatch, tmp_path, env_path, **env: str):
    from scripts import m1_live_flash_loop

    key_file = tmp_path / "model.env"
    key_file.write_text("DEEPSEEK_API_KEY=test-key\n")
    for name in (USER_VAR, PASSWORD_VAR, "OPSPILOT_OTEL_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("M0_ENV_FILE", str(key_file))
    monkeypatch.setenv("OPSPILOT_TRACE", "lab")
    monkeypatch.setenv("OPSPILOT_TOOL_PROFILE", "otel-demo")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(m1_live_flash_loop, "load_langsmith_env", lambda path: [])
    monkeypatch.setattr(investigator, "configure_lab_round", lambda env, rid: None)
    monkeypatch.setattr(investigator.tracing, "check_lab_target", lambda env: None)
    return investigator.prepare_round("canary-round", investigator_env_file=env_path)


def test_run_gets_investigator_account_from_private_env_file(monkeypatch, tmp_path):
    session = _prepare(monkeypatch, tmp_path, env_file(tmp_path))
    assert session.config.prometheus_basic_auth == (USER, PASSWORD)
    assert PASSWORD not in repr(session)


def test_environment_account_wins_over_env_file(monkeypatch, tmp_path):
    session = _prepare(
        monkeypatch,
        tmp_path,
        env_file(tmp_path),
        **{USER_VAR: "env-user", PASSWORD_VAR: "env-pass"},
    )
    assert session.config.prometheus_basic_auth == ("env-user", "env-pass")


def test_env_file_readable_by_others_is_refused(monkeypatch, tmp_path):
    with pytest.raises(LabError) as error:
        _prepare(monkeypatch, tmp_path, env_file(tmp_path, mode=0o644))
    assert error.value.code == "ENV_UNCONFIRMED"


def test_missing_env_file_leaves_run_without_account(monkeypatch, tmp_path):
    session = _prepare(monkeypatch, tmp_path, tmp_path / "absent.env")
    assert session.config.prometheus_basic_auth is None


# -- the probe against an authenticated fake Prometheus ---------------------------


def test_probe_is_one_bounded_read_only_query_answered_200(prometheus):
    source = investigator.InvestigatorSource(config(prometheus.url))
    assert source.credential() == "basic"
    assert source.probe() == 200
    assert len(prometheus.requests) == 1
    method, path, _ = prometheus.requests[0]
    parsed = urllib.parse.urlsplit(path)
    assert method == "GET"
    assert parsed.path == "/api/v1/query"
    assert urllib.parse.parse_qs(parsed.query)["query"] == [investigator.SOURCE_QUERY]


def test_probe_with_wrong_password_reports_401(prometheus):
    source = investigator.InvestigatorSource(config(prometheus.url, "wrong-pass"))
    assert source.probe() == 401


def test_absent_account_is_reported_without_request(prometheus):
    source = investigator.InvestigatorSource(config(prometheus.url, None))
    assert source.credential() == "absent"
    assert prometheus.requests == []


# -- the driver preflight -------------------------------------------------------


def _with_source(h, source):
    from dataclasses import replace

    begun: list[str] = []
    h.investigator.begin_round = begun.append
    h.deps = replace(h.deps, investigator_source=source)
    return begun


def _assert_zero_writes(h, begun):
    assert not h.public().exists()
    assert not list(h.deps.evidence_root.glob("**/attempt.json"))
    assert not h.pg.started
    assert not h.flags.writes
    assert not h.investigator.requests
    assert begun == []


def test_absent_account_refuses_before_any_lab_call(h, prometheus, capsys, monkeypatch):
    begun = _with_source(
        h, investigator.InvestigatorSource(config(prometheus.url, None))
    )
    monkeypatch.setattr(
        h.identity, "verify", lambda: pytest.fail("lab identity called")
    )
    code, result = h.final(capsys)
    assert (code, result["status"], result["reason"]) == (
        3,
        "refused",
        "ENV_UNCONFIRMED",
    )
    assert prometheus.requests == []
    _assert_zero_writes(h, begun)


def test_probe_not_200_refuses_with_zero_external_writes(h, prometheus, capsys):
    begun = _with_source(
        h, investigator.InvestigatorSource(config(prometheus.url, "wrong-pass"))
    )
    code, result = h.final(capsys)
    assert (code, result["status"], result["reason"]) == (
        3,
        "refused",
        "ENV_UNCONFIRMED",
    )
    assert [request[0] for request in prometheus.requests] == ["GET"]
    _assert_zero_writes(h, begun)


def test_probe_runs_before_setup_and_lifecycle_records_it(h, prometheus, capsys):
    order: list[str] = []
    real = investigator.InvestigatorSource(config(prometheus.url))

    class Spy:
        def credential(self) -> str:
            return real.credential()

        def probe(self) -> int:
            order.append("probe")
            return real.probe()

    _with_source(h, Spy())
    h.investigator.begin_round = lambda round_id: order.append("begin_round")
    h.pg.callback = lambda directory, port: order.append("pg_start")
    h.final(capsys)
    assert order[:3] == ["probe", "begin_round", "pg_start"]
    raw = (h.public() / "lifecycle.json").read_text()
    assert json.loads(raw)["investigator_source"] == {
        "credential": "basic",
        "query": investigator.SOURCE_QUERY,
        "http_status": 200,
        "result": "confirmed",
    }
    for path in h.public().rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert PASSWORD not in text and USER not in text
