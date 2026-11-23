"""The postmortem drafting model input carries only the published report's
summary, claims and gaps (#807, B1-U5; PRODUCT-CONSTRAINTS "Data flow
contract"). The incident's ``conclusion`` column holds the whole terminal
step -- operator replies, prompt revision, calibration, the raw report JSON
with its root cause -- and none of it may reach the model. The report's own
markers must arrive, so the check is not vacuous. Guarded by mutation M59.
"""

from __future__ import annotations

from tests.postmortem_input_support import (
    SENTINELS,
    SUMMARY,
    model_visible,
    report_content,
    terminal_step,
)


def test_terminal_step_bookkeeping_never_reaches_the_drafting_model():
    """M59: the payload's report is the allowlisted projection, never the
    raw ``conclusion`` column."""
    _, sent = model_visible(terminal_step(report_content()))

    assert SUMMARY in sent, "the report never reached the model; the check is vacuous"
    leaked = [field for field, sentinel in SENTINELS.items() if sentinel in sent]
    assert leaked == []
