"""Strict runtime context/clock tests; no live requests or credential reads."""

from scripts.m0_environment.report_contract import source_timing


def test_evaluation_and_collection_time_do_not_become_prometheus_sample_time():
    record = {
        "observed_at": "2026-09-10T00:00:00Z",
        "operation_started_at": "2026-09-10T00:00:00Z",
        "collection_completed_at": "2026-09-10T00:00:01Z",
    }
    view = {
        "tool": "otel_metrics",
        "query": {"end": 1789000000},
        "data": {"result": [{"value": [1789000000, "1"]}]},
    }
    timing = source_timing(record, view)
    assert timing["source_time_basis"] == "unknown"
    assert timing["source_start_at"] is None and timing["source_end_at"] is None
    assert timing["collection_completed_at"] == record["collection_completed_at"]


def test_only_visible_events_determine_time_not_omitted_raw_rows():
    view = {
        "tool": "otel_logs",
        "data": {
            "displayed_logs": [{"timestamp": "2026-09-09T10:00:00Z"}],
            "backend_returned_hit_count": 20,
        },
    }
    timing = source_timing({}, view)
    assert timing["source_time_basis"] == "event_time"
    assert timing["source_start_at"] == "2026-09-09T10:00:00+00:00"
    assert timing["collection_completed_at"] is None
    view["data"]["displayed_logs"].append({"timestamp": "unknown"})
    assert source_timing({}, view)["source_time_basis"] == "unknown"


def test_legacy_observed_at_is_not_backfilled_as_collection_complete():
    timing = source_timing({"observed_at": "2026-09-09T10:00:00Z"}, {})
    assert timing["collection_completed_at"] is None
    assert timing["operation_started_at"] is None


def strict_context():
    return {
        "type": "opspilot-evidence-context-v4",
        "run_id": "r",
        "target_catalog": {},
        "view_bindings": {},
        "time_policies": [],
    }


def test_strict_wire_really_contains_context_without_losing_private_protocol():
    import json

    from scripts.m0_environment.report_contract import prepare_wire

    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "synthetic-private",
                "tool_calls": [
                    {
                        "id": "t",
                        "type": "function",
                        "function": {"name": "otel_logs", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "t", "content": "synthetic observation"},
        ],
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": 32768,
    }
    raw = json.dumps(payload).encode()
    for final_phase in (False, True):
        wire = json.loads(prepare_wire(raw, [], final_phase, context=strict_context()))
        assert wire["messages"][:2] == payload["messages"]
        assert json.loads(wire["messages"][2]["content"]) == strict_context()
        assert wire["thinking"] == payload["thinking"] and wire["max_tokens"] == 32768


def test_context_bytes_cannot_bypass_existing_http_envelope_limit():
    import json

    import pytest

    from scripts.m0_environment.report_contract import prepare_wire
    from scripts.m0_environment.round02 import PROFILE, envelope_check

    context = strict_context()
    context["target_catalog"] = {"synthetic": "x" * PROFILE.request_bytes}
    payload = {
        "model": PROFILE.model,
        "messages": [],
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": PROFILE.output_tokens,
    }
    raw = prepare_wire(json.dumps(payload).encode(), [], True, context=context)
    with pytest.raises(ValueError, match="bytes"):
        envelope_check(raw)


def test_new_default_rejects_legacy_report_instead_of_backfilling_scope():
    import json

    import pytest

    from scripts.m0_environment.report_contract import validate_report

    report = {
        "schema_version": "m0-report-v1",
        "assessment_status": "completed",
        "conclusion": "inconclusive",
        "summary": "legacy",
        "claims": [],
        "gaps": ["unknown"],
        "next_steps": [],
    }
    with pytest.raises(ValueError):
        validate_report(json.dumps(report), "stop", set(), context=strict_context())
    assert (
        validate_report(json.dumps(report), "stop", set(), version="m0-report-v1")
        == report
    )


def test_v2_without_evidence_policy_can_only_return_no_fact_scope():
    import json

    import pytest

    from scripts.m0_environment.report_contract import validate_report

    report = {
        "schema_version": "m0-report-v2",
        "assessment_status": "incomplete",
        "conclusion": "inconclusive",
        "summary": "No qualified evidence",
        "claims": [],
        "gaps": ["No trusted time policy"],
        "next_steps": [],
    }
    assert (
        validate_report(json.dumps(report), "stop", set(), context=strict_context())[
            "summary"
        ]
        == report["summary"]
    )
    report["claims"] = [
        {"kind": "fact", "text": "Cannot invent a target", "evidence_ids": ["e"]}
    ]
    with pytest.raises(ValueError):
        validate_report(json.dumps(report), "stop", {"e"}, context=strict_context())


def test_actual_legacy_log_events_are_extracted_without_inventing_capture_time():
    import json
    from pathlib import Path

    from scripts.m0_environment.holmes_baseline import log_projection

    raw = json.loads(
        Path("tests/fixtures/m0_environment/normal02_log_raw.json").read_text()
    )
    timing = source_timing(raw, log_projection(raw))
    assert timing["source_time_basis"] == "event_time"
    assert timing["source_start_at"] and timing["source_end_at"]
    assert timing["collection_completed_at"] is None


def test_trace_event_interval_uses_visible_start_and_duration():
    from datetime import datetime, timezone

    view = {
        "tool": "otel_traces",
        "data": {"sampled_spans": [{"start_us": 1788976626000000, "duration_us": 200}]},
    }
    timing = source_timing({}, view)
    assert timing["source_time_basis"] == "event_time"
    assert (
        timing["source_start_at"]
        == datetime.fromtimestamp(1788976626, timezone.utc).isoformat()
    )
    assert (
        datetime.fromisoformat(timing["source_end_at"])
        - datetime.fromisoformat(timing["source_start_at"])
    ).total_seconds() == 0.0002
