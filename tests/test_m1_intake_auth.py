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


def test_a_rejected_secret_does_not_survive_in_the_validation_error():
    """Rejecting a credential field must not export it through the error.

    An adapter that hands a raw payload to these contracts will log the
    rejection; the message must not become the leak the field prevented.
    """

    with pytest.raises(ValidationError) as rejected_extra:
        Principal(
            actor_id="oncall-1",
            channel="ui_basic",
            auth_revision="auth-v1",
            password="s3cr3t-password",
        )
    assert "s3cr3t-password" not in str(rejected_extra.value)

    with pytest.raises(ValidationError) as rejected_header:
        Principal(
            actor_id=b"Basic b25jYWxsLTE6czNjcjN0",
            channel="ui_basic",
            auth_revision="auth-v1",
        )
    assert "b25jYWxsLTE6czNjcjN0" not in str(rejected_header.value)


CONTROL_TEXT = {
    "nul": "\x00",
    "newline": "\n",
    "delete": "\x7f",
    "c1_csi": "\x9b",
    "soft_hyphen": "\u00ad",
    "zero_width_space": "\u200b",
    "left_to_right_mark": "\u200e",
    "right_to_left_override": "\u202e",
    "left_to_right_isolate": "\u2066",
    "line_separator": "\u2028",
    "paragraph_separator": "\u2029",
    "byte_order_mark": "\ufeff",
    "tag_character": "\U000e0001",
}


@pytest.mark.parametrize("char", CONTROL_TEXT.values(), ids=list(CONTROL_TEXT))
@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "actor_id",
        "auth_revision",
        "target_id",
        "question",
        "idempotency_key",
    ],
)
def test_every_guarded_field_rejects_ambiguous_text(field, char):
    """Each guarded field rejects each class of invisible or line-breaking text.

    A bidirectional override or a zero-width character would let a stored
    identity render in an audit line as a different identity; a line or
    paragraph separator would split one audit record into two.
    """

    tainted = f"value{char}suffix"
    if field == "request_id":
        with pytest.raises(ValidationError, match="CONTROL_CHARACTER_FORBIDDEN"):
            envelope(request_id=tainted)
    elif field in {"actor_id", "auth_revision"}:
        payload = {
            "actor_id": "oncall-1",
            "channel": "ui_basic",
            "auth_revision": "auth-v1",
        }
        payload[field] = tainted
        with pytest.raises(ValidationError, match="CONTROL_CHARACTER_FORBIDDEN"):
            Principal(**payload)
    else:
        payload = {
            "target_id": "checkout-prod",
            "question": "why?",
            "idempotency_key": "idem-1",
        }
        payload[field] = tainted
        with pytest.raises(ValidationError, match="CONTROL_CHARACTER_FORBIDDEN"):
            IntakeRequest(**payload)


@pytest.mark.parametrize(
    "text",
    [
        "why is checkout slow?",
        "\u4e2d\u6587\u63d0\u95ee",
        "caf\u00e9 latency",
        "a b\tc".replace("\t", " "),
    ],
)
def test_ordinary_text_is_not_rejected(text):
    """The guard must reject ambiguity, not non-ASCII prose."""

    assert (
        IntakeRequest(
            target_id="checkout-prod", question=text, idempotency_key="idem-1"
        ).question
        == text
    )
