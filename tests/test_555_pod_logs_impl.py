"""Implementer's unit tests for ``fetch_pod_logs`` (tool expansion A3, #555).

The independent contract tests live in
``tests/test_tool_expansion_a3_contract.py``; these pin the implementation's
own choices (stream order, notes, view counters) through the same executor
seam as the A2 contract tests: ``select_profile`` with an injected
``urllib.request.build_opener`` double. Synthetic credentials only.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.tools import TransportRequest, TransportTimeout, otel_demo_ext
from opspilot.tools import kubernetes_core as k8s
from opspilot.tools import kubernetes_logs as logs
from tests.test_tool_expansion_a2_contract import (
    AUTHORIZED_WINDOW,
    _call,
    _executor,
    _reply,
    _synthetic_k8s_ca,  # noqa: F401 - autouse fixture: the CA file
    _view,
)

TOOL = "fetch_pod_logs"
NS = "otel-demo"
POD = "checkout-7d9f8c6b5-abcde"
END = AUTHORIZED_WINDOW.end.astimezone(timezone.utc)


def _stamp(seconds_before_end: float) -> str:
    moment = END - timedelta(seconds=seconds_before_end)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"


def _log(*lines: tuple[float, str]) -> bytes:
    return "".join(f"{_stamp(s)} {text}\n" for s, text in lines).encode()


def _pod(*containers: str, init: tuple[str, ...] = ()) -> bytes:
    spec: dict[str, object] = {"containers": [{"name": c} for c in containers]}
    if init:
        spec["initContainers"] = [{"name": c} for c in init]
    return json.dumps(
        {"kind": "Pod", "metadata": {"name": POD, "namespace": NS}, "spec": spec}
    ).encode()


def _status(code: int, message: str) -> bytes:
    return json.dumps(
        {"kind": "Status", "status": "Failure", "message": message, "code": code}
    ).encode()


class LogOpener:
    """Answers by (path, container, previous); records every request."""

    def __init__(self, pod: object, streams: dict[tuple[str, bool], object]):
        self.pod = pod
        self.streams = streams
        self.requests: list[urllib.request.Request] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        url = request.full_url
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.path.endswith("/log"):
            key = (query["container"][0], query.get("previous") == ["true"])
            value = self.streams.get(key, b"")
        else:
            value = self.pod
        if isinstance(value, tuple):
            code, body = value
            raise urllib.error.HTTPError(url, code, "synthetic", {}, io.BytesIO(body))
        if isinstance(value, int):
            raise urllib.error.HTTPError(url, value, "synthetic", {}, io.BytesIO(b"{}"))
        return _reply(value)


def _run(monkeypatch, opener, **params):
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    return executor.execute(_call(TOOL, {"pod_name": POD, "namespace": NS, **params}))


def test_every_container_current_and_previous_merged_in_time_order(monkeypatch):
    opener = LogOpener(
        _pod("app", "sidecar"),
        {
            ("app", True): _log((300, "panic: boom")),
            ("sidecar", True): (
                400,
                _status(400, "previous terminated container not found"),
            ),
            ("app", False): _log((200, "started"), (100, "serving")),
            ("sidecar", False): _log((250, "proxy up")),
        },
    )
    outcome = _run(monkeypatch, opener)
    assert outcome.status == "ok", outcome
    view = _view(outcome)
    texts = [
        (row["container"], ("previous" if row["previous"] else "current"), row["text"])
        for row in view["content"]
    ]
    assert texts == [
        ("app", "previous", "panic: boom"),
        ("sidecar", "current", "proxy up"),
        ("app", "current", "started"),
        ("app", "current", "serving"),
    ]
    assert len(opener.requests) == 1 + 2 * 2
    assert {r.get_method() for r in opener.requests} == {"GET"}
    log_queries = [parse_qs(urlsplit(r.full_url).query) for r in opener.requests[1:]]
    assert all(q["timestamps"] == ["true"] for q in log_queries)
    assert all("sinceTime" in q and "limitBytes" in q for q in log_queries)
    # previous streams are read first
    assert [q.get("previous") == ["true"] for q in log_queries] == [
        True,
        True,
        False,
        False,
    ]
    assert any("sidecar has no previous instance" in n for n in view["notes"])
    assert view["backend_lines_returned"] == 4 and view["lines_omitted"] == 0
    assert view["incomplete"] is False
    assert view["source_start_at"] < view["source_end_at"]


def test_init_containers_are_read_too(monkeypatch):
    opener = LogOpener(_pod("app", init=("migrate",)), {})
    outcome = _run(monkeypatch, opener)
    assert outcome.status == "no_data"
    assert _view(outcome)["containers"] == ["migrate", "app"]
    assert len(opener.requests) == 1 + 2 * 2


def test_missing_pod_is_invalid_params_pod_not_found(monkeypatch):
    opener = LogOpener(404, {})
    outcome = _run(monkeypatch, opener)
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert len(opener.requests) == 1
    text = json.dumps(outcome.model_view)
    assert "POD_NOT_FOUND" in text and POD in text and NS in text


@pytest.mark.parametrize(
    ("where", "code", "reason"),
    [
        ("pod", 401, "SOURCE_FORBIDDEN"),
        ("pod", 403, "SOURCE_FORBIDDEN"),
        ("stream", 403, "SOURCE_FORBIDDEN"),
        ("stream", 429, "SOURCE_UNAVAILABLE"),
        ("stream", 503, "SOURCE_UNAVAILABLE"),
        ("pod", 500, "SOURCE_UNAVAILABLE"),
    ],
)
def test_denials_and_unavailability(monkeypatch, where, code, reason):
    if where == "pod":
        opener = LogOpener(code, {})
    else:
        opener = LogOpener(_pod("app"), {("app", True): code})
    outcome = _run(monkeypatch, opener)
    assert outcome.reason == reason


def test_limit_keeps_the_newest_and_marks_incomplete(monkeypatch):
    lines = [(250 - i, f"line {i}") for i in range(150)]
    opener = LogOpener(_pod("app"), {("app", False): _log(*lines)})
    outcome = _run(monkeypatch, opener)
    view = _view(outcome)
    assert view["lines_shown"] == 100 and view["lines_omitted"] == 50
    assert view["content"][0]["text"] == "line 50"
    assert view["content"][-1]["text"] == "line 149"
    assert view["incomplete"] is True and "limit" in view["incomplete_reason"]


@pytest.mark.parametrize("limit", [0, 1001, -1])
def test_limit_range_is_refused_before_sending(monkeypatch, limit):
    opener = LogOpener(_pod("app"), {})
    outcome = _run(monkeypatch, opener, limit=limit)
    assert outcome.reason == "INVALID_PARAMS" and opener.requests == []


def test_read_limit_keeps_what_was_read_and_states_where(monkeypatch):
    big = _log(*[(250 - i * 0.01, "x" * 1000) for i in range(5000)])
    assert len(big) > k8s.MAX_SOURCE_READ_BYTES
    opener = LogOpener(
        _pod("app", "sidecar"),
        {("app", True): big, ("sidecar", False): _log((10, "y"))},
    )
    outcome = _run(monkeypatch, opener)
    view = _view(outcome)
    assert view["incomplete"] is True
    assert view["read_limit_reached"] is True
    assert view["bytes_read"] == k8s.MAX_SOURCE_READ_BYTES
    statuses = {(s["container"], s["instance"]): s["status"] for s in view["streams"]}
    assert statuses[("app", "previous")] == "read"
    assert statuses[("sidecar", "current")] == "not_read"
    assert len(opener.requests) == 2  # nothing left for the other streams
    assert any("move start_time later" in n for n in view["notes"])
    assert view["lines_shown"] == 100


def test_window_untimed_and_long_lines(monkeypatch):
    body = (
        _log((30, "in window"), (-30, "after end"), (2 * 3600, "before start"))
        + b"no timestamp here\n"
        + _log((20, "L" * 2500))
    )
    opener = LogOpener(_pod("app"), {("app", False): body})
    outcome = _run(monkeypatch, opener)
    view = _view(outcome)
    assert [r["text"][:9] for r in view["content"]] == ["in window", "LLLLLLLLL"]
    assert view["lines_after_window"] == 1 and view["lines_before_window"] == 1
    assert view["lines_without_timestamp"] == 1
    long = view["content"][-1]
    assert long["text_truncated"] is True and len(long["text"]) == 2000
    assert view["content"][0]["text_truncated"] is False


def test_filters_regex_case_insensitive_and_substring_fallback(monkeypatch):
    body = _log((50, "ERROR db down"), (40, "error health probe"), (30, "info ok"))
    opener = LogOpener(_pod("app"), {("app", False): body})
    outcome = _run(monkeypatch, opener, filter="err(or)?", exclude_filter="HEALTH")
    view = _view(outcome)
    assert [r["text"] for r in view["content"]] == ["ERROR db down"]
    assert view["lines_removed_by_filter"] == 1
    assert view["lines_removed_by_exclude_filter"] == 1
    assert view["filter_mode"] == {"filter": "regex", "exclude_filter": "regex"}

    opener = LogOpener(_pod("app"), {("app", False): _log((5, "value (x"))})
    outcome = _run(monkeypatch, opener, filter="(X")
    view = _view(outcome)
    assert [r["text"] for r in view["content"]] == ["value (x"]
    assert view["filter_mode"]["filter"] == "substring"
    assert any("substring" in n for n in view["notes"])


def test_too_long_pattern_is_refused_before_sending(monkeypatch):
    opener = LogOpener(_pod("app"), {})
    outcome = _run(monkeypatch, opener, filter="a" * 2001)
    assert outcome.reason == "INVALID_PARAMS" and opener.requests == []


def test_pathological_regex_is_stopped_at_the_deadline():
    opener = LogOpener(_pod("app"), {("app", False): _log((5, "a" * 40 + "!"))})
    transport = logs.PodLogsTransport(
        credentials={k8s.CREDENTIAL_REF: "k8s-aaaaaaaa357"},
        python_path=k8s.require_python(),
        opener=opener,  # type: ignore[arg-type]
    )
    request = TransportRequest(
        operation_id="op",
        source="otel-demo",
        verb="logs",
        endpoint="http://127.0.0.1:1",
        selector={k8s.ENDPOINT_SELECTOR: "https://127.0.0.1:16443"},
        params={"pod_name": POD, "namespace": NS, "filter": "(a+)+$"},
        window=AUTHORIZED_WINDOW,
        timeout_seconds=1.5,
        max_result_bytes=logs.MAX_RESULT_BYTES,
        credential_ref="otel-demo-ro",
        tool=TOOL,
    )
    started = time.monotonic()
    with pytest.raises(TransportTimeout) as raised:
        transport.fetch(request)
    assert raised.value.source_contact == "confirmed"
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "params",
    [
        {"pod_name": "../secrets", "namespace": NS},
        {"pod_name": POD, "namespace": "Bad_NS"},
        {"pod_name": POD, "namespace": NS, "start_time": "-3600", "end_time": "x"},
        {"pod_name": POD, "namespace": NS, "start_time": "2026-09-14T00:00:00Z"},
    ],
)
def test_bad_names_and_windows_send_nothing(monkeypatch, params):
    opener = LogOpener(_pod("app"), {})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TOOL, params))
    assert outcome.reason == "INVALID_PARAMS" and opener.requests == []


def test_window_outside_the_frame_is_refused(monkeypatch):
    opener = LogOpener(_pod("app"), {})
    start = (AUTHORIZED_WINDOW.start - timedelta(hours=1)).isoformat()
    end = AUTHORIZED_WINDOW.end.isoformat()
    outcome = _run(monkeypatch, opener, start_time=start, end_time=end)
    assert outcome.reason == "INVALID_PARAMS" and opener.requests == []


def test_registration_and_profile_listing():
    (registration,) = logs.pod_logs_registrations(max_window_seconds=86400)
    assert registration.verb == "logs"
    assert registration.source == "otel-demo"
    assert registration.may_contain_secrets is True
    assert TOOL in otel_demo_ext.TOOL_NAMES
    sources = otel_demo_ext._data_sources()
    assert TOOL in sources["available"]["kubernetes"]
    assert "pod_logs" not in sources["not_integrated"]


def test_regex_child_is_a_fixed_package_file():
    assert k8s.REGEX_CHILD.is_file()
    assert k8s.REGEX_CHILD.parent == Path(k8s.__file__).parent
    decided = k8s.filter_lines(
        ["Boom", "ok"],
        include="boom",
        exclude=None,
        ignore_case=True,
        python_path=k8s.require_python(),
        deadline=time.monotonic() + 10,
    )
    assert decided is not None and decided.keep == (True, False)
