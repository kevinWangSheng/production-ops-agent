"""Deterministic doubles and builders for the read-only tool executor tests.

No network, no container and no real data source: every test drives the
executor through a fake clock, a fake transport, a recording evidence sink and
a fixed control authority.
"""

import json
from datetime import datetime, timedelta, timezone

from opspilot.tools import (
    ControlSnapshot,
    ParameterSpec,
    QueryScope,
    ReadOnlyToolExecutor,
    RegisteredTarget,
    TargetRegistry,
    ToolRegistration,
    ToolRegistry,
    ToolRequest,
    Window,
)

WINDOW_START = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 14, 1, 5, tzinfo=timezone.utc)

# Stands in for credential material that only the transport double knows. No
# test ever puts it into an executor input, so finding it in an executor output
# means it leaked. Deliberately not shaped like any real credential.
TRANSPORT_ONLY_MARKER = "transport-only-marker-not-a-real-value"


class FakeClock:
    def __init__(self, start=NOW, monotonic=1_000.0):
        self._now = start
        self._monotonic = monotonic

    def now(self):
        return self._now

    def monotonic(self):
        return self._monotonic

    def advance(self, seconds):
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class FakeTransport:
    """Returns a prepared response, or raises, after burning fake wall time."""

    def __init__(self, response=None, error=None, clock=None, duration=0.0):
        self.response = response
        self.error = error
        self.clock = clock
        self.duration = duration
        self.requests = []
        self.marker = TRANSPORT_ONLY_MARKER

    def fetch(self, request):
        self.requests.append(request)
        if self.clock is not None and self.duration:
            self.clock.advance(self.duration)
        if self.error is not None:
            raise self.error
        return self.response

    @property
    def called(self):
        return bool(self.requests)


class RecordingSink:
    def __init__(self, fail=False, reference=None):
        self.records = []
        self.fail = fail
        self.reference = reference

    def register(self, record):
        self.records.append(record)
        if self.fail:
            raise RuntimeError("evidence store unavailable")
        return self.reference if self.reference is not None else record.evidence_id


class FixedControl:
    def __init__(self, generation=7, suspended=False, later=None):
        self.generation = generation
        self.suspended = suspended
        self.later = later
        self.calls = 0

    def snapshot(self, scope):
        self.calls += 1
        if self.later is not None and self.calls > 1:
            return self.later
        return ControlSnapshot(
            control_generation=self.generation, suspended=self.suspended
        )


class SlowControl(FixedControl):
    """A control lookup that burns fake wall time, as a real PG read would.

    The duration is charged to the fake clock, so a test can put the scope
    deadline inside the lookup without sleeping. ``slow_on`` limits the cost to
    specific 1-based call numbers, so a test can make only the pre-dispatch
    lookup slow, or only the in-flight re-check.
    """

    def __init__(self, clock, duration, slow_on=None, **overrides):
        super().__init__(**overrides)
        self.clock = clock
        self.duration = duration
        self.slow_on = slow_on

    def snapshot(self, scope):
        if self.slow_on is None or (self.calls + 1) in self.slow_on:
            self.clock.advance(self.duration)
        return super().snapshot(scope)


class UnavailableControl:
    def __init__(self):
        self.calls = 0

    def snapshot(self, scope):
        self.calls += 1
        raise RuntimeError("control database unreachable")


def registration(**overrides):
    fields = {
        "name": "metrics.range_query",
        "version": "v1",
        "source": "prometheus",
        "verb": "query",
        "parameters": {
            "expr": ParameterSpec("string", required=True),
            "step_seconds": ParameterSpec("integer"),
        },
        "result_path": ("data", "result"),
        "request_timeout_seconds": 10.0,
        "max_result_bytes": 4096,
        "max_view_bytes": 512,
        "max_window_seconds": 3600,
        "error_classes": {"503": "SOURCE_UNAVAILABLE", "400": "INVALID_PARAMS"},
        "incomplete_marker": "partial",
    }
    fields.update(overrides)
    return ToolRegistration(**fields)


def target(**overrides):
    fields = {
        "target_id": "checkout-prod",
        "source": "prometheus",
        "endpoint": "https://metrics.internal:9090",
        "credential_ref": "prom-ro-checkout",
        "selector": {"namespace": "checkout"},
        "display_name": "checkout",
    }
    fields.update(overrides)
    return RegisteredTarget(**fields)


def scope(targets, tools=None, *, tool_registry, **overrides):
    fields = {
        "scope_id": "scope-1",
        "subject_kind": "incident",
        "subject_id": "incident-42",
        "run_id": "run-9",
        "control_generation": 7,
        "registry_revision": targets.revision,
        "tool_registry_revision": tool_registry.revision,
        "target_ids": frozenset({"checkout-prod"}),
        "tool_names": frozenset({"metrics.range_query"}),
        "window": Window(WINDOW_START, WINDOW_END),
        "deadline": NOW + timedelta(seconds=600),
    }
    if tools is not None:
        fields["tool_names"] = frozenset(tools)
    fields.update(overrides)
    return QueryScope(**fields)


def body(rows=None, partial=False, extra=None):
    payload = {"data": {"result": [] if rows is None else rows}}
    if partial:
        payload["partial"] = True
    if extra:
        payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def request(**overrides):
    fields = {
        "step_id": "step-1",
        "tool_index": 0,
        "tool_name": "metrics.range_query",
        "target_ref": "checkout-prod",
        "params": {"expr": "rate(http_errors[5m])"},
        "window": {
            "start": WINDOW_START.isoformat(),
            "end": (WINDOW_START + timedelta(minutes=10)).isoformat(),
        },
    }
    fields.update(overrides)
    return ToolRequest(**fields)


def build(
    *,
    registrations=None,
    targets=None,
    transport=None,
    sink=None,
    control=None,
    clock=None,
    scope_overrides=None,
):
    """Assemble an executor plus the doubles the test will assert on."""

    clock = clock or FakeClock()
    tool_registry = ToolRegistry(registrations or [registration()])
    target_registry = TargetRegistry(targets or [target()])
    transport = transport if transport is not None else FakeTransport(clock=clock)
    sink = sink if sink is not None else RecordingSink()
    control = control if control is not None else FixedControl()
    executor = ReadOnlyToolExecutor(
        scope=scope(
            target_registry, tool_registry=tool_registry, **(scope_overrides or {})
        ),
        tools=tool_registry,
        targets=target_registry,
        transport=transport,
        evidence=sink,
        control=control,
        clock=clock,
    )
    return executor, transport, sink, clock
