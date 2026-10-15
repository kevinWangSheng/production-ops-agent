"""Pure shaping, bounds and report observations for historical knowledge hints."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from opspilot.tools.registry import canonical

POLICY_VERSION = "knowledge-hint-v1"
CONTENT_BYTES = 4096
ENTRIES_BYTES = 12288


def hint_content(content: str) -> tuple[str, bool]:
    value = json.loads(content)
    refs = value.get("evidence_refs", [])
    value["evidence_refs"] = {"omitted": True, "count": len(refs)}
    text = canonical(value)
    raw = text.encode("utf-8")
    return raw[:CONTENT_BYTES].decode("utf-8", errors="ignore"), len(
        raw
    ) > CONTENT_BYTES


def bound_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    kept = list(entries)
    while len(canonical(kept).encode("utf-8")) > ENTRIES_BYTES:
        kept.pop()
    return kept, len(entries) - len(kept)


def unavailable_fact(max_age_seconds: int) -> dict[str, Any]:
    return dict(
        policy_version=POLICY_VERSION,
        status="unavailable",
        retrieved_at=None,
        max_age_seconds=max_age_seconds,
        omitted_for_size=0,
        entries=[],
    )


def valid_knowledge(value: object) -> bool:
    """Validate a persisted v5 fact, including byte bounds and typed provenance."""
    if not isinstance(value, Mapping) or set(value) != {
        "policy_version",
        "status",
        "retrieved_at",
        "max_age_seconds",
        "omitted_for_size",
        "entries",
    }:
        return False
    if value["policy_version"] != POLICY_VERSION or value["status"] not in (
        "ok",
        "unavailable",
    ):
        return False
    for key in ("max_age_seconds", "omitted_for_size"):
        if type(value[key]) is not int or value[key] < 0:
            return False
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) > 3:
        return False
    if value["status"] == "unavailable":
        return (
            value["retrieved_at"] is None
            and not entries
            and value["omitted_for_size"] == 0
        )
    try:
        if not _timestamp(value["retrieved_at"]):
            return False
        if len(canonical(entries).encode("utf-8")) > ENTRIES_BYTES:
            return False
        seen = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {
                "entry_id",
                "revision",
                "name",
                "tags",
                "content",
                "content_sha256",
                "source_incident_id",
                "source_postmortem_id",
                "source_version",
                "approved_by",
                "approved_at",
                "observation_ended_at",
                "age_seconds",
                "freshness",
                "truncated",
            }:
                return False
            for key in ("entry_id", "source_incident_id", "source_postmortem_id"):
                if not isinstance(entry[key], str):
                    return False
                UUID(entry[key])
            if entry["entry_id"] in seen:
                return False
            seen.add(entry["entry_id"])
            for key in ("revision", "source_version"):
                if type(entry[key]) is not int or entry[key] < 1:
                    return False
            if (
                type(entry["age_seconds"]) is not int
                or type(entry["truncated"]) is not bool
            ):
                return False
            for key in ("name", "content", "approved_by"):
                if not isinstance(entry[key], str) or not entry[key]:
                    return False
            if not isinstance(entry["tags"], list) or any(
                not isinstance(t, str) for t in entry["tags"]
            ):
                return False
            if not isinstance(entry["content_sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", entry["content_sha256"]
            ):
                return False
            if any(
                not _timestamp(entry[k])
                for k in ("approved_at", "observation_ended_at")
            ):
                return False
            if len(entry["content"].encode("utf-8")) > CONTENT_BYTES:
                return False
            # The recorded age must be the one its two recorded instants give
            # (R3, whole seconds), or the freshness label is unfounded.
            age = datetime.fromisoformat(
                value["retrieved_at"]
            ) - datetime.fromisoformat(entry["observation_ended_at"])
            if entry["age_seconds"] != age // timedelta(seconds=1):
                return False
            if entry["freshness"] != (
                "stale" if entry["age_seconds"] > value["max_age_seconds"] else "fresh"
            ):
                return False
        return True
    except (ValueError, TypeError, UnicodeError):
        return False


def _timestamp(value: Any) -> bool:
    return (
        isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    )


def knowledge_checks(
    knowledge_fact: Mapping[str, Any], report: Any
) -> list[dict[str, Any]]:
    """Observe tags in committed claims; never accept, reject or retry a report."""
    claims = (
        report.get("claims", [])
        if isinstance(report, Mapping)
        else getattr(report, "claims", [])
    )
    results = []
    for entry in knowledge_fact.get("entries", []):
        tag = f"[knowledge:{entry['entry_id']}@r{entry['revision']}]"
        kinds = []
        verdicts = set()
        for claim in claims:

            def get(key: str) -> Any:
                return (
                    claim.get(key)
                    if isinstance(claim, Mapping)
                    else getattr(claim, key, None)
                )

            if tag not in (get("text") or ""):
                continue
            kind = get("kind")
            if kind not in kinds:
                kinds.append(kind)
            if get("evidence_ids"):
                if kind == "fact":
                    verdicts.add("consistent")
                elif kind == "rejected_hypothesis":
                    verdicts.add("contradicted")
        verdict = (
            "conflicting"
            if len(verdicts) == 2
            else next(iter(verdicts), "unverified" if kinds else "untagged")
        )
        results.append(
            dict(
                entry_id=entry["entry_id"],
                revision=entry["revision"],
                verdict=verdict,
                claim_kinds=kinds,
            )
        )
    return results
