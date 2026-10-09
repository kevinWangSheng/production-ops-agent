"""Issue #156: each Prometheus request records when it actually left the
Observer (``sent_at``), the raw bundle keeps it, the replay binds it and the
F6 audit orders every ``read_only_query`` by it.

User decision 2026-10-09 (issue #156 comment): the instant is read right
after the request bytes were handed to the socket, from the Observer's UTC
wall clock; a request never sent has none; within one reading the times do
not decrease and none lies after ``sample_time`` (60 s tolerance); a value
that breaks this is an integrity mismatch; an older bundle without the field
falls back to ``evaluated_at`` -> ``submitted_at`` and is marked approximate;
ordering against database-clock control rows keeps the 60 s tolerance and
marks what it cannot order as uncertain.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any

import pytest

from opspilot.acceptance import recovery_outcome
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.observer.replay import INTEGRITY_MISMATCH, replay_history
from opspilot.observer.sampler import take_sample
from tests.m1_02_replay_support import NOW, healthy_history, rehash
from tests.test_f6_recovery_outcome import _control, _records, _scenario
from tests.test_m1_observer import (
    PROFILE,
    FakeSource,
    FakeStore,
    healthy_answers,
    lease,
    vector,
)

KINDS = ("query", "coverage", "freshness")
#: issue #156 decision 2: the clock tolerance the audit orders within
ORDER_SKEW = timedelta(seconds=60)


# --- the source: the socket-write boundary


class _Handler(BaseHTTPRequestHandler):
    delay = 0.0
    received: list[float] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).received.append(time.monotonic())
        time.sleep(type(self).delay)
        body = vector(1.0)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def server():
    class Handler(_Handler):
        received: list[float] = []

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, Handler
    finally:
        httpd.shutdown()
        httpd.server_close()


def _monotonic_clock() -> tuple[list[float], Any]:
    """A wall clock that also remembers the monotonic instant it was read."""
    reads: list[float] = []

    def clock() -> datetime:
        reads.append(time.monotonic())
        return datetime.now(timezone.utc)

    return reads, clock


def test_a_sent_request_records_the_send_not_the_response(server):
    httpd, handler = server
    handler.delay = 0.4
    reads, clock = _monotonic_clock()
    source = PrometheusReadOnlySource(
        f"http://127.0.0.1:{httpd.server_address[1]}", clock=clock
    )
    result = source.instant("up", at=NOW, timeout_seconds=5)
    returned = time.monotonic()
    assert result.status == "ok"
    assert result.sent_at is not None and result.sent_at.tzinfo is not None
    assert len(reads) == 1
    # read before the server answered (it held the response 0.4 s)
    assert reads[0] <= handler.received[0] + 0.05
    assert returned - reads[0] >= 0.35


def test_a_request_that_times_out_after_it_was_sent_keeps_its_send_time():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        source = PrometheusReadOnlySource(
            f"http://127.0.0.1:{listener.getsockname()[1]}",
            clock=lambda: NOW,
        )
        result = source.instant("up", at=NOW, timeout_seconds=1)
    finally:
        listener.close()
    assert (result.status, result.detail) == ("timeout", "TIMEOUT")
    assert result.sent_at == NOW


def test_a_request_that_never_connected_has_no_send_time():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens: the connect is refused
    source = PrometheusReadOnlySource(f"http://127.0.0.1:{port}", clock=lambda: NOW)
    result = source.instant("up", at=NOW, timeout_seconds=2)
    assert (result.status, result.detail) == ("failed", "UNREACHABLE")
    assert result.sent_at is None


class _Opener:
    def __init__(self, *, refuse: bool = False):
        self.refuse = refuse

    def open(self, request, timeout=None):
        if self.refuse:
            raise urllib.error.URLError("refused")
        response = BytesIO(vector(1.0))
        response.status = 200  # type: ignore[attr-defined]
        return response


def test_an_injected_opener_records_the_send_once_it_answered():
    sent = PrometheusReadOnlySource(
        "http://p.invalid", opener=_Opener(), clock=lambda: NOW
    ).instant("up", at=NOW, timeout_seconds=2)
    refused = PrometheusReadOnlySource(
        "http://p.invalid", opener=_Opener(refuse=True), clock=lambda: NOW
    ).instant("up", at=NOW, timeout_seconds=2)
    assert sent.sent_at == NOW
    assert refused.sent_at is None


# --- the sampler: each part of the bundle carries its own send time


class _SentSource(FakeSource):
    def __init__(self, answers, start: datetime):
        super().__init__(answers)
        self.at = start

    def instant(self, expr, *, at, timeout_seconds):
        result = super().instant(expr, at=at, timeout_seconds=timeout_seconds)
        self.at += timedelta(milliseconds=100)
        return replace(result, sent_at=self.at)


def test_the_bundle_keeps_each_requests_send_time_and_none_for_unsent():
    store = FakeStore(clock=[NOW, NOW + timedelta(seconds=2)])
    source = _SentSource(healthy_answers(), NOW)
    take_sample(lease(), PROFILE, source, store)
    ((_, _, readings),) = store.submitted
    times = []
    for reading in readings:
        bundle = json.loads(reading.raw)
        for kind in KINDS:
            times.append(datetime.fromisoformat(bundle[kind]["sent_at"]))
    assert times == sorted(times) and len(set(times)) == len(times)
    assert all(at.tzinfo is not None for at in times)


def test_a_part_never_sent_for_want_of_lease_budget_has_no_send_time():
    store = FakeStore(clock=[NOW, NOW + timedelta(seconds=1)])
    source = _SentSource(healthy_answers(), NOW)
    # a lease ending inside the submission margin: nothing can be sent
    short = replace(lease(), lease_until=NOW + timedelta(seconds=5))
    take_sample(short, PROFILE, source, store)
    ((_, _, readings),) = store.submitted
    for reading in readings:
        bundle = json.loads(reading.raw)
        for kind in KINDS:
            assert bundle[kind]["detail"] == "LEASE_BUDGET"
            assert "sent_at" not in bundle[kind]


# --- the replay binds the send times


def _with_sent(history, at_of) -> dict:
    """Every reading's bundle with ``sent_at`` set by ``at_of(sample_index,
    reading_index, kind)`` (a string is written as is, ``None`` drops it)."""
    for s_index, sample in enumerate(history["samples"]):
        for r_index, reading in enumerate(sample["readings"]):
            bundle = json.loads(bytes(reading["raw"]))
            for kind in KINDS:
                value = at_of(s_index, r_index, kind)
                if value is None:
                    bundle[kind].pop("sent_at", None)
                else:
                    bundle[kind]["sent_at"] = (
                        value if isinstance(value, str) else value.isoformat()
                    )
            sample["readings"][r_index] = rehash(
                reading, json.dumps(bundle, sort_keys=True).encode()
            )
    return history


def _genuine(s_index, r_index, kind):
    # sent just after each window end, in request order
    return NOW + timedelta(
        seconds=60 * s_index, milliseconds=1 + 30 * r_index + 10 * KINDS.index(kind)
    )


def test_recorded_send_times_replay_consistently():
    history = _with_sent(healthy_history(), _genuine)
    result = replay_history(history)
    assert result.consistent and result.recovery_verdict == "healthy"


def test_a_send_time_within_the_skew_after_sample_time_is_accepted():
    # sample_time is window end + 1 s (FakeStore); 60 s later is the limit
    history = _with_sent(
        healthy_history(count=1),
        lambda s, r, k: NOW + timedelta(seconds=61),
    )
    assert replay_history(history).consistent


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("2026-10-07T12:00:01", "SENT_AT_INVALID"),
        ("2026-10-07", "SENT_AT_INVALID"),
        ("20261007T120001Z", "SENT_AT_INVALID"),
        (1759838401, "SENT_AT_INVALID"),
        ("2026-10-07T20:00:01+08:00", "SENT_AT_INVALID"),
        (NOW + timedelta(seconds=62), "SENT_AT_AFTER_SAMPLE_TIME"),
    ],
    ids=["naive", "date-only", "compact", "number", "non-utc", "after-sample-time"],
)
def test_a_send_time_the_observer_cannot_have_written_is_an_integrity_mismatch(
    value, code
):
    history = healthy_history(count=1)

    def at_of(s, r, kind):
        if (r, kind) == (0, "coverage"):
            return value if not isinstance(value, int) else None
        return _genuine(s, r, kind)

    _with_sent(history, at_of)
    if isinstance(value, int):
        reading = history["samples"][0]["readings"][0]
        bundle = json.loads(bytes(reading["raw"]))
        bundle["coverage"]["sent_at"] = value
        history["samples"][0]["readings"][0] = rehash(
            reading, json.dumps(bundle, sort_keys=True).encode()
        )
    result = replay_history(history)
    assert result.recovery_verdict == "unknown"
    assert INTEGRITY_MISMATCH in result.integrity
    codes = result.samples[0].integrity
    assert any(c.startswith(code) and c.endswith(":coverage") for c in codes), codes


def test_send_times_that_go_backwards_within_a_reading_are_refused():
    def at_of(s, r, kind):
        if (r, kind) == (0, "freshness"):
            return NOW - timedelta(seconds=1)
        return _genuine(s, r, kind)

    result = replay_history(_with_sent(healthy_history(count=1), at_of))
    assert result.recovery_verdict == "unknown"
    assert any(c.startswith("SENT_AT_ORDER:") for c in result.samples[0].integrity)


def test_a_send_time_on_a_request_never_sent_is_refused():
    store = FakeStore(clock=[NOW, NOW + timedelta(seconds=1)])
    short = replace(lease(), lease_until=NOW + timedelta(seconds=5))
    take_sample(short, PROFILE, FakeSource(healthy_answers()), store)
    history = healthy_history(count=1)
    ((_, _, readings),) = store.submitted
    budget = json.loads(readings[0].raw)
    original = history["samples"][0]["readings"][0]
    bundle = json.loads(bytes(original["raw"]))
    bundle["query"] = {**budget["query"], "sent_at": NOW.isoformat()}
    history["samples"][0]["readings"][0] = rehash(
        original, json.dumps(bundle, sort_keys=True).encode()
    )
    codes = replay_history(history).samples[0].integrity
    assert any(c.startswith("SENT_AT_UNSENT:") for c in codes), codes


def test_a_bundle_without_send_times_still_replays():
    """Older bundles (``docs/evidence/m1-02-live-2``) carry none."""
    history = healthy_history()
    assert (
        "sent_at"
        not in json.loads(bytes(history["samples"][0]["readings"][0]["raw"]))["query"]
    )
    assert replay_history(history).consistent


# --- the audit orders each query by its own send time


def _takeover(history, at):
    return {
        **_control(1, history["session"]["incident_id"]),
        "action": "takeover",
        "created_at": at,
    }


def _events(history, controls):
    return recovery_outcome(
        _scenario(history), _records(history, controls=controls)
    ).action_events


def test_each_query_is_its_own_event_at_its_send_time():
    history = _with_sent(healthy_history(count=2), _genuine)
    outcome = recovery_outcome(_scenario(history), _records(history))
    queries = [e for e in outcome.action_events if e.action == "read_only_query"]
    assert (
        len(queries)
        == outcome.actions.count("read_only_query")
        == 2 * len(PROFILE.signals) * 3
    )
    assert all(
        e.time_source == "sent_at" and not e.approximate and not e.order_uncertain
        for e in queries
    )
    assert [e.at for e in queries] == sorted(e.at for e in queries)
    assert outcome.actions == tuple(e.action for e in outcome.action_events)
    sent = outcome.recovery_samples[0].signals["rate"].sent_at
    assert set(sent) == set(KINDS)


def test_a_takeover_after_the_window_but_before_the_send_precedes_the_queries():
    """The race the issue names: the window was chosen, the human took over,
    and only then did the requests go out. ``evaluated_at`` put them before
    the takeover; the send time puts them after."""
    history = healthy_history(count=1)
    history["samples"][0]["submitted_at"] = NOW + timedelta(seconds=400)
    took_at = NOW + timedelta(seconds=100)
    before = [e.action for e in _events(history, [_takeover(history, took_at)])]
    assert before.index("human_takeover") > before.index("read_only_query")

    _with_sent(history, lambda s, r, k: NOW + timedelta(seconds=200 + r))
    after = [e.action for e in _events(history, [_takeover(history, took_at)])]
    took = after.index("human_takeover")
    assert "read_only_query" not in after[:took]
    assert after.count("read_only_query") == before.count("read_only_query")


def test_a_query_sent_before_a_takeover_stays_before_it():
    history = _with_sent(
        healthy_history(count=1), lambda s, r, k: NOW + timedelta(seconds=1)
    )
    history["samples"][0]["submitted_at"] = NOW + timedelta(seconds=300)
    events = _events(history, [_takeover(history, NOW + timedelta(seconds=100))])
    actions = [e.action for e in events]
    took = actions.index("human_takeover")
    assert "read_only_query" not in actions[took:]
    assert actions.index("persist_observation") > took


def test_within_the_skew_the_record_position_orders_and_both_are_uncertain():
    history = _with_sent(
        healthy_history(count=1), lambda s, r, k: NOW + timedelta(seconds=100)
    )
    history["samples"][0]["submitted_at"] = NOW + timedelta(seconds=400)
    # 30 s after the send by the database clock: inside ORDER_SKEW
    took_at = NOW + timedelta(seconds=130)
    assert timedelta(seconds=30) < ORDER_SKEW
    events = _events(history, [_takeover(history, took_at)])
    actions = [e.action for e in events]
    # control rows come first in the record position
    assert actions.index("human_takeover") < actions.index("read_only_query")
    takeover = next(e for e in events if e.action == "human_takeover")
    assert takeover.order_uncertain
    assert all(e.order_uncertain for e in events if e.action == "read_only_query")
    persist = next(e for e in events if e.action == "persist_observation")
    assert not persist.order_uncertain


def test_a_query_and_its_own_persistence_are_not_marked_uncertain():
    history = _with_sent(healthy_history(count=1), _genuine)
    events = _events(history, [])
    assert [e.action for e in events][-1] == "persist_observation"
    assert not any(e.order_uncertain for e in events)


def test_a_bundle_without_send_times_is_ordered_approximately():
    history = healthy_history(count=1)
    events = _events(history, [])
    queries = [e for e in events if e.action == "read_only_query"]
    assert queries and all(
        e.approximate and e.time_source == "evaluated_at" and e.at == NOW
        for e in queries
    )


def test_a_tampered_send_time_falls_back_and_the_outcome_is_unverified():
    history = _with_sent(
        healthy_history(count=1),
        lambda s, r, k: "2026-10-07T12:00:01" if r == 0 else _genuine(s, r, k),
    )
    outcome = recovery_outcome(_scenario(history), _records(history))
    assert outcome.recovery_verdict == "unknown"
    first = [e for e in outcome.action_events if e.action == "read_only_query"][:3]
    assert all(e.approximate and e.time_source == "evaluated_at" for e in first)


def test_a_send_that_finishes_right_at_the_deadline_keeps_its_send_time(
    monkeypatch,
):
    """Codex review of #156, P1: the request bytes reached the socket before
    the deadline cut, but ``endheaders()`` returned only after it."""
    from http.client import HTTPConnection

    original = HTTPConnection.endheaders

    def slow_return(self, *args, **kwargs):
        original(self, *args, **kwargs)  # the bytes are in the socket
        time.sleep(1.2)  # ... and the call returns past the 1 s deadline

    monkeypatch.setattr(HTTPConnection, "endheaders", slow_return)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        source = PrometheusReadOnlySource(
            f"http://127.0.0.1:{listener.getsockname()[1]}", clock=lambda: NOW
        )
        result = source.instant("up", at=NOW, timeout_seconds=1)
    finally:
        listener.close()
    assert (result.status, result.detail) == ("timeout", "TIMEOUT")
    assert result.sent_at == NOW
