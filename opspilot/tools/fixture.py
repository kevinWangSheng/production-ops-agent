"""The fixture tool profile: one Prometheus range query with a canned view.

This is the tool setup the bounded live Runs and the local demo use (no
real OTel, no network): one registered target, one read-only tool, a
transport that returns a fixed sample inside a fixed historical window.
It is a tool *profile*, not a tool source -- a production profile replaces
the transport and registrations, not the executor, the ledger or the
evidence path, which are the product's and are wired here exactly as they
would be for a real source:

* ``DurableToolLedger`` charges every dispatch through the lease-fenced
  ``charge_tool`` write, so ``tool_operations_used`` / ``tool_seconds_used``
  survive a worker restart (technical plan section 13);
* the evidence sink is the workbench's durable evidence store, so a view
  the model cites resolves on the page.

The control authority echoes the scope's generations: this profile has no
Controller of its own, and the real fence on every write is the store's
lease (``charge_tool``, ``commit_tool``). The same holds for the doubles
the tests use.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from opspilot.investigation.context import InvestigationInput
from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import DISCIPLINE_VARIANT, investigation_versions
from opspilot.investigation.runner import ExecutorFactory
from opspilot.persistence import DurableStore, Lease
from opspilot.tools.executor import (
    MAX_OPERATIONS_PER_RUN,
    MAX_TOOL_SECONDS_PER_RUN,
    Clock,
    ControlSnapshot,
    EvidenceSink,
    QueryScope,
    ReadOnlyToolExecutor,
    TransportRequest,
    TransportResponse,
)
from opspilot.tools.ledger import DurableToolLedger
from opspilot.tools.outcomes import Window
from opspilot.tools.registry import (
    ParameterSpec,
    RegisteredTarget,
    TargetRegistry,
    ToolDescription,
    ToolRegistration,
    ToolRegistry,
)

__all__ = [
    "FIXTURE_TARGET",
    "FIXTURE_TOOL",
    "TOOL_SCHEMA_REVISION",
    "WINDOW_END",
    "WINDOW_START",
    "fixture_executor_factory",
    "fixture_face",
    "fixture_versions",
]

#: Provider function names may not contain ``.``; the registered tool is
#: ``metrics.range_query`` in the registry vocabulary and this is its face.
FIXTURE_TOOL = "metrics_range_query"
FIXTURE_TARGET = "checkout-prod"
FIXTURE_EXPR = "rate(http_errors[5m])"
WINDOW_START = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
TOOL_SCHEMA_REVISION = "fixture-1"
_TIME_POLICY = "policy-window-1"
_CANNED_BODY = b'{"data": {"result": [{"metric": "http_errors_rate", "value": 0.042}]}}'

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": FIXTURE_TOOL,
            "description": (
                "Return a projected Prometheus range query for one registered "
                "target inside the authorized window. The view is sampled: a "
                "missing series is unknown, not zero. Listing a series does "
                "not prove health. Available expressions: "
                f'["{FIXTURE_EXPR}"]; any other value returns an error. '
                "At most 512 view bytes; truncated views set truncated true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expr": {
                        "type": "string",
                        "description": "Exact PromQL string from the available list.",
                    }
                },
                "required": ["expr"],
            },
        },
    },
)


def _registration() -> ToolRegistration:
    return ToolRegistration(
        name=FIXTURE_TOOL,
        version="v1",
        source="prometheus",
        verb="query",
        description=ToolDescription(
            returns=(
                "The evaluated Prometheus range-query series: one point series "
                "per returned label set, from the range_query source."
            ),
            window_format="The authorized absolute query window, appended as {window}.",
            values_format=(
                "The authorized target label enumeration for this Run, appended "
                "as {values}; any other label value is refused."
            ),
            limits=(
                "Truncated at the registered max_view_bytes; excess points are "
                "dropped, not summarized or averaged."
            ),
            cannot_prove=(
                "A non-zero rate over this window does not by itself prove a "
                "user-visible error; it must be compared against the alert "
                "threshold and the service's normal baseline separately."
            ),
        ),
        may_contain_secrets=False,
        parameters={
            "expr": ParameterSpec(
                "string",
                required=True,
                description="The PromQL expression to evaluate.",
            ),
            "step_seconds": ParameterSpec(
                "integer",
                description="The resolution step, in seconds, between returned points.",
            ),
        },
        result_path=("data", "result"),
        request_timeout_seconds=10.0,
        max_result_bytes=4096,
        max_view_bytes=512,
        max_window_seconds=3600,
        error_classes={"503": "SOURCE_UNAVAILABLE", "400": "INVALID_PARAMS"},
        incomplete_marker="partial",
    )


def _target() -> RegisteredTarget:
    # ``credential_ref`` names a credential slot the transport would look
    # up; the canned transport never reads one.
    return RegisteredTarget(
        target_id=FIXTURE_TARGET,
        source="prometheus",
        endpoint="https://metrics.internal:9090",
        credential_ref="prom-ro-checkout",
        selector={"namespace": "checkout"},
        display_name="checkout",
    )


def _evidence_context(run_id: str) -> Mapping[str, Any]:
    """One historical-window policy over the fixture window, this Run's own.

    ``all_authorized_targets`` must be explicit or no fact can bind to the
    policy; ``reference_rule`` names the instant a delivered view is judged
    against (the bounded live Runs record the same context).
    """
    return {
        "type": "opspilot-evidence-context-v4",
        "run_id": run_id,
        "time_policies": [
            {
                "id": _TIME_POLICY,
                "mode": "historical_window",
                "all_authorized_targets": True,
                "window": {
                    "start": WINDOW_START.isoformat(),
                    "end": WINDOW_END.isoformat(),
                },
                "reference_rule": "response_received_at",
            }
        ],
    }


def fixture_face() -> ToolFace:
    return ToolFace(
        tool_schemas=TOOL_SCHEMAS,
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=_evidence_context,
    )


def fixture_versions() -> dict[str, str]:
    """The ``versions`` every Run under this profile records and every worker
    compares on claim (C3 §5): the loop's revisions plus this tool face."""
    return {**investigation_versions(), "tool_schema_revision": TOOL_SCHEMA_REVISION}


class _CannedTransport:
    def fetch(self, request: TransportRequest) -> TransportResponse:
        return TransportResponse(body=_CANNED_BODY, data_as_of=WINDOW_START)


class _EchoControl:
    """No Controller of its own: the scope's generations are the snapshot."""

    def snapshot(self, scope: QueryScope) -> ControlSnapshot:
        return ControlSnapshot(
            control_generation=scope.control_generation,
            global_suspension_generation=scope.global_suspension_generation,
            target_suspension_generation=scope.target_suspension_generation,
            suspended=False,
        )


def fixture_executor_factory(
    store: DurableStore, *, evidence: EvidenceSink, clock: Clock
) -> ExecutorFactory:
    """An ``ExecutorFactory`` for ``InvestigationRunner`` over this profile.

    The scope is issued per lease from the committed Run row (its deadline),
    never from the input snapshot or the model; the ledger and the evidence
    sink are the durable ones.
    """
    tools = ToolRegistry([_registration()])
    targets = TargetRegistry([_target()])

    def factory(lease: Lease, input: InvestigationInput) -> ReadOnlyToolExecutor:
        run = store.rebuild(lease.incident_id)["run"]
        scope = QueryScope(
            scope_id=f"fixture:{lease.run_id}:{lease.epoch}",
            subject_kind="incident",
            subject_id=str(lease.incident_id),
            run_id=str(lease.run_id),
            control_generation=lease.control_generation,
            global_suspension_generation=lease.global_suspension_generation,
            target_suspension_generation=lease.target_suspension_generation,
            registry_revision=targets.revision,
            tool_registry_revision=tools.revision,
            target_ids=frozenset({FIXTURE_TARGET}),
            tool_names=frozenset({FIXTURE_TOOL}),
            window=Window(WINDOW_START, WINDOW_END),
            deadline=run["deadline"],
        )
        return ReadOnlyToolExecutor(
            scope=scope,
            tools=tools,
            targets=targets,
            transport=_CannedTransport(),
            evidence=evidence,
            control=_EchoControl(),
            clock=clock,
            ledger=DurableToolLedger(
                store,
                lease,
                max_operations=MAX_OPERATIONS_PER_RUN,
                max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            ),
        )

    return factory
