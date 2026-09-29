"""Contract tests for M1-01 A (v3): the whole model view is bounded, in tokens,
by ``max_view_tokens`` for every tool, and an over-limit view is refused, never
truncated (``docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md``, section
"A 修复合同").

Written from the contract text and its public interface
(``ReadOnlyToolExecutor(..., token_counter=)``, ``ToolOutcome``,
``ToolRegistration.max_view_tokens``, ``MAX_VIEW_TOKENS``), not from any
implementation. The measured quantity is ``token_counter(canonical(view))`` of
the complete adopted view, all rows included.

Everything here injects a deterministic counter, so nothing depends on the real
tokenizer; real-tokenizer behaviour (golden values, real ``metrics_range_query``
/ ``traces_search`` paths, fail-closed loading) is in
``tests/test_m1_view_tokenizer_integration.py``. Two counters are used so no
test passes by accident of one unit: one token per UTF-8 byte, and one token
per started group of four bytes.

Interface assumptions the contract pins (tell the lead if any is wrong):
``ReadOnlyToolExecutor`` and ``otel_demo_executor_factory`` take the keyword
``token_counter``; ``registration`` from ``tests/m1_tool_support.py`` accepts
``max_view_tokens`` (the mechanical rename of ``max_view_bytes``).
"""

from __future__ import annotations

import dataclasses
import json
import urllib.request
from datetime import timedelta
from hashlib import sha256

import pytest

from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import DISCIPLINE_VARIANT
from opspilot.tools import (
    PROJECTION_REVISION,
    ControlSnapshot,
    ReadOnlyToolExecutor,
    TargetRegistry,
    ToolContractError,
    ToolRegistry,
    TransportResponse,
    Window,
)
from opspilot.tools.otel_demo import (
    MAX_VIEW_TOKENS,
    METRICS_TOOL,
    TARGET_ID,
    TOOL_SCHEMAS,
    TRACES_TOOL,
    OtelDemoConfig,
    otel_demo_executor_factory,
)
from opspilot.tools.registry import canonical, canonical_hash
from tests.m1_tool_support import (
    TRANSPORT_ONLY_MARKER,
    FakeClock,
    FakeTransport,
    FixedControl,
    RecordingLedger,
    RecordingSink,
    body,
    registration,
    request,
    scope,
    target,
)
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_TRACES,
    GOOD_EXPR,
    JAEGER_URL,
    NOW,
    PROMETHEUS_URL,
    WINDOW,
    FakeOpener,
    FakeStore,
    _call,
    _input,
    _lease,
)


def bytes_counter(text: str) -> int:
    return len(text.encode("utf-8"))


def quarter_counter(text: str) -> int:
    return -(-len(text.encode("utf-8")) // 4)


COUNTERS = [
    pytest.param(bytes_counter, id="one-token-per-byte"),
    pytest.param(quarter_counter, id="one-token-per-four-bytes"),
]

ROWS = [{"series": index, "value": index * 2} for index in range(14)]

# The exact keys of an over-limit refusal view (the task record's "拒绝路径的
# 逐项澄清", as revised after the real Runs: ``query`` echoed, ``window`` is the
# queried window): the ordinary refusal view plus ``query`` and the three limit
# details. No ``adopted``, no ``evidence_id``, and the row fields are absent,
# not zero.
REFUSAL_KEYS = {
    "operation_id",
    "trust",
    "status",
    "citable_as_fact",
    "reason",
    "source_contact",
    "tool",
    "target_id",
    "window",
    "requested_at",
    "content",
    "query",
    "max_view_tokens",
    "view_tokens",
    "message",
}

# The fields an over-limit refusal must not carry: it has no rows to describe.
ROW_FIELDS = ("returned_count", "truncated", "omitted_rows", "omitted_bytes")


def _row_bytes(row) -> int:
    return len(canonical(row).encode("utf-8"))


def _build(cap, counter, *, reg=None, control=None):
    """A generic executor whose only view limit is ``cap`` tokens."""

    clock = FakeClock()
    reg = reg or dataclasses.replace(registration(), max_view_tokens=cap)
    tools = ToolRegistry([reg])
    targets = TargetRegistry([target()])
    transport = FakeTransport(clock=clock)
    sink = RecordingSink()
    ledger = RecordingLedger()
    executor = ReadOnlyToolExecutor(
        scope=scope(targets, tool_registry=tools),
        tools=tools,
        targets=targets,
        transport=transport,
        evidence=sink,
        control=control if control is not None else FixedControl(),
        clock=clock,
        ledger=ledger,
        token_counter=counter,
    )
    return executor, transport, sink, ledger


def _run(rows, cap, counter, **kwargs):
    payload = body(rows)
    executor, transport, sink, ledger = _build(cap, counter, **kwargs)
    transport.response = TransportResponse(body=payload)
    outcome = executor.execute(request())
    return outcome, payload, sink, ledger, executor


def _tokens(view, counter) -> int:
    return counter(canonical(view))


def _stable(view):
    """The view minus what legitimately differs between two executions: the
    dispatch-specific evidence id and the registry revision, which hashes the
    registered limit itself."""

    return {
        k: v
        for k, v in view.items()
        if k not in ("evidence_id", "tool_registry_revision")
    }


def _full_tokens(counter) -> int:
    """Token count of the complete, untruncated view of ``ROWS``."""

    outcome, *_ = _run(ROWS, 1_000_000, counter)
    assert outcome.status == "ok"
    return _tokens(outcome.model_view, counter)


def _assert_delivered_whole(outcome, payload, sink, rows):
    """Contract 1 for one adopted outcome over ``rows``."""

    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("ok", None)
    assert outcome.adopted
    assert view["content"] == rows
    assert view["result_count"] == view["returned_count"] == len(rows)
    assert view["truncated"] is False
    assert view["omitted_rows"] == 0 and view["omitted_bytes"] == 0
    assert "view_tokens" not in view and "max_view_tokens" not in view
    evidence = outcome.evidence
    assert evidence.raw == payload
    assert evidence.raw_sha256 == sha256(payload).hexdigest()
    assert evidence.view_sha256 == canonical_hash(view)
    assert evidence.view == view
    assert evidence.truncated is False
    assert evidence.omitted_rows == 0 and evidence.omitted_bytes == 0
    assert evidence.result_count == len(rows)
    assert [r.evidence_id for r in sink.records] == [evidence.evidence_id]


def _assert_refused_as_too_large(outcome, sink, *, cap, tokens, marker=None):
    """Contract 2: the whole shape of an over-limit refusal."""

    view = outcome.model_view
    assert set(view) == REFUSAL_KEYS
    assert outcome.status == "error"
    assert outcome.reason == "RESULT_TOO_LARGE"
    assert outcome.source_contact == "confirmed"
    assert not outcome.adopted
    assert outcome.evidence is None
    assert sink.records == []
    assert view["trust"] == "gateway"
    assert view["status"] == "error" and view["reason"] == "RESULT_TOO_LARGE"
    assert view["citable_as_fact"] is False
    assert view["content"] is None
    assert view["max_view_tokens"] == cap
    assert view["view_tokens"] == tokens
    assert "evidence_id" not in view
    for field in ROW_FIELDS:
        assert field not in view, field
    message = view["message"]
    assert message.startswith(
        f"The tool call result is too large to return: {tokens}/{cap} tokens."
    )
    lowered = message.lower()
    assert "narrow" in lowered or "window" in lowered
    assert "limit" in lowered
    assert "://" not in message and TRANSPORT_ONLY_MARKER not in message
    if marker is not None:
        assert marker not in json.dumps(view)


# -- 1, 2. the whole view against the cap, to the token -------------------------


@pytest.mark.parametrize("counter", COUNTERS)
def test_a_view_is_delivered_whole_at_or_under_the_cap_and_refused_one_token_over(
    counter,
):
    """Contract 1 and 2 at every cap around the view's size ``T``: at ``T`` the
    view is delivered unchanged (equal counts as fitting), at ``T - 1`` it is
    refused and reports ``T``."""

    full = _full_tokens(counter)
    assert full > 40
    for cap in range(full - 30, full + 4):
        outcome, payload, sink, *_ = _run(ROWS, cap, counter)
        if cap >= full:
            _assert_delivered_whole(outcome, payload, sink, ROWS)
            assert _tokens(outcome.model_view, counter) <= cap
        else:
            _assert_refused_as_too_large(outcome, sink, cap=cap, tokens=full)


@pytest.mark.parametrize("counter", COUNTERS)
@pytest.mark.parametrize("cap", [1, 2, 50])
def test_a_cap_far_below_the_fixed_fields_refuses_rather_than_returns_no_rows(
    counter, cap
):
    """No "empty but ok" view exists any more: nothing is partly delivered."""

    full = _full_tokens(counter)
    outcome, _, sink, *_ = _run(ROWS, cap, counter)
    _assert_refused_as_too_large(outcome, sink, cap=cap, tokens=full)


def test_a_refusal_leaks_no_source_content():
    rows = [{"series": "SECRET-ROW-MARKER-" + "z" * 300}]
    full = bytes_counter(canonical(_run(rows, 1_000_000, bytes_counter)[0].model_view))
    outcome, _, sink, *_ = _run(rows, full - 1, bytes_counter)
    _assert_refused_as_too_large(
        outcome, sink, cap=full - 1, tokens=full, marker="SECRET-ROW-MARKER"
    )


def test_the_unit_is_the_counters_not_bytes():
    """A cap that refuses the view under one counter admits it under a coarser
    one."""

    cap = _full_tokens(quarter_counter)
    assert _run(ROWS, cap, quarter_counter)[0].status == "ok"
    assert _run(ROWS, cap, bytes_counter)[0].reason == "RESULT_TOO_LARGE"


def test_the_same_response_yields_the_same_outcome_twice():
    """Determinism, delivered and refused."""

    full = _full_tokens(bytes_counter)
    for cap in (full, full - 1):
        first, *_ = _run(ROWS, cap, bytes_counter)
        second, *_ = _run(ROWS, cap, bytes_counter)
        assert (first.status, first.reason) == (second.status, second.reason)
        assert _stable(first.model_view) == _stable(second.model_view)


def test_a_view_under_the_cap_is_identical_at_any_larger_cap():
    full = _full_tokens(bytes_counter)
    tight, *_ = _run(ROWS, full, bytes_counter)
    roomy, *_ = _run(ROWS, 1_000_000, bytes_counter)
    assert _stable(tight.model_view) == _stable(roomy.model_view)


# -- what does not change ------------------------------------------------------


def test_no_data_under_the_cap_is_unchanged():
    outcome, _, _, *_ = _run([], 1_000_000, bytes_counter)
    view = outcome.model_view
    assert outcome.status == "no_data"
    assert view["content"] == [] and view["truncated"] is False
    assert view["omitted_rows"] == 0 and view["omitted_bytes"] == 0


def test_the_projection_revision_moves_because_the_view_semantics_changed():
    outcome, *_ = _run(ROWS, 1_000_000, bytes_counter)
    assert outcome.model_view["projection_revision"] == PROJECTION_REVISION
    assert PROJECTION_REVISION != "m1-01-tool-view-v5"
    assert outcome.evidence.projection_revision == PROJECTION_REVISION


# -- 3. precedence: other refusals come first ----------------------------------


def test_a_malformed_result_is_reported_before_the_size_check():
    payload = b"this is not json"
    executor, transport, sink, _ = _build(1, bytes_counter)
    transport.response = TransportResponse(body=payload)
    outcome = executor.execute(request())
    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
    assert sink.records == []


def test_a_suspension_during_the_read_wins_over_a_too_large_view():
    """Existing precedence (``test_a_suspension_during_flight_...``): the human
    decision is the outcome, and the read survives as history only."""

    control = FixedControl(
        later=ControlSnapshot(7, 0, 0, suspended=True), later_after=2
    )
    outcome, payload, sink, *_ = _run(ROWS, 1, bytes_counter, control=control)
    assert (outcome.status, outcome.reason) == ("denied", "SUSPENDED")
    assert outcome.source_contact == "confirmed" and not outcome.adopted
    assert outcome.model_view["content"] is None
    assert len(sink.records) == 1
    assert sink.records[0].adopted is False and sink.records[0].raw == payload


def test_the_byte_ceiling_is_checked_first_and_its_refusal_is_unchanged():
    """Contract 6: the same reason, the byte-path detail field and prose."""

    reg = dataclasses.replace(registration(), max_result_bytes=64, max_view_tokens=1)
    executor, transport, sink, _ = _build(1, bytes_counter, reg=reg)
    transport.response = TransportResponse(
        body=body([{"series": "x" * 200, "value": 1}])
    )
    outcome = executor.execute(request())
    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.source_contact == "confirmed"
    assert view["content"] is None and outcome.evidence is None
    assert view["max_result_bytes"] == 64
    assert view["message"] == (
        "The result exceeded the 64-byte limit for this tool. "
        "Retry with a smaller limit or a narrower window."
    )
    assert "view_tokens" not in view and "max_view_tokens" not in view
    assert sink.records == []


# -- 4. accounting -------------------------------------------------------------


def test_a_refused_view_is_still_a_read_the_ledger_and_counters_charge():
    full = _full_tokens(bytes_counter)
    refused, _, sink, ledger, executor = _run(ROWS, full - 1, bytes_counter)
    assert refused.reason == "RESULT_TOO_LARGE" and sink.records == []
    assert executor.operations_used == 1
    assert len(ledger.charges) >= 1 and len(ledger.dispatches) >= 1
    delivered, _, _, ledger_ok, executor_ok = _run(ROWS, full, bytes_counter)
    assert delivered.status == "ok"
    assert executor_ok.operations_used == executor.operations_used
    assert ledger_ok.charges == ledger.charges


# -- 5. citation semantics -----------------------------------------------------


def test_a_refusal_is_not_citable_and_carries_no_evidence_reference():
    full = _full_tokens(bytes_counter)
    outcome, *_ = _run(ROWS, full - 1, bytes_counter)
    view = outcome.model_view
    assert view["citable_as_fact"] is False and view["status"] != "ok"
    assert "evidence_id" not in view and outcome.evidence is None


# -- 7. the counter ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [lambda s: -1, lambda s: True, lambda s: "3", lambda s: 1.5]
)
def test_a_counter_that_returns_a_non_count_is_refused(bad):
    with pytest.raises(ToolContractError, match="INVALID_TOKEN_COUNTER"):
        _run(ROWS, 1000, bad)


# -- 9. one count per adopted view ---------------------------------------------


def test_each_adopted_view_is_counted_exactly_once_over_its_whole_text():
    texts = []

    def recording(text: str) -> int:
        texts.append(text)
        return len(text.encode("utf-8"))

    full = _full_tokens(bytes_counter)
    ok, *_ = _run(ROWS, full, recording)
    assert len(texts) == 1
    assert texts[0] == canonical(ok.model_view)

    texts.clear()
    refused, *_ = _run(ROWS, full - 1, recording)
    assert len(texts) == 1
    complete = json.loads(texts[0])
    assert complete["content"] == ROWS and complete["truncated"] is False
    assert refused.model_view["view_tokens"] == len(texts[0].encode("utf-8"))


def test_a_one_mebibyte_result_is_counted_once_and_refused():
    rows = [{"series": index, "pad": "y" * 100} for index in range(7000)]
    payload = body(rows)
    assert 700_000 < len(payload) <= 1024 * 1024
    counted = []

    def recording(text: str) -> int:
        counted.append(len(text))
        return len(text.encode("utf-8"))

    reg = dataclasses.replace(
        registration(), max_result_bytes=1024 * 1024, max_view_tokens=25_000
    )
    outcome, *_ = _run(rows, 25_000, recording, reg=reg)
    assert outcome.reason == "RESULT_TOO_LARGE"
    assert len(counted) == 1 and counted[0] >= 700_000


# -- 8. registration -----------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "25000", None])
def test_max_view_tokens_must_be_an_int_of_at_least_one(bad):
    with pytest.raises(ToolContractError, match="VIEW_LIMIT_OUT_OF_RANGE"):
        dataclasses.replace(registration(), max_view_tokens=bad)


@pytest.mark.parametrize("ok", [1, 2, 8192, MAX_VIEW_TOKENS, 1_000_000])
def test_max_view_tokens_is_not_tied_to_max_result_bytes(ok):
    reg = dataclasses.replace(registration(), max_result_bytes=64, max_view_tokens=ok)
    assert reg.max_view_tokens == ok


def test_the_old_byte_field_is_gone_without_an_alias():
    fields = {f.name for f in dataclasses.fields(registration())}
    assert "max_view_tokens" in fields and "max_view_bytes" not in fields
    with pytest.raises(TypeError):
        registration(max_view_bytes=512)


def test_the_registered_limit_is_part_of_the_tool_registry_revision():
    one = ToolRegistry([dataclasses.replace(registration(), max_view_tokens=1000)])
    two = ToolRegistry([dataclasses.replace(registration(), max_view_tokens=1001)])
    assert one.revision != two.revision


def test_the_byte_constant_is_replaced_by_the_token_constant():
    import opspilot.tools.otel_demo as otel_demo

    assert MAX_VIEW_TOKENS == 25_000
    assert not hasattr(otel_demo, "MAX_VIEW_BYTES")


# -- 11. model-visible wording -------------------------------------------------


def test_the_shipped_tools_describe_refusal_not_truncation():
    from opspilot.tools import otel_demo

    registrations = otel_demo._registrations()
    assert {r.name for r in registrations} == {METRICS_TOOL, TRACES_TOOL}
    for reg in registrations:
        limits = reg.description.limits.lower()
        assert "max_view_bytes" not in limits, reg.name
        assert "token" in limits, reg.name
        assert "omitted_rows" not in limits, reg.name
        assert "dropping trailing" not in limits, reg.name
        assert "truncated at" not in limits, reg.name
    assert "max_view_bytes" not in json.dumps(TOOL_SCHEMAS)


def test_the_removed_view_augment_machinery_is_gone():
    import opspilot.tools.executor as executor
    import opspilot.tools.otel_demo as otel_demo

    assert "view_augment" not in {f.name for f in dataclasses.fields(TransportResponse)}
    assert not hasattr(executor, "_fit_rows")
    assert not hasattr(otel_demo, "MAX_VIEW_BYTES")


# -- the shipped tools through the profile factory, injected counter -----------


def _factory_executor(monkeypatch, opener, counter):
    store = FakeStore(_lease(), deadline=NOW + timedelta(minutes=10))
    lease = store.lease
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    config = OtelDemoConfig(
        prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL, token=None
    )
    factory = otel_demo_executor_factory(
        store,
        evidence=RecordingSink(),
        clock=FakeClock(start=NOW),
        config=config,
        token_counter=counter,
    )
    executor = factory(lease, _input(str(lease.run_id)))
    assert isinstance(executor, ReadOnlyToolExecutor)
    return executor


def _long_expr(length: int) -> str:
    expr = "up" + " or up" * ((length - 2) // 6)
    assert length - 6 < len(expr) <= length
    return expr


def _series(count: int, *, pad: int) -> list[dict]:
    stamp = int(WINDOW.start.timestamp()) + 60
    return [
        {
            "metric": {
                "__name__": "m",
                "service_name": f"svc-{index:05d}",
                "pad": "x" * pad,
            },
            "values": [[stamp, "1"]],
        }
        for index in range(count)
    ]


def _matrix(rows: list[dict]) -> bytes:
    return json.dumps(
        {"status": "success", "data": {"resultType": "matrix", "result": rows}}
    ).encode()


def _spans_trace(count: int) -> bytes:
    start_us = int(WINDOW.start.timestamp() * 1_000_000) + 1_000_000
    spans = [
        {
            "traceID": "t-cardinality",
            "spanID": f"span{i:05d}",
            "operationName": f"op-{i:05d}",
            "startTime": start_us + i,
            "duration": 100 + i,
            "processID": "p1",
            "tags": [],
            "logs": [],
            "references": [],
        }
        for i in range(count)
    ]
    trace = {
        "traceID": "t-cardinality",
        "spans": spans,
        "processes": {"p1": {"serviceName": "checkout", "tags": []}},
    }
    return json.dumps({"data": [trace]}).encode()


class _Recording:
    """A counter that remembers what it was asked to count."""

    def __init__(self, offset: int = 0, divisor: int = 1):
        self.offset = offset
        self.divisor = divisor
        self.texts: list[str] = []

    def __call__(self, text: str) -> int:
        self.texts.append(text)
        return -(-len(text.encode("utf-8")) // self.divisor) + self.offset


SHIPPED = [
    pytest.param(
        lambda: FakeOpener(by_query={GOOD_EXPR: _matrix(_series(20, pad=10))}),
        lambda: _call(METRICS_TOOL, {"expr": GOOD_EXPR}),
        id="metrics_range_query",
    ),
    pytest.param(
        lambda: FakeOpener(routes={"/api/traces": CHECKOUT_TRACES}),
        lambda: _call(TRACES_TOOL, {"service": "checkout", "limit": 2}),
        id="traces_search",
    ),
]


@pytest.mark.parametrize(("opener", "call"), SHIPPED)
def test_the_shipped_tools_are_held_to_exactly_25k_tokens_on_the_whole_view(
    monkeypatch, opener, call
):
    """Contract 1/2 through both real registrations, at the exact boundary: a
    counter offset so the complete view counts exactly ``MAX_VIEW_TOKENS`` is
    delivered untouched, one token more is refused. Includes whatever the
    adapter adds to the view (``span_groups`` for traces)."""

    probe = _Recording(divisor=4)
    view = _factory_executor(monkeypatch, opener(), probe).execute(call()).model_view
    assert view["status"] == "ok" and view["truncated"] is False
    natural = probe(probe.texts[0])
    assert natural < MAX_VIEW_TOKENS

    exact = _Recording(offset=MAX_VIEW_TOKENS - natural, divisor=4)
    delivered = _factory_executor(monkeypatch, opener(), exact).execute(call())
    assert delivered.status == "ok"
    assert delivered.model_view["content"] == view["content"]
    assert delivered.evidence is not None

    over = _Recording(offset=MAX_VIEW_TOKENS - natural + 1, divisor=4)
    refused = _factory_executor(monkeypatch, opener(), over).execute(call())
    assert (refused.status, refused.reason) == ("error", "RESULT_TOO_LARGE")
    assert refused.evidence is None and refused.model_view["content"] is None
    assert refused.model_view["max_view_tokens"] == MAX_VIEW_TOKENS
    assert refused.model_view["view_tokens"] == MAX_VIEW_TOKENS + 1
    assert refused.model_view["message"].startswith(
        f"The tool call result is too large to return: {MAX_VIEW_TOKENS + 1}/"
        f"{MAX_VIEW_TOKENS} tokens."
    )


@pytest.mark.parametrize(
    "expr",
    [GOOD_EXPR, _long_expr(1990)],
    ids=["short-expression", "1990-char-expression"],
)
def test_metrics_range_query_over_the_limit_is_refused_with_the_true_count(
    monkeypatch, expr
):
    """No ``view_augment`` here, and the old gap (fixed fields and the echoed
    query on top of full rows) is now a refusal. Rows fill the budget almost
    exactly, so it is the fixed fields that tip it over."""

    each = _row_bytes(_series(1, pad=60)[0]) + 1
    rows = _series((MAX_VIEW_TOKENS - 64) // each, pad=60)
    assert MAX_VIEW_TOKENS - 200 <= _row_bytes(rows) <= MAX_VIEW_TOKENS
    opener = FakeOpener(by_query={expr: _matrix(rows)})
    counter = _Recording()
    executor = _factory_executor(monkeypatch, opener, counter)

    outcome = executor.execute(_call(METRICS_TOOL, {"expr": expr}))

    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.evidence is None and view["content"] is None
    assert len(counter.texts) == 1
    complete = json.loads(counter.texts[0])
    assert complete["content"] == rows and complete["truncated"] is False
    assert view["view_tokens"] == len(counter.texts[0].encode("utf-8"))
    assert view["view_tokens"] > MAX_VIEW_TOKENS


def test_traces_search_over_the_limit_is_refused_as_a_whole(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": _spans_trace(260)})
    counter = _Recording()
    executor = _factory_executor(monkeypatch, opener, counter)

    outcome = executor.execute(
        _call(TRACES_TOOL, {"service": "checkout", "limit": 260})
    )

    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.evidence is None and view["content"] is None
    assert "span_groups" not in view
    assert view["view_tokens"] > MAX_VIEW_TOKENS
    counted = json.loads(counter.texts[0])
    assert len(counted["content"]) == 260 and "span_groups" in counted


def test_traces_search_span_groups_cover_every_row_when_under_the_limit(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _factory_executor(monkeypatch, opener, quarter_counter)

    outcome = executor.execute(_call(TRACES_TOOL, {"service": "checkout", "limit": 2}))

    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("ok", None), view
    assert view["truncated"] is False and view["omitted_rows"] == 0
    assert view["returned_count"] == view["result_count"] == len(view["content"])
    assert sum(g["rows"] for g in view["span_groups"]) == len(view["content"])
    assert view["spans_shown"] == len(view["content"])
    assert quarter_counter(canonical(view)) <= MAX_VIEW_TOKENS
    # The note no longer speaks of truncation: nothing is dropped for size.
    note = view["span_groups_note"].lower()
    assert "truncat" not in note and "byte cap" not in note


def test_metrics_range_query_under_the_limit_is_unchanged(monkeypatch):
    rows = _series(20, pad=10)
    opener = FakeOpener(by_query={GOOD_EXPR: _matrix(rows)})
    executor = _factory_executor(monkeypatch, opener, bytes_counter)

    view = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR})).model_view

    assert view["content"] == rows
    assert view["truncated"] is False and view["omitted_rows"] == 0
    assert view["returned_count"] == view["result_count"] == 20


# -- the refusal says which call it refused (contract revision, real Runs) ------


def test_a_refusal_echoes_the_accepted_query_of_the_same_call_as_the_ok_view():
    full = _full_tokens(bytes_counter)
    delivered, *_ = _run(ROWS, full, bytes_counter)
    refused, *_ = _run(ROWS, full - 1, bytes_counter)
    assert refused.model_view["query"] == delivered.model_view["query"]
    assert refused.model_view["window"] == delivered.model_view["window"]
    assert refused.model_view["query"]  # the call's own parameters, not empty


def test_a_refusal_query_carries_no_row_content():
    rows = [{"series": "SECRET-ROW-MARKER-" + "z" * 300}]
    full = bytes_counter(canonical(_run(rows, 1_000_000, bytes_counter)[0].model_view))
    outcome, *_ = _run(rows, full - 1, bytes_counter)
    view = outcome.model_view
    assert "SECRET-ROW-MARKER" not in json.dumps(view["query"])
    assert "SECRET-ROW-MARKER" not in json.dumps(view["window"])


def _narrow_window():
    """Strictly inside ``WINDOW``, the authorized frame ``_call`` carries."""

    return WINDOW.start + timedelta(seconds=60), WINDOW.start + timedelta(seconds=180)


NARROWED = [
    pytest.param(
        lambda start, end: FakeOpener(by_query={"up": _matrix(_series(20, pad=10))}),
        lambda start, end: _call(
            METRICS_TOOL,
            {"expr": "up", "start": start.isoformat(), "end": end.isoformat()},
        ),
        id="metrics_range_query",
    ),
    pytest.param(
        lambda start, end: FakeOpener(routes={"/api/traces": CHECKOUT_TRACES}),
        lambda start, end: _call(
            TRACES_TOOL,
            {
                "service": "checkout",
                "limit": 2,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        ),
        id="traces_search",
    ),
]


@pytest.mark.parametrize(("opener", "call"), NARROWED)
def test_a_refusal_reports_the_queried_window_and_query_not_the_authorized_frame(
    monkeypatch, opener, call
):
    """The real Runs showed the model reading the authorized frame (24 h in a
    Run) in a refusal as the window it had asked for. With ``start``/``end`` given, the
    refusal's ``window`` is that narrowed window (as the ok view's is), and its
    ``query`` is the ok view's ``query`` for the same call."""

    start, end = _narrow_window()
    probe = _Recording(divisor=4)
    ok = (
        _factory_executor(monkeypatch, opener(start, end), probe)
        .execute(call(start, end))
        .model_view
    )
    assert ok["status"] == "ok"
    natural = probe(probe.texts[0])
    assert ok["window"] == Window(start, end).as_json()
    assert ok["window"] != WINDOW.as_json()

    over = _Recording(offset=MAX_VIEW_TOKENS - natural + 1, divisor=4)
    refused = _factory_executor(monkeypatch, opener(start, end), over).execute(
        call(start, end)
    )
    view = refused.model_view
    assert (refused.status, refused.reason) == ("error", "RESULT_TOO_LARGE")
    assert set(view) == REFUSAL_KEYS
    assert view["window"] == ok["window"]
    assert view["query"] == ok["query"]
    assert "start" in view["query"] and "end" in view["query"]
    # The narrowed window, not the authorized frame the call carried.
    assert view["window"] == Window(start, end).as_json()
    assert view["window"] != WINDOW.as_json()


def _wide_frame_executor(monkeypatch, opener, frame, counter):
    """Like ``_factory_executor`` but with the Run's authorized frame set to
    ``frame`` (the default one is ``WINDOW``, only five minutes)."""

    store = FakeStore(_lease(), deadline=NOW + timedelta(minutes=10))
    lease = store.lease
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    config = OtelDemoConfig(
        prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL, token=None
    )
    face = ToolFace(
        tool_schemas=TOOL_SCHEMAS,
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=lambda run_id: {
            "type": "opspilot-evidence-context-v4",
            "run_id": run_id,
            "time_policies": [
                {
                    "id": "policy-window-1",
                    "mode": "historical_window",
                    "all_authorized_targets": True,
                    "window": frame.as_json(),
                }
            ],
        },
    )
    investigation_input = face.input_for(
        run_id=str(lease.run_id),
        question="Why is checkout erroring?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )
    factory = otel_demo_executor_factory(
        store,
        evidence=RecordingSink(),
        clock=FakeClock(start=NOW),
        config=config,
        token_counter=counter,
    )
    return factory(lease, investigation_input)


@pytest.mark.parametrize(
    ("opener", "call"),
    [
        pytest.param(
            lambda: FakeOpener(by_query={"up": _matrix(_series(20, pad=10))}),
            lambda frame: _call(METRICS_TOOL, {"expr": "up"}, window=frame),
            id="metrics_range_query",
        ),
        pytest.param(
            lambda: FakeOpener(routes={"/api/traces": CHECKOUT_TRACES}),
            lambda frame: _call(
                TRACES_TOOL, {"service": "checkout", "limit": 2}, window=frame
            ),
            id="traces_search",
        ),
    ],
)
def test_a_refusal_without_start_end_reports_the_default_one_hour_query_window(
    monkeypatch, opener, call
):
    """No ``start``/``end`` given: the query window is the default one (batch
    B: the hour ending at the frame's end), so a refusal reports that hour --
    the same ``window`` the ok view of the same call carries -- and not the
    whole authorized frame. The frame here is three hours, so the two differ."""

    frame = Window(WINDOW.end - timedelta(hours=3), WINDOW.end)
    default = Window(WINDOW.end - timedelta(hours=1), WINDOW.end)
    probe = _Recording(divisor=4)
    ok = (
        _wide_frame_executor(monkeypatch, opener(), frame, probe)
        .execute(call(frame))
        .model_view
    )
    assert ok["status"] == "ok", ok
    assert ok["window"] == default.as_json()
    natural = probe(probe.texts[0])

    over = _Recording(offset=MAX_VIEW_TOKENS - natural + 1, divisor=4)
    refused = _wide_frame_executor(monkeypatch, opener(), frame, over).execute(
        call(frame)
    )
    view = refused.model_view
    assert (refused.status, refused.reason) == ("error", "RESULT_TOO_LARGE")
    assert view["window"] == default.as_json() == ok["window"]
    assert view["window"] != frame.as_json()
    assert view["query"] == ok["query"]
