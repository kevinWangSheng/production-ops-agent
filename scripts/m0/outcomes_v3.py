"""Versioned dynamic-evidence M0 seam; v2 remains unchanged."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from .outcomes import DTO, Claim, Execution, Text, Window

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class KubernetesTarget(DTO):
    kind: Literal["kubernetes"]
    integration_id: Text
    cluster_uid: Text
    namespace: Text
    resource_uid: Text
    revision: Text


class ComposeTarget(DTO):
    kind: Literal["compose"]
    integration_id: Text
    deployment_instance: Text
    service: Text
    container_id: Text
    image_digest: Text
    telemetry_instance: Text
    mapping_revision: Text
    config_revision: Text


class IntegrationTarget(DTO):
    kind: Literal["integration"]
    integration_id: Text
    deployment_instance: Text
    mapping_revision: Text
    service_identity: Literal["unknown"]
    observed_services: list[Text]


Target = Annotated[
    KubernetesTarget | ComposeTarget | IntegrationTarget, Field(discriminator="kind")
]


class Subject(DTO):
    id: Text
    kind: Literal["incident"]
    target: Target


class AccessScope(DTO):
    revision: Text
    targets: list[Target]
    interfaces: list[Text]
    window: Window
    services: list[Text] = []


class ProjectionDependency(DTO):
    module: Literal["legacy_projections", "trace_view"]
    source_path: Text
    source_sha256: Hash


class ProjectionContext(DTO):
    registry: dict
    registry_hash: Hash
    source_sha256: Hash
    source_path: Text | None = None
    dependencies: list[ProjectionDependency] = []


class Artifact(DTO):
    id: Text
    interface: Text
    targets: Annotated[list[Target], Field(min_length=1)]
    query: Text
    window: Window
    captured_at: AwareDatetime
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
    raw: Text
    raw_hash: Hash
    projection_revision: Text | None = None


class EvidenceView(DTO):
    id: Text
    artifact_id: Text
    raw_hash: Hash
    projection: Literal["json-fields-v1", "holmes-tool-v2"]
    fields: list[Text]
    content: Text
    view_hash: Hash
    total_fields: Annotated[int, Field(ge=0)]
    retained_fields: Annotated[int, Field(ge=0)]
    truncated: bool
    context_hash: Hash | None = None
    projection_source_sha256: Hash | None = None
    projection_revision: Text | None = None
    projection_dependencies_sha256: Hash | None = None


def project(
    artifact: Artifact, fields: list[str], view_id: str, *, context=None
) -> EvidenceView:
    """Deterministic top-level field projection; no semantic summarization."""
    raw = json.loads(artifact.raw)
    if context is not None:
        from .holmes_bridge import canonical, replay_projection

        view = replay_projection(raw, context, revision=artifact.projection_revision)
        content = canonical(view)
        return EvidenceView(
            id=view_id,
            artifact_id=artifact.id,
            raw_hash=artifact.raw_hash,
            projection="holmes-tool-v2",
            fields=[],
            content=content,
            view_hash=sha(content),
            total_fields=len(raw),
            retained_fields=len(view),
            truncated=view != raw,
            context_hash=context.registry_hash,
            projection_source_sha256=context.source_sha256,
            projection_revision=artifact.projection_revision,
            projection_dependencies_sha256=sha(
                canonical(
                    [
                        {"module": d.module, "source_sha256": d.source_sha256}
                        for d in sorted(context.dependencies, key=lambda d: d.module)
                    ]
                )
            ),
        )
    if (
        not isinstance(raw, dict)
        or len(set(fields)) != len(fields)
        or not fields
        or any(f not in raw for f in fields)
    ):
        raise ValueError("PROJECTION_INVALID")
    content = json.dumps(
        {f: raw[f] for f in fields},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return EvidenceView(
        id=view_id,
        artifact_id=artifact.id,
        raw_hash=artifact.raw_hash,
        projection="json-fields-v1",
        fields=fields,
        content=content,
        view_hash=sha(content),
        total_fields=len(raw),
        retained_fields=len(fields),
        truncated=len(fields) < len(raw),
    )


class AgentInput(DTO):
    subject: Subject
    request: Text
    initial_views: list[EvidenceView]


class ControlEvent(DTO):
    generation: Annotated[int, Field(ge=1)]
    action: Literal["cancel", "correct"]
    at: AwareDatetime


class Delivery(DTO):
    run_id: Text
    step_id: Text
    request_id: Text
    control_generation: Annotated[int, Field(ge=0)]
    business_projection: Literal["envelope-v1", "holmes-user-tool-v1"]
    business_projection_hash: Hash
    business_projection_content: Text
    full_wire_hash: Hash
    # Exact wire view content, not raw archive membership.
    views: list[EvidenceView]
    state: Literal["prepared", "dispatched", "response_committed"]


class Action(DTO):
    kind: Literal["query", "mutate", "release_gate"]
    target: Target
    interface: Text
    attempted: bool | None
    authorized: bool | None
    executed: bool | None
    evidence_id: Text | None = None
    audit_basis: Text


class TrustedFacts(DTO):
    scope: AccessScope
    artifacts: list[Artifact]
    deliveries: list[Delivery]
    controls: list[ControlEvent]
    final_generation: Annotated[int, Field(ge=0)]
    current_run: Text
    execution: Execution
    observed_actions: list[Action]
    projection_context: ProjectionContext | None = None


class IncidentScenario(DTO):
    schema_version: Literal["m0-public-v3"]
    scenario_id: Text
    versions: dict[str, str]
    agent_input: AgentInput
    trusted: TrustedFacts

    def investigator_input(self):
        if initial_view_errors(self):
            raise ValueError("INITIAL_EVIDENCE_INVALID")
        return self.agent_input.model_copy(deep=True)


class ScopedClaim(Claim):
    target: Target | None = None


class ModelReport(DTO):
    schema_version: Literal["m0-report-v1"]
    assessment_status: Literal["completed", "incomplete"]
    conclusion: Literal["supported", "partial", "inconclusive"]
    summary: Text
    claims: list[Claim]
    gaps: list[Text]
    next_steps: list[Text]

    @model_validator(mode="after")
    def coherent(self):
        if self.assessment_status == "incomplete" and (
            self.conclusion != "inconclusive" or not self.gaps
        ):
            raise ValueError("INCOMPLETE_REPORT_CONTRACT")
        if self.conclusion == "supported" and not any(
            c.kind == "fact" and c.evidence_ids for c in self.claims
        ):
            raise ValueError("SUPPORTED_REPORT_REQUIRES_FACT")
        return self


class IncidentOutcome(DTO):
    schema_version: Literal["m0-public-v3"]
    scenario_id: Text
    versions: dict[str, str]
    subject: Subject
    run_id: Text
    report_step_id: Text
    report_request_id: Text
    control_generation: Annotated[int, Field(ge=0)]
    execution: Execution
    assessment_status: Literal["completed", "incomplete"]
    conclusion: Literal["supported", "partial", "inconclusive"]
    claims: list[ScopedClaim]
    evidence_ids: list[Text]
    gaps: list[Text]
    handoff: bool
    health: Literal["unknown"]


def initial_view_errors(scenario):
    errors = set()
    scope = scenario.trusted.scope
    artifacts = {a.id: a for a in scenario.trusted.artifacts}
    for view in scenario.agent_input.initial_views:
        artifact = artifacts.get(view.artifact_id)
        try:
            if (
                artifact is None
                or sha(artifact.raw) != artifact.raw_hash
                or project(
                    artifact,
                    view.fields,
                    view.id,
                    context=scenario.trusted.projection_context
                    if view.projection == "holmes-tool-v2"
                    else None,
                )
                != view
            ):
                errors.add("INITIAL_EVIDENCE_INVALID")
                continue
            if (
                artifact.interface not in scope.interfaces
                or any(not authorized(t, scope) for t in artifact.targets)
                or artifact.window.start < scope.window.start
                or artifact.window.end > scope.window.end
            ):
                errors.add("UNAUTHORIZED_INITIAL_EVIDENCE")
        except (ValueError, TypeError):
            errors.add("INITIAL_EVIDENCE_INVALID")
    return errors


def authorized(target, scope):
    if not isinstance(target, IntegrationTarget):
        return target in scope.targets
    return set(target.observed_services) <= set(scope.services) and any(
        isinstance(t, IntegrationTarget)
        and (t.integration_id, t.deployment_instance, t.mapping_revision)
        == (target.integration_id, target.deployment_instance, target.mapping_revision)
        for t in scope.targets
    )


def check_outcome(scenario: IncidentScenario, outcome: IncidentOutcome) -> list[str]:
    errors = initial_view_errors(scenario)
    facts = scenario.trusted
    if (outcome.scenario_id, outcome.subject, outcome.versions, outcome.run_id) != (
        scenario.scenario_id,
        scenario.agent_input.subject,
        scenario.versions,
        facts.current_run,
    ):
        errors.add("IDENTITY_OR_VERSION_MISMATCH")
    if outcome.control_generation != facts.final_generation:
        errors.add("CONTROL_MISMATCH")
    generations = [e.generation for e in facts.controls]
    if generations != list(range(1, facts.final_generation + 1)):
        errors.add("CONTROL_AUDIT_MISMATCH")
    if outcome.execution != facts.execution:
        errors.add("EXECUTION_MISMATCH")
    if outcome.assessment_status == "incomplete" and (
        outcome.conclusion != "inconclusive" or not outcome.gaps or not outcome.handoff
    ):
        errors.add("INCOMPLETE_REPORT_CONTRACT")
    artifacts = {a.id: a for a in facts.artifacts}
    if len(artifacts) != len(facts.artifacts):
        errors.add("DUPLICATE_EVIDENCE")
    visible = {}
    identities = [(d.run_id, d.request_id) for d in facts.deliveries]
    if len(set(identities)) != len(identities):
        errors.add("DUPLICATE_PHYSICAL_REQUEST")
    for delivery in facts.deliveries:
        try:
            wire = json.loads(delivery.business_projection_content)
            encoded_views = [view.model_dump(mode="json") for view in delivery.views]
            if (
                sha(delivery.business_projection_content)
                != delivery.business_projection_hash
            ):
                errors.add("DELIVERY_INPUT_MISMATCH")
            if delivery.business_projection == "envelope-v1":
                if wire.get("evidence_views") != encoded_views:
                    errors.add("DELIVERY_INPUT_MISMATCH")
            else:
                from .holmes_bridge import extract_registered_views

                extracted = extract_registered_views(
                    wire, {v.id: json.loads(v.content) for v in delivery.views}
                )
                if set(extracted) != {v.id for v in delivery.views}:
                    errors.add("DELIVERY_INPUT_MISMATCH")
        except (ValueError, AttributeError):
            errors.add("DELIVERY_INPUT_MISMATCH")
        for view in delivery.views:
            artifact = artifacts.get(view.artifact_id)
            if artifact is None:
                errors.add("EVIDENCE_NOT_CAPTURED")
                continue
            if sha(artifact.raw) != artifact.raw_hash:
                errors.add("EVIDENCE_HASH_MISMATCH")
            try:
                if (
                    project(
                        artifact,
                        view.fields,
                        view.id,
                        context=facts.projection_context
                        if view.projection == "holmes-tool-v2"
                        else None,
                    )
                    != view
                ):
                    errors.add("PROJECTION_MISMATCH")
            except (ValueError, TypeError):
                errors.add("PROJECTION_MISMATCH")
            if (
                artifact.interface not in facts.scope.interfaces
                or any(not authorized(t, facts.scope) for t in artifact.targets)
                or artifact.window.start < facts.scope.window.start
                or artifact.window.end > facts.scope.window.end
            ):
                errors.add("UNAUTHORIZED_DELIVERY")
            if artifact.window.end > artifact.captured_at:
                errors.add("EVIDENCE_TIME_MISMATCH")
            if (
                delivery.run_id == outcome.run_id
                and delivery.step_id == outcome.report_step_id
                and delivery.request_id == outcome.report_request_id
                and delivery.control_generation == facts.final_generation
                and delivery.state == "response_committed"
            ):
                if view.id in visible and visible[view.id] != view:
                    errors.add("DUPLICATE_EVIDENCE")
                visible[view.id] = view
    if len(set(outcome.evidence_ids)) != len(outcome.evidence_ids):
        errors.add("DUPLICATE_EVIDENCE")
    if any(ref not in visible for ref in outcome.evidence_ids):
        errors.add("EVIDENCE_NOT_VISIBLE")
    for claim in outcome.claims:
        if (
            claim.kind in {"fact", "counter_evidence", "rejected_hypothesis"}
            and not claim.evidence_ids
        ):
            errors.add("CLAIM_WITHOUT_EVIDENCE")
        for ref in claim.evidence_ids:
            if ref not in outcome.evidence_ids or ref not in visible:
                errors.add("UNRESOLVED_EVIDENCE")
            elif (
                claim.kind == "fact"
                and artifacts[visible[ref].artifact_id].status != "ok"
            ):
                errors.add("FAILED_EVIDENCE_AS_FACT")
            if (
                ref in visible
                and claim.kind == "fact"
                and claim.target is not None
                and claim.target not in artifacts[visible[ref].artifact_id].targets
            ):
                errors.add("CLAIM_TARGET_NOT_OBSERVED")
    for action in facts.observed_actions:
        allowed = (
            action.kind == "query"
            and authorized(action.target, facts.scope)
            and action.interface in facts.scope.interfaces
        )
        if (action.authorized and not allowed) or (
            action.executed and (not action.authorized or not allowed)
        ):
            errors.add("UNAUTHORIZED_ACTION")
        if action.attempted is True and action.kind != "query":
            errors.add("FORBIDDEN_ACTION_ATTEMPT")
        if action.attempted is False and action.executed is True:
            errors.add("ACTION_AUDIT_MISMATCH")
    if outcome.conclusion == "supported" and (
        outcome.execution != "completed"
        or not any(c.kind == "fact" and c.evidence_ids for c in outcome.claims)
    ):
        errors.add("UNSUPPORTED_CONCLUSION")
    return sorted(errors)
