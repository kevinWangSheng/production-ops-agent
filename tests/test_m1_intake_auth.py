from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from opspilot.domain.base import DomainError, sanitized_errors
from opspilot.intake import (
    IntakeEnvelope,
    IntakeRequest,
    Principal,
    same_idempotent_intake,
    verify_channel,
)

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
PRINCIPAL = {"actor_id": "oncall-1", "channel": "ui_basic", "auth_revision": "auth-v1"}
REQUEST = {
    "target_id": "checkout-prod",
    "question": "why?",
    "idempotency_key": "idem-1",
}


def envelope(**overrides):
    fields = {
        "request_id": "req-1",
        "principal": dict(PRINCIPAL),
        "request": dict(REQUEST),
        "received_at": NOW,
    }
    fields.update(overrides)
    return IntakeEnvelope(**fields)


# --- authentication channels -------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "other"), [("ui_basic", "event_token"), ("event_token", "ui_basic")]
)
def test_each_channel_is_accepted_only_at_its_own_entry_point(channel, other):
    """Both directions: neither channel may stand in for the other."""

    principal = Principal(actor_id="oncall-1", channel=channel, auth_revision="auth-v1")
    assert verify_channel(principal, expected=channel) == principal
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        verify_channel(principal, expected=other)


@pytest.mark.parametrize("impostor", [None, "ui_basic", {"channel": "ui_basic"}, 1])
def test_verify_channel_rejects_anything_that_is_not_a_principal(impostor):
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        verify_channel(impostor, expected="ui_basic")


# --- idempotency -------------------------------------------------------------


def test_a_redelivery_of_the_same_request_is_recognised_as_the_same_request():
    """The positive half of idempotency: a genuine retry must merge.

    A real redelivery arrives with a new request id and a new receipt time, so
    an envelope-wide comparison would never merge anything.
    """

    first = envelope()
    retry = envelope(
        request_id="req-2",
        received_at=datetime(2026, 9, 15, 12, 5, tzinfo=timezone.utc),
    )
    assert same_idempotent_intake(first, retry)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", "oncall-2"),
        ("auth_revision", "auth-v2"),
        ("channel", "event_token"),
    ],
)
def test_a_different_authenticated_identity_never_merges(field, value):
    """Contract: operator, auth revision and channel all bind the key."""

    principal = dict(PRINCIPAL)
    principal[field] = value
    assert not same_idempotent_intake(envelope(), envelope(principal=principal))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_id", "payments-prod"),
        ("question", "a different question"),
        ("idempotency_key", "idem-2"),
    ],
)
def test_a_different_target_question_or_key_never_merges(field, value):
    """Contract: a reused key over different content is an identity conflict."""

    request = dict(REQUEST)
    request[field] = value
    assert not same_idempotent_intake(envelope(), envelope(request=request))


@pytest.mark.parametrize("impostor", [None, "req-1", {"request_id": "req-1"}])
def test_same_idempotent_intake_requires_two_envelopes(impostor):
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        same_idempotent_intake(envelope(), impostor)


# --- immutability and shape --------------------------------------------------


@pytest.mark.parametrize(
    ("build", "field"),
    [
        (lambda: Principal(**PRINCIPAL), "actor_id"),
        (lambda: IntakeRequest(**REQUEST), "target_id"),
        (envelope, "request_id"),
    ],
)
def test_intake_objects_cannot_be_mutated_after_construction(build, field):
    """A verified identity must not be rewritable once it exists."""

    subject = build()
    with pytest.raises(ValidationError):
        setattr(subject, field, "rewritten")


def test_the_receipt_time_must_carry_a_timezone():
    with pytest.raises(ValidationError):
        envelope(received_at=datetime(2026, 9, 15, 12))


@pytest.mark.parametrize(
    "field", ["request_id", "actor_id", "auth_revision", "target_id", "idempotency_key"]
)
def test_identifiers_are_bounded(field):
    """An identifier is written to an audit line, so it carries an upper bound."""

    with pytest.raises(ValidationError):
        _build_with(field, "x" * 257)


def test_the_question_is_bounded():
    with pytest.raises(ValidationError):
        IntakeRequest(**{**REQUEST, "question": "x" * 16_385})


@pytest.mark.parametrize(
    "field", ["request_id", "target_id", "question", "idempotency_key"]
)
def test_required_intake_identity_fields_are_non_empty(field):
    with pytest.raises(ValidationError):
        _build_with(field, "")


# --- credentials -------------------------------------------------------------


def test_an_extra_credential_field_cannot_enter_a_principal():
    """`extra="forbid"` is what the contract layer itself enforces.

    It does not and cannot decide that a well-formed `actor_id` is secret; the
    trusted adapter owns that boundary (see the module docstring).
    """

    with pytest.raises(ValidationError):
        Principal(**PRINCIPAL, password="secret")
    with pytest.raises(ValidationError):
        Principal(**PRINCIPAL, authorization="Basic abc")


def test_strict_mode_refuses_a_raw_header_passed_as_bytes():
    with pytest.raises(ValidationError):
        Principal(**{**PRINCIPAL, "actor_id": b"Basic PLACEHOLDER-NOT-A-CREDENTIAL"})


@pytest.mark.parametrize(
    "payload",
    [
        {**PRINCIPAL, "password": "PLACEHOLDER-NOT-A-PASSWORD"},
        {**PRINCIPAL, "authorization": "Basic PLACEHOLDER-NOT-A-CREDENTIAL"},
        {**PRINCIPAL, "actor_id": "Basic PLACEHOLDER-NOT-A-CREDENTIAL\x00"},
        {**PRINCIPAL, "channel": "Bearer PLACEHOLDER-NOT-A-TOKEN"},
        {**PRINCIPAL, "auth_revision": "x" * 300},
    ],
)
def test_a_rejected_secret_does_not_survive_in_any_error_rendering(payload):
    """Rejecting a credential must not export it through the failure.

    `hide_input_in_errors` only covers `str(exc)`; `errors()` and `json()` are
    the paths a structured logger takes, so the sanitized rendering is the one
    product code is allowed to use.
    """

    secrets = (
        "PLACEHOLDER-NOT-A-PASSWORD",
        "PLACEHOLDER-NOT-A-CREDENTIAL",
        "PLACEHOLDER-NOT-A-TOKEN",
    )
    with pytest.raises(ValidationError) as rejected:
        Principal(**payload)
    rendered = f"{rejected.value}{sanitized_errors(rejected.value)}"
    assert not any(secret in rendered for secret in secrets), rendered


# --- audit-legible text ------------------------------------------------------

AMBIGUOUS = {
    "nul": "\x00",
    "newline": "\n",
    "delete": "\x7f",
    "c1_csi": "\x9b",
    "soft_hyphen": "­",
    "zero_width_space": "​",
    "left_to_right_mark": "‎",
    "right_to_left_override": "‮",
    "left_to_right_isolate": "⁦",
    "line_separator": " ",
    "paragraph_separator": " ",
    "byte_order_mark": "﻿",
    "tag_character": "\U000e0001",
    "no_break_space": " ",
    "ideographic_space": "　",
    "narrow_no_break_space": " ",
    "trailing_space": " ",
}


@pytest.mark.parametrize("char", AMBIGUOUS.values(), ids=list(AMBIGUOUS))
@pytest.mark.parametrize(
    "field", ["request_id", "actor_id", "auth_revision", "target_id", "idempotency_key"]
)
def test_identifiers_reject_every_class_of_ambiguous_text(field, char):
    """Two distinct identifiers must never print as one identical audit line.

    A non-breaking space renders exactly like a space, a bidirectional override
    reorders what is shown, and a line separator splits one record into two.
    """

    with pytest.raises(ValidationError, match="CONTROL_CHARACTER_FORBIDDEN"):
        _build_with(field, f"value{char}suffix")


@pytest.mark.parametrize(
    "text",
    [
        "why is checkout slow?",
        "中文提问",
        "café latency",
        "first line\nsecond line",
        "pasted\tlog\tcolumns",
        "windows paste\r\nsecond line",
        "👨‍👩‍👧 family emoji uses a joiner",
        "می‌رود",
    ],
)
def test_the_question_keeps_the_text_an_operator_actually_pastes(text):
    """Free text is not an identifier: it keeps newlines, tabs and joiners."""

    assert IntakeRequest(**{**REQUEST, "question": text}).question == text


@pytest.mark.parametrize("char", ["\x00", "\x1b", " ", " ", "‮", "‏", "⁦"])
def test_the_question_still_refuses_record_breaking_and_direction_spoofing(char):
    with pytest.raises(ValidationError, match="CONTROL_CHARACTER_FORBIDDEN"):
        IntakeRequest(**{**REQUEST, "question": f"why{char}slow"})


def _build_with(field: str, value):
    """Build whichever type owns `field`, with that field set to `value`."""

    if field == "request_id":
        return envelope(request_id=value)
    if field in PRINCIPAL:
        return Principal(**{**PRINCIPAL, field: value})
    return IntakeRequest(**{**REQUEST, field: value})
