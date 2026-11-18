"""Data-driven stand-in Elasticsearch for the local end-to-end run (#666 H-25).

Not a replay of fixed answers: upstream's ``before_test`` scripts load their
data into it through the same REST calls they make against a real cluster
(index creation, templates, ``_bulk``, ``_doc``, ``_refresh``, ``_count``,
``DELETE``, ``_cluster/settings``), and the product's Elasticsearch tools then
read it back (``_cat/indices``, ``_mapping``, ``_search``). Whatever the
product sends that this module does not implement is a 400
``parsing_exception`` -- never a silent approximation.

* Binds ``127.0.0.1`` only (the lab gate, ``opspilot/tracing.py``).
* ``Authorization: ApiKey <value>`` only. The engineer key given at start may
  do everything; keys issued through ``POST /_security/api_key`` are read-only
  (B1-5's role: read, view_index_metadata, monitor), so writes with them are
  403 ``security_exception`` -- ``scripts/es_readonly_identity.py`` works
  against this server unchanged.
* Search semantics are a small, documented subset (``SUPPORTED_QUERIES``,
  ``SUPPORTED_AGGREGATIONS``); text fields use a lowercase word tokenizer.
  ``profile`` returns a fixed-shape block whose query types follow Lucene's
  class names for the query actually sent.

Usage::

    .venv/bin/python -m scripts.holmes_evals.fake_es --port 29292 --key-file <600 file>

The first stdout line is ``{"url": "http://127.0.0.1:<port>"}``.
"""

from __future__ import annotations

import argparse
import base64
import copy
import fnmatch
import json
import math
import re
import secrets
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

HOST = "127.0.0.1"
MAX_RESULT_WINDOW = 10_000
DEFAULT_MAX_SHARDS = "1000"
#: Per-document bookkeeping bytes added to the stored size (an estimate).
DOC_OVERHEAD_BYTES = 64
INDEX_BASE_BYTES = 225

SUPPORTED_QUERIES = (
    "match_all",
    "match_none",
    "bool",
    "term",
    "terms",
    "match",
    "match_phrase",
    "range",
    "exists",
    "prefix",
    "wildcard",
    "query_string",
)
SUPPORTED_AGGREGATIONS = (
    "min",
    "max",
    "avg",
    "sum",
    "value_count",
    "cardinality",
    "terms",
    "date_histogram",
)
SEARCH_KEYS = frozenset(
    {
        "query",
        "size",
        "from",
        "sort",
        "_source",
        "aggs",
        "aggregations",
        "track_total_hits",
        "timeout",
        "profile",
    }
)
_CAT_DEFAULT = (
    "health",
    "status",
    "index",
    "uuid",
    "pri",
    "rep",
    "docs.count",
    "docs.deleted",
    "store.size",
    "pri.store.size",
)
_CAT_NUMERIC = frozenset(
    {"pri", "rep", "docs.count", "docs.deleted", "store.size", "pri.store.size"}
)
_SIZE_COLUMNS = frozenset({"store.size", "pri.store.size"})
_BYTE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
_BYTE_UNITS["pb"] = 1024**5
_WORD = re.compile(r"\w+", re.UNICODE)
_ISO_DATE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?"
    r"(Z|z|[+-]\d{2}(?::?\d{2})?)?)?$"
)
_SLASH_DATE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})(?: (\d{2}):(\d{2}):(\d{2}))?$")
_DATE_MATH = re.compile(
    r"^now(?P<ops>(?:[+-]\d+[smhdwMy])*)(?:/(?P<round>[smhdwMy]))?$"
)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_UNIT_SECONDS["M"] = 30 * 86400
_UNIT_SECONDS["y"] = 365 * 86400
_INTERVAL = re.compile(r"^(\d+)(ms|s|m|h|d)$")


class EsError(Exception):
    """An Elasticsearch-shaped error answer."""

    def __init__(self, status: int, kind: str, reason: str) -> None:
        self.status = status
        self.kind = kind
        self.reason = reason
        super().__init__(f"{status} {kind}: {reason}")

    def body(self) -> dict[str, Any]:
        cause = {"type": self.kind, "reason": self.reason}
        return {"error": {"root_cause": [cause], **cause}, "status": self.status}


def _bad(reason: str) -> EsError:
    return EsError(400, "parsing_exception", reason)


# -- dates ----------------------------------------------------------------------


def parse_date(value: Any) -> datetime | None:
    """An ES ``strict_date_optional_time`` (or ``yyyy/MM/dd``) string or epoch
    millis as an aware UTC datetime; ``None`` when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        try:
            return datetime.fromtimestamp(value / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    m = _ISO_DATE.match(value)
    if m:
        y, mo, d, h, mi, s, frac, zone = m.groups()
        try:
            micro = int((frac or "0")[:6].ljust(6, "0"))
            moment = datetime(
                int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), micro
            )
        except ValueError:
            return None
        if zone in (None, "Z", "z"):
            return moment.replace(tzinfo=UTC)
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        return (moment - sign * offset).replace(tzinfo=UTC)
    m = _SLASH_DATE.match(value)
    if m:
        y, mo, d, h, mi, s = m.groups()
        try:
            return datetime(
                int(y),
                int(mo),
                int(d),
                int(h or 0),
                int(mi or 0),
                int(s or 0),
                tzinfo=UTC,
            )
        except ValueError:
            return None
    return None


def _date_math(value: Any, now: datetime) -> datetime | None:
    if isinstance(value, str):
        m = _DATE_MATH.match(value)
        if m:
            moment = now
            for sign, number, unit in re.findall(r"([+-])(\d+)([smhdwMy])", m["ops"]):
                delta = timedelta(seconds=int(number) * _UNIT_SECONDS[unit])
                moment = moment + delta if sign == "+" else moment - delta
            return moment
    return parse_date(value)


def _millis(moment: datetime) -> int:
    return int(round(moment.timestamp() * 1000))


def _iso_millis(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{moment.microsecond // 1000:03d}Z"
    )


# -- mappings ---------------------------------------------------------------------


def _infer(value: Any) -> dict[str, Any] | None:
    """Dynamic mapping of one new value (ES defaults, date detection on)."""
    if isinstance(value, list):
        for item in value:
            if item is not None:
                return _infer(item)
        return None
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "long"}
    if isinstance(value, float):
        return {"type": "float"}
    if isinstance(value, dict):
        return {"properties": {}}
    if isinstance(value, str):
        if parse_date(value) is not None:
            return {"type": "date"}
        return {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
        }
    return None


def _extend(properties: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        existing = properties.get(key)
        if existing is None:
            inferred = _infer(value)
            if inferred is None:
                continue
            properties[key] = inferred
            existing = inferred
        inner = value[0] if isinstance(value, list) and value else value
        if isinstance(inner, dict) and "properties" in existing:
            _extend(existing["properties"], inner)


def field_type(properties: Mapping[str, Any], path: str) -> str | None:
    """The mapped type of a dotted path; ``a.keyword`` finds a multi-field."""
    parts = path.split(".")
    node: Mapping[str, Any] | None = {"properties": properties}
    for i, part in enumerate(parts):
        if node is None:
            return None
        props = node.get("properties")
        child = props.get(part) if isinstance(props, Mapping) else None
        if child is None:
            fields = node.get("fields")
            if isinstance(fields, Mapping) and i == len(parts) - 1 and part in fields:
                return str(fields[part].get("type"))
            return None
        node = child
    if node is None:
        return None
    if "type" in node:
        return str(node["type"])
    return "object" if "properties" in node else None


def _parent_of_multifield(properties: Mapping[str, Any], path: str) -> str | None:
    head, _, tail = path.rpartition(".")
    if not head:
        return None
    parts = head.split(".")
    node: Any = {"properties": properties}
    for part in parts:
        props = node.get("properties") if isinstance(node, Mapping) else None
        node = props.get(part) if isinstance(props, Mapping) else None
        if node is None:
            return None
    fields = node.get("fields") if isinstance(node, Mapping) else None
    return head if isinstance(fields, Mapping) and tail in fields else None


def _date_paths(properties: Mapping[str, Any], prefix: str = "") -> list[str]:
    out = []
    for name, spec in properties.items():
        if not isinstance(spec, Mapping):
            continue
        path = f"{prefix}{name}"
        if spec.get("type") in ("date", "date_nanos"):
            out.append(path)
        inner = spec.get("properties")
        if isinstance(inner, Mapping):
            out.extend(_date_paths(inner, path + "."))
    return out


def values_at(source: Any, path: str) -> list[Any]:
    """All leaf values at a dotted path (objects and arrays flattened)."""
    if not path:
        return [source]
    if isinstance(source, list):
        out: list[Any] = []
        for item in source:
            out.extend(values_at(item, path))
        return out
    if not isinstance(source, Mapping):
        return []
    if path in source:
        value = source[path]
        return (
            [v for v in value if v is not None]
            if isinstance(value, list)
            else ([] if value is None else [value])
        )
    head, dot, rest = path.partition(".")
    if dot and head in source:
        return values_at(source[head], rest)
    # Literal dotted keys at this level ("a.b": ...).
    for key, value in source.items():
        if isinstance(key, str) and path.startswith(key + "."):
            return values_at(value, path[len(key) + 1 :])
    return []


def tokens(text: Any) -> list[str]:
    return _WORD.findall(str(text).lower())


# -- index state --------------------------------------------------------------------


@dataclass
class _Index:
    name: str
    uuid: str
    created: datetime
    shards: int = 1
    replicas: int = 1
    properties: dict[str, Any] = field(default_factory=dict)
    docs: dict[str, dict[str, Any]] = field(default_factory=dict)
    deleted_bytes: int = 0
    deleted_docs: int = 0
    seq: int = 0

    def store_bytes(self) -> int:
        live = sum(
            len(json.dumps(d, separators=(",", ":"))) + DOC_OVERHEAD_BYTES
            for d in self.docs.values()
        )
        return INDEX_BASE_BYTES + live + self.deleted_bytes

    def health(self) -> str:
        return "green" if self.replicas == 0 else "yellow"


def _settings(body: Mapping[str, Any]) -> tuple[int | None, int | None]:
    settings = body.get("settings") or {}
    if not isinstance(settings, Mapping):
        raise _bad("[settings] must be an object")
    flat: dict[str, Any] = {}
    for key, value in settings.items():
        if key == "index" and isinstance(value, Mapping):
            for inner, v in value.items():
                flat[f"index.{inner}"] = v
        else:
            flat[key if key.startswith("index.") else f"index.{key}"] = value
    shards = flat.get("index.number_of_shards")
    replicas = flat.get("index.number_of_replicas")
    return (
        None if shards is None else int(shards),
        None if replicas is None else int(replicas),
    )


def _mapping_properties(body: Mapping[str, Any]) -> dict[str, Any]:
    mappings = body.get("mappings") or {}
    if not isinstance(mappings, Mapping):
        raise _bad("[mappings] must be an object")
    props = mappings.get("properties") or {}
    if not isinstance(props, Mapping):
        raise _bad("[properties] must be an object")
    return copy.deepcopy(dict(props))


def _human_bytes(n: int) -> str:
    for unit in ("pb", "tb", "gb", "mb", "kb"):
        size = _BYTE_UNITS[unit]
        if n >= size:
            text = f"{n / size:.1f}".rstrip("0").rstrip(".")
            return f"{text}{unit}"
    return f"{n}b"


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), inner, out)
    else:
        out[prefix] = None if value is None else str(value)


# -- the store ------------------------------------------------------------------------


Answer = tuple[int, Any]


class FakeElasticsearch:
    """In-memory state behind :func:`serve`; :meth:`handle` is the whole API."""

    def __init__(
        self, engineer_key: str, *, now: Callable[[], datetime] | None = None
    ) -> None:
        if not engineer_key:
            raise ValueError("ENGINEER_KEY_EMPTY")
        self._engineer = engineer_key
        self._read_only: set[str] = set()
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self.indices: dict[str, _Index] = {}
        self.templates: dict[str, dict[str, Any]] = {}
        self.cluster: dict[str, dict[str, str]] = {"persistent": {}, "transient": {}}

    # -- entry point ---------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        headers: Mapping[str, str],
        body: bytes,
    ) -> Answer:
        """One request; returns ``(status, JSON value or text)``."""
        try:
            with self._lock:
                return self._route(method.upper(), path, query, headers, body)
        except EsError as error:
            return error.status, error.body()

    def _auth(self, headers: Mapping[str, str]) -> bool:
        """True for the engineer, False for a read-only key; raises 401."""
        value = ""
        for key, inner in headers.items():
            if key.lower() == "authorization":
                value = inner
        scheme, _, token = value.partition(" ")
        if scheme != "ApiKey" or not token:
            raise EsError(401, "security_exception", "missing authentication")
        if secrets.compare_digest(token, self._engineer):
            return True
        if token in self._read_only:
            return False
        raise EsError(401, "security_exception", "unable to authenticate")

    def _route(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        headers: Mapping[str, str],
        raw: bytes,
    ) -> Answer:
        engineer = self._auth(headers)
        parts = [unquote(p) for p in path.split("/") if p]
        q = {k: v[-1] for k, v in query.items() if v}

        def write() -> None:
            if not engineer:
                raise EsError(
                    403,
                    "security_exception",
                    f"action [{method} {path}] is unauthorized",
                )

        if not parts:
            if method in ("GET", "HEAD"):
                return 200, {"name": "fake-es", "version": {"number": "8.15.0"}}
            raise _bad(f"no handler for [{method} /]")
        head = parts[0]
        if head == "_security" and parts[1:] == ["api_key"] and method == "POST":
            write()
            return self._api_key(_json(raw))
        if head == "_cluster" and parts[1:] == ["settings"]:
            if method == "GET":
                return self._get_settings(q)
            if method == "PUT":
                write()
                return self._put_settings(_json(raw))
        if head == "_cat" and len(parts) in (2, 3) and parts[1] == "indices":
            if method == "GET":
                return self._cat_indices(parts[2] if len(parts) == 3 else "*", q)
        if head == "_bulk" and len(parts) == 1 and method in ("POST", "PUT"):
            write()
            return self._bulk(None, raw)
        if head == "_refresh" and len(parts) == 1 and method in ("POST", "GET"):
            write()
            return 200, self._shards(list(self.indices.values()))
        if head == "_index_template" and len(parts) == 2:
            return self._template(method, parts[1], raw, write)
        if head.startswith("_"):
            raise EsError(400, "illegal_argument_exception", f"no handler for [{path}]")

        expr = head
        rest = parts[1:]
        if not rest:
            if method == "PUT":
                write()
                return self._create_index(expr, _json(raw))
            if method == "DELETE":
                write()
                return self._delete(expr)
            if method in ("GET", "HEAD"):
                found = self._resolve(expr)
                return 200, {
                    i.name: {
                        "aliases": {},
                        "mappings": {"properties": i.properties},
                        "settings": {
                            "index": {
                                "number_of_shards": str(i.shards),
                                "number_of_replicas": str(i.replicas),
                            }
                        },
                    }
                    for i in found
                }
        action = rest[0]
        if action == "_doc" and method in ("POST", "PUT") and len(rest) <= 2:
            write()
            return self._index_doc(
                expr, rest[1] if len(rest) == 2 else None, _json(raw)
            )
        if action == "_bulk" and len(rest) == 1 and method in ("POST", "PUT"):
            write()
            return self._bulk(expr, raw)
        if action == "_refresh" and len(rest) == 1 and method in ("POST", "GET"):
            write()
            return 200, self._shards(self._resolve(expr))
        if action == "_forcemerge" and len(rest) == 1 and method == "POST":
            write()
            found = self._resolve(expr)
            for index in found:
                index.deleted_bytes = 0
                index.deleted_docs = 0
            return 200, self._shards(found)
        if action == "_count" and len(rest) == 1 and method in ("GET", "POST"):
            return self._count(expr, _json(raw))
        if action == "_mapping" and len(rest) == 1 and method == "GET":
            return 200, {
                i.name: {"mappings": {"properties": i.properties}}
                for i in sorted(self._resolve(expr), key=lambda i: i.name)
            }
        if action == "_search" and len(rest) == 1 and method in ("GET", "POST"):
            return self._search(expr, _json(raw), q)
        raise EsError(400, "illegal_argument_exception", f"no handler for [{path}]")

    # -- security, cluster ---------------------------------------------------

    def _api_key(self, body: Any) -> Answer:
        name = body.get("name") if isinstance(body, Mapping) else None
        key_id = secrets.token_hex(10)
        raw = secrets.token_urlsafe(16)
        encoded = base64.b64encode(f"{key_id}:{raw}".encode()).decode("ascii")
        self._read_only.add(encoded)
        return 200, {"id": key_id, "name": name, "api_key": raw, "encoded": encoded}

    def _get_settings(self, q: Mapping[str, str]) -> Answer:
        out: dict[str, Any] = {
            "persistent": dict(self.cluster["persistent"]),
            "transient": dict(self.cluster["transient"]),
        }
        if q.get("include_defaults") == "true":
            out["defaults"] = {"cluster.max_shards_per_node": DEFAULT_MAX_SHARDS}
        if q.get("flat_settings") != "true":
            out = {k: _nest(v) for k, v in out.items()}
        return 200, out

    def _put_settings(self, body: Any) -> Answer:
        if not isinstance(body, Mapping):
            raise _bad("request body is required")
        for scope in ("persistent", "transient"):
            value = body.get(scope)
            if value is None:
                continue
            flat: dict[str, Any] = {}
            _flatten("", value, flat)
            for key, inner in flat.items():
                if inner is None:
                    self.cluster[scope].pop(key, None)
                else:
                    self.cluster[scope][key] = inner
        return 200, {"acknowledged": True, **{k: v for k, v in self.cluster.items()}}

    # -- templates and indices -------------------------------------------------

    def _template(
        self, method: str, name: str, raw: bytes, write: Callable[[], None]
    ) -> Answer:
        if method == "PUT":
            write()
            body = _json(raw)
            if not isinstance(body, Mapping) or not body.get("index_patterns"):
                raise _bad("[index_patterns] is required")
            patterns = body["index_patterns"]
            self.templates[name] = {
                "index_patterns": [patterns]
                if isinstance(patterns, str)
                else list(patterns),
                "priority": int(body.get("priority") or 0),
                "template": copy.deepcopy(body.get("template") or {}),
            }
            return 200, {"acknowledged": True}
        if method == "DELETE":
            write()
            if name not in self.templates:
                raise EsError(
                    404,
                    "resource_not_found_exception",
                    f"index_template [{name}] missing",
                )
            del self.templates[name]
            return 200, {"acknowledged": True}
        if method == "GET":
            if name not in self.templates:
                raise EsError(404, "resource_not_found_exception", name)
            return 200, {
                "index_templates": [
                    {"name": name, "index_template": self.templates[name]}
                ]
            }
        raise EsError(400, "illegal_argument_exception", f"no handler for {method}")

    def _new_index(self, name: str, body: Mapping[str, Any]) -> _Index:
        if (
            not name
            or name != name.lower()
            or name.startswith(("-", "_", "+"))
            or any(c in name for c in '\\/*?"<>| ,#:')
        ):
            raise EsError(
                400, "invalid_index_name_exception", f"Invalid index name [{name}]"
            )
        index = _Index(name=name, uuid=secrets.token_urlsafe(16), created=self._now())
        matching = [
            t
            for t in self.templates.values()
            if any(fnmatch.fnmatchcase(name, p) for p in t["index_patterns"])
        ]
        if matching:
            best = max(matching, key=lambda t: t["priority"])["template"]
            shards, replicas = _settings(best)
            index.shards = shards or index.shards
            index.replicas = index.replicas if replicas is None else replicas
            index.properties = _mapping_properties(best)
        shards, replicas = _settings(body)
        index.shards = shards or index.shards
        index.replicas = index.replicas if replicas is None else replicas
        index.properties.update(_mapping_properties(body))
        self.indices[name] = index
        return index

    def _create_index(self, name: str, body: Any) -> Answer:
        if name in self.indices:
            raise EsError(
                400,
                "resource_already_exists_exception",
                f"index [{name}] already exists",
            )
        self._new_index(name, body if isinstance(body, Mapping) else {})
        return 200, {"acknowledged": True, "shards_acknowledged": True, "index": name}

    def _delete(self, expr: str) -> Answer:
        found = self._resolve(expr)
        for index in found:
            del self.indices[index.name]
        return 200, {"acknowledged": True}

    def _resolve(self, expr: str, *, allow_missing: bool = False) -> list[_Index]:
        """Indices named by a comma list of names and wildcards; a concrete
        missing name is 404 (wildcards may match nothing)."""
        out: dict[str, _Index] = {}
        for part in expr.split(","):
            if part in ("_all", "*"):
                out.update(self.indices)
            elif "*" in part or "?" in part:
                for name, index in self.indices.items():
                    if fnmatch.fnmatchcase(name, part):
                        out[name] = index
            elif part in self.indices:
                out[part] = self.indices[part]
            elif not allow_missing:
                raise EsError(
                    404, "index_not_found_exception", f"no such index [{part}]"
                )
        return [out[n] for n in sorted(out)]

    @staticmethod
    def _shards(indices: Sequence[_Index]) -> dict[str, Any]:
        total = sum(i.shards for i in indices)
        return {"_shards": {"total": total, "successful": total, "failed": 0}}

    # -- documents ---------------------------------------------------------------

    def _store(
        self, index_name: str, doc_id: str | None, source: Any, op: str
    ) -> dict[str, Any]:
        if not isinstance(source, Mapping):
            raise EsError(400, "mapper_parsing_exception", "failed to parse")
        index = self.indices.get(index_name) or self._new_index(index_name, {})
        props = copy.deepcopy(index.properties)
        _extend(props, source)
        for path in _date_paths(props):
            for value in values_at(source, path):
                if parse_date(value) is None:
                    raise EsError(
                        400,
                        "document_parsing_exception",
                        f"failed to parse field [{path}] of type [date]",
                    )
        index.properties = props
        if doc_id is None:
            doc_id = secrets.token_urlsafe(15)[:20]
        result, status = "created", 201
        if doc_id in index.docs:
            if op == "create":
                raise EsError(
                    409,
                    "version_conflict_engine_exception",
                    f"[{doc_id}]: document already exists",
                )
            old = index.docs[doc_id]
            index.deleted_bytes += (
                len(json.dumps(old, separators=(",", ":"))) + DOC_OVERHEAD_BYTES
            )
            index.deleted_docs += 1
            result, status = "updated", 200
        index.seq += 1
        index.docs[doc_id] = copy.deepcopy(dict(source))
        return {
            "_index": index_name,
            "_id": doc_id,
            "_version": 1,
            "result": result,
            "_shards": {"total": 1, "successful": 1, "failed": 0},
            "_seq_no": index.seq,
            "status": status,
        }

    def _index_doc(self, expr: str, doc_id: str | None, body: Any) -> Answer:
        item = self._store(expr, doc_id, body, "index")
        status = item.pop("status")
        return status, item

    def _bulk(self, default_index: str | None, raw: bytes) -> Answer:
        lines = [line for line in raw.decode("utf-8").split("\n") if line.strip()]
        items: list[dict[str, Any]] = []
        errors = False
        i = 0
        while i < len(lines):
            try:
                action_doc = json.loads(lines[i])
            except ValueError:
                raise _bad("Malformed action/metadata line") from None
            if not isinstance(action_doc, Mapping) or len(action_doc) != 1:
                raise _bad("Malformed action/metadata line")
            op, meta = next(iter(action_doc.items()))
            meta = meta or {}
            index_name = meta.get("_index") or default_index
            doc_id = meta.get("_id")
            i += 1
            if op not in ("index", "create", "delete"):
                raise EsError(
                    400, "illegal_argument_exception", f"Unsupported bulk action [{op}]"
                )
            if not index_name:
                raise EsError(
                    400, "action_request_validation_exception", "index is missing"
                )
            if op == "delete":
                index = self.indices.get(index_name)
                found = index is not None and doc_id in index.docs
                if found and index is not None:
                    old = index.docs.pop(str(doc_id))
                    index.deleted_bytes += len(json.dumps(old)) + DOC_OVERHEAD_BYTES
                    index.deleted_docs += 1
                items.append(
                    {"delete": {"_index": index_name, "_id": doc_id,
                                "result": "deleted" if found else "not_found",
                                "status": 200 if found else 404}}
                )  # fmt: skip
                continue
            if i >= len(lines):
                raise _bad("The bulk request must be terminated by a newline")
            try:
                source = json.loads(lines[i])
            except ValueError:
                raise _bad("Malformed document line") from None
            i += 1
            try:
                items.append({op: self._store(index_name, doc_id, source, op)})
            except EsError as error:
                errors = True
                items.append(
                    {op: {"_index": index_name, "_id": doc_id, "status": error.status,
                          "error": {"type": error.kind, "reason": error.reason}}}
                )  # fmt: skip
        return 200, {"took": 1, "errors": errors, "items": items}

    # -- reading ---------------------------------------------------------------------

    def _cat_indices(self, pattern: str, q: Mapping[str, str]) -> Answer:
        found = self._resolve(pattern)
        unit = q.get("bytes")
        if unit is not None and unit not in _BYTE_UNITS:
            raise EsError(400, "illegal_argument_exception", f"bytes [{unit}] unknown")
        columns = [c for c in (q.get("h") or ",".join(_CAT_DEFAULT)).split(",") if c]
        rows: list[dict[str, Any]] = []
        raw_rows: list[dict[str, Any]] = []
        for index in found:
            if q.get("health") and index.health() != q["health"]:
                continue
            size = index.store_bytes()
            raw = {
                "health": index.health(),
                "status": "open",
                "index": index.name,
                "uuid": index.uuid,
                "pri": index.shards,
                "rep": index.replicas,
                "docs.count": len(index.docs),
                "docs.deleted": index.deleted_docs,
                "store.size": size
                * (1 + (index.replicas if index.health() == "green" else 0)),
                "pri.store.size": size,
                "creation.date": _millis(index.created),
                "creation.date.string": _iso_millis(index.created),
            }
            unknown = [c for c in columns if c not in raw]
            if unknown:
                raise EsError(
                    400,
                    "illegal_argument_exception",
                    f"header [{unknown[0]}] does not exist",
                )
            raw_rows.append(raw)
        for spec in reversed([s for s in (q.get("s") or "").split(",") if s]):
            name, _, order = spec.partition(":")
            if name not in _CAT_DEFAULT + ("creation.date", "creation.date.string"):
                raise EsError(
                    400,
                    "illegal_argument_exception",
                    f"Unable to sort by unknown sort key `{name}`",
                )
            raw_rows.sort(key=_by_column(name), reverse=order == "desc")
        for raw in raw_rows:
            row = {}
            for column in columns:
                value: Any = raw[column]
                if column in _SIZE_COLUMNS:
                    value = (
                        str(value // _BYTE_UNITS[unit])
                        if unit
                        else _human_bytes(int(value))
                    )
                row[column] = str(value)
            rows.append(row)
        if q.get("format") == "json":
            return 200, rows
        widths = {
            c: max([len(r[c]) for r in rows] + [len(c) if q.get("v") == "true" else 0])
            for c in columns
        }
        lines = []
        if q.get("v") == "true":
            lines.append(" ".join(c.ljust(widths[c]) for c in columns).rstrip())
        for row in rows:
            lines.append(" ".join(row[c].ljust(widths[c]) for c in columns).rstrip())
        return 200, "".join(line + "\n" for line in lines)

    def _count(self, expr: str, body: Any) -> Answer:
        found = self._resolve(expr, allow_missing="*" in expr)
        query = body.get("query") if isinstance(body, Mapping) else None
        n = 0
        for index in found:
            for _, source in index.docs.items():
                if query is None or self._matches(index, source, query):
                    n += 1
        return 200, {"count": n, **self._shards(found)}

    def _search(self, expr: str, body: Any, q: Mapping[str, str]) -> Answer:
        if body is None:
            body = {}
        if not isinstance(body, Mapping):
            raise _bad("request body must be an object")
        unknown = sorted(set(body) - SEARCH_KEYS)
        if unknown:
            raise _bad(f"Unknown key for a START_OBJECT in [{unknown[0]}].")
        found = self._resolve(expr, allow_missing="*" in expr)
        size = int(body.get("size", q.get("size", 10)))
        offset = int(body.get("from", q.get("from", 0)))
        if size < 0 or offset < 0:
            raise EsError(
                400, "illegal_argument_exception", "[size] and [from] must be >= 0"
            )
        if size + offset > MAX_RESULT_WINDOW:
            raise EsError(
                400,
                "illegal_argument_exception",
                f"Result window is too large, from + size must be less than or equal to: [{MAX_RESULT_WINDOW}]",
            )
        query = body.get("query", {"match_all": {}})
        matched: list[tuple[_Index, str, dict[str, Any]]] = []
        for index in found:
            for doc_id, source in index.docs.items():
                if self._matches(index, source, query):
                    matched.append((index, doc_id, source))
        sort = body.get("sort")
        sort_keys = self._sort_specs(sort, found) if sort is not None else []
        sorted_rows = self._sorted(matched, sort_keys)
        page = sorted_rows[offset : offset + size]
        hits = []
        for index, doc_id, source, sort_values in page:
            hit: dict[str, Any] = {
                "_index": index.name,
                "_id": doc_id,
                "_score": None if sort_keys else 1.0,
            }
            filtered = _source_filter(source, body.get("_source", True))
            if filtered is not None:
                hit["_source"] = filtered
            if sort_keys:
                hit["sort"] = sort_values
            hits.append(hit)
        total = len(matched)
        track = body.get("track_total_hits", MAX_RESULT_WINDOW)
        relation = "eq"
        if track is not True:
            cap = (
                MAX_RESULT_WINDOW
                if track in (None,)
                else int(track)
                if not isinstance(track, bool)
                else 0
            )
            if track is False:
                total, relation = 0, "gte"
            elif total > cap:
                total, relation = cap, "gte"
        out: dict[str, Any] = {
            "took": 1,
            "timed_out": False,
            "_shards": {
                "total": sum(i.shards for i in found),
                "successful": sum(i.shards for i in found),
                "skipped": 0,
                "failed": 0,
            },
            "hits": {
                "total": {"value": total, "relation": relation},
                "max_score": None if sort_keys or not hits else 1.0,
                "hits": hits,
            },
        }
        aggs = body.get("aggs", body.get("aggregations"))
        if aggs is not None:
            out["aggregations"] = self._aggregate(
                aggs, [(i, s) for i, _, s in matched], found
            )
        if body.get("profile") is True:
            out["profile"] = {
                "shards": [
                    {
                        "id": f"[fake][{index.name}][0]",
                        "searches": [
                            {
                                "query": [_profile_node(query, index)],
                                "rewrite_time": 1000,
                                "collector": [
                                    {
                                        "name": "QueryPhaseCollector",
                                        "reason": "search_query_phase",
                                        "time_in_nanos": 1000,
                                    }
                                ],
                            }
                        ],
                        "aggregations": [],
                    }
                    for index in found
                ]
            }
        return 200, out

    # -- query evaluation ------------------------------------------------------------

    def _matches(self, index: _Index, source: Mapping[str, Any], query: Any) -> bool:
        if not isinstance(query, Mapping) or len(query) != 1:
            raise _bad("query malformed, must start with start_object and have one key")
        kind, spec = next(iter(query.items()))
        if kind == "match_all":
            return True
        if kind == "match_none":
            return False
        if kind == "bool":
            return self._bool(index, source, spec)
        if kind == "exists":
            name = spec.get("field") if isinstance(spec, Mapping) else None
            if not isinstance(name, str):
                raise _bad("[exists] requires [field]")
            return bool(self._values(index, source, name))
        if kind == "query_string":
            return self._query_string(index, source, spec)
        if kind not in SUPPORTED_QUERIES:
            raise _bad(f"unknown query [{kind}]")
        if not isinstance(spec, Mapping) or len(spec) != 1:
            raise _bad(f"[{kind}] query malformed, no field specified")
        name, arg = next(iter(spec.items()))
        ftype = field_type(index.properties, name)
        values = self._values(index, source, name)
        if kind == "term":
            target = arg.get("value") if isinstance(arg, Mapping) else arg
            insensitive = (
                isinstance(arg, Mapping) and arg.get("case_insensitive") is True
            )
            return any(_term_equal(v, target, ftype, insensitive) for v in values)
        if kind == "terms":
            if not isinstance(arg, list):
                raise _bad("[terms] query requires an array of values")
            return any(_term_equal(v, t, ftype, False) for v in values for t in arg)
        if kind in ("match", "match_phrase"):
            text = arg.get("query") if isinstance(arg, Mapping) else arg
            operator = (
                str(arg.get("operator", "or")).lower()
                if isinstance(arg, Mapping)
                else "or"
            )
            if ftype == "text":
                wanted = tokens(text)
                if not wanted:
                    return False
                for value in values:
                    have = tokens(value)
                    if kind == "match_phrase":
                        n = len(wanted)
                        if any(
                            have[i : i + n] == wanted for i in range(len(have) - n + 1)
                        ):
                            return True
                    elif operator == "and":
                        if all(w in have for w in wanted):
                            return True
                    elif any(w in have for w in wanted):
                        return True
                return False
            return any(_term_equal(v, text, ftype, False) for v in values)
        if kind == "range":
            if not isinstance(arg, Mapping):
                raise _bad("[range] query malformed")
            return any(self._in_range(v, arg, ftype) for v in values)
        if kind in ("prefix", "wildcard"):
            pattern = (
                arg.get("value", arg.get("wildcard"))
                if isinstance(arg, Mapping)
                else arg
            )
            if not isinstance(pattern, str):
                raise _bad(f"[{kind}] requires a string value")
            insensitive = (
                isinstance(arg, Mapping) and arg.get("case_insensitive") is True
            )
            if kind == "prefix":
                pattern = _escape_glob(pattern) + "*"
            candidates: list[str] = []
            for value in values:
                candidates.extend(tokens(value) if ftype == "text" else [str(value)])
            if ftype == "text" or insensitive:
                pattern = pattern.lower()
                candidates = [c.lower() for c in candidates]
            return any(fnmatch.fnmatchcase(c, pattern) for c in candidates)
        raise _bad(f"unknown query [{kind}]")

    def _values(self, index: _Index, source: Mapping[str, Any], name: str) -> list[Any]:
        parent = _parent_of_multifield(index.properties, name)
        if parent is not None and field_type(index.properties, name) == "keyword":
            values = values_at(source, parent)
            return [v for v in values if not (isinstance(v, str) and len(v) > 256)]
        return values_at(source, name)

    def _bool(self, index: _Index, source: Mapping[str, Any], spec: Any) -> bool:
        if not isinstance(spec, Mapping):
            raise _bad("[bool] query malformed")
        allowed = {
            "must",
            "filter",
            "should",
            "must_not",
            "minimum_should_match",
            "boost",
        }
        extra = set(spec) - allowed
        if extra:
            raise _bad(f"[bool] query does not support [{sorted(extra)[0]}]")

        def clauses(key: str) -> list[Any]:
            value = spec.get(key)
            if value is None:
                return []
            return list(value) if isinstance(value, list) else [value]

        must = clauses("must") + clauses("filter")
        if not all(self._matches(index, source, c) for c in must):
            return False
        if any(self._matches(index, source, c) for c in clauses("must_not")):
            return False
        should = clauses("should")
        if not should:
            return True
        default = 0 if must else 1
        minimum = spec.get("minimum_should_match", default)
        if isinstance(minimum, str):
            minimum = (
                math.floor(len(should) * int(minimum.rstrip("%")) / 100)
                if minimum.endswith("%")
                else int(minimum)
            )
        return sum(1 for c in should if self._matches(index, source, c)) >= int(minimum)

    def _query_string(
        self, index: _Index, source: Mapping[str, Any], spec: Any
    ) -> bool:
        if not isinstance(spec, Mapping) or not isinstance(spec.get("query"), str):
            raise _bad("[query_string] requires [query]")
        text = spec["query"].strip()
        default_field = spec.get("default_field")
        groups = [g.strip() for g in re.split(r"\s+OR\s+", text)]
        for group in groups:
            parts = [p.strip() for p in re.split(r"\s+AND\s+", group) if p.strip()]
            if all(self._qs_term(index, source, p, default_field) for p in parts):
                return True
        return False

    def _qs_term(
        self, index: _Index, source: Mapping[str, Any], term: str, default_field: Any
    ) -> bool:
        if any(c in term for c in "()[]{}~^"):
            raise _bad(f"query_string construct not supported: [{term}]")
        name, colon, value = term.partition(":")
        value = value.strip().strip('"') if colon else term.strip('"')
        if colon:
            fields = [name.strip()]
        elif isinstance(default_field, str):
            fields = [default_field]
        else:
            fields = sorted(
                p
                for p in _all_paths(index.properties)
                if field_type(index.properties, p) in ("text", "keyword")
            )
        for f in fields:
            ftype = field_type(index.properties, f)
            for v in self._values(index, source, f):
                if "*" in value or "?" in value:
                    cands = tokens(v) if ftype == "text" else [str(v)]
                    if any(
                        fnmatch.fnmatchcase(c.lower(), value.lower()) for c in cands
                    ):
                        return True
                elif ftype == "text":
                    wanted = tokens(value)
                    have = tokens(v)
                    n = len(wanted)
                    if n and any(
                        have[i : i + n] == wanted for i in range(len(have) - n + 1)
                    ):
                        return True
                elif _term_equal(v, value, ftype, False):
                    return True
        return False

    def _in_range(self, value: Any, spec: Mapping[str, Any], ftype: str | None) -> bool:
        now = self._now()
        bounds = {k: spec[k] for k in ("gt", "gte", "lt", "lte") if k in spec}
        extra = set(spec) - {"gt", "gte", "lt", "lte", "format", "time_zone", "boost"}
        if extra:
            raise _bad(f"[range] query does not support [{sorted(extra)[0]}]")
        if ftype in ("date", "date_nanos"):
            have = parse_date(value)
            if have is None:
                return False
            for op, bound in bounds.items():
                limit = _date_math(bound, now)
                if limit is None:
                    raise EsError(
                        400, "parse_exception", f"failed to parse date field [{bound}]"
                    )
                if not _compare(have, op, limit):
                    return False
            return True
        if ftype in (
            "long",
            "integer",
            "short",
            "byte",
            "float",
            "double",
            "half_float",
            "scaled_float",
        ):
            try:
                num = float(value)
                return all(_compare(num, op, float(b)) for op, b in bounds.items())
            except (TypeError, ValueError):
                return False
        return all(_compare(str(value), op, str(b)) for op, b in bounds.items())

    # -- sorting -----------------------------------------------------------------------

    def _sort_specs(
        self, sort: Any, found: Sequence[_Index]
    ) -> list[tuple[str, bool, str | None]]:
        items = sort if isinstance(sort, list) else [sort]
        specs: list[tuple[str, bool, str | None]] = []
        for item in items:
            if isinstance(item, str):
                name, desc = item, item == "_score"
            elif isinstance(item, Mapping) and len(item) == 1:
                name, order = next(iter(item.items()))
                if isinstance(order, Mapping):
                    order = order.get("order", "asc")
                desc = str(order).lower() == "desc"
            else:
                raise _bad("malformed sort")
            ftype = None
            if name not in ("_doc", "_score"):
                types = {field_type(i.properties, name) for i in found} - {None}
                if "text" in types:
                    raise EsError(
                        400,
                        "illegal_argument_exception",
                        f"Text fields are not optimised for operations that require per-document field data like aggregations and sorting, so these operations are disabled by default. Please use a keyword field instead. Alternatively, set fielddata=true on [{name}] in order to load field data by uninverting the inverted index.",
                    )
                ftype = next(iter(types)) if types else None
            specs.append((name, desc, ftype))
        return specs

    def _sorted(
        self,
        rows: list[tuple[_Index, str, dict[str, Any]]],
        specs: list[tuple[str, bool, str | None]],
    ) -> list[tuple[_Index, str, dict[str, Any], list[Any]]]:
        keyed = []
        for position, (index, doc_id, source) in enumerate(rows):
            values: list[Any] = []
            for name, desc, ftype in specs:
                if name in ("_doc", "_score"):
                    values.append(position if name == "_doc" else 1.0)
                    continue
                found = self._values(index, source, name)
                value: Any = None
                if found:
                    picked = (
                        min(found, key=_sortable)
                        if not desc
                        else max(found, key=_sortable)
                    )
                    if ftype in ("date", "date_nanos"):
                        moment = parse_date(picked)
                        value = None if moment is None else _millis(moment)
                    else:
                        value = picked
                values.append(value)
            keyed.append((index, doc_id, source, values))
        for i in reversed(range(len(specs))):
            desc = specs[i][1]
            present = [r for r in keyed if r[3][i] is not None]
            missing = [r for r in keyed if r[3][i] is None]
            present.sort(key=_by_sort_value(i), reverse=desc)
            keyed = present + missing
        return keyed

    # -- aggregations -------------------------------------------------------------------

    def _aggregate(
        self,
        aggs: Any,
        rows: list[tuple[_Index, dict[str, Any]]],
        found: Sequence[_Index],
    ) -> dict[str, Any]:
        if not isinstance(aggs, Mapping):
            raise _bad("[aggs] must be an object")
        out: dict[str, Any] = {}
        for name, spec in aggs.items():
            if not isinstance(spec, Mapping):
                raise _bad(f"aggregation [{name}] must be an object")
            sub = spec.get("aggs", spec.get("aggregations"))
            kinds = [k for k in spec if k not in ("aggs", "aggregations", "meta")]
            if len(kinds) != 1:
                raise _bad(f"Expected one aggregation type for [{name}]")
            kind = kinds[0]
            body = spec[kind]
            if kind not in SUPPORTED_AGGREGATIONS:
                raise _bad(f"Unknown aggregation type [{kind}] did you mean [terms]?")
            if not isinstance(body, Mapping) or not isinstance(body.get("field"), str):
                raise _bad(f"[{kind}] aggregation requires [field]")
            fname = body["field"]
            types = {field_type(i.properties, fname) for i in found} - {None}
            ftype = next(iter(types)) if types else None
            if ftype == "text":
                raise EsError(
                    400,
                    "illegal_argument_exception",
                    f"Text fields are not optimised for operations that require per-document field data like aggregations and sorting, so these operations are disabled by default. Please use a keyword field instead. Alternatively, set fielddata=true on [{fname}] in order to load field data by uninverting the inverted index.",
                )
            is_date = ftype in ("date", "date_nanos")
            per_doc = [(i, s, self._values(i, s, fname)) for i, s in rows]
            flat = [v for _, _, vs in per_doc for v in vs]
            if kind in ("min", "max", "avg", "sum"):
                nums = _numbers(flat, is_date)
                if kind == "sum":
                    value: float | None = float(sum(nums))
                elif not nums:
                    value = None
                elif kind == "avg":
                    value = sum(nums) / len(nums)
                else:
                    value = float(min(nums) if kind == "min" else max(nums))
                result: dict[str, Any] = {"value": value}
                if is_date and value is not None and kind in ("min", "max"):
                    result["value_as_string"] = _iso_millis(
                        datetime.fromtimestamp(value / 1000, UTC)
                    )
                out[name] = result
            elif kind == "value_count":
                out[name] = {"value": len(flat)}
            elif kind == "cardinality":
                out[name] = {
                    "value": len({json.dumps(v, sort_keys=True) for v in flat})
                }
            elif kind == "terms":
                size = int(body.get("size", 10))
                buckets: dict[Any, list[tuple[_Index, dict[str, Any]]]] = {}
                for index, source, vs in per_doc:
                    for v in {json.dumps(x, sort_keys=True): x for x in vs}.values():
                        key = (
                            _millis(parse_date(v) or datetime.min.replace(tzinfo=UTC))
                            if is_date
                            else v
                        )
                        buckets.setdefault(key, []).append((index, source))
                ordered = sorted(
                    buckets.items(), key=lambda kv: (-len(kv[1]), _sortable(kv[0]))
                )
                rows_out = []
                for key, members in ordered[:size]:
                    bucket: dict[str, Any] = {"key": key, "doc_count": len(members)}
                    if is_date:
                        bucket["key_as_string"] = _iso_millis(
                            datetime.fromtimestamp(key / 1000, UTC)
                        )
                    if sub is not None:
                        bucket.update(self._aggregate(sub, members, found))
                    rows_out.append(bucket)
                out[name] = {
                    "doc_count_error_upper_bound": 0,
                    "sum_other_doc_count": sum(len(m) for _, m in ordered[size:]),
                    "buckets": rows_out,
                }
            elif kind == "date_histogram":
                interval = (
                    body.get("fixed_interval")
                    or body.get("calendar_interval")
                    or body.get("interval")
                )
                step = _interval_ms(interval)
                if not is_date:
                    raise EsError(
                        400,
                        "illegal_argument_exception",
                        f"Field [{fname}] is not a date field",
                    )
                groups: dict[int, list[tuple[_Index, dict[str, Any]]]] = {}
                for index, source, vs in per_doc:
                    for v in vs:
                        moment = parse_date(v)
                        if moment is not None:
                            groups.setdefault(
                                _millis(moment) // step * step, []
                            ).append((index, source))
                min_count = int(body.get("min_doc_count", 0))
                rows_out = []
                if groups:
                    start, end = min(groups), max(groups)
                    if (end - start) // step > 100_000:
                        raise EsError(
                            400,
                            "too_many_buckets_exception",
                            "Trying to create too many buckets",
                        )
                    for key in range(start, end + step, step):
                        members = groups.get(key, [])
                        if len(members) < min_count:
                            continue
                        bucket = {
                            "key_as_string": _iso_millis(
                                datetime.fromtimestamp(key / 1000, UTC)
                            ),
                            "key": key,
                            "doc_count": len(members),
                        }
                        if sub is not None:
                            bucket.update(self._aggregate(sub, members, found))
                        rows_out.append(bucket)
                out[name] = {"buckets": rows_out}
        return out


# -- helpers --------------------------------------------------------------------------


def _json(raw: bytes) -> Any:
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _bad("request body is not valid JSON") from None


def _nest(flat: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in flat.items():
        node = out
        *heads, last = key.split(".")
        for head in heads:
            node = node.setdefault(head, {})
        node[last] = value
    return out


def _all_paths(properties: Mapping[str, Any], prefix: str = "") -> Iterable[str]:
    for name, spec in properties.items():
        path = f"{prefix}{name}"
        if isinstance(spec, Mapping):
            if "type" in spec:
                yield path
            inner = spec.get("properties")
            if isinstance(inner, Mapping):
                yield from _all_paths(inner, path + ".")


def _by_column(name: str) -> Callable[[dict[str, Any]], Any]:
    return lambda row: row[name]


def _by_sort_value(i: int) -> Callable[[tuple[Any, ...]], tuple[int, Any]]:
    return lambda row: _sortable(row[3][i])


def _escape_glob(text: str) -> str:
    return re.sub(r"([*?\[\]])", r"[\1]", text)


def _sortable(value: Any) -> tuple[int, Any]:
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (1, str(value))


def _compare(have: Any, op: str, limit: Any) -> bool:
    if op == "gt":
        return bool(have > limit)
    if op == "gte":
        return bool(have >= limit)
    if op == "lt":
        return bool(have < limit)
    return bool(have <= limit)


def _term_equal(value: Any, target: Any, ftype: str | None, insensitive: bool) -> bool:
    if ftype == "text":
        return (
            str(target) in tokens(value)
            if not insensitive
            else str(target).lower() in tokens(value)
        )
    if ftype in ("date", "date_nanos"):
        a, b = parse_date(value), parse_date(target)
        return a is not None and a == b
    if ftype == "boolean":
        return str(value).lower() == str(target).lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value) == float(target)
        except (TypeError, ValueError):
            return False
    if insensitive:
        return str(value).lower() == str(target).lower()
    return str(value) == str(target) if not isinstance(target, (dict, list)) else False


def _numbers(values: Sequence[Any], is_date: bool) -> list[float]:
    out: list[float] = []
    for value in values:
        if is_date:
            moment = parse_date(value)
            if moment is not None:
                out.append(float(_millis(moment)))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(float(value))
        else:
            try:
                out.append(float(value))
            except (TypeError, ValueError):
                continue
    return out


def _interval_ms(value: Any) -> int:
    calendar = {
        "minute": "1m",
        "hour": "1h",
        "day": "1d",
        "1M": None,
        "week": "7d",
        "1w": "7d",
    }
    if isinstance(value, str) and value in calendar and calendar[value]:
        value = calendar[value]
    m = _INTERVAL.match(str(value)) if value is not None else None
    if not m:
        raise _bad(f"date_histogram interval [{value}] not supported")
    n, unit = int(m[1]), m[2]
    factor = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]
    if n <= 0:
        raise _bad("interval must be positive")
    return n * factor


def _source_filter(source: Mapping[str, Any], spec: Any) -> dict[str, Any] | None:
    if spec is True or spec is None:
        return copy.deepcopy(dict(source))
    if spec is False:
        return None
    includes: list[str]
    excludes: list[str] = []
    if isinstance(spec, str):
        includes = [spec]
    elif isinstance(spec, list):
        includes = [str(s) for s in spec]
    elif isinstance(spec, Mapping):
        inc = spec.get("includes", spec.get("include", []))
        exc = spec.get("excludes", spec.get("exclude", []))
        includes = [inc] if isinstance(inc, str) else list(inc)
        excludes = [exc] if isinstance(exc, str) else list(exc)
    else:
        raise _bad("[_source] malformed")
    flat: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, Mapping) and value:
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v)
        else:
            flat[prefix] = value

    walk("", source)
    kept: dict[str, Any] = {}
    for path, value in flat.items():

        def hit(patterns: Sequence[str]) -> bool:
            return any(
                fnmatch.fnmatchcase(path, p) or path.startswith(p + ".")
                for p in patterns
            )

        if includes and not hit(includes):
            continue
        if excludes and hit(excludes):
            continue
        kept[path] = value
    return _nest(kept)


def _profile_node(query: Any, index: _Index) -> dict[str, Any]:
    """Lucene-style profile node of the query as sent."""
    kind, spec = (
        next(iter(query.items()))
        if isinstance(query, Mapping) and query
        else ("match_all", {})
    )
    children: list[dict[str, Any]] = []
    description = kind
    if kind == "bool" and isinstance(spec, Mapping):
        lucene = "BooleanQuery"
        parts = []
        for key, prefix in (
            ("must", "+"),
            ("filter", "#"),
            ("should", ""),
            ("must_not", "-"),
        ):
            clauses = spec.get(key) or []
            for clause in clauses if isinstance(clauses, list) else [clauses]:
                child = _profile_node(clause, index)
                children.append(child)
                parts.append(prefix + child["description"])
        description = " ".join(parts)
    elif kind in ("match_all",):
        lucene, description = "MatchAllDocsQuery", "*:*"
    elif kind == "exists":
        lucene = "FieldExistsQuery"
        description = f"FieldExistsQuery [field={spec.get('field') if isinstance(spec, Mapping) else ''}]"
    elif kind == "query_string":
        lucene, description = (
            "BooleanQuery",
            str(spec.get("query") if isinstance(spec, Mapping) else ""),
        )
    else:
        name, arg = (
            next(iter(spec.items())) if isinstance(spec, Mapping) and spec else ("", "")
        )
        value = (
            arg.get("value", arg.get("query", arg)) if isinstance(arg, Mapping) else arg
        )
        ftype = field_type(index.properties, name)
        if kind == "wildcard":
            lucene = "WildcardQuery"
        elif kind == "prefix":
            lucene = "PrefixQuery"
        elif kind == "terms":
            lucene = "TermInSetQuery"
        elif kind == "range":
            lucene = (
                "IndexOrDocValuesQuery"
                if ftype not in ("keyword", "text")
                else "TermRangeQuery"
            )
        elif kind == "match_phrase":
            lucene = "PhraseQuery"
        elif kind == "match" and ftype == "text" and len(tokens(value)) > 1:
            lucene = "BooleanQuery"
        else:
            lucene = "TermQuery"
        description = f"{name}:{value if not isinstance(value, Mapping) else json.dumps(value, sort_keys=True)}"
    node: dict[str, Any] = {
        "type": lucene,
        "description": description,
        "time_in_nanos": 1000 + 100 * len(children),
        "breakdown": {
            "create_weight": 500,
            "build_scorer": 300,
            "next_doc": 100,
            "score": 100,
        },
    }
    if children:
        node["children"] = children
    return node


# -- HTTP ---------------------------------------------------------------------------------


def serve(store: FakeElasticsearch, host: str, port: int) -> ThreadingHTTPServer:
    """A threaded HTTP server for ``store`` on ``127.0.0.1:port`` (not started)."""
    if host != HOST:
        raise ValueError("FAKE_ES_LOOPBACK_ONLY")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            status, answer = store.handle(
                self.command,
                parts.path,
                parse_qs(parts.query, keep_blank_values=True),
                {k: v for k, v in self.headers.items()},
                body,
            )
            if isinstance(answer, str):
                data, kind = answer.encode("utf-8"), "text/plain; charset=UTF-8"
            else:
                data = json.dumps(
                    answer, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                kind = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            if status == 401:
                self.send_header("WWW-Authenticate", 'ApiKey realm="fake-es"')
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # request lines may carry index names only; keep stdout clean

    server = _QuietServer((host, port), Handler)
    server.daemon_threads = True
    return server


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client closing its keep-alive connection (curl -f on a 4xx) is
        # not an error worth a traceback on stderr.
        return


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--key-file", type=Path, required=True, help="engineer key")
    args = parser.parse_args(argv)
    key = args.key_file.read_text().strip()
    server = serve(FakeElasticsearch(key), HOST, args.port)
    print(json.dumps({"url": f"http://{HOST}:{server.server_address[1]}"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
