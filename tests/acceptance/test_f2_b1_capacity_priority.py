"""Independent acceptance tests for F2 PR-B1 that need no database.

Contract: ``docs/tasks/2026-10-21-f2-b1-contract.md`` section 3 (public
interface) and decision rows U5-A / R-B1-1: ``priority_of`` is a pure,
fixed mapping of the parsed ``severity`` label -- ``critical`` -> 0,
``warning`` -> 1, anything else or missing -> 2, after ``strip().lower()``,
with no aliases. ``accept`` / ``_accept_in`` take ``priority`` (default 2);
``claimable_incidents`` keeps its signature. The PostgreSQL behaviour
(capacity, claim order, isolation, pool constraint) is in
``tests/integration/test_f2_b1_capacity_priority_postgres.py``.
"""

from __future__ import annotations

import inspect
from types import MappingProxyType

import pytest

from opspilot import alertmanager
from opspilot.persistence import DurableStore


def _priority_of():
    assert hasattr(alertmanager, "priority_of"), "opspilot.alertmanager.priority_of"
    return alertmanager.priority_of


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        ("critical", 0),
        ("warning", 1),
        ("Critical", 0),
        ("WARNING", 1),
        ("  critical ", 0),
        ("\twarning\n", 1),
        ("info", 2),
        ("none", 2),
        ("", 2),
        ("   ", 2),
        # U5-A is a fixed mapping: no aliases.
        ("page", 2),
        ("error", 2),
        ("high", 2),
        ("crit", 2),
        ("critical-ish", 2),
    ],
)
def test_priority_of_maps_severity(severity, expected):
    priority_of = _priority_of()
    labels = {"alertname": "X", "severity": severity}
    value = priority_of(labels)
    assert type(value) is int and value == expected


def test_priority_of_without_a_severity_label_is_other():
    priority_of = _priority_of()
    assert priority_of({}) == 2
    assert priority_of({"alertname": "X", "namespace": "otel-demo"}) == 2


def test_priority_of_reads_only_the_severity_label_and_does_not_mutate():
    priority_of = _priority_of()
    labels = {"alertname": "critical", "priority": "critical", "severity": "warning"}
    before = dict(labels)
    assert priority_of(labels) == 1
    assert labels == before
    # Any read-only Mapping is accepted.
    assert priority_of(MappingProxyType({"severity": "critical"})) == 0


def test_priority_of_is_deterministic():
    priority_of = _priority_of()
    for _ in range(3):
        assert [priority_of({"severity": s}) for s in ("critical", "warning", "x")] == [
            0,
            1,
            2,
        ]


@pytest.mark.parametrize("method", ["accept", "_accept_in"])
def test_accept_entry_points_take_a_keyword_priority_defaulting_to_other(method):
    parameters = inspect.signature(getattr(DurableStore, method)).parameters
    assert "priority" in parameters, method
    priority = parameters["priority"]
    assert priority.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    assert priority.default == 2


def test_claimable_incidents_signature_is_unchanged():
    parameters = inspect.signature(DurableStore.claimable_incidents).parameters
    assert list(parameters) == ["self", "limit"]
    assert parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["limit"].default == 20
