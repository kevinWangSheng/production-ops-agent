"""#443: /metrics and logs (``health._revision``) and the outbox
``image_revision`` give the same result for any ``OPSPILOT_REVISION``."""

from __future__ import annotations

import pytest

from opspilot import health
from opspilot.persistence import outbox


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc123", "abc123"),
        ("a b", "unknown"),
        ("-lead", "unknown"),
        ("", "unknown"),
        ("a" * 65, "unknown"),
    ],
)
def test_revision_same_in_health_and_outbox(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv(health.REVISION_ENV, raw)
    stored = outbox.build_payload(outbox.image_fields())["image_revision"]
    assert health._revision() == expected
    assert stored == expected
