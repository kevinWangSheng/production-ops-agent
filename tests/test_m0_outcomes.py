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
        assert schema["properties"]["schema_version"]["const"] == "m0-public-v1"
        assert all(
            definition.get("additionalProperties") is False
            for definition in schema["$defs"].values()
        )
