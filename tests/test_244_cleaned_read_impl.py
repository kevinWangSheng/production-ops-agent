"""#244 PR-3: cleaned evidence on the read paths that need no database.

A cleaned record keeps its view, both hashes and its identity; only ``raw``
is gone. The chart reads the view alone, so it still draws (a missing raw
digest is not a hash mismatch); the tool-level evidence read refuses the
record as not readable instead of raising.
"""

from __future__ import annotations

from dataclasses import replace

from opspilot.tools.evidence_read import EVIDENCE_TOOL, EvidenceReadTransport
from opspilot.tools.executor import EVIDENCE_SOURCE, TransportRequest
from opspilot.tools.otel_demo import METRICS_TOOL, TARGET_ID
from tests.test_m1_otel_demo_contract import PROMETHEUS_URL, WINDOW
from tests.test_m1_report_charts_contract import evidence, figure_of, render


def _cleaned(record):
    return replace(record, raw=None, payload_state="cleaned")


def test_a_cleaned_record_is_unverifiable_but_its_view_still_verifies() -> None:
    cleaned = _cleaned(evidence("ev-c"))
    assert cleaned.hashes_verified is None
    assert cleaned.view_verified is True
    assert _cleaned(evidence("ev-d", verified=False)).view_verified is False


def test_a_cleaned_metrics_record_still_charts_from_its_view() -> None:
    present = render(evidence("ev-c"))
    cleaned = render(_cleaned(evidence("ev-c")))
    assert cleaned is not None and "hash_mismatch" not in cleaned
    assert figure_of(cleaned) is not None
    assert cleaned == present


def test_a_cleaned_record_with_a_rewritten_view_is_still_a_hash_mismatch() -> None:
    html = render(_cleaned(evidence("ev-c", verified=False)))
    assert html is not None and 'data-reason="hash_mismatch"' in html


def test_a_missing_raw_that_is_not_cleaned_never_charts() -> None:
    """Review #382: the state, not a NULL raw, says cleaned."""
    html = render(replace(evidence("ev-c"), raw=None))
    assert html is not None and 'data-reason="hash_mismatch"' in html


class _Store:
    def __init__(self, record):
        self.record = record

    def get(self, evidence_id):
        return self.record


def test_the_tool_read_refuses_a_cleaned_record_without_raising() -> None:
    record = _cleaned(evidence("ev-c", status="error", adopted=False))
    record = replace(
        record,
        view={**record.view, "reason": "RESULT_TOO_LARGE", "target_id": TARGET_ID},
    )
    transport = EvidenceReadTransport(
        store=_Store(record),
        run_id=record.run_id,
        target_ids=frozenset({TARGET_ID}),
        result_paths={METRICS_TOOL: ("data", "result")},
    )
    response = transport.fetch(
        TransportRequest(
            operation_id="s-t0",
            source=EVIDENCE_SOURCE,
            verb="get",
            endpoint=PROMETHEUS_URL,
            selector={},
            params={"evidence_id": "ev-c", "limit": 1},
            window=WINDOW,
            timeout_seconds=5.0,
            max_result_bytes=1 << 20,
            credential_ref="otel-demo-ro",
            tool=EVIDENCE_TOOL,
            target_id=TARGET_ID,
        )
    )
    assert (response.source_status, response.sent) == ("EVIDENCE_NOT_READABLE", False)
