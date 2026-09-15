from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from opspilot.domain.base import DomainError
from opspilot.intake import (
    IntakeEnvelope,
    IntakeRequest,
    Principal,
    same_idempotent_intake,
    verify_channel,
)

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def envelope(**overrides):
    fields = {
        "request_id": "req-1",
        "principal": {
            "actor_id": "oncall-1",
            "channel": "ui_basic",
            "auth_revision": "auth-v1",
        },
        "request": {
            "target_id": "checkout-prod",
            "question": "why?",
            "idempotency_key": "idem-1",
        },
        "received_at": NOW,
    }
    fields.update(overrides)
    return IntakeEnvelope(**fields)


def test_ui_and_event_channels_are_distinct():
    principal = Principal(
        actor_id="oncall-1", channel="ui_basic", auth_revision="auth-v1"
    )
    assert verify_channel(principal, expected="ui_basic") == principal
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        verify_channel(principal, expected="event_token")


def test_idempotency_requires_the_same_authenticated_identity_and_payload():
    assert same_idempotent_intake(envelope(), envelope())
    assert not same_idempotent_intake(
        envelope(),
        envelope(
            principal={
                "actor_id": "oncall-2",
                "channel": "ui_basic",
                "auth_revision": "auth-v1",
            }
        ),
    )
    assert not same_idempotent_intake(
        envelope(),
        envelope(
            request={
                "target_id": "other",
                "question": "why?",
                "idempotency_key": "idem-1",
            }
        ),
    )


@pytest.mark.parametrize(
    "field", ["request_id", "target_id", "question", "idempotency_key"]
)
def test_required_intake_identity_fields_are_non_empty(field):
    values = {
        "request_id": "req-1",
        "target_id": "checkout-prod",
        "question": "why?",
        "idempotency_key": "idem-1",
    }
    values[field] = ""
    payload = {
        "target_id": values["target_id"],
        "question": values["question"],
        "idempotency_key": values["idempotency_key"],
    }
    if field == "request_id":
        with pytest.raises(ValidationError):
            envelope(request_id="")
    else:
        with pytest.raises(ValidationError):
            IntakeRequest(**payload)


def test_credentials_and_raw_auth_material_are_not_representable():
    with pytest.raises(ValidationError):
        Principal(
            actor_id="oncall-1",
            channel="ui_basic",
            auth_revision="auth-v1",
            password="secret",
        )


def test_control_characters_are_rejected_from_idempotency_and_question():
    with pytest.raises(ValidationError, match="CONTROL_CHARACTER_FORBIDDEN"):
        IntakeRequest(
            target_id="checkout-prod", question="why?\x00", idempotency_key="idem-1"
        )
