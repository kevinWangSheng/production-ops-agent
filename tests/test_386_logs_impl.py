"""F8 structured logs (#386): implementer tests for ``opspilot.logs`` and the
three entry points' wiring. The independent acceptance tests are written
separately from the contract (F8L-Q3)."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from opspilot import __version__, logs

ROOT = Path(__file__).resolve().parents[1]
KEYS = ["ts", "level", "logger", "msg", "version", "revision"]
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
SECRET = "canary-credential-canary-credential"
PROVIDER = "PROVIDER-SENTINEL-386 the model said the root cause is checkout"
UNREACHABLE = "host=127.0.0.1 port=1 dbname=none user=x connect_timeout=1"


@pytest.fixture
def restore_logging(monkeypatch):
    """Undo what ``configure()`` changes in this process."""
    root = logging.getLogger()
    level = root.level
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    yield
    for handler in list(root.handlers):
        if isinstance(handler, logs._Handler):
            root.removeHandler(handler)
    root.setLevel(level)


@pytest.fixture
def buf(restore_logging):
    stream = io.StringIO()
    logs.configure({}, stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict]:
    raw = stream.getvalue().splitlines()
    return [json.loads(line) for line in raw]


def one(stream: io.StringIO) -> dict:
    (line,) = lines(stream)
    return line


# -- the line ------------------------------------------------------------------


def test_fixed_keys_in_order_with_process_constants(restore_logging):
    stream = io.StringIO()
    logs.configure({"OPSPILOT_REVISION": "abc123"}, stream=stream)
    logging.getLogger("opspilot.t").info("worker started")
    line = one(stream)
    assert list(line) == KEYS
    assert TS.match(line["ts"])
    assert line["level"] == "INFO"
    assert line["logger"] == "opspilot.t"
    assert line["msg"] == "worker started"
    assert line["version"] == __version__
    assert line["revision"] == "abc123"


@pytest.mark.parametrize("raw", [None, "", "bad value", "x" * 65, "a/b"])
def test_a_missing_or_malformed_revision_is_unknown(restore_logging, raw):
    stream = io.StringIO()
    logs.configure({} if raw is None else {"OPSPILOT_REVISION": raw}, stream=stream)
    logging.getLogger("t").warning("x")
    assert one(stream)["revision"] == "unknown"


def test_revision_is_read_once_at_configure(restore_logging):
    stream = io.StringIO()
    env = {"OPSPILOT_REVISION": "first"}
    logs.configure(env, stream=stream)
    env["OPSPILOT_REVISION"] = "second"
    logging.getLogger("t").warning("x")
    assert one(stream)["revision"] == "first"


def test_multiline_message_and_traceback_stay_one_line(buf):
    log = logging.getLogger("opspilot.schema")
    log.warning('takeover accepted:\n- a int\n+ b "int" </script>\r\x00')
    try:
        raise ValueError("boom\nsecond line")
    except ValueError:
        log.exception("failed for %s", "x")
    raw = buf.getvalue().splitlines()
    assert len(raw) == 2
    first, second = (json.loads(line) for line in raw)
    assert first["msg"] == 'takeover accepted:\n- a int\n+ b "int" </script>\r\x00'
    assert list(second) == KEYS + ["exc"]
    assert second["exc"].startswith("Traceback (most recent call last):\n")
    assert second["exc"].endswith("ValueError: boom\nsecond line")


def test_stack_info_is_the_last_key(buf):
    logging.getLogger("t").info("x=%s", 1, stack_info=True)
    line = one(buf)
    assert list(line) == KEYS + ["kv", "stack"]
    assert line["stack"].startswith("Stack (most recent call last):")


def test_no_redaction_of_message_or_traceback(buf):
    """F8L-Q2: the formatter does not rewrite anything."""
    text = f"sample session=abc Authorization: Bearer {SECRET} prompt_tokens=3"
    logging.getLogger("t").warning(text)
    assert one(buf)["msg"] == text


def test_a_call_whose_arguments_do_not_fit_still_yields_one_json_line():
    # Formatted directly: pytest's own capture handler re-raises the error.
    record = logging.LogRecord(
        "t", logging.WARNING, __file__, 1, "a=%s b=%d", ("x", "nan"), None
    )
    formatter = logs.JsonFormatter(version="v", revision="r")
    line = json.loads(formatter.format(record))
    assert line["msg"] == "a=%s b=%d"
    assert "kv" not in line


# -- kv ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "args", "kv"),
    [
        (
            "health listener bind=%s port=%d",
            ("0.0.0.0:9464", 9464),
            {"bind": "0.0.0.0:9464", "port": 9464},
        ),
        ("signal=%s: stop claiming", (15,), {"signal": 15}),
        ("grace=%ss: exiting within %ss", (30.0, 600), {"grace": 30.0}),
        (
            "postmortem status=%s code=%s version=%s ok=%s",
            ("ok", None, 2, True),
            {"status": "ok", "code": None, "version": 2, "ok": True},
        ),
        ("reason=%s", (("MODEL_UNAVAILABLE",),), {"reason": "('MODEL_UNAVAILABLE',)"}),
        ("value=%r", ("q",), {"value": "q"}),
        ("rate=%.2f nan=%s", (0.5, float("nan")), {"rate": 0.5, "nan": "nan"}),
        ("width=%*d", (5, 3), {"width": 3}),
        ("pct=%d%% done=%s", (50, "y"), {"pct": 50, "done": "y"}),
        ("a=%sb=%s", (1, 2), {"a": 1, "b": 2}),
        ("k=%s k=%s", (1, 2), {"k": 2}),
    ],
)
def test_kv_from_name_equals_placeholders(buf, fmt, args, kv):
    logging.getLogger("t").info(fmt, *args)
    assert one(buf)["kv"] == kv


def test_uuid_and_exception_values_are_strings(buf):
    from opspilot.persistence import PersistenceError

    incident = uuid4()
    logging.getLogger("t").warning(
        "refused incident=%s code=%s", incident, PersistenceError("TIMEOUT")
    )
    assert one(buf)["kv"] == {"incident": str(incident), "code": "TIMEOUT"}


@pytest.mark.parametrize(
    ("fmt", "args"),
    [
        ("replay failed for session %s", ("s",)),  # no name=
        ("mapping %(a)s", ({"a": 1},)),  # dict args
        ("a=%s", ()),  # too few
        ("a=%s", (1, 2)),  # too many
        ("a=%y", (1,)),  # unknown conversion
        ("x-a=%s", None),  # placeholder only, no args at all
        ("weird=\n%s", (1,)),  # the key must end right at the %
    ],
)
def test_no_kv_key(fmt, args):
    # Formatted directly (as ``Logger`` builds the record): pytest's own
    # capture handler re-raises on the mismatched ones.
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, fmt, () if args is None else args, None
    )
    line = json.loads(logs.JsonFormatter(version="v", revision="r").format(record))
    assert "kv" not in line


def test_key_needs_a_word_boundary(buf):
    logging.getLogger("t").info("-a=%s é=%s", 1, 2)
    assert one(buf)["kv"] == {"a": 1}


def test_call_site_version_does_not_shadow_the_fixed_field(buf):
    logging.getLogger("t").info("postmortem version=%s revision=%s", 7, "r9")
    line = one(buf)
    assert line["version"] == __version__
    assert line["revision"] == "unknown"
    assert line["kv"] == {"version": 7, "revision": "r9"}


# -- format switch, idempotency ------------------------------------------------


def test_text_format_is_the_previous_one(restore_logging):
    stream = io.StringIO()
    logs.configure({"OPSPILOT_LOG_FORMAT": "text"}, stream=stream)
    logging.getLogger("opspilot.worker").warning("worker loop died; exiting")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} opspilot.worker WARNING "
        r"worker loop died; exiting\n",
        stream.getvalue(),
    )


@pytest.mark.parametrize("value", ["bogus", "JSON", " json", "text "])
def test_unknown_format_refuses_before_changing_anything(restore_logging, value):
    root = logging.getLogger()
    before = (list(root.handlers), root.level, sys.excepthook, threading.excepthook)
    with pytest.raises(SystemExit) as raised:
        logs.configure({"OPSPILOT_LOG_FORMAT": value})
    assert raised.value.code == "OPSPILOT_LOG_FORMAT must be json or text"
    assert (list(root.handlers), root.level, sys.excepthook, threading.excepthook) == (
        before
    )


def test_configure_twice_replaces_only_its_own_handler(restore_logging):
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        first, second = io.StringIO(), io.StringIO()
        logs.configure({}, stream=first)
        logs.configure({"OPSPILOT_LOG_FORMAT": "text"}, stream=second)
        logging.getLogger("t").warning("once")
        assert first.getvalue() == ""
        assert second.getvalue().count("once") == 1
        assert foreign in root.handlers
        assert sum(isinstance(h, logs._Handler) for h in root.handlers) == 1
        assert root.level == logging.INFO
    finally:
        root.removeHandler(foreign)


def test_default_stream_is_stderr_at_emit_time(restore_logging, capsys):
    logs.configure({})
    logging.getLogger("t").info("to stderr")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.splitlines()[-1])["msg"] == "to stderr"


# -- uncaught exceptions (Q5) --------------------------------------------------


def test_a_thread_crash_is_one_error_line_with_the_full_traceback(buf):
    def crash():
        raise RuntimeError("thread boom")

    thread = threading.Thread(target=crash, name="opspilot-test-thread")
    thread.start()
    thread.join()
    line = one(buf)
    assert line["level"] == "ERROR"
    assert line["logger"] == "opspilot.uncaught"
    assert line["msg"] == "uncaught exception thread=opspilot-test-thread"
    assert line["kv"] == {"thread": "opspilot-test-thread"}
    assert line["exc"].endswith("RuntimeError: thread boom")


def test_systemexit_in_a_thread_stays_silent(buf):
    thread = threading.Thread(target=sys.exit, args=("bye",))
    thread.start()
    thread.join()
    assert buf.getvalue() == ""


def _python(code: str, env: dict[str, str], timeout: float = 20):
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OPSPILOT_HEALTH_BIND": ""}
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ROOT,
    )


def test_main_thread_crash_is_one_json_line_and_exit_1():
    proc = _python(
        "from opspilot import logs; logs.configure()\n"
        "try:\n    raise KeyError('inner')\n"
        "except KeyError as e:\n    raise ValueError('outer') from e\n",
        {},
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    (raw,) = proc.stderr.splitlines()
    line = json.loads(raw)
    assert line["msg"] == "uncaught exception thread=MainThread"
    assert "KeyError: 'inner'" in line["exc"]
    assert "direct cause" in line["exc"]
    assert line["exc"].endswith("ValueError: outer")


def test_a_deliberate_refusal_stays_plain_text():
    proc = _python(
        "from opspilot import logs; logs.configure()\n"
        "raise SystemExit('OPSPILOT_X is required')\n",
        {},
    )
    assert proc.returncode == 1
    assert proc.stderr == "OPSPILOT_X is required\n"


# -- the three entry points ----------------------------------------------------

ENTRIES = {
    "worker": ("opspilot.worker_main", [], "OPSPILOT_WORKER_DSN"),
    "observer": ("opspilot.observer", [], "OPSPILOT_OBSERVER_DSN"),
    "web": ("opspilot.web", ["serve"], "OPSPILOT_WEB_DSN"),
}


def _entry(name: str, extra: dict[str, str], *, dsn: str | None = UNREACHABLE):
    module, args, variable = ENTRIES[name]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "OPSPILOT_HEALTH_BIND": "",
        "OPSPILOT_POOL_TIMEOUT_SECONDS": "1",
        "OPSPILOT_OBSERVER_PROMETHEUS_URL": "http://127.0.0.1:9",
        **extra,
    }
    if dsn is not None:
        env[variable] = dsn
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
    )


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_entry_point_writes_json_lines_including_psycopg_and_the_crash(name):
    dsn = f"postgresql://u:{SECRET}@127.0.0.1:1/none?connect_timeout=1"
    proc = _entry(name, {"OPSPILOT_REVISION": "rev386"}, dsn=dsn)
    assert proc.returncode == 1, proc.stderr
    assert proc.stdout == ""
    parsed = [json.loads(line) for line in proc.stderr.splitlines()]
    assert all(list(p)[:6] == KEYS and p["revision"] == "rev386" for p in parsed)
    assert any(p["logger"] == "psycopg.pool" for p in parsed)
    # The pool's own thread may still log a connect failure after the crash.
    (crash,) = [p for p in parsed if p["logger"] == "opspilot.uncaught"]
    assert crash["exc"].endswith("PersistenceError: TIMEOUT")
    assert SECRET not in proc.stderr


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_entry_point_refuses_an_unknown_format_before_anything_else(name):
    proc = _entry(name, {"OPSPILOT_LOG_FORMAT": "bogus"})
    assert proc.returncode == 1
    assert proc.stderr == "OPSPILOT_LOG_FORMAT must be json or text\n"


def test_entry_point_text_fallback():
    proc = _entry("observer", {"OPSPILOT_LOG_FORMAT": "text"})
    assert proc.returncode == 1
    assert " psycopg.pool WARNING " in proc.stderr
    assert "Traceback (most recent call last):" in proc.stderr
    assert not proc.stderr.lstrip().startswith("{")


def test_entry_point_dsn_refusal_is_still_text():
    proc = _entry("worker", {}, dsn=None)
    assert proc.returncode == 1
    assert proc.stderr == "OPSPILOT_WORKER_DSN is required\n"


# -- uvicorn -------------------------------------------------------------------

UVICORN_SCRIPT = """
import socket, threading, time, urllib.request, uvicorn
from opspilot import logs
logs.configure()

async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})

sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
sock.close()
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                       log_config=None, lifespan="off"))
thread = threading.Thread(target=server.run); thread.start()
while not server.started:
    time.sleep(0.02)
urllib.request.urlopen(f"http://127.0.0.1:{port}/ping?cursor=1", timeout=5).read()
server.should_exit = True
thread.join(10)
"""


def test_uvicorn_lines_share_the_format_and_go_to_stderr():
    proc = _python(UVICORN_SCRIPT, {})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    parsed = [json.loads(line) for line in proc.stderr.splitlines()]
    loggers = {p["logger"] for p in parsed}
    assert {"uvicorn.error", "uvicorn.access"} <= loggers
    access = next(p for p in parsed if p["logger"] == "uvicorn.access")
    assert "GET /ping?cursor=1" in access["msg"]
    assert "kv" not in access


def test_web_serve_hands_uvicorn_no_log_config(monkeypatch, restore_logging):
    """``_serve()`` configures logging first and starts uvicorn with
    ``log_config=None``; storage and the workbench are stand-ins."""
    import uvicorn

    from opspilot.web import __main__ as web_main

    class Stub:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    profile = SimpleNamespace(versions=lambda: {}, face=lambda clock: None)
    for name in (
        "DurableStore",
        "DurableEventLog",
        "DurableEvidenceStore",
        "DurableWebLedger",
        "DurableClock",
        "DurableIncidentStore",
        "KnowledgeStore",
        "Workbench",
        "PostmortemReview",
        "Authenticator",
    ):
        monkeypatch.setattr(web_main, name, Stub)
    monkeypatch.setattr(web_main, "select_profile", lambda env: profile)
    monkeypatch.setattr(web_main, "create_app", lambda *a, **k: "app")
    monkeypatch.setattr(web_main, "_start_health", lambda store: None)
    monkeypatch.setattr(web_main, "_target_registry", lambda: None)
    monkeypatch.setattr(web_main, "_health_profile", lambda: None)
    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    monkeypatch.setenv("OPSPILOT_WEB_DSN", "host=unused")
    monkeypatch.setenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("OPSPILOT_LOG_FORMAT", raising=False)
    assert web_main._serve() == 0
    assert seen["app"] == "app"
    assert seen["log_config"] is None
    assert any(isinstance(h, logs._Handler) for h in logging.getLogger().handlers)


# -- sentinels: model / provider text never reaches a line (F8L-Q2, O6) -------


def _assert_clean(stream: io.StringIO) -> list[dict]:
    text = stream.getvalue()
    assert SECRET not in text
    assert PROVIDER not in text
    return lines(stream)


def test_worker_slot_crash_logs_the_type_only(buf):
    from opspilot.worker_main import WorkerLoop

    incident = uuid4()

    class Queue:
        def claimable_incidents(self, *, limit=20):
            return (incident,)

    class Sweeper:
        def sweep_expired_runs_with_generations(self, *, incident_id=None, limit=100):
            return ()

    class Runner:
        def resume(self, incident_id: UUID):
            raise RuntimeError(f"{PROVIDER} Authorization: Bearer {SECRET}")

    loop = WorkerLoop(queue=Queue(), runner=Runner(), sweeper=Sweeper(), events=None)
    assert loop.poll_once() == []
    parsed = _assert_clean(buf)
    crash = [p for p in parsed if p["level"] == "ERROR"]
    assert [p["msg"] for p in crash] == [
        f"attempt crashed incident={incident} error=RuntimeError"
    ]
    assert crash[0]["kv"] == {"incident": str(incident), "error": "RuntimeError"}
    assert all(p["logger"] != "opspilot.uncaught" for p in parsed)


def test_model_client_errors_carry_codes_not_provider_text(buf):
    from opspilot.investigation.client import DeepSeekClient
    from opspilot.investigation.loop import ModelCall, ModelError

    class Response:
        status = 401

        def __init__(self, payload: bytes) -> None:
            self._chunks = [payload, b""]

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self, size=-1):
            return self._chunks.pop(0) if self._chunks else b""

    class Opener:
        def open(self, request, timeout=None):
            body = json.dumps({"error": {"message": f"{PROVIDER} {SECRET}"}})
            return Response(body.encode())

    client = DeepSeekClient(SECRET, opener=Opener())
    call = ModelCall(
        messages=({"role": "user", "content": "hi"},),
        tools=None,
        json_mode=False,
        max_tokens=16,
        timeout_seconds=5.0,
    )
    with pytest.raises(ModelError) as raised:
        client.complete(call)
    assert SECRET not in str(raised.value)
    assert PROVIDER not in str(raised.value)
    logging.getLogger("opspilot.worker").warning("model refused code=%s", raised.value)
    _assert_clean(buf)


def test_postmortem_crash_logs_the_type_only(buf):
    from opspilot.knowledge.worker import PostmortemWorker

    incident = uuid4()
    jobs = SimpleNamespace(
        stale_versions=lambda limit: [], candidates=lambda **kw: [incident]
    )
    worker = PostmortemWorker(knowledge=None, jobs=jobs, model=None)

    def generate(incident_id):
        raise ValueError(f"{PROVIDER} api_key={SECRET}")

    worker.generate = generate
    assert worker.poll_once() == []
    (line,) = _assert_clean(buf)
    assert line["msg"] == f"generation crashed incident={incident} error=ValueError"


def test_observer_sample_crash_logs_the_type_only(buf):
    from opspilot.observer.loop import ObserverLoop

    session = uuid4()
    store = SimpleNamespace(
        sweep_expired_sessions=lambda limit: [],
        claim_due_samples=lambda owner, limit, lease_seconds: [
            SimpleNamespace(session_id=session, observation_id=None)
        ],
    )
    loop = ObserverLoop(store=store, source=None, batch=1)

    def sample(lease):
        raise RuntimeError(f"prometheus said {PROVIDER} token={SECRET}")

    loop.sample = sample
    assert loop.poll_once(compare=False) == []
    (line,) = _assert_clean(buf)
    assert line["level"] == "ERROR"
    assert line["msg"] == (
        f"sample crashed session={session} observation_id=None error=RuntimeError"
    )


def test_web_self_heal_exception_keeps_its_traceback_on_one_line(buf):
    """R8: the web's ``.exception()`` sites keep the full traceback (it is
    on the review list, not redacted); it is one JSON line."""
    from opspilot.web import __main__ as web_main

    stop = threading.Event()

    class Workbench:
        def self_heal_pass(self):
            stop.set()
            raise RuntimeError("database down\nline two")

        def release_authorize_pass(self):
            return None

        def release_anomaly_pass(self):
            return None

    thread = threading.Thread(
        target=web_main.self_heal_loop, args=(Workbench(), 0.01, stop)
    )
    thread.start()
    thread.join(5)
    (line,) = lines(buf)
    assert line["msg"] == "automatic observation pass failed"
    assert line["exc"].endswith("RuntimeError: database down\nline two")
