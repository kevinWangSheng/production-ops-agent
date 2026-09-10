"""Current strict report contract. Historical v3 remains explicitly versioned."""

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from . import outcomes_v3 as legacy
from .outcomes import DTO, Claim, Execution, Text, Window

Hash = legacy.Hash
Target = legacy.Target
FACTLIKE = {"fact", "counter_evidence", "rejected_hypothesis"}
CONTEXT_TYPE = "opspilot-evidence-context-v4"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def content_hash(content):
    return hashlib.sha256(content.encode()).hexdigest()


def target_ref(run_id, target):
    value = target.model_dump(mode="json") if hasattr(target, "model_dump") else target
    return "target:" + digest({"run_id": run_id, "target": value})


class Timing(DTO):
    operation_started_at: AwareDatetime | None = None
    collection_completed_at: AwareDatetime | None = None
    source_start_at: AwareDatetime | None = None
    source_end_at: AwareDatetime | None = None
    source_time_basis: Literal[
        "event_time", "source_sample_time", "source_coverage", "unknown"
    ] = "unknown"


class TimePolicy(DTO):
    id: Text
    revision: Text
    integration_id: Text
    interfaces: Annotated[list[Text], Field(min_length=1)]
    mode: Literal["historical_window", "current"]
    reference_rule: Literal["dispatch_started_at", "response_received_at"]
    window: Window | None = None
    max_source_age_seconds: Annotated[int, Field(gt=0)] | None = None
    target_refs: list[Text] = []
    all_authorized_targets: bool = False
    scope_revision: Text | None = None

    @model_validator(mode="after")
    def explicit_target_scope(self):
        if self.all_authorized_targets and not self.scope_revision:
            raise ValueError("POLICY_SCOPE_REVISION_REQUIRED")
        return self


class ViewBinding(DTO):
    view_hash: Hash
    target_refs: list[Text]
    time_scope_refs: list[Text]
    timing: Timing
    status: Literal[
        "ok",
        "no_data",
        "stale",
        "invalid_input",
        "denied",
        "connectivity",
        "timeout",
        "failed",
        "unknown",
    ]


class TimingRecord(DTO):
    view_hash: Hash
    timing: Timing


class EvidenceContext(DTO):
    type: Literal["opspilot-evidence-context-v4"] = CONTEXT_TYPE
    run_id: Text
    target_catalog: dict[str, Target]
    view_bindings: dict[str, ViewBinding]
    time_policies: list[TimePolicy]


class ClaimV2(Claim):
    target_refs: list[Text] = []
    time_scope_ref: Text | None = None

    @model_validator(mode="after")
    def explicit_fact_scope(self):
        if self.kind in FACTLIKE and (
            not self.target_refs or not self.time_scope_ref or not self.evidence_ids
        ):
            raise ValueError("FACT_SCOPE_REQUIRED")
        if len(set(self.target_refs)) != len(self.target_refs):
            raise ValueError("DUPLICATE_TARGET_REF")
        return self


class ModelReportV2(DTO):
    schema_version: Literal["m0-report-v2"]
    assessment_status: Literal["completed", "incomplete"]
    conclusion: Literal["supported", "partial", "inconclusive"]
    summary: Text
    claims: list[ClaimV2]
    gaps: list[Text]
    next_steps: list[Text]

    @model_validator(mode="after")
    def coherent(self):
        if not self.summary.strip():
            raise ValueError("EMPTY_SUMMARY")
        if self.assessment_status == "incomplete" and (
            self.conclusion != "inconclusive" or not self.gaps
        ):
            raise ValueError("INCOMPLETE_REPORT_CONTRACT")
        if self.conclusion == "supported" and not any(
            c.kind == "fact" and c.evidence_ids for c in self.claims
        ):
            raise ValueError("SUPPORTED_REPORT_REQUIRES_FACT")
        return self


class Delivery(legacy.Delivery):
    context: EvidenceContext
    dispatch_started_at: AwareDatetime | None = None
    response_received_at: AwareDatetime | None = None


class ReportCapture(DTO):
    run_id: Text
    step_id: Text
    request_id: Text
    control_generation: Annotated[int, Field(ge=0)]
    content: str
    content_sha256: Hash
    response_received_at: AwareDatetime | None = None


class TrustedFacts(legacy.TrustedFacts):
    deliveries: list[Delivery]
    time_policies: list[TimePolicy]
    evaluation_at: AwareDatetime | None = None
    report_capture: ReportCapture | None = None
    timing_records: dict[str, TimingRecord] = {}


class UnverifiedInitialView(DTO):
    location: Text
    content: str
    content_sha256: Hash
    reason: Text


class AgentInput(legacy.AgentInput):
    evidence_context: EvidenceContext | None = None
    original_user_content: str | None = None
    original_user_content_sha256: Hash | None = None
    actual_user_content: str | None = None
    actual_user_content_sha256: Hash | None = None
    unverified_initial_views: list[UnverifiedInitialView] = []


class IncidentScenario(DTO):
    schema_version: Literal["m0-public-v4"]
    scenario_id: Text
    versions: dict[str, str]
    agent_input: AgentInput
    trusted: TrustedFacts

    def investigator_input(self):
        if self.agent_input.unverified_initial_views:
            raise ValueError("UNVERIFIED_INITIAL_EVIDENCE")
        errors = input_provenance_errors(self.agent_input) | context_errors(
            self.agent_input.evidence_context,
            self.agent_input.initial_views,
            self.trusted,
            self.trusted.current_run,
        )
        errors |= legacy.initial_view_errors(self)
        if errors:
            raise ValueError("INITIAL_CONTEXT_INVALID")
        return self.agent_input.model_copy(deep=True)


class IncidentOutcome(DTO):
    schema_version: Literal["m0-public-v4"]
    scenario_id: Text
    versions: dict[str, str]
    subject: legacy.Subject
    run_id: Text
    control_generation: Annotated[int, Field(ge=0)]
    execution: Execution
    report_step_id: Text | None = None
    report_request_id: Text | None = None
    report: ModelReportV2 | None = None
    report_content: str | None = None
    report_content_sha256: Hash | None = None
    evidence_ids: list[Text]
    handoff: bool
    handoff_reasons: list[Text]
    health: Literal["unknown"]


def build_context(run_id, views, registry, time_policies, timings, *, allowed_scope):
    """Build only from runner-registered views destined for this request."""
    from .holmes_bridge import observed_targets

    policies = [
        p if isinstance(p, TimePolicy) else TimePolicy.model_validate(p)
        for p in time_policies
    ]
    catalog, bindings = {}, {}
    if registry["integration_id"] != allowed_scope["integration_id"]:
        raise ValueError("TARGET_SCOPE_DENIED")
    for evidence_id, view in views.items():
        if view.get("evidence_id") != evidence_id:
            raise ValueError("VIEW_ID_MISMATCH")
        status = (
            "ok"
            if view.get("http_status") == 200 and not view.get("error")
            else "failed"
        )
        targets = (
            observed_targets(view, registry, digest(registry)) if status == "ok" else []
        )
        refs = []
        for target in targets:
            services = (
                {target.service}
                if isinstance(target, legacy.ComposeTarget)
                else set(target.observed_services)
            )
            if target.integration_id != allowed_scope[
                "integration_id"
            ] or not services <= set(allowed_scope["services"]):
                raise ValueError("TARGET_SCOPE_DENIED")
            ref = target_ref(run_id, target)
            catalog[ref] = target
            refs.append(ref)
        timing = timings.get(evidence_id, Timing())
        scope_revision = allowed_scope["policy_revision"]
        bindings[evidence_id] = ViewBinding(
            view_hash=digest(view),
            target_refs=refs,
            time_scope_refs=[
                p.id
                for p in policies
                if p.integration_id == registry["integration_id"]
                and view["tool"] in p.interfaces
                and any(
                    ref in p.target_refs
                    or (p.all_authorized_targets and p.scope_revision == scope_revision)
                    for ref in refs
                )
            ],
            timing=timing
            if isinstance(timing, Timing)
            else Timing.model_validate(timing),
            status=status,
        )
    return EvidenceContext(
        run_id=run_id,
        target_catalog=catalog,
        view_bindings=bindings,
        time_policies=policies,
    )


def validate_report_context(report, context):
    """Structural scope/status check. Full temporal/output checks use check_outcome."""
    errors = set()
    policies = {p.id: p for p in context.time_policies}
    if len(policies) != len(context.time_policies):
        errors.add("DUPLICATE_TIME_POLICY")
    for claim in report.claims:
        cited = [context.view_bindings.get(ref) for ref in claim.evidence_ids]
        if any(binding is None for binding in cited):
            errors.add("UNRESOLVED_EVIDENCE")
        if any(ref not in context.target_catalog for ref in claim.target_refs):
            errors.add("UNRESOLVED_TARGET_REF")
        if claim.time_scope_ref is not None and claim.time_scope_ref not in policies:
            errors.add("UNRESOLVED_TIME_SCOPE_REF")
        if claim.kind not in FACTLIKE:
            continue
        if claim.time_scope_ref not in policies:
            errors.add("FACT_TIME_SCOPE_UNKNOWN")
        for binding in cited:
            if binding is not None and binding.status != "ok":
                errors.add("FAILED_EVIDENCE_AS_FACT")
            if (
                binding is not None
                and claim.time_scope_ref not in binding.time_scope_refs
            ):
                errors.add("FACT_TIME_SCOPE_NOT_DELIVERED")
        for ref in claim.target_refs:
            if ref not in context.target_catalog or not any(
                b is not None and ref in b.target_refs for b in cited
            ):
                errors.add("CLAIM_TARGET_NOT_OBSERVED")
            policy = policies.get(claim.time_scope_ref)
            if (
                policy is not None
                and ref not in policy.target_refs
                and not policy.all_authorized_targets
            ):
                errors.add("CLAIM_TIME_TARGET_MISMATCH")
    return sorted(errors)


def context_errors(context, views, facts, run_id):
    if context is None:
        return {"MISSING_EVIDENCE_CONTEXT"} if views else set()
    errors = set()
    if context.run_id != run_id:
        errors.add("CONTEXT_RUN_MISMATCH")
    if context.time_policies != facts.time_policies:
        errors.add("TIME_POLICY_MISMATCH")
    view_map = {v.id: v for v in views}
    if len(view_map) != len(views) or set(context.view_bindings) != set(view_map):
        errors.add("VIEW_BINDING_MISMATCH")
    artifacts = {a.id: a for a in facts.artifacts}
    policies = {p.id: p for p in facts.time_policies}
    for ref, target in context.target_catalog.items():
        if ref != target_ref(run_id, target) or not legacy.authorized(
            target, facts.scope
        ):
            errors.add("TARGET_CATALOG_MISMATCH")
    for view_id, binding in context.view_bindings.items():
        view = view_map.get(view_id)
        artifact = artifacts.get(view.artifact_id) if view else None
        if artifact is None or view.view_hash != binding.view_hash:
            errors.add("VIEW_BINDING_MISMATCH")
            continue
        timing_record = facts.timing_records.get(view_id)
        if timing_record is None:
            errors.add("TIMING_RECORD_UNKNOWN")
        elif (
            timing_record.view_hash != view.view_hash
            or timing_record.timing != binding.timing
        ):
            errors.add("TIMING_RECORD_MISMATCH")
        expected = (
            {target_ref(run_id, t) for t in artifact.targets}
            if artifact.status == "ok"
            else set()
        )
        if set(binding.target_refs) != expected or not set(binding.target_refs) <= set(
            context.target_catalog
        ):
            errors.add("VIEW_TARGET_MISMATCH")
        if binding.status != artifact.status:
            errors.add("VIEW_STATUS_MISMATCH")
        for policy_id in binding.time_scope_refs:
            policy = policies.get(policy_id)
            if (
                policy is None
                or artifact.interface not in policy.interfaces
                or any(
                    t.integration_id != policy.integration_id for t in artifact.targets
                )
            ):
                errors.add("TIME_POLICY_NOT_APPLICABLE")
            elif (
                policy.all_authorized_targets
                and policy.scope_revision != facts.scope.revision
            ):
                errors.add("TIME_POLICY_SCOPE_MISMATCH")
        timing = binding.timing
        if facts.evaluation_at and artifact.captured_at > facts.evaluation_at:
            errors.add("FUTURE_OBSERVATION")
        if (
            timing.operation_started_at
            and timing.collection_completed_at
            and not timing.operation_started_at
            <= artifact.captured_at
            <= timing.collection_completed_at
        ):
            errors.add("TIMING_CAPTURE_MISMATCH")
        sequence = [timing.operation_started_at, timing.collection_completed_at]
        if all(sequence) and sequence[0] > sequence[1]:
            errors.add("EVIDENCE_TIME_MISMATCH")
        if (
            timing.source_start_at
            and timing.source_end_at
            and timing.source_start_at > timing.source_end_at
        ):
            errors.add("EVIDENCE_TIME_MISMATCH")
        if (
            timing.source_end_at
            and timing.collection_completed_at
            and timing.source_end_at > timing.collection_completed_at
        ):
            errors.add("FUTURE_OBSERVATION")
        if facts.evaluation_at and any(
            t is not None and t > facts.evaluation_at
            for t in (
                timing.operation_started_at,
                timing.collection_completed_at,
                timing.source_start_at,
                timing.source_end_at,
            )
        ):
            errors.add("FUTURE_OBSERVATION")
    return errors


def temporal_verdict(binding, policy, delivery, evaluation_at, query_window=None):
    """Whole-visible-interval eligibility; never infer text or missing sample age."""
    timing = binding.timing
    required = [
        timing.operation_started_at,
        timing.collection_completed_at,
        timing.source_start_at,
        timing.source_end_at,
        delivery.dispatch_started_at,
        delivery.response_received_at,
        evaluation_at,
    ]
    if not all(required) or timing.source_time_basis == "unknown":
        return "unknown"
    if not (
        timing.operation_started_at
        <= timing.collection_completed_at
        <= delivery.dispatch_started_at
        <= delivery.response_received_at
        <= evaluation_at
    ):
        return "invalid"
    if not (
        timing.source_start_at <= timing.source_end_at <= timing.collection_completed_at
    ):
        return "invalid"
    if query_window is None:
        return "unknown"
    if (
        not query_window.start
        <= timing.source_start_at
        <= timing.source_end_at
        <= query_window.end
    ):
        return "outside_query_window"
    reference = getattr(delivery, policy.reference_rule)
    if policy.mode == "historical_window":
        if policy.window is None:
            return "unknown"
        return (
            "eligible"
            if policy.window.start
            <= timing.source_start_at
            <= timing.source_end_at
            <= policy.window.end
            else "outside_window"
        )
    if policy.max_source_age_seconds is None:
        return "unknown"
    if timing.source_end_at > reference:
        return "invalid"
    # A fresh row cannot make older visible observations look fresh as a group.
    age = (reference - timing.source_start_at).total_seconds()
    return "eligible" if age <= policy.max_source_age_seconds else "stale"


def _context_in_payload(delivery):
    try:
        body = json.loads(delivery.business_projection_content)
        if delivery.business_projection == "envelope-v1":
            candidates = [body.get("context")]
        else:
            candidates = []
            for message in body:
                if message.get("role") != "user" or not isinstance(
                    message.get("content"), str
                ):
                    continue
                try:
                    value = json.loads(message["content"])
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("type") == CONTEXT_TYPE:
                    candidates.append(value)
        return candidates == [delivery.context.model_dump(mode="json")]
    except (ValueError, AttributeError, TypeError):
        return False


def input_provenance_errors(initial):
    """Missing metadata remains representable, never sufficient for strict export."""
    errors = set()
    if initial.unverified_initial_views:
        errors.add("UNVERIFIED_INITIAL_EVIDENCE")
        if any(
            content_hash(item.content) != item.content_sha256
            for item in initial.unverified_initial_views
        ):
            errors.add("INITIAL_AUDIT_HASH_MISMATCH")
    if any(
        value is None
        for value in (
            initial.original_user_content,
            initial.original_user_content_sha256,
            initial.actual_user_content,
            initial.actual_user_content_sha256,
        )
    ):
        errors.add("INITIAL_INPUT_PROVENANCE_UNKNOWN")
    for text, sha in (
        (initial.original_user_content, initial.original_user_content_sha256),
        (initial.actual_user_content, initial.actual_user_content_sha256),
    ):
        if (text is None) != (sha is None) or (
            text is not None and content_hash(text) != sha
        ):
            errors.add("INITIAL_INPUT_HASH_MISMATCH")
    if (
        initial.initial_views or initial.unverified_initial_views
    ) and initial.actual_user_content is None:
        errors.add("ACTUAL_INITIAL_INPUT_UNKNOWN")
    return errors


def check_outcome(scenario, outcome):
    """Strict current seam; legacy projection below reuses structural checks only."""
    facts, report = scenario.trusted, outcome.report
    base_deliveries = [
        legacy.Delivery.model_validate(
            d.model_dump(
                exclude={"context", "dispatch_started_at", "response_received_at"}
            )
        )
        for d in facts.deliveries
    ]
    base_facts = legacy.TrustedFacts.model_validate(
        facts.model_dump(
            exclude={
                "time_policies",
                "evaluation_at",
                "report_capture",
                "deliveries",
                "timing_records",
            }
        )
        | {"deliveries": base_deliveries}
    )
    base_input = legacy.AgentInput.model_validate(
        scenario.agent_input.model_dump(include={"subject", "request", "initial_views"})
    )
    base_scenario = legacy.IncidentScenario(
        schema_version="m0-public-v3",
        scenario_id=scenario.scenario_id,
        versions=scenario.versions,
        agent_input=base_input,
        trusted=base_facts,
    )
    base_outcome = legacy.IncidentOutcome(
        schema_version="m0-public-v3",
        scenario_id=outcome.scenario_id,
        versions=outcome.versions,
        subject=outcome.subject,
        run_id=outcome.run_id,
        report_step_id=outcome.report_step_id or "not_started",
        report_request_id=outcome.report_request_id or "not_started",
        control_generation=outcome.control_generation,
        execution=outcome.execution,
        assessment_status=report.assessment_status if report else "incomplete",
        conclusion=report.conclusion if report else "inconclusive",
        claims=[
            legacy.ScopedClaim(kind=c.kind, text=c.text, evidence_ids=c.evidence_ids)
            for c in report.claims
        ]
        if report
        else [],
        evidence_ids=outcome.evidence_ids,
        gaps=report.gaps if report else outcome.handoff_reasons,
        handoff=outcome.handoff,
        health=outcome.health,
    )
    errors = set(
        legacy.check_outcome(
            base_scenario,
            base_outcome,
            _initial_view_ids={v.id for v in scenario.agent_input.initial_views},
        )
    )
    if not re.fullmatch(
        r"[a-f0-9]{40}|[a-f0-9]{64}", scenario.versions.get("upstream_commit", "")
    ) or not re.fullmatch(
        r"[a-f0-9]{64}", scenario.versions.get("tool_schema_sha256", "")
    ):
        errors.add("EXECUTION_VERSION_UNKNOWN")
    initial = scenario.agent_input
    errors |= input_provenance_errors(initial)
    errors |= context_errors(
        scenario.agent_input.evidence_context,
        scenario.agent_input.initial_views,
        facts,
        facts.current_run,
    )
    last = facts.controls[-1].action if facts.controls else None
    required_state = {"cancel": "cancelled", "correct": "waiting_human"}.get(last)
    if required_state and facts.execution != required_state:
        errors.add("CONTROL_STATE_MISMATCH")
    if required_state and report is not None:
        errors.add("CONTROL_REPORT_NOT_AUTHORIZED")
    for delivery in facts.deliveries:
        if initial.actual_user_content is not None:
            try:
                business = json.loads(delivery.business_projection_content)
                if delivery.business_projection == "envelope-v1":
                    inputs = [business.get("actual_user_content")]
                else:
                    inputs = [
                        m.get("content")
                        for m in business
                        if m.get("role") == "user"
                        and m.get("content") == initial.actual_user_content
                    ]
                if inputs != [initial.actual_user_content]:
                    errors.add("ACTUAL_INITIAL_INPUT_NOT_DELIVERED")
            except (ValueError, TypeError, AttributeError):
                errors.add("ACTUAL_INITIAL_INPUT_NOT_DELIVERED")
        if not _context_in_payload(delivery):
            errors.add("CONTEXT_NOT_DELIVERED")
        errors |= context_errors(
            delivery.context, delivery.views, facts, delivery.run_id
        )
        if (
            delivery.dispatch_started_at
            and delivery.response_received_at
            and delivery.dispatch_started_at > delivery.response_received_at
        ):
            errors.add("DELIVERY_TIME_MISMATCH")
        if (
            facts.evaluation_at
            and delivery.response_received_at
            and delivery.response_received_at > facts.evaluation_at
        ):
            errors.add("DELIVERY_TIME_MISMATCH")
    capture = facts.report_capture
    if (outcome.report_content is None) != (outcome.report_content_sha256 is None) or (
        outcome.report_content is not None
        and content_hash(outcome.report_content) != outcome.report_content_sha256
    ):
        errors.add("REPORT_OUTPUT_BINDING_MISMATCH")
    if report is not None or capture is not None:
        if capture is None or (
            capture.run_id,
            capture.step_id,
            capture.request_id,
            capture.control_generation,
        ) != (
            outcome.run_id,
            outcome.report_step_id,
            outcome.report_request_id,
            outcome.control_generation,
        ):
            errors.add("REPORT_OUTPUT_BINDING_MISMATCH")
        elif (
            capture.content != outcome.report_content
            or capture.content_sha256 != content_hash(capture.content)
            or outcome.report_content_sha256 != capture.content_sha256
        ):
            errors.add("REPORT_OUTPUT_BINDING_MISMATCH")
    matching = [
        d
        for d in facts.deliveries
        if (d.run_id, d.step_id, d.request_id, d.control_generation, d.state)
        == (
            outcome.run_id,
            outcome.report_step_id,
            outcome.report_request_id,
            outcome.control_generation,
            "response_committed",
        )
    ]
    delivery = matching[0] if len(matching) == 1 else None
    if (report is not None or capture is not None) and delivery is None:
        errors.add("REPORT_DELIVERY_MISMATCH")
    if capture is not None and delivery is not None:
        if (
            capture.response_received_at is None
            or delivery.response_received_at is None
        ):
            errors.add("REPORT_TIME_UNKNOWN")
        elif capture.response_received_at != delivery.response_received_at:
            errors.add("REPORT_TIME_MISMATCH")
    if report is None:
        if (
            outcome.execution == "completed"
            or outcome.report_content is not None
            or outcome.report_content_sha256 is not None
            or not outcome.handoff
            or not outcome.handoff_reasons
        ):
            errors.add("MISSING_REPORT_OR_HANDOFF")
        return sorted(errors)
    from scripts.m0_environment.report_contract import parse_report

    try:
        if parse_report(
            outcome.report_content or "", version="m0-report-v2"
        ) != report.model_dump(mode="json"):
            errors.add("REPORT_CONTENT_MISMATCH")
    except ValueError:
        errors.add("REPORT_CONTENT_MISMATCH")
    if delivery is None:
        return sorted(errors)
    if (
        not delivery.dispatch_started_at
        or not delivery.response_received_at
        or not facts.evaluation_at
        or capture is None
        or not capture.response_received_at
    ):
        errors.add("REPORT_TIME_UNKNOWN")
    elif (
        not delivery.dispatch_started_at
        <= delivery.response_received_at
        == capture.response_received_at
        <= facts.evaluation_at
    ):
        errors.add("REPORT_TIME_MISMATCH")
    errors.update(validate_report_context(report, delivery.context))
    policies = {p.id: p for p in facts.time_policies}
    artifacts = {a.id: a for a in facts.artifacts}
    views = {v.id: v for v in delivery.views}
    for claim in report.claims:
        if claim.kind not in FACTLIKE:
            continue
        policy = policies.get(claim.time_scope_ref)
        if policy is None:
            continue
        for ref in claim.evidence_ids:
            binding = delivery.context.view_bindings.get(ref)
            if binding is not None:
                verdict = temporal_verdict(
                    binding,
                    policy,
                    delivery,
                    facts.evaluation_at,
                    artifacts[views[ref].artifact_id].window
                    if ref in views and views[ref].artifact_id in artifacts
                    else None,
                )
                if verdict != "eligible":
                    errors.add("FACT_FRESHNESS_" + verdict.upper())
    return sorted(errors)
