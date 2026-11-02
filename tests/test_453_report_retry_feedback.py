"""#453: the one forced-final retry must name every layer that rejected the
report, not only the outermost one.

Run 89459277 sent a ```json-fenced report whose body also broke the
``incomplete`` rule (``reports.py`` ``INCOMPLETE_REPORT_CONTRACT``). The
feedback named only the JSON parse error, so the single retry fixed the fence
and failed the rule it had never been told about. Acceptance is unchanged:
a fenced reply is still ``REPORT_INVALID``; only the feedback text grows.
"""

from __future__ import annotations

import json

from opspilot.investigation.reports import parse_report, report_retry_feedback
from tests.m1_investigation_support import report_json

PARSE = "JSON parse error: Expecting value: line 1 column 1 (char 0)."


def _incomplete_partial() -> str:
    payload = json.loads(report_json(evidence_id="ev-1"))
    payload["assessment_status"] = "incomplete"
    payload["conclusion"] = "partial"
    return json.dumps(payload)


def _fenced(body: str) -> str:
    return f"```json\n{body}\n```"


def _feedback(content: str) -> str:
    return report_retry_feedback(
        "REPORT_INVALID", report=None, views=(), content=content
    )


def test_fenced_report_breaking_the_incomplete_rule_names_both_layers():
    content = _fenced(_incomplete_partial())
    assert parse_report(content, finish_reason="stop") == (None, "REPORT_INVALID")
    feedback = _feedback(content)
    assert PARSE in feedback
    assert "INCOMPLETE_REPORT_CONTRACT" in feedback
    assert feedback.index(PARSE) < feedback.index("INCOMPLETE_REPORT_CONTRACT")


def test_unfenced_report_breaking_the_rule_is_unchanged():
    content = _incomplete_partial()
    assert parse_report(content, finish_reason="stop") == (None, "REPORT_INVALID")
    feedback = _feedback(content)
    assert "INCOMPLETE_REPORT_CONTRACT" in feedback
    assert "JSON parse error" not in feedback


def test_fenced_valid_report_is_still_rejected_and_names_only_the_fence():
    content = _fenced(report_json(evidence_id="ev-1"))
    assert parse_report(content, finish_reason="stop") == (None, "REPORT_INVALID")
    feedback = _feedback(content)
    assert PARSE in feedback
    assert "schema_error" not in feedback


def test_fenced_broken_json_reports_the_inner_parse_error():
    feedback = _feedback(_fenced('{"schema_version": '))
    assert PARSE in feedback
    assert "After removing the Markdown json fence, JSON parse error:" in feedback


def test_unfenced_garbage_keeps_the_single_parse_error():
    feedback = _feedback("not json at all")
    assert "JSON parse error" in feedback
    assert "Markdown json fence" not in feedback
    assert "schema_error" not in feedback
