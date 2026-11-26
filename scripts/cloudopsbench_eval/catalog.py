"""The benchmark's closed lists and tool catalog, pinned (#871 U1, U2).

``VALID_*`` and ``ROOT_CAUSE_LIST`` are List B and List A of the bundled
agent's prompt, copied verbatim from Cloud-OpsBench ec1d4f3
``agents/cloudops_agent/runtime/contracts.py`` (MIT). ``tool_catalog.json``
is the benchmark MCP server's ``tools/list`` for the three tool sets it
serves (Boutique, Boutique code-defect -- whose ``GetResources`` has
``output_yaml`` in place of ``label_selector`` -- and TrainTicket), captured
from the server itself; the driver re-lists the tools of every case's server
and refuses a case whose tools differ (``TOOLS_MISMATCH``).

Tool names: the product's tool registry accepts lower-case names only
(``opspilot/tools/registry.py`` ``_NAME``), so each benchmark tool is
registered under a fixed snake-case name (``GetResources`` ->
``get_resources``) with the same arguments, and the face's description names
the benchmark tool. :data:`TOOL_NAMES` is the one mapping both ways.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

CATALOG_FILE = Path(__file__).with_name("tool_catalog.json")
CODE_DEFECT_CATEGORIES = frozenset({"codedefect", "code_defect", "code-defect"})

#: Benchmark tool name -> registered (model-visible) name.
TOOL_NAMES: Mapping[str, str] = {
    "GetResources": "get_resources",
    "DescribeResource": "describe_resource",
    "GetAppYAML": "get_app_yaml",
    "GetRecentLogs": "get_recent_logs",
    "GetErrorLogs": "get_error_logs",
    "CheckServiceConnectivity": "check_service_connectivity",
    "GetServiceDependencies": "get_service_dependencies",
    "GetClusterConfiguration": "get_cluster_configuration",
    "GetAlerts": "get_alerts",
    "CheckNodeServiceStatus": "check_node_service_status",
    "ListCodeFiles": "list_code_files",
    "GetSourceCode": "get_source_code",
}
BENCHMARK_NAMES: Mapping[str, str] = {v: k for k, v in TOOL_NAMES.items()}

VALID_NODES: tuple[str, ...] = ("master", "worker-01", "worker-02", "worker-03")
VALID_SERVICES: dict[str, tuple[str, ...]] = {
    "boutique": (
        "frontend",
        "cartservice",
        "productcatalogservice",
        "currencyservice",
        "paymentservice",
        "shippingservice",
        "emailservice",
        "checkoutservice",
        "recommendationservice",
        "adservice",
        "redis-cart",
    ),
    "train-ticket": (
        "ts-assurance-service",
        "ts-auth-service",
        "ts-avatar-service",
        "ts-basic-service",
        "ts-cancel-service",
        "ts-config-service",
        "ts-consign-price-service",
        "ts-consign-service",
        "ts-contacts-service",
        "ts-delivery-service",
        "ts-execute-service",
        "ts-food-delivery-service",
        "ts-food-service",
        "ts-gateway-service",
        "ts-inside-payment-service",
        "ts-news-service",
        "ts-notification-service",
        "ts-order-other-service",
        "ts-order-service",
        "ts-payment-service",
        "ts-preserve-other-service",
        "ts-preserve-service",
        "ts-price-service",
        "ts-rebook-service",
        "ts-route-plan-service",
        "ts-route-service",
        "ts-seat-service",
        "ts-security-service",
        "ts-station-food-service",
        "ts-station-service",
        "ts-ticket-office-service",
        "ts-train-food-service",
        "ts-train-service",
        "ts-travel-plan-service",
        "ts-travel-service",
        "ts-travel2-service",
        "ts-ui-dashboard",
        "ts-user-service",
        "ts-verification-code-service",
        "ts-voucher-service",
        "ts-wait-order-service",
        "tsdb-mysql",
    ),
}
VALID_NAMESPACES: dict[str, tuple[str, ...]] = {
    "boutique": ("boutique",),
    "train-ticket": ("train-ticket",),
}
ROOT_CAUSE_LIST = """
- namespace_cpu_quota_exceeded (Requires Target: NAMESPACE): CPU resource quota exceeded
- namespace_memory_quota_exceeded (Requires Target: NAMESPACE): memory resource quota exceeded
- namespace_pod_quota_exceeded (Requires Target: NAMESPACE): Pod count quota exceeded
- namespace_service_quota_exceeded (Requires Target: NAMESPACE): Service count quota exceeded
- namespace_storage_quota_exceeded (Requires Target: NAMESPACE): storage resource quota exceeded
- missing_service_account (Requires Target: APP): missing ServiceAccount
- node_cordon_mismatch (Requires Target: APP): Pod cannot be scheduled because the node is cordoned
- node_affinity_mismatch (Requires Target: APP): node affinity configuration mismatch
- node_selector_mismatch (Requires Target: APP): node selector mismatch
- pod_anti_affinity_conflict (Requires Target: APP): Pod anti-affinity rule conflict
- taint_toleration_mismatch (Requires Target: APP): taint and toleration mismatch
- cpu_capacity_mismatch (Requires Target: APP): insufficient node CPU capacity
- memory_capacity_mismatch (Requires Target: APP): insufficient node memory capacity
- node_network_delay (Requires Target: NODE): excessive node network latency
- node_network_packet_loss (Requires Target: NODE): node network packet loss
- containerd_unavailable (Requires Target: NODE): containerd unavailable
- kubelet_unavailable (Requires Target: NODE): kubelet unavailable
- kube_proxy_unavailable (Requires Target: NODE): kube-proxy unavailable
- kube_scheduler_unavailable (Requires Target: NODE): kube-scheduler unavailable
- image_registry_dns_failure (Requires Target: APP): image registry DNS resolution failure
- incorrect_image_reference (Requires Target: APP): incorrect image reference
- missing_image_pull_secret (Requires Target: APP): missing image pull secret
- pvc_selector_mismatch (Requires Target: APP): PVC selector mismatch
- pvc_storage_class_mismatch (Requires Target: APP): PVC storage class mismatch
- pvc_access_mode_mismatch (Requires Target: APP): PVC access mode mismatch
- pvc_capacity_mismatch (Requires Target: APP): PVC capacity mismatch
- pv_binding_occupied (Requires Target: APP): PV binding already occupied
- volume_mount_permission_denied (Requires Target: APP): volume mount permission denied
- container_memory_limit_too_low (Requires Target: APP): process killed due to low memory configuration
- liveness_probe_incorrect_protocol (Requires Target: APP): incorrect liveness probe protocol
- liveness_probe_incorrect_port (Requires Target: APP): incorrect liveness probe port
- liveness_probe_incorrect_timing (Requires Target: APP): incorrect liveness probe timing configuration
- readiness_probe_incorrect_protocol (Requires Target: APP): incorrect readiness probe protocol
- readiness_probe_incorrect_port (Requires Target: APP): incorrect readiness probe port
- service_selector_mismatch (Requires Target: APP): Service selector mismatch
- service_port_mapping_mismatch (Requires Target: APP): incorrect Service port mapping
- service_protocol_mismatch (Requires Target: APP): incorrect Service protocol configuration
- service_env_var_address_mismatch (Requires Target: APP): incorrect service address environment variable configuration
- pod_cpu_overload (Requires Target: APP): excessive Pod CPU load
- pod_network_delay (Requires Target: APP): excessive Pod network latency
- service_sidecar_port_conflict (Requires Target: APP): sidecar port conflict
- service_dns_resolution_failure (Requires Target: APP): service DNS resolution failure
- mysql_invalid_credentials (Requires Target: APP): invalid MySQL credentials
- mysql_invalid_port (Requires Target: APP): incorrect MySQL port
- missing_secret_binding (Requires Target: APP): missing Secret binding
- db_connection_exhaustion (Requires Target: APP): database connections exhausted
- db_readonly_mode (Requires Target: APP): database in read-only mode
- gateway_misrouted (Requires Target: APP): incorrect gateway routing
- deployment_zero_replicas (Requires Target: APP): Deployment replica count is 0
- code_busy_loop (Requires Target: APP): application code contains a CPU-intensive busy loop
- code_memory_leak (Requires Target: APP): application code leaks memory or retains data unexpectedly
- code_artificial_delay (Requires Target: APP): application code introduces artificial latency or blocking delay
- code_excessive_file_reads (Requires Target: APP): application code performs excessive file reads
- code_excessive_file_writes (Requires Target: APP): application code performs excessive file writes
- code_wrong_return (Requires Target: APP): application code returns an incorrect value or wrong response
- code_missing_parameter (Requires Target: APP): application code omits a required parameter in a call/request
- code_wrong_argument_order (Requires Target: APP): application code passes arguments in the wrong order
"""

#: The 57 labels of List A, in its order.
LABELS: tuple[str, ...] = tuple(
    re.findall(r"^- ([a-z0-9_]+) \(Requires Target", ROOT_CAUSE_LIST, re.MULTILINE)
)
#: Label -> ``APP`` / ``NODE`` / ``NAMESPACE``, as tagged in List A.
LABEL_TARGETS: Mapping[str, str] = dict(
    re.findall(
        r"^- ([a-z0-9_]+) \(Requires Target: ([A-Z]+)\)", ROOT_CAUSE_LIST, re.MULTILINE
    )
)


def variant(system: str, category: str) -> str:
    """The tool set the server selects: ``boutique``, ``boutique-codedefect``
    or ``train-ticket`` (``tools/cloudops.py`` ``use_get_resources_v2``)."""
    if system == "train-ticket":
        return "train-ticket"
    if system != "boutique":
        raise ValueError("SYSTEM_INVALID")
    if category.strip().lower() in CODE_DEFECT_CATEGORIES:
        return "boutique-codedefect"
    return "boutique"


@cache
def _catalog() -> Mapping[str, Any]:
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def benchmark_tools(system: str, category: str) -> list[dict[str, Any]]:
    """The pinned ``tools/list`` entries (name, description, inputSchema)."""
    return [dict(t) for t in _catalog()["variants"][variant(system, category)]]


def tools_match(
    listed: Sequence[Mapping[str, Any]], system: str, category: str
) -> bool:
    """Whether a server's ``tools/list`` is the pinned set: same names, order,
    descriptions and argument schemas."""
    pinned = benchmark_tools(system, category)
    seen = [
        {
            "name": t.get("name"),
            "description": t.get("description"),
            "inputSchema": t.get("inputSchema"),
        }
        for t in listed
    ]
    return seen == pinned
