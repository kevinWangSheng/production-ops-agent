"""F2 step 3 (PR-A) acceptance: a stalled query is bounded by one whole-request deadline.

Written by an Agent that did not implement the change, from the contract only:

- ``feature_list.json`` F2 step 3: "Stall a query and verify bounded timeout,
  child-work cleanup, preserved partial evidence and explicit retry or handoff
  status".
- F2 contract pre-review section 6 (PR-A), contract points 1-5, decided r1 by the
  user on 2026-10-17 together with U3-A: a timeout goes back to the model as a
  tool error recorded as ``timeout`` with ``source_contact=possible``; the gateway
  does not retry on its own; Run-level failure still hands off per ADR-0005.
  R1 (thread + close the socket, no subprocess) and R2 (bytes of a stalled
  response are discarded; "preserved evidence" is the Run's other committed
  evidence) are the reversible defaults recorded there.
- C3 section 8 (``docs/design/technical-proposal-2026-09-07.md``): the SDK
  timeout and the gateway bound hold together, the authorization deadline wins;
  section 4: a pause or cancel invalidates in-flight results, history only.
- Public interface only: ``OtelDemoTransport.fetch``, ``TransportRequest``,
  ``TransportTimeout``, ``otel_demo_executor_factory``, ``ReadOnlyToolExecutor
  .execute`` and ``ToolOutcome``.

Interface assumptions (the implementer must keep these):

1. ``OtelDemoTransport.fetch`` treats ``TransportRequest.timeout_seconds`` as
   one wall-clock bound over connect, request, headers and body, and raises
   ``TransportTimeout`` when it passes (point 1-2), within 1 s of it.
2. On that timeout the HTTP connection is closed, so the source sees the
   socket go away (point 2), and no helper thread outlives the call (R1).
3. A response finishing inside the bound is unchanged (regression).
4. The executor path stays: ``TransportTimeout`` -> ``timeout`` /
   ``TOOL_TIMEOUT`` / ``possible``, dispatched, exactly one request (U3-A).

Hermetic: a stdlib socket server on 127.0.0.1 plays a source that drips one byte
every ``DRIP_SECONDS`` and never finishes -- each byte resets a per-socket-
operation timeout, so only a whole-request deadline can end the read. No
PostgreSQL, no model, no kind.

Not covered here: the Run-level ADR-0005 handoff (existing PG acceptance), and
whether the workbench tool row shows ``timeout`` and possible source contact
(pre-review point 6, unconfirmed -- left to the PR's own page test).
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.persistence import Lease
from opspilot.tools import (
    ToolRequest,
    TransportRequest,
    TransportResponse,
    TransportTimeout,
    Window,
)
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    SOURCE,
    TARGET_ID,
    OtelDemoConfig,
    OtelDemoTransport,
    otel_demo_executor_factory,
    otel_demo_face,
)
from tests.m1_tool_support import RecordingSink

DRIP_SECONDS = 0.2
# Contract point 1: return no later than the bound + 1 s.
SLACK_SECONDS = 1.0
# How long a test waits for a hung call before declaring it hung, so a red run
# fails instead of blocking the suite.
HANG_GUARD_SECONDS = 6.0
EXPR = "sum by(service_name) (increase(traces_span_metrics_calls_total[5m]))"
SERVER_THREAD_PREFIX = "f2-stall-source"


# -- the fake source ----------------------------------------------------------


class StallingSource:
    """A one-port HTTP source whose behaviour is set per test.

    ``mode``:
    - ``"drip_body"``: status line and headers (large Content-Length) at once,
      then one body byte every ``DRIP_SECONDS``, forever.
    - ``"drip_headers"``: the status line, then one header byte every
      ``DRIP_SECONDS``, never ending the headers.
    - ``"fast"``: ``body`` in one write.
    - ``"slow_finish"``: ``body`` dripped over ``finish_seconds``, then done.

    For every connection it records when the client closed it (``closed``).
    ``on_request`` runs once a request line arrives (used to flip control).
    """

    def __init__(self, mode: str, *, body: bytes = b"", finish_seconds: float = 0.0):
        self.mode = mode
        self.body = body
        self.finish_seconds = finish_seconds
        self.on_request = None
        self.requests: list[bytes] = []
        self.closed: list[threading.Event] = []
        self._stop = threading.Event()
        self._sockets: list[socket.socket] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._accept = threading.Thread(
            target=self._serve, name=f"{SERVER_THREAD_PREFIX}-accept", daemon=True
        )
        self._accept.start()

    def _serve(self) -> None:
        n = 0
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (socket.timeout, OSError):
                continue
            n += 1
            closed = threading.Event()
            self.closed.append(closed)
            self._sockets.append(conn)
            threading.Thread(
                target=self._handle,
                args=(conn, closed),
                name=f"{SERVER_THREAD_PREFIX}-conn-{n}",
                daemon=True,
            ).start()

    def _client_gone(self, conn: socket.socket) -> bool:
        try:
            readable, _, _ = select.select([conn], [], [], 0)
            if readable and conn.recv(4096, socket.MSG_PEEK) == b"":
                return True
        except OSError:
            return True
        return False

    def _send(self, conn: socket.socket, data: bytes) -> bool:
        try:
            conn.sendall(data)
            return True
        except OSError:
            return False

    def _handle(self, conn: socket.socket, closed: threading.Event) -> None:
        try:
            conn.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    closed.set()
                    return
                head += chunk
            conn.settimeout(None)
            self.requests.append(head)
            if self.on_request is not None:
                self.on_request()
            if self.mode == "fast":
                self._send(conn, self._response_head(len(self.body)) + self.body)
                return
            if self.mode == "slow_finish":
                self._send(conn, self._response_head(len(self.body)))
                pause = self.finish_seconds / max(1, len(self.body))
                for i in range(len(self.body)):
                    if not self._send(conn, self.body[i : i + 1]):
                        closed.set()
                        return
                    time.sleep(pause)
                return
            if self.mode == "drip_body":
                if not self._send(conn, self._response_head(10_000_000)):
                    closed.set()
                    return
                drip = b"{"
            else:  # drip_headers
                if not self._send(conn, b"HTTP/1.1 200 OK\r\n"):
                    closed.set()
                    return
                drip = b"X"
            while not self._stop.is_set():
                if self._client_gone(conn) or not self._send(conn, drip):
                    closed.set()
                    return
                time.sleep(DRIP_SECONDS)
        except OSError:
            closed.set()

    @staticmethod
    def _response_head(length: int) -> bytes:
        return (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {length}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()

    def stop(self) -> None:
        self._stop.set()
        for conn in self._sockets:
            try:
                conn.close()
            except OSError:
                pass
        self._listener.close()


@pytest.fixture
def source_factory():
    made: list[StallingSource] = []

    def make(mode: str, **kwargs) -> StallingSource:
        source = StallingSource(mode, **kwargs)
        made.append(source)
        return source

    yield make
    for source in made:
        source.stop()


def _client_threads() -> set[threading.Thread]:
    return {
        t
        for t in threading.enumerate()
        if t.is_alive() and not t.name.startswith(SERVER_THREAD_PREFIX)
    }


def _settles_to(baseline: set[threading.Thread], within: float = 2.0) -> set:
    """Threads created since ``baseline`` still alive after ``within`` seconds."""
    deadline = time.monotonic() + within
    while True:
        extra = _client_threads() - baseline
        if not extra or time.monotonic() >= deadline:
            return extra
        time.sleep(0.05)


def _guarded(call):
    """Run ``call`` in a daemon thread; return (finished, elapsed, result, error).

    A call still running after ``HANG_GUARD_SECONDS`` is reported unfinished so
    the test fails on the contract instead of hanging the suite.
    """
    box: dict = {}

    def target():
        try:
            box["result"] = call()
        except BaseException as error:  # noqa: BLE001 -- reported to the test
            box["error"] = error
        finally:
            box["elapsed"] = time.monotonic() - started

    started = time.monotonic()
    runner = threading.Thread(
        target=target, name=f"{SERVER_THREAD_PREFIX}-guard", daemon=True
    )
    runner.start()
    runner.join(HANG_GUARD_SECONDS)
    if runner.is_alive():
        return False, time.monotonic() - started, None, None
    return True, box["elapsed"], box.get("result"), box.get("error")


# -- transport level ----------------------------------------------------------


def _window() -> Window:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    return Window(end - timedelta(hours=1), end)


def _request(
    endpoint: str, *, timeout: float, window: Window | None = None
) -> TransportRequest:
    return TransportRequest(
        operation_id="step-1-t0",
        source=SOURCE,
        verb="query",
        endpoint=endpoint,
        selector={"opspilot.integration.id": TARGET_ID, "traces_endpoint": endpoint},
        params={"expr": EXPR},
        window=window or _window(),
        timeout_seconds=timeout,
        max_result_bytes=1_048_576,
        credential_ref=CREDENTIAL_REF,
        tool=METRICS_TOOL,
    )


def _transport() -> OtelDemoTransport:
    # Default opener: the real urllib stack, as the worker builds it.
    return OtelDemoTransport(credentials={CREDENTIAL_REF: None})


def _prometheus_body(window: Window) -> bytes:
    t = (window.end - timedelta(seconds=60)).timestamp()
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {"service_name": "checkout"}, "values": [[t, "3"]]}
                ],
            },
        }
    ).encode()


@pytest.mark.parametrize("mode", ["drip_body", "drip_headers"])
def test_stalled_source_times_out_within_the_whole_request_bound(source_factory, mode):
    """Point 1: the bound covers headers and body; each dripped byte resets only
    a per-operation socket timeout, so the call must still end at the bound."""
    source = source_factory(mode)
    timeout = 1.0
    finished, elapsed, result, error = _guarded(
        lambda: _transport().fetch(_request(source.url, timeout=timeout))
    )
    assert finished, (
        f"fetch still running after {HANG_GUARD_SECONDS}s (no whole-request bound)"
    )
    assert isinstance(error, TransportTimeout), (result, error)
    assert elapsed <= timeout + SLACK_SECONDS, elapsed
    # Not cut short either: the bound is the request's timeout, not less.
    assert elapsed >= timeout * 0.9, elapsed


def test_stalled_response_bytes_are_discarded_not_returned(source_factory):
    """R2: a stalled response is refused whole, never returned truncated."""
    source = source_factory("drip_body")
    finished, _, result, error = _guarded(
        lambda: _transport().fetch(_request(source.url, timeout=1.0))
    )
    assert finished
    assert result is None
    assert isinstance(error, TransportTimeout)


def test_timeout_closes_the_connection_to_the_source(source_factory):
    """Point 2: the connection is closed on timeout; the source sees it go."""
    source = source_factory("drip_body")
    finished, _, _, error = _guarded(
        lambda: _transport().fetch(_request(source.url, timeout=1.0))
    )
    assert finished and isinstance(error, TransportTimeout)
    assert len(source.closed) == 1
    assert source.closed[0].wait(2.0), "source still holds an open connection"


def test_timeout_leaves_no_helper_thread_behind(source_factory):
    """R1 cleanup: once fetch returns, no thread it started is still alive."""
    source = source_factory("drip_body")
    baseline = _client_threads()
    finished, _, _, error = _guarded(
        lambda: _transport().fetch(_request(source.url, timeout=1.0))
    )
    assert finished and isinstance(error, TransportTimeout)
    assert not _settles_to(baseline), "threads left running after the timeout"


def test_repeated_stalls_do_not_accumulate_threads_or_connections(source_factory):
    """Five stalls in a row: threads back to baseline, every connection closed."""
    source = source_factory("drip_body")
    transport = _transport()
    baseline = _client_threads()
    for _ in range(5):
        finished, elapsed, _, error = _guarded(
            lambda: transport.fetch(_request(source.url, timeout=0.6))
        )
        assert finished and isinstance(error, TransportTimeout)
        assert elapsed <= 0.6 + SLACK_SECONDS
    assert not _settles_to(baseline)
    assert len(source.closed) == 5
    assert all(event.wait(2.0) for event in source.closed)


def test_fast_response_is_unchanged(source_factory):
    """Regression: an ordinary response still comes back whole."""
    window = _window()
    body = _prometheus_body(window)
    source = source_factory("fast", body=body)
    response = _transport().fetch(_request(source.url, timeout=5.0, window=window))
    assert isinstance(response, TransportResponse)
    assert response.body == body
    assert len(source.requests) == 1


def test_slow_response_that_finishes_inside_the_bound_is_kept(source_factory):
    """Regression: the bound is whole-request, not a per-byte rate limit."""
    window = _window()
    body = _prometheus_body(window)
    source = source_factory("slow_finish", body=body, finish_seconds=0.8)
    finished, _, result, error = _guarded(
        lambda: _transport().fetch(_request(source.url, timeout=4.0, window=window))
    )
    assert finished and error is None, error
    assert isinstance(result, TransportResponse)
    assert result.body == body


# -- executor level -----------------------------------------------------------


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeStore:
    """The ``DurableStore`` reads/writes the profile factory touches."""

    def __init__(self, lease: Lease, *, deadline: datetime):
        self.lease = lease
        self.deadline = deadline
        self.control = {
            "incident_generation": lease.control_generation,
            "incident_state": "running",
            "global_suspended": False,
            "global_generation": lease.global_suspension_generation,
            "target_suspended": False,
            "target_generation": lease.target_suspension_generation,
        }
        self.charges: list[tuple] = []

    def rebuild(self, incident_id):
        return {
            "run": {
                "run_id": self.lease.run_id,
                "deadline": self.deadline,
                "tool_operations_used": 0,
                "tool_seconds_used": 0.0,
            }
        }

    def charge_tool(
        self,
        lease,
        operation_id,
        seconds,
        *,
        max_operations,
        max_tool_seconds,
        dispatch_id,
    ):
        self.charges.append((operation_id, seconds, dispatch_id))

    def control_state(self, incident_id):
        return dict(self.control)

    def pause(self):
        """What an incident pause looks like to the gateway: a new generation."""
        self.control = {
            **self.control,
            "incident_state": "paused",
            "incident_generation": self.control["incident_generation"] + 1,
        }


def _executor(source: StallingSource, *, run_seconds: float):
    clock = RealClock()
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=3,
        global_suspension_generation=1,
        target_suspension_generation=2,
    )
    deadline = clock.now() + timedelta(seconds=run_seconds)
    store = FakeStore(lease, deadline=deadline)
    sink = RecordingSink()
    face = otel_demo_face(clock)
    run_input = face.input_for(
        run_id=str(lease.run_id),
        question="Why is checkout slow?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=2,
    )
    config = OtelDemoConfig(
        prometheus_url=source.url, jaeger_url=source.url, token=None
    )
    factory = otel_demo_executor_factory(
        store, evidence=sink, clock=clock, config=config
    )
    return factory(lease, run_input), store, sink, run_input


def _scope_window_json(run_input) -> dict:
    return run_input.evidence_context["time_policies"][0]["window"]


def _call(run_input, index: int) -> ToolRequest:
    return ToolRequest(
        step_id="step-1",
        tool_index=index,
        tool_name=METRICS_TOOL,
        target_ref=TARGET_ID,
        params={"expr": EXPR},
        window=_scope_window_json(run_input),
    )


class SwitchingSource(StallingSource):
    """First request answered fast with ``body``; every later one stalls."""

    def __init__(self, body: bytes):
        super().__init__("fast", body=body)

    def _handle(self, conn, closed):
        self.mode = "fast" if not self.requests else "drip_body"
        super()._handle(conn, closed)


@pytest.fixture
def switching_source():
    made: list[StallingSource] = []

    def make(body: bytes) -> SwitchingSource:
        source = SwitchingSource(body)
        made.append(source)
        return source

    yield make
    for source in made:
        source.stop()


# The registered per-query timeout is 30 s; the gateway clips it to the Run's
# remaining deadline (C3 section 8, ``executor.py`` _reserve), which is how these
# tests get a short bound without touching the registration.
RUN_SECONDS = 2.5


def test_stalled_query_becomes_a_timeout_tool_result_inside_the_run_bound(
    source_factory,
):
    """U3-A + point 2: ``timeout`` / ``TOOL_TIMEOUT`` / ``possible``, sent, back
    to the model as a tool result, inside the clipped bound + 1 s."""
    source = source_factory("drip_body")
    executor, store, _, run_input = _executor(source, run_seconds=RUN_SECONDS)
    finished, elapsed, outcome, error = _guarded(
        lambda: executor.execute(_call(run_input, 0))
    )
    assert finished, f"execute still running after {HANG_GUARD_SECONDS}s"
    assert error is None, error
    assert elapsed <= RUN_SECONDS + SLACK_SECONDS, elapsed
    assert outcome.status == "timeout"
    assert outcome.reason == "TOOL_TIMEOUT"
    assert outcome.source_contact == "possible"
    assert outcome.operation.dispatched is True
    assert outcome.model_view.get("status") == "timeout"
    # The measured wall time charged to the Run stays inside the bound.
    assert store.charges and store.charges[-1][1] <= RUN_SECONDS + SLACK_SECONDS


def test_timeout_keeps_earlier_committed_evidence_and_is_not_retried(switching_source):
    """Partial evidence: the first query's committed evidence survives the
    second query's stall unchanged; the stall adds no adopted evidence; the
    gateway sends the stalled query exactly once (U3-A: no auto retry)."""
    probe_clock = RealClock()
    window = Window(probe_clock.now() - timedelta(hours=24), probe_clock.now())
    source = switching_source(_prometheus_body(window))
    executor, store, sink, run_input = _executor(source, run_seconds=RUN_SECONDS)

    first = executor.execute(_call(run_input, 0))
    assert first.status in ("ok", "no_data"), (first.status, first.reason)
    assert first.adopted
    committed = [record.evidence_id for record in sink.records]
    assert committed

    finished, _, second, error = _guarded(lambda: executor.execute(_call(run_input, 1)))
    assert finished, f"execute still running after {HANG_GUARD_SECONDS}s"
    assert error is None, error
    assert second.status == "timeout"
    assert second.adopted is False

    assert [record.evidence_id for record in sink.records][
        : len(committed)
    ] == committed
    assert not any(record.adopted for record in sink.records[len(committed) :]), (
        "a stalled read must not add adopted evidence"
    )
    # Wait past one more bound: no second attempt reaches the source.
    time.sleep(SLACK_SECONDS + 0.5)
    assert len(source.requests) == 2, len(source.requests)
    assert source.closed[-1].wait(2.0), "stalled connection left open"


def test_pause_while_stalled_is_reported_as_the_human_decision(source_factory):
    """Point 3: a pause taken while the read is stalled outranks the timeout;
    the outcome is ``denied`` with the control reason, and still bounded."""
    source = source_factory("drip_body")
    executor, store, _, run_input = _executor(source, run_seconds=RUN_SECONDS)
    source.on_request = store.pause
    finished, elapsed, outcome, error = _guarded(
        lambda: executor.execute(_call(run_input, 0))
    )
    assert finished, f"execute still running after {HANG_GUARD_SECONDS}s"
    assert error is None, error
    assert elapsed <= RUN_SECONDS + SLACK_SECONDS, elapsed
    assert outcome.status == "denied"
    assert outcome.reason in ("CONTROL_GENERATION_CHANGED", "SUSPENDED"), outcome.reason
    assert outcome.source_contact == "possible"
