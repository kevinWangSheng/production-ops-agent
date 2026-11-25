"""#859: a v3-only Jaeger is a new model input channel; a credential in it
never reaches the model or the stored evidence (PRODUCT-CONSTRAINTS data
flow; pre-review D6). Guarded by mutation M67.

The OTLP span ``status.message`` (mapped to ``otel.status_description``) and
the ``exception`` event's attributes (mapped to span-log fields) are the
fields the trace projection shows as clipped error detail; the D6 contract
routes them through the same redact-then-clip step as v1. Any other span
attribute is not shown at all. Each case also plants a non-credential marker
in the same field and requires it to arrive, so the check is not vacuous.

Independent test (implementation unseen); fixtures and the faked Jaeger are
in ``tests/jaeger_v3_support.py``.
"""

from __future__ import annotations

import pytest

from tests.jaeger_v3_support import (
    TRACE_A,
    JaegerDouble,
    default_spans,
    ext_gateway,
)
from tests.safety.gateway_support import everything_seen

#: Low-entropy synthetic credentials, assembled so no source line is itself
#: a credential-shaped literal for the secret scanner.
STATUS_SECRET = "canary-v3-status-" + "a" * 8
EVENT_SECRET = "canary-v3-event-" + "b" * 8
STACK_SECRET = "canary-v3-stack-" + "c" * 8
ATTRIBUTE_SECRET = "canary-v3-attr-" + "d" * 8
SECRETS = (STATUS_SECRET, EVENT_SECRET, STACK_SECRET, ATTRIBUTE_SECRET)

STATUS_MARKER = "status-marker-v3 upstream refused"
EVENT_MARKER = "event-marker-v3 connect failed"
STACK_MARKER = "stack-marker-v3 at charge()"


def _planted() -> list:
    return default_spans(
        status_message=f"{STATUS_MARKER}; password={STATUS_SECRET}",
        exception_message=(
            f"{EVENT_MARKER} dsn postgres://admin:{EVENT_SECRET}@db.internal:5432/app"
        ),
        exception_stacktrace=f"{STACK_MARKER}\nAuthorization: Bearer {STACK_SECRET}",
        # not an error-detail key: the projection drops it, shown or not
        extra_attribute=(
            "http.url",
            f"https://svc:{ATTRIBUTE_SECRET}@payments.internal/charge",
        ),
    )


CASES = {
    "traces_search": {"service": "checkout"},
    "traces_search_by_tags": {"service": "payment"},
    "traces_query_trace_by_id": {"trace_id": TRACE_A},
}


@pytest.mark.parametrize("tool", sorted(CASES))
def test_v3_status_message_and_exception_event_credentials_are_redacted(
    monkeypatch, tmp_path, tool
):
    """M67: the v3 status message and exception event are redacted before
    the model view and the evidence record."""
    double = JaegerDouble("v3", spans=_planted())
    gw = ext_gateway(monkeypatch, tmp_path, double)
    outcome = gw.call(tool, CASES[tool])

    # by trace ID, v2.23 still serves ``/api/traces/{id}``: either API may
    # answer it, the redaction promise is the same
    assert double.requests
    assert outcome.status == "ok", outcome.model_view
    seen = everything_seen(outcome, gw.evidence)
    for marker in (STATUS_MARKER, EVENT_MARKER, STACK_MARKER):
        assert marker in seen, f"{marker!r} never reached the model; check is vacuous"
    leaked = [secret for secret in SECRETS if secret in seen]
    assert leaked == []
