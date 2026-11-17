"""#686 Q2: a replies-keyed object without schema_version is the wrapper."""

import json

from opspilot.investigation.reports import split_response

REPLY = {"to_input_seq": 3, "text": "checked", "evidence_ids": []}


def test_replies_keyed_object_without_schema_version_is_a_wrapper() -> None:
    report = {"schema_version": "m0-report-v3", "summary": "s"}
    split = split_response(
        json.dumps({"note": "n", "replies": [REPLY], "report": report})
    )
    assert split.wrapped is True
    assert split.replies == [REPLY]
    assert json.loads(split.report_content or "") == report


def test_replies_keyed_object_with_null_or_missing_report_has_no_report() -> None:
    for payload in ({"replies": [REPLY], "report": None}, {"replies": [REPLY]}):
        split = split_response(json.dumps(payload))
        assert split.wrapped is True
        assert split.report_content is None


def test_other_schema_version_with_replies_is_not_a_wrapper() -> None:
    content = json.dumps({"schema_version": "m0-report-v3", "replies": [REPLY]})
    split = split_response(content)
    assert split.wrapped is False
    assert split.replies is None
    assert split.report_content == content


def test_bare_report_is_unchanged() -> None:
    content = json.dumps({"schema_version": "m0-report-v3", "summary": "s"})
    split = split_response(content)
    assert split.wrapped is False
    assert split.report_content == content
