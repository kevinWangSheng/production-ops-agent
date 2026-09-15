"""Authenticated intake contracts for the first M1 vertical slice.

This module deliberately stops at the trust boundary. A future HTTP adapter
may turn verified proxy credentials or the separate event token into these
objects; raw headers, password material and bearer tokens never enter them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator

from .domain.base import DTO, DomainError, Text

AuthChannel = Literal["ui_basic", "event_token"]


class Principal(DTO):
    """Identity already verified by the trusted authentication adapter."""

    actor_id: Text
    channel: AuthChannel
    auth_revision: Text

    @field_validator("actor_id", "auth_revision")
    @classmethod
    def reject_control_text(cls, value: str) -> str:
        return _reject_control_text(value)


class IntakeRequest(DTO):
    """An authenticated request to start or resume one incident intake."""

    target_id: Text
    question: str = Field(min_length=1, max_length=16_384)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("target_id", "question", "idempotency_key")
    @classmethod
    def reject_control_text(cls, value: str) -> str:
        return _reject_control_text(value)


class IntakeEnvelope(DTO):
    """The only data a controller may pass to durable intake creation."""

    request_id: Text
    principal: Principal
    request: IntakeRequest
    received_at: AwareDatetime

    @field_validator("request_id")
    @classmethod
    def reject_control_text(cls, value: str) -> str:
        return _reject_control_text(value)

    @field_validator("received_at")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return value


def verify_channel(principal: Principal, *, expected: AuthChannel) -> Principal:
    """Require the caller to use the channel intended for this entry point."""

    if not isinstance(principal, Principal) or principal.channel != expected:
        raise DomainError("INVALID_INPUT", "authentication channel mismatch")
    return principal


def _reject_control_text(value: str) -> str:
    if any(ord(char) < 32 for char in value):
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
