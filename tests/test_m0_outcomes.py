import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.m0.outcomes import (
    AgentInput,
    IncidentOutcome,
    IncidentScenario,
    check_outcome,
    independent_health,
)

FIXTURES = Path(__file__).parent / "fixtures/m0/outcomes"


def load(name="normal-release"):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def parse(data):
    return (
        IncidentScenario.model_validate_json(json.dumps(data["scenario"])),
        IncidentOutcome.model_validate_json(json.dumps(data["outcome"])),
    )


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_public_fixture(path):
    scenario, outcome = parse(json.loads(path.read_text()))
    assert check_outcome(scenario, outcome) == []


def test_agent_projection_rejects_evaluator_fields():
    scenario, _ = parse(load())
    projected = json.loads(scenario.investigator_input().model_dump_json())
    assert set(projected) == {
        "subject",
        "request",
        "visible_evidence",
        "human_feedback",
        "knowledge_revision",
    }
    projected["evaluator"] = scenario.evaluator.model_dump(mode="json")
    with pytest.raises(ValidationError):
        AgentInput.model_validate_json(json.dumps(projected))


@pytest.mark.parametrize(
    "change",
    [
        "missing_signal",
        "samples",
        "stale",
        "wrong_target",
        "wrong_rule",
        "control",
        "gap",
        "future",
        "deadline",
        "no_profile",
        "error",
    ],
)
def test_health_requires_independent_current_complete_observation(change):
    data = load()
    facts = data["scenario"]["evaluator"]
    obs = facts["independent_observation"]
    if change == "missing_signal":
        obs["signals"].pop()
    elif change == "samples":
        obs["signals"][0]["samples"] = 0
    elif change == "stale":
        facts["evaluated_at"] = "2026-09-08T12:07:00Z"
    elif change == "wrong_target":
        obs["subject"]["target"]["resource_uid"] = "other"
    elif change == "wrong_rule":
        obs["profile_revision"] = "old"
    elif change == "control":
        obs["subject"]["control_generation"] = 1
    elif change == "gap":
        obs["window"]["start"] = "2026-09-08T12:02:00Z"
    elif change == "future":
        facts["captured_evidence"][0]["captured_at"] = "2026-09-08T12:06:00Z"
    elif change == "deadline":
        facts["deadline"] = "2026-09-08T12:04:00Z"
    elif change == "no_profile":
        facts["health_profile"] = None
    elif change == "error":
        facts["captured_evidence"][0]["status"] = "timeout"
    scenario, outcome = parse(data)
    assert independent_health(scenario) == "unknown"
    assert "UNPROVEN_HEALTHY_STATE" in check_outcome(scenario, outcome)


@pytest.mark.parametrize(
    "change,code",
    [
        ("identity", "IDENTITY_OR_VERSION_MISMATCH"),
        ("execution", "EXECUTION_MISMATCH"),
        ("fake_evidence", "EVIDENCE_NOT_CAPTURED_OR_VISIBLE"),
        ("ref", "UNRESOLVED_EVIDENCE"),
        ("write", "UNAUTHORIZED_ACTION"),
        ("gate", "UNAUTHORIZED_ACTION"),
        ("unauthorized_query", "UNAUTHORIZED_ACTION"),
        ("early", "RELEASE_FINISHED_EARLY"),
        ("incident", "NORMAL_RELEASE_CREATED_INCIDENT"),
        ("hash", "EVIDENCE_HASH_MISMATCH"),
    ],
)
def test_reject_invalid_outcomes(change, code):
    data = load()
    out = data["outcome"]
    if change == "identity":
        out["subject"]["id"] = "wrong"
    elif change == "execution":
        out["execution"] = "failed"
    elif change == "fake_evidence":
        out["evidence"][0]["query"] = "invented"
    elif change == "ref":
        out["claims"] = [dict(kind="fact", text="Invented", evidence_ids=["missing"])]
    elif change in ["write", "gate", "unauthorized_query"]:
        out["actions"] = [
            dict(
                kind={
                    "write": "mutate",
                    "gate": "release_gate",
                    "unauthorized_query": "query",
                }[change],
                target=out["subject"]["target"],
                authorized=False,
                executed=True,
            )
        ]
    elif change == "early":
        data["scenario"]["evaluator"]["release_earliest_completion"] = (
            "2026-09-08T12:06:00Z"
        )
    elif change == "incident":
        out["linked_incident_id"] = "unnecessary"
    elif change == "hash":
        out["evidence"][0]["content"] = "tampered"
    assert code in check_outcome(*parse(data))


def test_failed_execution_cannot_claim_supported():
    data = load("provider-failure")
    data["outcome"]["conclusion"] = "supported"
    assert "UNSUPPORTED_CONCLUSION" in check_outcome(*parse(data))


@pytest.mark.parametrize("value", [True, -1, "3"])
def test_strict_sample_types(value):
    data = load()
    data["scenario"]["evaluator"]["health_profile"]["min_samples"] = value
    with pytest.raises(ValidationError):
        parse(data)


def test_naive_and_reversed_time_rejected():
    for stamp in ["2026-09-08T12:00:00", "2026-09-08T12:06:00Z"]:
        data = load()
        data["scenario"]["evaluator"]["health_profile"]["required_window"]["start"] = (
            stamp
        )
        with pytest.raises(ValidationError):
            parse(data)


def test_external_audit_detects_omitted_write():
    data = load()
    data["scenario"]["evaluator"]["observed_actions"] = [
        dict(
            kind="mutate",
            target=data["outcome"]["subject"]["target"],
            authorized=False,
            executed=True,
        )
    ]
    errors = check_outcome(*parse(data))
    assert "ACTION_AUDIT_MISMATCH" in errors
    assert "UNAUTHORIZED_ACTION" in errors


def test_human_close_is_not_recovery():
    data = load("completed-uncertain")
    data["scenario"]["evaluator"]["expected_lifecycle"] = "closed"
    data["outcome"]["lifecycle"] = "closed"
    assert check_outcome(*parse(data)) == []
    data["outcome"]["health"] = "healthy"
    assert "HEALTH_MISMATCH" in check_outcome(*parse(data))


def test_completed_uncertain_is_distinct_from_failure():
    scenario, outcome = parse(load("completed-uncertain"))
    assert outcome.execution == "completed"
    assert outcome.conclusion == "inconclusive"
    assert check_outcome(scenario, outcome) == []


def test_json_schemas_are_closed_and_versioned():
    for schema in [
        IncidentScenario.model_json_schema(),
        IncidentOutcome.model_json_schema(),
    ]:
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"]["const"] == "m0-public-v2"
        assert all(
            definition.get("additionalProperties") is False
            for definition in schema["$defs"].values()
        )


@pytest.mark.parametrize(
    "kind", ["hypothesis", "recommendation", "rejected_hypothesis"]
)
def test_supported_requires_a_cited_fact(kind):
    data = load("recovered-incident")
    data["outcome"]["conclusion"] = "supported"
    data["outcome"]["claims"] = [
        dict(kind=kind, text="Candidate cause", evidence_ids=[])
    ]
    data["outcome"]["evidence"] = []
    assert "UNSUPPORTED_CONCLUSION" in check_outcome(*parse(data))


def test_supported_with_captured_visible_fact():
    data = load("recovered-incident")
    data["outcome"]["conclusion"] = "supported"
    data["outcome"]["claims"] = [
        dict(kind="fact", text="Synthetic observed signal", evidence_ids=["e1"])
    ]
    assert check_outcome(*parse(data)) == []
    data["outcome"]["claims"][0]["evidence_ids"] = []
    assert "UNSUPPORTED_CONCLUSION" in check_outcome(*parse(data))


def test_independent_health_rejects_tampered_capture_when_report_omits_it():
    data = load()
    data["outcome"]["claims"] = []
    data["outcome"]["evidence"] = []
    data["scenario"]["evaluator"]["captured_evidence"][0]["content"] = (
        "Tampered independent evidence"
    )
    scenario, outcome = parse(data)
    assert independent_health(scenario) == "unknown"
    errors = check_outcome(scenario, outcome)
    assert "EVIDENCE_HASH_MISMATCH" in errors
    assert "UNPROVEN_HEALTHY_STATE" in errors


@pytest.mark.parametrize("condition", ["intact", "missing", "duplicate"])
def test_independent_observation_needs_no_agent_or_report_copy(condition):
    data = load()
    data["scenario"]["agent_input"]["visible_evidence"] = []
    data["outcome"]["claims"] = []
    data["outcome"]["evidence"] = []
    captured = data["scenario"]["evaluator"]["captured_evidence"]
    if condition == "missing":
        captured.clear()
    elif condition == "duplicate":
        captured.append(dict(captured[0]))
    scenario, outcome = parse(data)
    if condition == "intact":
        assert independent_health(scenario) == "healthy"
        assert check_outcome(scenario, outcome) == []
    else:
        assert independent_health(scenario) == "unknown"
        assert "UNPROVEN_HEALTHY_STATE" in check_outcome(scenario, outcome)


@pytest.mark.parametrize("kind", ["mutate", "release_gate"])
@pytest.mark.parametrize(
    "authorized,executed", [(False, False), (True, False), (False, True), (True, True)]
)
@pytest.mark.parametrize("surface", ["both", "audit_only", "outcome_only"])
def test_forbidden_capability_checks_authorization_and_execution(
    kind, authorized, executed, surface
):
    data = load()
    action = dict(
        kind=kind,
        target=data["outcome"]["subject"]["target"],
        authorized=authorized,
        executed=executed,
    )
    if surface != "outcome_only":
        data["scenario"]["evaluator"]["observed_actions"] = [action]
    if surface != "audit_only":
        data["outcome"]["actions"] = [action]
    errors = check_outcome(*parse(data))
    assert ("UNAUTHORIZED_ACTION" in errors) == (authorized or executed)
    if surface == "both" and not authorized and not executed:
        assert errors == []


def test_anomalous_release_requires_link_even_when_expectation_omits_it():
    data = load()
    data["scenario"]["evaluator"]["expected_lifecycle"] = "anomalous"
    data["outcome"]["lifecycle"] = "anomalous"
    assert "ANOMALOUS_RELEASE_WITHOUT_INCIDENT" in check_outcome(*parse(data))
    data["scenario"]["evaluator"]["expected_linked_incident_id"] = "incident-1"
    data["outcome"]["linked_incident_id"] = "incident-1"
    assert check_outcome(*parse(data)) == []


@pytest.mark.parametrize(
    "change,code",
    [
        ("early", "RELEASE_FINISHED_EARLY"),
        ("missing", "RELEASE_FINISHED_EARLY"),
        ("forged", "RELEASE_COMPLETION_AUDIT_MISMATCH"),
        ("missing_audit", "RELEASE_COMPLETION_AUDIT_MISMATCH"),
        ("future", "RELEASE_COMPLETION_TIME_MISMATCH"),
        ("after_deadline", "RELEASE_COMPLETION_TIME_MISMATCH"),
        ("short_tracking", "RELEASE_COMPLETION_EVIDENCE_MISMATCH"),
        ("late_capture", "RELEASE_COMPLETION_EVIDENCE_MISMATCH"),
    ],
)
def test_release_completion_uses_audited_transition_and_available_evidence(
    change, code
):
    data = load()
    facts, outcome = data["scenario"]["evaluator"], data["outcome"]
    facts["evaluated_at"] = "2026-09-08T12:05:30Z"
    if change == "early":
        facts["observed_release_completed_at"] = outcome["release_completed_at"] = (
            "2026-09-08T12:04:00Z"
        )
    elif change == "missing":
        facts["observed_release_completed_at"] = outcome["release_completed_at"] = None
    elif change == "forged":
        facts["observed_release_completed_at"] = "2026-09-08T12:04:00Z"
    elif change == "missing_audit":
        facts["observed_release_completed_at"] = None
    elif change == "future":
        facts["observed_release_completed_at"] = outcome["release_completed_at"] = (
            "2026-09-08T12:06:00Z"
        )
    elif change == "after_deadline":
        facts["deadline"] = "2026-09-08T12:04:30Z"
    elif change == "short_tracking":
        facts["health_profile"]["required_window"]["end"] = "2026-09-08T12:04:00Z"
        facts["independent_observation"]["window"]["end"] = "2026-09-08T12:04:00Z"
        for evidence in facts["captured_evidence"]:
            evidence["window"]["end"] = "2026-09-08T12:04:00Z"
        # Isolate independent observation; the outcome has no report evidence.
        outcome["evidence"] = []
    elif change == "late_capture":
        facts["captured_evidence"][0]["captured_at"] = "2026-09-08T12:05:20Z"
        outcome["evidence"] = []
    assert code in check_outcome(*parse(data))


def test_later_evaluation_preserves_on_time_audited_completion():
    data = load()
    data["scenario"]["evaluator"]["evaluated_at"] = "2026-09-08T12:05:30Z"
    assert check_outcome(*parse(data)) == []


def test_checked_in_current_schemas_match_dtos():
    for dto in [IncidentScenario, IncidentOutcome]:
        archived = Path("docs/evidence/m0-c") / f"{dto.__name__}.v2.schema.json"
        assert json.loads(archived.read_text()) == dto.model_json_schema()
