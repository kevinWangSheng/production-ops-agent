"""#807 (B1-U5, B2-U2): the postmortem drafting model receives only the
published report's summary, claims and gaps -- nothing else from the terminal
step the incident's ``conclusion`` column stores, and not the root cause.

Written by an independent test author before the fix; driven through the
public drafting path ``build_input`` -> ``messages`` (what the worker sends).
"""

from __future__ import annotations

import pytest

from opspilot.knowledge import contract
from tests.postmortem_input_support import (
    CLAIMS,
    GAPS,
    SENTINELS,
    SUMMARY,
    model_visible,
    report_content,
    terminal_step,
)


def test_drafting_input_is_the_published_reports_summary_claims_and_gaps():
    report, sent = model_visible(terminal_step(report_content()))

    assert report is not None
    assert set(report) == {"summary", "claims", "gaps"}
    assert report["summary"] == SUMMARY
    assert [(c["kind"], c["text"]) for c in report["claims"]] == [
        (c["kind"], c["text"]) for c in CLAIMS
    ]
    assert report["gaps"] == list(GAPS)
    for field, sentinel in SENTINELS.items():
        assert sentinel not in sent, field


@pytest.mark.parametrize(
    "content", [None, "{not json"], ids=["no-report", "unparseable"]
)
def test_a_step_without_a_readable_report_gives_the_model_no_report(content):
    report, sent = model_visible(terminal_step(content))

    assert report is None
    for field, sentinel in SENTINELS.items():
        assert sentinel not in sent, field


def test_input_policy_version_moves_with_the_input_change():
    # f13-postmortem-input-v4 was the input before #807; drafts made from the
    # old input must read as a different policy.
    assert contract.POSTMORTEM_INPUT_POLICY_VERSION == "f13-postmortem-input-v5"
