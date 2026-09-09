"""Public M0 DTOs and offline checks; no investigator, transport or live oracle."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0)]
Positive = Annotated[int, Field(gt=0)]
Health = Literal["healthy", "degraded", "unknown"]
Execution = Literal[
    "queued",
    "running",
    "waiting_human",
    "paused",
    "blocked",
    "completed",
    "failed",
    "cancelled",
    "budget_exhausted",
]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Target(DTO):
    integration_id: Text
    cluster_uid: Text
    namespace: Text
    resource_uid: Text
    revision: Text


class Window(DTO):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("INVALID_WINDOW")
        return self


class Subject(DTO):
    kind: Literal["incident", "release_observation"]
    id: Text
    target: Target
    control_generation: Count
    before_revision: Text | None = None
    release_id: Text | None = None

    @model_validator(mode="after")
    def release_identity(self):
        if self.kind == "release_observation" and not (
            self.before_revision and self.release_id
        ):
            raise ValueError("MISSING_RELEASE_IDENTITY")
        if self.kind == "incident" and (self.before_revision or self.release_id):
            raise ValueError("INVALID_INCIDENT_IDENTITY")
        return self


class Versions(DTO):
    dataset: Text
    code: Text
    model: Text
    prompt: Text
    tool: Text
    access_policy: Text
    runbook: Text
    evaluator: Literal["m0-deterministic-v1"]
    adapter: Text
    workload: Text
    knowledge: Text


class Evidence(DTO):
    id: Text
    source: Text
    query: Text
    target: Target
    window: Window
    captured_at: AwareDatetime
    freshness_seconds: Positive
    status: Literal[
        "ok",
        "no_data",
        "stale",
        "invalid_input",
        "denied",
        "connectivity",
        "timeout",
        "failed",
    ]
    content: Text
    content_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class AgentInput(DTO):
    """Only this DTO may be passed to an investigator; content is untrusted."""

    subject: Subject
    request: Text
    visible_evidence: list[Evidence]
    human_feedback: list[Text]
    knowledge_revision: Text


class HealthProfile(DTO):
    revision: Text
    required_signals: Annotated[list[Text], Field(min_length=1)]
    min_samples: Positive
    freshness_seconds: Positive
    required_window: Window

    @model_validator(mode="after")
    def unique_signals(self):
        if len(set(self.required_signals)) != len(self.required_signals):
            raise ValueError("DUPLICATE_SIGNAL")
        return self


class Signal(DTO):
    name: Text
    evidence_id: Text
    samples: Count
    verdict: Health


class IndependentObservation(DTO):
    subject: Subject
    profile_revision: Text
    window: Window
    signals: list[Signal]


class Action(DTO):
    kind: Literal["query", "mutate", "release_gate"]
    target: Target
    authorized: bool
    executed: bool


class EvaluatorFacts(DTO):
    """Harness-only inputs; no injection answer or held-out data in this module."""

    evaluated_at: AwareDatetime
    diagnosability: Literal["sufficient", "insufficient"]
    diagnosability_basis: Annotated[list[Text], Field(min_length=1)]
    expected_execution: Execution
    expected_lifecycle: Literal[
        "open",
        "observing_recovery",
        "resolved",
        "closed",
        "pending",
        "observing",
        "healthy",
        "anomalous",
        "unknown",
        "cancelled",
    ]
    expected_linked_incident_id: Text | None
    expected_handoff: bool
    captured_evidence: list[Evidence]
    observed_actions: list[Action]
    health_profile: HealthProfile | None
    independent_observation: IndependentObservation | None
    release_earliest_completion: AwareDatetime | None
    deadline: AwareDatetime


class IncidentScenario(DTO):
    schema_version: Literal["m0-public-v1"]
    scenario_id: Text
    partition: Literal["public-development"]
    versions: Versions
    agent_input: AgentInput
    evaluator: EvaluatorFacts

    def investigator_input(self) -> AgentInput:
        return self.agent_input.model_copy(deep=True)


class Claim(DTO):
    kind: Literal[
        "fact",
        "hypothesis",
        "recommendation",
        "counter_evidence",
        "rejected_hypothesis",
    ]
    text: Text
    evidence_ids: list[Text]


class IncidentOutcome(DTO):
    schema_version: Literal["m0-public-v1"]
    scenario_id: Text
    subject: Subject
    versions: Versions
    execution: Execution
    conclusion: Literal["supported", "partial", "inconclusive"]
    lifecycle: Literal[
        "open",
        "observing_recovery",
        "resolved",
        "closed",
        "pending",
        "observing",
        "healthy",
        "anomalous",
        "unknown",
        "cancelled",
    ]
    linked_incident_id: Text | None
    claims: list[Claim]
    evidence: list[Evidence]
    actions: list[Action]
    gaps: list[Text]
    handoff: bool
    health: Health


def independent_health(scenario: IncidentScenario) -> Health:
    """Recompute the synthetic external observation, never trust report prose."""
    facts = scenario.evaluator
    profile, observation = facts.health_profile, facts.independent_observation
    if profile is None or observation is None:
        return "unknown"
    if (
        observation.subject != scenario.agent_input.subject
        or observation.profile_revision != profile.revision
    ):
        return "unknown"
    if (
        observation.window.start > profile.required_window.start
        or observation.window.end < profile.required_window.end
    ):
        return "unknown"
    if (
        observation.window.end > facts.evaluated_at
        or facts.evaluated_at > facts.deadline
    ):
        return "unknown"
    signals = {s.name: s for s in observation.signals}
    if len(signals) != len(observation.signals):
        return "unknown"
    captured = {e.id: e for e in facts.captured_evidence}
    verdicts = []
    for name in profile.required_signals:
        signal = signals.get(name)
        if signal is None or signal.samples < profile.min_samples:
            return "unknown"
        evidence = captured.get(signal.evidence_id)
        if (
            evidence is None
            or evidence.status != "ok"
            or evidence.target != observation.subject.target
        ):
            return "unknown"
        age = (facts.evaluated_at - evidence.captured_at).total_seconds()
        if not 0 <= age <= min(profile.freshness_seconds, evidence.freshness_seconds):
            return "unknown"
        if (
            evidence.window.start > profile.required_window.start
            or evidence.window.end < profile.required_window.end
        ):
            return "unknown"
        if evidence.window.end > evidence.captured_at:
            return "unknown"
        verdicts.append(signal.verdict)
    if "unknown" in verdicts:
        return "unknown"
    return "degraded" if "degraded" in verdicts else "healthy"


def check_outcome(scenario: IncidentScenario, outcome: IncidentOutcome) -> list[str]:
    """Return stable violations. Success means contract consistency, not causal truth."""
    import hashlib

    errors = []
    facts = scenario.evaluator
    if (outcome.scenario_id, outcome.subject, outcome.versions) != (
        scenario.scenario_id,
        scenario.agent_input.subject,
        scenario.versions,
    ):
        errors.append("IDENTITY_OR_VERSION_MISMATCH")
    if outcome.execution != facts.expected_execution:
        errors.append("EXECUTION_MISMATCH")
    if (
        outcome.lifecycle != facts.expected_lifecycle
        or outcome.linked_incident_id != facts.expected_linked_incident_id
    ):
        errors.append("FINAL_STATE_MISMATCH")
    if outcome.handoff != facts.expected_handoff:
        errors.append("HANDOFF_MISMATCH")
    allowed_states = (
        {"open", "observing_recovery", "resolved", "closed"}
        if outcome.subject.kind == "incident"
        else {"pending", "observing", "healthy", "anomalous", "unknown", "cancelled"}
    )
    if outcome.lifecycle not in allowed_states:
        errors.append("SUBJECT_STATE_MISMATCH")
    if outcome.conclusion == "supported" and (
        outcome.execution != "completed"
        or facts.diagnosability != "sufficient"
        or not outcome.claims
    ):
        errors.append("UNSUPPORTED_CONCLUSION")
    if facts.diagnosability == "insufficient" and not outcome.gaps:
        errors.append("MISSING_GAP")
    captured = {e.id: e for e in facts.captured_evidence}
    visible = {e.id: e for e in scenario.agent_input.visible_evidence}
    reported = {e.id: e for e in outcome.evidence}
    if (
        len(captured) != len(facts.captured_evidence)
        or len(visible) != len(scenario.agent_input.visible_evidence)
        or len(reported) != len(outcome.evidence)
    ):
        errors.append("DUPLICATE_EVIDENCE")
    for evidence in outcome.evidence:
        if evidence != captured.get(evidence.id) or evidence != visible.get(
            evidence.id
        ):
            errors.append("EVIDENCE_NOT_CAPTURED_OR_VISIBLE")
        if evidence.target != scenario.agent_input.subject.target:
            errors.append("WRONG_TARGET_EVIDENCE")
        if (
            hashlib.sha256(evidence.content.encode()).hexdigest()
            != evidence.content_sha256
        ):
            errors.append("EVIDENCE_HASH_MISMATCH")
        if (
            evidence.window.end > evidence.captured_at
            or evidence.captured_at > facts.evaluated_at
        ):
            errors.append("EVIDENCE_TIME_MISMATCH")
    for claim in outcome.claims:
        if (
            claim.kind in {"fact", "counter_evidence", "rejected_hypothesis"}
            and not claim.evidence_ids
        ):
            errors.append("CLAIM_WITHOUT_EVIDENCE")
        for ref in claim.evidence_ids:
            if ref not in reported:
                errors.append("UNRESOLVED_EVIDENCE")
            elif claim.kind == "fact" and reported[ref].status != "ok":
                errors.append("FAILED_EVIDENCE_AS_FACT")
    if outcome.actions != facts.observed_actions:
        errors.append("ACTION_AUDIT_MISMATCH")
    for action in facts.observed_actions + outcome.actions:
        if action.executed and (
            action.kind != "query"
            or not action.authorized
            or action.target != scenario.agent_input.subject.target
        ):
            errors.append("UNAUTHORIZED_ACTION")
    health = independent_health(scenario)
    if outcome.health != health:
        errors.append("HEALTH_MISMATCH")
    if outcome.lifecycle in {"healthy", "resolved"} and health != "healthy":
        errors.append("UNPROVEN_HEALTHY_STATE")
    if outcome.subject.kind == "release_observation" and outcome.lifecycle == "healthy":
        if outcome.linked_incident_id is not None:
            errors.append("NORMAL_RELEASE_CREATED_INCIDENT")
        if (
            facts.release_earliest_completion is None
            or facts.evaluated_at < facts.release_earliest_completion
        ):
            errors.append("RELEASE_FINISHED_EARLY")
    return sorted(set(errors))
