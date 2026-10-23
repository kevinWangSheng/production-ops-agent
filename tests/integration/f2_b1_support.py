"""Harness for the independent F2 PR-B1 acceptance tests (capacity + priority).

The worker under test is the real entry point, ``python -m
opspilot.worker_main``'s ``main()``, run as a child process under a
throwaway ``opspilot_worker`` login role, configured only through its
environment (``OPSPILOT_WORKER_CAPACITY``, ``OPSPILOT_POOL_MAX_SIZE``, ...).
The one substitution is the model provider: every ``DeepSeekClient`` the
child builds is pointed at a local HTTPS stub (``ModelStub``) that the test
controls request by request -- hold until released, reject (401, a
``MODEL_REJECTED`` handoff), hang until the client gives up, answer a tool
round or a report. The child trusts the stub's throwaway certificate and
nothing else; no request leaves the machine.

What the tests observe is external only: the Run rows (state, owner,
lease), the subject events (``run_claimed`` / ``run_handoff`` order and
``recorded_at``), the stub's view of concurrent model requests, and whether
the process starts, keeps running or exits.

Top-level imports are stdlib only: the child imports this module before
anything of ``opspilot``.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TAG_RE = re.compile(rb"F2B1T[0-9a-f]{12}")
CRASH_CONTENT = "F2B1-INJECTED-ATTEMPT-CRASH"
HOLD_CAP_SECONDS = 60.0


def new_tag() -> str:
    return "F2B1T" + os.urandom(6).hex()


def wait_until(
    predicate: Callable[[], Any], *, timeout: float, interval: float = 0.05
) -> Any:
    """Poll ``predicate`` until truthy or ``timeout``; returns the last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


# -- child entry point ------------------------------------------------------


def child_main() -> None:  # pragma: no cover - runs in the child process
    """The product ``main()`` with the model endpoint redirected to the stub."""
    context = ssl.create_default_context(cafile=os.environ["F2B1_CA_FILE"])
    ssl._create_default_https_context = lambda *a, **k: context  # type: ignore[attr-defined]

    from opspilot.investigation import client as model_client

    endpoint = os.environ["F2B1_ENDPOINT"]
    original_init = model_client.DeepSeekClient.__init__
    original_complete = model_client.DeepSeekClient.complete

    def init(self: Any, api_key: str, **kwargs: Any) -> None:
        kwargs["endpoint"] = endpoint
        original_init(self, api_key, **kwargs)

    def complete(self: Any, call: Any) -> Any:
        reply = original_complete(self, call)
        if reply.content == CRASH_CONTENT:
            # An attempt that dies with something other than a refusal: the
            # crashed-slot case of contract B1 item 6.
            raise RuntimeError("injected attempt crash")
        return reply

    model_client.DeepSeekClient.__init__ = init  # type: ignore[method-assign]
    model_client.DeepSeekClient.complete = complete  # type: ignore[method-assign]

    from opspilot.worker_main import main

    raise SystemExit(main([]))


# -- the model stub ---------------------------------------------------------


@dataclass(frozen=True)
class Act:
    """What the stub does with one model request of a tag.

    ``kind``: ``reject`` (401), ``crash`` (200, content the child turns into
    an exception), ``tool`` (one fixture tool call), ``report`` (a report
    citing the last tool result), ``hang`` (never answer; wait for the
    client to close the connection). ``hold``: wait for ``release(tag)``
    first.
    """

    kind: str = "reject"
    hold: bool = False
    delay: float = 0.0


@dataclass
class Arrival:
    tag: str
    index: int
    at: float


@dataclass
class ModelStub:
    cert: Path
    key: Path
    plans: dict[str, list[Act]] = field(default_factory=dict)
    arrivals: list[Arrival] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    released: set[str] = field(default_factory=set)
    inflight: set[tuple[str, int]] = field(default_factory=set)
    peak: int = 0
    eof_at: dict[str, float] = field(default_factory=dict)
    writes: list[tuple[str, int, bool]] = field(default_factory=list)
    untagged: int = 0
    release_all_flag: bool = False

    def __post_init__(self) -> None:
        self.cond = threading.Condition()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                stub._handle(self)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert), str(self.key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"https://127.0.0.1:{self.server.server_port}/v1/chat/completions"

    # control ---------------------------------------------------------------

    def plan(self, tag: str, *acts: Act) -> None:
        with self.cond:
            self.plans[tag] = list(acts)

    def release(self, tag: str) -> None:
        with self.cond:
            self.released.add(tag)
            self.cond.notify_all()

    def release_all(self) -> None:
        with self.cond:
            self.release_all_flag = True
            self.cond.notify_all()

    def close(self) -> None:
        self.release_all()
        self.server.shutdown()
        self.server.server_close()

    # observation -------------------------------------------------------------

    def arrived(self) -> list[str]:
        with self.cond:
            return [a.tag for a in self.arrivals]

    def first_arrival(self, tag: str) -> float | None:
        with self.cond:
            for a in self.arrivals:
                if a.tag == tag:
                    return a.at
        return None

    def inflight_tags(self) -> set[str]:
        with self.cond:
            return {tag for tag, _ in self.inflight}

    def count(self, tag: str) -> int:
        with self.cond:
            return self.counts.get(tag, 0)

    # serving -----------------------------------------------------------------

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(length)
        match = TAG_RE.search(body)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        tag = match.group(0).decode() if match else None
        # Only the investigation loop's requests carry the tool schemas; a
        # postmortem generation for the same incident must not count.
        if tag is None or tag not in self.plans or "tools" not in payload:
            with self.cond:
                self.untagged += 1
            self._send(handler, 401, {"error": {"message": "stub: unplanned"}}, None, 0)
            return
        with self.cond:
            index = self.counts.get(tag, 0)
            self.counts[tag] = index + 1
            acts = self.plans[tag]
            act = acts[index] if index < len(acts) else Act("reject")
            self.arrivals.append(Arrival(tag, index, time.monotonic()))
            self.inflight.add((tag, index))
            self.peak = max(self.peak, len(self.inflight))
        try:
            if act.kind == "hang":
                self._wait_for_eof(handler, tag)
                return
            if act.hold:
                with self.cond:
                    self.cond.wait_for(
                        lambda: tag in self.released or self.release_all_flag,
                        timeout=HOLD_CAP_SECONDS,
                    )
            if act.delay:
                time.sleep(act.delay)
            if act.kind == "reject":
                self._send(
                    handler, 401, {"error": {"message": "stub reject"}}, tag, index
                )
            elif act.kind == "crash":
                self._send(handler, 200, _completion(CRASH_CONTENT, None), tag, index)
            elif act.kind == "tool":
                self._send(handler, 200, _completion(None, _tool_calls()), tag, index)
            elif act.kind == "report":
                evidence_id = _last_evidence_id(payload) or "missing"
                self._send(
                    handler, 200, _completion(_report(evidence_id), None), tag, index
                )
            else:  # pragma: no cover - test bug
                raise AssertionError(act.kind)
        finally:
            with self.cond:
                self.inflight.discard((tag, index))
                self.cond.notify_all()

    def _wait_for_eof(self, handler: BaseHTTPRequestHandler, tag: str) -> None:
        sock = handler.connection
        deadline = time.monotonic() + HOLD_CAP_SECONDS
        while time.monotonic() < deadline:
            try:
                ready, _, _ = select.select([sock], [], [], 0.1)
                if not ready:
                    continue
                if sock.recv(1) == b"":
                    break
            except (OSError, ssl.SSLError, ValueError):
                break
        else:
            return
        with self.cond:
            self.eof_at.setdefault(tag, time.monotonic())
            self.cond.notify_all()

    def _send(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        payload: dict[str, Any],
        tag: str | None,
        index: int,
    ) -> None:
        data = json.dumps(payload).encode()
        ok = True
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            handler.wfile.flush()
        except (OSError, ssl.SSLError):
            ok = False
        if tag is not None:
            with self.cond:
                self.writes.append((tag, index, ok))


def _completion(content: str | None, tool_calls: list[dict] | None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "reasoning_content": "reasoned",
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "f2b1-" + os.urandom(4).hex(),
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 16, "total_tokens": 24},
    }


def _tool_calls() -> list[dict[str, Any]]:
    from opspilot.tools.fixture import FIXTURE_TOOL

    return [
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": FIXTURE_TOOL,
                "arguments": '{"expr":"rate(http_errors[5m])"}',
            },
        }
    ]


def _last_evidence_id(payload: dict[str, Any]) -> str | None:
    for message in reversed(payload.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "tool":
            try:
                return json.loads(message["content"])["evidence_id"]
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _report(evidence_id: str) -> str:
    from tests.m1_investigation_support import report_json

    return report_json(evidence_id=evidence_id)


def make_certificate(directory: Path) -> tuple[Path, Path]:
    cert, key = directory / "stub-cert.pem", directory / "stub-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return cert, key


# -- the worker child -------------------------------------------------------

_DROPPED_ENV = (
    "OPSPILOT_DSN",
    "OPSPILOT_WEB_DSN",
    "OPSPILOT_ENV_FILE",
    "OPSPILOT_WORKER_CAPACITY",
    "OPSPILOT_BACKLOG_MAX",
    "OPSPILOT_POOL_MIN_SIZE",
    "OPSPILOT_POOL_MAX_SIZE",
    "OPSPILOT_POOL_TIMEOUT_SECONDS",
    "OPSPILOT_POOL_MAX_IDLE_SECONDS",
    "OPSPILOT_WORKER_BATCH",
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "DEEPSEEK_API_KEY",
)


class WorkerProcess:
    """One ``main()`` child; stopped with SIGTERM like a deployment would."""

    def __init__(
        self,
        stub: ModelStub,
        worker_dsn: str,
        log_path: Path,
        *,
        poll_seconds: float = 0.2,
        **env: str,
    ) -> None:
        self.stub = stub
        self.log_path = log_path
        child_env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
        child_env.update(
            {
                "OPSPILOT_WORKER_DSN": worker_dsn,
                "DEEPSEEK_API_KEY": "f2b1-stub-key-not-a-credential",
                "OPSPILOT_TOOL_PROFILE": "fixture",
                "OPSPILOT_TRACE": "off",
                "OPSPILOT_WORKER_POLL_SECONDS": str(poll_seconds),
                "OPSPILOT_WORKER_GRACE_SECONDS": "5",
                "OPSPILOT_POSTMORTEM_POLL_SECONDS": "3600",
                "F2B1_ENDPOINT": stub.endpoint,
                "F2B1_CA_FILE": str(stub.cert),
                "PYTHONUNBUFFERED": "1",
            }
        )
        child_env.update(env)
        self._log = open(log_path, "wb")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from tests.integration.f2_b1_support import child_main; child_main()",
            ],
            cwd=REPO,
            env=child_env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def alive(self) -> bool:
        return self.proc.poll() is None

    def log(self) -> str:
        if not self._log.closed:
            self._log.flush()
        return self.log_path.read_text(errors="replace")

    def wait_exit(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> int | None:
        self.stub.release_all()
        if self.alive():
            self.proc.send_signal(signal.SIGTERM)
        code = self.wait_exit(30)
        if code is None:
            # Our own child (this Popen), never matched by name.
            self.proc.kill()
            code = self.wait_exit(10)
        self._log.close()
        return code


# -- database observations (owner connection, outside the product) ----------


def neutralize_claimable(conn: Any) -> None:
    """Make every Run that is claimable right now overdue, so the worker's
    own deadline sweep parks it instead of claiming it. The lab database
    keeps every earlier test's rows; a stale claimable Run would otherwise
    take a slot and disturb the order and capacity measured here."""
    conn.execute(
        "UPDATE opspilot_runs r SET deadline = clock_timestamp() - interval '1 second' "
        "FROM opspilot_incidents i WHERE i.current_run_id = r.run_id "
        "AND r.state IN ('queued','running') AND r.deadline > clock_timestamp()"
    )
    conn.commit()


def run_rows(conn: Any, incidents: Iterable[Any]) -> dict[Any, dict[str, Any]]:
    """Current Run of each incident, with the incident's own state and mode."""
    names = (
        "incident_id",
        "run_id",
        "state",
        "owner",
        "lease_until",
        "deadline",
        "lease_live",
        "incident_state",
        "mode",
    )
    rows = conn.execute(
        "SELECT i.incident_id, i.current_run_id, r.state, r.owner, "
        "r.lease_until, r.deadline, (r.lease_until IS NOT NULL AND "
        "r.lease_until > clock_timestamp()), i.state, i.mode "
        "FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id = i.current_run_id "
        "WHERE i.incident_id = ANY(%s)",
        (list(incidents),),
    ).fetchall()
    conn.commit()
    return {row[0]: dict(zip(names, row)) for row in rows}


def events_of(
    conn: Any, incidents: Iterable[Any], kind: str
) -> list[tuple[Any, Any, dict]]:
    """(subject_id, recorded_at, payload) of ``kind``, oldest first."""
    rows = conn.execute(
        "SELECT subject_id, recorded_at, payload FROM opspilot_subject_events "
        "WHERE kind = %s AND subject_id = ANY(%s) ORDER BY recorded_at, sequence",
        (kind, list(incidents)),
    ).fetchall()
    conn.commit()
    return [(r[0], r[1], r[2]) for r in rows]


def running_live(conn: Any, incidents: Iterable[Any]) -> int:
    row = conn.execute(
        "SELECT count(*) FROM opspilot_runs r JOIN opspilot_incidents i "
        "ON i.current_run_id = r.run_id WHERE i.incident_id = ANY(%s) "
        "AND r.state = 'running' AND r.lease_until > clock_timestamp()",
        (list(incidents),),
    ).fetchone()
    conn.commit()
    return int(row[0])


class Sampler:
    """Background sampler of the peak number of running, leased Runs."""

    def __init__(self, dsn: str, incidents: Iterable[Any]) -> None:
        import psycopg

        self.incidents = list(incidents)
        self.conn = psycopg.connect(dsn)
        self.peak = 0
        self.samples = 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, running_live(self.conn, self.incidents))
            self.samples += 1
            self._stop.wait(0.03)

    def close(self) -> None:
        self._stop.set()
        self.thread.join(5)
        self.conn.close()
