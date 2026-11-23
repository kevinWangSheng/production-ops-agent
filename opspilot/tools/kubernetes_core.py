"""Read-only cluster object queries: ``kubernetes_jq_query`` and ``kubernetes_count``.

A2 of the tool expansion (docs/tasks/2026-11-12-tool-expansion-a2.md,
decision table r2 A2-1 to A2-15). The two tools are the upstream HolmesGPT
``kubernetes/core`` tools of the same names and parameters, served by a
direct HTTPS GET to the API server (R1) instead of a command-line client pipe:

* ``kind`` is a plural resource name from the static table ``RESOURCES``
  (A2-5). Anything naming ``secret`` in any case or number is refused with
  ``RESOURCE_NOT_ALLOWED`` before anything is sent (A2-6, C3 section 8);
  any other name outside the table is ``INVALID_PARAMS``.
* ``namespace`` omitted is the whole cluster (R17, A2-7); given, it must be a
  DNS label, and a cluster-scoped kind ignores it with a note.
* Pages of 500 are read to the end and merged, with a 4 MiB cumulative
  read cap -- past it the whole call is ``RESULT_TOO_LARGE``, never a
  partial result (A2-9); ``metadata.managedFields`` is replaced in every
  object by ``metadata.managedFieldsSummary`` (manager, operation, time,
  subresource; no field tree; at most 20, newest kept, #696 B4-6). Events are kept only inside the authorized window, before
  jq runs (A2-11); the dropped count is in ``notes``.
* The model's jq expression runs once over the merged list in a separate
  process (A2-8): argument list without a shell, empty environment, an
  empty working and module directory, no ``import``/``include``, at most
  2000 characters, the call's remaining time, 2 MiB of output and 512 MiB
  of memory. A jq timeout is ``timeout``/``TOOL_TIMEOUT`` with confirmed
  source contact (the reads already happened), the same class a later
  bounded child process (A3, A6 regex) reports.

The API address travels in the target's selector (``kubernetes_endpoint``,
A2-1) and the token in this transport's own credential slot ``k8s-ro``; the
executor's ``credential_ref`` (the Prometheus slot) is never used here, and
neither backend falls back to the other (R3). Configuration is read only by
the worker (``KubernetesConfig.from_env``, A2-3).

The pieces A3 (Pod logs) and A6 (columns) reuse are plain functions and one
client class: ``resolve_kind``, ``check_namespace``, ``check_selector``,
``KubernetesClient`` (GET, pagination, the 4 MiB cap, status mapping and the
event window filter in ``list_objects``), ``run_isolated`` and
``ERROR_CLASSES``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from opspilot.redaction import redact_payload
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
)
from opspilot.tools.http_deadline import DeadlineHandlers, RequestSockets
from opspilot.tools.otel_demo import OBSERVATION_SECONDS, SOURCE
from opspilot.tools.outcomes import SourceContact, Window
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolContractError,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "API_URL_ENV",
    "CA_FILE_ENV",
    "COUNT_TOOL",
    "CREDENTIAL_REF",
    "DEFAULT_NAMESPACE",
    "ENDPOINT_SELECTOR",
    "ERROR_CLASSES",
    "JQ_QUERY_TOOL",
    "K8S_TOOLS",
    "NAMESPACE_ENV",
    "RESOURCES",
    "TOKEN_ENV",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "KubernetesClient",
    "KubernetesConfig",
    "KubernetesRequestRefused",
    "KubernetesTransport",
    "ListResult",
    "Resource",
    "LineFilter",
    "check_namespace",
    "check_object_name",
    "check_release_name",
    "check_selector",
    "filter_lines",
    "pattern_problem",
    "pod_url",
    "require_python",
    "kubernetes_registrations",
    "require_jq",
    "resolve_kind",
    "run_isolated",
    "secret_request_problem",
]

JQ_QUERY_TOOL = "kubernetes_jq_query"
COUNT_TOOL = "kubernetes_count"
TOOL_NAMES: tuple[str, ...] = (JQ_QUERY_TOOL, COUNT_TOOL)
K8S_TOOLS: frozenset[str] = frozenset(TOOL_NAMES)
#: A3's tool, served by ``KubernetesTransport`` but routed by exact name.
_POD_LOGS_TOOL = "fetch_pod_logs"
#: A6's tool, served the same way (``kubernetes_tabular.TabularQueryTransport``).
_TABULAR_TOOL = "kubernetes_tabular_query"
#: B3 (#562): served by ``kubernetes_live_metrics.LiveMetricsTransport``.
_LIVE_METRICS_TOOLS = frozenset({"kubectl_top_pods", "kubectl_top_nodes"})
#: B4 (#563): served by ``helm_releases.HelmReleasesTransport``.
_HELM_TOOLS = frozenset({"helm_list", "helm_history", "helm_status"})
#: C4 (#568): served by ``argocd_apps.ArgoCDTransport``.
_ARGOCD_TOOLS = frozenset(
    {"argocd_app_list", "argocd_app_get", "argocd_app_history", "argocd_app_resources"}
)

#: The Kubernetes transport's own credential slot (A2-1); the target's
#: ``credential_ref`` names the Prometheus slot and is not used here.
CREDENTIAL_REF = "k8s-ro"
#: The target selector key carrying the API server address (A2-1).
ENDPOINT_SELECTOR = "kubernetes_endpoint"
#: The selector key carrying the application namespace hint (A2-4).
NAMESPACE_SELECTOR = "kubernetes_namespace"

API_URL_ENV = "OPSPILOT_K8S_API_URL"
TOKEN_ENV = "OPSPILOT_K8S_TOKEN"
CA_FILE_ENV = "OPSPILOT_K8S_CA_FILE"
NAMESPACE_ENV = "OPSPILOT_K8S_NAMESPACE"
DEFAULT_NAMESPACE = "otel-demo"

#: R6 / A2-9: objects per list page.
PAGE_LIMIT = 500
#: A2-9: cumulative raw bytes read across all pages of one call.
MAX_SOURCE_READ_BYTES = 4 * 1024 * 1024
#: A2-8: jq limits.
MAX_JQ_EXPR_CHARS = 2000
MAX_JQ_OUTPUT_BYTES = 2 * 1024 * 1024
JQ_MEMORY_BYTES = 512 * 1024 * 1024
#: A2-10: the count tool's preview.
COUNT_PREVIEW_ITEMS = 10
COUNT_PREVIEW_CHARS = 200
#: Selector strings are copied into the query string; bounded and printable.
MAX_SELECTOR_CHARS = 1000
#: A non-2xx body is read only this far (same as the OTel Demo transport).
ERROR_BODY_BYTES = 8192
#: stderr of a bounded child is drained and discarded past this.
_STDERR_BYTES = 8192
#: One request's timeout (A2-9: paging and jq share 30 s).
REQUEST_TIMEOUT_SECONDS = 30.0
#: jq's exit status for a program that does not compile.
_JQ_COMPILE_STATUS = 3
_JQ_RUNTIME_STATUS = 5

_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
#: A3-6: an object name (a Pod's) is a DNS subdomain: dot-separated DNS
#: labels of at most 63 characters each, at most 253 characters in all.
MAX_OBJECT_NAME_CHARS = 253
_JQ_FORBIDDEN = ("import", "include")


# -- static resource table (A2-5) -------------------------------------------


@dataclass(frozen=True)
class Resource:
    """One listable resource: plural name, API group/version, scope."""

    plural: str
    group: str
    version: str
    namespaced: bool

    def path(self, namespace: str | None) -> str:
        base = (
            f"/api/{self.version}"
            if not self.group
            else f"/apis/{self.group}/{self.version}"
        )
        if self.namespaced and namespace is not None:
            base += f"/namespaces/{namespace}"
        return f"{base}/{self.plural}"

    def as_json(self) -> dict[str, object]:
        return {
            "group": self.group,
            "version": self.version,
            "namespaced": self.namespaced,
        }


def _table(*entries: tuple[str, str, str, bool]) -> Mapping[str, Resource]:
    return {
        plural: Resource(plural, group, version, ns)
        for plural, group, version, ns in entries
    }


#: A2-5: the upstream default ClusterRole's unconditional rules (A1-2),
#: narrowed to top-level resources that are served by a current API server
#: and that A2 lists: no subresources, no ``events.k8s.io`` (out of scope),
#: no ``metrics.k8s.io`` (live metrics, B3), no removed ``extensions`` group
#: or ``podsecuritypolicies``. A name served by several groups resolves to
#: the core group, else to the group that serves it today. No ``secrets``.
RESOURCES: Mapping[str, Resource] = _table(
    ("configmaps", "", "v1", True),
    ("endpoints", "", "v1", True),
    ("events", "", "v1", True),
    ("namespaces", "", "v1", False),
    ("nodes", "", "v1", False),
    ("persistentvolumeclaims", "", "v1", True),
    ("persistentvolumes", "", "v1", False),
    ("pods", "", "v1", True),
    ("replicationcontrollers", "", "v1", True),
    ("resourcequotas", "", "v1", True),
    ("serviceaccounts", "", "v1", True),
    ("services", "", "v1", True),
    ("daemonsets", "apps", "v1", True),
    ("deployments", "apps", "v1", True),
    ("replicasets", "apps", "v1", True),
    ("statefulsets", "apps", "v1", True),
    ("cronjobs", "batch", "v1", True),
    ("jobs", "batch", "v1", True),
    ("horizontalpodautoscalers", "autoscaling", "v2", True),
    ("storageclasses", "storage.k8s.io", "v1", False),
    ("ingresses", "networking.k8s.io", "v1", True),
    ("networkpolicies", "networking.k8s.io", "v1", True),
    ("poddisruptionbudgets", "policy", "v1", True),
    ("clusterrolebindings", "rbac.authorization.k8s.io", "v1", False),
    ("clusterroles", "rbac.authorization.k8s.io", "v1", False),
    ("rolebindings", "rbac.authorization.k8s.io", "v1", True),
    ("roles", "rbac.authorization.k8s.io", "v1", True),
    ("apiservices", "apiregistration.k8s.io", "v1", False),
    ("customresourcedefinitions", "apiextensions.k8s.io", "v1", False),
    ("mutatingwebhookconfigurations", "admissionregistration.k8s.io", "v1", False),
    ("validatingwebhookconfigurations", "admissionregistration.k8s.io", "v1", False),
)


def resolve_kind(kind: object) -> tuple[Resource | None, str | None]:
    """The table entry for ``kind``, or the fixed refusal code.

    Secrets first (A2-6): any string naming ``secret`` -- ``Secret``,
    ``SECRETS``, ``secrets.v1`` -- is ``RESOURCE_NOT_ALLOWED`` whatever else
    it says. Otherwise the plural name must match a table key exactly.
    """
    if not isinstance(kind, str):
        return None, "UNKNOWN_KIND"
    if "secret" in kind.lower():
        return None, "RESOURCE_NOT_ALLOWED"
    resource = RESOURCES.get(kind)
    if resource is None:
        return None, "UNKNOWN_KIND"
    return resource, None


def check_namespace(namespace: object) -> bool:
    """A2-7: a namespace is a DNS label (it is spliced into the path)."""
    return isinstance(namespace, str) and _DNS_LABEL.fullmatch(namespace) is not None


def check_object_name(name: object) -> bool:
    """A3-6: an object name is a DNS subdomain (it is spliced into the path):
    every dot-separated part a DNS label (at most 63 characters), at most
    253 characters in all."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_OBJECT_NAME_CHARS
        and all(_DNS_LABEL.fullmatch(part) is not None for part in name.split("."))
    )


def pod_url(endpoint: str, namespace: str, pod_name: str, *, log: bool = False) -> str:
    """The URL of one Pod, or of its ``log`` subresource (A3-2).

    ``namespace`` and ``pod_name`` must already have passed
    ``check_namespace`` and ``check_object_name``; they are still
    percent-encoded so nothing in them can change the path.
    """
    base = endpoint.rstrip("/") + RESOURCES["pods"].path(
        urllib.parse.quote(namespace, safe="")
    )
    url = f"{base}/{urllib.parse.quote(pod_name, safe='')}"
    return url + "/log" if log else url


def check_selector(value: object) -> bool:
    """A label or field selector: a printable string, bounded in length."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_SELECTOR_CHARS
        and value.isprintable()
    )


# -- error classes (A2-6, A2-12) ---------------------------------------------

#: Every fixed code this module reports as ``source_status``. HTTP statuses
#: are the API server's (A2-12); 5xx raise ``TransportUnavailable`` instead.
ERROR_CLASSES: Mapping[str, str] = {
    "400": "INVALID_PARAMS",
    "404": "INVALID_PARAMS",
    "422": "INVALID_PARAMS",
    "401": "SOURCE_FORBIDDEN",
    "403": "SOURCE_FORBIDDEN",
    "410": "SOURCE_ERROR",
    "429": "SOURCE_UNAVAILABLE",
    "503": "SOURCE_UNAVAILABLE",
    "RESOURCE_NOT_ALLOWED": "RESOURCE_NOT_ALLOWED",
    "UNKNOWN_KIND": "INVALID_PARAMS",
    "INVALID_NAMESPACE": "INVALID_PARAMS",
    "INVALID_SELECTOR": "INVALID_PARAMS",
    "INVALID_JQ": "INVALID_PARAMS",
    "JQ_COMPILE_ERROR": "INVALID_PARAMS",
    "JQ_RUNTIME_ERROR": "INVALID_PARAMS",
    "JQ_FAILED": "SOURCE_ERROR",
}


def refused(code: str) -> TransportResponse:
    """A fixed-code refusal decided before anything was sent to the cluster."""
    return TransportResponse(
        body=canonical({"error": code}).encode(), source_status=code, sent=False
    )


def _answered(code: str) -> TransportResponse:
    """A fixed-code failure after the cluster was read (jq failed)."""
    return TransportResponse(
        body=canonical({"error": code}).encode(), source_status=code
    )


# -- configuration -------------------------------------------------------------


def _clean_api_url(value: str) -> str:
    parts = urllib.parse.urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or (parts.path not in ("", "/"))
    ):
        raise ToolContractError("K8S_CONFIG_INVALID")
    try:
        parts.port
    except ValueError:
        raise ToolContractError("K8S_CONFIG_INVALID") from None
    return value.rstrip("/")


@dataclass(frozen=True)
class KubernetesConfig:
    """The worker's cluster connection: API address, token, CA, namespace hint.

    Read only by the worker when it builds the executor (A2-3); the web never
    constructs one. ``token`` is the value behind ``CREDENTIAL_REF`` and is
    never rendered. ``namespace`` is the application namespace hint shown in
    each view's ``scope``; it does not narrow a query (R17).
    """

    api_url: str
    token: str = field(repr=False)
    ca_file: str
    namespace: str = DEFAULT_NAMESPACE

    def __post_init__(self) -> None:
        if (
            not isinstance(self.token, str)
            or not self.token
            or not self.token.isprintable()
        ):
            raise ToolContractError("K8S_CONFIG_INVALID")
        object.__setattr__(self, "api_url", _clean_api_url(self.api_url))
        if not check_namespace(self.namespace):
            raise ToolContractError("K8S_CONFIG_INVALID")
        path = Path(self.ca_file)
        if not path.is_file() or not os.access(path, os.R_OK):
            raise ToolContractError("K8S_CONFIG_INVALID")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> KubernetesConfig:
        """``OPSPILOT_K8S_*`` (A2-4); any of the first three missing is
        ``K8S_CONFIG_MISSING``, a malformed value ``K8S_CONFIG_INVALID``."""
        api_url = env.get("OPSPILOT_K8S_API_URL") or ""
        token = env.get("OPSPILOT_K8S_TOKEN") or ""
        ca_file = env.get("OPSPILOT_K8S_CA_FILE") or ""
        if not api_url or not token or not ca_file:
            raise ToolContractError("K8S_CONFIG_MISSING")
        return cls(
            api_url=api_url,
            token=token,
            ca_file=ca_file,
            namespace=env.get("OPSPILOT_K8S_NAMESPACE") or DEFAULT_NAMESPACE,
        )


def require_jq() -> str:
    """The absolute path of ``jq`` on this worker's PATH, or a startup error
    (A2-8: a worker of the extended profile without jq does not start)."""
    path = shutil.which("jq")
    if not path:
        raise ToolContractError("JQ_NOT_FOUND")
    return os.path.abspath(path)


# -- HTTPS GET -----------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _CAHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS that trusts only the configured CA, with hostname checks.

    A CA file that does not load leaves a context with no trust anchor, so
    every handshake fails (``SOURCE_UNAVAILABLE``) rather than falling back
    to the system roots. ``KubernetesConfig`` already refused a missing or
    unreadable file at startup.
    """

    def __init__(self, ca_file: str) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            context.load_verify_locations(cafile=ca_file)
        except (OSError, ssl.SSLError, ValueError):
            pass  # no trust anchor: every handshake fails closed
        super().__init__(context=context)


def kubernetes_opener(ca_file: str | None) -> urllib.request.OpenerDirector:
    """No proxy, no redirects, HTTPS verified against ``ca_file`` only."""
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    ]
    if ca_file is not None:
        handlers.append(_CAHTTPSHandler(ca_file))
    return urllib.request.build_opener(*handlers)


@dataclass(frozen=True)
class _Fetched:
    status: int
    body: bytes
    #: ``get(..., cut=True)`` only: the 2xx body ran past ``max_bytes`` and
    #: was cut there (A3-3: a log stream keeps what it read).
    cut: bool = False


def _monotonic() -> float:
    return time.monotonic()


def _remaining(deadline: float) -> float:
    left = deadline - _monotonic()
    if left <= 0:
        raise TransportTimeout("TIMEOUT", source_contact="possible")
    return left


# -- the Secret gateway (B4-5, B4-6, r11) --------------------------------------

#: B4: the only field selector a Secret list may carry, compared whole.
HELM_FIELD_SELECTOR = "type=helm.sh/release.v1"
#: B4: the ``owner`` label value Helm puts on its release records.
HELM_OWNER = "helm"
_HELM_OWNER_SELECTOR = f"owner={HELM_OWNER}"
_HELM_NAME_PREFIX = f"{_HELM_OWNER_SELECTOR},name="
#: B4-9: objects per Helm release page (a record can be about 1.4 MiB).
HELM_PAGE_LIMIT = 5
#: Helm's release-name rule (a DNS-1123 subdomain, at most 53 characters).
HELM_RELEASE_NAME = re.compile(
    r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*"
)
MAX_HELM_RELEASE_NAME_CHARS = 53
_CONTINUE_TOKEN = re.compile(r"[A-Za-z0-9_=+/-]{1,4096}")
_HELM_QUERY_ORDER = ("fieldSelector", "labelSelector", "limit", "continue")
_HELM_FIRST_PAGE_KEYS = frozenset(_HELM_QUERY_ORDER[:3])
_HELM_NEXT_PAGE_KEYS = frozenset(_HELM_QUERY_ORDER)
_NAMESPACED_SECRETS = re.compile(r"/api/v1/namespaces/([^/]+)/secrets")
#: Refused at the resource position of every request (B4-5 step 2): the old
#: watch and proxy prefixes move ``secrets`` to another position.
_REFUSED_RESOURCES = frozenset({"watch", "proxy"})


class KubernetesRequestRefused(Exception):
    """``KubernetesClient.get`` refused a request before sending it (B4-5).

    Not a ``TransportError``: nothing was attempted, so every cluster
    transport turns it into ``refused(code)`` (``sent=False``).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_release_name(name: object) -> bool:
    """B4-1: Helm's release-name rule (it goes into the label selector)."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_HELM_RELEASE_NAME_CHARS
        and HELM_RELEASE_NAME.fullmatch(name) is not None
    )


def helm_label_selector(release_name: str | None) -> str:
    """The only two label selectors a Helm page carries (B4-7)."""
    if release_name is None:
        return _HELM_OWNER_SELECTOR
    return _HELM_NAME_PREFIX + release_name


def helm_secrets_url(endpoint: str, namespace: str | None) -> str:
    """The Helm release page path (B4-1): every namespace or one."""
    base = endpoint.rstrip("/") + "/api/v1"
    if namespace is None:
        return base + "/secrets"
    return f"{base}/namespaces/{namespace}/secrets"


def helm_page_query(label_selector: str, continue_token: str | None) -> str:
    """The exact query string of one Helm release page, in the fixed key
    order the gateway re-encodes and compares against (B4-5 step 5)."""
    values = {
        "fieldSelector": HELM_FIELD_SELECTOR,
        "labelSelector": label_selector,
        "limit": str(HELM_PAGE_LIMIT),
    }
    if continue_token is not None:
        values["continue"] = continue_token
    return urllib.parse.urlencode(
        [(key, values[key]) for key in _HELM_QUERY_ORDER if key in values]
    )


def _resource_segment(segments: Sequence[str]) -> str | None:
    """The resource position of a decoded API path (lower case), or ``None``
    for a path that has none (discovery, ``/version``, ...)."""
    lower = [segment.lower() for segment in segments]
    if len(lower) >= 3 and lower[0] == "api":
        base = 2
    elif len(lower) >= 4 and lower[0] == "apis":
        base = 3
    else:
        return None
    if lower[base] == "namespaces" and len(lower) > base + 2:
        return lower[base + 2]
    return lower[base]


def _helm_labels_ok(value: str) -> bool:
    if value == _HELM_OWNER_SELECTOR:
        return True
    return value.startswith(_HELM_NAME_PREFIX) and check_release_name(
        value[len(_HELM_NAME_PREFIX) :]
    )


def secret_request_problem(url: str, accept: str, *, helm_page: bool) -> str | None:
    """``RESOURCE_NOT_ALLOWED`` for a request the gateway refuses, else
    ``None`` (B4-5, r11).

    Every request: the path is percent-decoded segment by segment; an empty
    segment (other than a trailing one), ``.``, ``..`` or a decoded ``/`` is
    refused, and so is ``watch`` or ``proxy`` at the resource position. A
    request whose resource position is ``secrets`` in any spelling passes
    only as a Helm release page: ``helm_page`` (the client's switch and the
    Helm transport's own call), the raw path exactly ``/api/v1/secrets`` or
    ``/api/v1/namespaces/{namespace}/secrets``, ``Accept`` exactly
    ``application/json``, no userinfo or fragment, and a query whose key set
    is exactly ``fieldSelector, labelSelector, limit`` (plus ``continue`` on
    later pages), each key once, each value equal to what the Helm tools
    build, and the raw string byte-equal to its re-encoding. Pure.
    """
    refusal = "RESOURCE_NOT_ALLOWED"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return refusal
    if not parts.path.startswith("/"):
        return refusal
    decoded = [urllib.parse.unquote(s) for s in parts.path.split("/")[1:]]
    for index, segment in enumerate(decoded):
        if segment == "" and index != len(decoded) - 1:
            return refusal
        if segment in (".", "..") or "/" in segment:
            return refusal
    if decoded and decoded[-1] == "":
        decoded = decoded[:-1]
    resource = _resource_segment(decoded)
    if resource in _REFUSED_RESOURCES:
        return refusal
    if resource != "secrets":
        return None
    # A Secret request: only the exact Helm release page.
    if not helm_page or "#" in url or "@" in parts.netloc:
        return refusal
    if parts.path != "/api/v1/secrets":
        match = _NAMESPACED_SECRETS.fullmatch(parts.path)
        if match is None or not check_namespace(match[1]):
            return refusal
    if accept != "application/json":
        return refusal
    raw = parts.query
    if ";" in raw:
        return refusal
    try:
        pairs = urllib.parse.parse_qsl(raw, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        return refusal
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        return refusal
    if set(keys) not in (_HELM_FIRST_PAGE_KEYS, _HELM_NEXT_PAGE_KEYS):
        return refusal
    values = dict(pairs)
    if values["fieldSelector"] != HELM_FIELD_SELECTOR:
        return refusal
    if values["limit"] != str(HELM_PAGE_LIMIT):
        return refusal
    if not _helm_labels_ok(values["labelSelector"]):
        return refusal
    token = values.get("continue")
    if token is not None and _CONTINUE_TOKEN.fullmatch(token) is None:
        return refusal
    if raw != helm_page_query(values["labelSelector"], token):
        return refusal
    return None


@dataclass(frozen=True)
class ListResult:
    """Every object of one kind (one scope), merged across pages.

    ``items`` have ``managedFields`` replaced by ``managedFieldsSummary`` and carry ``kind``/``apiVersion``
    as the upstream tools' JSON list output does; events are already limited to the
    window. ``source_start_at``/``source_end_at`` bound the kept events' own
    timestamps (``None`` for other kinds: an object is current state).
    """

    items: list[dict[str, Any]]
    pages: int
    bytes_read: int
    notes: list[str]
    source_start_at: datetime | None = None
    source_end_at: datetime | None = None

    def document(self) -> dict[str, Any]:
        return {"apiVersion": "v1", "kind": "List", "items": self.items}


class KubernetesClient:
    """GET-only access to one API server with the ``k8s-ro`` token.

    ``credentials`` maps ``CREDENTIAL_REF`` to the bearer token; a missing or
    empty slot is ``CREDENTIAL_REF_UNKNOWN`` before anything is sent -- no
    other slot is ever tried. ``opener`` replaces the default (tests).
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        opener: urllib.request.OpenerDirector | None = None,
        ca_file: str | None = None,
        helm_releases: bool = False,
    ) -> None:
        self._credentials = dict(credentials)
        self._opener = opener or kubernetes_opener(ca_file)
        self._deadline_handlers = DeadlineHandlers(self._opener)
        #: B4-2: ``OPSPILOT_HELM_RELEASES=1``; only then may a Helm release
        #: page (and nothing else of Secrets) pass the gateway.
        self._helm_releases = helm_releases

    def token(self) -> str:
        token = self._credentials.get(CREDENTIAL_REF)
        if not token:
            raise TransportError("CREDENTIAL_REF_UNKNOWN")
        return token

    def get(
        self,
        url: str,
        *,
        deadline: float,
        max_bytes: int,
        accept: str = "application/json",
        cut: bool = False,
        helm_page: bool = False,
    ) -> _Fetched:
        """One GET bounded by ``deadline`` (monotonic) and ``max_bytes``.

        The single exit of every cluster read, so the Secret gateway runs
        here first (B4-5): a refused request raises
        ``KubernetesRequestRefused`` and the opener is never called.
        ``helm_page`` is passed only by the Helm release transport; it counts
        only with the client's ``helm_releases`` switch.

        2xx and 4xx come back (a 4xx body cut at ``ERROR_BODY_BYTES``); 5xx is
        ``TransportUnavailable``; a body past ``max_bytes`` is
        ``TransportResultTooLarge`` -- or, with ``cut=True``, its first
        ``max_bytes`` bytes with ``_Fetched.cut`` set (A3-3); the deadline is
        ``TransportTimeout``.
        """
        code = secret_request_problem(
            url, accept, helm_page=self._helm_releases and helm_page
        )
        if code is not None:
            raise KubernetesRequestRefused(code)
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token()}",
        }
        http_request = urllib.request.Request(url, headers=headers, method="GET")
        sockets = RequestSockets(_remaining(deadline))

        def read() -> _Fetched:
            self._deadline_handlers.local.sockets = sockets
            try:
                try:
                    with self._opener.open(
                        http_request, timeout=sockets.remaining()
                    ) as response:
                        body = response.read(max_bytes + 1)
                        status = int(response.status)
                except urllib.error.HTTPError as error:
                    with error:
                        body = error.read(ERROR_BODY_BYTES)
                    code = int(error.code)
                    if code >= 500:
                        raise TransportUnavailable(str(code)) from None
                    return _Fetched(code, body)
                if len(body) > max_bytes:
                    if cut:
                        return _Fetched(status, body[:max_bytes], cut=True)
                    raise TransportResultTooLarge(str(max_bytes))
                return _Fetched(status, body)
            finally:
                del self._deadline_handlers.local.sockets

        future: Future[_Fetched] = Future()

        def run() -> None:
            try:
                future.set_result(read())
            except BaseException as error:
                future.set_exception(error)

        thread = threading.Thread(target=run, daemon=True, name="k8s-query")
        try:
            thread.start()
            return future.result(timeout=sockets.remaining())
        except TimeoutError:
            raise TransportTimeout("TIMEOUT") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TransportTimeout("TIMEOUT") from None
            raise TransportUnavailable("UNREACHABLE") from None
        except OSError:
            raise TransportUnavailable("UNREACHABLE") from None
        finally:
            sockets.abort()

    def list_objects(
        self,
        *,
        endpoint: str,
        resource: Resource,
        namespace: str | None,
        label_selector: str | None,
        field_selector: str | None,
        window: Window,
        deadline: float,
        consumer: str = "jq ran",
    ) -> ListResult | TransportResponse:
        """All objects of ``resource`` in scope: pages of ``PAGE_LIMIT`` read
        to the end under one ``MAX_SOURCE_READ_BYTES`` budget (A2-9).

        ``consumer`` names what runs over the list in the event notes
        ("dropped before <consumer>"): jq for A2, the columns for A6.

        Returns a ``TransportResponse`` for a source error (the caller hands
        it on): 4xx as its HTTP status code, a page that is not a list as an
        unparseable body (``MALFORMED_RESULT``). Over the byte budget raises
        ``TransportResultTooLarge`` -- the whole call fails, no partial list.
        """
        notes: list[str] = []
        scoped_namespace = namespace if resource.namespaced else None
        if namespace is not None and not resource.namespaced:
            notes.append(
                f"{resource.plural} is cluster-scoped; the namespace parameter "
                "was ignored."
            )
        base = endpoint.rstrip("/") + resource.path(scoped_namespace)
        items: list[dict[str, Any]] = []
        used = 0
        pages = 0
        token: str | None = None
        while True:
            query: list[tuple[str, str]] = [("limit", str(PAGE_LIMIT))]
            if label_selector is not None:
                query.append(("labelSelector", label_selector))
            if field_selector is not None:
                query.append(("fieldSelector", field_selector))
            if token:
                query.append(("continue", token))
            budget = MAX_SOURCE_READ_BYTES - used
            if budget <= 0:
                raise TransportResultTooLarge(str(MAX_SOURCE_READ_BYTES))
            fetched = self.get(
                base + "?" + urllib.parse.urlencode(query),
                deadline=deadline,
                max_bytes=budget,
            )
            pages += 1
            if fetched.status != 200:
                return TransportResponse(
                    body=fetched.body,
                    source_status=str(fetched.status),
                    http_status=fetched.status,
                )
            used += len(fetched.body)
            page = _page(fetched.body)
            if page is None:
                return TransportResponse(body=b"")
            page_items, token, list_kind, api_version = page
            for item in page_items:
                metadata = item.get("metadata")
                if isinstance(metadata, dict):
                    _summarise_managed_fields(metadata)
                if list_kind:
                    item.setdefault("kind", list_kind)
                if api_version:
                    item.setdefault("apiVersion", api_version)
                items.append(item)
            if not token:
                break
        start_at = end_at = None
        if resource.plural == "events":
            items, start_at, end_at, outside, untimed = _events_in_window(items, window)
            if outside:
                notes.append(
                    f"{outside} events outside the authorized window were "
                    f"dropped before {consumer}."
                )
            if untimed:
                notes.append(
                    f"{untimed} events without any timestamp were dropped "
                    f"before {consumer} (they cannot be placed in the window)."
                )
        return ListResult(
            items=items,
            pages=pages,
            bytes_read=used,
            notes=notes,
            source_start_at=start_at,
            source_end_at=end_at,
        )


MAX_MANAGED_FIELDS_SUMMARY = 20
_MANAGED_FIELDS_KEYS = ("manager", "operation", "time", "subresource")


def _summarise_managed_fields(metadata: dict[str, Any]) -> None:
    """Replace ``managedFields`` with ``managedFieldsSummary`` (#696, B4-6).

    Each entry keeps only ``manager``, ``operation``, ``time`` and, when
    present, ``subresource`` -- never the field tree; an entry with none of
    manager, operation or time is skipped. Entries are sorted by
    ``time`` ascending (the API server's string, unchanged); past
    ``MAX_MANAGED_FIELDS_SUMMARY`` the newest are kept and the number dropped
    is ``managedFieldsSummaryDropped``. Absent or empty ``managedFields``
    leaves no summary key.
    """
    managed = metadata.pop("managedFields", None)
    if not isinstance(managed, list):
        return
    summary = [
        {
            key: entry[key]
            for key in _MANAGED_FIELDS_KEYS
            if isinstance(entry.get(key), str)
        }
        for entry in managed
        if isinstance(entry, dict)
    ]
    # An entry with none of manager, operation or time says nothing.
    summary = [
        entry
        for entry in summary
        if any(key in entry for key in ("manager", "operation", "time"))
    ]
    if not summary:
        return
    summary.sort(key=lambda entry: entry.get("time", ""))
    dropped = max(0, len(summary) - MAX_MANAGED_FIELDS_SUMMARY)
    metadata["managedFieldsSummary"] = summary[dropped:]
    if dropped:
        metadata["managedFieldsSummaryDropped"] = dropped


def _page(body: bytes) -> tuple[list[dict[str, Any]], str | None, str, str] | None:
    """``(items, continue, item kind, apiVersion)`` of one list page."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    raw_items = payload.get("items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list) or any(
        not isinstance(i, dict) for i in raw_items
    ):
        return None
    metadata = payload.get("metadata")
    token = metadata.get("continue") if isinstance(metadata, dict) else None
    if token is not None and not isinstance(token, str):
        return None
    kind = payload.get("kind")
    item_kind = (
        kind[: -len("List")] if isinstance(kind, str) and kind.endswith("List") else ""
    )
    api_version = payload.get("apiVersion")
    return (
        raw_items,
        token or None,
        item_kind,
        api_version if isinstance(api_version, str) else "",
    )


_EVENT_TIME_FIELDS: tuple[tuple[str, ...], ...] = (
    ("lastTimestamp",),
    # The latest repeat of a series event, before its first observation:
    # an event first seen before the window and repeated inside it is in it.
    ("series", "lastObservedTime"),
    ("eventTime",),
    ("firstTimestamp",),
    ("metadata", "creationTimestamp"),
)


def event_time(event: Mapping[str, Any]) -> datetime | None:
    """An event's own time: the first usable of ``lastTimestamp``,
    ``series.lastObservedTime``, ``eventTime``, ``firstTimestamp``,
    ``metadata.creationTimestamp`` (A2-11; these are often null)."""
    for path in _EVENT_TIME_FIELDS:
        cursor: object = event
        for step in path:
            cursor = cursor.get(step) if isinstance(cursor, Mapping) else None
        if isinstance(cursor, str) and cursor:
            try:
                moment = datetime.fromisoformat(cursor)
            except ValueError:
                continue
            if moment.tzinfo is not None and moment.utcoffset() is not None:
                return moment
    return None


def _events_in_window(
    items: Sequence[dict[str, Any]], window: Window
) -> tuple[list[dict[str, Any]], datetime | None, datetime | None, int, int]:
    kept: list[dict[str, Any]] = []
    stamps: list[datetime] = []
    outside = untimed = 0
    for item in items:
        moment = event_time(item)
        if moment is None:
            untimed += 1
        elif window.start <= moment <= window.end:
            kept.append(item)
            stamps.append(moment)
        else:
            outside += 1
    if not stamps:
        return kept, None, None, outside, untimed
    return kept, min(stamps), max(stamps), outside, untimed


# -- bounded child process (A2-8; reused by A3/A6) -----------------------------


def _limit_memory() -> None:
    # Runs in the child between fork and exec: one syscall, no allocation.
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (JQ_MEMORY_BYTES, JQ_MEMORY_BYTES))
    except (ImportError, ValueError, OSError):
        pass  # best effort where the platform refuses (macOS); Linux enforces


def run_isolated(
    argv: Sequence[str | os.PathLike[str]],
    stdin: bytes,
    *,
    deadline: float,
    max_output_bytes: int,
    cwd: str,
    source_contact: SourceContact = "confirmed",
) -> tuple[int, bytes]:
    """Run ``argv`` (no shell) with an empty environment in ``cwd``.

    Returns ``(exit status, stdout)``. Past ``deadline`` the child is killed
    and ``TransportTimeout`` is raised with ``source_contact`` (confirmed
    by default: the caller has already read the source); past ``max_output_bytes`` of stdout it is killed and
    ``TransportResultTooLarge`` is raised. Memory is capped at
    ``JQ_MEMORY_BYTES`` where the platform allows. stderr is discarded.
    """
    left = deadline - _monotonic()
    if left <= 0:
        raise TransportTimeout("TIMEOUT", source_contact=source_contact)
    try:
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={},
            cwd=cwd,
            close_fds=True,
            preexec_fn=_limit_memory,  # noqa: PLW1509 - setrlimit only
        )
    except OSError:
        raise TransportError("CHILD_UNAVAILABLE") from None
    output = bytearray()
    over = threading.Event()

    def feed() -> None:
        assert child.stdin is not None
        try:
            child.stdin.write(stdin)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass

    def drain() -> None:
        assert child.stdout is not None
        while True:
            chunk = child.stdout.read(65536)
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > max_output_bytes:
                over.set()
                child.kill()
                return

    def discard() -> None:
        assert child.stderr is not None
        while child.stderr.read(_STDERR_BYTES):
            pass

    threads = [
        threading.Thread(target=feed, daemon=True, name="child-stdin"),
        threading.Thread(target=drain, daemon=True, name="child-stdout"),
        threading.Thread(target=discard, daemon=True, name="child-stderr"),
    ]
    for thread in threads:
        thread.start()
    try:
        status = child.wait(timeout=max(0.0, deadline - _monotonic()))
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        raise TransportTimeout("CHILD_TIMEOUT", source_contact=source_contact) from None
    finally:
        for thread in threads:
            thread.join(timeout=1.0)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    if over.is_set():
        raise TransportResultTooLarge(str(max_output_bytes))
    return status, bytes(output)


def _jq_problem(expr: object) -> bool:
    return (
        not isinstance(expr, str)
        or not expr.strip()
        or len(expr) > MAX_JQ_EXPR_CHARS
        or any(word in expr for word in _JQ_FORBIDDEN)
        or "\x00" in expr
    )


class _Jq:
    """One jq binary run under ``run_isolated`` with an empty module path."""

    def __init__(self, path: str) -> None:
        self._path = path

    def _run(
        self,
        program: str,
        stdin: bytes,
        *,
        null_input: bool,
        deadline: float,
        source_contact: SourceContact,
    ) -> tuple[int, bytes]:
        with tempfile.TemporaryDirectory(prefix="opspilot-jq-") as root:
            work = Path(root) / "work"
            modules = Path(root) / "modules"
            work.mkdir()
            modules.mkdir()
            # The program goes in a file, never on the command line: an
            # expression starting with ``-`` cannot be read as an option.
            program_file = Path(root) / "program.jq"
            program_file.write_text(program, encoding="utf-8")
            argv = [self._path, "--compact-output", "-L", str(modules)]
            if null_input:
                argv.append("-n")
            argv += ["-f", str(program_file)]
            return run_isolated(
                argv,
                stdin,
                deadline=deadline,
                max_output_bytes=MAX_JQ_OUTPUT_BYTES,
                cwd=str(work),
                source_contact=source_contact,
            )

    def evaluate(
        self, expr: str, document: bytes, *, deadline: float
    ) -> tuple[int, bytes]:
        return self._run(
            expr,
            document,
            null_input=False,
            deadline=deadline,
            source_contact="confirmed",
        )


# -- bounded regular-expression child (A3-7; reused by A6) --------------------

#: A3-7: the longest pattern the child is given.
MAX_PATTERN_CHARS = 2000

#: The child program, shipped in the package and run by its path (never
#: written at run time): Python ``re`` over a JSON list of lines on stdin.
REGEX_CHILD = Path(__file__).with_name("_regex_child.py")


def require_python() -> str:
    """The absolute path of ``python3`` on this worker's PATH, or a startup
    error (A3-7: the regex child needs an interpreter; like jq, a worker of
    the extended profile without one does not start)."""
    path = shutil.which("python3")
    if not path:
        raise ToolContractError("PYTHON_NOT_FOUND")
    return os.path.abspath(path)


def pattern_problem(pattern: object) -> bool:
    """A3-7: a pattern is a non-empty string of at most 2000 characters."""
    return (
        not isinstance(pattern, str)
        or not pattern
        or len(pattern) > MAX_PATTERN_CHARS
        or "\x00" in pattern
    )


@dataclass(frozen=True)
class LineFilter:
    """What the child decided: one flag per input line and how it matched."""

    keep: tuple[bool, ...]
    include_mode: str | None
    exclude_mode: str | None
    removed_include: int
    removed_exclude: int


def filter_lines(
    lines: Sequence[str],
    *,
    include: str | None,
    exclude: str | None,
    ignore_case: bool,
    deadline: float,
) -> LineFilter | None:
    """Run ``include``/``exclude`` over ``lines`` in a bounded child.

    The child is ``REGEX_CHILD`` (a fixed file of this package) run by the
    ``python3`` that ``require_python`` resolves (the worker already checked
    it at startup) in isolated mode without ``site`` (``-I -S -B``), started
    by ``run_isolated``: no shell, an empty
    environment, an empty working directory, the call's remaining time
    (a pathological pattern is killed: ``timeout``/``TOOL_TIMEOUT``, the jq
    timeout class) and 512 MiB of memory. A line is kept when it matches
    ``include`` (if given) and does not match ``exclude`` (if given).
    ``None`` when the child failed or answered something malformed.
    Patterns must already have passed ``pattern_problem``.
    """
    spec = {
        "ignore_case": ignore_case,
        "include": include,
        "exclude": exclude,
        "lines": list(lines),
    }
    stdin = json.dumps(spec, separators=(",", ":")).encode("ascii")
    try:
        python = require_python()
    except ToolContractError:
        raise TransportError("CHILD_UNAVAILABLE") from None
    with tempfile.TemporaryDirectory(prefix="opspilot-re-") as work:
        status, output = run_isolated(
            [python, "-I", "-S", "-B", REGEX_CHILD],
            stdin,
            deadline=deadline,
            max_output_bytes=len(lines) + 4096,
            cwd=work,
        )
    if status != 0:
        return None
    try:
        answer = json.loads(output.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(answer, dict):
        return None
    keep = answer.get("keep")
    if not isinstance(keep, str) or len(keep) != len(lines) or set(keep) - {"0", "1"}:
        return None
    modes = (answer.get("include_mode"), answer.get("exclude_mode"))
    if any(mode not in (None, "regex", "substring") for mode in modes):
        return None
    removed_include = answer.get("removed_include")
    removed_exclude = answer.get("removed_exclude")
    if not (
        type(removed_include) is int
        and type(removed_exclude) is int
        and removed_include >= 0
        and removed_exclude >= 0
    ):
        return None
    return LineFilter(
        keep=tuple(flag == "1" for flag in keep),
        include_mode=modes[0],
        exclude_mode=modes[1],
        removed_include=removed_include,
        removed_exclude=removed_exclude,
    )


# -- transport -------------------------------------------------------------------


def _optional_text(params: Mapping[str, object], name: str) -> tuple[bool, str | None]:
    value = params.get(name)
    if value is None:
        return True, None
    if not check_selector(value):
        return False, None
    return True, str(value)


class KubernetesTransport:
    """Serves ``kubernetes_jq_query`` and ``kubernetes_count``.

    Every refusal of the model's input is decided before anything is sent:
    the kind (Secret first), the namespace, the selectors, the jq expression
    (length, ``import``/``include``). Then the list is read
    (``KubernetesClient.list_objects``) and jq runs once over it; an
    expression that does not compile is reported after the read.

    ``fetch_pod_logs`` (A3) is served by the same transport, over the same
    client: it is handed to ``kubernetes_logs.PodLogsTransport``. It is still
    routed by its exact name (``RoutedTransport`` ``routes``), never through
    ``K8S_TOOLS``.
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        jq_path: str | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        ca_file: str | None = None,
        application_namespace: str = DEFAULT_NAMESPACE,
        monotonic: Callable[[], float] | None = None,
        helm_releases: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = KubernetesClient(
            credentials=credentials,
            opener=opener,
            ca_file=ca_file,
            helm_releases=helm_releases,
        )
        # ``None``: resolved from PATH on first use (the worker resolves it at
        # startup and passes it in, A2-8).
        self._jq_path = jq_path
        self._application_namespace = application_namespace
        self._monotonic = monotonic or _monotonic
        # Imported here: kubernetes_logs builds on this module.
        from opspilot.tools.kubernetes_logs import PodLogsTransport

        self._logs = PodLogsTransport(
            client=self._client,
            application_namespace=application_namespace,
            monotonic=self._monotonic,
        )
        from opspilot.tools.kubernetes_tabular import TabularQueryTransport

        self._tabular = TabularQueryTransport(
            client=self._client,
            application_namespace=application_namespace,
            monotonic=self._monotonic,
        )
        from opspilot.tools.kubernetes_live_metrics import LiveMetricsTransport

        self._live_metrics = LiveMetricsTransport(
            client=self._client, monotonic=self._monotonic
        )
        from opspilot.tools.helm_releases import HelmReleasesTransport

        self._helm = HelmReleasesTransport(
            client=self._client, monotonic=self._monotonic, now=now
        )
        from opspilot.tools.argocd_apps import ArgoCDTransport

        self._argocd = ArgoCDTransport(
            client=self._client, monotonic=self._monotonic, now=now
        )

    def fetch(self, request: TransportRequest) -> TransportResponse:
        try:
            return self._fetch(request)
        except KubernetesRequestRefused as refusal:
            # B4-5: the gateway refused before the opener ran.
            return refused(refusal.code)

    def _fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool in _HELM_TOOLS:
            return self._helm.fetch(request)
        if request.tool in _ARGOCD_TOOLS:
            return self._argocd.fetch(request)
        if request.tool == _POD_LOGS_TOOL:
            return self._logs.fetch(request)
        if request.tool == _TABULAR_TOOL:
            return self._tabular.fetch(request)
        if request.tool in _LIVE_METRICS_TOOLS:
            return self._live_metrics.fetch(request)
        if request.tool not in K8S_TOOLS:
            raise TransportError("TOOL_NOT_SUPPORTED")
        deadline = self._monotonic() + request.timeout_seconds
        params = request.params
        resource, code = resolve_kind(params.get("kind"))
        if resource is None:
            return refused(code or "UNKNOWN_KIND")
        namespace = params.get("namespace")
        if namespace is not None and not check_namespace(namespace):
            return refused("INVALID_NAMESPACE")
        ok_label, label_selector = _optional_text(params, "label_selector")
        ok_field, field_selector = _optional_text(params, "field_selector")
        if not (ok_label and ok_field):
            return refused("INVALID_SELECTOR")
        expr = params.get("jq_expr")
        if _jq_problem(expr):
            return refused("INVALID_JQ")
        assert isinstance(expr, str)
        self._client.token()  # a missing slot fails before anything runs
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("K8S_ENDPOINT_MISSING")
        if self._jq_path is None:
            self._jq_path = require_jq()
        jq = _Jq(self._jq_path)
        listed = self._client.list_objects(
            endpoint=endpoint,
            resource=resource,
            namespace=namespace if isinstance(namespace, str) else None,
            label_selector=label_selector,
            field_selector=field_selector,
            window=request.window,
            deadline=deadline,
        )
        if isinstance(listed, TransportResponse):
            return listed
        # Redacted before the model's expression runs (#574, B1-U2 a): an
        # expression such as ``[.items[].data.DB_PASSWORD]`` or
        # ``.spec.containers[].env[].value`` would otherwise return bare values
        # with no name beside them for the output redaction to recognise. A
        # filter on a redacted field (``select(.data.API_TOKEN == "x")``)
        # therefore matches the marker, not the value.
        document = json.dumps(
            redact_payload(listed.document()), separators=(",", ":")
        ).encode("utf-8")
        # jq runs exactly once per call (A2-9), so a compile error is only
        # known after the read: still INVALID_PARAMS, with the source read.
        status, output = jq.evaluate(expr, document, deadline=deadline)
        if status == _JQ_COMPILE_STATUS:
            return _answered("JQ_COMPILE_ERROR")
        if status == _JQ_RUNTIME_STATUS:
            return _answered("JQ_RUNTIME_ERROR")
        if status != 0:
            return _answered("JQ_FAILED")
        lines = _output_lines(output)
        if lines is None:
            return _answered("JQ_FAILED")
        scope = {
            "kind": resource.plural,
            "api_group": resource.group,
            "cluster_scoped": not resource.namespaced,
            "namespace": namespace if resource.namespaced else None,
            "all_namespaces": resource.namespaced and namespace is None,
            "application_namespace": self._application_namespace,
        }
        view_fields: dict[str, object] = {
            "total_items_processed": len(listed.items),
            "pages_read": listed.pages,
            "scope": scope,
            "notes": list(listed.notes),
            "cannot_prove": _CANNOT_PROVE,
        }
        if request.tool == COUNT_TOOL:
            preview = [
                text[:COUNT_PREVIEW_CHARS] for text, _ in lines[:COUNT_PREVIEW_ITEMS]
            ]
            body = canonical({"count": len(lines), "preview": preview}).encode("utf-8")
            return TransportResponse(
                body=body,
                source_start_at=listed.source_start_at,
                source_end_at=listed.source_end_at,
                row_unit="matches",
                backend_rows_returned=len(lines),
                view_fields={**view_fields, "matches_found": len(lines)},
            )
        body = canonical({"results": [value for _, value in lines]}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            source_start_at=listed.source_start_at,
            source_end_at=listed.source_end_at,
            view_fields=view_fields,
        )


def _output_lines(output: bytes) -> list[tuple[str, object]] | None:
    """jq ``-c`` output: one JSON value per line, as ``(text, value)``."""
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines: list[tuple[str, object]] = []
    # Only "\n" separates jq -c outputs: ``splitlines`` would also split on
    # U+2028, U+2029 and U+0085, which a JSON string may carry unescaped.
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            json.dumps(value, allow_nan=False)
        except (ValueError, RecursionError):
            return None
        lines.append((line, value))
    return lines


# -- model-visible face and registrations --------------------------------------

_KINDS_TEXT = ", ".join(sorted(RESOURCES))

_COMMON_DESCRIPTION = (
    "kind is a plural resource name from this list, exactly as written: "
    f"{_KINDS_TEXT}. Events are kind=events. secrets cannot be read: asking "
    "for them is denied with RESOURCE_NOT_ALLOWED and nothing is sent; any "
    "other kind not listed is refused as invalid. Omit namespace to read the "
    "whole cluster; a cluster-scoped kind ignores namespace. "
    "label_selector and field_selector are passed to the API server as is. "
    "jq_expr runs once over the whole list "
    '{"apiVersion":"v1","kind":"List","items":[...]}, every object as the '
    "API server returns it, except that metadata.managedFields is replaced "
    "by metadata.managedFieldsSummary: one entry per manager, operation and "
    "subresource, each with manager, operation, time and subresource (when "
    "set), sorted oldest first (newest last), at most the newest 20 "
    "(metadata.managedFieldsSummaryDropped counts the rest). Do not escape "
    "jq_expr; give it unescaped. import and include are refused, and at most "
    "2000 characters are accepted. "
    "Objects are their state at the moment of the query, not at the time of "
    "the incident: they cannot prove what the cluster looked like earlier. "
    "Events are only those whose own timestamp lies inside the authorized "
    "window; the others are dropped before jq runs and counted in notes. "
    "More than 4 MiB of objects, or more than 2 MiB of jq output, refuses the "
    "whole call with RESULT_TOO_LARGE and no partial result; narrow it with "
    "namespace, label_selector or field_selector. "
    "Events are the exception: read them across all namespaces (omit "
    "namespace) without filtering by type, and group them first by "
    "involvedObject.kind, involvedObject.namespace and reason, since the "
    "events that explain a fault are often Normal and recorded in another "
    "namespace. A grouping example: .items | map({kind: "
    ".involvedObject.kind, namespace: .involvedObject.namespace, reason}) | "
    "group_by(.) | .[] | .[0] + {count: length}. If that is still too large, "
    "filter with field_selector on involvedObject fields, e.g. "
    "involvedObject.kind=Pod, instead of narrowing to one namespace. "
    "HTTP 401/403 from the cluster is denied with SOURCE_FORBIDDEN."
)

_PARAMETERS_SCHEMA: Mapping[str, Any] = {
    "kind": {
        "type": "string",
        "description": "Plural resource name, for example pods, deployments or events.",
    },
    "jq_expr": {
        "type": "string",
        "description": (
            "jq expression applied to the list, for example "
            '.items[] | select(.status.phase != "Running") | .metadata.name'
        ),
    },
    "namespace": {
        "type": "string",
        "description": "Namespace to read; omit for the whole cluster.",
    },
    "label_selector": {
        "type": "string",
        "description": "Optional label selector, for example app=checkout.",
    },
    "field_selector": {
        "type": "string",
        "description": "Optional field selector, for example status.phase=Running.",
    },
}

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": JQ_QUERY_TOOL,
            "description": (
                "Get the JSON of all cluster objects of one kind and filter it "
                "with a jq expression; returns the jq outputs as results. "
                "Use select() to filter objects before extracting fields, "
                "e.g. .items[] | .spec.containers[].image | "
                'select(test("^gcr.io/") | not). ' + _COMMON_DESCRIPTION
            ),
            "parameters": {
                "type": "object",
                "properties": dict(_PARAMETERS_SCHEMA),
                "required": ["kind", "jq_expr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": COUNT_TOOL,
            "description": (
                "Get the JSON of all cluster objects of one kind, apply a jq "
                "filter and count its outputs. Use this whenever asked to "
                "count Kubernetes resources. Use select() to filter objects "
                "before extracting properties, e.g. .items[] | "
                'select(.metadata.namespace == "test-1") | .metadata.name. '
                "Returns count (every output) and preview: the first 10 "
                "outputs, each cut to 200 characters. " + _COMMON_DESCRIPTION
            ),
            "parameters": {
                "type": "object",
                "properties": dict(_PARAMETERS_SCHEMA),
                "required": ["kind", "jq_expr"],
            },
        },
    },
)

_RETURNS_TAIL = (
    " The view adds total_items_processed (objects jq ran over), pages_read, "
    "scope (kind, namespace or all_namespaces, cluster_scoped, "
    "application_namespace) and notes."
)
_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. Objects are "
    "read at query time whatever the window says; only events are limited to "
    "it, by their own timestamp, before jq runs."
)
_VALUES_FORMAT = (
    "The authorized target for this Run is appended as {values}. kind must be "
    "one of: " + _KINDS_TEXT + "; secrets in any spelling is RESOURCE_NOT_ALLOWED "
    "before anything is sent; namespace, when given, is a DNS label."
)
_LIMITS = (
    "Pages of 500 objects are read to the end and merged; more than 4 MiB "
    "read in total is RESULT_TOO_LARGE for the whole call, never a partial "
    "list. jq runs once in a separate process with no environment, no "
    "modules (import and include refused), at most 2000 characters of "
    "expression, the call's remaining time of the 30 s request (a jq timeout "
    "is timeout/TOOL_TIMEOUT), 2 MiB of output (more is RESULT_TOO_LARGE) and "
    "512 MiB of memory; an expression that does not compile or fails at run "
    "time is INVALID_PARAMS. The whole view is limited to the registered "
    "max_view_tokens (25,000 DeepSeek tokens): a larger result is refused "
    "with RESULT_TOO_LARGE, never truncated. HTTP 400/404/422 are "
    "INVALID_PARAMS, 401/403 denied/SOURCE_FORBIDDEN, 410 SOURCE_ERROR, 429 "
    "and 5xx SOURCE_UNAVAILABLE."
)
_MANAGED_FIELDS_CANNOT_PROVE = (
    "metadata.managedFieldsSummary gives each manager's last write time "
    "only: it is not a full change history (earlier writes by the same "
    "manager are gone) and it does not say which fields that write changed."
)
_CANNOT_PROVE = (
    "Objects are the cluster's state when the query ran, not during the "
    "incident window: they cannot prove what was true earlier, and an "
    "object missing now may have existed then. Events cover only their own "
    "timestamps inside the window; an event that is not returned is unknown, "
    "not proof nothing happened (events expire and timestamps are often "
    "null). A jq result is what the expression selected, not a full picture "
    "of the kind. " + _MANAGED_FIELDS_CANNOT_PROVE
)


def _parameters() -> dict[str, ParameterSpec]:
    return {
        "kind": ParameterSpec(
            "string", required=True, description="Plural resource name from the table."
        ),
        "jq_expr": ParameterSpec(
            "string", required=True, description="jq expression applied to the list."
        ),
        "namespace": ParameterSpec(
            "string", description="Namespace to read; omitted is the whole cluster."
        ),
        "label_selector": ParameterSpec(
            "string", description="Optional label selector."
        ),
        "field_selector": ParameterSpec(
            "string", description="Optional field selector."
        ),
    }


def kubernetes_registrations(
    *, max_window_seconds: int
) -> tuple[ToolRegistration, ...]:
    """The two registrations (A2-1: ``source`` is the target's, the
    ``otel-demo`` source; ``may_contain_secrets=True``, Q3/A2-14)."""
    common: dict[str, Any] = {
        "version": "v1",
        "source": SOURCE,
        "verb": "list",
        "may_contain_secrets": True,
        "parameters": _parameters(),
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_result_bytes": MAX_JQ_OUTPUT_BYTES,
        "max_view_tokens": MAX_VIEW_TOKENS,
        "max_window_seconds": max_window_seconds,
        "error_classes": dict(ERROR_CLASSES),
    }
    jq_query = ToolRegistration(
        name=JQ_QUERY_TOOL,
        description=ToolDescription(
            returns=(
                "The jq outputs over the list of all objects of one kind read "
                "from the cluster's API server, under results." + _RETURNS_TAIL
            ),
            window_format=_WINDOW_FORMAT,
            values_format=_VALUES_FORMAT,
            limits=_LIMITS,
            cannot_prove=_CANNOT_PROVE,
        ),
        result_path=("results",),
        **common,
    )
    count = ToolRegistration(
        name=COUNT_TOOL,
        description=ToolDescription(
            returns=(
                "The number of jq outputs over the list of all objects of one "
                "kind read from the cluster's API server, as count, and the "
                "first 10 outputs cut to 200 characters under preview; "
                "backend_matches_returned, matches_shown and matches_omitted "
                "state the same counts." + _RETURNS_TAIL
            ),
            window_format=_WINDOW_FORMAT,
            values_format=_VALUES_FORMAT,
            limits=_LIMITS,
            cannot_prove=_CANNOT_PROVE,
        ),
        result_path=("preview",),
        **common,
    )
    return jq_query, count


def _registrations() -> tuple[ToolRegistration, ...]:
    """Every registration this transport serves under the profiles' 24 h
    frame -- the two object queries, ``fetch_pod_logs`` (A3) and
    ``kubernetes_tabular_query`` (A6) -- for the
    private inventory seam ``otel_demo._registrations`` also offers."""
    from opspilot.tools.kubernetes_logs import pod_logs_registrations
    from opspilot.tools.kubernetes_tabular import tabular_registrations

    return (
        *kubernetes_registrations(max_window_seconds=OBSERVATION_SECONDS),
        *pod_logs_registrations(max_window_seconds=OBSERVATION_SECONDS),
        *tabular_registrations(max_window_seconds=OBSERVATION_SECONDS),
    )


def resource_table() -> dict[str, dict[str, object]]:
    """The static table as JSON, for the profile's version hash (A2-5)."""
    return {plural: RESOURCES[plural].as_json() for plural in sorted(RESOURCES)}
