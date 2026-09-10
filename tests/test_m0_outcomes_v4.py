"""Strict-version contracts; historical v3 payloads are not silently upgraded."""

import pytest
from pydantic import ValidationError

from scripts.m0.outcomes_v4 import ModelReportV2


def test_factlike_claims_require_explicit_model_scope():
    for kind in ("fact", "counter_evidence", "rejected_hypothesis"):
        with pytest.raises(ValidationError):
            ModelReportV2.model_validate(
                {
                    "schema_version": "m0-report-v2",
                    "assessment_status": "completed",
                    "conclusion": "partial",
                    "summary": "Preserved report",
                    "claims": [
                        {
                            "kind": kind,
                            "text": "An observation",
                            "evidence_ids": ["e1"],
                            "target_refs": [],
                            "time_scope_ref": None,
                        }
                    ],
                    "gaps": [],
                    "next_steps": [],
                }
            )


def strict_packet():
    import json

    from test_m0_outcomes_v3 import packet

    from scripts.m0 import outcomes_v4 as v4

    s, o = packet()
    s["trusted"]["controls"][0]["action"] = "new_run"
    target = s["trusted"]["artifacts"][0]["targets"][0]
    ref = v4.target_ref("run", target)
    timing = {
        "operation_started_at": "2026-09-10T00:02:00Z",
        "collection_completed_at": "2026-09-10T00:02:01Z",
        "source_start_at": "2026-09-10T00:00:10Z",
        "source_end_at": "2026-09-10T00:00:20Z",
        "source_time_basis": "event_time",
    }
    policy = {
        "id": "window",
        "revision": "p1",
        "integration_id": "lab",
        "interfaces": ["traces"],
        "mode": "historical_window",
        "reference_rule": "response_received_at",
        "window": s["trusted"]["scope"]["window"],
        "max_source_age_seconds": None,
        "target_refs": [],
        "all_authorized_targets": True,
        "scope_revision": "v1",
    }
    view = s["trusted"]["deliveries"][0]["views"][0]
    context = {
        "type": v4.CONTEXT_TYPE,
        "run_id": "run",
        "target_catalog": {ref: target},
        "view_bindings": {
            "view": {
                "view_hash": view["view_hash"],
                "target_refs": [ref],
                "time_scope_refs": ["window"],
                "timing": timing,
                "status": "ok",
            }
        },
        "time_policies": [policy],
    }
    # Canonicalize through DTO once so the exact JSON payload equals its context.
    context = v4.EvidenceContext.model_validate_json(json.dumps(context)).model_dump(
        mode="json"
    )
    d = s["trusted"]["deliveries"][0]
    payload = json.dumps({"evidence_views": [view], "context": context})
    d.update(
        context=context,
        dispatch_started_at="2026-09-10T00:03:00Z",
        response_received_at="2026-09-10T00:03:10Z",
        business_projection_content=payload,
        business_projection_hash=v4.content_hash(payload),
        full_wire_hash=v4.content_hash(payload),
    )
    report = {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": "Complete preserved summary",
        "claims": [
            {
                "kind": "fact",
                "text": "Five errors observed",
                "evidence_ids": ["view"],
                "target_refs": [ref],
                "time_scope_ref": "window",
            }
        ],
        "gaps": [],
        "next_steps": ["Ask a human to inspect the service"],
    }
    content = json.dumps(report)
    capture = {
        "run_id": "run",
        "step_id": "step",
        "request_id": "request",
        "control_generation": 1,
        "content": content,
        "content_sha256": v4.content_hash(content),
        "response_received_at": "2026-09-10T00:03:10Z",
    }
    s["schema_version"] = "m0-public-v4"
    s["agent_input"]["evidence_context"] = None
    s["trusted"].update(
        time_policies=[policy],
        evaluation_at="2026-09-10T00:03:11Z",
        report_capture=capture,
        timing_records={"view": {"view_hash": view["view_hash"], "timing": timing}},
    )
    new_o = {
        "schema_version": "m0-public-v4",
        "scenario_id": o["scenario_id"],
        "versions": o["versions"],
        "subject": o["subject"],
        "run_id": "run",
        "control_generation": 1,
        "execution": "completed",
        "report_step_id": "step",
        "report_request_id": "request",
        "report": report,
        "report_content": content,
        "report_content_sha256": v4.content_hash(content),
        "evidence_ids": ["view"],
        "handoff": False,
        "handoff_reasons": [],
        "health": "unknown",
    }
    return s, new_o


def checked(s, o):
    import json

    from scripts.m0 import outcomes_v4 as v4

    return v4.check_outcome(
        v4.IncidentScenario.model_validate_json(json.dumps(s)),
        v4.IncidentOutcome.model_validate_json(json.dumps(o)),
    )


def resync_context(s):
    import json

    from scripts.m0 import outcomes_v4 as v4

    d = s["trusted"]["deliveries"][0]
    d["context"]["time_policies"] = s["trusted"]["time_policies"]
    d["context"] = v4.EvidenceContext.model_validate_json(
        json.dumps(d["context"])
    ).model_dump(mode="json")
    payload = json.dumps({"evidence_views": d["views"], "context": d["context"]})
    d.update(
        business_projection_content=payload,
        business_projection_hash=v4.content_hash(payload),
        full_wire_hash=v4.content_hash(payload),
    )


def resync_report(s, o):
    import json

    from scripts.m0 import outcomes_v4 as v4

    text = json.dumps(o["report"])
    o.update(report_content=text, report_content_sha256=v4.content_hash(text))
    s["trusted"]["report_capture"].update(
        content=text, content_sha256=v4.content_hash(text)
    )


def test_full_report_is_preserved_and_output_bound():
    from copy import deepcopy

    s, o = strict_packet()
    assert checked(s, o) == []
    for field, value in [
        ("summary", "Unsupported replacement"),
        ("next_steps", ["Execute a repair"]),
    ]:
        changed = deepcopy(o)
        changed["report"][field] = value
        assert "REPORT_CONTENT_MISMATCH" in checked(s, changed)
    s["trusted"]["report_capture"]["content"] = "another report"
    assert "REPORT_OUTPUT_BINDING_MISMATCH" in checked(s, o)


@pytest.mark.parametrize("kind", ["fact", "counter_evidence", "rejected_hypothesis"])
def test_all_factlike_kinds_enforce_targets_status_and_freshness(kind):
    from copy import deepcopy

    s, o = strict_packet()
    o["report"].update(conclusion="partial")
    o["report"]["claims"][0]["kind"] = kind
    resync_report(s, o)
    assert checked(s, o) == []
    bad = deepcopy(o)
    bad["report"]["claims"][0]["target_refs"] = ["not-seen"]
    assert "CLAIM_TARGET_NOT_OBSERVED" in checked(s, bad)
    bad_s = deepcopy(s)
    bad_s["trusted"]["artifacts"][0]["status"] = "failed"
    bad_s["trusted"]["deliveries"][0]["context"]["view_bindings"]["view"]["status"] = (
        "failed"
    )
    resync_context(bad_s)
    assert "FAILED_EVIDENCE_AS_FACT" in checked(bad_s, o)
    s["trusted"]["time_policies"][0].update(
        mode="current", window=None, max_source_age_seconds=60
    )
    resync_context(s)
    assert "FACT_FRESHNESS_STALE" in checked(s, o)


def test_historical_window_is_not_stale_because_replayed_later():
    s, o = strict_packet()
    s["trusted"]["evaluation_at"] = "2035-01-01T00:00:00Z"
    assert checked(s, o) == []
    s["trusted"]["deliveries"][0]["context"]["view_bindings"]["view"]["timing"][
        "source_time_basis"
    ] = "unknown"
    s["trusted"]["timing_records"]["view"]["timing"]["source_time_basis"] = "unknown"
    resync_context(s)
    assert "FACT_FRESHNESS_UNKNOWN" in checked(s, o)


def test_current_requires_source_proof_and_explicit_threshold():
    s, o = strict_packet()
    s["trusted"]["time_policies"][0].update(
        mode="current", window=None, max_source_age_seconds=300
    )
    resync_context(s)
    assert checked(s, o) == []
    s["trusted"]["time_policies"][0]["max_source_age_seconds"] = None
    resync_context(s)
    assert "FACT_FRESHNESS_UNKNOWN" in checked(s, o)


@pytest.mark.parametrize(
    "action,state", [("cancel", "cancelled"), ("correct", "waiting_human")]
)
def test_last_control_owns_state_but_new_run_allows_completion(action, state):
    s, o = strict_packet()
    s["trusted"]["controls"][0]["action"] = action
    assert "CONTROL_STATE_MISMATCH" in checked(s, o)
    s["trusted"]["execution"] = state
    o.update(
        execution=state,
        report=None,
        report_content=None,
        report_content_sha256=None,
        evidence_ids=[],
        handoff=True,
        handoff_reasons=["Human control stops this run"],
    )
    s["trusted"].update(deliveries=[], report_capture=None)
    assert checked(s, o) == []
    s, o = strict_packet()
    assert checked(s, o) == []


def test_catalog_must_really_be_delivered_and_initial_input_checks_it():
    import json

    from scripts.m0 import outcomes_v4 as v4

    s, o = strict_packet()
    d = s["trusted"]["deliveries"][0]
    payload = json.dumps({"evidence_views": d["views"]})
    d.update(
        business_projection_content=payload,
        business_projection_hash=v4.content_hash(payload),
    )
    assert "CONTEXT_NOT_DELIVERED" in checked(s, o)
    s, o = strict_packet()
    s["agent_input"]["initial_views"] = s["trusted"]["deliveries"][0]["views"]
    with pytest.raises(ValueError, match="INITIAL_CONTEXT_INVALID"):
        v4.IncidentScenario.model_validate_json(json.dumps(s)).investigator_input()
    s["agent_input"]["evidence_context"] = s["trusted"]["deliveries"][0]["context"]
    assert (
        v4.IncidentScenario.model_validate_json(json.dumps(s))
        .investigator_input()
        .evidence_context
        is not None
    )


def test_future_capture_and_old_visible_event_cannot_be_fresh():
    s, o = strict_packet()
    s["trusted"]["artifacts"][0]["captured_at"] = "2099-01-01T00:00:00Z"
    assert "FUTURE_OBSERVATION" in checked(s, o)
    s, o = strict_packet()
    s["trusted"]["time_policies"][0].update(
        mode="current", window=None, max_source_age_seconds=100
    )
    s["trusted"]["scope"]["window"]["end"] = "2026-09-10T00:02:00Z"
    s["trusted"]["artifacts"][0]["window"]["end"] = "2026-09-10T00:02:00Z"
    for timing in [
        s["trusted"]["deliveries"][0]["context"]["view_bindings"]["view"]["timing"],
        s["trusted"]["timing_records"]["view"]["timing"],
    ]:
        timing["source_end_at"] = "2026-09-10T00:02:00Z"
    # The latest event is newer; oldest visible event still determines the bound.
    resync_context(s)
    assert "FACT_FRESHNESS_STALE" in checked(s, o)


def test_empty_or_hypothesis_report_still_requires_output_clocks():
    s, o = strict_packet()
    o["report"].update(conclusion="inconclusive", claims=[])
    resync_report(s, o)
    s["trusted"]["deliveries"][0].update(
        dispatch_started_at=None, response_received_at=None
    )
    s["trusted"]["report_capture"]["response_received_at"] = None
    s["trusted"]["evaluation_at"] = None
    assert "REPORT_TIME_UNKNOWN" in checked(s, o)


def test_hypothesis_optional_references_must_resolve():
    s, o = strict_packet()
    o["report"].update(conclusion="partial")
    o["report"]["claims"][0].update(
        kind="hypothesis", target_refs=["not-seen"], time_scope_ref="not-a-policy"
    )
    resync_report(s, o)
    errors = checked(s, o)
    assert "UNRESOLVED_TARGET_REF" in errors and "UNRESOLVED_TIME_SCOPE_REF" in errors


def test_current_freshness_does_not_expand_query_window():
    s, o = strict_packet()
    s["trusted"]["time_policies"][0].update(
        mode="current", window=None, max_source_age_seconds=300
    )
    for timing in [
        s["trusted"]["deliveries"][0]["context"]["view_bindings"]["view"]["timing"],
        s["trusted"]["timing_records"]["view"]["timing"],
    ]:
        timing.update(
            source_start_at="2026-09-10T00:01:30Z", source_end_at="2026-09-10T00:01:40Z"
        )
    resync_context(s)
    assert "FACT_FRESHNESS_OUTSIDE_QUERY_WINDOW" in checked(s, o)
