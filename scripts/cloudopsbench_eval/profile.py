"""The evaluation tool profile ``cloudopsbench`` (#871 U1 = a).

The benchmark's 12 (Boutique) or 10 (TrainTicket) diagnostic tools, with the
benchmark's argument schemas, registered for the product's own
``ReadOnlyToolExecutor`` with the durable ledger, the durable evidence store
and the durable control fence -- the same composition as the shipped
profiles (``opspilot.tools.otel_demo_ext``), with this module's transport in
place of theirs. The transport forwards each call to the benchmark's MCP
server for the case (``backend.start_mcp_server``), which answers from the
case's recorded snapshot; nothing here reads a benchmark file.

This profile is evaluation-side only. ``opspilot.tools.profiles`` does not
know it; ``scripts.cloudopsbench_eval.product_entry`` starts the product's
web and worker with it. Configuration (both processes the same):

* ``COB_EVAL_SYSTEM``: ``boutique`` or ``train-ticket``;
* ``COB_EVAL_CATEGORY``: the case's fault category (picks the benchmark's
  ``GetResources`` variant for Boutique code-defect cases);
* ``COB_EVAL_MCP_URL`` (worker only): the loopback MCP endpoint.

What the model sees of a result (A3): the tool's text as the benchmark
returns it, one row per line. A query the snapshot did not record comes back
as the benchmark's own synthetic ``Error from server (NotFound)`` text; every
tool description says so, and the driver counts those answers.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta, timezone
from typing import Any

from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import investigation_versions
from opspilot.investigation.reports import DATA_SOURCES_INSTRUCTION
from opspilot.investigation.runner import ExecutorFactory
from opspilot.persistence import DurableStore, Lease
from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    Clock,
    EvidenceSink,
    QueryScope,
    ReadOnlyToolExecutor,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
)
from opspilot.tools.ledger import DurableToolLedger
from opspilot.tools.outcomes import PROJECTION_REVISION, Window
from opspilot.tools.registry import (
    MAX_RESULT_BYTES,
    MAX_VIEW_TOKENS,
    ParameterSpec,
    RegisteredTarget,
    TargetRegistry,
    ToolDescription,
    ToolRegistration,
    ToolRegistry,
    canonical,
    canonical_hash,
)
from opspilot.tools.tokens import TokenCounter, default_counter
from scripts.cloudopsbench_eval import catalog
from scripts.cloudopsbench_eval.mcp_client import McpClient, McpError

PROFILE_NAME = "cloudopsbench"
SYSTEM_ENV = "COB_EVAL_SYSTEM"
CATEGORY_ENV = "COB_EVAL_CATEGORY"
MCP_URL_ENV = "COB_EVAL_MCP_URL"
#: The extended profile's prompt variant: the Kubernetes investigation
#: guidance, unchanged (A-row: the product prompt is not changed).
DISCIPLINE_VARIANT = "replay-candidate-k8s"
SOURCE = "cloudopsbench"
CREDENTIAL_REF = "cloudopsbench-none"
#: The Run's one target is the identity file's target, as in every eval
#: profile; its source is this profile's.
TARGET_ID = otel_demo.TARGET_ID
#: The frame each Run is recorded with: the product's 24 h, unused by the
#: snapshot tools (they take no time argument).
FRAME_SECONDS = otel_demo.OBSERVATION_SECONDS
REQUEST_TIMEOUT_SECONDS = 30.0
ARGUMENT_ERROR_PREFIX = "Invalid tool arguments:"

NOT_FOUND_NOTE = (
    "This tool reads a recorded snapshot of the incident: a query the "
    "snapshot did not record returns Error from server (NotFound), so a "
    "NotFound answer does not prove the object is absent."
)
VIEW_NOTE = (
    "The result is the tool's text output, one row per line under lines. A "
    "result whose whole view exceeds 25,000 tokens is refused with "
    "RESULT_TOO_LARGE and no rows; it is never truncated."
)

_DESCRIPTION = ToolDescription(
    returns=(
        "The text the Cloud-OpsBench tool of the same name returns for this "
        "case's recorded snapshot, one row per output line."
    ),
    window_format=(
        "The snapshot has no time argument; the Run's authorized window is "
        "appended as {window} and does not select data."
    ),
    values_format=(
        "The benchmark's own argument values; allowed names are stated in the "
        "argument descriptions, appended as {values}."
    ),
    limits=(
        "The whole view is limited to the registered max_view_tokens; a larger "
        "result is refused with RESULT_TOO_LARGE, never truncated."
    ),
    cannot_prove=(
        "A NotFound or empty answer can mean the query was not recorded in the "
        "snapshot; it does not prove the object or condition is absent."
    ),
)
_ERROR_CLASSES = {"INVALID_ARGUMENTS": "INVALID_PARAMS", "TOOL_ERROR": "SOURCE_ERROR"}


def _kind(spec: Mapping[str, Any]) -> str:
    """The JSON type of one pydantic property (``Optional[X]`` is X)."""
    if isinstance(spec.get("type"), str):
        return str(spec["type"])
    for option in spec.get("anyOf") or ():
        if isinstance(option, Mapping) and option.get("type") not in (None, "null"):
            return str(option["type"])
    raise ValueError("SCHEMA_TYPE_UNKNOWN")


def face_schema(tool: Mapping[str, Any]) -> dict[str, Any]:
    """One model-visible function: the registered name, the benchmark's
    description (naming the benchmark tool) and its arguments -- the same
    names, types, enums, descriptions and required set."""
    schema = tool["inputSchema"]
    properties: dict[str, Any] = {}
    for name, spec in (schema.get("properties") or {}).items():
        item: dict[str, Any] = {"type": _kind(spec)}
        if "enum" in spec:
            item["enum"] = list(spec["enum"])
        if spec.get("description"):
            item["description"] = spec["description"]
        properties[name] = item
    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if schema.get("required"):
        parameters["required"] = list(schema["required"])
    bench = tool["name"]
    return {
        "type": "function",
        "function": {
            "name": catalog.TOOL_NAMES[bench],
            "description": (
                f"Cloud-OpsBench tool {bench}. {tool['description']} "
                f"{NOT_FOUND_NOTE} {VIEW_NOTE}"
            ),
            "parameters": parameters,
        },
    }


def registration(tool: Mapping[str, Any]) -> ToolRegistration:
    schema = tool["inputSchema"]
    required = set(schema.get("required") or ())
    return ToolRegistration(
        name=catalog.TOOL_NAMES[tool["name"]],
        version="v1",
        source=SOURCE,
        verb="query",
        description=_DESCRIPTION,
        # Snapshots hold Secret and ConfigMap YAML: the shared credential
        # redaction applies to every view.
        may_contain_secrets=True,
        parameters={
            name: ParameterSpec(
                _kind(spec),
                required=name in required,
                description=str(spec.get("description") or ""),
            )
            for name, spec in (schema.get("properties") or {}).items()
        },
        result_path=("lines",),
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        max_result_bytes=MAX_RESULT_BYTES,
        max_view_tokens=MAX_VIEW_TOKENS,
        max_window_seconds=FRAME_SECONDS,
        error_classes=dict(_ERROR_CLASSES),
    )


def tool_schemas(system: str, category: str) -> tuple[dict[str, Any], ...]:
    return tuple(face_schema(t) for t in catalog.benchmark_tools(system, category))


def registrations(system: str, category: str) -> tuple[ToolRegistration, ...]:
    return tuple(registration(t) for t in catalog.benchmark_tools(system, category))


def data_sources(system: str, category: str) -> dict[str, Any]:
    return {
        "available": {
            "cloudopsbench_snapshot": sorted(
                s["function"]["name"] for s in tool_schemas(system, category)
            )
        },
        "not_integrated": ["metric_queries", "traces", "change_history"],
        "instruction": DATA_SOURCES_INSTRUCTION,
    }


def tool_schema_revision(system: str, category: str) -> str:
    digest = canonical_hash(
        {
            "tool_schemas": list(tool_schemas(system, category)),
            "registry": ToolRegistry(registrations(system, category)).revision,
            "projection_revision": PROJECTION_REVISION,
            "data_sources": data_sources(system, category),
        }
    )
    return f"{PROFILE_NAME}-{digest[:12]}"


def versions(system: str, category: str) -> dict[str, str]:
    return {
        **investigation_versions(DISCIPLINE_VARIANT),
        "tool_schema_revision": tool_schema_revision(system, category),
    }


def face(clock: Clock, system: str, category: str) -> ToolFace:
    def evidence_context(run_id: str) -> Mapping[str, Any]:
        end = clock.now().astimezone(timezone.utc).replace(microsecond=0)
        window = Window(end - timedelta(seconds=FRAME_SECONDS), end)
        context = dict(otel_demo._evidence_context(run_id, window))
        context["data_sources"] = data_sources(system, category)
        return context

    return ToolFace(
        tool_schemas=tool_schemas(system, category),
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=evidence_context,
    )


class CobTransport:
    """``ReadOnlyTransport`` over the case's MCP server."""

    def __init__(self, client: McpClient) -> None:
        self._client = client

    def fetch(self, request: TransportRequest) -> TransportResponse:
        bench = catalog.BENCHMARK_NAMES.get(request.tool)
        if bench is None:
            return TransportResponse(body=b"{}", source_status="TOOL_ERROR", sent=False)
        try:
            text, is_error = self._client.call_tool(
                bench, dict(request.params), timeout=request.timeout_seconds
            )
        except McpError as error:
            if error.code == "MCP_TIMEOUT":
                raise TransportTimeout(error.code) from None
            raise TransportUnavailable(error.code) from None
        if is_error:
            # The server's adapter error (arguments it refused before the
            # backend ran, or a backend exception); its text is the note.
            status = (
                "INVALID_ARGUMENTS"
                if text.startswith(ARGUMENT_ERROR_PREFIX)
                else "TOOL_ERROR"
            )
            return TransportResponse(
                body=canonical({"error": status}).encode("utf-8"),
                source_status=status,
                view_fields={"notes": [text[:1000]]},
            )
        lines = text.splitlines()
        body = canonical({"tool": bench, "lines": lines}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body, row_unit="lines", backend_rows_returned=len(lines)
        )


def _overflow_summary(
    tool: str, rows: object, query: Mapping[str, object]
) -> dict[str, object]:
    return {"row_unit": "lines"}


def target(mcp_url: str) -> RegisteredTarget:
    return RegisteredTarget(
        target_id=TARGET_ID,
        source=SOURCE,
        endpoint=mcp_url,
        credential_ref=CREDENTIAL_REF,
        selector={"opspilot.integration.id": TARGET_ID},
        display_name="Cloud-OpsBench case snapshot",
    )


def executor_factory(
    store: DurableStore,
    *,
    evidence: EvidenceSink,
    clock: Clock,
    system: str,
    category: str,
    mcp_url: str,
    token_counter: TokenCounter | None = None,
    client: McpClient | None = None,
) -> ExecutorFactory:
    tools = ToolRegistry(registrations(system, category))
    targets = TargetRegistry([target(mcp_url)])
    control = otel_demo.DurableControl(store)
    transport = CobTransport(client or McpClient(mcp_url))
    counter = token_counter if token_counter is not None else default_counter()
    names = frozenset(r.name for r in registrations(system, category))

    def factory(lease: Lease, input: Any) -> ReadOnlyToolExecutor:
        run = store.rebuild(lease.incident_id)["run"]
        scope = QueryScope(
            scope_id=f"{PROFILE_NAME}:{lease.run_id}:{lease.epoch}",
            subject_kind="incident",
            subject_id=str(lease.incident_id),
            run_id=str(lease.run_id),
            control_generation=lease.control_generation,
            global_suspension_generation=lease.global_suspension_generation,
            target_suspension_generation=lease.target_suspension_generation,
            registry_revision=targets.revision,
            tool_registry_revision=tools.revision,
            target_ids=frozenset({TARGET_ID}),
            tool_names=names,
            window=otel_demo.scope_window(input),
            default_query_window=otel_demo.scope_default_query_window(input),
            deadline=run["deadline"],
        )
        return ReadOnlyToolExecutor(
            scope=scope,
            tools=tools,
            targets=targets,
            transport=transport,
            evidence=evidence,
            control=control,
            clock=clock,
            ledger=DurableToolLedger(store, lease),
            token_counter=counter,
            overflow_summary=_overflow_summary,
        )

    return factory


def from_env(env: Mapping[str, str], *, worker: bool) -> Any:
    """The ``ToolProfile`` the product entry hands to the web or worker."""
    from opspilot.tools.profiles import ToolProfile

    system = env.get(SYSTEM_ENV, "")
    category = env.get(CATEGORY_ENV, "")
    if system not in ("boutique", "train-ticket") or not category:
        raise SystemExit("COB_EVAL_CONFIG_INVALID")
    url = env.get(MCP_URL_ENV, "")
    if worker and not url:
        raise SystemExit("COB_EVAL_MCP_URL_MISSING")

    def factory(store: DurableStore, evidence: EvidenceSink, clock: Clock) -> Any:
        return executor_factory(
            store,
            evidence=evidence,
            clock=clock,
            system=system,
            category=category,
            mcp_url=url,
        )

    return ToolProfile(
        name=PROFILE_NAME,
        face=lambda clock: face(clock, system, category),
        versions=lambda: versions(system, category),
        executor_factory=factory,
    )
