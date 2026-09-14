"""`Integration / Target` and `InputEvent` (C3 section 4).

Target identity is immutable and registry-resolved; it is never derived from a
name supplied by the model. Delivery keys prefer a stable source event id and
otherwise require enough identity to avoid deduplicating a later real incident
away (C3 section 6).
"""

import hashlib
import json
from datetime import datetime

from pydantic import AwareDatetime

from .base import DTO, DomainError, Positive, Text


class Integration(DTO):
    """Connection metadata for one evidence source.

    Credentials live outside the model boundary and outside this type. The
    strict/extra-forbid config makes a credential field unrepresentable.
    """

    integration_id: Text
    kind: Text
    revision: Text
    endpoint_ref: Text


class Target(DTO):
    """Immutable target identity registered under an integration."""

    integration_id: Text
    cluster_uid: Text
    namespace: Text
    resource_uid: Text
    revision: Text


class InputEvent(DTO):
    """A received source event plus the delivery identity used to deduplicate."""

    event_id: Text
    source: Text
    delivery_key: Text
    received_at: AwareDatetime
    sequence: Positive
    external_event_id: Text | None = None
    source_instance: Text | None = None
    object_identity: Text | None = None
    state_time: AwareDatetime | None = None
    payload_hash: Text | None = None
    target: Target | None = None
    fingerprint: Text | None = None


def _digest(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def delivery_key(
    *,
    source: str,
    external_event_id: str | None = None,
    source_instance: str | None = None,
    object_identity: str | None = None,
    state_time: datetime | None = None,
    payload_hash: str | None = None,
) -> str:
    """Build the deduplication key for one delivery.

    A stable ``(source, external_event_id)`` pair wins. Without it the caller
    must supply source instance, stable object identity, state time and a
    normalized payload hash together; an object name alone is rejected, because
    deduplicating on it forever would swallow the next real incident.
    """
    if not isinstance(source, str) or not source:
        raise DomainError("INVALID_INPUT", "source is required")
    if external_event_id is not None:
        if not isinstance(external_event_id, str) or not external_event_id:
            raise DomainError("INVALID_INPUT", "external_event_id must be non-empty")
        return "evt:" + _digest(
            {"basis": "external_event_id", "source": source, "id": external_event_id}
        )
    parts = {
        "source_instance": source_instance,
        "object_identity": object_identity,
        "payload_hash": payload_hash,
    }
    missing = sorted(
        name for name, value in parts.items() if not isinstance(value, str) or not value
    )
    if missing:
        raise DomainError("INVALID_INPUT", f"composite delivery key needs {missing}")
    if not isinstance(state_time, datetime):
        raise DomainError("INVALID_INPUT", "composite delivery key needs state_time")
    if state_time.tzinfo is None or state_time.utcoffset() is None:
        raise DomainError("INVALID_INPUT", "state_time must be timezone aware")
    return "evt:" + _digest(
        {
            "basis": "composite",
            "source": source,
            "state_time": state_time.isoformat(),
            **parts,
        }
    )


def is_duplicate_delivery(event: InputEvent, seen: frozenset[str]) -> bool:
    """Deduplicate on the delivery key only, never on a resource name."""
    if not isinstance(event, InputEvent) or not isinstance(seen, frozenset):
        raise DomainError("INVALID_INPUT", "event and seen delivery keys are required")
    return event.delivery_key in seen
