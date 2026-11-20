"""The approved-runbook catalog a Run starts with (C2b, #566; r11 C2-U2=a).

Interface: ``docs/tasks/2026-11-19-tool-expansion-c2.md`` section 6 and
"C2b 签名级细节". At Run creation the web reads, in its own read-only
snapshot, every active imported runbook of the Run's target and records them
in the input as the ``runbook_catalog`` fact (input v7). The model is sent
the name, description, revision and freshness of each, as one untrusted-data
message; ``fetch_skill`` reads a runbook's content on demand.

Pure shaping, bounds and validation; the SQL is ``RunbookStore.active_catalog``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from opspilot.tools.registry import canonical, redact_credentials

POLICY_VERSION = "opspilot-runbook-catalog-v1"
MAX_ENTRIES = 50
#: The model-visible part (``visible_entries``) in canonical UTF-8 bytes.
MAX_VISIBLE_BYTES = 8192
#: A redacted description can grow past the 300 stored characters; the
#: validator allows this much.
MAX_DESCRIPTION_CHARS = 1024
FIELDS = frozenset(
    {
        "policy_version",
        "status",
        "retrieved_at",
        "max_age_seconds",
        "omitted_for_size",
        "entries",
    }
)
ENTRY_FIELDS = frozenset(
    {
        "skill_id",
        "entry_id",
        "revision",
        "description",
        "approved_at",
        "age_seconds",
        "freshness",
    }
)
VISIBLE_FIELDS = ("skill_id", "description", "revision", "freshness")
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class CatalogSource(Protocol):
    def active_catalog(self, resource_uid: str) -> dict[str, Any]:
        """``{"retrieved_at": datetime, "rows": [...]}`` with ``name``,
        ``entry_id``, ``revision``, ``description``, ``approved_at`` and
        ``age_seconds`` per active runbook of the target."""
        ...


def freshness_of(age_seconds: int, max_age_seconds: int) -> str:
    return "stale" if age_seconds > max_age_seconds else "fresh"


def visible_entries(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What the model is sent of each entry, in the fact's order."""
    return [{key: entry[key] for key in VISIBLE_FIELDS} for entry in entries]


def _visible_bytes(entries: Sequence[Mapping[str, Any]]) -> int:
    return len(canonical(visible_entries(entries)).encode("utf-8"))


def build_catalog(
    rows: Sequence[Mapping[str, Any]],
    *,
    retrieved_at: datetime,
    max_age_seconds: int,
) -> dict[str, Any]:
    """The ``ok`` fact: entries sorted by name, at most ``MAX_ENTRIES`` and
    ``MAX_VISIBLE_BYTES``; what does not fit is dropped from the end of the
    name order and counted in ``omitted_for_size``."""
    entries: list[dict[str, Any]] = [
        {
            "skill_id": str(row["name"]),
            "entry_id": str(row["entry_id"]),
            "revision": int(row["revision"]),
            "description": redact_credentials(str(row["description"])),
            "approved_at": _iso(row["approved_at"]),
            "age_seconds": int(row["age_seconds"]),
            "freshness": freshness_of(int(row["age_seconds"]), max_age_seconds),
        }
        for row in rows
    ]
    entries.sort(key=lambda entry: str(entry["skill_id"]))
    kept = entries[:MAX_ENTRIES]
    while kept and _visible_bytes(kept) > MAX_VISIBLE_BYTES:
        kept.pop()
    return {
        "policy_version": POLICY_VERSION,
        "status": "ok",
        "retrieved_at": _iso(retrieved_at),
        "max_age_seconds": max_age_seconds,
        "omitted_for_size": len(entries) - len(kept),
        "entries": kept,
    }


def unavailable_catalog(max_age_seconds: int) -> dict[str, Any]:
    """A storage failure at Run creation: recorded, the Run still starts."""
    return {
        "policy_version": POLICY_VERSION,
        "status": "unavailable",
        "retrieved_at": None,
        "max_age_seconds": max_age_seconds,
        "omitted_for_size": 0,
        "entries": [],
    }


def catalog_for_target(
    source: CatalogSource, resource_uid: str, *, max_age_seconds: int
) -> dict[str, Any]:
    read = source.active_catalog(resource_uid)
    return build_catalog(
        read["rows"],
        retrieved_at=read["retrieved_at"],
        max_age_seconds=max_age_seconds,
    )


def valid_catalog(value: object) -> bool:
    """Validate a persisted v7 fact: shape, order, bounds and that every
    ``freshness`` follows from its age and the recorded threshold."""
    if not isinstance(value, Mapping) or set(value) != FIELDS:
        return False
    if value["policy_version"] != POLICY_VERSION or value["status"] not in (
        "ok",
        "unavailable",
    ):
        return False
    maximum = value["max_age_seconds"]
    omitted = value["omitted_for_size"]
    if type(maximum) is not int or maximum < 0 or type(omitted) is not int:
        return False
    if omitted < 0:
        return False
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        return False
    if value["status"] == "unavailable":
        return value["retrieved_at"] is None and not entries and omitted == 0
    if not _timestamp(value["retrieved_at"]):
        return False
    names: list[str] = []
    for entry in entries:
        if not _valid_entry(entry, maximum):
            return False
        names.append(entry["skill_id"])
    if names != sorted(names) or len(set(names)) != len(names):
        return False
    try:
        return _visible_bytes(entries) <= MAX_VISIBLE_BYTES
    except (TypeError, ValueError):
        return False


def _valid_entry(entry: object, max_age_seconds: int) -> bool:
    if not isinstance(entry, Mapping) or set(entry) != ENTRY_FIELDS:
        return False
    name, description = entry["skill_id"], entry["description"]
    if not isinstance(name, str) or len(name) > 100 or not _NAME.fullmatch(name):
        return False
    if (
        not isinstance(description, str)
        or not description
        or len(description) > MAX_DESCRIPTION_CHARS
    ):
        return False
    revision, age = entry["revision"], entry["age_seconds"]
    if type(revision) is not int or revision < 1 or type(age) is not int or age < 0:
        return False
    if entry["freshness"] != freshness_of(age, max_age_seconds):
        return False
    try:
        UUID(entry["entry_id"])
    except (TypeError, ValueError, AttributeError):
        return False
    return isinstance(entry["entry_id"], str) and _timestamp(entry["approved_at"])


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None
