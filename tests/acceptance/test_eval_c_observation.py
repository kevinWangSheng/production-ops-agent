"""U1 gates: exact signatures, complete observations and fresh wash windows."""

from dataclasses import replace
from datetime import timedelta

import pytest

from scripts import eval_live_lab as lab
from tests.acceptance import eval_c_support as s
from tests.acceptance.test_eval_c_live_driver import offline  # noqa: F401


@pytest.fixture
def h(tmp_path):
    return s.Harness(tmp_path)


def span(h, **changes):
    matcher = h.signature().any_of[0]
    return replace(
        lab.ObservedSpan(
            "CANARY-TRACE",
            "CANARY-SPAN",
            matcher.service,
            matcher.operation,
            h.clock.now(),
            {"error": True},
        ),
        **changes,
    )


@pytest.mark.parametrize(
    "tags",
    [
        {"error": True},
        {"otel.status_code": "ERROR"},
        {"rpc.grpc.status_code": "13"},
        {"http.status_code": "503"},
    ],
)
def test_fault_gate_accepts_exact_visible_error_signature_with_checkout(h, tags):
    confirmed = h.clock.now()
    h.telemetry.custom_spans = lambda service, start, end: lab.SpanQuery(
        (span(h, tags=tags),) if service == h.signature().any_of[0].service else (),
        True,
    )
    result = lab.fault_gate(h.signature(), confirmed_at=confirmed, deps=h.deps)
    assert result.passed
    assert result.reason is None
    assert result.flag_confirmed_at == confirmed
    assert result.checkout_calls > 0
    assert result.matched
    assert all(item.start >= confirmed for item in result.matched)


@pytest.mark.parametrize(
    "defect",
    [
        "suffix",
        "service",
        "before-confirm",
        "not-error",
        "string-true",
        "span-incomplete",
        "calls-incomplete",
        "zero-calls",
        "missing-calls",
    ],
)
def test_fault_gate_rejects_unqualified_or_incomplete_observation(h, defect):
    confirmed = h.clock.now()
    candidate = span(h)
    if defect == "suffix":
        candidate = replace(candidate, operation="CANARY-PREFIX/" + candidate.operation)
    elif defect == "service":
        candidate = replace(candidate, service="CANARY-OTHER-SERVICE")
    elif defect == "before-confirm":
        candidate = replace(candidate, start=confirmed - timedelta(microseconds=1))
    elif defect == "not-error":
        candidate = replace(
            candidate, tags={"http.status_code": "200", "rpc.grpc.status_code": "0"}
        )
    elif defect == "string-true":
        candidate = replace(candidate, tags={"error": "true"})
    h.telemetry.custom_spans = lambda service, start, end: lab.SpanQuery(
        (candidate,) if service == candidate.service else (),
        defect != "span-incomplete",
    )
    h.telemetry.custom_calls = lambda start, end: lab.CallCount(
        None if defect == "missing-calls" else 0 if defect == "zero-calls" else 1,
        defect != "calls-incomplete",
    )
    before = h.clock.monotonic()
    result = lab.fault_gate(
        h.signature(),
        confirmed_at=confirmed,
        deps=h.deps,
        budget_seconds=20,
        poll_seconds=5,
    )
    assert not result.passed
    assert result.reason == "FAULT_NOT_OBSERVED"
    assert h.clock.monotonic() - before <= 20


def test_fault_gate_retries_incomplete_query_then_accepts_complete_data(h):
    confirmed = h.clock.now()
    h.telemetry.custom_spans = lambda service, start, end: lab.SpanQuery(
        (span(h),) if service == h.signature().any_of[0].service else (),
        h.clock.monotonic() >= 5,
    )
    result = lab.fault_gate(h.signature(), confirmed_at=confirmed, deps=h.deps)
    assert result.passed
    assert h.clock.monotonic() >= 5
    assert result.probes >= 2


def signatures(h):
    return [
        answer.signature for _, answer in h.scenarios if answer.signature is not None
    ]


def test_normal_requires_fresh_full_300_seconds_and_no_signature_hits(h):
    start = h.clock.now()
    result = lab.normal_window(signatures(h), not_before=start, deps=h.deps)
    assert result.passed and result.kind == "normal"
    assert result.matched == ()
    assert result.window.start >= start
    assert (result.window.end - result.window.start).total_seconds() == 300
    assert h.clock.now() >= result.window.end
    assert h.clock.monotonic() >= 300
    assert h.telemetry.queries
    assert all(
        end - start == timedelta(seconds=300) for _, start, end in h.telemetry.queries
    )


@pytest.mark.parametrize("defect", ["truncated", "missing", "zero", "error-span"])
def test_normal_window_never_certifies_missing_truncated_or_fault_data(h, defect):
    if defect == "truncated":
        h.telemetry.complete = False
    elif defect == "missing":
        h.telemetry.traffic = None
    elif defect == "zero":
        h.telemetry.traffic = 0
    else:
        h.telemetry.bad_wash = True
    result = lab.normal_window(signatures(h), not_before=h.clock.now(), deps=h.deps)
    assert not result.passed
    assert result.reason == "NORMAL_WINDOW_UNCONFIRMED"
    assert h.clock.monotonic() <= 600


def test_dirty_first_normal_window_is_discarded_and_fresh_window_rebuilt(h):
    start = h.clock.now()

    def query(service, begin, end):
        dirty = begin == start and service == h.signature().any_of[0].service
        return lab.SpanQuery((span(h, start=end),) if dirty else (), True)

    h.telemetry.custom_spans = query
    result = lab.normal_window(signatures(h), not_before=start, deps=h.deps)
    assert result.passed
    assert result.window.start >= start + timedelta(seconds=300)
    assert h.clock.monotonic() == 600
