"""Helm release history: ``helm_list``, ``helm_history`` and ``helm_status`` (B4, #563).

The upstream HolmesGPT Helm core tools of the same names (its Helm toolset
at ``a0009195``), served from the release records Helm keeps in the cluster
(Secrets of Helm's release type, ``kubernetes_core.HELM_FIELD_SELECTOR``)
over the A2 client (:class:`opspilot.tools.kubernetes_core.KubernetesClient`)
instead of the command-line client. The public interface is
``docs/tasks/2026-11-20-tool-expansion-b4.md``; the decisions are R8-18,
r11 (B4C4-U1=a, the corrected R8-18 wording) and B4-1 to B4-16:

* Only Helm release pages are read: a GET of Secrets in every namespace or
  one, with the fixed field selector, Helm's owner label (plus the release
  name) and ``limit=5``, built by ``kubernetes_core`` and checked again by
  the gateway in
  ``KubernetesClient.get`` (``helm_page=True``). At most 200 pages and 16 MiB
  read per call; past either the whole call is ``RESULT_TOO_LARGE``.
* Decoding happens here, in memory: base64 (Kubernetes) -> base64 (Helm) ->
  gzip when it starts with ``1f 8b 08`` (inflated at most 8 MiB) -> JSON.
  Each row is a new dict of twelve fields; values, manifest, hooks, notes,
  chart content, the Helm 4 resource snapshot and every Secret label and
  annotation are never copied anywhere. A record that does not decode or
  disagrees with its labels is dropped and counted, never echoed.
* Time (B4C4-U1=a): release records are the object's current content -- no
  source time, nothing cut to the incident window.

Residual risk (r11, quoted in the task record): the identity can read the
content of every Secret in the synthetic environment; only this gateway
narrows it to Helm release records.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import zlib
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from opspilot.redaction import redact_credentials
from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
)
from opspilot.tools.kubernetes_core import (
    ENDPOINT_SELECTOR,
    HELM_OWNER,
    KubernetesClient,
    check_namespace,
    check_release_name,
    helm_label_selector,
    helm_page_query,
    helm_secrets_url,
    refused,
)
from opspilot.tools.kubernetes_core import _monotonic as _monotonic
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "ENV",
    "HISTORY_TOOL",
    "HelmReleasesTransport",
    "INVALID_CODE",
    "LIST_TOOL",
    "NOT_LAB_CODE",
    "STATUS_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "helm_registrations",
    "helm_releases_enabled",
]

LIST_TOOL = "helm_list"
HISTORY_TOOL = "helm_history"
STATUS_TOOL = "helm_status"
TOOL_NAMES: tuple[str, ...] = (LIST_TOOL, HISTORY_TOOL, STATUS_TOOL)

#: B4-2: the switch, read once at startup by both processes (not an
#: ``OPSPILOT_K8S_*`` name: the web reads it, A2-3).
ENV = "OPSPILOT_HELM_RELEASES"
INVALID_CODE = "HELM_RELEASES_INVALID"
#: B4-3: the worker refuses to start with the switch on and a cluster
#: address that is not a lab host.
NOT_LAB_CODE = "HELM_NOT_LAB"

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 2 * 1024 * 1024
#: B4-9: raw bytes read across every page of one call.
MAX_READ_BYTES = 16 * 1024 * 1024
#: B4-9: pages of one call.
MAX_PAGES = 200
#: B4-9: one record inflated (gzip bomb guard).
MAX_INFLATED_BYTES = 8 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 2000

_GZIP_MAGIC = b"\x1f\x8b\x08"
_STATUS = re.compile(r"[a-z-]{1,32}")
_ROLLBACK = re.compile(r"Rollback to ([0-9]{1,9})")
_ZERO_TIME = datetime(1, 1, 1, tzinfo=timezone.utc)


def _wall_clock() -> datetime:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc)


def helm_releases_enabled(env: Mapping[str, str]) -> bool:
    """``1`` is on; unset, empty or ``0`` off; anything else is a startup
    error (``HELM_RELEASES_INVALID``), as for the other switches."""
    value = env.get(ENV)
    if value is None or value in ("", "0"):
        return False
    if value == "1":
        return True
    raise SystemExit(INVALID_CODE)


# -- decoding one record (B4-8, B4-10) ----------------------------------------


class _Malformed(Exception):
    pass


class _TooLarge(Exception):
    pass


def _inflate(blob: str) -> bytes:
    try:
        helm_layer = base64.b64decode(blob, validate=True)
        raw = base64.b64decode(helm_layer, validate=True)
    except (binascii.Error, ValueError):
        raise _Malformed from None
    del helm_layer
    if raw[:3] != _GZIP_MAGIC:
        if len(raw) > MAX_INFLATED_BYTES:
            raise _TooLarge
        return raw
    inflater = zlib.decompressobj(wbits=31)
    try:
        inflated = inflater.decompress(raw, MAX_INFLATED_BYTES + 1)
    except zlib.error:
        raise _Malformed from None
    if len(inflated) > MAX_INFLATED_BYTES:
        raise _TooLarge
    if not inflater.eof:
        raise _Malformed
    return inflated


def _time(info: Mapping[str, Any], key: str) -> str | None:
    value = info.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise _Malformed
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise _Malformed from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise _Malformed
    try:
        moment = moment.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise _Malformed from None
    if moment.replace(microsecond=0) == _ZERO_TIME:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _optional_text(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _Malformed
    return value


def _project(item: Mapping[str, Any]) -> dict[str, Any]:
    """The twelve-field row of one Secret, or ``_Malformed``/``_TooLarge``.

    Only the listed fields are copied into a new dict; the decoded record is
    local to this call and never returned, logged or raised.
    """
    metadata = item.get("metadata")
    data = item.get("data")
    if not isinstance(metadata, dict) or not isinstance(data, dict):
        raise _Malformed
    blob = data.get("release")
    if not isinstance(blob, str):
        raise _Malformed
    inflated = _inflate(blob)
    try:
        record = json.loads(inflated.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Malformed from None
    del inflated
    if not isinstance(record, dict):
        raise _Malformed
    name = record.get("name")
    version = record.get("version")
    namespace = record.get("namespace")
    if not isinstance(name, str) or not name:
        raise _Malformed
    if type(version) is not int or version <= 0:
        raise _Malformed
    if not isinstance(namespace, str) or namespace != metadata.get("namespace"):
        raise _Malformed
    labels = metadata.get("labels")
    if not isinstance(labels, dict):
        raise _Malformed
    if (
        labels.get("owner") != HELM_OWNER
        or labels.get("name") != name
        or labels.get("version") != str(version)
    ):
        raise _Malformed
    info = record.get("info")
    if not isinstance(info, dict):
        raise _Malformed
    status = info.get("status")
    if not isinstance(status, str) or _STATUS.fullmatch(status) is None:
        raise _Malformed
    chart = record.get("chart")
    chart_metadata = chart.get("metadata") if isinstance(chart, dict) else None
    if chart is not None and not isinstance(chart, dict):
        raise _Malformed
    if chart_metadata is not None and not isinstance(chart_metadata, dict):
        raise _Malformed
    chart_fields: Mapping[str, Any] = chart_metadata or {}
    description = _optional_text(info, "description")
    rollback: int | None = None
    field = info.get("rollback_revision")
    if type(field) is int and field > 0:
        rollback = field
    elif description is not None:
        match = _ROLLBACK.fullmatch(description)
        if match is not None:
            rollback = int(match[1])
    return {
        "name": name,
        "namespace": namespace,
        "revision": version,
        "status": status,
        "chart": _optional_text(chart_fields, "name"),
        "chart_version": _optional_text(chart_fields, "version"),
        "app_version": _optional_text(chart_fields, "appVersion"),
        "first_deployed": _time(info, "first_deployed"),
        "last_deployed": _time(info, "last_deployed"),
        "deleted": _time(info, "deleted"),
        "description": None
        if description is None
        else redact_credentials(description[:MAX_DESCRIPTION_CHARS]),
        "rollback_revision": rollback,
    }


def _page_items(body: bytes) -> tuple[list[Any], str | None]:
    """``(items, continue)`` of one ``SecretList`` page, or ``_Malformed``."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Malformed from None
    if not isinstance(payload, dict) or payload.get("kind") != "SecretList":
        raise _Malformed
    items = payload.get("items")
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise _Malformed
    metadata = payload.get("metadata")
    token = metadata.get("continue") if isinstance(metadata, dict) else None
    if token is not None and not isinstance(token, str):
        raise _Malformed
    return items, token or None


# -- transport ----------------------------------------------------------------


class HelmReleasesTransport:
    """Serves the three Helm tools over the A2 client (GET only)."""

    def __init__(
        self,
        *,
        client: KubernetesClient,
        monotonic: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._monotonic = monotonic or _monotonic
        # The worker passes the executor's clock; the fallback is the wall
        # clock, used only for the "queried at" note, never for a decision.
        self._now = now or _wall_clock

    def fetch(self, request: TransportRequest) -> TransportResponse:
        tool = request.tool
        if tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        deadline = self._monotonic() + request.timeout_seconds
        params = request.params
        namespace = params.get("namespace")
        release: str | None = None
        if tool == LIST_TOOL:
            if namespace is not None and not check_namespace(namespace):
                return refused("INVALID_NAMESPACE")
        else:
            if not check_namespace(namespace):
                return refused("INVALID_NAMESPACE")
            raw_release = params.get("release_name")
            if not check_release_name(raw_release):
                return refused("INVALID_RELEASE_NAME")
            assert isinstance(raw_release, str)
            release = raw_release
        assert namespace is None or isinstance(namespace, str)
        self._client.token()  # a missing slot fails before anything is sent
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("K8S_ENDPOINT_MISSING")
        url = helm_secrets_url(endpoint, namespace)
        label = helm_label_selector(release)
        queried_at = self._now().astimezone(timezone.utc).replace(microsecond=0)
        rows: list[dict[str, Any]] = []
        malformed = too_large = 0
        used = pages = 0
        token: str | None = None
        while True:
            budget = MAX_READ_BYTES - used
            if pages >= MAX_PAGES or budget <= 0:
                return _too_large()
            try:
                fetched = self._client.get(
                    url + "?" + helm_page_query(label, token),
                    deadline=deadline,
                    max_bytes=budget,
                    helm_page=True,
                )
            except TransportResultTooLarge:
                return _too_large()
            pages += 1
            if fetched.status != 200:
                return _source_error(fetched.status, fetched.body)
            used += len(fetched.body)
            try:
                items, token = _page_items(fetched.body)
            except _Malformed:
                return TransportResponse(body=b"")
            del fetched
            for item in items:
                try:
                    rows.append(_project(item))
                except _TooLarge:
                    too_large += 1
                except Exception:  # noqa: BLE001 -- never echo a bad record
                    malformed += 1
            del items
            if not token:
                break
        shown = _select(tool, rows, release)
        notes = [
            f"Helm release records as stored now (queried at "
            f"{queried_at.strftime('%Y-%m-%dT%H:%M:%SZ')}); revisions outside "
            "the authorized window are listed too."
        ]
        if malformed:
            notes.append(
                f"{malformed} malformed Helm release records were dropped (they "
                "did not decode or disagreed with their labels); nothing from "
                "them is shown."
            )
        if too_large:
            notes.append(
                f"{too_large} Helm release records were dropped as too_large "
                "(they inflate past 8 MiB); nothing from them is shown."
            )
        if not shown:
            notes.append(_empty_note(tool, namespace, release))
        view_fields: dict[str, object] = {}
        if tool == LIST_TOOL:
            view_fields["scope"] = {
                "namespace": namespace,
                "all_namespaces": namespace is None,
            }
        view_fields.update(
            {
                "dropped": {"malformed": malformed, "too_large": too_large},
                "notes": notes,
                "cannot_prove": _CANNOT_PROVE,
            }
        )
        body = canonical({"results": shown}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            return _too_large()
        return TransportResponse(
            body=body,
            row_unit="revisions" if tool == HISTORY_TOOL else "releases",
            backend_rows_returned=len(shown),
            view_fields=view_fields,
        )


def _select(
    tool: str, rows: list[dict[str, Any]], release: str | None
) -> list[dict[str, Any]]:
    if release is not None:
        rows = [row for row in rows if row["name"] == release]
    if tool == HISTORY_TOOL:
        return sorted(rows, key=lambda row: int(row["revision"]))
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["namespace"]), str(row["name"]))
        kept = latest.get(key)
        if kept is None or int(row["revision"]) > int(kept["revision"]):
            latest[key] = row
    ordered = [latest[key] for key in sorted(latest)]
    if tool == STATUS_TOOL:
        return ordered[:1]
    return ordered


def _empty_note(tool: str, namespace: str | None, release: str | None) -> str:
    if release is not None:
        return (
            f"No Helm release named {release} in namespace {namespace}: no "
            "release record was found there."
        )
    if namespace is not None:
        return f"No Helm release records in namespace {namespace}."
    return "No Helm release records in the cluster."


_TOO_LARGE_NOTE = (
    "The Helm release records exceed this call's limits (16 MiB read, 200 "
    "pages of 5, or 2 MiB of rows); no partial result is given. Give "
    "namespace, or release_name with helm_history or helm_status, to read "
    "fewer records."
)


def _too_large() -> TransportResponse:
    """A2-9: never a partial result; the whole call is RESULT_TOO_LARGE,
    with a note on how to narrow it (the records were read, so ``sent``)."""
    return TransportResponse(
        body=canonical({"error": "RESULT_TOO_LARGE"}).encode("utf-8"),
        source_status="RESULT_TOO_LARGE",
        view_fields={"notes": [_TOO_LARGE_NOTE]},
    )


_FORBIDDEN_NOTE = (
    "This cluster's read-only identity has no permission to read Helm release records."
)


def _source_error(status: int, body: bytes) -> TransportResponse:
    fields: dict[str, object] | None = None
    if status in (401, 403):
        fields = {"notes": [_FORBIDDEN_NOTE]}
    return TransportResponse(
        body=body, source_status=str(status), http_status=status, view_fields=fields
    )


# -- model-visible face (B4-16) -----------------------------------------------

_WHAT = (
    "Reads the release records Helm stores in the cluster (Secrets of Helm's "
    "release type), decoded inside OpsPilot. Each row gives name, "
    "namespace, revision, status, chart, chart_version, app_version, "
    "first_deployed, last_deployed, deleted, description and "
    "rollback_revision. Chart values, the rendered manifest, hooks and "
    "release notes are never returned. History is not cut to the incident "
    "window: every kept revision is listed with its own time. Changes made "
    "outside Helm (manual edits, other deployment tools) are not in these "
    "records; check ReplicaSets and events for them."
)

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": LIST_TOOL,
            "description": (
                "Every Helm release, one row each at its latest revision "
                "whatever its status (deployed, failed, pending-*, "
                "uninstalled...). Omit namespace for the whole cluster. " + _WHAT
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
            "name": HISTORY_TOOL,
            "description": (
                "The revision history of one Helm release, every revision "
                "Helm kept (10 by default), oldest first. " + _WHAT
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "release_name": {
                        "type": "string",
                        "description": "The Helm release name.",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Namespace of the release.",
                    },
                },
                "required": ["release_name", "namespace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": STATUS_TOOL,
            "description": (
                "The status of one Helm release: its latest revision. " + _WHAT
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "release_name": {
                        "type": "string",
                        "description": "The Helm release name.",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Namespace of the release.",
                    },
                },
                "required": ["release_name", "namespace"],
            },
        },
    },
)

_RETURNS = (
    "Rows under results, each with name, namespace, revision, status, chart, "
    "chart_version, app_version, first_deployed, last_deployed, deleted "
    "(UTC, to the second; null when unset), description (cut to 2000 "
    "characters) and rollback_revision. helm_list: one row per release at "
    "its highest revision, sorted by namespace and name; helm_history: every "
    "kept revision, ascending; helm_status: the highest revision. The view "
    "adds dropped (malformed, too_large), notes and cannot_prove; helm_list "
    "also scope (namespace, all_namespaces). Values, manifest, hooks and "
    "notes are never returned."
)
_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. Release records "
    "are read at query time and not cut to the window; the first note says "
    "so."
)
_VALUES_FORMAT = (
    "The authorized target for this Run is appended as {values}. namespace "
    "is a DNS label and release_name follows Helm's rule (lower-case DNS "
    "subdomain, at most 53 characters); anything else is INVALID_PARAMS "
    "before anything is sent."
)
_LIMITS = (
    "Pages of 5 release records, at most 200 pages and 16 MiB read in all, "
    "a record inflated to at most 8 MiB (larger ones are dropped as "
    "too_large), 2 MiB of rows and the registered max_view_tokens (25,000 "
    "DeepSeek tokens): anything larger is RESULT_TOO_LARGE for the whole "
    "call, never truncated; give namespace or release_name. 30 s per call, "
    "no retry. HTTP 400/404/422 is INVALID_PARAMS, 401/403 "
    "SOURCE_FORBIDDEN (the identity has no Helm permission), 410 "
    "SOURCE_ERROR, 429 and 5xx SOURCE_UNAVAILABLE; a page that is not a "
    "Secret list is MALFORMED_RESULT."
)
_CANNOT_PROVE = (
    "Release history is the current content of Helm's records: revisions "
    "outside the authorized window are listed too, so it cannot prove that "
    "only these changes happened inside the window. Only Helm's own record: "
    "changes made with kubectl apply or kubectl edit, or by any tool other "
    "than Helm, are not in it. Revision times come from Helm, not from when "
    "Pods finished rolling out; deployed only means Helm considered the "
    "release successful, not that its Pods are healthy."
)

_ERROR_CLASSES: Mapping[str, str] = {
    "400": "INVALID_PARAMS",
    "404": "INVALID_PARAMS",
    "422": "INVALID_PARAMS",
    "401": "SOURCE_FORBIDDEN",
    "403": "SOURCE_FORBIDDEN",
    "410": "SOURCE_ERROR",
    "429": "SOURCE_UNAVAILABLE",
    "503": "SOURCE_UNAVAILABLE",
    "RESOURCE_NOT_ALLOWED": "RESOURCE_NOT_ALLOWED",
    "RESULT_TOO_LARGE": "RESULT_TOO_LARGE",
    "INVALID_NAMESPACE": "INVALID_PARAMS",
    "INVALID_RELEASE_NAME": "INVALID_PARAMS",
}


def helm_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The three registrations (``source`` is the lab target's;
    ``may_contain_secrets=True``: a description may carry a failure text)."""
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
    description = ToolDescription(
        returns=_RETURNS,
        window_format=_WINDOW_FORMAT,
        values_format=_VALUES_FORMAT,
        limits=_LIMITS,
        cannot_prove=_CANNOT_PROVE,
    )
    named = {
        "release_name": ParameterSpec(
            "string", required=True, description="The Helm release name."
        ),
        "namespace": ParameterSpec(
            "string", required=True, description="Namespace of the release."
        ),
    }
    return (
        ToolRegistration(
            name=LIST_TOOL,
            description=description,
            parameters={
                "namespace": ParameterSpec(
                    "string",
                    description="Namespace to read; omitted is the whole cluster.",
                )
            },
            **common,
        ),
        ToolRegistration(
            name=HISTORY_TOOL, description=description, parameters=dict(named), **common
        ),
        ToolRegistration(
            name=STATUS_TOOL, description=description, parameters=dict(named), **common
        ),
    )
