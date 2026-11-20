"""ArgoCD read-only tools (C4, #568): ``argocd_app_list``, ``argocd_app_get``,
``argocd_app_history`` and ``argocd_app_resources``.

The read-only subset of the upstream HolmesGPT ``argocd/core`` toolset
(``holmes/plugins/toolsets/argocd.yaml`` at ``a0009195``), served from the
``applications.argoproj.io`` custom resources through the A2 client
(:class:`opspilot.tools.kubernetes_core.KubernetesClient`) instead of the
``argocd`` command-line client. The public interface is
``docs/tasks/2026-11-20-tool-expansion-c4.md``; the decisions are R8-19,
B4C4-U1 = a and C4-1 to C4-11:

* GET only, fixed paths of ``applications`` (C4-5): never ``applicationsets``,
  ``appprojects`` or a Secret, and not in the generic ``RESOURCES`` table, so
  the jq, count and tabular tools never see an Application.
* A whitelist projection (C4-6): every row is built from the listed fields
  only. Helm values and parameters, kustomize, plugin environment, jsonnet
  variables, the copies of the source in ``status``, the top-level
  ``operation``, annotations and labels never leave this module; a repository
  URL loses its userinfo, query and fragment.
* Current state (B4C4-U1 = a): no source time, history not cut to the window.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
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
    KubernetesClient,
    check_namespace,
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
    "ArgoCDTransport",
    "ENV",
    "GET_TOOL",
    "HISTORY_TOOL",
    "INVALID_CODE",
    "LIST_TOOL",
    "RESOURCES_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "argocd_enabled",
    "argocd_registrations",
]

LIST_TOOL = "argocd_app_list"
GET_TOOL = "argocd_app_get"
HISTORY_TOOL = "argocd_app_history"
RESOURCES_TOOL = "argocd_app_resources"
TOOL_NAMES: tuple[str, ...] = (LIST_TOOL, GET_TOOL, HISTORY_TOOL, RESOURCES_TOOL)

#: C4-2: the switch, read once at startup by both processes.
ENV = "OPSPILOT_ARGOCD"
INVALID_CODE = "ARGOCD_INVALID"

API_BASE = "/apis/argoproj.io/v1alpha1"
DEFAULT_NAMESPACE = "argocd"
LIST_PAGE_LIMIT = 100
MAX_PAGES = 50
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_LIST_ROWS = 120
MAX_RESOURCE_ROWS = 250
MAX_VALUE_CHARS = 2000
MAX_APP_NAME = 253

_APP_NAME = re.compile(
    r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*"
)


def _wall_clock() -> datetime:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc)


def argocd_enabled(env: Mapping[str, str]) -> bool:
    """``1`` is on; unset, empty or ``0`` off; anything else is a startup
    error (``ARGOCD_INVALID``), as for the other switches."""
    value = env.get(ENV)
    if value is None or value in ("", "0"):
        return False
    if value == "1":
        return True
    raise SystemExit(INVALID_CODE)


def check_app_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_APP_NAME
        and _APP_NAME.fullmatch(name) is not None
    )


# -- projection (C4-6) ------------------------------------------------------------


class _Malformed(Exception):
    pass


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:MAX_VALUE_CHARS]


def _time(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


class _Counts:
    def __init__(self) -> None:
        self.bad_repo_urls = 0


def _repo_url(value: object, counts: _Counts) -> str | None:
    """``scheme://host[:port]/path`` only: userinfo, query and fragment are
    dropped (a token can be written into the URL)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        counts.bad_repo_urls += 1
        return None
    if parts.scheme and host:
        netloc = host if port is None else f"{host}:{port}"
        if ":" in host:  # IPv6 literal
            netloc = f"[{host}]" if port is None else f"[{host}]:{port}"
        return _text(
            urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        )
    if "@" in value or "?" in value or "#" in value or "://" in value:
        # Not a URL that splits cleanly but carries URL syntax: never echoed.
        counts.bad_repo_urls += 1
        return None
    return _text(value)  # e.g. an OCI or scp-style reference without secrets


def _source(value: object, counts: _Counts) -> dict[str, Any]:
    src = _map(value)
    return {
        "repo_url": _repo_url(src.get("repoURL"), counts),
        "path": _text(src.get("path")),
        "chart": _text(src.get("chart")),
        "target_revision": _text(src.get("targetRevision")),
        "ref": _text(src.get("ref")),
        "name": _text(src.get("name")),
    }


def _sources(holder: Mapping[str, Any], counts: _Counts) -> list[dict[str, Any]]:
    """``source`` or ``sources[]`` as one list of six-field entries."""
    many = holder.get("sources")
    if isinstance(many, list) and many:
        return [_source(item, counts) for item in many]
    if isinstance(holder.get("source"), Mapping):
        return [_source(holder["source"], counts)]
    return []


def _revisions(holder: Mapping[str, Any]) -> dict[str, Any]:
    many = holder.get("revisions")
    return {
        "revision": _text(holder.get("revision")),
        "revisions": [_text(r) for r in many if isinstance(r, str)]
        if isinstance(many, list)
        else None,
    }


def _destination(spec: Mapping[str, Any]) -> dict[str, Any]:
    dest = _map(spec.get("destination"))
    return {
        "server": _text(dest.get("server")),
        "name": _text(dest.get("name")),
        "namespace": _text(dest.get("namespace")),
    }


def _automated(spec: Mapping[str, Any]) -> dict[str, Any] | None:
    policy = _map(spec.get("syncPolicy"))
    if "automated" not in policy:
        return None
    auto = _map(policy.get("automated"))
    return {
        "prune": auto.get("prune") is True,
        "self_heal": auto.get("selfHeal") is True,
    }


def _identity(app: Mapping[str, Any]) -> tuple[str, str]:
    meta = _map(app.get("metadata"))
    name, namespace = meta.get("name"), meta.get("namespace")
    if not isinstance(name, str) or not name or not isinstance(namespace, str):
        raise _Malformed
    return name, namespace


def _summary(app: Mapping[str, Any], counts: _Counts) -> dict[str, Any]:
    name, namespace = _identity(app)
    spec, status = _map(app.get("spec")), _map(app.get("status"))
    return {
        "name": _text(name),
        "namespace": _text(namespace),
        "project": _text(spec.get("project")),
        "sources": _sources(spec, counts),
        "destination": _destination(spec),
        "sync_status": _text(_map(status.get("sync")).get("status")),
        "health_status": _text(_map(status.get("health")).get("status")),
        "operation_phase": _text(_map(status.get("operationState")).get("phase")),
        "reconciled_at": _time(status.get("reconciledAt")),
    }


def _sync_result(state: Mapping[str, Any]) -> dict[str, Any] | None:
    result = state.get("syncResult")
    if not isinstance(result, Mapping):
        return None
    return {
        **_revisions(result),
        "resources": [
            {
                "group": _text(item.get("group")),
                "kind": _text(item.get("kind")),
                "namespace": _text(item.get("namespace")),
                "name": _text(item.get("name")),
                "status": _text(item.get("status")),
                "message": _text(item.get("message")),
                "hook_phase": _text(item.get("hookPhase")),
                "sync_phase": _text(item.get("syncPhase")),
            }
            for item in _list(result.get("resources"))
            if isinstance(item, Mapping)
        ],
    }


def _detail(app: Mapping[str, Any], counts: _Counts) -> dict[str, Any]:
    name, namespace = _identity(app)
    spec, status = _map(app.get("spec")), _map(app.get("status"))
    sync, health = _map(status.get("sync")), _map(status.get("health"))
    state = status.get("operationState")
    operation_state = None
    if isinstance(state, Mapping):
        operation_state = {
            "phase": _text(state.get("phase")),
            "message": _text(state.get("message")),
            "started_at": _time(state.get("startedAt")),
            "finished_at": _time(state.get("finishedAt")),
            "retry_count": _int(state.get("retryCount")),
            "sync_result": _sync_result(state),
        }
    images = _map(status.get("summary")).get("images")
    return {
        "name": _text(name),
        "namespace": _text(namespace),
        "project": _text(spec.get("project")),
        "sources": _sources(spec, counts),
        "destination": _destination(spec),
        "automated": _automated(spec),
        "sync": {
            "status": _text(sync.get("status")),
            **_revisions(sync),
            "compared_to_destination": _destination(_map(sync.get("comparedTo"))),
        },
        "health": {
            "status": _text(health.get("status")),
            "message": _text(health.get("message")),
        },
        "conditions": [
            {
                "type": _text(item.get("type")),
                "message": _text(item.get("message")),
                "last_transition_time": _time(item.get("lastTransitionTime")),
            }
            for item in _list(status.get("conditions"))
            if isinstance(item, Mapping)
        ],
        "operation_state": operation_state,
        "reconciled_at": _time(status.get("reconciledAt")),
        "images": [_text(i) for i in _list(images) if isinstance(i, str)],
    }


def _history(app: Mapping[str, Any], counts: _Counts) -> list[dict[str, Any]]:
    rows = []
    for item in _list(_map(app.get("status")).get("history")):
        if not isinstance(item, Mapping):
            continue
        who = _map(item.get("initiatedBy"))
        rows.append(
            {
                "id": _int(item.get("id")),
                **_revisions(item),
                "deployed_at": _time(item.get("deployedAt")),
                "deploy_started_at": _time(item.get("deployStartedAt")),
                "initiated_by": {
                    "username": _text(who.get("username")),
                    "automated": who.get("automated") is True,
                },
                "sources": _sources(item, counts),
            }
        )
    rows.sort(key=lambda row: (row["id"] is None, row["id"] or 0))
    return rows


def _resources(app: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in _list(_map(app.get("status")).get("resources")):
        if not isinstance(item, Mapping):
            continue
        health = _map(item.get("health"))
        rows.append(
            {
                "group": _text(item.get("group")),
                "version": _text(item.get("version")),
                "kind": _text(item.get("kind")),
                "namespace": _text(item.get("namespace")),
                "name": _text(item.get("name")),
                "status": _text(item.get("status")),
                "health": {
                    "status": _text(health.get("status")),
                    "message": _text(health.get("message")),
                },
                "hook": item.get("hook") is True,
                "requires_pruning": item.get("requiresPruning") is True,
                "sync_wave": _int(item.get("syncWave")),
            }
        )
    rows.sort(
        key=lambda r: (
            str(r["group"] or ""),
            str(r["kind"] or ""),
            str(r["namespace"] or ""),
            str(r["name"] or ""),
        )
    )
    return rows


# -- transport ----------------------------------------------------------------------


def _refused(code: str, text: str) -> TransportResponse:
    """A fixed-code refusal decided before anything was sent (B1-6 notes)."""
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        sent=False,
        view_fields={"notes": [f"{code}: {text}"]},
    )


_FORBIDDEN_NOTE = (
    "This cluster's read-only identity has no permission to read ArgoCD Applications."
)
_CRD_MISSING_NOTE = (
    "ARGOCD_CRD_MISSING: this cluster has no ArgoCD Application CRD "
    "(applications.argoproj.io)."
)
_TOO_LARGE_NOTE = (
    "The ArgoCD Applications exceed this call's limits (4 MiB read or 2 MiB "
    "of rows); no partial result is given. Give namespace to read fewer."
)


def _not_found_name(body: bytes) -> str | None:
    """The object name of a Kubernetes ``Status`` NotFound body, if any."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "Status":
        return None
    name = _map(payload.get("details")).get("name")
    return name if isinstance(name, str) and name else None


def _source_error(status: int, body: bytes) -> TransportResponse:
    fields: dict[str, object] | None = None
    if status in (401, 403):
        fields = {"notes": [_FORBIDDEN_NOTE]}
    elif status == 404:
        fields = {"notes": [_CRD_MISSING_NOTE]}
        return TransportResponse(
            body=body, source_status="CRD_MISSING", http_status=404, view_fields=fields
        )
    return TransportResponse(
        body=body, source_status=str(status), http_status=status, view_fields=fields
    )


def _too_large() -> TransportResponse:
    return TransportResponse(
        body=canonical({"error": "RESULT_TOO_LARGE"}).encode("utf-8"),
        source_status="RESULT_TOO_LARGE",
        view_fields={"notes": [_TOO_LARGE_NOTE]},
    )


class ArgoCDTransport:
    """Serves the four ArgoCD tools over the A2 client (GET only)."""

    def __init__(
        self,
        *,
        client: KubernetesClient,
        monotonic: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._monotonic = monotonic or _monotonic
        # The worker passes the executor's clock; used only for the note.
        self._now = now or _wall_clock

    def fetch(self, request: TransportRequest) -> TransportResponse:
        tool = request.tool
        if tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        deadline = self._monotonic() + request.timeout_seconds
        params = request.params
        namespace = params.get("namespace")
        if tool == LIST_TOOL:
            if namespace is not None and not check_namespace(namespace):
                return _refused("INVALID_NAMESPACE", _NAMESPACE_RULE)
        else:
            if namespace is None:
                namespace = DEFAULT_NAMESPACE
            if not check_namespace(namespace):
                return _refused("INVALID_NAMESPACE", _NAMESPACE_RULE)
            if not check_app_name(params.get("app_name")):
                return _refused("INVALID_APP_NAME", _APP_NAME_RULE)
        assert namespace is None or isinstance(namespace, str)
        self._client.token()  # a missing slot fails before anything is sent
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("K8S_ENDPOINT_MISSING")
        base = endpoint.rstrip("/") + API_BASE
        queried_at = self._now().astimezone(timezone.utc).replace(microsecond=0)
        counts = _Counts()
        notes = [
            "ArgoCD Application state as stored now (queried at "
            f"{queried_at.strftime('%Y-%m-%dT%H:%M:%SZ')}); history entries "
            "outside the authorized window are listed too."
        ]
        incomplete = False
        view_fields: dict[str, object] = {}
        try:
            if tool == LIST_TOOL:
                listed = self._list(base, namespace, deadline)
                if isinstance(listed, TransportResponse):
                    return listed
                rows = sorted(
                    (_summary(app, counts) for app in listed),
                    key=lambda r: (str(r["namespace"]), str(r["name"])),
                )
                returned = len(rows)
                if returned > MAX_LIST_ROWS:
                    incomplete = True
                    notes.append(
                        f"{returned} applications; the first {MAX_LIST_ROWS} by "
                        "namespace and name are shown. Give namespace to narrow."
                    )
                    rows = rows[:MAX_LIST_ROWS]
                if not rows:
                    notes.append(
                        "No ArgoCD Applications"
                        + (f" in namespace {namespace}." if namespace else ".")
                    )
                view_fields["scope"] = {
                    "namespace": namespace,
                    "all_namespaces": namespace is None,
                }
                unit = "applications"
            else:
                assert namespace is not None
                app_name = str(params["app_name"])
                got = self._get(base, namespace, app_name, deadline)
                if isinstance(got, TransportResponse):
                    return got
                if got is None:
                    notes.append(
                        f"APP_NOT_FOUND: {namespace}/{app_name}: no ArgoCD "
                        "Application of this name in this namespace."
                    )
                    rows, returned = [], 0
                    unit = _UNITS[tool]
                else:
                    rows, unit = self._rows(tool, got, counts)
                    returned = len(rows)
                    if tool == RESOURCES_TOOL and returned > MAX_RESOURCE_ROWS:
                        incomplete = True
                        notes.append(
                            f"{returned} resources; the first "
                            f"{MAX_RESOURCE_ROWS} by group, kind, namespace and "
                            "name are shown."
                        )
                        rows = rows[:MAX_RESOURCE_ROWS]
                    if not rows:
                        what = "sync history" if tool == HISTORY_TOOL else "resources"
                        notes.append(
                            f"The Application {namespace}/{app_name} exists but "
                            f"has no recorded {what}."
                        )
        except TransportResultTooLarge:
            return _too_large()
        except _Malformed:
            return TransportResponse(body=b"")
        if counts.bad_repo_urls:
            notes.append(
                f"{counts.bad_repo_urls} repository URLs could not be parsed "
                "and are shown as null."
            )
        view_fields.update({"notes": notes, "cannot_prove": _CANNOT_PROVE})
        body = canonical({"results": rows}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            return _too_large()
        return TransportResponse(
            body=body,
            row_unit=unit,
            backend_rows_returned=returned,
            incomplete=incomplete,
            view_fields=view_fields,
        )

    def _rows(
        self, tool: str, app: Mapping[str, Any], counts: _Counts
    ) -> tuple[list[dict[str, Any]], str]:
        if tool == GET_TOOL:
            return [_detail(app, counts)], "applications"
        if tool == HISTORY_TOOL:
            return _history(app, counts), "history"
        return _resources(app), "resources"

    def _list(
        self, base: str, namespace: str | None, deadline: float
    ) -> list[Mapping[str, Any]] | TransportResponse:
        path = (
            f"{base}/applications"
            if namespace is None
            else f"{base}/namespaces/{namespace}/applications"
        )
        apps: list[Mapping[str, Any]] = []
        used = pages = 0
        token: str | None = None
        while True:
            budget = MAX_SOURCE_READ_BYTES - used
            if pages >= MAX_PAGES or budget <= 0:
                raise TransportResultTooLarge(str(MAX_SOURCE_READ_BYTES))
            query: list[tuple[str, str]] = [("limit", str(LIST_PAGE_LIMIT))]
            if token:
                query.append(("continue", token))
            fetched = self._client.get(
                path + "?" + urllib.parse.urlencode(query),
                deadline=deadline,
                max_bytes=budget,
            )
            pages += 1
            if fetched.status != 200:
                return _source_error(fetched.status, fetched.body)
            used += len(fetched.body)
            page = _page(fetched.body)
            if page is None:
                raise _Malformed
            items, token, _, _ = page
            apps.extend(items)
            del fetched
            if not token:
                return apps

    def _get(
        self, base: str, namespace: str, app_name: str, deadline: float
    ) -> Mapping[str, Any] | None | TransportResponse:
        fetched = self._client.get(
            f"{base}/namespaces/{namespace}/applications/{app_name}",
            deadline=deadline,
            max_bytes=MAX_SOURCE_READ_BYTES,
        )
        if fetched.status == 404 and _not_found_name(fetched.body) == app_name:
            return None
        if fetched.status != 200:
            return _source_error(fetched.status, fetched.body)
        try:
            payload = json.loads(fetched.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _Malformed from None
        if not isinstance(payload, dict) or payload.get("kind") != "Application":
            raise _Malformed
        return payload


_UNITS: Mapping[str, str] = {
    LIST_TOOL: "applications",
    GET_TOOL: "applications",
    HISTORY_TOOL: "history",
    RESOURCES_TOOL: "resources",
}

_NAMESPACE_RULE = "namespace is a DNS label (lowercase letters, digits and -)."
_APP_NAME_RULE = (
    "app_name is the Application's name: a DNS-1123 subdomain of at most "
    f"{MAX_APP_NAME} characters."
)


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    del rows, query
    return {"row_unit": _UNITS[tool]}


# -- model-visible face (C4-11: tool descriptions only) --------------------------------

_WHAT = (
    "Read from ArgoCD's Application objects in the cluster "
    "(applications.argoproj.io), not from the ArgoCD API. Helm values and "
    "parameters, kustomize patches, plugin settings, annotations and "
    "repository credentials are never returned. The state is the ArgoCD "
    "controller's last reconciliation (reconciled_at). Not available here: "
    "field-level diffs, rendered manifests, repositories, clusters and "
    "projects; OutOfSync only says the live object differs from what Git "
    "renders, so check the object with the Kubernetes tools. History is not "
    "cut to the incident window. namespace defaults to argocd, where ArgoCD "
    "keeps its Applications."
)

_NAME_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "app_name": {"type": "string", "description": "The Application's name."},
        "namespace": {
            "type": "string",
            "description": "Namespace of the Application; default argocd.",
        },
    },
    "required": ["app_name"],
}

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": LIST_TOOL,
            "description": (
                "Every ArgoCD Application with its project, sources, "
                "destination, sync status, health status, last operation phase "
                "and reconciled_at. Omit namespace for the whole cluster. " + _WHAT
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
            "name": GET_TOOL,
            "description": (
                "One ArgoCD Application: sources, destination, automated sync "
                "policy, sync status and revision, health, conditions, the last "
                "operation (phase, message, times, per-resource results) and "
                "images. " + _WHAT
            ),
            "parameters": _NAME_PARAMS,
        },
    },
    {
        "type": "function",
        "function": {
            "name": HISTORY_TOOL,
            "description": (
                "The sync history of one ArgoCD Application, every entry ArgoCD "
                "kept (10 by default), oldest first: id, revision, deployed_at, "
                "deploy_started_at, who started it, sources. " + _WHAT
            ),
            "parameters": _NAME_PARAMS,
        },
    },
    {
        "type": "function",
        "function": {
            "name": RESOURCES_TOOL,
            "description": (
                "The resources one ArgoCD Application manages, each with its "
                "sync status and health. " + _WHAT
            ),
            "parameters": _NAME_PARAMS,
        },
    },
)

_RETURNS = (
    "Rows under results. argocd_app_list: one summary per Application (name, "
    "namespace, project, sources, destination, sync_status, health_status, "
    "operation_phase, reconciled_at), sorted by namespace and name. "
    "argocd_app_get: one row with sources (repo_url without credentials, "
    "path, chart, target_revision, ref, name), destination, automated, sync, "
    "health, conditions, operation_state, reconciled_at and images. "
    "argocd_app_history: status.history, ascending id. argocd_app_resources: "
    "status.resources sorted by group, kind, namespace and name. Times are "
    "UTC to the second. The view adds notes and cannot_prove; the list adds "
    "scope."
)
_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. Applications "
    "are read at query time whatever the window says; there is no source "
    "time, and history entries outside the window are listed too."
)
_VALUES_FORMAT = (
    "The authorized target for this Run is appended as {values}. namespace "
    "is a DNS label (INVALID_NAMESPACE); app_name a DNS-1123 subdomain "
    "(INVALID_APP_NAME); both refused before anything is sent."
)
_LIMITS = (
    f"argocd_app_list at most {MAX_LIST_ROWS} rows and argocd_app_resources "
    f"{MAX_RESOURCE_ROWS}, sorted, the rest marked incomplete. String values "
    "cut to 2000 characters. At most 4 MiB read, 2 MiB of rows and 25,000 "
    "tokens, refused whole above that (RESULT_TOO_LARGE). 30 s per call, no "
    "retry. A missing Application is no_data (APP_NOT_FOUND); a cluster "
    "without the ArgoCD Application CRD is SOURCE_ERROR (ARGOCD_CRD_MISSING); "
    "no permission is SOURCE_FORBIDDEN."
)
_CANNOT_PROVE = (
    "The Application state is the ArgoCD controller's last reconciliation "
    "(reconciled_at), not a live comparison at query time. The sync history "
    "only has syncs ArgoCD ran; changes made to the cluster outside ArgoCD "
    "only show as OutOfSync, without what changed, and field-level diffs are "
    "not available here. Entries outside the incident window are listed "
    "too, so this cannot prove that only these syncs happened in the window."
)

_ERROR_CLASSES: Mapping[str, str] = {
    "400": "INVALID_PARAMS",
    "422": "INVALID_PARAMS",
    "401": "SOURCE_FORBIDDEN",
    "403": "SOURCE_FORBIDDEN",
    "CRD_MISSING": "SOURCE_ERROR",
    "410": "SOURCE_ERROR",
    "429": "SOURCE_UNAVAILABLE",
    "503": "SOURCE_UNAVAILABLE",
    "RESOURCE_NOT_ALLOWED": "RESOURCE_NOT_ALLOWED",
    "RESULT_TOO_LARGE": "RESULT_TOO_LARGE",
    "INVALID_NAMESPACE": "INVALID_PARAMS",
    "INVALID_APP_NAME": "INVALID_PARAMS",
}


def argocd_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The four registrations (``source`` is the lab target's;
    ``may_contain_secrets=True``: a message may carry a rendering error)."""
    common: dict[str, Any] = {
        "version": "v1",
        "source": otel_demo.SOURCE,
        "may_contain_secrets": True,
        "result_path": ("results",),
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_result_bytes": MAX_RESULT_BYTES,
        "max_view_tokens": MAX_VIEW_TOKENS,
        "max_window_seconds": max_window_seconds,
        "error_classes": dict(_ERROR_CLASSES),
    }
    description = ToolDescription(
        returns=_RETURNS,
        window_format=_WINDOW_FORMAT,
        values_format=_VALUES_FORMAT,
        limits=_LIMITS,
        cannot_prove=_CANNOT_PROVE,
    )
    named = {
        "app_name": ParameterSpec(
            "string", required=True, description="The Application's name."
        ),
        "namespace": ParameterSpec(
            "string", description="Namespace of the Application; default argocd."
        ),
    }
    return (
        ToolRegistration(
            name=LIST_TOOL,
            verb="list",
            description=description,
            parameters={
                "namespace": ParameterSpec(
                    "string",
                    description="Namespace to read; omitted is the whole cluster.",
                )
            },
            **common,
        ),
        *(
            ToolRegistration(
                name=name,
                verb="get",
                description=description,
                parameters=dict(named),
                **common,
            )
            for name in (GET_TOOL, HISTORY_TOOL, RESOURCES_TOOL)
        ),
    )
