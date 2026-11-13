"""Column queries over cluster objects: ``kubernetes_tabular_query`` (A6, #558).

The upstream HolmesGPT ``kubernetes_tabular_query`` (``kubernetes.yaml`` at
``a0009195``) with the same name and parameters, served by the A2 client
(:class:`opspilot.tools.kubernetes_core.KubernetesClient`) instead of a
command-line client's custom-columns printer. Decision table r3 A6-1 to A6-6:

* Reading is A2's, unchanged (A6-1): the static resource table, Secret
  refused first, ``namespace`` omitted is the whole cluster, pages of 500
  under one 4 MiB budget, ``managedFields`` dropped, API errors mapped the
  same way, and events kept only inside the authorized window by the same
  ``list_objects`` filter. One call is one operation.
* ``columns`` is ``HEADER:PATH,...`` in a small subset (A6-2): field names
  separated by dots, each followed by any number of ``[n]`` (an index from
  0) or ``[*]`` (every element), with an optional leading dot and optional
  ``{}`` around the path. A missing field, an index out of range, a null or
  an empty ``[*]`` is ``<none>``; several values are joined by commas. At
  most 20 columns and 2000 characters; ``'`` ``/`` ``;`` and line breaks are
  refused. Anything else is ``INVALID_PARAMS`` with the fixed code
  ``INVALID_COLUMNS`` before anything is sent,
  with the offending column echoed in the refusal body.
* Columns are evaluated here, in Python (A6-3): no jq.
* ``filter_pattern`` is case-sensitive and matched against each row's
  cells joined by three spaces (A6-4), in the bounded regex child of
  ``kubernetes_core.filter_lines``: a pattern still running at the call's
  deadline is killed (``timeout``/``TOOL_TIMEOUT``); one that does not
  compile is matched as a substring (noted).
* The result is ``{header, rows, count, scope, notes}``, each row an array
  of cells in header order (A6-5). No row limit: a view over the
  registered token limit is refused whole by the executor.
* Time (A6-6): objects carry no source time (current state); for
  ``kind=events`` the source time is the first and last own timestamp of
  the events whose rows are returned.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
)
from opspilot.tools.kubernetes_core import (
    _KINDS_TEXT,
    DEFAULT_NAMESPACE,
    ENDPOINT_SELECTOR,
    ERROR_CLASSES,
    MAX_PATTERN_CHARS,
    KubernetesClient,
    check_namespace,
    check_selector,
    event_time,
    filter_lines,
    pattern_problem,
    refused,
    resolve_kind,
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
    "MAX_COLUMNS",
    "MAX_COLUMNS_CHARS",
    "MISSING",
    "ROW_SEPARATOR",
    "TABULAR_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "Column",
    "ColumnProblem",
    "TabularQueryTransport",
    "cell",
    "parse_columns",
    "tabular_registrations",
]

TABULAR_TOOL = "kubernetes_tabular_query"
TOOL_NAMES: tuple[str, ...] = (TABULAR_TOOL,)

#: A6-2.
MAX_COLUMNS = 20
MAX_COLUMNS_CHARS = 2000
#: Upstream's forbidden characters, plus every line break.
_FORBIDDEN = frozenset("'/;\n\r")
#: A refused column is echoed this far.
_ECHO_CHARS = 200
#: The longest index accepted in ``[n]``.
_MAX_INDEX_DIGITS = 6
#: What a missing value is shown as (the upstream printer's text).
MISSING = "<none>"
#: Cells are joined by this for ``filter_pattern`` (the upstream table is
#: padded with at least three spaces between columns).
ROW_SEPARATOR = "   "

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 2 * 1024 * 1024

_FIELD = re.compile(r"[A-Za-z0-9_-]+")
_INDEX = re.compile(r"\[(\*|[0-9]{1,%d})\]" % _MAX_INDEX_DIGITS)


# -- column specification (A6-2) ----------------------------------------------


class _Wildcard:
    """``[*]``: every element of a list."""

    def __repr__(self) -> str:
        return "[*]"


WILDCARD = _Wildcard()
Step = str | int | _Wildcard


@dataclass(frozen=True)
class Column:
    """One ``HEADER:PATH`` entry: the header and the parsed path."""

    header: str
    path: tuple[Step, ...]


@dataclass(frozen=True)
class ColumnProblem:
    """Why ``columns`` was refused, and the entry it was refused at."""

    problem: str
    column: str

    def response(self) -> TransportResponse:
        return TransportResponse(
            body=canonical(
                {
                    "error": "INVALID_COLUMNS",
                    "problem": self.problem,
                    "column": self.column[:_ECHO_CHARS],
                }
            ).encode("utf-8"),
            source_status="INVALID_COLUMNS",
            sent=False,
        )


def _parse_path(text: str) -> tuple[Step, ...] | None:
    if text.startswith("{"):
        if not text.endswith("}") or len(text) < 2:
            return None
        text = text[1:-1].strip()
    if text.startswith("."):
        text = text[1:]
    steps: list[Step] = []
    position = 0
    while True:
        field = _FIELD.match(text, position)
        if field is None:
            return None
        steps.append(field.group())
        position = field.end()
        while True:
            index = _INDEX.match(text, position)
            if index is None:
                break
            value = index.group(1)
            steps.append(WILDCARD if value == "*" else int(value))
            position = index.end()
        if position == len(text):
            return tuple(steps)
        if text[position] != ".":
            return None
        position += 1


def parse_columns(columns: object) -> list[Column] | ColumnProblem:
    """``columns`` as ``Column`` entries, or the first problem found."""
    if not isinstance(columns, str) or not columns.strip():
        return ColumnProblem("empty", "")
    if len(columns) > MAX_COLUMNS_CHARS:
        return ColumnProblem("too_long", columns)
    entries = columns.split(",")
    if len(entries) > MAX_COLUMNS:
        return ColumnProblem("too_many_columns", entries[MAX_COLUMNS])
    parsed: list[Column] = []
    for entry in entries:
        if _FORBIDDEN & set(entry):
            return ColumnProblem("forbidden_character", entry)
        if not entry.isprintable():
            return ColumnProblem("forbidden_character", entry)
        parts = entry.split(":")
        if len(parts) != 2:
            return ColumnProblem("not_header_colon_path", entry)
        header, path_text = parts[0].strip(), parts[1].strip()
        if not header:
            return ColumnProblem("empty_header", entry)
        path = _parse_path(path_text)
        if path is None:
            return ColumnProblem("unsupported_path", entry)
        parsed.append(Column(header, path))
    return parsed


# -- evaluation (A6-3) ----------------------------------------------------------


def _values(obj: object, path: Sequence[Step]) -> list[object]:
    current: list[object] = [obj]
    for step in path:
        following: list[object] = []
        for value in current:
            if isinstance(step, str):
                if isinstance(value, Mapping) and step in value:
                    following.append(value[step])
            elif isinstance(step, int):
                if isinstance(value, list) and step < len(value):
                    following.append(value[step])
            elif isinstance(value, list):
                following.extend(value)
        current = following
    return [value for value in current if value is not None]


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return canonical(value)


def cell(obj: object, column: Column) -> str:
    """One cell: every value the path reaches, joined by commas; ``<none>``
    when it reaches none."""
    values = _values(obj, column.path)
    if not values:
        return MISSING
    return ",".join(_text(value) for value in values)


# -- transport -------------------------------------------------------------------


def _optional_text(params: Mapping[str, object], name: str) -> tuple[bool, str | None]:
    value = params.get(name)
    if value is None:
        return True, None
    if not check_selector(value):
        return False, None
    return True, str(value)


def _optional_pattern(params: Mapping[str, object]) -> tuple[bool, str | None]:
    value = params.get("filter_pattern")
    if value is None or value == "":
        return True, None
    if pattern_problem(value):
        return False, None
    assert isinstance(value, str)
    return True, value


class TabularQueryTransport:
    """Serves ``kubernetes_tabular_query`` over the A2 client (GET only)."""

    def __init__(
        self,
        *,
        client: KubernetesClient,
        application_namespace: str = DEFAULT_NAMESPACE,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._application_namespace = application_namespace
        self._monotonic = monotonic or _monotonic

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool != TABULAR_TOOL:
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
        columns = parse_columns(params.get("columns"))
        if isinstance(columns, ColumnProblem):
            return columns.response()
        ok_pattern, pattern = _optional_pattern(params)
        if not ok_pattern:
            return refused("INVALID_FILTER")
        self._client.token()  # a missing slot fails before anything is sent
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("K8S_ENDPOINT_MISSING")
        listed = self._client.list_objects(
            endpoint=endpoint,
            resource=resource,
            namespace=namespace if isinstance(namespace, str) else None,
            label_selector=label_selector,
            field_selector=field_selector,
            window=request.window,
            deadline=deadline,
            consumer="the columns were read",
        )
        if isinstance(listed, TransportResponse):
            return listed
        notes = list(listed.notes)
        items = listed.items
        rows = [[cell(item, column) for column in columns] for item in items]
        filter_mode: str | None = None
        if pattern is not None and rows:
            decided = filter_lines(
                [ROW_SEPARATOR.join(row) for row in rows],
                include=pattern,
                exclude=None,
                ignore_case=False,
                deadline=deadline,
            )
            if decided is None:
                return TransportResponse(
                    body=canonical({"error": "FILTER_FAILED"}).encode(),
                    source_status="FILTER_FAILED",
                )
            filter_mode = decided.include_mode
            kept = [index for index, keep in enumerate(decided.keep) if keep]
            rows = [rows[index] for index in kept]
            items = [items[index] for index in kept]
            if filter_mode == "substring":
                notes.append(
                    "filter_pattern is not a valid regular expression; it was "
                    "matched as a plain, case-sensitive substring."
                )
        start_at: datetime | None = None
        end_at: datetime | None = None
        if resource.plural == "events":
            start_at, end_at = _event_bounds(items)
        header = [column.header for column in columns]
        scope = {
            "kind": resource.plural,
            "api_group": resource.group,
            "cluster_scoped": not resource.namespaced,
            "namespace": namespace if resource.namespaced else None,
            "all_namespaces": resource.namespaced and namespace is None,
            "label_selector": label_selector,
            "field_selector": field_selector,
            "application_namespace": self._application_namespace,
        }
        body = canonical(
            {
                "header": header,
                "rows": rows,
                "count": len(rows),
                "scope": scope,
                "notes": notes,
            }
        ).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            source_start_at=start_at,
            source_end_at=end_at,
            view_fields={
                "header": header,
                "count": len(rows),
                "scope": scope,
                "notes": notes,
                "total_items_processed": len(listed.items),
                "rows_removed_by_filter": len(listed.items) - len(rows),
                "filter_mode": filter_mode,
                "pages_read": listed.pages,
                "cannot_prove": _CANNOT_PROVE,
            },
        )


def _event_bounds(
    items: Sequence[Mapping[str, Any]],
) -> tuple[datetime | None, datetime | None]:
    """A6-6: the first and last own timestamp of the events returned."""
    stamps = [moment for moment in map(event_time, items) if moment is not None]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


# -- model-visible face and registration ---------------------------------------

_COLUMNS_TEXT = (
    "Column specification format: HEADER:FIELD_PATH,HEADER2:FIELD_PATH2,... "
    "e.g. NAME:.metadata.name,STATUS:.status.phase,IMAGE:.spec.containers[0].image. "
    "A path is field names separated by dots, each optionally followed by "
    "[n] (an index from 0) or [*] (every element; several values are joined "
    "with commas); the path may be wrapped in {}. A missing field, an index "
    "out of range or an empty [*] is shown as <none>. Filter expressions, "
    "recursive descent (..), slices, negative indexes and quoted keys are "
    f"not supported. At most {MAX_COLUMNS} columns and {MAX_COLUMNS_CHARS} "
    "characters; a column outside this subset is refused with "
    "INVALID_PARAMS (INVALID_COLUMNS) before anything is sent."
)

_LABEL_KEYS_TEXT = (
    "LIMITATIONS - not allowed characters in columns: ' / ; and newline. "
    "Label keys containing dots or slashes (e.g. kubernetes.io/arch, "
    "app.kubernetes.io/name) CANNOT be queried with this tool because dots "
    "are path separators and slashes are forbidden characters. Use "
    "kubernetes_jq_query instead for labels with special characters, e.g.: "
    '.items[] | {name: .metadata.name, arch: .metadata.labels["kubernetes.io/arch"]}. '
    "Similarly, use .status.nodeInfo.architecture instead of "
    '.metadata.labels["kubernetes.io/arch"] when possible.'
)

_DESCRIPTION = (
    "Extract specific fields from cluster objects of one kind in tabular "
    "format with optional filtering. Returns header (the column names), "
    "rows (one array of cells per object, in header order), count (number "
    "of rows), scope (the kind, namespace and selectors actually queried) "
    "and notes (e.g. a namespace ignored for a cluster-scoped kind, events "
    "dropped outside the authorized window). If you know the namespace or "
    "labels of the resources you need, pass namespace, label_selector and/or "
    "field_selector so filtering happens server-side; unscoped queries read "
    "the whole cluster and may hit the 4 MiB read limit. "
    + _COLUMNS_TEXT
    + " Optional filter_pattern is a case-sensitive regular expression "
    "(Python re, like grep -E) matched against each row's cells joined by "
    'three spaces, e.g. filter_pattern="Running"; a pattern that does not '
    "compile is matched as a plain substring. "
    + _LABEL_KEYS_TEXT
    + f" kind is a plural resource name from this list, exactly as written: "
    f"{_KINDS_TEXT}. Events are kind=events. secrets cannot be read: asking "
    "for them is denied with RESOURCE_NOT_ALLOWED and nothing is sent. Omit "
    "namespace to read the whole cluster; a cluster-scoped kind ignores "
    "namespace. Objects are their state at the moment of the query, not at "
    "the time of the incident: they cannot prove what the cluster looked "
    "like earlier. Events are only those whose own timestamp lies inside the "
    "authorized window. More than 4 MiB of objects refuses the whole call "
    "with RESULT_TOO_LARGE and no partial result. HTTP 401/403 from the "
    "cluster is denied with SOURCE_FORBIDDEN."
)

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": TABULAR_TOOL,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": (
                            "Plural resource name, for example pods, "
                            "deployments or events."
                        ),
                    },
                    "columns": {
                        "type": "string",
                        "description": (
                            "HEADER:FIELD_PATH pairs separated by commas, e.g. "
                            "NAME:.metadata.name,STATUS:.status.phase,"
                            "IMAGE:.spec.containers[*].image"
                        ),
                    },
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Only query resources in this namespace. Ignored "
                            "for cluster-scoped kinds such as nodes; omit for "
                            "the whole cluster."
                        ),
                    },
                    "label_selector": {
                        "type": "string",
                        "description": (
                            "Label selector applied server-side, e.g. "
                            "'app=web,tier!=db' or 'env in (prod,staging)'. "
                            "Unlike columns, label keys with dots or slashes "
                            "work here."
                        ),
                    },
                    "field_selector": {
                        "type": "string",
                        "description": (
                            "Field selector applied server-side. All kinds "
                            "support metadata.name and metadata.namespace; "
                            "pods also support status.phase and spec.nodeName, "
                            "e.g. 'status.phase!=Running'."
                        ),
                    },
                    "filter_pattern": {
                        "type": "string",
                        "description": (
                            "Case-sensitive regular expression matched against "
                            "each row, e.g. 'Running'."
                        ),
                    },
                },
                "required": ["kind", "columns"],
            },
        },
    },
)

_RETURNS = (
    "A table of every object of one kind read from the cluster's API server: "
    "header (the column names in the order given), rows under rows (one "
    "array of cells per object, in header order, in the API server's order) "
    "and count (rows returned). The view adds header, count, scope (kind, "
    "namespace or all_namespaces, cluster_scoped, label_selector, "
    "field_selector, application_namespace), notes, total_items_processed "
    "(objects read), rows_removed_by_filter, filter_mode (regex, substring "
    "or null) and pages_read."
)
_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. Objects are "
    "read at query time whatever the window says; only events are limited to "
    "it, by their own timestamp, before the columns are read."
)
_VALUES_FORMAT = (
    "The authorized target for this Run is appended as {values}. kind must be "
    "one of: " + _KINDS_TEXT + "; secrets in any spelling is RESOURCE_NOT_ALLOWED "
    "before anything is sent; namespace, when given, is a DNS label. columns "
    f"is HEADER:PATH entries separated by commas, at most {MAX_COLUMNS} and "
    f"{MAX_COLUMNS_CHARS} characters, without ' / ; or line breaks; anything "
    "outside the supported subset is INVALID_PARAMS (INVALID_COLUMNS) before "
    "anything is "
    f"sent. filter_pattern is at most {MAX_PATTERN_CHARS} characters."
)
_LIMITS = (
    "Pages of 500 objects are read to the end and merged; more than 4 MiB "
    "read in total is RESULT_TOO_LARGE for the whole call, never a partial "
    "table. Columns are evaluated in this process; no row limit applies. "
    "filter_pattern runs over the rows in a separate process with Python re, "
    "case-sensitive; a pattern still running at the end of the call's 30 s "
    "is stopped and the call is timeout/TOOL_TIMEOUT. The whole view is "
    "limited to the registered max_view_tokens (25,000 DeepSeek tokens): a "
    "larger table is refused with RESULT_TOO_LARGE, never truncated; narrow "
    "it with namespace, label_selector, field_selector, filter_pattern or "
    "fewer columns. HTTP 400/404/422 are INVALID_PARAMS, 401/403 "
    "denied/SOURCE_FORBIDDEN, 410 SOURCE_ERROR, 429 and 5xx SOURCE_UNAVAILABLE."
)
_CANNOT_PROVE = (
    "Objects are the cluster's state when the query ran, not during the "
    "incident window: they cannot prove what was true earlier, and an "
    "object missing now may have existed then. Events cover only their own "
    "timestamps inside the window; an event that is not returned is unknown, "
    "not proof nothing happened (events expire and timestamps are often "
    "null). <none> means the path reached no value in that object, not that "
    "the field is absent from the kind; a filtered table is not a full "
    "picture of the kind."
)

#: A2's codes without the jq ones, plus this tool's.
_ERROR_CLASSES: Mapping[str, str] = {
    **{
        key: value
        for key, value in ERROR_CLASSES.items()
        if not key.startswith("JQ_") and key != "INVALID_JQ"
    },
    "INVALID_COLUMNS": "INVALID_PARAMS",
    "INVALID_FILTER": "INVALID_PARAMS",
    "FILTER_FAILED": "SOURCE_ERROR",
}


def tabular_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The one registration (A2-1: ``source`` is the lab target's;
    ``may_contain_secrets=True`` like the other cluster tools)."""
    return (
        ToolRegistration(
            name=TABULAR_TOOL,
            version="v1",
            source=otel_demo.SOURCE,
            verb="list",
            may_contain_secrets=True,
            description=ToolDescription(
                returns=_RETURNS,
                window_format=_WINDOW_FORMAT,
                values_format=_VALUES_FORMAT,
                limits=_LIMITS,
                cannot_prove=_CANNOT_PROVE,
            ),
            parameters={
                "kind": ParameterSpec(
                    "string",
                    required=True,
                    description="Plural resource name from the table.",
                ),
                "columns": ParameterSpec(
                    "string",
                    required=True,
                    description="HEADER:PATH entries separated by commas.",
                ),
                "namespace": ParameterSpec(
                    "string",
                    description="Namespace to read; omitted is the whole cluster.",
                ),
                "label_selector": ParameterSpec(
                    "string", description="Optional label selector."
                ),
                "field_selector": ParameterSpec(
                    "string", description="Optional field selector."
                ),
                "filter_pattern": ParameterSpec(
                    "string", description="Case-sensitive regex over each row."
                ),
            },
            result_path=("rows",),
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_result_bytes=MAX_RESULT_BYTES,
            max_view_tokens=MAX_VIEW_TOKENS,
            max_window_seconds=max_window_seconds,
            error_classes=dict(_ERROR_CLASSES),
        ),
    )


def _registrations() -> tuple[ToolRegistration, ...]:
    """The registration under the profiles' 24 h frame (inventory seam)."""
    return tabular_registrations(max_window_seconds=otel_demo.OBSERVATION_SECONDS)
