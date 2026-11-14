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
#: Kubernetes ``kind`` to plural, for rows without ``k8s.resource.name``.
PLURAL_OF_KIND = {kind: plural for plural, kind in RESOURCE_KINDS.items()}
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


def object_names(snapshot: Snapshot) -> Iterator[str]:
    """``metadata.name`` of every kept object row (L-V6, L-V7).

    The resource class is the row's ``LogAttributes`` ``k8s.resource.name``
    (what the fake API server serves it as); a row without one falls back to
    the object's own ``kind``.
    """
    path = snapshot.tsv_path("objects")
    if path is None:
        return
    for number, row in enumerate(snapshot.iter_tsv(path), start=1):
        where = f"{path.name} data row {number}"
        plural = _literal(row.get("LogAttributes"), where).get("k8s.resource.name")
        if plural is not None and plural not in OBJECT_PLURALS:
            continue
        try:
            body = json.loads(row.get("Body") or "")
        except (ValueError, RecursionError):
            raise ValueError(f"{where}: Body is not JSON") from None
        if plural is None:
            kind = body.get("kind") if isinstance(body, dict) else None
            if PLURAL_OF_KIND.get(kind) not in OBJECT_PLURALS:
                continue
        metadata = body.get("metadata") if isinstance(body, dict) else None
        if not isinstance(metadata, dict):
            raise ValueError(f"{where}: object has no metadata")
        if metadata.get("namespace") in DEFAULT_EXCLUDED_NAMESPACES:
            continue
        name = metadata.get("name")
        if isinstance(name, str):
            yield name


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


def normalise(names: Iterable[str]) -> list[str]:
    """L-V7 stop words out, L-V8 case-insensitive de-duplication and sort.

    Of several spellings of one name the smallest is kept, so the result does
    not depend on row order.
    """
    kept: dict[str, str] = {}
    for raw in names:
        name = raw.strip() if isinstance(raw, str) else ""
        if not name or "\n" in name or "\r" in name:
            continue
        key = name.casefold()
        if key in STOPWORDS:
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
        )
    )


def render(names: Sequence[str]) -> str:
    return "".join(f"{name}\n" for name in names)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", help="clean copy of one scenario directory")
    parser.add_argument("--out", required=True, help="vocabulary text file")
    args = parser.parse_args(argv)
    names = build(load_snapshot(args.snapshot))
    Path(args.out).write_text(render(names), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
