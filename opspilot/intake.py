"""Authenticated intake contracts for the first M1 vertical slice.

This module deliberately stops at the trust boundary: a future HTTP adapter
verifies proxy credentials or the separate event token and turns the result
into these objects. Two boundaries are worth stating exactly, because both
have already been described here more strongly than they are enforced.

What this layer enforces. ``extra="forbid"`` makes a password, an
``authorization`` field or any other unexpected attribute unrepresentable,
and ``strict=True`` refuses a raw header handed over as bytes. Neither the
objects nor ``sanitized_errors`` rendering of a rejection carries the value
that was refused.

What the adapter still owns. This layer cannot tell that a well-formed
``actor_id`` happens to be a raw Basic header, because nothing in the string
distinguishes it from a legitimate identifier. Keeping credentials out of
these fields is the adapter's obligation, not a property of these types.

Channel separation is likewise enforced at runtime, not in the type system.
``Principal`` is one type whose ``channel`` distinguishes the two, so an entry
point restricted to one channel must call ``verify_channel``; nothing stops a
caller from forgetting. Making the channels distinct types is a live design
question, recorded in the task record rather than decided here.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeVar
from unicodedata import category

from pydantic import AwareDatetime, Field, ValidationError, field_validator

from .domain.base import DTO, DomainError

_ModelT = TypeVar("_ModelT", bound=DTO)

AuthChannel = Literal["ui_basic", "event_token"]

#: How a delivery relates to one already accepted under the same key.
#: ``key_conflict`` is the case a boolean would hide: same key, different
#: actor, channel, target or question.
IntakeDelivery = Literal["same_request", "key_conflict", "different_request"]

#: An identity, target or key that is rendered verbatim into an audit line.
#: Bounded because every one of these is written to a log an operator reads;
#: ``Text`` carries no upper bound, and an unbounded identifier is not one.
Identifier = Annotated[str, Field(min_length=1, max_length=256)]

#: ``revalidate_instances="always"`` must sit on the model being revalidated,
#: not on the container. Without it an already-built instance is stored as
#: handed over, so a ``Principal`` produced by ``model_construct``, derived via
#: ``model_copy(update=...)`` or widened by an ``extra="allow"`` subclass would
#: reach durable creation unchecked - and a credential riding on a subclass
#: attribute would reach the envelope's repr. Set on this module's own types
#: rather than the shared ``DTO``, so neither the cost nor the behaviour change
#: leaves this entry point.
_REVALIDATED = DTO.model_config | {"revalidate_instances": "always"}


class Principal(DTO):
    """Identity already verified by the trusted authentication adapter."""

    model_config = _REVALIDATED

    actor_id: Identifier
    channel: AuthChannel
    auth_revision: Identifier

    @field_validator("actor_id", "auth_revision")
    @classmethod
    def reject_ambiguous_identifier(cls, value: str) -> str:
        return _reject_ambiguous_identifier(value)


class IntakeRequest(DTO):
    """An authenticated request to start or resume one incident intake.

    ``target_id`` is the operator's unresolved request, not an identity. The
    resolved identity is ``opspilot.domain.intake.Target``, which is immutable
    and registry-resolved; this string must be resolved into one before it
    names anything. The two are deliberately different types and this module
    does not perform that resolution.
    """

    model_config = _REVALIDATED

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
        if not value.strip():
            raise ValueError("QUESTION_IS_BLANK")
        return _reject_ambiguous_text(value)


class IntakeEnvelope(DTO):
    """The only data a controller may pass to durable intake creation.

    Revalidated for the same reason its members are: an envelope can itself be
    produced by ``model_construct`` or ``model_copy(update=...)``, and an
    ``isinstance`` check cannot tell that apart from a validated one."""

    model_config = _REVALIDATED

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

    checked = _revalidate(principal, Principal)
    if checked.channel != expected:
        raise DomainError("INVALID_INPUT", "authentication channel mismatch")
    return checked


def _revalidate(value: object, model: type[_ModelT]) -> _ModelT:
    """Re-run validation on an instance the caller says is already valid.

    ``isinstance`` cannot distinguish a validated instance from one built by
    ``model_construct`` or derived by ``model_copy(update=...)``; both skip
    every field validator. Re-running validation at the entry point is what
    makes the check mean something.
    """

    if not isinstance(value, model):
        raise DomainError("INVALID_INPUT", f"{model.__name__} is required")
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise DomainError("INVALID_INPUT", f"{model.__name__} is not valid") from error


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


def classify_intake_delivery(
    left: IntakeEnvelope, right: IntakeEnvelope
) -> IntakeDelivery:
    """Say how a second delivery relates to the first.

    A boolean cannot carry this answer. "Not the same request" covers two
    materially different situations: a new request, which the caller should
    accept, and a reused idempotency key over a different actor, channel,
    target or question, which is a client error the caller must surface rather
    than quietly open a second incident for. Returning three states makes the
    conflict a branch the caller has to write.
    """

    left = _revalidate(left, IntakeEnvelope)
    right = _revalidate(right, IntakeEnvelope)
    if left.request.idempotency_key != right.request.idempotency_key:
        return "different_request"
    if (
        left.principal == right.principal
        and left.request.target_id == right.request.target_id
        and left.request.question == right.request.question
    ):
        return "same_request"
    return "key_conflict"
