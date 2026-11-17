"""Independent #686 U2/U3/D6 tests at the public report/response boundaries.

Interpretation: r1 does not specify a nearest-id metric or hint wording.
Use single-character substitutions with an unambiguous closest value, and
check identifiers rather than a new sentence's exact bytes. No threshold,
tie-break rule, ordering, or inheritance in continuation Runs is prescribed.
"""

from __future__ import annotations

import json

import pytest

from opspilot.investigation.reports import (
    DeliveredView,
    evidence_context_projection,
    split_response,
    validated_report,
)
from tests.m1_investigation_support import report_json
from tests.m1_tool_support import historical_window_context

TARGETS = frozenset({"checkout-prod"})
VIEWS = tuple(
    DeliveredView(
        evidence_id=evidence_id,
        target_ids=TARGETS,
        status="ok",
        time_scope_refs=frozenset({"policy-window-1"}),
        citable_as_fact=True,
    )
    for evidence_id in ("ev-alpha", "ev-zulu")
)


def _report():
    return json.loads(report_json(evidence_id="ev-alpha"))


def _validate(payload, *, wrapped=False, context=None):
    content = json.dumps(payload)
    if wrapped:
        envelope = json.dumps(
            {"schema_version": "m0-response-v1", "replies": [], "report": payload}
        )
        split = split_response(envelope)
        assert split.wrapped and split.report_content is not None
        content = split.report_content
    return validated_report(
        content,
        finish_reason="stop",
        delivered=VIEWS,
        evidence_context=context or historical_window_context("run-686"),
        authorized_targets=TARGETS,
    )


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "p1-report"])
@pytest.mark.parametrize("kind", ["hypothesis", "recommendation"])
def test_non_fact_claim_missing_evidence_ids_defaults_to_empty(kind, wrapped):
    payload = _report()
    payload["claims"].append(
        {"kind": kind, "text": "A human could investigate this possibility."}
    )
    report, reason, feedback = _validate(payload, wrapped=wrapped)
    assert report is not None, (reason, feedback)
    assert report.claims[-1].evidence_ids == []
    assert reason == "" and feedback == ""


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "p1-report"])
def test_fact_missing_evidence_ids_is_fact_scope_required(wrapped):
    payload = _report()
    del payload["claims"][0]["evidence_ids"]
    report, reason, feedback = _validate(payload, wrapped=wrapped)
    assert report is None and reason == "REPORT_INVALID"
    assert "FACT_SCOPE_REQUIRED" in feedback


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "p1-report"])
@pytest.mark.parametrize("evidence_ids", [None, []], ids=["missing", "empty"])
def test_root_cause_evidence_ids_remains_required_and_nonempty(wrapped, evidence_ids):
    payload = _report()
    payload["schema_version"] = "m0-report-v3"
    payload["root_cause"] = {"kind": "service", "name": "checkout"}
    if evidence_ids is not None:
        payload["root_cause"]["evidence_ids"] = evidence_ids
    report, reason, feedback = _validate(payload, wrapped=wrapped)
    assert report is None and reason == "REPORT_INVALID"
    assert "root_cause.evidence_ids" in feedback


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "p1-report"])
def test_missing_next_steps_defaults_to_empty(wrapped):
    payload = _report()
    del payload["next_steps"]
    report, reason, feedback = _validate(payload, wrapped=wrapped)
    assert report is not None, (reason, feedback)
    assert report.next_steps == []
    assert reason == "" and feedback == ""


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "p1-report"])
def test_missing_gaps_is_still_rejected(wrapped):
    payload = _report()
    del payload["gaps"]
    report, reason, feedback = _validate(payload, wrapped=wrapped)
    assert report is None and reason == "REPORT_INVALID"
    assert "gaps" in feedback and "schema_error" in feedback


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "p1-report"])
def test_incomplete_report_still_requires_a_nonempty_gap(wrapped):
    payload = _report()
    payload.update(assessment_status="incomplete", conclusion="inconclusive", gaps=[])
    report, reason, feedback = _validate(payload, wrapped=wrapped)
    assert report is None and reason == "REPORT_INVALID"
    assert "INCOMPLETE_REPORT_CONTRACT" in feedback


@pytest.mark.parametrize(
    "same_claim", [False, True], ids=["separate-claims", "same-claim"]
)
def test_retry_hint_names_nearest_delivered_id_for_each_wrong_evidence_id(same_claim):
    payload = _report()
    claim = payload["claims"][0]
    if same_claim:
        claim["evidence_ids"] = ["ev-alphx", "ev-zulx"]
    else:
        payload["claims"] = [
            {**claim, "evidence_ids": [wrong]} for wrong in ("ev-alphx", "ev-zulx")
        ]
    report, reason, feedback = _validate(payload)
    assert report is None and reason == "REPORT_INVALID"
    assert "unknown_evidence_id" in feedback
    for wrong, nearest in (("ev-alphx", "ev-alpha"), ("ev-zulx", "ev-zulu")):
        assert wrong in feedback
        assert nearest in feedback, (
            f"{wrong} must name its unique nearest delivered id: {feedback}"
        )


@pytest.mark.parametrize(
    ("wrong", "nearest"), [("ev-alphx", "ev-alpha"), ("ev-zulx", "ev-zulu")]
)
def test_nearest_evidence_id_is_selected_for_the_individual_error(wrong, nearest):
    payload = _report()
    payload["claims"][0]["evidence_ids"] = [wrong]
    report, reason, feedback = _validate(payload)
    assert report is None and reason == "REPORT_INVALID"
    assert wrong in feedback and nearest in feedback


def test_root_cause_bad_citations_also_name_each_nearest_delivered_id():
    payload = _report()
    payload["schema_version"] = "m0-report-v3"
    payload["root_cause"] = {
        "kind": "service",
        "name": "checkout",
        "evidence_ids": ["ev-alphx", "ev-zulx"],
    }
    report, reason, feedback = _validate(payload)
    assert report is None and reason == "REPORT_INVALID"
    assert "root_cause" in feedback and "unknown_evidence_id" in feedback
    assert all(
        identifier in feedback
        for identifier in ("ev-alphx", "ev-alpha", "ev-zulx", "ev-zulu")
    )


def _projected_context():
    context = historical_window_context("run-686")
    context["target_catalog"] = {
        "svc-alpha": {"target_id": "checkout-prod"},
        "svc-zulu": {"target_id": "checkout-prod"},
    }
    # These raw, non-contract keys are deliberately absent from the projection
    # passed to the validator. The hint may not mine additional candidate ids.
    context["private_candidate_ids"] = [
        "ev-alphx",
        "svc-alphx",
        "policy-window-x",
        "PRIVATE-686",
    ]
    return evidence_context_projection(context, run_id="run-686")


@pytest.mark.parametrize(
    ("field", "wrong", "nearest"),
    [
        ("target_refs", "svc-alphx", "svc-alpha"),
    ],
)
def test_retry_hint_names_unique_nearest_scope_ref(field, wrong, nearest):
    payload = _report()
    payload["claims"][0]["target_refs"] = ["svc-alpha"]
    payload["claims"][0][field] = [wrong] if field == "target_refs" else wrong
    report, reason, feedback = _validate(payload, context=_projected_context())
    assert report is None and reason == "REPORT_INVALID"
    assert wrong in feedback
    assert nearest in feedback
    assert "PRIVATE-686" not in feedback


def test_missing_target_refs_hint_lists_allowed_projected_values():
    payload = _report()
    # A nonempty but unknown ref reaches the citation gate's
    # missing_target_refs. An absent/empty key is FACT_SCOPE_REQUIRED instead.
    payload["claims"][0]["target_refs"] = ["svc-alphx"]
    report, reason, feedback = _validate(payload, context=_projected_context())
    assert report is None and reason == "REPORT_INVALID"
    assert "missing_target_refs" in feedback
    assert "svc-alpha" in feedback and "svc-zulu" in feedback
    assert "PRIVATE-686" not in feedback
