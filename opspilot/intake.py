"""Authenticated intake contracts for the first M1 vertical slice.

This module deliberately stops at the trust boundary. A future HTTP adapter
may turn verified proxy credentials or the separate event token into these
objects; raw headers, password material and bearer tokens never enter them.
"""

from __future__ import annotations

from typing import Annotated, Literal
from unicodedata import category

from pydantic import AwareDatetime, Field, field_validator

from .domain.base import DTO, DomainError

AuthChannel = Literal["ui_basic", "event_token"]

#: An identity, target or key that is rendered verbatim into an audit line.
#: Bounded because every one of these is written to a log an operator reads;
#: ``Text`` carries no upper bound, and an unbounded identifier is not one.
Identifier = Annotated[str, Field(min_length=1, max_length=256)]


class Principal(DTO):
    """Identity already verified by the trusted authentication adapter."""

    actor_id: Identifier
    channel: AuthChannel
    auth_revision: Identifier

    @field_validator("actor_id", "auth_revision")
    @classmethod
    def reject_ambiguous_identifier(cls, value: str) -> str:
        return _reject_ambiguous_identifier(value)


class IntakeRequest(DTO):
    """An authenticated request to start or resume one incident intake."""

    target_id: Identifier
    question: str = Field(min_length=1, max_length=16_384)
    idempotency_key: Identifier

    @field_validator("target_id", "idempotency_key")
    @classmethod
    def reject_ambiguous_identifier(cls, value: str) -> str:
        return _reject_ambiguous_identifier(value)

    @field_validator("question")
    @classmethod
    def reject_ambiguous_text(cls, value: str) -> str:
        return _reject_ambiguous_text(value)


class IntakeEnvelope(DTO):
    """The only data a controller may pass to durable intake creation."""

    request_id: Identifier
    principal: Principal
    request: IntakeRequest
    received_at: AwareDatetime

    @field_validator("request_id")
    @classmethod
    def reject_ambiguous_identifier(cls, value: str) -> str:
        return _reject_ambiguous_identifier(value)


def verify_channel(principal: Principal, *, expected: AuthChannel) -> Principal:
    """Require the caller to use the channel intended for this entry point."""

    if not isinstance(principal, Principal) or principal.channel != expected:
        raise DomainError("INVALID_INPUT", "authentication channel mismatch")
    return principal


#: Categories that make a stored identifier and its rendered audit line
#: disagree: ``Cc`` control codes, ``Cf`` invisible formatting and
#: bidirectional overrides, and the ``Zl``/``Zp``/``Zs`` separators. ``Zs`` is
#: included because U+00A0 and its siblings render as an ordinary space, so
#: two different actors would print one identical audit line.
_AMBIGUOUS_IDENTIFIER_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Zs"})

#: Free text keeps the whitespace an operator actually pastes.
_TEXT_WHITESPACE = frozenset("\t\n\r")

#: Direction controls reorder rendered text without changing what is stored,
#: so they are refused even where joiners and emoji are welcome.
_BIDIRECTIONAL_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


def _reject_ambiguous_identifier(value: str) -> str:
    """Reject an identifier that cannot be read back from an audit line.

    Identifiers carry no whitespace at all. A trailing space or a non-breaking
    space would make two distinct actors, targets or keys print identically
    while comparing unequal, which is the failure this guard exists to prevent.
    """

    if any(category(char) in _AMBIGUOUS_IDENTIFIER_CATEGORIES for char in value):
        raise ValueError("CONTROL_CHARACTER_FORBIDDEN")
    return value


def _reject_ambiguous_text(value: str) -> str:
    """Reject free text that breaks a record boundary or spoofs direction.

    The question is an operator's description of an incident, so it keeps
    newlines, tabs and the joiners that ordinary prose and emoji need. What it
    cannot carry is a separator that splits one stored record into two, or a
    bidirectional control that renders the text in an order it was not stored
    in.
    """

    for char in value:
        if char in _TEXT_WHITESPACE:
            continue
        if category(char) in {"Cc", "Zl", "Zp"} or char in _BIDIRECTIONAL_CONTROLS:
            raise ValueError("CONTROL_CHARACTER_FORBIDDEN")
    return value


def same_idempotent_intake(left: IntakeEnvelope, right: IntakeEnvelope) -> bool:
    """Return whether two deliveries are the same authenticated request.

    The key alone is insufficient: a reused key by another actor, target or
    question is an identity conflict and must never silently join an incident.
    """

    if not isinstance(left, IntakeEnvelope) or not isinstance(right, IntakeEnvelope):
        raise DomainError("INVALID_INPUT", "intake envelopes are required")
    return (
        left.principal == right.principal
        and left.request.idempotency_key == right.request.idempotency_key
        and left.request.target_id == right.request.target_id
        and left.request.question == right.request.question
    )
