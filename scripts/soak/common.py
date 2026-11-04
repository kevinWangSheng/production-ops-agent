"""Shared helpers: time, JSON lines, the soak window (S-R4)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOUR = 3600
#: S-R4: the soak covers T0 -> T0 + 72 h.
DURATION_S = 72 * HOUR
#: S-R4: environment-only interruptions are not deducted; above this total the
#: end moves out once by the same length, at most EXTENSION_CAP_S.
INTERRUPTION_THRESHOLD_S = 4 * HOUR
EXTENSION_CAP_S = 12 * HOUR


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError(f"timestamp without a zone: {raw!r}")
    return value.astimezone(timezone.utc)


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    """One line per record, flushed and fsynced: the runner may be killed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Records of a JSON-lines file; a missing file is empty and a torn last
    line (the writer was killed mid-write) is skipped."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def merge_intervals(
    intervals: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    ordered = sorted((a, b) for a, b in intervals if b > a)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def interval_total(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(end - start for start, end in merge_intervals(intervals))


def extension_s(interruption_s: float) -> float:
    """S-R4: the end moves out by the interruption total once it exceeds 4 h,
    only once and by at most 12 h."""
    if interruption_s <= INTERRUPTION_THRESHOLD_S:
        return 0.0
    return min(interruption_s, float(EXTENSION_CAP_S))


def load_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines (``kind_lab.py env-file`` output); values are never
    logged by any caller."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().removeprefix("export ").strip()] = value.strip()
    return values
