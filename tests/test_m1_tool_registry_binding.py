"""Issued scopes cannot survive changed tool or credential contracts."""

import pytest

from opspilot.tools import (
    ParameterSpec,
    ReadOnlyToolExecutor,
    TargetRegistry,
    ToolRegistry,
    TransportResponse,
)
from tests.m1_tool_support import (
    FakeClock,
    FakeTransport,
    FixedControl,
    RecordingLedger,
    RecordingSink,
    body,
    build,
    description,
    registration,
    request,
    target,
)

# Keep the public name/version/source/verb unchanged. Each of these edits
# previously changed a query contract without invalidating its authorization.
CONTRACT_CHANGES = [
    {"parameters": {"expr": ParameterSpec("string", required=False)}},
    {"parameters": {"expr": ParameterSpec("number", required=True)}},
    # C3 section 7 check 2: the model-visible face is part of the registration
    # contract, so a parameter description edit alone (kind/required, and the
    # rest of the parameter set, held fixed) must also invalidate an old
    # scope, same as a schema edit. `step_seconds` is kept unchanged here —
    # overriding `parameters` replaces the whole mapping, so dropping it would
    # confound "description changed" with "a parameter was removed".
    {
        "parameters": {
            "expr": ParameterSpec(
                "string",
                required=True,
                description="A different rendering of the same query parameter.",
            ),
            "step_seconds": ParameterSpec(
                "integer",
                description="The resolution step, in seconds, between returned points.",
            ),
        }
    },
    {"description": description(cannot_prove="A different cannot_prove sentence.")},
    {"result_path": ("other", "result")},
    {"request_timeout_seconds": 20.0},
    {"max_result_bytes": 8192},
    {"max_view_bytes": 1024},
    {"max_window_seconds": 7200},
    {"error_classes": {"503": "SOURCE_ERROR", "400": "INVALID_PARAMS"}},
    {"incomplete_marker": "incomplete"},
]


@pytest.mark.parametrize("change", CONTRACT_CHANGES)
def test_tool_revision_changes_with_the_complete_contract(change):
    original = ToolRegistry([registration()])
    changed = ToolRegistry([registration(**change)])
    assert original.revision != changed.revision


@pytest.mark.parametrize("change", CONTRACT_CHANGES)
def test_old_scope_is_denied_before_transport_after_contract_change(change):
    issued, _, _, _ = build()
    transport, sink = FakeTransport(), RecordingSink()
    executor = ReadOnlyToolExecutor(
        scope=issued.scope,
        tools=ToolRegistry([registration(**change)]),
        targets=TargetRegistry([target()]),
        transport=transport,
        evidence=sink,
        control=FixedControl(),
        clock=FakeClock(),
        ledger=RecordingLedger(),
    )

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("denied", "TOOL_REGISTRY_CHANGED")
    assert outcome.source_contact == "none"
    assert not transport.called and sink.records == []
    assert executor.operations_used == 0


def test_old_scope_is_denied_after_credential_binding_changes():
    issued, _, _, _ = build()
    executor, transport, sink, _ = build(
        targets=[target(credential_ref="prom-ro-other")],
        scope_overrides={"registry_revision": issued.scope.registry_revision},
    )
    outcome = executor.execute(request())
    assert (outcome.status, outcome.reason) == ("denied", "TARGET_REGISTRY_CHANGED")
    assert not transport.called and sink.records == []


def test_new_scope_accepts_the_changed_contract_and_keeps_its_audit_revision():
    tools = ToolRegistry([registration(max_window_seconds=7200)])
    executor, transport, _, _ = build(
        registrations=[registration(max_window_seconds=7200)]
    )
    transport.response = TransportResponse(body=body([{"value": 1}]))

    outcome = executor.execute(request())

    assert outcome.status == "ok" and outcome.adopted
    assert outcome.operation.audit_json()["tool_registry_revision"] == tools.revision
    assert outcome.evidence.view["tool_registry_revision"] == tools.revision
    assert executor.scope.tool_registry_revision == tools.revision


def test_contract_ordering_does_not_invalidate_an_unchanged_authorization():
    first = registration()
    reordered = registration(
        parameters=dict(reversed(list(first.parameters.items()))),
        error_classes=dict(reversed(list(first.error_classes.items()))),
    )
    second = registration(name="metrics.other_query")
    assert (
        ToolRegistry([first, second]).revision
        == ToolRegistry([second, reordered]).revision
    )
