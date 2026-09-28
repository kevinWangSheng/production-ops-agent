"""Contract tests for M1-01 upstream alignment batch B
(``docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md``, B1-B5, plus the
lead's B6 supplement: ``MAX_CONTEXT_TOKENS`` moves to the conservative
1_000_000 rather than the exact DeepSeek byte count, ``MAX_OUTPUT_TOKENS``
unchanged).

Written from the contract text, the public interface of
``opspilot.tools.otel_demo`` / ``opspilot.tools.executor`` /
``opspilot.investigation.{context,limits,loop,reports}`` and upstream
HolmesGPT source at
``production-ops-agent-m0-environment/tmp/m0-environment/holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4``
(``holmes/common/env_vars.py:137-147``, ``holmes/plugins/toolsets/utils.py:
75-125``, ``holmes/core/llm.py:159-159``, ``holmes/core/safeguards.py``,
``holmes/config.py:112``) -- not from any batch-B implementation, which does
not exist yet. No implementation code for B1-B6 was read.

Interpretive notes (no implementation exists to check against, so these are
this test author's best-grounded reading of ambiguous contract text; flagged
so the implementer can treat a mismatch as a contract question rather than a
silent assertion change):

* B1's "default 与上游一致，查不到则取 20" default trace-search ``limit`` is
  not pinned to a number here; only the *cap removal* (a request above the
  old ceiling of 20 must reach the backend) and the *byte-cap raise* are
  tested.
* B2: the model's per-call time selection is tested as tool *parameters*
  (``start``/``end`` inside ``ToolRequest.params``, exactly where every other
  model-supplied field already lives -- ``tool_request_for`` copies the
  model's JSON arguments into ``params`` verbatim and is tool-agnostic, so a
  window carried as a *separate* ``ToolRequest.window`` would need loop
  changes the contract does not mention). ``ToolRequest.window`` /
  ``scope.window`` is read as the runner's *outer* authorization frame,
  unchanged from how the executor already uses it today.
* B2's "explain the frame" refusal is asserted as: the transport response
  carries a refusal (``source_status`` set, nothing dispatched), and the
  authorized frame's ISO bounds are discoverable somewhere in the refusal
  body. The exact field name is not pinned.
* B3's fixed-template feedback message is asserted by content (contains the
  offending evidence_id or the parse-failure reason code, excludes a planted
  raw-view marker) rather than by exact wording, which does not exist yet.

Hermetic: no network, no PostgreSQL.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from opspilot.investigation.context import CONTEXT_POLICY
from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.limits import (
    M1_FROZEN_LIMITS,
    MAX_CONTEXT_TOKENS,
    MAX_OUTPUT_TOKENS,
)
from opspilot.investigation.loop import DISCIPLINE_VARIANT
from opspilot.persistence import Lease
from opspilot.tools import ReadOnlyToolExecutor, ToolRequest, TransportRequest, Window
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    SOURCE,
    TARGET_ID,
    TOOL_SCHEMAS,
    TRACES_TOOL,
    OtelDemoConfig,
    OtelDemoTransport,
    otel_demo_executor_factory,
)
from tests.m1_investigation_support import (
    assemble,
    reply,
    report_json,
    tool_call,
)

# -- shared doubles: a minimal otel_demo executor harness --------------------
#
# Built from the same public pieces ``otel_demo_executor_factory`` composes
# internally (``ToolFace``, ``DISCIPLINE_VARIANT``, ``TOOL_SCHEMAS``) rather
# than through ``otel_demo_face`` (whose *default* window is exactly what B2
# changes), so these tests do not accidentally depend on today's fixed 300 s
# window while probing tomorrow's 24 h one.

PROMETHEUS_URL = "http://127.0.0.1:19090"
JAEGER_URL = "http://127.0.0.1:16686/jaeger/ui"
SUBMIT = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
NOW = SUBMIT + timedelta(minutes=2)


class _Reply:
    def __init__(self, body: bytes, status: int = 200):
        self._buffer = io.BytesIO(body)
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._buffer.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Serves a fixed body by URL-path suffix; records every request made."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []

    @property
    def called(self) -> bool:
        return bool(self.requests)

    def urls(self) -> list[str]:
        return [_url(r) for r in self.requests]

    def open(self, request, timeout=None):
        self.requests.append(request)
        url = _url(request)
        parts = urlsplit(url)
        for suffix, value in self.routes.items():
            if parts.path.endswith(suffix):
                if isinstance(value, int):
                    raise urllib.error.HTTPError(
                        url, value, "error", {}, io.BytesIO(b"{}")
                    )
                return _Reply(value)
        raise AssertionError(f"unexpected request: {url}")


def _url(request) -> str:
    return request.full_url if isinstance(request, urllib.request.Request) else request


class FakeStore:
    def __init__(self, lease: Lease, *, deadline: datetime):
        self.lease = lease
        self.deadline = deadline

    def rebuild(self, incident_id):
        assert incident_id == self.lease.incident_id
        return {
            "run": {
                "run_id": self.lease.run_id,
                "deadline": self.deadline,
                "tool_operations_used": 0,
                "tool_seconds_used": 0.0,
            }
        }

    def charge_tool(self, *a, **kw):
        pass

    def control_state(self, incident_id):
        return {
            "incident_generation": self.lease.control_generation,
            "incident_state": "running",
            "global_suspended": False,
            "global_generation": self.lease.global_suspension_generation,
            "target_suspended": False,
            "target_generation": self.lease.target_suspension_generation,
        }


def _lease() -> Lease:
    return Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=3,
        global_suspension_generation=1,
        target_suspension_generation=2,
    )


def _evidence_context(run_id: str, window: Window) -> dict:
    return {
        "type": "opspilot-evidence-context-v4",
        "run_id": run_id,
        "time_policies": [
            {
                "id": "policy-window-1",
                "mode": "historical_window",
                "all_authorized_targets": True,
                "window": window.as_json(),
            }
        ],
    }


def _otel_executor(monkeypatch, opener: FakeOpener, *, window: Window, token=None):
    """A real ``ReadOnlyToolExecutor`` over the profile, ``scope.window ==
    window`` -- the runner's authorized frame for these tests, independent of
    ``otel_demo_face``'s own (pre-B2) default."""
    lease = _lease()
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))

    def build_opener(*handlers):
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    config = OtelDemoConfig(
        prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL, token=token
    )

    class _Clock:
        def now(self):
            return NOW

        def monotonic(self):
            return 1_000.0

    face = ToolFace(
        tool_schemas=TOOL_SCHEMAS,
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=lambda run_id: _evidence_context(run_id, window),
    )
    run_id = str(lease.run_id)
    investigation_input = face.input_for(
        run_id=run_id,
        question="Why is checkout erroring?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )
    factory = otel_demo_executor_factory(
        store, evidence=_Sink(), clock=_Clock(), config=config
    )
    executor = factory(lease, investigation_input)
    assert isinstance(executor, ReadOnlyToolExecutor)
    return executor


class _Sink:
    def register(self, record):
        return record.evidence_id


def _call(tool: str, params: dict, *, window: Window, index: int = 0):
    return ToolRequest(
        step_id="step-1",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=window.as_json(),
    )


def _transport_request(tool: str, params: dict, *, window: Window, **over):
    fields = {
        "operation_id": "step-1-t0",
        "source": SOURCE,
        "verb": "query",
        "endpoint": PROMETHEUS_URL,
        "selector": {
            "opspilot.integration.id": TARGET_ID,
            "traces_endpoint": JAEGER_URL,
        },
        "params": params,
        "window": window,
        "timeout_seconds": 30.0,
        "max_result_bytes": 1_048_576,
        "credential_ref": CREDENTIAL_REF,
        "tool": tool,
    }
    fields.update(over)
    return TransportRequest(**fields)


def _transport(opener: FakeOpener) -> OtelDemoTransport:
    return OtelDemoTransport(credentials={CREDENTIAL_REF: None}, opener=opener)


def _prom_body(rows: list[dict]) -> bytes:
    return json.dumps({"data": {"result": rows}}).encode("utf-8")


def _prom_rows(count: int, *, ts: int) -> list[dict]:
    """``ts`` must land inside whatever ``scope.window`` the test uses -- the
    executor refuses adopted rows whose sample timestamps fall outside it
    (``WINDOW_OUT_OF_SCOPE``, ``opspilot/tools/executor.py``'s post-dispatch
    source-timestamp check, unrelated to this batch)."""
    return [
        {"metric": {"service_name": f"checkout-{i:04d}"}, "values": [[ts + i, "1"]]}
        for i in range(count)
    ]


def _trace_body(spans: list[dict]) -> bytes:
    payload = {
        "data": [
            {
                "traceID": "trace0",
                "processes": {"p1": {"serviceName": "checkout"}},
                "spans": spans,
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def _small_span(i: int, *, start_us: int) -> dict:
    return {
        "traceID": f"trace{i:06x}",
        "spanID": f"span{i:06x}",
        "operationName": "op",
        "startTime": start_us + i,
        "duration": 1000 + i,
        "processID": "p1",
    }


def _heavy_span(i: int, *, start_us: int) -> dict:
    """~3.5 KB projected: pads ``operationName`` to the 150-char cap and adds
    the four detail fields ``_details`` reads, mirroring
    ``tests/test_m1_view_explicit_contract.py``'s own byte-inflation
    technique."""
    return {
        "traceID": f"trace{i:06x}",
        "spanID": f"span{i:06x}",
        "operationName": "op-" + "x" * 140,
        "startTime": start_us + i,
        "duration": 1000 + i,
        "processID": "p1",
        "tags": [
            {"key": "error", "value": True},
            {"key": "otel.status_code", "value": "ERROR"},
        ],
        "logs": [
            {
                "timestamp": start_us + i,
                "fields": [
                    {"key": "exception.stacktrace", "value": "E" * 700},
                    {"key": "exception.message", "value": "M" * 700},
                    {"key": "otel.status_description", "value": "D" * 700},
                    {"key": "error.description", "value": "F" * 700},
                ],
            }
        ],
    }


# =============================================================================
# B1 -- trace search and view byte size aligned to upstream scale
# =============================================================================

_FRAME_1H = Window(SUBMIT - timedelta(hours=1), SUBMIT)


def test_b1_traces_search_limit_above_the_old_cap_of_20_reaches_the_backend(
    monkeypatch,
):
    """The old ``MAX_TRACE_LIMIT=20`` refused any ``limit`` above 20 before a
    request went out. Contract: no more count ceiling."""
    opener = FakeOpener(routes={"/api/traces": _trace_body([])})
    executor = _otel_executor(monkeypatch, opener, window=_FRAME_1H)
    outcome = executor.execute(
        _call(TRACES_TOOL, {"service": "checkout", "limit": 50}, window=_FRAME_1H)
    )
    assert opener.called
    sent_limit = parse_qs(urlsplit(opener.urls()[0]).query).get("limit")
    assert sent_limit == ["50"], outcome.model_view


def test_b1_traces_view_no_longer_caps_sampled_spans_at_20():
    """Row-cap-free assertion: 40 *small* spans (~370 bytes each projected --
    corrected count: the original 60 was sized against a stale ~123
    bytes/row estimate that had actually only measured the *old* code's
    20-row-capped output) stay comfortably under today's 16 KiB
    ``max_view_bytes``, so a returned-row count above 20 proves the row cap
    (not the byte cap) moved."""
    start_us = int(_FRAME_1H.start.timestamp() * 1_000_000) + 1_000_000
    from opspilot.tools.otel_demo import project_traces
    from opspilot.tools.registry import canonical

    payload_data = [
        {
            "traceID": "trace0",
            "processes": {"p1": {"serviceName": "checkout"}},
            "spans": [_small_span(i, start_us=start_us) for i in range(40)],
        }
    ]
    record, _s, _e = project_traces(
        {"data": payload_data},
        service="checkout",
        limit=2,
        window=_FRAME_1H,
        source_sha256="0" * 64,
        source_bytes=100,
    )
    rows = record["data"]["sampled_spans"]
    assert len(canonical(rows).encode("utf-8")) < 16 * 1024, (
        "test invariant: stays small"
    )
    assert len(rows) > 20, "the 20-span sampling cap must be lifted"


def test_b1_traces_view_byte_cap_matches_the_upstream_tool_result_scale(
    monkeypatch,
):
    """25 000 tokens at this repo's own 4-bytes/token estimator convention
    (``opspilot.investigation.context._TOKENS_PER_BYTE``) is ~100 000 bytes
    (upstream ``holmes/common/env_vars.py:142``,
    ``TOOL_MAX_ALLOCATED_CONTEXT_WINDOW_TOKENS=25000``). 20 spans padded with
    the standard detail fields project to ~68.8 KB -- over the *old* 16 KiB
    ``max_view_bytes`` (must truncate today) and comfortably under a 90 KB
    floor (must not truncate once B1 lands). Exactly 20 spans so this is
    independent of the separate row-cap-removal contract clause above."""
    start_us = int(_FRAME_1H.start.timestamp() * 1_000_000) + 1_000_000
    spans = [_heavy_span(i, start_us=start_us) for i in range(20)]
    opener = FakeOpener(routes={"/api/traces": _trace_body(spans)})
    executor = _otel_executor(monkeypatch, opener, window=_FRAME_1H)
    outcome = executor.execute(
        _call(TRACES_TOOL, {"service": "checkout", "limit": 2}, window=_FRAME_1H)
    )
    assert outcome.status == "ok", outcome.model_view
    view = outcome.model_view
    assert view["truncated"] is False, view.get("omitted_bytes")
    assert view["omitted_rows"] == 0
    assert len(view["content"]) == 20


def test_b1_metrics_view_byte_cap_also_widened(monkeypatch):
    """1200 minimal Prometheus series rows canonicalize to ~86.4 KB -- above
    the old 24 KiB metrics ``max_view_bytes`` (must truncate today) and under
    a 90 KB floor (must not truncate once B1 lands)."""
    rows = _prom_rows(1200, ts=int(_FRAME_1H.start.timestamp()) + 60)
    opener = FakeOpener(routes={"/api/v1/query_range": _prom_body(rows)})
    executor = _otel_executor(monkeypatch, opener, window=_FRAME_1H)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": "up"}, window=_FRAME_1H))
    assert outcome.status == "ok", outcome.model_view
    view = outcome.model_view
    assert view["truncated"] is False, view.get("omitted_bytes")
    assert view["omitted_rows"] == 0
    assert len(view["content"]) == 1200


def test_sanity_traces_search_within_the_old_cap_still_works(monkeypatch):
    """Fixture sanity: a small, unremarkable request must keep working
    exactly as before B1 touches anything."""
    opener = FakeOpener(routes={"/api/traces": _trace_body([])})
    executor = _otel_executor(monkeypatch, opener, window=_FRAME_1H)
    outcome = executor.execute(
        _call(TRACES_TOOL, {"service": "checkout", "limit": 5}, window=_FRAME_1H)
    )
    assert outcome.status in ("ok", "no_data"), outcome.model_view
    assert parse_qs(urlsplit(opener.urls()[0]).query)["limit"] == ["5"]


def test_b1_review_a_too_large_trace_result_refusal_is_actionable(monkeypatch):
    """Review disposition P2 ("trace limit 过大时整条失败"): B1 removed the
    trace-search count ceiling, so a large ``limit`` can now make the
    backend's response overflow the read/result byte ceiling. Upstream's own
    oversized-result tools return an actionable hint, not a bare error code
    (contract text: state the byte ceiling and suggest a smaller ``limit`` or
    a narrower window). 300 heavy spans project to just over 1 MiB."""
    start_us = int(_FRAME_1H.start.timestamp() * 1_000_000) + 1_000_000
    spans = [_heavy_span(i, start_us=start_us) for i in range(300)]
    opener = FakeOpener(routes={"/api/traces": _trace_body(spans)})
    executor = _otel_executor(monkeypatch, opener, window=_FRAME_1H)
    outcome = executor.execute(
        _call(TRACES_TOOL, {"service": "checkout", "limit": 300}, window=_FRAME_1H)
    )
    assert outcome.status != "ok"
    assert outcome.reason == "RESULT_TOO_LARGE"
    serialized = json.dumps(outcome.model_view, default=str).lower()
    assert "byte" in serialized, "refusal must state the byte ceiling"
    assert "limit" in serialized or "window" in serialized, (
        "refusal must suggest a smaller limit or a narrower window"
    )


# =============================================================================
# B2 -- the model may pick a time window inside a runner-authorized frame
# =============================================================================

_FRAME_24H = Window(SUBMIT - timedelta(hours=24), SUBMIT)


def test_b2_tool_schemas_declare_optional_start_and_end():
    by_name = {schema["function"]["name"]: schema for schema in TOOL_SCHEMAS}
    for name in (METRICS_TOOL, TRACES_TOOL):
        parameters = by_name[name]["function"]["parameters"]
        assert {"start", "end"} <= set(parameters["properties"]), name
        assert "start" not in parameters.get("required", ())
        assert "end" not in parameters.get("required", ())


# These four tests drive ``OtelDemoTransport`` directly (as
# ``test_m1_otel_demo_contract.py`` already does for its own transport-level
# tests), not through ``ReadOnlyToolExecutor.execute``. That sidesteps a real
# but B2-orthogonal fact this test file discovered by *running* against
# today's code: ``ReadOnlyToolExecutor._authorize`` refuses any top-level
# ``ToolRequest.window`` wider than the registration's own
# ``max_window_seconds`` (today 3600 s for both tools) with
# ``WINDOW_TOO_LARGE``, before any tool-specific code ever sees it -- so once
# the runner's outer frame grows to 24 h, *something* must give either that
# ceiling (if the top-level window stays the full frame and per-call
# selection happens inside ``_metrics``/``_traces`` against ``request.params``
# -- what these tests assume) or the loop (if it narrows ``ToolRequest.window``
# itself before calling ``execute()``, mirroring how it already derives
# ``target_ref``). The contract text calls ``start``/``end`` tool
# *parameters* and ``tool_request_for`` (``opspilot/investigation/loop.py``)
# already copies a model tool call's JSON arguments into ``ToolRequest.params``
# generically for every tool, with no per-tool special-casing -- carrying them
# instead through a separately-computed top-level ``window`` would need new,
# tool-schema-aware logic in that generic function. This test file therefore
# assumes the params-based reading and tests it at the layer that would carry
# it either way. If the implementation instead narrows the top-level window in
# the loop, these four tests need rework at the loop level -- a contract
# question for the implementer, not a silent assertion change.


def test_b2_metrics_request_without_start_end_defaults_to_one_hour_lookback_ending_at_submission():
    """Omitted start/end: upstream semantic (``holmes/plugins/toolsets/utils
    .py:111-113``) is "default one hour before end", end defaulting to now
    (here, the Run's own submission instant, ``SUBMIT`` = ``_FRAME_24H.end``)
    -- not the full 24 h authorized frame."""
    opener = FakeOpener(routes={"/api/v1/query_range": _prom_body([])})
    response = _transport(opener).fetch(
        _transport_request(METRICS_TOOL, {"expr": "up"}, window=_FRAME_24H)
    )
    assert response.source_status is None, response.body
    query = parse_qs(urlsplit(opener.urls()[0]).query)
    sent_start = datetime.fromtimestamp(float(query["start"][0]), timezone.utc)
    sent_end = datetime.fromtimestamp(float(query["end"][0]), timezone.utc)
    assert sent_end == SUBMIT
    assert sent_start == SUBMIT - timedelta(hours=1), (
        "default window must be the last 1 h, not the full 24 h frame"
    )


def test_b2_metrics_request_with_explicit_start_end_inside_the_frame_is_used():
    sub_window = Window(SUBMIT - timedelta(hours=3), SUBMIT - timedelta(hours=2))
    opener = FakeOpener(routes={"/api/v1/query_range": _prom_body([])})
    response = _transport(opener).fetch(
        _transport_request(
            METRICS_TOOL,
            {
                "expr": "up",
                "start": sub_window.start.isoformat(),
                "end": sub_window.end.isoformat(),
            },
            window=_FRAME_24H,
        )
    )
    assert response.source_status is None, response.body
    query = parse_qs(urlsplit(opener.urls()[0]).query)
    sent_start = datetime.fromtimestamp(float(query["start"][0]), timezone.utc)
    sent_end = datetime.fromtimestamp(float(query["end"][0]), timezone.utc)
    assert sent_start == sub_window.start
    assert sent_end == sub_window.end


def test_b2_metrics_request_outside_the_24h_frame_is_refused_and_states_the_frame():
    """A model-chosen window entirely before the runner's 24 h authorized
    frame must be refused, not silently clamped or silently answered from
    the full frame, and the refusal must let the model discover the frame it
    may retry within (PRODUCT-CONSTRAINTS "Query scope ... constrained")."""
    out_of_frame = Window(SUBMIT - timedelta(hours=30), SUBMIT - timedelta(hours=28))
    opener = FakeOpener(routes={"/api/v1/query_range": _prom_body([])})
    response = _transport(opener).fetch(
        _transport_request(
            METRICS_TOOL,
            {
                "expr": "up",
                "start": out_of_frame.start.isoformat(),
                "end": out_of_frame.end.isoformat(),
            },
            window=_FRAME_24H,
        )
    )
    assert response.source_status is not None, "an out-of-frame window must be refused"
    assert not opener.called, "must be refused before any backend request"
    serialized = response.body.decode("utf-8")
    assert _FRAME_24H.start.isoformat() in serialized, (
        "refusal must state the authorized frame's start so the model can "
        "retry inside it"
    )
    assert _FRAME_24H.end.isoformat() in serialized


def test_sanity_a_full_frame_request_with_no_time_params_dispatches_today():
    """Fixture sanity for the generic transport plumbing this batch builds
    on: a request whose top-level window is the runner's own frame, with no
    time params at all, was never refused before this batch and must not
    become one by accident."""
    opener = FakeOpener(routes={"/api/v1/query_range": _prom_body([])})
    response = _transport(opener).fetch(
        _transport_request(METRICS_TOOL, {"expr": "up"}, window=_FRAME_1H)
    )
    assert response.source_status is None, response.body
    assert opener.called


def test_b2_p1_default_query_window_view_reports_the_query_window_not_the_frame(
    monkeypatch,
):
    """Review disposition P1: the view's ``window`` and ``lookback_start_at``
    must reflect the *actual query window* the model was answered over, not
    the runner's 24 h authorized frame -- even though the frame is what
    ``scope.window`` (and, per the B2 design note, the unnarrowed top-level
    ``ToolRequest.window``) still carries. No start/end given: the query
    window is the default 1 h ending at submission."""
    opener = FakeOpener(routes={"/api/v1/query_range": _prom_body([])})
    executor = _otel_executor(monkeypatch, opener, window=_FRAME_24H)
    outcome = executor.execute(
        _call(METRICS_TOOL, {"expr": "rate(x[5m])"}, window=_FRAME_24H)
    )
    view = outcome.model_view
    query_window = Window(SUBMIT - timedelta(hours=1), SUBMIT)
    assert view["window"] == query_window.as_json(), (
        "must be the 1 h query window, not the 24 h frame"
    )
    assert view["lookback_start_at"] == query_window.start.isoformat(), (
        "must be the query window's own start, not the frame's"
    )


def test_b2_p1_explicit_sub_window_view_records_that_sub_window(monkeypatch):
    """Review disposition P1, second scenario: an explicit start/end (here,
    the 10 minutes ending 20 minutes before submission) must be exactly what
    the view's ``window``/``lookback_start_at`` report."""
    sub_window = Window(SUBMIT - timedelta(minutes=30), SUBMIT - timedelta(minutes=20))
    opener = FakeOpener(routes={"/api/v1/query_range": _prom_body([])})
    executor = _otel_executor(monkeypatch, opener, window=_FRAME_24H)
    outcome = executor.execute(
        _call(
            METRICS_TOOL,
            {
                "expr": "rate(x[5m])",
                "start": sub_window.start.isoformat(),
                "end": sub_window.end.isoformat(),
            },
            window=_FRAME_24H,
        )
    )
    view = outcome.model_view
    assert view["window"] == sub_window.as_json()
    assert view["lookback_start_at"] == sub_window.start.isoformat()


def test_b2_p2_metrics_description_no_longer_hardcodes_300s_for_whole_window_totals():
    """Review disposition P2: the whole-window-aggregate examples
    (``increase(x[300s])`` etc.) hardcoded a stale fixed window length; the
    query window is now model-chosen and may be any length, so the
    description must no longer state 300 specifically."""
    by_name = {schema["function"]["name"]: schema for schema in TOOL_SCHEMAS}
    description = by_name[METRICS_TOOL]["function"]["description"]
    assert "300s" not in description
    assert "A whole-window total, maximum or minimum" in description, (
        "the underlying guidance must still be present, just not tied to "
        "a literal 300s example"
    )


# =============================================================================
# B3 -- report validation failures get one templated feedback + retry,
#        merged with loop-limits L1a into a single one-shot mechanism
# =============================================================================

_FINAL_REPORT_MARKER = "Collection is now CLOSED"


def _report_citing_first_tool_result(call):
    """Final-round reply citing the *first* tool result's evidence_id --
    unlike ``report_from_transcript`` (which cites the last), this stays
    correct when a later duplicate call's result is a B5 dedup pointer."""
    for message in call.messages:
        if message.get("role") == "tool":
            evidence_id = json.loads(message["content"])["evidence_id"]
            return reply(content=report_json(evidence_id=evidence_id))
    return reply(content=_report_json_with_no_claims())


def _report_json_with_no_claims() -> str:
    """A schema-valid report citing no evidence at all (mirrors
    ``tests/test_m1_loop_limits_contract.py``'s own L1a fixture): needed
    wherever a round must end validly before any tool has ever run, so no
    evidence_id exists yet for a claim to cite."""
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "incomplete",
        "conclusion": "inconclusive",
        "summary": "Ending without querying any tool; no claims are made.",
        "claims": [],
        "gaps": ["No evidence was gathered."],
        "next_steps": [],
    }
    return json.dumps(payload, ensure_ascii=False)


def test_b3_l1a_forced_final_round_carries_diagnostic_feedback_naming_the_failure():
    """A non-final, no-tool-call reply that fails to parse still forces
    exactly one final-report retry (loop-limits L1a, unchanged); B3 adds the
    requirement that the forced round also carries a fixed-template message
    naming *what* failed -- here the parser's own reason code, since a
    Markdown-fenced reply never became a claim to name."""
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(content='```json\n{"not": "bare json"}\n```', finish="stop"),
            reply(content=_report_json_with_no_claims(), finish="stop"),
        ],
        model_requests=100,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert outcome.model_requests_used == 2, (
        "the diagnostic feedback must ride inside the existing forced-final "
        "round, not add a request of its own"
    )
    retry_texts = [str(message.get("content")) for message in model.calls[1].messages]
    assert any(_FINAL_REPORT_MARKER in text for text in retry_texts)
    assert any("REPORT_INVALID" in text for text in retry_texts), (
        "the failure's reason code must reach the model, not just the "
        "unconditional final-report instruction"
    )


def test_b3_citation_failure_names_the_evidence_id_omits_raw_view_and_retry_succeeds():
    """Round 1 adopts one real view (its raw content carries a marker that
    must never leak). Round 2, with no further tool calls, cites an
    evidence_id nothing delivered -- a citation failure, not a parse one.
    Under B3 this earns one retry whose feedback names the bad id and
    excludes the raw marker; round 3 cites the real id and completes."""
    raw_marker = "raw-view-marker-9f3a21"
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(tool_calls=[tool_call(call_id="c1")], finish="tool_calls"),
            reply(content=report_json(evidence_id="not-a-real-id"), finish="stop"),
            _report_citing_first_tool_result,
        ],
        model_requests=3,
    )
    from opspilot.tools import TransportResponse
    from tests.m1_tool_support import WINDOW_START, body

    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": raw_marker}]),
        data_as_of=WINDOW_START,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert outcome.model_requests_used == 3
    retry_texts = [str(message.get("content")) for message in model.calls[2].messages]
    # "not-a-real-id" already appears once, verbatim, inside round 2's own
    # (invalid) assistant turn, which L1a already keeps in history today --
    # that alone is not evidence of a *new* diagnostic message. A second,
    # independent mention -- together with the checker's own reason code,
    # which the model's JSON report text would never itself contain -- is.
    id_mentions = sum(text.count("not-a-real-id") for text in retry_texts)
    assert id_mentions >= 2, (
        "the retry feedback must independently name the unsupported "
        "evidence_id, not merely carry the model's own prior turn forward"
    )
    assert any("REPORT_INVALID" in text for text in retry_texts), (
        "the failure's reason code must reach the model"
    )
    # The marker legitimately appears once already, inside round 1's own
    # adopted tool view (still part of the ongoing transcript) -- what must
    # not happen is the *new* diagnostic feedback message adding a second,
    # independent leak of it.
    before_texts = [str(message.get("content")) for message in model.calls[1].messages]
    marker_before = sum(text.count(raw_marker) for text in before_texts)
    marker_after = sum(text.count(raw_marker) for text in retry_texts)
    assert marker_after <= marker_before, (
        "the retry feedback must not include raw view content"
    )


def test_b3_merged_cap_a_second_validation_failure_ends_report_invalid_with_no_third_request():
    """The single retry is shared with L1a: once the forced-final round
    (round 2, spent by round 1's failure) also fails, there is no second
    retry -- the Run ends REPORT_INVALID after exactly two requests, exactly
    as it does today without B3."""
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(content=report_json(evidence_id="not-a-real-id-1"), finish="stop"),
            reply(content=report_json(evidence_id="not-a-real-id-2"), finish="stop"),
        ],
        model_requests=100,
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert outcome.model_requests_used == 2
    assert len(model.calls) == 2


# =============================================================================
# B4 -- compaction threshold aligned to upstream (0.8 -> 0.95)
# =============================================================================


def test_b4_compaction_threshold_is_0_95():
    """Upstream: ``get_context_window_compaction_threshold_pct()`` defaults
    ``CONTEXT_WINDOW_COMPACTION_THRESHOLD_PCT`` to ``"95"``
    (``holmes/core/llm.py:159``)."""
    assert CONTEXT_POLICY.compaction_pct == 0.95


# =============================================================================
# B5 -- an exact-duplicate tool call within a Run is not re-executed
# =============================================================================


def test_b5_second_identical_tool_call_in_the_same_round_is_not_redispatched():
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(
                tool_calls=[tool_call(call_id="c1"), tool_call(call_id="c2")],
                finish="tool_calls",
            ),
            _report_citing_first_tool_result,
        ],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert len(transport.requests) == 1, (
        "the exact-duplicate call must not reach the transport a second time"
    )
    first_msg, second_msg = (
        message for message in model.calls[1].messages if message.get("role") == "tool"
    )
    first_evidence_id = json.loads(first_msg["content"])["evidence_id"]
    assert first_evidence_id in second_msg["content"], (
        "the duplicate's result must point at the first call's evidence_id"
    )


def test_b5_duplicate_tool_call_in_a_later_round_is_not_redispatched():
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(tool_calls=[tool_call(call_id="c1")], finish="tool_calls"),
            reply(tool_calls=[tool_call(call_id="c2")], finish="tool_calls"),
            _report_citing_first_tool_result,
        ],
        model_requests=3,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert len(transport.requests) == 1, (
        "a later round's exact-duplicate call must not be re-executed either"
    )


def test_sanity_two_different_tool_calls_in_one_round_both_dispatch():
    """Fixture sanity: B5 must not over-trigger on genuinely different
    arguments."""
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(
                tool_calls=[
                    tool_call(
                        call_id="c1", arguments='{"expr":"rate(http_errors[5m])"}'
                    ),
                    tool_call(
                        call_id="c2", arguments='{"expr":"rate(http_errors[1m])"}'
                    ),
                ],
                finish="tool_calls",
            ),
            _report_citing_first_tool_result,
        ],
        model_requests=2,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert len(transport.requests) == 2


# =============================================================================
# B6 (lead supplement) -- context ceiling is the conservative 1_000_000
# =============================================================================


def test_b6_max_context_tokens_is_the_conservative_1_000_000():
    """DeepSeek's own pricing page states only the abbreviated "1M"; the lead
    2026-09-28 decided the conservative reading (1_000_000) over the base-1024
    reading (1_048_576, L3a's current value) absent an exact digit count.
    Output stays untouched at 65_536 (L3)."""
    assert MAX_CONTEXT_TOKENS == 1_000_000
    assert M1_FROZEN_LIMITS.context_tokens == 1_000_000
    assert MAX_OUTPUT_TOKENS == 65_536
    assert M1_FROZEN_LIMITS.output_tokens == 65_536
