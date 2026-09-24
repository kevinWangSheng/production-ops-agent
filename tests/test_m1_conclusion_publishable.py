"""The one publish rule both drivers apply to a committed conclusion row."""

import hashlib

from opspilot.investigation.context import conclusion_publishable

CONTENT = '{"schema_version": "m0-report-v2"}'


def _row(**overrides):
    body = {
        "execution": "completed",
        "handoff": False,
        "handoff_reasons": [],
        "report_schema_version": "m0-report-v2",
        "report_content": CONTENT,
        "report_content_sha256": hashlib.sha256(CONTENT.encode()).hexdigest(),
    }
    body.update(overrides)
    return {"kind": "conclusion", "conclusion": body}


def test_a_qualified_report_without_handoff_is_publishable():
    assert conclusion_publishable(_row()) is True


def test_every_handoff_shape_is_parked_not_published():
    assert conclusion_publishable(_row(handoff=True)) is False
    assert conclusion_publishable(_row(execution="budget_exhausted")) is False
    assert conclusion_publishable(_row(execution="failed", handoff=True)) is False
    assert conclusion_publishable(_row(report_schema_version=None)) is False


def test_a_malformed_recovered_row_is_not_published():
    # Bot review (PR #44): a row with a non-string schema version, missing
    # content or a digest mismatch fails closed.
    assert conclusion_publishable(_row(report_schema_version=2)) is False
    assert conclusion_publishable(_row(report_schema_version="")) is False
    assert conclusion_publishable(_row(report_content=None)) is False
    assert conclusion_publishable(_row(report_content_sha256="0" * 64)) is False
    assert conclusion_publishable({"kind": "conclusion"}) is False
    assert conclusion_publishable({"kind": "conclusion", "conclusion": []}) is False
