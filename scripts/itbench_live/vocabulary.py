"""Entity vocabulary of a live scenario (#564, A564-3).

The Lite rules (``scripts.itbench_lite.vocabulary``: L-V6 to L-V8, #647, the
#646 C4 kind index) applied to live cluster lists instead of a snapshot:

* object names: ``metadata.name`` of every object of the
  ``vocabulary.OBJECT_PLURALS`` resource classes (no Secrets, no Namespaces),
  outside the namespaces the official filter excludes;
* service names: those objects' Service names plus Jaeger's ``/api/services``;
* every namespace name is removed as a string; stop words are removed; one
  name per spelling, sorted.

The lists are read with OpsPilot's own read-only identity (get/list, no
Secrets), twice: before the fault and after the Run, and united, so Pods the
fault replaced are covered as Lite's whole-window snapshot covers them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from scripts.itbench_lite.answer import RESOURCE_KINDS
from scripts.itbench_lite.entity_match import DEFAULT_EXCLUDED_NAMESPACES
from scripts.itbench_lite.vocabulary import OBJECT_PLURALS, normalise

#: ``(plural, name, namespace or None)``.
ObjectRow = tuple[str, str, str | None]


def object_rows(lists: Mapping[str, Any]) -> list[ObjectRow]:
    """Rows of ``kubectl get <plural> -A -o json`` bodies, keyed by plural.
    Plurals outside ``OBJECT_PLURALS`` and unreadable bodies are skipped."""
    rows: list[ObjectRow] = []
    for plural, body in lists.items():
        if plural not in OBJECT_PLURALS or not isinstance(body, Mapping):
            continue
        items = body.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            meta = item.get("metadata") if isinstance(item, Mapping) else None
            if not isinstance(meta, Mapping):
                continue
            name = meta.get("name")
            if not (isinstance(name, str) and name.strip()):
                continue
            namespace = meta.get("namespace")
            rows.append(
                (
                    plural,
                    name.strip(),
                    namespace if isinstance(namespace, str) and namespace else None,
                )
            )
    return rows


def namespace_names(lists: Mapping[str, Any], rows: Iterable[ObjectRow]) -> set[str]:
    """Casefolded names of every Namespace object and every row's namespace."""
    found = {ns.casefold() for _, _, ns in rows if ns}
    body = lists.get("namespaces")
    items = body.get("items") if isinstance(body, Mapping) else None
    for item in items if isinstance(items, list) else []:
        meta = item.get("metadata") if isinstance(item, Mapping) else None
        name = meta.get("name") if isinstance(meta, Mapping) else None
        if isinstance(name, str) and name.strip():
            found.add(name.strip().casefold())
    return found


def kept(rows: Iterable[ObjectRow]) -> list[ObjectRow]:
    return [r for r in rows if r[2] not in DEFAULT_EXCLUDED_NAMESPACES]


def build(
    lists: Sequence[Mapping[str, Any]], jaeger_services: Iterable[str] = ()
) -> list[str]:
    """The vocabulary from one or more list sets (united)."""
    rows: list[ObjectRow] = []
    namespaces: set[str] = set()
    for one in lists:
        found = object_rows(one)
        rows += found
        namespaces |= namespace_names(one, found)
    names = [name for _, name, _ in kept(rows)]
    return normalise([*names, *jaeger_services], namespaces)


def build_kinds(
    lists: Sequence[Mapping[str, Any]],
) -> dict[str, list[list[str | None]]]:
    """#646 C4: name (casefold) -> sorted distinct ``[Kind, namespace]`` pairs."""
    pairs: dict[str, set[tuple[str, str | None]]] = {}
    for one in lists:
        for plural, name, namespace in kept(object_rows(one)):
            pairs.setdefault(name.casefold(), set()).add(
                (RESOURCE_KINDS[plural], namespace)
            )
    return {
        key: [list(pair) for pair in sorted(found, key=lambda p: (p[0], p[1] or ""))]
        for key, found in sorted(pairs.items())
    }


def jaeger_service_names(body: object) -> list[str]:
    """``/api/services`` -> ``data`` (v1) or ``/api/v3/services`` ->
    ``services`` (v3); anything else is no names."""
    data = None
    if isinstance(body, Mapping):
        data = body.get("data", body.get("services"))
    return [s for s in data if isinstance(s, str)] if isinstance(data, list) else []


#: The plurals the harness lists (sorted, Namespaces for the exclusion).
LIST_PLURALS: tuple[str, ...] = tuple(sorted(OBJECT_PLURALS | {"namespaces"}))
