"""Whitelist loader for one ITBench-Lite SRE scenario directory (#581, L9).

The adapter may read only the metrics TSVs, the alert JSON files and the four
raw TSVs (traces, cluster objects, events, logs). A directory that holds an
answer file anywhere below it is refused before anything is opened: the check
looks at file names only and never opens, reads or prints such a file.

Every other module of the adapter reads snapshot data through ``Snapshot``,
whose readers refuse any path outside the whitelist.
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: File names that carry answers or grading state. Compared case-insensitively.
ANSWER_FILE_NAMES = frozenset(
    {"ground_truth.yaml", "ground_truth.yml", "assertion.json", "status.json"}
)

#: The four raw TSVs, keyed by data class.
TSV_FILES: Mapping[str, str] = {
    "traces": "otel_traces_raw.tsv",
    "objects": "k8s_objects_raw.tsv",
    "events": "k8s_events_raw.tsv",
    "logs": "otel_logs_raw.tsv",
}

METRICS_DIR = "metrics"
ALERTS_DIR = "alerts"

#: Cells such as an object's ``Body`` run to megabytes.
_FIELD_LIMIT = 2**31 - 1


class AnswerFilePresent(RuntimeError):
    """The scenario directory holds an answer file; the adapter will not start."""


class UnsafeSnapshot(RuntimeError):
    """The scenario tree cannot be checked completely; the adapter will not start."""


class NotWhitelisted(PermissionError):
    """A read outside the loader's whitelist."""


def _is_answer_name(name: str) -> bool:
    return name.casefold() in ANSWER_FILE_NAMES


def _walk_error(error: OSError) -> None:
    raise UnsafeSnapshot(
        "scenario directory cannot be fully listed; refusing to start"
    ) from error


def refuse_answer_files(root: Path) -> None:
    """Raise unless every name below ``root`` was listed and none is an answer name.

    Walks names only (``os.walk`` lists directory entries, it opens no file).
    Fails closed: a directory symlink anywhere in the tree (which the walk
    would not follow but a later glob would) and any listing error raise
    ``UnsafeSnapshot``; an answer name raises ``AnswerFilePresent``.
    """
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
        for name in (*filenames, *dirnames):
            if _is_answer_name(name):
                raise AnswerFilePresent(
                    "scenario directory holds an answer file; refusing to start"
                )
        for name in dirnames:
            if os.path.islink(os.path.join(dirpath, name)):
                raise UnsafeSnapshot(
                    "scenario directory holds a directory symlink; refusing to start"
                )


@dataclass(frozen=True)
class Snapshot:
    """The whitelisted inputs of one scenario directory."""

    root: Path
    metrics: tuple[Path, ...]
    alerts: tuple[Path, ...]
    tsv: Mapping[str, Path]

    def tsv_path(self, kind: str) -> Path | None:
        """The raw TSV of ``kind`` (traces, objects, events, logs), if present."""
        if kind not in TSV_FILES:
            raise KeyError(kind)
        return self.tsv.get(kind)

    def _allowed(self) -> frozenset[Path]:
        return frozenset((*self.metrics, *self.alerts, *self.tsv.values()))

    def _check(self, path: Path) -> Path:
        if path not in self._allowed():
            raise NotWhitelisted(str(path))
        return path

    def iter_tsv(self, path: Path) -> Iterator[dict[str, str]]:
        """Rows of one whitelisted TSV as ``{column: cell}``.

        The exports quote cells CSV-style (``""`` inside quotes) with a tab
        delimiter, so the stdlib reader handles embedded tabs and newlines.
        """
        self._check(path)
        csv.field_size_limit(_FIELD_LIMIT)
        with path.open(newline="", encoding="utf-8") as handle:
            yield from csv.DictReader(handle, delimiter="\t")

    def read_alert_file(self, path: Path) -> list[dict[str, Any]]:
        """The alert entries of one whitelisted Prometheus ``/api/v1/alerts`` file.

        Accepts the API envelope ``{"data": {"alerts": [...]}}``, a bare
        ``{"alerts": [...]}`` and a bare list. Non-object entries are dropped.
        """
        self._check(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict) and "alerts" in inner:
                data = inner["alerts"]
            else:
                data = data.get("alerts", [])
        if not isinstance(data, list):
            raise ValueError(f"{path.name}: alerts is not a list")
        return [entry for entry in data if isinstance(entry, dict)]


def _whitelisted_file(path: Path) -> bool:
    """A regular file whose symlink target, if any, is not an answer file."""
    if not path.is_file():
        return False
    if _is_answer_name(path.resolve().name):
        raise AnswerFilePresent(
            "a whitelisted name points at an answer file; refusing to start"
        )
    return True


def load_snapshot(root: str | os.PathLike[str]) -> Snapshot:
    """Load the whitelist of one scenario directory, refusing answer files first."""
    base = Path(root)
    if not base.is_dir():
        raise FileNotFoundError(str(base))
    refuse_answer_files(base)

    metrics_dir = base / METRICS_DIR
    metrics: tuple[Path, ...] = ()
    if metrics_dir.is_dir():
        metrics = tuple(
            sorted(p for p in metrics_dir.glob("*.tsv") if _whitelisted_file(p))
        )

    alert_files = [p for p in base.glob("alerts*.json") if _whitelisted_file(p)]
    alerts_dir = base / ALERTS_DIR
    if alerts_dir.is_dir():
        alert_files += [p for p in alerts_dir.glob("*.json") if _whitelisted_file(p)]
    alerts = tuple(sorted(alert_files, key=lambda p: (p.name, str(p))))

    tsv = {
        kind: base / name
        for kind, name in TSV_FILES.items()
        if _whitelisted_file(base / name)
    }
    return Snapshot(root=base, metrics=metrics, alerts=alerts, tsv=tsv)
