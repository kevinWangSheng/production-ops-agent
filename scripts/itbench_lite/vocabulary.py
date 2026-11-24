"""Per-scenario entity vocabulary generator for ITBench-Lite (#605, r6 L-V6-L-V8).

Rules (decision table r6, docs/tasks/2026-11-12-tool-expansion-contract.md),
fixed before any answer was looked at and not tuned on scores:

* L-V6 object names: every ``metadata.name`` that appears in any collection
  of the cluster object TSV (recreated Pods included), only for the resource
  classes of ``answer.RESOURCE_KINDS``, without ``secrets`` and without
  objects in a namespace the official filter excludes
  (``entity_match.DEFAULT_EXCLUDED_NAMESPACES``). Service names: the union of
  Service object names (same filters), the trace ``ServiceName`` column and
  the log ``service.name`` resource attribute (and ``ServiceName`` column).
* L-V7 (user ruling 2026-11-13): Namespace object names are left out, and so
  is every name in ``STOPWORDS`` (case-insensitive).
* L-V8 one name per line, de-duplicated case-insensitively (the
  ``answer._Matcher`` rule) and sorted.
* #647 (L-V7 clarified 2026-11-15, decision C5 = a): every namespace name --
  each Namespace object's ``metadata.name`` and each object row's
  ``metadata.namespace`` value, any resource class -- is removed from the
  vocabulary as a string, whatever the source of the name (object, trace or
  log).  ``answer.convert``'s own namespace exclusion stays as a backstop.
* #646 C4 kind index (``build_kinds``, ``--kinds-out``): name (casefold) ->
  sorted distinct ``[Kind, namespace]`` pairs of the kept object rows, a JSON
  sidecar ``answer.convert`` uses to pick a Kind.

Data comes only through the whitelist loader (``snapshot.load_snapshot``);
every TSV is streamed row by row.

Usage::

    python -m scripts.itbench_lite.vocabulary <clean copy> --out <file>
"""

from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Iterable, Iterator, Sequence
from itertools import chain
from pathlib import Path
from typing import Any

from scripts.itbench_lite.answer import RESOURCE_KINDS
from scripts.itbench_lite.entity_match import DEFAULT_EXCLUDED_NAMESPACES
from scripts.itbench_lite.snapshot import Snapshot, load_snapshot

#: Generic words never put in the vocabulary (L-V7), compared case-insensitively.
STOPWORDS = frozenset({"kubernetes", "default"})
#: Resource classes left out although ``answer.RESOURCE_KINDS`` lists them.
EXCLUDED_PLURALS = frozenset({"secrets", "namespaces"})
OBJECT_PLURALS = frozenset(RESOURCE_KINDS) - EXCLUDED_PLURALS
SERVICE_ATTRIBUTE = "service.name"


def _literal(text: str | None, where: str) -> dict[str, Any]:
    if text is None or not text.strip():
        return {}
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        raise ValueError(f"{where}: attributes do not parse") from None
    if not isinstance(value, dict):
        raise ValueError(f"{where}: attributes are not a mapping")
    return value


def _object_rows(snapshot: Snapshot) -> Iterator[tuple[str, dict[str, Any]]]:
    """``(plural, metadata)`` of every object row, any resource class.

    The resource class is the row's ``LogAttributes`` ``k8s.resource.name``;
    a row without one is malformed and raises, as in the fake API server
    (``fake_k8s._object_type``).
    """
    path = snapshot.tsv_path("objects")
    if path is None:
        return
    for number, row in enumerate(snapshot.iter_tsv(path), start=1):
        where = f"{path.name} data row {number}"
        plural = _literal(row.get("LogAttributes"), where).get("k8s.resource.name")
        if not (isinstance(plural, str) and plural):
            raise ValueError(f"{where}: no k8s.resource.name")
        try:
            body = json.loads(row.get("Body") or "")
        except (ValueError, RecursionError):
            raise ValueError(f"{where}: Body is not JSON") from None
        metadata = body.get("metadata") if isinstance(body, dict) else None
        if not isinstance(metadata, dict):
            raise ValueError(f"{where}: object has no metadata")
        yield plural, metadata


def _kept_objects(snapshot: Snapshot) -> Iterator[tuple[str, dict[str, Any]]]:
    for plural, metadata in _object_rows(snapshot):
        if plural not in OBJECT_PLURALS:
            continue
        if metadata.get("namespace") in DEFAULT_EXCLUDED_NAMESPACES:
            continue
        yield plural, metadata


def object_names(snapshot: Snapshot) -> Iterator[str]:
    """``metadata.name`` of every kept object row (L-V6, L-V7)."""
    for _plural, metadata in _kept_objects(snapshot):
        name = metadata.get("name")
        if isinstance(name, str):
            yield name


def namespace_names(snapshot: Snapshot) -> set[str]:
    """#647: casefolded Namespace object names and ``metadata.namespace`` values."""
    found: set[str] = set()
    for plural, metadata in _object_rows(snapshot):
        if plural == "namespaces":
            name = metadata.get("name")
            if isinstance(name, str) and name.strip():
                found.add(name.strip().casefold())
        value = metadata.get("namespace")
        if isinstance(value, str) and value.strip():
            found.add(value.strip().casefold())
    return found


def build_kinds(snapshot: Snapshot) -> dict[str, list[list[str | None]]]:
    """#646 C4: name (casefold) -> sorted distinct ``[Kind, namespace]`` pairs."""
    pairs: dict[str, set[tuple[str, str | None]]] = {}
    for plural, metadata in _kept_objects(snapshot):
        name = metadata.get("name")
        if not (isinstance(name, str) and name.strip()):
            continue
        value = metadata.get("namespace")
        namespace = value if isinstance(value, str) and value else None
        pairs.setdefault(name.strip().casefold(), set()).add(
            (RESOURCE_KINDS[plural], namespace)
        )
    return {
        key: [list(pair) for pair in sorted(found, key=lambda p: (p[0], p[1] or ""))]
        for key, found in sorted(pairs.items())
    }


def trace_service_names(snapshot: Snapshot) -> Iterator[str]:
    """The ``ServiceName`` column of every span row."""
    path = snapshot.tsv_path("traces")
    if path is None:
        return
    for row in snapshot.iter_tsv(path):
        yield row.get("ServiceName") or ""


def log_service_names(snapshot: Snapshot) -> Iterator[str]:
    """``service.name`` of every log record (resource attribute and column)."""
    path = snapshot.tsv_path("logs")
    if path is None:
        return
    for number, row in enumerate(snapshot.iter_tsv(path), start=1):
        where = f"{path.name} data row {number}"
        yield row.get("ServiceName") or ""
        value = _literal(row.get("ResourceAttributes"), where).get(SERVICE_ATTRIBUTE)
        if isinstance(value, str):
            yield value


def normalise(names: Iterable[str], excluded: Iterable[str] = ()) -> list[str]:
    """L-V7 stop words out, L-V8 case-insensitive de-duplication and sort.

    Of several spellings of one name the smallest is kept, so the result does
    not depend on row order.  ``excluded`` (casefolded names, #647) are
    dropped like stop words.
    """
    dropped = STOPWORDS | frozenset(excluded)
    kept: dict[str, str] = {}
    for raw in names:
        name = raw.strip() if isinstance(raw, str) else ""
        if not name or "\n" in name or "\r" in name:
            continue
        key = name.casefold()
        if key in dropped:
            continue
        if key not in kept or name < kept[key]:
            kept[key] = name
    return [kept[key] for key in sorted(kept)]


def build(snapshot: Snapshot) -> list[str]:
    """The vocabulary of one loaded scenario."""
    return normalise(
        chain(
            object_names(snapshot),
            trace_service_names(snapshot),
            log_service_names(snapshot),
        ),
        namespace_names(snapshot),
    )


def render(names: Sequence[str]) -> str:
    return "".join(f"{name}\n" for name in names)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", help="clean copy of one scenario directory")
    parser.add_argument("--out", required=True, help="vocabulary text file")
    parser.add_argument(
        "--kinds-out", help="C4 kind index JSON (S<id>.kinds.json beside --out)"
    )
    args = parser.parse_args(argv)
    snapshot = load_snapshot(args.snapshot)
    Path(args.out).write_text(render(build(snapshot)), encoding="utf-8")
    if args.kinds_out:
        Path(args.kinds_out).write_text(
            json.dumps(build_kinds(snapshot), ensure_ascii=False, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
