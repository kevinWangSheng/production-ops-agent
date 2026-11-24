"""The standing operator instructions a Run starts with (#780; B4-U3=a, B4-U4=a).

Pre-review defaults 780-5..780-7 (``docs/tasks/2026-11-24-round-prereview-a-eval.md``)
and B4-9/B4-10 (``docs/tasks/2026-11-22-next-batches-b4-prereview.md``). At
Run creation the web reads, in its own read-only snapshot, the target's one
active standing-instructions revision (``RunbookStore
.active_standing_instructions``) and records it in the input as the
``standing_instructions`` fact (input v9): entry, revision, approval time and
approver, and the content after the registry's credential redaction. The
model is sent the content as one untrusted-data user message, before the
runbook list. A target without approved standing instructions gets no fact,
so its Run input, model messages and ``prompt_revision`` stay as before.

Pure shaping and validation; the SQL is in ``opspilot.knowledge.runbooks``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from opspilot.tools.registry import redact_credentials

POLICY_VERSION = "opspilot-standing-instructions-v1"
#: A redacted text can grow past the 4096 stored bytes (each match becomes a
#: marker); the validator allows this many characters.
MAX_CONTENT_CHARS = 8192
FIELDS = frozenset(
    {
        "policy_version",
        "entry_id",
        "revision",
        "approved_at",
        "approved_by",
        "content",
    }
)


class StandingInstructionsSource(Protocol):
    def active_standing_instructions(self, resource_uid: str) -> dict[str, Any] | None:
        """``entry_id``, ``revision``, ``approved_at``, ``approved_by`` and
        ``content`` of the target's active revision, or ``None``."""
        ...


def redacted_content(content: str) -> str:
    """What the fact and the model get of a stored body. The store refuses a
    body whose redaction is longer than ``MAX_CONTENT_CHARS`` at submit and
    approve (``fits_after_redaction``), so an approved revision always builds
    a valid fact."""
    return redact_credentials(content)


def fits_after_redaction(content: str) -> bool:
    return len(redacted_content(content)) <= MAX_CONTENT_CHARS


def build_standing_instructions(row: Mapping[str, Any]) -> dict[str, Any]:
    """The fact for one active revision; the content is redacted here, once,
    so the input snapshot never holds a credential the model must not see."""
    approved_at = row["approved_at"]
    return {
        "policy_version": POLICY_VERSION,
        "entry_id": str(row["entry_id"]),
        "revision": int(row["revision"]),
        "approved_at": approved_at.isoformat()
        if isinstance(approved_at, datetime)
        else str(approved_at),
        "approved_by": str(row["approved_by"]),
        "content": redacted_content(str(row["content"])),
    }


def standing_instructions_for_target(
    source: StandingInstructionsSource, resource_uid: str
) -> dict[str, Any] | None:
    row = source.active_standing_instructions(resource_uid)
    return None if row is None else build_standing_instructions(row)


def valid_standing_instructions(value: object) -> bool:
    """Validate a persisted v9 fact: exact keys, types and bounds."""
    if not isinstance(value, Mapping) or set(value) != FIELDS:
        return False
    if value["policy_version"] != POLICY_VERSION:
        return False
    revision = value["revision"]
    if type(revision) is not int or revision < 1:
        return False
    content, approver = value["content"], value["approved_by"]
    if not isinstance(content, str) or not content.strip():
        return False
    if len(content) > MAX_CONTENT_CHARS or "\x00" in content:
        return False
    if not isinstance(approver, str) or not approver:
        return False
    try:
        UUID(value["entry_id"])
    except (TypeError, ValueError, AttributeError):
        return False
    if not isinstance(value["entry_id"], str):
        return False
    approved = value["approved_at"]
    if not isinstance(approved, str):
        return False
    try:
        parsed = datetime.fromisoformat(approved)
    except ValueError:
        return False
    return parsed.tzinfo is not None
