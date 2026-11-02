"""Independent acceptance tests for F8 structured logs (#386)."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).parents[2]
JSON_KEYS = ["ts", "level", "logger", "msg", "version", "revision"]
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
CREDENTIAL = "canary-credential-386-canary-credential"
PROVIDER = "PROVIDER-SENTINEL-386 the model said root cause is X"


def _logs():
    from opspilot import logs

    return logs


def _records(raw: str) -> list[dict]:
    lines = raw.splitlines()
    assert lines, "expected at least one captured log line"
    return [json.loads(line) for line in lines]


def _assert_json_record(record: dict, *, logger: str | None = None) -> None:
    assert list(record)[: len(JSON_KEYS)] == JSON_KEYS
    assert set(record) <= set(JSON_KEYS + ["kv", "exc", "stack"])
    assert TS_RE.fullmatch(record["ts"])
    assert record["level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    assert isinstance(record["msg"], str)
    if logger is not None:
        assert record["logger"] == logger


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)}
    env.update(overrides)
    return env


def _run_entry(role: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    module_args = {
        "worker": ["-m", "opspilot.worker_main"],
        "observer": ["-m", "opspilot.observer"],
        "web": ["-m", "opspilot.web", "serve"],
    }[role]
    env = _clean_env(
        OPSPILOT_HEALTH_BIND="",
        OPSPILOT_POOL_TIMEOUT_SECONDS="2",
        OPSPILOT_LOG_FORMAT="json",
    )
    env.update(overrides)
    if role == "worker":
        env.setdefault(
            "OPSPILOT_WORKER_DSN",
            "host=127.0.0.1 port=1 dbname=none user=x connect_timeout=1",
        )
    elif role == "observer":
        env.setdefault(
            "OPSPILOT_OBSERVER_DSN",
            "host=127.0.0.1 port=1 dbname=none user=x connect_timeout=1",
        )
        env.setdefault("OPSPILOT_OBSERVER_PROMETHEUS_URL", "http://127.0.0.1:9")
    else:
        env.setdefault(
            "OPSPILOT_WEB_DSN",
            "host=127.0.0.1 port=1 dbname=none user=x connect_timeout=1",
        )
    return subprocess.run(
        [sys.executable, *module_args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_three_entry_processes_emit_one_json_object_per_line_and_third_party_lines_match():
    for role in ("worker", "observer", "web"):
        result = _run_entry(role)
        assert result.returncode == 1, result
        assert result.stdout == ""
        records = _records(result.stderr)
        for record in records:
            _assert_json_record(record)
        assert any(record["logger"] == "psycopg.pool" for record in records)
        assert sum(record["logger"] == "opspilot.uncaught" for record in records) == 1
        uncaught = next(r for r in records if r["logger"] == "opspilot.uncaught")
        assert uncaught["level"] == "ERROR"
        assert "PersistenceError: TIMEOUT" in uncaught["exc"]


def test_configure_shape_key_order_multiline_message_and_traceback():
    stream = StringIO()
    _logs().configure({"OPSPILOT_REVISION": "abc123"}, stream=stream)
    logger = logging.getLogger("opspilot.acceptance")
    logger.info('line one\nline two with "quote"')
    logger.info("stack probe", stack_info=True)
    try:
        raise ValueError("boom\nsecond line")
    except ValueError:
        logger.exception("failed\nwith context")
    records = _records(stream.getvalue())
    assert len(records) == 3
    assert list(records[0]) == JSON_KEYS
    assert records[0]["revision"] == "abc123"
    assert list(records[1]) == JSON_KEYS + ["stack"]
    assert list(records[2]) == JSON_KEYS + ["exc"]
    assert records[0]["msg"] == 'line one\nline two with "quote"'
    assert 'File "' in records[1]["stack"]
    assert "ValueError: boom\nsecond line" in records[2]["exc"]
    assert len(stream.getvalue().splitlines()) == 3


@pytest.mark.parametrize(
    ("msg", "args", "expected"),
    [
        (
            "bind=%s port=%d",
            ("0.0.0.0:9464", 9464),
            {"bind": "0.0.0.0:9464", "port": 9464},
        ),
        ("enabled=%s value=%s", (True, None), {"enabled": True, "value": None}),
        ("cost=%f", (float("inf"),), {"cost": "inf"}),
        (
            "status=%s reason=%r",
            ("ok", ("MODEL_UNAVAILABLE",)),
            {"status": "ok", "reason": "('MODEL_UNAVAILABLE',)"},
        ),
        ("status=%s", ("ok",), {"status": "ok"}),
        ("signal=%s: stop", (15,), {"signal": 15}),
    ],
)
def test_kv_extraction_is_by_name_placeholder_position(msg, args, expected):
    stream = StringIO()
    _logs().configure(stream=stream)
    logging.getLogger("opspilot.kv").info(msg, *args)
    record = _records(stream.getvalue())[0]
    assert record["msg"] == msg % args
    assert record["kv"] == expected
    assert list(record).index("kv") == len(JSON_KEYS)


def test_version_and_revision_callsite_kv_do_not_override_top_level_fields():
    stream = StringIO()
    _logs().configure({"OPSPILOT_REVISION": "abc123"}, stream=stream)
    logging.getLogger("opspilot.version").info("version=%s revision=%s", 7, "callsite")
    record = _records(stream.getvalue())[0]
    assert record["version"] == "0.1.0"
    assert record["revision"] == "abc123"
    assert record["kv"] == {"version": 7, "revision": "callsite"}


@pytest.mark.parametrize(
    ("msg", "args", "expected_msg"),
    [
        ("status=%s", ("ok", "extra"), "status=%s"),
        (
            "replay failed for session %s",
            ("session-1",),
            "replay failed for session session-1",
        ),
        ("field=%(field)s", ({"field": "value"},), "field=value"),
    ],
)
def test_kv_mismatch_dict_args_and_non_name_placeholders_produce_no_kv(
    msg, args, expected_msg
):
    logs = _logs()
    record_obj = logging.LogRecord(
        "opspilot.kv", logging.INFO, __file__, 0, msg, args, None
    )
    line = logs.JsonFormatter(version="0.1.0", revision="unknown").format(record_obj)
    record = json.loads(line)
    assert "kv" not in record
    assert record["msg"] == expected_msg


@pytest.mark.parametrize("revision", ["", "bad value", "a" * 65, "bad/slash", "ümlaut"])
def test_invalid_revision_is_unknown(revision):
    stream = StringIO()
    _logs().configure({"OPSPILOT_REVISION": revision}, stream=stream)
    logging.getLogger("opspilot.revision").info("revision probe")
    assert _records(stream.getvalue())[0]["revision"] == "unknown"


def test_revision_is_snapshotted_at_configure_and_empty_format_defaults_to_json():
    stream = StringIO()
    env = {"OPSPILOT_LOG_FORMAT": "", "OPSPILOT_REVISION": "build-1"}
    _logs().configure(env, stream=stream)
    env["OPSPILOT_REVISION"] = "build-2"
    logging.getLogger("opspilot.revision").info("snapshot")
    record = _records(stream.getvalue())[0]
    assert record["revision"] == "build-1"
    assert list(record) == JSON_KEYS


def test_text_mode_is_old_format_and_invalid_mode_refuses_before_logging_state_changes():
    stream = StringIO()
    _logs().configure({"OPSPILOT_LOG_FORMAT": "text"}, stream=stream)
    logging.getLogger("opspilot.text").warning("hello")
    assert re.match(
        r"^\d{4}-\d{2}-\d\d \d\d:\d\d:\d\d,\d{3} opspilot\.text WARNING hello$",
        stream.getvalue().splitlines()[0],
    )
    root = logging.getLogger()
    before = list(root.handlers)
    with pytest.raises(SystemExit, match="OPSPILOT_LOG_FORMAT must be json or text"):
        _logs().configure({"OPSPILOT_LOG_FORMAT": " JSON "}, stream=StringIO())
    assert list(root.handlers) == before


def test_configure_is_idempotent_and_replaces_only_its_own_handler():
    stream = StringIO()
    other = logging.StreamHandler(StringIO())
    root = logging.getLogger()
    root.addHandler(other)
    try:
        before = list(root.handlers)
        _logs().configure(stream=stream)
        first = [h for h in root.handlers if h not in before]
        _logs().configure(stream=stream)
        ours = [h for h in root.handlers if h not in before]
        assert len(first) == len(ours) == 1
        assert other in root.handlers
        logging.getLogger("opspilot.idempotent").info("one line")
        assert len(stream.getvalue().splitlines()) == 1
    finally:
        root.removeHandler(other)


def test_uncaught_main_thread_is_one_error_json_with_complete_traceback():
    script = """
from opspilot import logs
logs.configure()
def inner():
    try:
        raise ValueError('inner')
    except ValueError as exc:
        raise RuntimeError('outer') from exc
inner()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    records = _records(result.stderr)
    assert len(records) == 1
    record = records[0]
    _assert_json_record(record, logger="opspilot.uncaught")
    assert record["msg"] == "uncaught exception thread=MainThread"
    assert record["kv"] == {"thread": "MainThread"}
    assert (
        "ValueError: inner" in record["exc"] and "RuntimeError: outer" in record["exc"]
    )
    assert "direct cause" in record["exc"]


def test_uncaught_thread_is_one_error_json_and_system_exit_refusal_stays_plain():
    script = """
import threading
from opspilot import logs
logs.configure()
def boom():
    raise LookupError('thread boom')
t = threading.Thread(target=boom, name='sentinel-thread')
t.start(); t.join()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    records = _records(result.stderr)
    assert len(records) == 1
    assert records[0]["msg"] == "uncaught exception thread=sentinel-thread"
    assert records[0]["kv"] == {"thread": "sentinel-thread"}
    assert "LookupError: thread boom" in records[0]["exc"]
    refusal = _run_entry(
        "worker", OPSPILOT_LOG_FORMAT="bogus", OPSPILOT_WORKER_DSN="ignored"
    )
    assert refusal.returncode == 1 and refusal.stdout == ""
    assert refusal.stderr == "OPSPILOT_LOG_FORMAT must be json or text\n"


def test_invalid_format_is_rejected_by_all_three_entry_points():
    for role in ("worker", "observer", "web"):
        result = _run_entry(role, OPSPILOT_LOG_FORMAT="bogus")
        assert result.returncode == 1 and result.stdout == ""
        assert result.stderr == "OPSPILOT_LOG_FORMAT must be json or text\n"


def test_text_fallback_is_observable_in_each_entry():
    for role in ("worker", "observer", "web"):
        result = _run_entry(role, OPSPILOT_LOG_FORMAT="text")
        assert result.returncode == 1 and result.stdout == ""
        assert any(
            "psycopg.pool WARNING" in line for line in result.stderr.splitlines()
        )
        assert not all(line.startswith("{") for line in result.stderr.splitlines())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_uvicorn_error_and_access_logs_use_root_json_handler():
    stream = StringIO()
    _logs().configure(stream=stream)
    import uvicorn

    async def app(scope, receive, send):
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not server.started:
        time.sleep(0.01)
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        sock.recv(1024)
    server.should_exit = True
    thread.join(5)
    records = _records(stream.getvalue())
    assert any(r["logger"] == "uvicorn.error" for r in records)
    access = next(r for r in records if r["logger"] == "uvicorn.access")
    assert list(access) == JSON_KEYS and "GET / HTTP/1.1" in access["msg"]
    assert "kv" not in access


def test_worker_model_and_runner_sentinels_are_stopped_at_public_boundaries():
    stream = StringIO()
    _logs().configure(stream=stream)
    from opspilot.investigation.client import DeepSeekClient

    class Opener:
        def open(self, request, timeout):
            raise RuntimeError(f"{CREDENTIAL} {PROVIDER}")

    with pytest.raises(Exception):
        DeepSeekClient(CREDENTIAL, opener=Opener()).complete(
            SimpleNamespace(
                messages=(), tools=(), json_mode=False, max_tokens=1, timeout_seconds=1
            )
        )
    assert CREDENTIAL not in stream.getvalue() and PROVIDER not in stream.getvalue()
    from opspilot.worker_main import WorkerLoop

    class Runner:
        def resume(self, incident_id):
            raise RuntimeError(f"{CREDENTIAL} {PROVIDER}")

    class Queue:
        def claimable_incidents(self, *, limit=20):
            return (uuid4(),)

        def remove(self, incident_id):
            return None

    class Sweeper:
        def sweep_expired_runs_with_generations(self, *, incident_id=None, limit=100):
            return ()

    loop = WorkerLoop(queue=Queue(), runner=Runner(), sweeper=Sweeper(), events=None)
    try:
        loop.poll_once()
    except Exception:
        pass
    assert CREDENTIAL not in stream.getvalue() and PROVIDER not in stream.getvalue()


def test_postmortem_and_observer_sentinels_are_stopped_at_public_boundaries():
    stream = StringIO()
    _logs().configure(stream=stream)
    from opspilot.knowledge.worker import PostmortemWorker

    incident = uuid4()
    jobs = SimpleNamespace(
        candidates=lambda *a, **k: [incident], stale_versions=lambda *a, **k: []
    )
    model = SimpleNamespace(
        complete=lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError(f"{CREDENTIAL} {PROVIDER}")
        )
    )
    worker = PostmortemWorker(
        knowledge=SimpleNamespace(), jobs=jobs, model=model, events=None
    )
    try:
        worker.poll_once()
    except Exception:
        pass
    from opspilot.observer.loop import ObserverLoop
    from tests.test_m1_observer import PROFILE
    from tests.test_m1_observer import lease as observer_lease

    lease = observer_lease()

    class ObserverStore:
        def sweep_expired_sessions(self, *, limit):
            return []

        def claim_due_samples(self, owner, *, limit, lease_seconds):
            return [lease]

        def health_profile(self, revision):
            return PROFILE.model_dump(mode="json")

        def current_time(self):
            return datetime.now(timezone.utc)

    source = SimpleNamespace(
        instant=lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError(f"{CREDENTIAL} {PROVIDER}")
        )
    )
    ObserverLoop(store=ObserverStore(), source=source).poll_once(compare=False)
    output = stream.getvalue()
    assert "sample crashed" in output
    assert CREDENTIAL not in output and PROVIDER not in output


@pytest.mark.parametrize("site", ["automatic", "authorize", "anomaly"])
def test_web_background_exception_sites_do_not_log_provider_or_credential_sentinels(
    site,
):
    stream = StringIO()
    _logs().configure(stream=stream)
    from pydantic import BaseModel, ValidationError

    from opspilot.persistence.base import PersistenceError
    from opspilot.web import __main__ as web_main

    class SentinelInput(BaseModel):
        value: int

    def sentinel_validation_error() -> ValidationError:
        try:
            SentinelInput.model_validate({"value": f"{CREDENTIAL} {PROVIDER}"})
        except ValidationError as exc:
            return exc
        raise AssertionError("sentinel input unexpectedly validated")

    class Workbench:
        def __init__(self):
            self.calls = []
            self.done = threading.Event()
            self.store = SimpleNamespace(
                run=lambda payload: (_ for _ in ()).throw(PersistenceError("TIMEOUT"))
            )

        def _pass(self, name):
            self.calls.append(name)
            if len(self.calls) >= 3:
                self.done.set()
            if site == "anomaly" and name == "release_anomaly_pass":
                raise sentinel_validation_error()
            if (
                name.rsplit("_", 1)[-1] == site
                or (site == "automatic" and name == "self_heal_pass")
                or (site == "authorize" and name == "release_authorize_pass")
            ):
                self.store.run({"provider_text": PROVIDER, "credential": CREDENTIAL})
            return []

        def self_heal_pass(self):
            return self._pass("self_heal_pass")

        def release_authorize_pass(self):
            return self._pass("release_authorize_pass")

        def release_anomaly_pass(self):
            return self._pass("release_anomaly_pass")

    stop = threading.Event()
    workbench = Workbench()
    thread = threading.Thread(
        target=web_main.self_heal_loop, args=(workbench, 0, stop), daemon=True
    )
    thread.start()
    assert workbench.done.wait(5)
    stop.set()
    thread.join(2)
    output = stream.getvalue()
    assert len(workbench.calls) >= 3
    assert "TIMEOUT" in output
    assert CREDENTIAL not in output and PROVIDER not in output


def test_web_service_exception_sites_have_explicit_sentinel_coverage_contract():
    stream = StringIO()
    _logs().configure(stream=stream)
    from pydantic import BaseModel, ValidationError

    import opspilot.web.service as service
    from opspilot.persistence.base import PersistenceError

    class SentinelInput(BaseModel):
        value: int

    def sentinel_validation_error() -> ValidationError:
        try:
            SentinelInput.model_validate({"value": f"{CREDENTIAL} {PROVIDER}"})
        except ValidationError as exc:
            return exc
        raise AssertionError("sentinel input unexpectedly validated")

    workbench = object.__new__(service.Workbench)
    workbench.incidents = SimpleNamespace(
        observation_history=lambda session_id: {
            "session_id": str(session_id),
            "provider_text": PROVIDER,
            "credential": CREDENTIAL,
        }
    )
    original_replay = service.replay_history
    original_chart = service.evidence_chart
    service.replay_history = lambda *a, **k: (_ for _ in ()).throw(
        PersistenceError("TIMEOUT")
    )
    chart_record = SimpleNamespace(
        evidence_id="ev-386", provider_text=PROVIDER, credential=CREDENTIAL
    )
    service.evidence_chart = lambda *a, **k: (_ for _ in ()).throw(
        PersistenceError("TIMEOUT")
    )
    try:
        try:
            workbench._recovery_view(uuid4())
        except Exception:
            pass
        workbench.evidence_for = lambda *a, **k: chart_record
        report = {
            "parsed": True,
            **{
                category: [{"evidence_ids": ["ev-386"]}]
                for category in service.CHART_CATEGORIES
            },
        }
        try:
            workbench._charts(uuid4(), report)
        except Exception:
            pass
        workbench._release_store = lambda: SimpleNamespace(
            pending_anomaly_endings=lambda *, limit: [
                SimpleNamespace(observation_id=uuid4())
            ],
            consume_anomaly=lambda *a, **k: (_ for _ in ()).throw(
                sentinel_validation_error()
            ),
        )
        try:
            workbench.release_anomaly_pass()
        except Exception:
            pass
    finally:
        service.replay_history = original_replay
        service.evidence_chart = original_chart
    output = stream.getvalue()
    assert output.count('"exc"') >= 2
    assert output.count("TIMEOUT") >= 2
    assert CREDENTIAL not in output and PROVIDER not in output
