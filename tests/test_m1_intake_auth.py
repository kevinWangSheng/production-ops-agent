from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from opspilot.domain.base import DomainError, sanitized_errors
from opspilot.intake import (
    IntakeEnvelope,
    IntakeRequest,
    Principal,
    classify_intake_delivery,
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
    assert classify_intake_delivery(first, retry) == "same_request"


def test_a_fresh_key_is_a_new_request_not_a_conflict():
    request = dict(REQUEST)
    request["idempotency_key"] = "idem-2"
    assert (
        classify_intake_delivery(envelope(), envelope(request=request))
        == "different_request"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", "oncall-2"),
        ("auth_revision", "auth-v2"),
        ("channel", "event_token"),
    ],
)
def test_a_reused_key_under_a_different_identity_is_a_conflict(field, value):
    """Contract: operator, auth revision and channel all bind the key.

    The caller must be able to tell this apart from a new request, or it would
    open a second incident for what is a client error.
    """

    principal = dict(PRINCIPAL)
    principal[field] = value
    assert (
        classify_intake_delivery(envelope(), envelope(principal=principal))
        == "key_conflict"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("target_id", "payments-prod"), ("question", "a different question")],
)
def test_a_reused_key_over_different_content_is_a_conflict(field, value):
    """Contract: a reused key over different content is an identity conflict."""

    request = dict(REQUEST)
    request[field] = value
    assert (
        classify_intake_delivery(envelope(), envelope(request=request))
        == "key_conflict"
    )


@pytest.mark.parametrize("impostor", [None, "req-1", {"request_id": "req-1"}])
def test_classify_intake_delivery_requires_two_envelopes(impostor):
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        classify_intake_delivery(envelope(), impostor)


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


# --- the envelope revalidates whatever it is handed ---------------------------


def _tampered_principal():
    """A principal whose control-character check was skipped after the fact."""

    clean = Principal(**PRINCIPAL)
    return clean.model_copy(
        update={"actor_id": "oncall\x00-1", "channel": "event_token"}
    )


@pytest.mark.parametrize(
    ("name", "build"),
    [
        (
            "model_construct skips every validator",
            lambda: Principal.model_construct(
                actor_id="admin\x1b[31m", channel="ui_basic", auth_revision="auth-v1"
            ),
        ),
        ("model_copy(update=) skips them too", _tampered_principal),
    ],
)
def test_an_unvalidated_principal_cannot_reach_the_envelope(name, build):
    """The envelope is the boundary, so it must not trust what it is handed.

    Both of these are the ordinary ways to derive a frozen pydantic model, and
    both skip field validation, so without revalidation an identity that never
    passed the audit-legibility rule would reach durable creation.
    """

    with pytest.raises(ValidationError):
        envelope(principal=build())


def test_a_widened_subclass_cannot_carry_a_credential_into_the_envelope():
    """An `extra="allow"` subclass would otherwise ride straight into repr."""

    class WidenedPrincipal(Principal):
        model_config = Principal.model_config | {"extra": "allow"}

    leaky = WidenedPrincipal(**PRINCIPAL, password="PLACEHOLDER-NOT-A-PASSWORD")
    with pytest.raises(ValidationError):
        envelope(principal=leaky)


def test_a_subclass_without_extras_is_narrowed_to_the_declared_type():
    """Otherwise one logical identity compares unequal to itself."""

    class ElevatedPrincipal(Principal):
        pass

    stored = envelope(principal=ElevatedPrincipal(**PRINCIPAL)).principal
    assert type(stored) is Principal
    assert stored == Principal(**PRINCIPAL)


def test_a_blank_question_is_not_a_question():
    for blank in (" ", "  \n\t "):
        with pytest.raises(ValidationError):
            IntakeRequest(**{**REQUEST, "question": blank})


def test_every_request_field_is_accounted_for_in_the_delivery_comparison():
    """A field added later must not be silently left out of idempotency.

    `classify_intake_delivery` names the request fields one by one, so a new
    field would default to "not part of the request's identity" without anyone
    noticing. This pins that choice as deliberate.
    """

    compared = {"target_id", "question", "idempotency_key"}
    assert set(IntakeRequest.model_fields) == compared


# --- the API boundary revalidates, and errors redact attacker-chosen names ----


def test_a_credential_sent_as_a_field_name_is_not_exported_through_loc():
    """`extra_forbidden` puts the caller's own key in `loc`, not just `input`.

    Redacting the input alone would still export a secret sent as a field name
    rather than a field value.
    """

    with pytest.raises(ValidationError) as rejected:
        Principal(**PRINCIPAL, **{"Basic PLACEHOLDER-NOT-A-CREDENTIAL": "x"})
    rendered = repr(sanitized_errors(rejected.value))
    assert "PLACEHOLDER-NOT-A-CREDENTIAL" not in rendered
    assert "<redacted>" in rendered


def test_a_declared_field_path_is_still_reported():
    """Redaction must not blind the caller to which field actually failed."""

    with pytest.raises(ValidationError) as rejected:
        Principal(**{**PRINCIPAL, "actor_id": "oncall\x00-1"})
    assert sanitized_errors(rejected.value)[0]["loc"] == ("actor_id",)


@pytest.mark.parametrize(
    ("name", "update"),
    [
        (
            "a control character in a nested field",
            {"request": {**REQUEST, "target_id": "t\x00"}},
        ),
        ("a nested field removed", {"request": {"target_id": "t", "question": "q"}}),
        ("a member replaced by a string", {"request": "not-a-request"}),
        (
            "a credential on a nested member",
            {"principal": {**PRINCIPAL, "password": "x"}},
        ),
    ],
)
def test_an_unvalidated_envelope_is_refused_at_the_api_boundary(name, update):
    """`isinstance` cannot tell a validated envelope from a derived one.

    `model_copy(update=...)` and `model_construct` both skip validation, so the
    entry point revalidates rather than trusting the type.
    """

    valid = envelope()
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        classify_intake_delivery(valid, valid.model_copy(update=update))


def test_an_unvalidated_envelope_is_refused_even_when_built_wholesale():
    valid = envelope()
    forged = IntakeEnvelope.model_construct(
        request_id="req\x001",
        principal=Principal(**PRINCIPAL),
        request=IntakeRequest(**REQUEST),
        received_at=NOW,
    )
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        classify_intake_delivery(valid, forged)


def test_verify_channel_refuses_a_principal_that_skipped_validation():
    tampered = Principal(**PRINCIPAL).model_copy(update={"actor_id": "oncall\x00-1"})
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        verify_channel(tampered, expected="ui_basic")
