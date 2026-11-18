"""Report validation: a report's authority is the captured evidence it cites
(PRODUCT-CONSTRAINTS "Evidence and context requirements": a report link
resolves to actual evidence; facts, hypotheses and gaps stay distinguishable;
"Product workflow": an incomplete investigation is not an uncertain finding).

Table-driven over the report model and the citation bind
(``opspilot.investigation.reports``): one good report passes, and each row
breaks exactly one rule and must be refused. Rules are named by their error
code, never by message wording.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from opspilot.investigation.reports import (
    DeliveredView,
    ReportV2,
    unsupported_citations,
)

TARGET = "m0-otel-20260909"
POLICY = "policy-window-1"
VIEWS = (
    DeliveredView(
        evidence_id="ev-ok",
        target_ids=frozenset({TARGET}),
        status="ok",
        time_scope_refs=frozenset({POLICY}),
        citable_as_fact=True,
    ),
    DeliveredView(
        evidence_id="ev-nodata",
        target_ids=frozenset({TARGET}),
        status="no_data",
        time_scope_refs=frozenset({POLICY}),
        citable_as_fact=False,
    ),
    # A failed read whose view still says citable: the status decides.
    DeliveredView(
        evidence_id="ev-failed-flagged",
        target_ids=frozenset({TARGET}),
        status="failed",
        time_scope_refs=frozenset({POLICY}),
        citable_as_fact=True,
    ),
    DeliveredView(
        evidence_id="ev-ok-not-citable",
        target_ids=frozenset({TARGET}),
        status="ok",
        time_scope_refs=frozenset({POLICY}),
        citable_as_fact=False,
    ),
    DeliveredView(
        evidence_id="ev-other-target",
        target_ids=frozenset({"payments-prod"}),
        status="ok",
        time_scope_refs=frozenset({POLICY}),
        citable_as_fact=True,
    ),
    DeliveredView(
        evidence_id="ev-other-window",
        target_ids=frozenset({TARGET}),
        status="ok",
        time_scope_refs=frozenset({"policy-other"}),
        citable_as_fact=True,
    ),
)


def _fact(**overrides: Any) -> dict[str, Any]:
    return {
        "kind": "fact",
        "text": "checkout error ratio rose to 0.4 after 12:01",
        "evidence_ids": ["ev-ok"],
        "target_refs": [TARGET],
        "time_scope_ref": POLICY,
        **overrides,
    }


def _report(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": "checkout errors follow the 12:00 deploy",
        "claims": [_fact()],
        "gaps": [],
        **overrides,
    }


def _refused_by_citations(payload: dict[str, Any]) -> bool:
    report = ReportV2.model_validate(payload)
    return unsupported_citations(
        report,
        views=VIEWS,
        authorized_targets=frozenset({TARGET}),
        time_policy_ids=[POLICY, "policy-other"],
    )


def test_a_report_citing_its_own_ok_evidence_is_accepted():
    assert not _refused_by_citations(_report())


#: Each row: a fact claim that cites evidence it may not rest on.
CITATION_BREAKS = {
    "undelivered evidence id": _fact(evidence_ids=["ev-never-delivered"]),
    "a no_data view": _fact(evidence_ids=["ev-nodata"]),
    "a failed view flagged citable": _fact(evidence_ids=["ev-failed-flagged"]),
    "an ok view not citable as fact": _fact(evidence_ids=["ev-ok-not-citable"]),
    "a view of another target": _fact(evidence_ids=["ev-other-target"]),
    "a view delivered under another time scope": _fact(
        evidence_ids=["ev-other-window"]
    ),
    "an unknown time scope": _fact(time_scope_ref="policy-unknown"),
}


@pytest.mark.parametrize("claim", CITATION_BREAKS.values(), ids=list(CITATION_BREAKS))
@pytest.mark.parametrize("kind", ["fact", "counter_evidence", "rejected_hypothesis"])
def test_a_fact_like_claim_resting_on_the_wrong_evidence_is_refused(claim, kind):
    payload = _report(conclusion="partial", claims=[_fact(), {**claim, "kind": kind}])
    assert _refused_by_citations(payload)


#: Each row: a report whose shape misstates what was established.
MODEL_BREAKS = {
    "FACT_SCOPE_REQUIRED": _report(claims=[_fact(evidence_ids=[])]),
    "SUPPORTED_REPORT_REQUIRES_FACT": _report(
        claims=[{"kind": "hypothesis", "text": "maybe the deploy", "evidence_ids": []}]
    ),
    "INCOMPLETE_REPORT_CONTRACT (incomplete but supported)": _report(
        assessment_status="incomplete", gaps=["traces not read"]
    ),
    "INCOMPLETE_REPORT_CONTRACT (incomplete without gaps)": _report(
        assessment_status="incomplete", conclusion="inconclusive", gaps=[]
    ),
    "EMPTY_SUMMARY": _report(summary="   "),
}


@pytest.mark.parametrize(
    ("name", "payload"), MODEL_BREAKS.items(), ids=list(MODEL_BREAKS)
)
def test_a_report_that_misstates_what_was_established_is_refused(name, payload):
    with pytest.raises(ValidationError) as refused:
        ReportV2.model_validate(payload)
    assert name.split(" ")[0] in str(refused.value)


def test_an_incomplete_inconclusive_report_with_gaps_is_accepted():
    """The control for the incomplete rows: stating the gap is allowed."""
    payload = _report(
        assessment_status="incomplete",
        conclusion="inconclusive",
        claims=[],
        gaps=["traces not read"],
    )
    assert ReportV2.model_validate(payload).assessment_status == "incomplete"
