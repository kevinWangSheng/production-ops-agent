"""Read-only fake Kubernetes API server over one ITBench-Lite snapshot (#583).

Serves the cluster objects, events and logs of one scenario so that the real
``kubernetes_jq_query``/``kubernetes_count`` (A2), ``kubernetes_tabular_query``
(A6) and ``fetch_pod_logs`` (A3) tools read them unchanged. Evaluation
infrastructure only: it lives under ``scripts/`` and is not in the product
image. Data comes through the #581 whitelist loader (``snapshot.Snapshot``),
moved by the scenario's one offset (``shift``).

Behaviour (docs/tasks/2026-11-12-itbench-lite-adapter.md, r4 defaults):

* An object list answers with the latest full collection of that resource
  class whose collection time is not after the virtual now (the wall clock by
  default). ``limit``/``continue`` page over it (a continue token pins the
  moment of the first page), ``labelSelector`` and ``fieldSelector`` filter
  it. Single-object GETs (``.../pods/{name}``) read the same collection.
* Events are the core ``v1`` half of the recorded watch stream replayed up
  to the virtual now: ADDED/MODIFIED keep the latest object, DELETED drops it.
* ``secrets`` is 403 in any group. A resource class never seen in the 35
  scenarios is 503; one seen but without objects here is an empty list.
* ``/pods/{name}/log`` renders the OTLP application log records of that Pod
  (``sinceTime``, ``sinceSeconds``, ``tailLines``, ``timestamps``,
  ``limitBytes``, ``container``); ``previous=true`` is 400 (there is no
  previous instance in the snapshot). Records sit on the container named
  like the Deployment, else the first container (L13); others are empty.
* HTTPS only, with a self-signed certificate made by ``openssl`` (L14); a
  fixed bearer token; GET only; binds a loopback address only.
"""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import hmac
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from scripts.itbench_lite.shift import (
    NS,
    parse_clickhouse_ns,
    parse_rfc3339_ns,
    shift_json_times,
)
from scripts.itbench_lite.snapshot import Snapshot, load_snapshot

# -- resource classes ----------------------------------------------------------


@dataclass(frozen=True)
class ResourceType:
    plural: str
    group: str
    version: str
    kind: str
    namespaced: bool

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version


#: A resource class is its API group, version and plural name.
TypeKey = tuple[str, str, str]


def _types(*rows: tuple[str, str, str, str, bool]) -> dict[TypeKey, ResourceType]:
    return {(r[1], r[2], r[0]): ResourceType(*r) for r in rows}


#: Resource classes seen in the scenario snapshots (the 2026-11-12 probe:
#: full reads of three scenarios plus streamed type counts of two more; the
#: probe found horizontalpodautoscalers, networkpolicies and resourcequotas
#: only in the latter). A class present in the loaded snapshot is served too,
#: even if it is missing here. Anything else is 503 (never seen).
SEEN_RESOURCES: Mapping[TypeKey, ResourceType] = _types(
    ("configmaps", "", "v1", "ConfigMap", True),
    ("endpoints", "", "v1", "Endpoints", True),
    ("events", "", "v1", "Event", True),
    ("limitranges", "", "v1", "LimitRange", True),
    ("namespaces", "", "v1", "Namespace", False),
    ("nodes", "", "v1", "Node", False),
    ("persistentvolumeclaims", "", "v1", "PersistentVolumeClaim", True),
    ("persistentvolumes", "", "v1", "PersistentVolume", False),
    ("pods", "", "v1", "Pod", True),
    ("resourcequotas", "", "v1", "ResourceQuota", True),
    ("serviceaccounts", "", "v1", "ServiceAccount", True),
    ("services", "", "v1", "Service", True),
    ("daemonsets", "apps", "v1", "DaemonSet", True),
    ("deployments", "apps", "v1", "Deployment", True),
    ("replicasets", "apps", "v1", "ReplicaSet", True),
    ("statefulsets", "apps", "v1", "StatefulSet", True),
    ("jobs", "batch", "v1", "Job", True),
    ("horizontalpodautoscalers", "autoscaling", "v2", "HorizontalPodAutoscaler", True),
    ("ingresses", "networking.k8s.io", "v1", "Ingress", True),
    ("networkpolicies", "networking.k8s.io", "v1", "NetworkPolicy", True),
    ("poddisruptionbudgets", "policy", "v1", "PodDisruptionBudget", True),
    ("clusterrolebindings", "rbac.authorization.k8s.io", "v1", "ClusterRoleBinding", False),
    ("clusterroles", "rbac.authorization.k8s.io", "v1", "ClusterRole", False),
    ("rolebindings", "rbac.authorization.k8s.io", "v1", "RoleBinding", True),
    ("roles", "rbac.authorization.k8s.io", "v1", "Role", True),
    ("storageclasses", "storage.k8s.io", "v1", "StorageClass", False),
    ("volumeattachments", "storage.k8s.io", "v1", "VolumeAttachment", False),
    ("customresourcedefinitions", "apiextensions.k8s.io", "v1", "CustomResourceDefinition", False),
)  # fmt: skip

#: Field selectors the real API server accepts per class (the subset A2/A6
#: use); every class also accepts ``metadata.name`` and ``metadata.namespace``.
#: The value is the object path read for the label.
_COMMON_FIELDS: Mapping[str, tuple[str, ...]] = {
    "metadata.name": ("metadata", "name"),
    "metadata.namespace": ("metadata", "namespace"),
}
_FIELDS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "pods": {
        "spec.nodeName": ("spec", "nodeName"),
        "spec.restartPolicy": ("spec", "restartPolicy"),
        "spec.schedulerName": ("spec", "schedulerName"),
        "spec.serviceAccountName": ("spec", "serviceAccountName"),
        "status.phase": ("status", "phase"),
        "status.podIP": ("status", "podIP"),
        "status.nominatedNodeName": ("status", "nominatedNodeName"),
    },
    "events": {
        "involvedObject.kind": ("involvedObject", "kind"),
        "involvedObject.namespace": ("involvedObject", "namespace"),
        "involvedObject.name": ("involvedObject", "name"),
        "involvedObject.uid": ("involvedObject", "uid"),
        "involvedObject.apiVersion": ("involvedObject", "apiVersion"),
        "involvedObject.resourceVersion": ("involvedObject", "resourceVersion"),
        "involvedObject.fieldPath": ("involvedObject", "fieldPath"),
        "reason": ("reason",),
        "reportingComponent": ("reportingComponent",),
        "source": ("source", "component"),
        "type": ("type",),
    },
    "namespaces": {"status.phase": ("status", "phase")},
    "nodes": {"spec.unschedulable": ("spec", "unschedulable")},
    "replicasets": {"status.replicas": ("status", "replicas")},
    "jobs": {"status.successful": ("status", "successful")},
}

_SECRET_PLURAL = "secrets"

# -- HTTP replies ----------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    content_type: str = "application/json"


def _json_reply(value: object, status: int = 200) -> Reply:
    return Reply(status, json.dumps(value, separators=(",", ":")).encode("utf-8"))


_REASONS = {
    400: "BadRequest",
    401: "Unauthorized",
    403: "Forbidden",
    404: "NotFound",
    405: "MethodNotAllowed",
    410: "Expired",
    503: "ServiceUnavailable",
}


def status_reply(code: int, message: str) -> Reply:
    """A ``Status`` object, as the API server writes its failures."""
    return _json_reply(
        {
            "kind": "Status",
            "apiVersion": "v1",
            "metadata": {},
            "status": "Failure",
            "message": message,
            "reason": _REASONS.get(code, "Unknown"),
            "code": code,
        },
        code,
    )


class SnapshotRowError(ValueError):
    """A snapshot row the server cannot load; the server does not start."""


class BadRequest(ValueError):
    """A request parameter the API server would answer with 400."""


# -- selectors -------------------------------------------------------------------

_LABEL_KEY = r"(?:[A-Za-z0-9.\-]+/)?[A-Za-z0-9](?:[A-Za-z0-9_.\-]*[A-Za-z0-9])?"
_LABEL_VALUE = r"(?:[A-Za-z0-9](?:[A-Za-z0-9_.\-]*[A-Za-z0-9])?)?"
_SET_RE = re.compile(rf"\s*({_LABEL_KEY})\s+(in|notin)\s+\(([^()]*)\)\s*")
_EQ_RE = re.compile(rf"\s*({_LABEL_KEY})\s*(==|!=|=)\s*({_LABEL_VALUE})\s*")
_EXISTS_RE = re.compile(rf"\s*(!?)\s*({_LABEL_KEY})\s*")
_VALUE_RE = re.compile(_LABEL_VALUE)

LabelTest = Callable[[Mapping[str, str]], bool]


def _split_requirements(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise BadRequest("unbalanced parentheses in selector")
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if depth != 0:
        raise BadRequest("unbalanced parentheses in selector")
    parts.append(current)
    return parts


def parse_label_selector(text: str) -> list[LabelTest]:
    """Requirements of a label selector: ``k=v``, ``k==v``, ``k!=v``,
    ``k in (a,b)``, ``k notin (a,b)``, ``k``, ``!k`` joined by commas."""
    if not text.strip():
        return []
    tests: list[LabelTest] = []
    for part in _split_requirements(text):
        tests.append(_label_requirement(part))
    return tests


def _label_requirement(part: str) -> LabelTest:
    match = _SET_RE.fullmatch(part)
    if match is not None:
        key, op, raw = match.group(1), match.group(2), match.group(3)
        values = frozenset(v.strip() for v in raw.split(","))
        if any(_VALUE_RE.fullmatch(v) is None for v in values):
            raise BadRequest(f"invalid label value in selector: {part.strip()}")
        if op == "in":
            return lambda labels: labels.get(key) in values
        return lambda labels: labels.get(key) not in values
    match = _EQ_RE.fullmatch(part)
    if match is not None:
        key, op, value = match.group(1), match.group(2), match.group(3)
        if op == "!=":
            return lambda labels: labels.get(key) != value
        return lambda labels: labels.get(key) == value
    match = _EXISTS_RE.fullmatch(part)
    if match is not None:
        negate, key = match.group(1) == "!", match.group(2)
        if negate:
            return lambda labels: key not in labels
        return lambda labels: key in labels
    raise BadRequest(f"unable to parse requirement: {part.strip()}")


def _lookup(obj: Mapping[str, Any], path: Sequence[str]) -> object:
    cursor: object = obj
    for step in path:
        cursor = cursor.get(step) if isinstance(cursor, Mapping) else None
    return cursor


def _field_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


FieldTest = Callable[[Mapping[str, Any]], bool]


def parse_field_selector(text: str, plural: str) -> list[FieldTest]:
    """Requirements of a field selector (``=``, ``==``, ``!=`` only) over the
    labels this class supports; any other label is 400 as on a real server."""
    if not text.strip():
        return []
    supported = {**_COMMON_FIELDS, **_FIELDS.get(plural, {})}
    tests: list[FieldTest] = []
    for part in text.split(","):
        match = re.fullmatch(r"\s*([A-Za-z0-9.]+)\s*(==|!=|=)\s*([^,]*?)\s*", part)
        if match is None:
            raise BadRequest(f"invalid field selector: {part.strip()}")
        label, op, value = match.groups()
        path = supported.get(label)
        if path is None:
            raise BadRequest(f'field label not supported: "{label}" for {plural}')
        tests.append(_field_requirement(path, op, value))
    return tests


def _field_requirement(path: tuple[str, ...], op: str, value: str) -> FieldTest:
    if op == "!=":
        return lambda obj: _field_text(_lookup(obj, path)) != value
    return lambda obj: _field_text(_lookup(obj, path)) == value


def _labels(obj: Mapping[str, Any]) -> Mapping[str, str]:
    labels = _lookup(obj, ("metadata", "labels"))
    return labels if isinstance(labels, Mapping) else {}


# -- continue tokens -------------------------------------------------------------


def _encode_continue(at_ns: int, offset: int) -> str:
    raw = json.dumps({"at": at_ns, "o": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_continue(token: str) -> tuple[int, int]:
    try:
        padded = token + "=" * (-len(token) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        at_ns, offset = value["at"], value["o"]
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise BadRequest("continue key is not valid") from None
    if not isinstance(at_ns, int) or not isinstance(offset, int) or offset < 0:
        raise BadRequest("continue key is not valid")
    return at_ns, offset


# -- the cluster data -------------------------------------------------------------


def _sort_key(obj: Mapping[str, Any]) -> tuple[str, str]:
    return (
        _field_text(_lookup(obj, ("metadata", "namespace"))),
        _field_text(_lookup(obj, ("metadata", "name"))),
    )


@dataclass
class _Collections:
    """Full collections of one class: ``(collected_at_ns, objects)``, by time."""

    batches: list[tuple[int, list[dict[str, Any]]]] = field(default_factory=list)

    def at(self, now_ns: int) -> tuple[int, list[dict[str, Any]]] | None:
        latest = None
        for collected, objects in self.batches:
            if collected <= now_ns:
                latest = (collected, objects)
            else:
                break
        return latest


@dataclass(frozen=True)
class _WatchRecord:
    at_ns: int
    kind: str
    key: str
    obj: dict[str, Any]


@dataclass(frozen=True)
class LogRecord:
    at_ns: int
    text: str


@dataclass
class _PodLogs:
    deployment: str | None
    records: list[LogRecord] = field(default_factory=list)


class FakeCluster:
    """The replayed cluster of one scenario, answering GET requests."""

    def __init__(
        self,
        *,
        objects: Mapping[TypeKey, _Collections],
        types: Mapping[TypeKey, ResourceType],
        watch: Sequence[_WatchRecord],
        logs: Mapping[tuple[str, str], _PodLogs],
        token: str,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        if not token or not token.isprintable():
            raise ValueError("token must be a printable non-empty string")
        self._objects = dict(objects)
        self._types = dict(types)
        self._watch = list(watch)
        self._logs = dict(logs)
        self._token = token
        self._clock = clock

    # -- loading ------------------------------------------------------------

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Snapshot,
        *,
        offset_seconds: int,
        token: str,
        clock: Callable[[], int] = time.time_ns,
    ) -> FakeCluster:
        """Load objects, events and logs of ``snapshot`` moved by the offset."""
        shift = offset_seconds * NS
        types: dict[TypeKey, ResourceType] = dict(SEEN_RESOURCES)
        objects: dict[TypeKey, _Collections] = {}
        path = snapshot.tsv_path("objects")
        if path is not None:
            grouped: dict[TypeKey, dict[int, list[dict[str, Any]]]] = {}
            for number, row in enumerate(snapshot.iter_tsv(path), start=1):
                where = f"{path.name} data row {number}"
                at_ns, body = _object_row(row, where, offset_seconds)
                rtype = _object_type(row, body, where)
                key = (rtype.group, rtype.version, rtype.plural)
                grouped.setdefault(key, {}).setdefault(at_ns + shift, []).append(body)
                types.setdefault(key, rtype)
            for key, by_time in grouped.items():
                objects[key] = _Collections(
                    [
                        (at, sorted(items, key=_sort_key))
                        for at, items in sorted(by_time.items())
                    ]
                )
        watch: list[_WatchRecord] = []
        path = snapshot.tsv_path("events")
        if path is not None:
            for number, row in enumerate(snapshot.iter_tsv(path), start=1):
                where = f"{path.name} data row {number}"
                at_ns, note = _object_row(row, where, offset_seconds)
                record = _watch_record(note, at_ns + shift, where)
                if record is not None:
                    watch.append(record)
            watch.sort(key=lambda r: r.at_ns)
        logs: dict[tuple[str, str], _PodLogs] = {}
        path = snapshot.tsv_path("logs")
        if path is not None:
            for number, row in enumerate(snapshot.iter_tsv(path), start=1):
                where = f"{path.name} data row {number}"
                at_ns = _row_time(row, where)
                attrs = _row_literal(row, "ResourceAttributes", where)
                pod = attrs.get("k8s.pod.name")
                namespace = attrs.get("k8s.namespace.name")
                if not (isinstance(pod, str) and pod):
                    raise SnapshotRowError(f"{where}: no k8s.pod.name")
                if not (isinstance(namespace, str) and namespace):
                    raise SnapshotRowError(f"{where}: no k8s.namespace.name")
                text = row.get("Body")
                if text is None:
                    raise SnapshotRowError(f"{where}: no Body column")
                deployment = attrs.get("k8s.deployment.name")
                entry = logs.setdefault(
                    (namespace, pod),
                    _PodLogs(deployment if isinstance(deployment, str) else None),
                )
                entry.records.append(LogRecord(at_ns + shift, text))
            for entry in logs.values():
                entry.records.sort(key=lambda r: r.at_ns)
        return cls(
            objects=objects,
            types=types,
            watch=watch,
            logs=logs,
            token=token,
            clock=clock,
        )

    # -- request handling -----------------------------------------------------

    def respond(self, method: str, target: str, headers: Mapping[str, str]) -> Reply:
        """The reply to one request (``target`` is the path with its query)."""
        if not self._authorized(headers):
            return status_reply(401, "Unauthorized")
        if method != "GET":
            return status_reply(405, f"method {method} is not allowed (read-only)")
        parts = urllib.parse.urlsplit(target)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        try:
            return self._route(parts.path, {k: v[-1] for k, v in query.items()})
        except BadRequest as error:
            return status_reply(400, str(error))

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        value = ""
        for key, item in headers.items():
            if key.lower() == "authorization":
                value = item
        expected = f"Bearer {self._token}"
        return hmac.compare_digest(value.encode(), expected.encode())

    def _route(self, path: str, query: Mapping[str, str]) -> Reply:
        segments = [urllib.parse.unquote(s) for s in path.strip("/").split("/")]
        if len(segments) >= 2 and segments[0] == "api":
            group, version, rest = "", segments[1], segments[2:]
        elif len(segments) >= 3 and segments[0] == "apis":
            group, version, rest = segments[1], segments[2], segments[3:]
        else:
            return status_reply(404, "the server could not find the requested resource")
        namespace: str | None = None
        if len(rest) >= 3 and rest[0] == "namespaces":
            namespace, rest = rest[1], rest[2:]
        if not rest or len(rest) > 3:
            return status_reply(404, "the server could not find the requested resource")
        plural = rest[0]
        if plural == _SECRET_PLURAL:
            return status_reply(
                403, 'secrets is forbidden: replay does not serve resource "secrets"'
            )
        rtype = self._types.get((group, version, plural))
        if rtype is None:
            return status_reply(
                503,
                f"the snapshot never recorded {plural} in {group or 'core'}/{version}",
            )
        if namespace is not None and not rtype.namespaced:
            return status_reply(404, "the server could not find the requested resource")
        if len(rest) == 3:
            if plural == "pods" and rest[2] == "log" and namespace is not None:
                return self._pod_log(namespace, rest[1], query)
            return status_reply(404, "the server could not find the requested resource")
        now_ns = self._clock()
        if len(rest) == 2:
            return self._get_one(rtype, namespace, rest[1], now_ns)
        return self._list(rtype, namespace, query, now_ns)

    def _current(
        self, rtype: ResourceType, at_ns: int
    ) -> tuple[int, list[dict[str, Any]]] | None:
        """The objects of a class as of ``at_ns``; ``None``: not collected yet."""
        if rtype.plural == "events":
            return at_ns, self._replay_events(at_ns)
        collections = self._objects.get((rtype.group, rtype.version, rtype.plural))
        if collections is None:
            return 0, []  # seen in the scenario set, no objects in this one
        return collections.at(at_ns)

    def _replay_events(self, at_ns: int) -> list[dict[str, Any]]:
        live: dict[str, dict[str, Any]] = {}
        for record in self._watch:
            if record.at_ns > at_ns:
                break
            if record.kind == "DELETED":
                live.pop(record.key, None)
            else:
                live[record.key] = record.obj
        return sorted(live.values(), key=_sort_key)

    def _not_collected(self, rtype: ResourceType) -> Reply:
        return status_reply(
            503, f"no full list of {rtype.plural} was collected before this moment"
        )

    def _get_one(
        self, rtype: ResourceType, namespace: str | None, name: str, now_ns: int
    ) -> Reply:
        if rtype.namespaced and namespace is None:
            return status_reply(404, "the server could not find the requested resource")
        current = self._current(rtype, now_ns)
        if current is None:
            return self._not_collected(rtype)
        for obj in current[1]:
            if _lookup(obj, ("metadata", "name")) == name and (
                namespace is None
                or _lookup(obj, ("metadata", "namespace")) == namespace
            ):
                return _json_reply(obj)
        return status_reply(404, f'{rtype.plural} "{name}" not found')

    def _list(
        self,
        rtype: ResourceType,
        namespace: str | None,
        query: Mapping[str, str],
        now_ns: int,
    ) -> Reply:
        labels = parse_label_selector(query.get("labelSelector", ""))
        fields = parse_field_selector(query.get("fieldSelector", ""), rtype.plural)
        limit = _non_negative_int(query.get("limit", "0") or "0", "limit")
        token = query.get("continue", "")
        at_ns, offset = (now_ns, 0) if not token else _decode_continue(token)
        current = self._current(rtype, at_ns)
        if current is None:
            return self._not_collected(rtype)
        collected_at, objects = current
        selected = [
            obj
            for obj in objects
            if (
                namespace is None
                or _lookup(obj, ("metadata", "namespace")) == namespace
            )
            and all(test(_labels(obj)) for test in labels)
            and all(test(obj) for test in fields)
        ]
        if offset > len(selected):
            return status_reply(410, "the provided continue parameter is too old")
        end = len(selected) if limit == 0 else min(len(selected), offset + limit)
        metadata: dict[str, object] = {"resourceVersion": str(collected_at)}
        if end < len(selected):
            metadata["continue"] = _encode_continue(at_ns, end)
            metadata["remainingItemCount"] = len(selected) - end
        return _json_reply(
            {
                "kind": f"{rtype.kind}List",
                "apiVersion": rtype.api_version,
                "metadata": metadata,
                "items": selected[offset:end],
            }
        )

    # -- logs -----------------------------------------------------------------

    def _pod_log(
        self, namespace: str, pod_name: str, query: Mapping[str, str]
    ) -> Reply:
        now_ns = self._clock()
        pods = self._current(self._types[("", "v1", "pods")], now_ns)
        pod = None
        for obj in pods[1] if pods is not None else []:
            if _lookup(obj, ("metadata", "name")) == pod_name and (
                _lookup(obj, ("metadata", "namespace")) == namespace
            ):
                pod = obj
        if pod is None:
            return status_reply(404, f'pods "{pod_name}" not found')
        containers = _container_names(pod)
        container = query.get("container", "")
        if not container:
            app = [c for c in _container_names(pod, init=False)]
            if len(app) != 1:
                return status_reply(
                    400,
                    f"a container name must be specified for pod {pod_name}, "
                    f"choose one of: [{' '.join(app)}]",
                )
            container = app[0]
        if container not in containers:
            return status_reply(
                400, f"container {container} is not valid for pod {pod_name}"
            )
        if _true(query.get("previous")):
            return status_reply(
                400,
                f'previous terminated container "{container}" in pod "{pod_name}" '
                "not found",
            )
        if _true(query.get("follow")):
            return status_reply(400, "follow is not supported by the snapshot replay")
        if "sinceTime" in query and "sinceSeconds" in query:
            raise BadRequest(
                "at most one of sinceTime or sinceSeconds may be specified"
            )
        since_ns: int | None = None
        if "sinceTime" in query:
            since_ns = _parse_since_time(query["sinceTime"])
        elif "sinceSeconds" in query:
            seconds = _non_negative_int(query["sinceSeconds"], "sinceSeconds")
            if seconds < 1:
                raise BadRequest("sinceSeconds must be greater than 0")
            since_ns = now_ns - seconds * NS
        tail = (
            _non_negative_int(query["tailLines"], "tailLines")
            if "tailLines" in query
            else None
        )
        limit_bytes = (
            _non_negative_int(query["limitBytes"], "limitBytes")
            if "limitBytes" in query
            else None
        )
        if limit_bytes is not None and limit_bytes < 1:
            raise BadRequest("limitBytes must be greater than 0")
        stamps = _true(query.get("timestamps"))
        lines = list(
            self._log_lines(namespace, pod_name, pod, container, now_ns, since_ns)
        )
        if tail is not None:
            lines = lines[-tail:] if tail else []
        text = "".join(
            (f"{rfc3339_nano(at)} {line}" if stamps else line) + "\n"
            for at, line in lines
        )
        body = text.encode("utf-8")
        if limit_bytes is not None:
            body = body[:limit_bytes]
        return Reply(200, body, "text/plain")

    def _log_lines(
        self,
        namespace: str,
        pod_name: str,
        pod: Mapping[str, Any],
        container: str,
        now_ns: int,
        since_ns: int | None,
    ) -> Iterable[tuple[int, str]]:
        entry = self._logs.get((namespace, pod_name))
        if entry is None or container != _log_container(pod, entry.deployment):
            return
        for record in entry.records:
            if record.at_ns > now_ns:
                break
            if since_ns is not None and record.at_ns < since_ns:
                continue
            # A record is one write: its own line breaks split it, a final
            # one only ends it.
            for line in record.text.rstrip("\r\n").split("\n"):
                yield record.at_ns, line.rstrip("\r")


def _row_time(row: Mapping[str, str | None], where: str) -> int:
    try:
        return parse_clickhouse_ns(row.get("Timestamp") or "")
    except ValueError:
        raise SnapshotRowError(f"{where}: bad Timestamp") from None


def _row_literal(
    row: Mapping[str, str | None], column: str, where: str
) -> dict[str, Any]:
    text = row.get(column)
    if text is None or not text.strip():
        raise SnapshotRowError(f"{where}: no {column}")
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        raise SnapshotRowError(f"{where}: {column} does not parse") from None
    if not isinstance(value, dict):
        raise SnapshotRowError(f"{where}: {column} is not a mapping")
    return value


def _object_row(
    row: Mapping[str, str | None], where: str, offset_seconds: int
) -> tuple[int, dict[str, Any]]:
    """``(collection time before the offset, shifted Body)`` of an object or
    event row; any missing or unparseable part raises ``SnapshotRowError``."""
    at_ns = _row_time(row, where)
    text = row.get("Body")
    if not text:
        raise SnapshotRowError(f"{where}: no Body")
    try:
        body = json.loads(text)
    except (ValueError, RecursionError):
        raise SnapshotRowError(f"{where}: Body is not JSON") from None
    if not isinstance(body, dict):
        raise SnapshotRowError(f"{where}: Body is not an object")
    shifted = shift_json_times(body, offset_seconds)
    assert isinstance(shifted, dict)
    return at_ns, shifted


def _object_type(
    row: Mapping[str, str | None], body: Mapping[str, Any], where: str
) -> ResourceType:
    """The class of an object row: plural from ``LogAttributes``, group and
    version from the object's own ``apiVersion``."""
    plural = _row_literal(row, "LogAttributes", where).get("k8s.resource.name")
    if not (isinstance(plural, str) and plural):
        raise SnapshotRowError(f"{where}: no k8s.resource.name")
    api_version = body.get("apiVersion")
    kind = body.get("kind")
    name = _lookup(body, ("metadata", "name"))
    if not (isinstance(api_version, str) and api_version):
        raise SnapshotRowError(f"{where}: object has no apiVersion")
    if not (isinstance(kind, str) and kind):
        raise SnapshotRowError(f"{where}: object has no kind")
    if not (isinstance(name, str) and name):
        raise SnapshotRowError(f"{where}: object has no metadata.name")
    group, _, version = api_version.rpartition("/")
    namespaced = isinstance(_lookup(body, ("metadata", "namespace")), str)
    return ResourceType(plural, group, version, kind, namespaced)


def _watch_record(
    note: Mapping[str, Any], at_ns: int, where: str
) -> _WatchRecord | None:
    """One core ``v1`` watch notification. The ``events.k8s.io`` half of the
    stream is dropped on purpose (``None``); anything malformed raises."""
    kind = note.get("type")
    obj = note.get("object")
    if kind not in ("ADDED", "MODIFIED", "DELETED"):
        raise SnapshotRowError(f"{where}: watch type is not ADDED/MODIFIED/DELETED")
    if not isinstance(obj, dict):
        raise SnapshotRowError(f"{where}: watch notification has no object")
    if obj.get("kind") != "Event":
        raise SnapshotRowError(f"{where}: watch object is not an Event")
    api_version = obj.get("apiVersion")
    if api_version == "events.k8s.io/v1":
        return None
    if api_version != "v1":
        raise SnapshotRowError(
            f"{where}: Event apiVersion is not v1 or events.k8s.io/v1"
        )
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        raise SnapshotRowError(f"{where}: Event has no metadata")
    uid, name = metadata.get("uid"), metadata.get("name")
    if isinstance(uid, str) and uid:
        key = uid
    elif isinstance(name, str) and name:
        key = f"{metadata.get('namespace')}/{name}"
    else:
        raise SnapshotRowError(f"{where}: Event has neither uid nor name")
    return _WatchRecord(at_ns, str(kind), key, obj)


def _container_names(pod: Mapping[str, Any], *, init: bool = True) -> list[str]:
    spec = pod.get("spec")
    if not isinstance(spec, Mapping):
        return []
    fields = ("initContainers", "containers") if init else ("containers",)
    names: list[str] = []
    for name in fields:
        entries = spec.get(name)
        if isinstance(entries, list):
            names += [
                e["name"]
                for e in entries
                if isinstance(e, Mapping) and isinstance(e.get("name"), str)
            ]
    return names


def _log_container(pod: Mapping[str, Any], deployment: str | None) -> str | None:
    """L13: the container named like the Deployment, else the first container."""
    app = _container_names(pod, init=False)
    if deployment is not None and deployment in app:
        return deployment
    return app[0] if app else None


def _true(value: str | None) -> bool:
    return value is not None and value.lower() in ("true", "1")


def _non_negative_int(text: str, name: str) -> int:
    if not re.fullmatch(r"\d{1,12}", text):
        raise BadRequest(f"{name} must be a non-negative integer")
    return int(text)


_SINCE_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})"
)


def _parse_since_time(text: str) -> int:
    if _SINCE_RE.fullmatch(text) is None:
        raise BadRequest("sinceTime must be an RFC 3339 time")
    return parse_rfc3339_ns(text)


def rfc3339_nano(at_ns: int) -> str:
    """Go's ``time.RFC3339Nano`` in UTC (trailing zeros of the fraction cut)."""
    seconds, nanos = divmod(at_ns, NS)
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
    frac = f"{nanos:09d}".rstrip("0")
    return f"{base}.{frac}Z" if frac else f"{base}Z"


# -- TLS and the HTTP server -------------------------------------------------------


def generate_self_signed_cert(directory: str | os.PathLike[str]) -> tuple[Path, Path]:
    """A one-day self-signed certificate for 127.0.0.1 and ::1, by ``openssl`` (L14).

    Returns ``(certificate, key)``; the certificate is also the CA file the
    tools trust. The key is written with mode 0600 into ``directory``, which
    the caller owns (a temporary directory; never commit it).
    """
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    cert, key = base / "fake-k8s.crt", base / "fake-k8s.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", "1",
            "-subj", "/CN=opspilot-itbench-fake-k8s",
            "-addext", "subjectAltName=IP:127.0.0.1,IP:::1",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,digitalSignature,keyCertSign",
            "-addext", "extendedKeyUsage=serverAuth",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )  # fmt: skip
    os.chmod(key, 0o600)
    return cert, key


def _require_loopback(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"bind address must be a loopback IP, got {host!r}") from None
    if not address.is_loopback:
        raise ValueError(f"bind address must be a loopback IP, got {host!r}")


class _Handler(BaseHTTPRequestHandler):
    server: _TLSServer
    protocol_version = "HTTP/1.1"

    def _answer(self) -> None:
        reply = self.server.cluster.respond(self.command, self.path, dict(self.headers))
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(reply.body)

    do_GET = _answer
    do_HEAD = _answer
    do_POST = _answer
    do_PUT = _answer
    do_PATCH = _answer
    do_DELETE = _answer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.verbose:
            sys.stderr.write("fake-k8s: " + (format % args) + "\n")


class _TLSServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        cluster: FakeCluster,
        context: ssl.SSLContext,
        *,
        verbose: bool,
    ) -> None:
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.cluster = cluster
        self.context = context
        self.verbose = verbose
        super().__init__(address, _Handler)

    def get_request(self) -> tuple[socket.socket, Any]:
        sock, addr = super().get_request()
        # The handshake runs on first read, in the handler's own thread.
        return (
            self.context.wrap_socket(
                sock, server_side=True, do_handshake_on_connect=False
            ),
            addr,
        )

    def handle_error(self, request: Any, client_address: Any) -> None:
        if self.verbose:
            super().handle_error(request, client_address)


@dataclass
class RunningServer:
    """A started fake API server; ``close()`` stops it and joins its thread."""

    url: str
    ca_file: Path
    _server: _TLSServer
    _thread: threading.Thread

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    def __enter__(self) -> RunningServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def serve(
    cluster: FakeCluster,
    *,
    cert_file: str | os.PathLike[str],
    key_file: str | os.PathLike[str],
    host: str = "127.0.0.1",
    port: int = 0,
    verbose: bool = False,
) -> RunningServer:
    """Start the server on a loopback address in a background thread."""
    _require_loopback(host)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    server = _TLSServer((host, port), cluster, context, verbose=verbose)
    bound = server.server_address[1]
    shown = f"[{host}]" if ":" in host else host
    thread = threading.Thread(target=server.serve_forever, name="fake-k8s", daemon=True)
    thread.start()
    return RunningServer(f"https://{shown}:{bound}", Path(cert_file), server, thread)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", help="one scenario directory")
    parser.add_argument("--offset-seconds", type=int, required=True)
    parser.add_argument("--token-file", required=True, help="file holding the token")
    parser.add_argument(
        "--cert-dir", required=True, help="directory for the generated certificate"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    _require_loopback(args.host)
    snapshot = load_snapshot(args.snapshot)  # refuses answer files first
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    cluster = FakeCluster.from_snapshot(
        snapshot, offset_seconds=args.offset_seconds, token=token
    )
    cert, key = generate_self_signed_cert(args.cert_dir)
    running = serve(
        cluster,
        cert_file=cert,
        key_file=key,
        host=args.host,
        port=args.port,
        verbose=args.verbose,
    )
    print(json.dumps({"api_url": running.url, "ca_file": str(cert)}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        running.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
