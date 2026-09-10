"""Strict bridge integration over isolated files, not new provider experiments."""

import hashlib
import json
from datetime import datetime, timezone

import pytest
from test_m0_holmes_bridge import captured as _captured_fixture
from test_m0_holmes_bridge import save

from scripts.m0 import holmes_bridge as bridge
from scripts.m0 import outcomes_v4 as v4

captured = _captured_fixture


@pytest.fixture
def strict_captured(captured):
    run, code = captured
    registry = json.loads((run / "deployment-registry.json").read_text())
    config = json.loads((run / "configuration.json").read_text())
    scope = config["trusted_access_scope"]
    scope["control_generation"] = 0
    policy = v4.TimePolicy.model_validate_json(
        json.dumps(
            {
                "id": "historical",
                "revision": "p1",
                "integration_id": "lab",
                "interfaces": ["otel_traces"],
                "mode": "historical_window",
                "reference_rule": "response_received_at",
                "window": {
                    "start": datetime.fromtimestamp(
                        scope["window"]["start"], timezone.utc
                    ).isoformat(),
                    "end": datetime.fromtimestamp(
                        scope["window"]["end"], timezone.utc
                    ).isoformat(),
                },
                "all_authorized_targets": True,
                "scope_revision": scope["policy_revision"],
            }
        )
    )
    config.update(
        report_schema_version="m0-report-v2",
        time_policies=[policy.model_dump(mode="json")],
        time_policies_sha256=bridge.canonical_hash([policy.model_dump(mode="json")]),
    )
    save(run / "configuration.json", config)
    save(run / "time-policies.json", config["time_policies"])
    raw = json.loads((run / "case-01-e1-raw.json").read_text())
    raw.update(
        tool="otel_traces",
        trusted_access_scope=scope,
        operation_started_at=raw["observed_at"],
        collection_completed_at="2026-09-10T01:00:01Z",
        data={
            "sampled_spans": [
                {
                    "start_us": (scope["window"]["start"] + 10) * 1000000,
                    "duration_us": 1000000,
                }
            ]
        },
    )
    timing = v4.Timing.model_validate_json(
        json.dumps(
            {
                "operation_started_at": raw["operation_started_at"],
                "collection_completed_at": raw["collection_completed_at"],
                "source_start_at": datetime.fromtimestamp(
                    scope["window"]["start"] + 10, timezone.utc
                ).isoformat(),
                "source_end_at": datetime.fromtimestamp(
                    scope["window"]["start"] + 11, timezone.utc
                ).isoformat(),
                "source_time_basis": "event_time",
            }
        )
    )
    context = v4.build_context(
        "case-01",
        {"case-01-e1": raw},
        registry,
        [policy],
        {"case-01-e1": timing},
        allowed_scope=scope,
    )
    save(run / "case-01-e1-raw.json", raw)
    save(run / "case-01-e1-tool-model-view.json", raw)
    save(run / "observations.json", [raw])
    save(
        run / "case-01-e1-manifest.json",
        {
            "evidence_id": "case-01-e1",
            "scope": scope,
            "raw_sha256": bridge.canonical_hash(raw),
            "view_sha256": bridge.canonical_hash(raw),
            "raw_file_sha256": hashlib.sha256(
                (run / "case-01-e1-raw.json").read_bytes()
            ).hexdigest(),
            "view_file_sha256": hashlib.sha256(
                (run / "case-01-e1-tool-model-view.json").read_bytes()
            ).hexdigest(),
        },
    )
    save(
        run / "evidence-timings.json",
        {
            "case-01-e1": {
                "view_hash": bridge.canonical_hash(raw),
                "timing": timing.model_dump(mode="json"),
            }
        },
    )
    user = json.loads((run / "input-business.json").read_text())[0]
    messages = [
        user,
        {
            "role": "tool",
            "tool_call_id": "call1",
            "content": "tool_call_metadata="
            + json.dumps({"tool_name": "otel_traces", "tool_call_id": "call1"})
            + json.dumps(raw),
        },
        {"role": "user", "content": json.dumps(context.model_dump(mode="json"))},
    ]
    delivery = {
        "request_ordinal": 2,
        "request_id": "case-01-http-2",
        "control_generation": 0,
        "state": "response_received",
        "http_status": 200,
        "messages": messages,
        "business_messages_sha256": bridge.canonical_hash(messages),
        "trusted_access_scope": scope,
        "actual_request_sha256": "a" * 64,
        "evidence_context": context.model_dump(mode="json"),
        "evidence_context_sha256": bridge.canonical_hash(
            context.model_dump(mode="json")
        ),
        "dispatch_started_at": "2026-09-10T01:01:00Z",
        "response_received_at": "2026-09-10T01:01:10Z",
    }
    save(run / "delivered-business.json", [delivery])
    report = v4.ModelReportV2.model_validate(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "The complete synthetic summary",
            "claims": [
                {
                    "kind": "fact",
                    "text": "A synthetic event was returned",
                    "evidence_ids": ["case-01-e1"],
                    "target_refs": list(context.target_catalog),
                    "time_scope_ref": "historical",
                }
            ],
            "gaps": [],
            "next_steps": ["Human review of this event"],
        }
    )
    content = json.dumps(report.model_dump(mode="json"))
    save(
        run / "result-business.json",
        {
            "run_id": "case-01",
            "finish_reason": "stop",
            "final_business_content": content,
            "final_report": report.model_dump(mode="json"),
        },
    )
    save(
        run / "response-2-business.json",
        {
            "run_id": "case-01",
            "request_ordinal": 2,
            "response_complete": True,
            "identity_accepted": True,
            "choices": [{"finish_reason": "stop", "content": content}],
        },
    )
    save(
        run / "report-capture-business.json",
        {
            "run_id": "case-01",
            "step_id": "case-01:report-step:2",
            "request_id": "case-01-http-2",
            "control_generation": 0,
            "content": content,
            "content_sha256": v4.content_hash(content),
            "response_received_at": delivery["response_received_at"],
        },
    )
    return run, code


def test_strict_default_preserves_full_report_and_metadata(strict_captured):
    run, code = strict_captured
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert v4.check_outcome(scenario, outcome) == []
    assert outcome.report.summary == "The complete synthetic summary"
    assert outcome.report.next_steps == ["Human review of this event"]
    assert outcome.report_content == scenario.trusted.report_capture.content
    assert outcome.report_request_id == "case-01-http-2"


def test_legacy_requires_explicit_replay_without_fabricating_fields(captured):
    run, code = captured
    with pytest.raises(ValueError, match="LEGACY_REPORT_REQUIRES_EXPLICIT_REPLAY"):
        bridge.load_packet(run, projection_source_sha256=code)
    scenario, outcome = bridge.load_legacy_packet(run, projection_source_sha256=code)
    assert scenario.schema_version == "m0-public-v3"
    assert all(c.target is None for c in outcome.claims)
    audit = bridge.legacy_report_audit(run)
    assert audit["original_report"]["summary"] == "One returned sample"
    assert "next_steps" in audit["original_report"]
    assert audit["strict_scope"] == audit["strict_freshness"] == "unknown"
    assert audit["current_acceptance_pass"] is False


def test_strict_bridge_never_backfills_missing_timing(strict_captured):
    run, code = strict_captured
    save(run / "evidence-timings.json", {})
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert "TIMING_RECORD_UNKNOWN" in v4.check_outcome(scenario, outcome)


def test_output_content_cannot_diverge_between_capture_and_result(strict_captured):
    run, code = strict_captured
    capture = json.loads((run / "report-capture-business.json").read_text())
    capture["content"] = "tampered complete report"
    capture["content_sha256"] = v4.content_hash(capture["content"])
    save(run / "report-capture-business.json", capture)
    with pytest.raises(ValueError, match="REPORT_OUTPUT_BINDING_MISMATCH"):
        bridge.load_packet(run, projection_source_sha256=code)
