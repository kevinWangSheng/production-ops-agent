"""#428 implementation tests without a database: the mode switch and its
fail-closed reason codes, OTLP identities, backoff, span construction, the
HTTP transport against a loopback endpoint, ``/metrics`` and the install
wiring. Delivery against PostgreSQL is in
``tests/integration/test_428_outbox_delivery_postgres.py``."""

from __future__ import annotations

import hashlib
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from opspilot import health, tracing
from opspilot.outbox_delivery import (
    ExportConfig,
    OutboxDelivery,
    backoff_seconds,
    root_span_id_for,
    span_id_for,
    trace_id_for,
    urllib_transport,
)
from opspilot.persistence.outbox import ClaimedRow
from tests.test_392_install_script_impl import host  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "deploy" / "docker-entrypoint.sh"
# Built at run time, not a credential-shaped literal (gitleaks).
SENTINEL_KEY = "lsv2_" + "pt_" + "0123abcd" * 4

GOOD = {
    "OPSPILOT_TRACE": "outbox",
    "LANGSMITH_API_KEY": SENTINEL_KEY,
    "LANGSMITH_PROJECT": "opspilot-lab-428",
}


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        (None, "off"),
        ("", "off"),
        ("off", "off"),
        ("lab", "lab"),
        ("outbox", "outbox"),
        ("OUTBOX", "off"),
        ("on", "off"),
    ],
)
def test_trace_mode_has_three_values_and_anything_else_is_off(raw, mode):
    env = {} if raw is None else {"OPSPILOT_TRACE": raw}
    assert tracing.trace_mode(env) == mode


def test_export_status_reports_the_mode_or_the_missing_preconditions():
    assert tracing.export_status({}) == ("TRACE_MODE_OFF",)
    assert tracing.export_status({**GOOD, "OPSPILOT_TRACE": "lab"}) == (
        "TRACE_MODE_LAB",
    )
    assert tracing.export_status(GOOD) == ("ENABLED",)
    # no data-source check (Q15): a non-loopback Prometheus is fine
    assert tracing.export_status(
        {**GOOD, "OPSPILOT_OTEL_PROMETHEUS_URL": "http://prometheus.example:9090"}
    ) == ("ENABLED",)
    assert tracing.export_status({"OPSPILOT_TRACE": "outbox"}) == (
        "PROJECT_NOT_LAB",
        "LANGSMITH_KEY_MISSING",
    )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"LANGSMITH_ENDPOINT": "https://smith.example.com"},
            "LANGSMITH_ENDPOINT_NOT_ALLOWED",
        ),
        (
            {"LANGSMITH_ENDPOINT": "http://api.smith.langchain.com"},
            "LANGSMITH_ENDPOINT_NOT_ALLOWED",
        ),
        ({"LANGSMITH_PROJECT": "opspilot-prod"}, "PROJECT_NOT_LAB"),
        ({"LANGSMITH_API_KEY": ""}, "LANGSMITH_KEY_MISSING"),
    ],
)
def test_each_missing_precondition_has_its_code_and_no_config(change, code):
    env = {**GOOD, **change}
    assert tracing.export_status(env) == (code,)
    assert ExportConfig.from_env(env) is None
    assert set(tracing.export_status(env)) <= tracing.EXPORT_STATUS_CODES


def test_config_targets_the_otlp_path_and_never_shows_the_key():
    config = ExportConfig.from_env(
        {**GOOD, "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com/"}
    )
    assert config is not None
    assert config.endpoint_url == "https://eu.api.smith.langchain.com/otel/v1/traces"
    assert SENTINEL_KEY not in repr(config)
    headers = config.headers()
    assert headers["x-api-key"] == SENTINEL_KEY
    assert headers["Langsmith-Project"] == "opspilot-lab-428"


def test_outbox_mode_installs_no_direct_tracer():
    try:
        assert isinstance(tracing.configure(GOOD), tracing.NullTracer)
    finally:
        tracing.reset()


def test_backoff_doubles_from_five_seconds_and_caps_at_ten_minutes():
    assert [backoff_seconds(n) for n in range(1, 8)] == [5, 10, 20, 40, 80, 160, 320]
    assert backoff_seconds(20) == 600


def test_ids_are_the_contract_hashes_and_stable():
    run_id = uuid4()
    key = f"model_step:{run_id}:3"
    sha = lambda text: hashlib.sha256(text.encode()).digest()  # noqa: E731
    assert trace_id_for("model_step", run_id, key) == int.from_bytes(
        sha(str(run_id))[:16], "big"
    )
    assert span_id_for(key) == int.from_bytes(sha(key)[:8], "big")
    attempt = uuid4()
    gen_key = f"generation:{attempt}"
    assert trace_id_for("generation", None, gen_key) == int.from_bytes(
        sha(f"generation:{attempt}")[:16], "big"
    )
    assert root_span_id_for(run_id) == int.from_bytes(sha(f"run:{run_id}")[:8], "big")
    assert span_id_for(key) == span_id_for(key)


def _row(kind="model_step", payload=None, **extra):
    run_id = extra.pop("run_id", uuid4())
    now = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    return ClaimedRow(
        entry_id=extra.pop("entry_id", 1),
        idempotency_key=extra.pop("key", f"{kind}:{run_id}:1"),
        incident_id=uuid4(),
        run_id=None if kind == "generation" else run_id,
        kind=kind,
        payload=payload if payload is not None else {},
        attempts=1,
        created_at=now,
        run_started_at=now - timedelta(minutes=2),
    )


def _delivery():
    config = ExportConfig(
        "http://127.0.0.1:9/otel/v1/traces", SENTINEL_KEY, "opspilot-lab-t"
    )
    return OutboxDelivery(None, config, transport=lambda *a: 200)  # type: ignore[arg-type]


def test_an_llm_span_carries_tokens_provider_model_and_metadata():
    row = _row(
        payload={
            "incident_id": str(uuid4()),
            "model": "deepseek-v4-flash",
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "reasoning_tokens": 12,
            "finish_reason": "tool_calls",
            "prompt_revision": "prompt-abc",
            "ended_at": "2026-11-01T03:00:00.250000+00:00",
            "span_kind": "model_step",
        }
    )
    span, root = _delivery()._spans(row)
    assert root is None
    attrs = dict(span.attributes)
    assert attrs["langsmith.span.kind"] == "llm"
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.reasoning.output_tokens"] == 12
    assert attrs["langsmith.metadata.ls_provider"] == "deepseek"
    assert attrs["langsmith.metadata.ls_model_name"] == "deepseek-v4-flash"
    assert attrs["langsmith.metadata.prompt_revision"] == "prompt-abc"
    assert span.parent.span_id == root_span_id_for(row.run_id)
    assert span.context.trace_id == trace_id_for(
        "model_step", row.run_id, row.idempotency_key
    )


def test_a_stop_state_row_also_sends_the_run_root_span():
    row = _row(
        "run_state",
        {"run_state": "completed", "ended_at": "2026-11-01T03:00:00+00:00"},
    )
    span, root = _delivery()._spans(row)
    assert root is not None
    assert root.name == "opspilot.run"
    assert root.parent is None
    assert root.context.span_id == root_span_id_for(row.run_id)
    assert root.start_time < root.end_time
    running = _row("run_state", {"run_state": "running"})
    assert _delivery()._spans(running)[1] is None


def test_a_generation_row_is_its_own_trace_root():
    row = _row("generation", {"model": "deepseek-v4-flash", "input_tokens": 5})
    span, root = _delivery()._spans(row)
    assert span.parent is None and root is None


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"ended_at": "yesterday"},
        {"ended_at": "2026-11-01T03:00:00"},  # naive
        {"model": "x" * 80},
        {"input_tokens": "8"},
        {"tool_reason": "free text with spaces"},
    ],
)
def test_a_row_that_is_not_what_the_write_side_writes_is_unserializable(payload):
    with pytest.raises(Exception):
        _delivery()._spans(_row(payload=payload))


def test_keys_outside_the_allowlist_never_reach_a_span():
    row = _row(
        payload={"reasoning_content": "SENTINEL_REASONING", "query": "up{job='x'}"}
    )
    span, _ = _delivery()._spans(row)
    text = repr(dict(span.attributes))
    assert "SENTINEL_REASONING" not in text and "up{" not in text


class _Endpoint:
    def __init__(self, status: int):
        self.status = status
        self.bodies: list[bytes] = []
        self.headers: list[dict[str, str]] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                endpoint.bodies.append(self.rfile.read(length))
                endpoint.headers.append({k.lower(): v for k, v in self.headers.items()})
                self.send_response(endpoint.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/otel/v1/traces"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("status", [200, 202, 400, 408, 429, 503])
def test_the_transport_returns_the_status_and_sends_the_headers(status):
    endpoint = _Endpoint(status)
    try:
        got = urllib_transport(
            endpoint.url,
            b"body",
            {"x-api-key": "k", "Content-Type": "application/x-protobuf"},
            5.0,
        )
    finally:
        endpoint.close()
    assert got == status
    assert endpoint.bodies == [b"body"]
    assert endpoint.headers[0]["x-api-key"] == "k"


def test_an_unreachable_endpoint_raises_oserror():
    with pytest.raises(OSError):
        urllib_transport("http://127.0.0.1:9/otel/v1/traces", b"", {}, 2.0)


def test_outbox_metrics_render_from_the_snapshot_only():
    clock = [100.0]
    stats = health.OutboxStats(
        ("PROJECT_NOT_LAB", "LANGSMITH_KEY_MISSING"), monotonic=lambda: clock[0]
    )
    text = health.worker_metrics(
        last_poll_age=1.0,
        in_flight=0,
        model={"calls": {}, "consecutive_failures": 0, "last_category": None},
        outbox=stats.snapshot(),
    )
    assert not any(
        line.startswith("opspilot_trace_outbox_pending ") for line in text.splitlines()
    )
    assert 'opspilot_trace_outbox_dropped_total{reason="BACKLOG"} 0' in text
    assert "opspilot_trace_outbox_last_delivery_age_seconds +Inf" in text
    assert 'opspilot_trace_export_enabled{reason="PROJECT_NOT_LAB"} 0' in text
    assert 'opspilot_trace_export_enabled{reason="LANGSMITH_KEY_MISSING"} 0' in text
    stats = health.OutboxStats(("ENABLED",), monotonic=lambda: clock[0])
    stats.set_pending(7)
    stats.record_dropped("REJECTED", 3)
    stats.record_delivery()
    clock[0] = 112.5
    text = health.worker_metrics(
        last_poll_age=1.0,
        in_flight=0,
        model={"calls": {}, "consecutive_failures": 0, "last_category": None},
        outbox=stats.snapshot(),
    )
    assert "opspilot_trace_outbox_pending 7" in text
    assert 'opspilot_trace_outbox_dropped_total{reason="REJECTED"} 3' in text
    assert "opspilot_trace_outbox_last_delivery_age_seconds 12.5" in text
    assert 'opspilot_trace_export_enabled{reason="ENABLED"} 1' in text


def test_the_entrypoint_exports_the_langsmith_project_and_endpoint_not_the_key(
    tmp_path,
):
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        f"LANGSMITH_API_KEY={SENTINEL_KEY}\n"
        "LANGSMITH_PROJECT='opspilot-lab-428'\n"
        "LANGSMITH_ENDPOINT=https://api.smith.langchain.com\n"
    )
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "env"],
        env={"PATH": "/usr/bin:/bin", "OPSPILOT_ENV_FILE": str(worker_env)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert env["LANGSMITH_PROJECT"] == "opspilot-lab-428"
    assert env["LANGSMITH_ENDPOINT"] == "https://api.smith.langchain.com"
    assert "LANGSMITH_API_KEY" not in env
    assert SENTINEL_KEY not in result.stdout + result.stderr


def test_install_defaults_the_trace_mode_to_off_and_remembers_it(host):  # noqa: F811
    assert host().returncode == 0
    assert "OPSPILOT_TRACE=off" in (host.secrets / "install.env").read_text()
    assert host(OPSPILOT_TRACE="outbox").returncode == 0
    assert "OPSPILOT_TRACE=outbox" in (host.secrets / "install.env").read_text()
    assert host("upgrade").returncode == 0
    assert "OPSPILOT_TRACE=outbox" in (host.secrets / "install.env").read_text()


def test_install_refuses_an_unknown_trace_mode_before_docker(host):  # noqa: F811
    result = host(OPSPILOT_TRACE="on")
    assert result.returncode != 0
    assert "OPSPILOT_TRACE" in result.stderr
    assert host.calls() == []


def test_compose_passes_the_trace_mode_to_the_worker_only():
    import yaml

    services = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())[
        "services"
    ]
    assert (
        services["worker"]["environment"]["OPSPILOT_TRACE"] == "${OPSPILOT_TRACE:-off}"
    )
    for name in ("web", "observer"):
        assert "OPSPILOT_TRACE" not in services[name]["environment"]


@pytest.mark.parametrize(
    ("env", "delivers", "status"),
    [
        ({}, False, ("TRACE_MODE_OFF",)),
        ({**GOOD, "OPSPILOT_TRACE": "lab"}, False, ("TRACE_MODE_LAB",)),
        ({**GOOD, "LANGSMITH_API_KEY": ""}, False, ("LANGSMITH_KEY_MISSING",)),
        (GOOD, True, ("ENABLED",)),
    ],
)
def test_the_worker_builds_delivery_only_when_enabled(env, delivers, status):
    from opspilot import worker_main

    maintainer, stats = worker_main.build_outbox(
        "host=127.0.0.1 port=9 dbname=none", env, stop=threading.Event()
    )
    assert (maintainer.delivery is not None) is delivers
    assert stats.snapshot()["export_status"] == status
    maintainer.store.close()


def test_an_unexpected_delivery_error_never_ends_the_outbox_thread():
    from opspilot.persistence.outbox import OutboxMaintainer

    class Boom:
        batch_size = 100

        def deliver_once(self):
            raise RuntimeError("bug " + SENTINEL_KEY)

    class Store:
        def purge(self, **_):
            return 0

    maintainer = OutboxMaintainer(Store(), stop=threading.Event(), delivery=Boom())  # type: ignore[arg-type]
    assert maintainer.deliver_once() is False


def test_a_non_oserror_from_the_transport_is_retried():
    from opspilot.outbox_delivery import DeliveryResult

    class Store:
        retried: dict = {}

        def mark_dropped(self, ids, reason, now=None):
            return len(ids)

        def schedule_retry(self, retries, now=None):
            Store.retried = dict(retries)
            return len(retries)

    def broken(*_):
        import http.client

        raise http.client.IncompleteRead(b"")

    config = ExportConfig(
        "http://127.0.0.1:9/otel/v1/traces", SENTINEL_KEY, "opspilot-lab-t"
    )
    delivery = OutboxDelivery(Store(), config, transport=broken)  # type: ignore[arg-type]
    result = DeliveryResult()
    delivery._send([_row(payload={"input_tokens": 1})], result)
    assert result.status is None and result.retried == 1
    assert list(Store.retried.values()) == [timedelta(seconds=5)]


def test_an_unexpected_cleanup_or_count_error_never_ends_the_outbox_thread(caplog):
    from opspilot.persistence.outbox import OutboxMaintainer

    class Broken:
        calls = 0

        def purge(self, **_):
            Broken.calls += 1
            raise RuntimeError("purge " + SENTINEL_KEY)

        def count_pending(self):
            raise KeyError(SENTINEL_KEY)

    stop = threading.Event()
    stats = health.OutboxStats(("TRACE_MODE_OFF",))
    maintainer = OutboxMaintainer(Broken(), stop=stop, interval=0.01, stats=stats)  # type: ignore[arg-type]
    assert maintainer.run_once() is None
    maintainer.refresh_pending()
    thread = threading.Thread(target=maintainer.run, daemon=True)
    thread.start()
    try:
        deadline = __import__("time").monotonic() + 5
        while Broken.calls < 5 and __import__("time").monotonic() < deadline:
            stop.wait(0.01)
        assert thread.is_alive()
        assert Broken.calls >= 5
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
    assert SENTINEL_KEY not in caplog.text
    assert "error=RuntimeError" in caplog.text
