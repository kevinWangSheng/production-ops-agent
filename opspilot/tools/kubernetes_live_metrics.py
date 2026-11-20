"""Live Pod and node usage: ``kubectl_top_pods`` and ``kubectl_top_nodes`` (B3, #562).

The upstream HolmesGPT ``kubernetes/live-metrics`` tools of the same names
(``kubernetes.yaml`` at ``a0009195``), served by the A2 client
(:class:`opspilot.tools.kubernetes_core.KubernetesClient`) as fixed GETs of
the ``metrics.k8s.io`` API instead of a command-line client. The public
interface is ``docs/tasks/2026-11-19-tool-expansion-b3.md``; the decisions
are B3-U1=a and B3-1 to B3-16:

* GET only, fixed paths (B3-4): ``/apis/metrics.k8s.io/v1beta1/pods`` or
  ``.../namespaces/{namespace}/pods``, ``/apis/metrics.k8s.io/v1beta1/nodes``
  and, for the node percentages, ``/api/v1/nodes`` read in pages of 500. One
  call reads at most 4 MiB in all (``RESULT_TOO_LARGE`` past it, the whole
  call). ``kubectl_top_pods`` takes an optional ``namespace`` (a DNS label;
  omitted is the whole cluster); ``kubectl_top_nodes`` takes nothing.
* Units (B3-6, kubectl's): quantities parse with the Kubernetes quantity
  grammar; CPU becomes millicores and memory bytes, each rounded up to an
  integer. A Pod's totals are the exact sum of its containers, rounded once.
  Node percentages are usage x 100 / allocatable, rounded down. Anything
  that does not parse is ``MALFORMED_RESULT`` for the whole call.
* Time (B3-U1=a, C3 section 8 current state): no source time and no
  ``data_as_of``; every row has its sample ``timestamp`` and ``window``, the
  view ``sampled_at`` and a first note on how far the samples are from the
  end of the authorized window.
* Errors (B3-8): this module's own table -- 404 (no ``metrics.k8s.io``) is
  ``SOURCE_ERROR`` with a note, unlike A2's 404.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
)
from opspilot.tools.kubernetes_core import (
    ENDPOINT_SELECTOR,
    MAX_SOURCE_READ_BYTES,
    PAGE_LIMIT,
    KubernetesClient,
    check_namespace,
    refused,
)
from opspilot.tools.kubernetes_core import _monotonic as _monotonic
from opspilot.tools.kubernetes_core import _page as _page
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "ENV",
    "LiveMetricsTransport",
    "NODES_TOOL",
    "PODS_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "live_metrics_enabled",
    "live_metrics_registrations",
    "parse_cpu_millicores",
    "parse_memory_bytes",
    "parse_quantity",
]

PODS_TOOL = "kubectl_top_pods"
NODES_TOOL = "kubectl_top_nodes"
TOOL_NAMES: tuple[str, ...] = (PODS_TOOL, NODES_TOOL)

#: B3-1: the switch, read once at startup by both processes.
ENV = "OPSPILOT_K8S_LIVE_METRICS"
INVALID_CODE = "K8S_LIVE_METRICS_INVALID"

METRICS_BASE = "/apis/metrics.k8s.io/v1beta1"
NODES_PATH = "/api/v1/nodes"

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 2 * 1024 * 1024


def live_metrics_enabled(env: Mapping[str, str]) -> bool:
    """``1`` is on; unset, empty or ``0`` off; anything else is a startup
    error (``K8S_LIVE_METRICS_INVALID``) so a misspelt value never silently
    drops the tools."""
    value = env.get(ENV)
    if value is None or value in ("", "0"):
        return False
    if value == "1":
        return True
    raise SystemExit(INVALID_CODE)


# -- quantities (B3-6) ---------------------------------------------------------

_QUANTITY = re.compile(
    r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))"
    r"(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E|[eE][+-]?[0-9]+)?"
)
_SUFFIXES: Mapping[str, Decimal] = {
    "": Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    "Ki": Decimal(2**10),
    "Mi": Decimal(2**20),
    "Gi": Decimal(2**30),
    "Ti": Decimal(2**40),
    "Pi": Decimal(2**50),
    "Ei": Decimal(2**60),
}
_MAX_QUANTITY_CHARS = 64


def parse_quantity(value: object) -> Decimal | None:
    """The exact value of a Kubernetes quantity string, or ``None``."""
    if not isinstance(value, str) or len(value) > _MAX_QUANTITY_CHARS:
        return None
    match = _QUANTITY.fullmatch(value)
    if match is None:
        return None
    number, suffix = match.group(1), match.group(2) or ""
    try:
        amount = Decimal(number)
        if len(suffix) > 1 and suffix[0] in ("e", "E"):
            exponent = int(suffix[1:])
            if abs(exponent) > 40:
                return None
            return amount.scaleb(exponent)
        return amount * _SUFFIXES[suffix]
    except (InvalidOperation, ValueError):
        return None


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def parse_cpu_millicores(value: object) -> int | None:
    """CPU cores as millicores, rounded up (``Quantity.MilliValue()``)."""
    amount = parse_quantity(value)
    return None if amount is None or amount < 0 else _ceil(amount * 1000)


def parse_memory_bytes(value: object) -> int | None:
    """Memory as bytes, rounded up (``Quantity.Value()``)."""
    amount = parse_quantity(value)
    return None if amount is None or amount < 0 else _ceil(amount)


# -- parsing the metrics lists ------------------------------------------------


class _Malformed(Exception):
    pass


def _usage(entry: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    usage = entry.get("usage")
    if not isinstance(usage, dict):
        raise _Malformed
    cpu = parse_quantity(usage.get("cpu"))
    memory = parse_quantity(usage.get("memory"))
    if cpu is None or memory is None or cpu < 0 or memory < 0:
        raise _Malformed
    return cpu, memory


def _timestamp(item: Mapping[str, Any]) -> datetime:
    raw = item.get("timestamp")
    if not isinstance(raw, str) or not raw or len(raw) > 64:
        raise _Malformed
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise _Malformed from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise _Malformed
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def _window(item: Mapping[str, Any]) -> str:
    raw = item.get("window")
    if not isinstance(raw, str) or not raw or len(raw) > 64:
        raise _Malformed
    return raw


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _name(metadata: object, key: str = "name") -> str:
    if not isinstance(metadata, dict):
        raise _Malformed
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise _Malformed
    return value


def _items(body: bytes, kind: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Malformed from None
    if not isinstance(payload, dict):
        raise _Malformed
    if payload.get("kind") not in (None, kind):
        raise _Malformed
    items = payload.get("items")
    if items is None:
        return []
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise _Malformed
    return items


def _pod_rows(
    items: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[datetime]]:
    rows: list[dict[str, Any]] = []
    stamps: list[datetime] = []
    for item in items:
        metadata = item.get("metadata")
        namespace = _name(metadata, "namespace")
        pod = _name(metadata)
        stamp = _timestamp(item)
        window = _window(item)
        containers = item.get("containers")
        if containers is None:
            containers = []
        if not isinstance(containers, list):
            raise _Malformed
        total_cpu = Decimal(0)
        total_memory = Decimal(0)
        shown: list[dict[str, Any]] = []
        for container in containers:
            if not isinstance(container, dict):
                raise _Malformed
            name = container.get("name")
            if not isinstance(name, str) or not name:
                raise _Malformed
            cpu, memory = _usage(container)
            total_cpu += cpu
            total_memory += memory
            shown.append(
                {
                    "name": name,
                    "cpu_millicores": _ceil(cpu * 1000),
                    "memory_bytes": _ceil(memory),
                }
            )
        shown.sort(key=lambda entry: str(entry["name"]))
        rows.append(
            {
                "namespace": namespace,
                "pod": pod,
                "cpu_millicores": _ceil(total_cpu * 1000),
                "memory_bytes": _ceil(total_memory),
                "timestamp": _iso(stamp),
                "window": window,
                "containers": shown,
            }
        )
        stamps.append(stamp)
    rows.sort(key=lambda row: (str(row["namespace"]), str(row["pod"])))
    return rows, stamps


def _allocatable(
    items: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[Decimal | None, Decimal | None]]:
    table: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    for item in items:
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            continue
        status = item.get("status")
        allocatable = status.get("allocatable") if isinstance(status, dict) else None
        if not isinstance(allocatable, dict):
            table[name] = (None, None)
            continue
        table[name] = (
            parse_quantity(allocatable.get("cpu")),
            parse_quantity(allocatable.get("memory")),
        )
    return table


def _percent(usage: int, capacity: int) -> int | None:
    if capacity <= 0:
        return None
    return usage * 100 // capacity


# -- transport ----------------------------------------------------------------


class LiveMetricsTransport:
    """Serves ``kubectl_top_pods`` and ``kubectl_top_nodes`` over the A2
    client (GET only)."""

    def __init__(
        self,
        *,
        client: KubernetesClient,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._monotonic = monotonic or _monotonic

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        deadline = self._monotonic() + request.timeout_seconds
        namespace: str | None = None
        if request.tool == PODS_TOOL:
            raw = request.params.get("namespace")
            if raw is not None:
                if not check_namespace(raw):
                    return refused("INVALID_NAMESPACE")
                assert isinstance(raw, str)
                namespace = raw
        self._client.token()  # a missing slot fails before anything is sent
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("K8S_ENDPOINT_MISSING")
        base = endpoint.rstrip("/")
        if request.tool == PODS_TOOL:
            path = (
                f"{METRICS_BASE}/pods"
                if namespace is None
                else f"{METRICS_BASE}/namespaces/{namespace}/pods"
            )
        else:
            path = f"{METRICS_BASE}/nodes"
        fetched = self._client.get(
            base + path, deadline=deadline, max_bytes=MAX_SOURCE_READ_BYTES
        )
        if fetched.status != 200:
            return _source_error(fetched.status, fetched.body)
        used = len(fetched.body)
        try:
            if request.tool == PODS_TOOL:
                items = _items(fetched.body, "PodMetricsList")
                rows, stamps = _pod_rows(items)
                notes: list[str] = []
                extra: dict[str, object] = {
                    "scope": {
                        "namespace": namespace,
                        "all_namespaces": namespace is None,
                    }
                }
                unit = "pods"
            else:
                items = _items(fetched.body, "NodeMetricsList")
                node_items: list[dict[str, Any]] = []
                if items:
                    listed = self._list_nodes(base, deadline, used)
                    if isinstance(listed, TransportResponse):
                        return listed
                    node_items = listed
                rows, stamps, notes = _node_rows(items, node_items)
                extra = {}
                unit = "nodes"
        except _Malformed:
            return TransportResponse(body=b"")
        if stamps:
            earliest, latest = min(stamps), max(stamps)
            notes.insert(0, _lateness_note(earliest, latest, request.window.end))
            sampled: dict[str, str] | None = {
                "earliest": _iso(earliest),
                "latest": _iso(latest),
            }
        else:
            sampled = None
        body = canonical({"results": rows}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            row_unit=unit,
            backend_rows_returned=len(rows),
            view_fields={
                **extra,
                "sampled_at": sampled,
                "notes": notes,
                "cannot_prove": _CANNOT_PROVE,
            },
        )

    def _list_nodes(
        self, base: str, deadline: float, used: int
    ) -> list[dict[str, Any]] | TransportResponse:
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            query: list[tuple[str, str]] = [("limit", str(PAGE_LIMIT))]
            if token:
                query.append(("continue", token))
            budget = MAX_SOURCE_READ_BYTES - used
            if budget <= 0:
                raise TransportResultTooLarge(str(MAX_SOURCE_READ_BYTES))
            fetched = self._client.get(
                base + NODES_PATH + "?" + urllib.parse.urlencode(query),
                deadline=deadline,
                max_bytes=budget,
            )
            if fetched.status != 200:
                return _source_error(fetched.status, fetched.body)
            used += len(fetched.body)
            page = _page(fetched.body)
            if page is None:
                raise _Malformed
            page_items, token, _, _ = page
            items.extend(page_items)
            if not token:
                return items


def _node_rows(
    items: Sequence[Mapping[str, Any]], node_items: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[datetime], list[str]]:
    allocatable = _allocatable(node_items)
    rows: list[dict[str, Any]] = []
    stamps: list[datetime] = []
    missing_allocatable: list[str] = []
    for item in items:
        node = _name(item.get("metadata"))
        stamp = _timestamp(item)
        window = _window(item)
        cpu, memory = _usage(item)
        cpu_m = _ceil(cpu * 1000)
        memory_b = _ceil(memory)
        cap_cpu, cap_memory = allocatable.get(node, (None, None))
        cpu_percent = (
            None
            if cap_cpu is None or cap_cpu < 0
            else _percent(cpu_m, _ceil(cap_cpu * 1000))
        )
        memory_percent = (
            None
            if cap_memory is None or cap_memory < 0
            else _percent(memory_b, _ceil(cap_memory))
        )
        if cpu_percent is None or memory_percent is None:
            missing_allocatable.append(node)
        rows.append(
            {
                "node": node,
                "cpu_millicores": cpu_m,
                "cpu_percent": cpu_percent,
                "memory_bytes": memory_b,
                "memory_percent": memory_percent,
                "timestamp": _iso(stamp),
                "window": window,
            }
        )
        stamps.append(stamp)
    rows.sort(key=lambda row: str(row["node"]))
    notes: list[str] = []
    if missing_allocatable:
        notes.append(
            f"{len(missing_allocatable)} nodes have no usable allocatable CPU "
            "or memory in /api/v1/nodes; their percentage is null: "
            + ", ".join(sorted(missing_allocatable)[:20])
            + "."
        )
    sampled_names = {str(row["node"]) for row in rows}
    unsampled = sorted(name for name in allocatable if name not in sampled_names)
    if unsampled:
        notes.append(
            f"{len(unsampled)} nodes have no usage sample (listed in "
            "/api/v1/nodes but not in the metrics API)."
        )
    return rows, stamps, notes


def _lateness_note(earliest: datetime, latest: datetime, window_end: datetime) -> str:
    end = window_end.astimezone(timezone.utc).replace(microsecond=0)
    seconds = int((earliest - end).total_seconds())
    relation = f"{seconds} s after" if seconds >= 0 else f"{-seconds} s before"
    return (
        f"Sampled at {_iso(earliest)}–{_iso(latest)}: current usage, "
        f"{relation} the end of the authorized window ({_iso(end)})."
    )


_NO_METRICS_NOTE = (
    "This cluster does not serve the live usage API (metrics.k8s.io): "
    "metrics-server is not installed. Use Prometheus for usage instead."
)


def _source_error(status: int, body: bytes) -> TransportResponse:
    if status == 404:
        return TransportResponse(
            body=body,
            source_status="404",
            http_status=404,
            view_fields={"notes": [_NO_METRICS_NOTE]},
        )
    return TransportResponse(body=body, source_status=str(status), http_status=status)


# -- model-visible face -------------------------------------------------------

_SNAPSHOT = (
    "This is a current snapshot from metrics-server, not history: it cannot "
    "show a trend or what usage was during the incident window; use "
    "Prometheus for change over time. For requests and limits use "
    "kubernetes_tabular_query. CPU is in millicores and memory in bytes, "
    "each rounded up to an integer; every row has the sample timestamp and "
    "the sampling window. A cluster without metrics-server answers "
    "SOURCE_ERROR."
)

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": PODS_TOOL,
            "description": (
                "Current CPU and memory usage of Pods (and of each container "
                "in them), from the cluster's metrics API. Omit namespace for "
                "every namespace; give it to read one namespace. "
                + _SNAPSHOT
                + " A result over the size limit is refused with "
                "RESULT_TOO_LARGE; narrow it with namespace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Namespace to read; omit for the whole cluster.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": NODES_TOOL,
            "description": (
                "Current CPU and memory usage of every node, from the "
                "cluster's metrics API, with the percentage of the node's "
                "allocatable CPU and memory (rounded down; it can exceed "
                "100). " + _SNAPSHOT
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
)

_RETURNS_PODS = (
    "Rows under results, one per Pod the metrics API sampled, sorted by "
    "namespace and pod: namespace, pod, cpu_millicores, memory_bytes (the "
    "exact sum of the containers, rounded up once), timestamp (the sample "
    "time), window (the sampling window as reported) and containers (name, "
    "cpu_millicores, memory_bytes, sorted by name). The view adds scope "
    "(namespace, all_namespaces), sampled_at (earliest, latest), notes and "
    "cannot_prove."
)
_RETURNS_NODES = (
    "Rows under results, one per node the metrics API sampled, sorted by "
    "node: node, cpu_millicores, cpu_percent, memory_bytes, memory_percent "
    "(usage x 100 / allocatable from /api/v1/nodes, rounded down; null when "
    "allocatable is missing), timestamp and window. The view adds "
    "sampled_at (earliest, latest), notes and cannot_prove."
)
_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. Usage is read "
    "at query time whatever the window says; the first note states how far "
    "the samples are from the window's end."
)
_VALUES_FORMAT = (
    "The authorized target for this Run is appended as {values}. namespace, "
    "when given, is a DNS label; anything else is INVALID_PARAMS "
    "(INVALID_NAMESPACE) before anything is sent."
)
_LIMITS = (
    "One fixed GET of the metrics API (and, for nodes, pages of 500 of "
    "/api/v1/nodes); at most 4 MiB read in total, 2 MiB of rows and the "
    "registered max_view_tokens (25,000 DeepSeek tokens): anything larger is "
    "RESULT_TOO_LARGE for the whole call, never truncated. 30 s per call, no "
    "retry. HTTP 400 is INVALID_PARAMS, 401/403 denied/SOURCE_FORBIDDEN, 404 "
    "(no metrics API) SOURCE_ERROR, 429 and 5xx SOURCE_UNAVAILABLE; a "
    "quantity that does not parse is MALFORMED_RESULT."
)
_CANNOT_PROVE = (
    "This is usage at this moment, not usage during the incident window, "
    "and not a history (use Prometheus for change over time). A Pod missing "
    "from the result may have just started, ended or not been scraped yet; "
    "it is not proof the Pod is not running. High usage is not proof of "
    "throttling or OOM; requests and limits are in kubernetes_tabular_query."
)

_ERROR_CLASSES: Mapping[str, str] = {
    "400": "INVALID_PARAMS",
    "401": "SOURCE_FORBIDDEN",
    "403": "SOURCE_FORBIDDEN",
    "404": "SOURCE_ERROR",
    "429": "SOURCE_UNAVAILABLE",
    "503": "SOURCE_UNAVAILABLE",
    "INVALID_NAMESPACE": "INVALID_PARAMS",
}


def live_metrics_registrations(
    *, max_window_seconds: int
) -> tuple[ToolRegistration, ...]:
    """The two registrations (``source`` is the lab target's;
    ``may_contain_secrets=True`` like the other cluster tools)."""
    common: dict[str, Any] = {
        "version": "v1",
        "source": otel_demo.SOURCE,
        "verb": "list",
        "may_contain_secrets": True,
        "result_path": ("results",),
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_result_bytes": MAX_RESULT_BYTES,
        "max_view_tokens": MAX_VIEW_TOKENS,
        "max_window_seconds": max_window_seconds,
        "error_classes": dict(_ERROR_CLASSES),
    }
    pods = ToolRegistration(
        name=PODS_TOOL,
        description=ToolDescription(
            returns=_RETURNS_PODS,
            window_format=_WINDOW_FORMAT,
            values_format=_VALUES_FORMAT,
            limits=_LIMITS,
            cannot_prove=_CANNOT_PROVE,
        ),
        parameters={
            "namespace": ParameterSpec(
                "string", description="Namespace to read; omitted is the whole cluster."
            )
        },
        **common,
    )
    nodes = ToolRegistration(
        name=NODES_TOOL,
        description=ToolDescription(
            returns=_RETURNS_NODES,
            window_format=_WINDOW_FORMAT,
            values_format=_VALUES_FORMAT,
            limits=_LIMITS,
            cannot_prove=_CANNOT_PROVE,
        ),
        parameters={},
        **common,
    )
    return pods, nodes
