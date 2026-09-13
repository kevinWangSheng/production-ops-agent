import json
from copy import deepcopy

import pytest

from scripts.m0.outcomes_v3 import (
    Artifact,
    IncidentOutcome,
    IncidentScenario,
    check_outcome,
    project,
    sha,
)


def packet():
    target = dict(
        kind="compose",
        integration_id="lab",
        deployment_instance="round02",
        service="checkout",
        container_id="immutable-a",
        image_digest="sha256:a",
        telemetry_instance="a",
        mapping_revision="v1",
        config_revision="config1",
    )
    dependency = dict(
        target, service="payment", container_id="immutable-b", telemetry_instance="b"
    )
    window = dict(start="2026-09-10T00:00:00Z", end="2026-09-10T00:01:00Z")
    raw = json.dumps({"errors": 5, "omitted": "not delivered"})
    artifact = dict(
        id="artifact",
        interface="traces",
        targets=[dependency],
        query="payment trace",
        window=window,
        captured_at="2026-09-10T00:02:00Z",
        status="ok",
        raw=raw,
        raw_hash=sha(raw),
    )
    a = Artifact.model_validate_json(json.dumps(artifact))
    view = project(a, ["errors"], "view").model_dump(mode="json")
    wire = json.dumps({"evidence_views": [view]})
    subject = dict(id="incident", kind="incident", target=target)
    scenario = dict(
        schema_version="m0-public-v3",
        scenario_id="case",
        versions={"code": "test"},
        agent_input=dict(subject=subject, request="investigate", initial_views=[]),
        trusted=dict(
            scope=dict(
                revision="v1",
                targets=[target, dependency],
                interfaces=["traces"],
                window=window,
            ),
            artifacts=[artifact],
            deliveries=[
                dict(
                    run_id="run",
                    step_id="step",
                    request_id="request",
                    control_generation=1,
                    business_projection="envelope-v1",
                    business_projection_hash=sha(wire),
                    full_wire_hash=sha(wire),
                    business_projection_content=wire,
                    views=[view],
                    state="response_committed",
                )
            ],
            controls=[dict(generation=1, action="correct", at="2026-09-10T00:03:00Z")],
            final_generation=1,
            current_run="run",
            execution="completed",
            observed_actions=[],
        ),
    )
    outcome = dict(
        schema_version="m0-public-v3",
        scenario_id="case",
        versions={"code": "test"},
        subject=subject,
        run_id="run",
        report_step_id="step",
        report_request_id="request",
        control_generation=1,
        execution="completed",
        assessment_status="completed",
        conclusion="supported",
        claims=[dict(kind="fact", text="Five errors observed", evidence_ids=["view"])],
        evidence_ids=["view"],
        gaps=[],
        handoff=False,
        health="unknown",
    )
    return scenario, outcome


def check(s, o):
    return check_outcome(
        IncidentScenario.model_validate_json(json.dumps(s)),
        IncidentOutcome.model_validate_json(json.dumps(o)),
    )


def test_dynamic_dependency_view_and_current_control():
    s, o = packet()
    assert check(s, o) == []
    assert (
        IncidentScenario.model_validate_json(json.dumps(s))
        .investigator_input()
        .initial_views
        == []
    )


def test_view_raw_delivery_and_permission_tampering():
    s, o = packet()
    changed = deepcopy(s)
    changed["trusted"]["deliveries"][0]["views"][0]["content"] = "fabricated"
    assert "PROJECTION_MISMATCH" in check(changed, o)
    changed = deepcopy(s)
    changed["trusted"]["scope"]["targets"] = changed["trusted"]["scope"]["targets"][:1]
    assert "UNAUTHORIZED_DELIVERY" in check(changed, o)
    changed = deepcopy(s)
    changed["trusted"]["deliveries"][0]["business_projection_content"] = "{}"
    assert "DELIVERY_INPUT_MISMATCH" in check(changed, o)
    changed = deepcopy(s)
    changed["trusted"]["deliveries"][0]["state"] = "dispatched"
    assert "EVIDENCE_NOT_VISIBLE" in check(changed, o)
    changed = deepcopy(o)
    changed["control_generation"] = 0
    assert "CONTROL_MISMATCH" in check(s, changed)


def test_raw_archive_is_not_visible_evidence():
    s, o = packet()
    o["claims"][0]["evidence_ids"] = ["artifact"]
    assert "UNRESOLVED_EVIDENCE" in check(s, o)


def test_initial_view_is_checked_before_investigator_projection():
    import pytest

    s, o = packet()
    bad = deepcopy(s["trusted"]["deliveries"][0]["views"][0])
    bad["artifact_id"] = "untrusted-secret"
    s["agent_input"]["initial_views"] = [bad]
    assert "INITIAL_EVIDENCE_INVALID" in check(s, o)
    scenario = IncidentScenario.model_validate_json(json.dumps(s))
    with pytest.raises(ValueError, match="INITIAL_EVIDENCE_INVALID"):
        scenario.investigator_input()


@pytest.mark.parametrize(
    "actions", [("new_run",), ("cancel", "new_run"), ("correct", "new_run")]
)
def test_new_run_generations_cross_highest_acceptance_seam(actions):
    scenario, outcome = packet()
    generation = len(actions)
    scenario["trusted"]["controls"] = [
        {"generation": index, "action": action, "at": f"2026-09-10T00:03:0{index}Z"}
        for index, action in enumerate(actions, 1)
    ]
    scenario["trusted"].update(final_generation=generation, current_run="fresh-run")
    scenario["trusted"]["deliveries"][0].update(
        run_id="fresh-run", control_generation=generation
    )
    outcome.update(run_id="fresh-run", control_generation=generation)
    assert check(scenario, outcome) == []
    stale_run = deepcopy(outcome)
    stale_run["run_id"] = "run"
    assert "IDENTITY_OR_VERSION_MISMATCH" in check(scenario, stale_run)
    stale_generation = deepcopy(outcome)
    stale_generation["control_generation"] = generation - 1
    assert "CONTROL_MISMATCH" in check(scenario, stale_generation)
    missing = deepcopy(scenario)
    missing["trusted"]["controls"].pop()
    assert "CONTROL_AUDIT_MISMATCH" in check(missing, outcome)


@pytest.mark.parametrize("conclusion", ["partial", "inconclusive"])
@pytest.mark.parametrize(
    "case",
    [
        "valid_empty",
        "missing",
        "wrong_run",
        "wrong_step",
        "wrong_request",
        "old_generation",
        "not_committed",
        "duplicate",
    ],
)
def test_completed_report_requires_exact_committed_delivery_even_without_evidence(
    conclusion, case
):
    scenario, outcome = packet()
    outcome.update(conclusion=conclusion, claims=[], evidence_ids=[])
    delivery = scenario["trusted"]["deliveries"][0]
    content = json.dumps({"evidence_views": []})
    delivery.update(
        views=[],
        business_projection_content=content,
        business_projection_hash=sha(content),
        full_wire_hash=sha(content),
    )
    if case == "missing":
        scenario["trusted"]["deliveries"] = []
    elif case == "wrong_run":
        delivery["run_id"] = "old-run"
    elif case == "wrong_step":
        delivery["step_id"] = "other-step"
    elif case == "wrong_request":
        delivery["request_id"] = "other-request"
    elif case == "old_generation":
        delivery["control_generation"] = 0
    elif case == "not_committed":
        delivery["state"] = "dispatched"
    elif case == "duplicate":
        scenario["trusted"]["deliveries"].append(deepcopy(delivery))
    errors = check(scenario, outcome)
    if case == "valid_empty":
        assert errors == []
    else:
        assert "REPORT_DELIVERY_MISMATCH" in errors


@pytest.mark.parametrize(
    "execution", ["waiting_human", "blocked", "cancelled", "budget_exhausted"]
)
def test_trusted_incomplete_handoff_needs_no_model_report(execution):
    scenario, outcome = packet()
    scenario["trusted"].update(execution=execution, deliveries=[])
    outcome.update(
        execution=execution,
        assessment_status="incomplete",
        conclusion="inconclusive",
        claims=[],
        evidence_ids=[],
        gaps=["Investigation could not proceed"],
        handoff=True,
        report_step_id="not_started",
        report_request_id="not_started",
    )
    assert check(scenario, outcome) == []


@pytest.mark.parametrize(
    "execution,assessment",
    [("completed", "incomplete"), ("running", "completed"), ("completed", "completed")],
)
def test_either_completion_dimension_requires_exact_report(execution, assessment):
    scenario, outcome = packet()
    scenario["trusted"]["execution"] = execution
    outcome.update(
        execution=execution,
        assessment_status=assessment,
        conclusion="inconclusive",
        claims=[],
        evidence_ids=[],
        gaps=["Uncertain finding"],
        handoff=True,
    )
    delivery = scenario["trusted"]["deliveries"][0]
    content = json.dumps({"evidence_views": []})
    delivery.update(
        views=[],
        business_projection_content=content,
        business_projection_hash=sha(content),
        full_wire_hash=sha(content),
    )
    assert check(scenario, outcome) == []
    scenario["trusted"]["deliveries"] = []
    assert "REPORT_DELIVERY_MISMATCH" in check(scenario, outcome)
