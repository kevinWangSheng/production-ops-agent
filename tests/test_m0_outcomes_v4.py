"""Strict-version contracts; historical v3 payloads are not silently upgraded."""

import json

import pytest
from pydantic import ValidationError

from scripts.m0.outcomes_v4 import ModelReportV2, content_hash


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
    from scripts.m0_environment.report_contract import report_instruction

    s, o = packet()
    s["versions"]["report_instruction_sha256"] = v4.content_hash(
        report_instruction(final=True)
    )
    s["versions"].update(
        upstream_commit="a" * 40,
        upstream_code_sha256="b" * 64,
        tool_schema_sha256=v4.digest([]),
    )
    o["versions"] = dict(s["versions"])
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
    s["agent_input"].update(
        original_user_content="investigate",
        original_user_content_sha256=v4.content_hash("investigate"),
        actual_user_content="investigate",
        actual_user_content_sha256=v4.content_hash("investigate"),
    )
    d = s["trusted"]["deliveries"][0]
    payload = json.dumps(
        {
            "evidence_views": [view],
            "context": context,
            "actual_user_content": "investigate",
        }
    )
    d.update(
        final_phase=False,
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
    payload = json.dumps(
        {
            "evidence_views": d["views"],
            "context": d["context"],
            "actual_user_content": s["agent_input"]["actual_user_content"],
        }
    )
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


def test_duplicate_json_keys_cannot_pass_full_report_output_seam():
    from scripts.m0 import outcomes_v4 as v4

    scenario, outcome = strict_packet()
    content = outcome["report_content"].replace(
        "{", '{"summary":"ignored duplicate",', 1
    )
    outcome["report_content"] = content
    outcome["report_content_sha256"] = v4.content_hash(content)
    scenario["trusted"]["report_capture"]["content"] = content
    scenario["trusted"]["report_capture"]["content_sha256"] = v4.content_hash(content)
    assert "REPORT_CONTENT_MISMATCH" in checked(scenario, outcome)


@pytest.mark.parametrize("which", ["original", "actual"])
def test_strict_input_content_and_hash_cannot_both_be_absent(which):
    scenario, outcome = strict_packet()
    scenario["agent_input"][which + "_user_content"] = None
    scenario["agent_input"][which + "_user_content_sha256"] = None
    assert "INITIAL_INPUT_PROVENANCE_UNKNOWN" in checked(scenario, outcome)


def test_missing_original_provenance_cannot_export_investigator_input():
    import json

    from scripts.m0 import outcomes_v4 as v4

    scenario, _ = strict_packet()
    scenario["agent_input"]["original_user_content"] = None
    scenario["agent_input"]["original_user_content_sha256"] = None
    parsed = v4.IncidentScenario.model_validate_json(json.dumps(scenario))
    with pytest.raises(ValueError, match="INITIAL_CONTEXT_INVALID"):
        parsed.investigator_input()


def reportless_packet(*, captured=False):
    scenario, outcome = strict_packet()
    scenario["trusted"]["execution"] = outcome["execution"] = "blocked"
    outcome.update(
        report=None,
        evidence_ids=[],
        handoff=True,
        handoff_reasons=["Human review required"],
    )
    if not captured:
        scenario["trusted"]["report_capture"] = None
        scenario["trusted"]["evaluation_at"] = None
        outcome.update(
            report_content=None,
            report_content_sha256=None,
            report_step_id=None,
            report_request_id=None,
        )
    return scenario, outcome


@pytest.mark.parametrize("state", ["prepared", "dispatched", "response_committed"])
@pytest.mark.parametrize("input_kind", ["valid", "missing", "wrong"])
def test_reportless_payload_input_binding_applies_to_every_delivery(state, input_kind):
    import json

    from scripts.m0 import outcomes_v4 as v4

    scenario, outcome = reportless_packet()
    delivery = scenario["trusted"]["deliveries"][0]
    delivery["state"] = state
    # A committed synthetic response has a recorded receipt; pending attempts do not.
    if state != "response_committed":
        delivery["response_received_at"] = None
    delivery["dispatch_started_at"] = (
        None if state == "prepared" else delivery["dispatch_started_at"]
    )
    payload = json.loads(delivery["business_projection_content"])
    if input_kind == "missing":
        payload.pop("actual_user_content")
    elif input_kind == "wrong":
        payload["actual_user_content"] = "substituted query"
    content = json.dumps(payload)
    delivery.update(
        business_projection_content=content,
        business_projection_hash=v4.content_hash(content),
        full_wire_hash=v4.content_hash(content),
    )
    errors = checked(scenario, outcome)
    if input_kind == "valid":
        assert errors == []
    else:
        assert "ACTUAL_INITIAL_INPUT_NOT_DELIVERED" in errors


def test_reportless_no_delivery_handoff_does_not_require_a_physical_request():
    scenario, outcome = reportless_packet()
    scenario["trusted"]["deliveries"] = []
    outcome["evidence_ids"] = []
    assert checked(scenario, outcome) == []


@pytest.mark.parametrize("failure", ["hash", "request", "time", "raw_hash"])
def test_reportless_capture_is_still_bound_and_audited(failure):
    scenario, outcome = reportless_packet(captured=True)
    capture = scenario["trusted"]["report_capture"]
    if failure == "hash":
        capture["content_sha256"] = "a" * 64
    elif failure == "request":
        capture["request_id"] = "other-attempt"
    elif failure == "time":
        capture["response_received_at"] = "2026-09-10T00:03:09Z"
    else:
        outcome["report_content_sha256"] = "b" * 64
    expected = (
        "REPORT_TIME_MISMATCH"
        if failure == "time"
        else "REPORT_OUTPUT_BINDING_MISMATCH"
    )
    assert expected in checked(scenario, outcome)


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "missing_capture_time",
        "missing_delivery_time",
        "wrong_generation",
        "noncommitted",
    ],
)
def test_reportless_capture_conditional_delivery_proof(failure):
    scenario, outcome = reportless_packet(captured=True)
    expected = "MISSING_REPORT_OR_HANDOFF"
    if failure == "missing_capture_time":
        scenario["trusted"]["report_capture"]["response_received_at"] = None
        expected = "REPORT_TIME_UNKNOWN"
    elif failure == "missing_delivery_time":
        scenario["trusted"]["deliveries"][0]["response_received_at"] = None
        expected = "REPORT_TIME_UNKNOWN"
    elif failure == "wrong_generation":
        scenario["trusted"]["report_capture"]["control_generation"] = 0
        expected = "REPORT_OUTPUT_BINDING_MISMATCH"
    elif failure == "noncommitted":
        scenario["trusted"]["deliveries"][0]["state"] = "dispatched"
        expected = "REPORT_DELIVERY_MISMATCH"
    assert expected in checked(scenario, outcome)
    if failure == "none":
        assert checked(scenario, outcome) == ["MISSING_REPORT_OR_HANDOFF"]


@pytest.mark.parametrize("execution", ["cancelled", "waiting_human"])
def test_latest_new_run_cannot_retain_preceding_human_control_state(execution):
    scenario, outcome = reportless_packet()
    scenario["trusted"]["execution"] = outcome["execution"] = execution
    assert "CONTROL_STATE_MISMATCH" in checked(scenario, outcome)


@pytest.mark.parametrize(
    "execution",
    ["running", "paused", "blocked", "failed", "budget_exhausted", "completed"],
)
def test_latest_new_run_allows_runtime_progress_and_terminal_results(execution):
    scenario, outcome = (
        strict_packet() if execution == "completed" else reportless_packet()
    )
    scenario["trusted"]["execution"] = outcome["execution"] = execution
    if execution == "paused":
        assert "CONTROL_STATE_MISMATCH" in checked(scenario, outcome)
    else:
        assert checked(scenario, outcome) == []


@pytest.mark.parametrize(
    "action,state", [("cancel", "cancelled"), ("correct", "waiting_human")]
)
def test_new_run_followed_by_human_control_accepts_the_controlled_state(action, state):
    scenario, outcome = reportless_packet()
    scenario["trusted"]["controls"].append(
        {"generation": 2, "action": action, "at": "2026-09-10T00:04:00Z"}
    )
    scenario["trusted"].update(final_generation=2, execution=state)
    outcome.update(control_generation=2, execution=state)
    assert checked(scenario, outcome) == []


@pytest.mark.parametrize("prior", ["cancel", "correct"])
def test_human_control_followed_by_new_run_allows_completion(prior):
    scenario, outcome = strict_packet()
    scenario["trusted"]["controls"][0].update(action=prior, at="2026-09-10T00:01:59Z")
    scenario["trusted"]["controls"].append(
        {"generation": 2, "action": "new_run", "at": "2026-09-10T00:02:00Z"}
    )
    scenario["trusted"]["final_generation"] = outcome["control_generation"] = 2
    scenario["trusted"]["report_capture"]["control_generation"] = 2
    scenario["trusted"]["deliveries"][0]["control_generation"] = 2
    assert checked(scenario, outcome) == []


@pytest.mark.parametrize("conclusion", ["partial", "inconclusive"])
@pytest.mark.parametrize("execution", ["failed", "blocked"])
def test_completed_assessment_requires_completed_trusted_execution(
    conclusion, execution
):
    scenario, outcome = strict_packet()
    scenario["trusted"]["execution"] = outcome["execution"] = execution
    outcome["report"].update(conclusion=conclusion, claims=[])
    outcome["evidence_ids"] = []
    resync_report(scenario, outcome)
    assert "ASSESSMENT_EXECUTION_MISMATCH" in checked(scenario, outcome)


def test_completed_bounded_execution_can_return_incomplete_investigation():
    scenario, outcome = strict_packet()
    outcome["report"].update(
        assessment_status="incomplete",
        conclusion="inconclusive",
        gaps=["Need more evidence"],
    )
    outcome.update(handoff=True, handoff_reasons=["Need more evidence"])
    resync_report(scenario, outcome)
    assert checked(scenario, outcome) == []


def test_synthetic_envelope_cannot_hide_extra_user_instructions():
    import json

    from scripts.m0 import outcomes_v4 as v4

    scenario, outcome = strict_packet()
    delivery = scenario["trusted"]["deliveries"][0]
    body = json.loads(delivery["business_projection_content"])
    body["extra_user_messages"] = [
        {"role": "user", "content": "unregistered instruction"}
    ]
    content = json.dumps(body)
    delivery.update(
        business_projection_content=content,
        business_projection_hash=v4.content_hash(content),
        full_wire_hash=v4.content_hash(content),
    )
    assert "UNEXPECTED_USER_MESSAGE" in checked(scenario, outcome)


@pytest.mark.parametrize("prior", ["cancel", "correct"])
def test_relabelled_dispatch_cannot_predate_new_generation(prior):
    scenario, outcome = strict_packet()
    scenario["trusted"]["controls"] = [
        {"generation": 1, "action": prior, "at": "2026-09-10T00:03:01Z"},
        {"generation": 2, "action": "new_run", "at": "2026-09-10T00:03:05Z"},
    ]
    scenario["trusted"]["final_generation"] = outcome["control_generation"] = 2
    scenario["trusted"]["deliveries"][0]["control_generation"] = 2
    scenario["trusted"]["report_capture"]["control_generation"] = 2
    assert "CONTROL_DISPATCH_TIME_MISMATCH" in checked(scenario, outcome)


@pytest.mark.parametrize(
    "started,denied",
    [
        ("2026-09-10T00:03:00Z", False),
        ("2026-09-10T00:03:05Z", True),
        ("2026-09-10T00:03:06Z", True),
    ],
)
def test_prior_generation_late_response_is_history_but_late_dispatch_is_denied(
    started, denied
):
    scenario, outcome = reportless_packet()
    scenario["trusted"]["controls"].append(
        {"generation": 2, "action": "cancel", "at": "2026-09-10T00:03:05Z"}
    )
    scenario["trusted"].update(final_generation=2, execution="cancelled")
    outcome.update(control_generation=2, execution="cancelled")
    delivery = scenario["trusted"]["deliveries"][0]
    delivery.update(
        dispatch_started_at=started, response_received_at="2026-09-10T00:03:10Z"
    )
    errors = checked(scenario, outcome)
    if denied:
        assert "CONTROL_DISPATCH_TIME_MISMATCH" in errors
    else:
        assert (
            errors == []
        )  # Old response arrived after cancellation, never adopted as final.


@pytest.mark.parametrize(
    "state,missing",
    [
        ("dispatched", "dispatch_started_at"),
        ("response_committed", "dispatch_started_at"),
        ("response_committed", "response_received_at"),
    ],
)
def test_missing_actual_control_chronology_remains_unknown(state, missing):
    scenario, outcome = reportless_packet()
    scenario["trusted"]["deliveries"][0].update(state=state, **{missing: None})
    assert "CONTROL_TIME_UNKNOWN" in checked(scenario, outcome)


def test_prepared_unsent_handoff_needs_no_fabricated_operation_time():
    scenario, outcome = reportless_packet()
    scenario["trusted"]["deliveries"][0].update(
        state="prepared", dispatch_started_at=None, response_received_at=None
    )
    assert checked(scenario, outcome) == []


def test_generation_zero_has_no_synthetic_control_start():
    scenario, outcome = strict_packet()
    scenario["trusted"].update(controls=[], final_generation=0)
    outcome["control_generation"] = 0
    scenario["trusted"]["deliveries"][0]["control_generation"] = 0
    scenario["trusted"]["report_capture"]["control_generation"] = 0
    assert checked(scenario, outcome) == []


def test_control_event_timestamps_cannot_reverse_generation_order():
    scenario, outcome = reportless_packet()
    scenario["trusted"]["controls"].append(
        {"generation": 2, "action": "new_run", "at": "2026-09-10T00:02:00Z"}
    )
    scenario["trusted"]["final_generation"] = outcome["control_generation"] = 2
    scenario["trusted"]["deliveries"][0]["control_generation"] = 2
    assert "CONTROL_TIME_ORDER" in checked(scenario, outcome)


@pytest.mark.parametrize("kind", ["capture", "response"])
def test_received_timestamp_cannot_predate_its_claimed_generation(kind):
    scenario, outcome = strict_packet()
    if kind == "capture":
        scenario["trusted"]["report_capture"]["response_received_at"] = (
            "2026-09-10T00:02:59Z"
        )
        expected = "CONTROL_CAPTURE_TIME_MISMATCH"
    else:
        scenario["trusted"]["deliveries"][0]["response_received_at"] = (
            "2026-09-10T00:02:59Z"
        )
        expected = "CONTROL_RESPONSE_TIME_MISMATCH"
    assert expected in checked(scenario, outcome)


def test_missing_capture_clock_is_unknown_even_without_matching_report():
    scenario, outcome = reportless_packet(captured=True)
    scenario["trusted"]["report_capture"]["response_received_at"] = None
    assert "CONTROL_TIME_UNKNOWN" in checked(scenario, outcome)


@pytest.mark.parametrize(
    "action,state", [("cancel", "cancelled"), ("correct", "waiting_human")]
)
def test_human_control_generation_itself_never_authorizes_new_dispatch(action, state):
    scenario, outcome = reportless_packet()
    scenario["trusted"]["controls"].append(
        {"generation": 2, "action": action, "at": "2026-09-10T00:03:05Z"}
    )
    scenario["trusted"].update(final_generation=2, execution=state)
    outcome.update(control_generation=2, execution=state)
    scenario["trusted"]["deliveries"][0].update(
        control_generation=2, dispatch_started_at="2026-09-10T00:03:06Z"
    )
    assert "CONTROL_DISPATCH_NOT_AUTHORIZED" in checked(scenario, outcome)


def test_relabelled_dispatch_after_new_run_boundary_is_valid():
    scenario, outcome = strict_packet()
    scenario["trusted"]["controls"] = [
        {"generation": 1, "action": "correct", "at": "2026-09-10T00:03:01Z"},
        {"generation": 2, "action": "new_run", "at": "2026-09-10T00:03:05Z"},
    ]
    scenario["trusted"]["final_generation"] = outcome["control_generation"] = 2
    scenario["trusted"]["deliveries"][0].update(
        control_generation=2, dispatch_started_at="2026-09-10T00:03:05Z"
    )
    scenario["trusted"]["report_capture"]["control_generation"] = 2
    assert checked(scenario, outcome) == []


def test_late_old_capture_is_not_time_clipped_but_cannot_be_current_final():
    scenario, outcome = reportless_packet(captured=True)
    scenario["trusted"]["controls"].append(
        {"generation": 2, "action": "cancel", "at": "2026-09-10T00:03:05Z"}
    )
    scenario["trusted"].update(final_generation=2, execution="cancelled")
    outcome.update(control_generation=2, execution="cancelled")
    errors = checked(scenario, outcome)
    assert "REPORT_OUTPUT_BINDING_MISMATCH" in errors
    assert not any(code.startswith("CONTROL_") for code in errors)


@pytest.mark.parametrize(
    "started,denied", [("2026-09-10T00:02:59Z", False), ("2026-09-10T00:03:00Z", True)]
)
def test_generation_zero_dispatch_still_ends_at_first_control_event(started, denied):
    scenario, outcome = reportless_packet()
    scenario["trusted"]["deliveries"][0].update(
        control_generation=0, dispatch_started_at=started
    )
    errors = checked(scenario, outcome)
    assert ("CONTROL_DISPATCH_TIME_MISMATCH" in errors) == denied
    if not denied:
        assert errors == []


def test_prepared_unsent_still_requires_a_known_generation():
    scenario, outcome = reportless_packet()
    scenario["trusted"]["deliveries"][0].update(
        state="prepared",
        control_generation=999,
        dispatch_started_at=None,
        response_received_at=None,
    )
    errors = checked(scenario, outcome)
    assert "CONTROL_GENERATION_UNKNOWN" in errors
    assert "CONTROL_TIME_UNKNOWN" not in errors


@pytest.mark.parametrize("execution", ["paused", "cancelled", "waiting_human"])
def test_human_state_without_control_audit_is_not_certified(execution):
    scenario, outcome = reportless_packet()
    scenario["trusted"].update(controls=[], final_generation=0, execution=execution)
    outcome.update(control_generation=0, execution=execution)
    scenario["trusted"]["deliveries"][0]["control_generation"] = 0
    assert "CONTROL_STATE_MISMATCH" in checked(scenario, outcome)


@pytest.mark.parametrize(
    "action,state", [("cancel", "cancelled"), ("correct", "waiting_human")]
)
@pytest.mark.parametrize("match", [True, False])
def test_human_state_requires_matching_latest_accepted_control(action, state, match):
    scenario, outcome = reportless_packet()
    scenario["trusted"]["controls"].append(
        {"generation": 2, "action": action, "at": "2026-09-10T00:04:00Z"}
    )
    actual = (
        state if match else ("waiting_human" if state == "cancelled" else "cancelled")
    )
    scenario["trusted"].update(final_generation=2, execution=actual)
    outcome.update(control_generation=2, execution=actual)
    errors = checked(scenario, outcome)
    assert ("CONTROL_STATE_MISMATCH" in errors) != match
    if match:
        assert errors == []


@pytest.mark.parametrize("action", ["cancel", "correct"])
def test_paused_does_not_borrow_an_unrelated_control_action(action):
    scenario, outcome = reportless_packet()
    scenario["trusted"]["controls"].append(
        {"generation": 2, "action": action, "at": "2026-09-10T00:04:00Z"}
    )
    scenario["trusted"].update(final_generation=2, execution="paused")
    outcome.update(control_generation=2, execution="paused")
    assert "CONTROL_STATE_MISMATCH" in checked(scenario, outcome)


def test_no_initial_views_require_original_and_actual_bytes_equal():
    scenario, outcome = reportless_packet()
    actual = "substituted question"
    scenario["agent_input"].update(
        actual_user_content=actual, actual_user_content_sha256=content_hash(actual)
    )
    delivery = scenario["trusted"]["deliveries"][0]
    body = json.loads(delivery["business_projection_content"])
    body["actual_user_content"] = actual
    value = json.dumps(body)
    delivery.update(
        business_projection_content=value, business_projection_hash=content_hash(value)
    )
    assert "INITIAL_INPUT_REASSEMBLY_MISMATCH" in checked(scenario, outcome)


def test_imported_views_allow_only_an_explicit_business_view_append():
    scenario, outcome = reportless_packet()
    view = scenario["trusted"]["deliveries"][0]["views"][0]
    original = json.dumps({"request": "investigate"}, sort_keys=True)
    actual = json.dumps(
        {
            "request": "investigate",
            "business_tool_views": [{"evidence_id": view["id"]}],
        },
        sort_keys=True,
    )
    scenario["agent_input"].update(
        evidence_context=scenario["trusted"]["deliveries"][0]["context"],
        initial_views=[view],
        original_user_content=original,
        original_user_content_sha256=content_hash(original),
        actual_user_content=actual,
        actual_user_content_sha256=content_hash(actual),
    )
    delivery = scenario["trusted"]["deliveries"][0]
    body = json.loads(delivery["business_projection_content"])
    body["actual_user_content"] = actual
    value = json.dumps(body)
    delivery.update(
        business_projection_content=value, business_projection_hash=content_hash(value)
    )
    assert "INITIAL_INPUT_REASSEMBLY_MISMATCH" not in checked(scenario, outcome)
    altered = json.loads(actual)
    altered["request"] = "replaced"
    scenario["agent_input"]["actual_user_content"] = json.dumps(altered, sort_keys=True)
    scenario["agent_input"]["actual_user_content_sha256"] = content_hash(
        scenario["agent_input"]["actual_user_content"]
    )
    assert "INITIAL_INPUT_REASSEMBLY_MISMATCH" in checked(scenario, outcome)


@pytest.mark.parametrize("action", ["cancel", "correct"])
def test_prepared_unsent_requires_new_run_authority(action):
    scenario, outcome = reportless_packet()
    scenario["trusted"]["controls"][0]["action"] = action
    delivery = scenario["trusted"]["deliveries"][0]
    delivery.update(
        state="prepared", dispatch_started_at=None, response_received_at=None
    )
    assert "CONTROL_DISPATCH_NOT_AUTHORIZED" in checked(scenario, outcome)


@pytest.mark.parametrize("mutation", [None, "request", "view", "extra"])
def test_verified_preembedded_business_views_preserve_original(mutation):
    scenario, outcome = reportless_packet()
    view = scenario["trusted"]["deliveries"][0]["views"][0]
    original_object = {
        "request": "investigate",
        "business_tool_views": [{"evidence_id": view["id"]}],
    }
    original = json.dumps(original_object, sort_keys=True)
    actual_object = json.loads(original)
    if mutation == "request":
        actual_object["request"] = "replacement"
    elif mutation == "view":
        actual_object["business_tool_views"][0]["evidence_id"] = "unverified"
    elif mutation == "extra":
        actual_object["extra"] = "not part of original input"
    actual = json.dumps(actual_object, sort_keys=True)
    scenario["agent_input"].update(
        evidence_context=scenario["trusted"]["deliveries"][0]["context"],
        initial_views=[view],
        original_user_content=original,
        original_user_content_sha256=content_hash(original),
        actual_user_content=actual,
        actual_user_content_sha256=content_hash(actual),
    )
    delivery = scenario["trusted"]["deliveries"][0]
    body = json.loads(delivery["business_projection_content"])
    body["actual_user_content"] = actual
    value = json.dumps(body)
    delivery.update(
        business_projection_content=value, business_projection_hash=content_hash(value)
    )
    errors = checked(scenario, outcome)
    assert ("INITIAL_INPUT_REASSEMBLY_MISMATCH" in errors) == (mutation is not None)
