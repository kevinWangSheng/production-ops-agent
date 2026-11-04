"""#456: the forced-final retry feedback must also see a report fenced inside
surrounding prose, and name its citation failures.

Probe a26613d9 (#432) replied with prose followed by a ```json fence whose
claims cited operation ids instead of evidence ids. The feedback named only
the outer JSON parse error: the fence was not found (prose before it), and
citation checks never run on a reply ``parse_report`` rejected. Acceptance is
unchanged: such a reply is still ``REPORT_INVALID``; only the feedback grows.
"""

from __future__ import annotations

import json
import time

import pytest

from opspilot.investigation.reports import (
    DeliveredView,
    parse_report,
    report_retry_feedback,
)
from tests.m1_investigation_support import report_json

PARSE = "JSON parse error: Expecting value: line 1 column 1 (char 0)."
WHOLE = "After removing the Markdown json fence"
EMBEDDED = "In the Markdown json fence found inside the reply"

VIEWS = (
    DeliveredView(
        evidence_id="ev-1",
        target_ids=frozenset({"checkout-prod"}),
        status="ok",
        time_scope_refs=frozenset({"policy-window-1"}),
        citable_as_fact=True,
    ),
)


def _feedback(content: str, *, views=VIEWS) -> str:
    return report_retry_feedback(
        "REPORT_INVALID",
        report=None,
        views=views,
        content=content,
        authorized_targets=frozenset({"checkout-prod"}),
        time_policy_ids=("policy-window-1",),
    )


def _prose_fenced(body: str) -> str:
    return f"I have sufficient evidence to report.\n\n```json\n{body}\n```\n"


def _whole_fenced(body: str) -> str:
    return f"```json\n{body}\n```"


def test_prose_wrapped_fence_with_bad_citation_names_the_evidence_id():
    content = _prose_fenced(report_json(evidence_id="op1-t0"))
    assert parse_report(content, finish_reason="stop") == (None, "REPORT_INVALID")
    feedback = _feedback(content)
    assert PARSE in feedback
    assert f"{EMBEDDED}, citation check:" in feedback
    assert "claim 0: unknown_evidence_id (evidence_id: op1-t0)." in feedback
    assert feedback.index(PARSE) < feedback.index("unknown_evidence_id")


def test_whole_fence_with_bad_citation_names_the_evidence_id():
    content = _whole_fenced(report_json(evidence_id="op1-t0"))
    feedback = _feedback(content)
    assert PARSE in feedback
    assert f"{WHOLE}, citation check:" in feedback
    assert "claim 0: unknown_evidence_id (evidence_id: op1-t0)." in feedback


@pytest.mark.parametrize("wrap", [_prose_fenced, _whole_fenced])
def test_fence_with_all_citations_valid_is_byte_identical_to_bare_parse_error(wrap):
    # F3 step-5 equivalence hinges on this: no new bytes when nothing fails.
    feedback = _feedback(wrap(report_json(evidence_id="ev-1")))
    assert feedback == _feedback("not json")


def test_prose_wrapped_fence_with_broken_json_names_inner_parse_error():
    feedback = _feedback(_prose_fenced('{"schema_version": '))
    assert PARSE in feedback
    assert f"{EMBEDDED}, JSON parse error:" in feedback


def test_prose_wrapped_fence_with_schema_error_names_schema_layer():
    payload = json.loads(report_json(evidence_id="ev-1"))
    payload["assessment_status"] = "incomplete"
    payload["conclusion"] = "partial"
    feedback = _feedback(_prose_fenced(json.dumps(payload)))
    assert PARSE in feedback
    assert "INCOMPLETE_REPORT_CONTRACT" in feedback
    assert "citation check" not in feedback


@pytest.mark.parametrize(
    "content",
    [
        'Here is the report:\n```json\n{"a": 1',
        "not json at all",
        "prose ``` only one fence",
    ],
)
def test_unclosed_or_absent_fence_keeps_only_the_outer_error(content):
    feedback = _feedback(content)
    assert "JSON parse error" in feedback
    assert "Markdown json fence" not in feedback


def test_unfenced_report_with_bad_citation_is_unchanged():
    # Parses at the top level: the report branch, not the parse-position one.
    content = report_json(evidence_id="op1-t0")
    feedback = _feedback(content)
    assert "JSON parse error" not in feedback
    assert "Markdown json fence" not in feedback


def test_citation_items_are_capped_at_fifty():
    payload = json.loads(report_json(evidence_id="ev-1"))
    claim = payload["claims"][0]
    payload["claims"] = [{**claim, "evidence_ids": [f"op{i}-t0"]} for i in range(60)]
    feedback = _feedback(_prose_fenced(json.dumps(payload)))
    assert "claim 49: unknown_evidence_id" in feedback
    assert "claim 50:" not in feedback
    assert "Truncated: 10 more claim(s) failed citation checks" in feedback


def test_first_fence_is_used_when_there_are_several():
    content = (
        "Draft:\n```json\n"
        + report_json(evidence_id="op1-t0")
        + "\n```\nand again:\n```json\n"
        + report_json(evidence_id="op2-t0")
        + "\n```"
    )
    feedback = _feedback(content)
    assert "op1-t0" in feedback
    assert "op2-t0" not in feedback


@pytest.mark.parametrize(
    "content",
    [
        "prose ```json" + " " * 1_000_000,
        "prose " + "`" * 900_000,
        "prose ```" + " \n" * 100_000 + "```",
    ],
)
def test_degenerate_embedded_fences_stay_linear(content):
    started = time.perf_counter()
    _feedback(content)
    assert time.perf_counter() - started < 0.5
