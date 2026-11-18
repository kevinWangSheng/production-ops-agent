"""#666 PR-A contract: report to judge text (H-12), tool-call attachment
(H-13, upstream ``property_manager.py:212-231``) and deshift (H-9).

Independent contract tests written from the task record's PR-A public
interface and the pre-review, without reading ``scripts/holmes_evals/``.
Red until the implementation lands; collection stays green (lazy import).

Upstream attachment format, derived by hand from property_manager.py:212-231
at a0009195::

    "\\n\\n# Tool Calls\\n\\n"
    per call i (1-based):
        f"* Tool #{i}: {description}\\n"
        if result:  "Output:\\n" + each line of str(result) prefixed "  " + "\\n"
        "---\\n"
    nothing at all when the call list is empty

H-13 fixes ``description`` = ``<tool>(<canonical JSON of params, keys
sorted>)`` and the output = the stored view as canonical JSON, indent 2.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* "canonical JSON" for params does not fix separators: both compact
  ``(",", ":")`` and Python default ``(", ", ": ")`` are accepted, nothing
  else; the output block is ``json.dumps(view, indent=2, sort_keys=True)``.
  Test data is ASCII so ``ensure_ascii`` does not matter.
* the refused call's output text is not fixed beyond "code and reason": only
  framing and the presence of both strings are asserted.
* ``Shift`` takes timezone-aware UTC datetimes.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from opspilot.evaluation.binary_judge import project_opspilot_report
from opspilot.investigation.reports import ReportV2
from tests.holmes_evals_support import mod

UTC = timezone.utc
HEADER = "\n\n# Tool Calls\n\n"
SEPARATORS = ((",", ":"), (", ", ": "))


def _report(
    summary: str = "Service checkout logged 12 errors in app-183c-logs.",
) -> ReportV2:
    return ReportV2.model_validate(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": summary,
            "claims": [
                {
                    "kind": "fact",
                    "text": "checkout returned HTTP 500 for 12 requests",
                    "evidence_ids": ["ev-opaque-1"],
                    "target_refs": ["target-1"],
                    "time_scope_ref": "window-1",
                },
                {
                    "kind": "hypothesis",
                    "text": "the payment dependency timed out",
                    "evidence_ids": ["ev-opaque-2", "ev-opaque-1"],
                },
            ],
            "gaps": ["no traces for the window"],
            "next_steps": ["check payment pod restarts"],
        }
    )


def _call(tool, params, output=None, refused_code=None, refused_reason=None):
    return mod("text").ToolCall(
        tool=tool,
        params=params,
        output=output,
        refused_code=refused_code,
        refused_reason=refused_reason,
    )


# Insertion order deliberately unsorted, nested.
PARAMS_1 = {
    "size": 10,
    "index": "app-183c-*",
    "query": {"bool": {"must": [{"match": {"message": "error"}}], "filter": []}},
}
OUTPUT_1 = {
    "total": 2,
    "hits": [
        {
            "service": "checkout",
            "message": "HTTP 500",
            "@timestamp": "2026-11-18T10:00:00Z",
        },
        {
            "service": "checkout",
            "message": "HTTP 500",
            "@timestamp": "2026-11-18T10:01:00Z",
        },
    ],
    "aggregations": {"by_service": {"buckets": [{"key": "checkout", "doc_count": 2}]}},
}
PARAMS_2 = {"pattern": "app-*"}
OUTPUT_2 = {
    "content": [{"index": "app-183c-logs", "docs.count": "120", "health": "green"}]
}
PARAMS_3 = {"z": {"b": 1, "a": [3, {"y": None, "x": True}]}, "a": "first"}


def _canon(params, seps) -> str:
    return json.dumps(params, sort_keys=True, separators=seps)


def _indented(output) -> str:
    rendered = json.dumps(output, indent=2, sort_keys=True)
    return "\n".join(f"  {line}" for line in rendered.split("\n"))


def _expected_block(i, tool, params, output, seps) -> str:
    block = f"* Tool #{i}: {tool}({_canon(params, seps)})\n"
    if output is not None:
        block += f"Output:\n{_indented(output)}\n"
    return block + "---\n"


# --------------------------------------------------------------------------
# 4. Tool-call attachment


def test_attachment_byte_exact_for_successful_calls():
    text = mod("text")
    calls = [
        _call("elasticsearch_search", PARAMS_1, OUTPUT_1),
        _call("elasticsearch_list_indices", PARAMS_2, OUTPUT_2),
        _call("elasticsearch_mappings", PARAMS_3, {"mappings": {"b": 1, "a": 2}}),
    ]
    got = text.format_tool_calls(calls)
    candidates = {
        HEADER
        + _expected_block(1, "elasticsearch_search", PARAMS_1, OUTPUT_1, seps)
        + _expected_block(2, "elasticsearch_list_indices", PARAMS_2, OUTPUT_2, seps)
        + _expected_block(
            3, "elasticsearch_mappings", PARAMS_3, {"mappings": {"b": 1, "a": 2}}, seps
        )
        for seps in SEPARATORS
    }
    assert got in candidates, got


def test_attachment_keys_sorted_at_every_depth():
    text = mod("text")
    got = text.format_tool_calls(
        [_call("t", PARAMS_3, {"z": 1, "a": {"d": 1, "c": 2}})]
    )
    line = got[len(HEADER) :].split("\n", 1)[0]
    m = re.fullmatch(r"\* Tool #1: t\((.*)\)", line)
    assert m, line
    assert json.loads(m.group(1)) == PARAMS_3
    assert m.group(1) in {_canon(PARAMS_3, s) for s in SEPARATORS}
    assert f"Output:\n{_indented({'z': 1, 'a': {'d': 1, 'c': 2}})}\n---\n" in got


def test_refused_call_is_numbered_and_carries_code_and_reason():
    text = mod("text")
    reason = "mapping has no @timestamp; pass time_field"
    calls = [
        _call("elasticsearch_list_indices", PARAMS_2, OUTPUT_2),
        _call(
            "elasticsearch_search",
            PARAMS_1,
            None,
            refused_code="TIME_FIELD_REQUIRED",
            refused_reason=reason,
        ),
        _call("elasticsearch_mappings", PARAMS_3, OUTPUT_1),
    ]
    got = text.format_tool_calls(calls)
    assert got.startswith(HEADER)
    body = got[len(HEADER) :]
    assert body.endswith("---\n")
    blocks = body[: -len("---\n")].split("---\n")
    assert len(blocks) == 3

    seps_ok = [
        s
        for s in SEPARATORS
        if blocks[0] + "---\n"
        == _expected_block(1, "elasticsearch_list_indices", PARAMS_2, OUTPUT_2, s)
    ]
    assert seps_ok, blocks[0]
    seps = seps_ok[0]
    assert blocks[2] + "---\n" == _expected_block(
        3, "elasticsearch_mappings", PARAMS_3, OUTPUT_1, seps
    )

    lines = blocks[1].rstrip("\n").split("\n")
    assert lines[0] == f"* Tool #2: elasticsearch_search({_canon(PARAMS_1, seps)})"
    assert lines[1] == "Output:"
    assert len(lines) >= 3
    assert all(line.startswith("  ") for line in lines[2:]), lines
    refused_text = "\n".join(lines[2:])
    assert "TIME_FIELD_REQUIRED" in refused_text
    assert reason in refused_text


def test_long_output_not_truncated():
    text = mod("text")
    huge = "x" * 300_000 + "-TAIL-MARKER"
    output = {"message": huge}
    got = text.format_tool_calls([_call("elasticsearch_search", PARAMS_2, output)])
    assert huge in got
    assert f"Output:\n{_indented(output)}\n---\n" in got


def test_call_without_output_has_no_output_section():
    # Upstream: "Output:" only when the call has a result.
    text = mod("text")
    got = text.format_tool_calls([_call("elasticsearch_list_indices", PARAMS_2, None)])
    assert got in {
        HEADER + _expected_block(1, "elasticsearch_list_indices", PARAMS_2, None, s)
        for s in SEPARATORS
    }


# --------------------------------------------------------------------------
# 5. report_to_text (H-12)


def test_no_report_is_not_converted():
    text = mod("text")
    calls = [_call("elasticsearch_search", PARAMS_1, OUTPUT_1)]
    for given_calls in (None, calls):
        converted = text.report_to_text(None, given_calls, None)
        assert converted.text is None
        assert converted.reason == "REPORT_REJECTED_BY_PRODUCT"


def test_report_without_tool_calls_is_the_projection():
    text = mod("text")
    report = _report()
    converted = text.report_to_text(report, None, None)
    assert converted.text == project_opspilot_report(report)
    assert converted.with_tool_calls is False
    assert converted.deshift_count == 0
    assert converted.reason is None
    assert "# Tool Calls" not in converted.text


def test_report_with_tool_calls_appends_attachment():
    text = mod("text")
    report = _report()
    calls = [
        _call("elasticsearch_search", PARAMS_1, OUTPUT_1),
        _call(
            "elasticsearch_search",
            PARAMS_2,
            None,
            "QUERY_CONSTRUCT_NOT_ALLOWED",
            "script",
        ),
    ]
    converted = text.report_to_text(report, calls, None)
    projection = project_opspilot_report(report)
    assert converted.text.startswith(projection)
    assert converted.text == projection + text.format_tool_calls(calls)
    assert converted.with_tool_calls is True
    assert converted.reason is None


def test_empty_call_list_adds_no_attachment():
    # ASSUMPTION: upstream appends nothing when the call list is empty
    # (``and result.tool_calls``, property_manager.py:216).
    text = mod("text")
    report = _report()
    converted = text.report_to_text(report, [], None)
    assert converted.text == project_opspilot_report(report)
    assert "# Tool Calls" not in converted.text


# --------------------------------------------------------------------------
# 6. deshift (H-9)

SECONDS = 1038 * 86400 + 3723  # not a whole number of days
SHIFTED_MIN = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
SHIFTED_MAX = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)


def _shift(seconds=SECONDS, lo=SHIFTED_MIN, hi=SHIFTED_MAX):
    return mod("text").Shift(seconds=seconds, shifted_min=lo, shifted_max=hi)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_ms(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def test_deshift_iso_datetimes_in_range_only():
    text = mod("text")
    back = timedelta(seconds=SECONDS)
    inside_a = datetime(2026, 11, 18, 9, 30, tzinfo=UTC)
    inside_b = datetime(2026, 11, 18, 11, 45, 10, tzinfo=UTC)  # max + 45 min
    inside_c = datetime(2026, 11, 18, 8, 15, 0, 250_000, tzinfo=UTC)  # min - 45 min
    outside_old = "2024-01-15T10:00:00Z"  # text that was never shifted (245 body)
    outside_late = _iso(datetime(2026, 11, 18, 14, 30, tzinfo=UTC))  # max + 3.5 h
    outside_early = _iso(datetime(2026, 11, 17, 22, 0, tzinfo=UTC))  # min - 11 h

    template = (
        "Errors began at {a} and peaked at {b}; first seen {c}. "
        "Message body says {o1}. Later {o2}, earlier {o3}. "
        "Stopped at 14:00, resumed 09:30:00."
    )
    given = template.format(
        a=_iso(inside_a),
        b=_iso(inside_b),
        c=_iso_ms(inside_c),
        o1=outside_old,
        o2=outside_late,
        o3=outside_early,
    )
    want = template.format(
        a=_iso(inside_a - back),
        b=_iso(inside_b - back),
        c=_iso_ms(inside_c - back),
        o1=outside_old,
        o2=outside_late,
        o3=outside_early,
    )
    out, count = text.deshift(given, _shift())
    assert out == want
    assert count == 3


def test_deshift_dates_and_no_double_rewrite():
    text = mod("text")
    days = 1038
    # ASSUMPTION: a whole-day shift keeps the date arithmetic unambiguous;
    # the window spans the whole of 2026-11-18 so any in-range rule holds.
    shift = _shift(
        seconds=days * 86400,
        lo=datetime(2026, 11, 17, 22, 30, tzinfo=UTC),
        hi=datetime(2026, 11, 19, 1, 30, tzinfo=UTC),
    )
    in_dt = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    template = "Rollover on {d} at {dt}. Archive dated {old}. At 14:00 it stopped."
    given = template.format(d="2026-11-18", dt=_iso(in_dt), old="2025-01-01")
    want = template.format(
        d=(date(2026, 11, 18) - timedelta(days=days)).isoformat(),
        dt=_iso(in_dt - timedelta(days=days)),
        old="2025-01-01",
    )
    out, count = text.deshift(given, shift)
    assert out == want
    assert count == 2


def test_deshift_time_only_untouched():
    text = mod("text")
    given = "The job stopped at 14:00 and ran again at 10:05:30 UTC."
    out, count = text.deshift(given, _shift())
    assert (out, count) == (given, 0)


def test_deshift_without_shift_is_identity():
    text = mod("text")
    given = f"at {_iso(datetime(2026, 11, 18, 10, 0, tzinfo=UTC))} on 2026-11-18"
    assert text.deshift(given, None) == (given, 0)


def test_report_to_text_deshifts_report_and_attachment():
    text = mod("text")
    in_range = _iso(datetime(2026, 11, 18, 10, 0, tzinfo=UTC))
    report = _report(f"First error at {in_range} in app-275-logs.")
    calls = [
        _call("elasticsearch_search", PARAMS_2, {"hits": [{"@timestamp": in_range}]})
    ]
    shift = _shift()
    converted = text.report_to_text(report, calls, shift)

    raw = project_opspilot_report(report) + text.format_tool_calls(calls)
    want, count = text.deshift(raw, shift)
    assert count == 2
    assert converted.text == want
    assert converted.deshift_count == count
    assert in_range not in converted.text
    assert converted.with_tool_calls is True


@pytest.mark.parametrize("shift", [None, "zero"])
def test_report_to_text_without_shift_counts_zero(shift):
    text = mod("text")
    report = _report("At 2026-11-18T10:00:00Z the error rate rose.")
    s = None if shift is None else _shift(seconds=0)
    converted = text.report_to_text(report, None, s)
    assert converted.text == project_opspilot_report(report)
    if shift is None:
        assert converted.deshift_count == 0
