"""L2 report contract and v4/report-v2 parsing.

The contract text is the single source for the model-visible report schema.
Runtime values (this Run's evidence ids, target refs, time-policy ids) stay
out of it: they belong on the evidence-context user message, not in L2, so
two Runs that differ only in window or target set keep the same
``prompt_revision``. Writing rules follow
``docs/design/deepseek-flash-prompt-tool-reference.md`` (the word ``json``
and a complete example are vendor requirements, not formatting).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from opspilot.domain.base import DTO, Text

REPORT_SCHEMA_VERSION = "m0-report-v2"
EVIDENCE_CONTEXT_TYPE = "opspilot-evidence-context-v4"
CLAIM_KINDS = (
    "fact",
    "hypothesis",
    "recommendation",
    "counter_evidence",
    "rejected_hypothesis",
)
_FACTLKE = frozenset({"fact", "counter_evidence", "rejected_hypothesis"})
_DSML = re.compile(r"<[^>\n]*DSML", re.IGNORECASE)

_REPORT_EXAMPLE: dict[str, object] = {
    "schema_version": REPORT_SCHEMA_VERSION,
    "assessment_status": "completed",
    "conclusion": "inconclusive",
    "summary": "Write the evidence-based assessment here.",
    "claims": [
        {
            "kind": "fact",
            "text": "A specific visible observation.",
            "evidence_ids": ["REPLACE_WITH_COMPLETE_VISIBLE_EVIDENCE_ID"],
            "target_refs": ["COPY_ACTUALLY_DELIVERED_TARGET_REF"],
            "time_scope_ref": "COPY_FIXED_TIME_POLICY_REF",
        }
    ],
    "gaps": ["State the specific missing information, if any."],
    "next_steps": ["An advisory next step, without executing it."],
}

# Stable L2 bytes. Instance values are not interpolated here.
REPORT_CONTRACT = (
    "When you end this investigation, including an early answer before the "
    "final request, return only one json object in the following format; "
    "replace the example values with your actual assessment. Do not emit "
    "DSML, tool invocation markup, Markdown fences or requests for more "
    "tools. schema_version must be m0-report-v2. assessment_status is "
    "completed or incomplete; conclusion is supported, partial, or "
    "inconclusive. Incomplete requires inconclusive and nonempty gaps. "
    "Supported requires at least one evidence-backed fact; partial means "
    "useful findings but unresolved conclusions. Each claim has kind, text, "
    "evidence_ids, target_refs and time_scope_ref. kind is fact, hypothesis, "
    "recommendation, counter_evidence, or rejected_hypothesis. Every fact, "
    "counter_evidence and rejected_hypothesis must cite at least one complete "
    "evidence_id actually supplied in this Run, nonempty target_refs from the "
    "delivered evidence context, and a time_scope_ref from that context; never "
    "abbreviate or invent IDs. Permission to query a target is not observed "
    "evidence. Missing policy or source timing cannot support a current fact; "
    "return explicit gaps and inconclusive or incomplete. Use gaps for missing "
    "information and next_steps for advisory human follow-up. Do not invent a "
    "supported cause to fill the format. json example: "
    + json.dumps(_REPORT_EXAMPLE, ensure_ascii=False, separators=(",", ":"))
)

FINAL_REPORT_INSTRUCTION = (
    "Collection is now CLOSED. This is the final report request. No further "
    "tools or fresh queries are permitted. Return only one json object in the "
    "report format already given. Do not emit DSML, tool invocation markup, "
    "Markdown fences or requests for more tools."
)


class ClaimV2(DTO):
    kind: Literal[
        "fact",
        "hypothesis",
        "recommendation",
        "counter_evidence",
        "rejected_hypothesis",
    ]
    text: Text
    evidence_ids: list[Text]
    target_refs: list[Text] = Field(default_factory=list)
    time_scope_ref: Text | None = None

    @model_validator(mode="after")
    def explicit_fact_scope(self) -> ClaimV2:
        if self.kind in _FACTLKE and (
            not self.evidence_ids or not self.target_refs or not self.time_scope_ref
        ):
            raise ValueError("FACT_SCOPE_REQUIRED")
        if len(set(self.target_refs)) != len(self.target_refs):
            raise ValueError("DUPLICATE_TARGET_REF")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("DUPLICATE_EVIDENCE_ID")
        return self


class ReportV2(DTO):
    schema_version: Literal["m0-report-v2"]
    assessment_status: Literal["completed", "incomplete"]
    conclusion: Literal["supported", "partial", "inconclusive"]
    summary: Text
    claims: list[ClaimV2]
    gaps: list[Text]
    next_steps: list[Text]

    @model_validator(mode="after")
    def coherent(self) -> ReportV2:
        if not self.summary.strip():
            raise ValueError("EMPTY_SUMMARY")
        if self.assessment_status == "incomplete" and (
            self.conclusion != "inconclusive" or not self.gaps
        ):
            raise ValueError("INCOMPLETE_REPORT_CONTRACT")
        if self.conclusion == "supported" and not any(
            claim.kind == "fact" and claim.evidence_ids for claim in self.claims
        ):
            raise ValueError("SUPPORTED_REPORT_REQUIRES_FACT")
        return self


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_FIELD")
        result[key] = value
    return result


def parse_report(
    content: str | None, *, finish_reason: str
) -> tuple[ReportV2 | None, str]:
    """Parse a candidate final text into a v2 report.

    Returns ``(report, "")`` on success. On failure ``report`` is None and the
    second value is a fixed reason code. Empty content is a failure, not an
    invented report; that is a vendor-documented DeepSeek failure mode.
    """
    if finish_reason == "length":
        return None, "OUTPUT_LENGTH"
    if finish_reason != "stop":
        return None, "REPORT_INVALID"
    if not isinstance(content, str) or not content.strip():
        return None, "EMPTY_REPORT"
    if _DSML.search(content):
        return None, "REPORT_INVALID"
    try:
        payload = json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        return None, "REPORT_INVALID"
    try:
        report = ReportV2.model_validate(payload)
    except (ValidationError, ValueError):
        return None, "REPORT_INVALID"
    return report, ""


@dataclass(frozen=True)
class DeliveredView:
    """One adopted view the report is allowed to cite."""

    evidence_id: str
    target_ids: frozenset[str]
    status: str
    time_scope_refs: frozenset[str] = frozenset()


# Field allowlists for ``evidence_context_projection`` (redline P3-4). Each
# map is exactly the keys the frozen v4 contract
# (``docs/evidence/m0-real-investigation/IncidentScenario.v4.schema.json``,
# ``$defs.EvidenceContext`` and everything it reaches) declares for that
# position -- not just the subset today's readers below happen to touch --
# so a schema-compliant caller's real, legitimate context is never truncated.
# Every other key, at any depth, is dropped rather than reaching the model or
# the citation checks. A key-name blocklist such as ``password``/``secret``/
# ``token`` only catches names someone thought of in advance; an allowlist
# also catches an unexpected field routed in later (e.g. an operator's
# follow-up text via a future ``append_input`` path).
#
# A key name alone is not enough (bot review finding, PR #29): each value
# is also checked against the shape the schema declares for that field, so
# an allowlisted key whose value is the *wrong shape* -- a nested object
# where the schema expects a scalar, or a list of objects where it expects
# a list of strings, e.g. ``interfaces: [{"token": "..."}]`` -- is dropped
# rather than copied verbatim into the model prompt.
def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_str_or_none(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_int_or_none(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _is_list_of_str(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


# ``type``/``run_id`` need no shape map: by the time evidence_context_projection
# reaches them, its own identity gate has already confirmed they exactly
# equal ``EVIDENCE_CONTEXT_TYPE``/``run_id`` (a mismatch of any shape, not
# just the right shape with the wrong value, is rejected there -- stronger
# than a standalone shape check could be). The three container fields
# (``view_bindings``/``target_catalog``/``time_policies``) are likewise
# never copied by ``_project_fields`` -- they are always rebuilt from
# scratch by the dedicated functions below, or omitted entirely when the
# raw value is not the right container type. Doing it any other way
# (allowlisting the key, then only *conditionally* overwriting it with a
# validated rebuild) would leave a malformed raw value in place whenever
# the overwrite's own type check failed.
# ``target_id`` is not part of the schema's ``ViewBinding``; it is a fallback
# ``delivered_from_context`` below also accepts and existing tests rely on.
# ``timing`` is handled the same way as the container fields above, never
# through this map.
_VIEW_BINDING_FIELDS: dict[str, Callable[[object], bool]] = {
    "view_hash": _is_str,
    "target_refs": _is_list_of_str,
    "time_scope_refs": _is_list_of_str,
    "status": _is_str,
    "target_id": _is_str,
}
_TIMING_FIELDS: dict[str, Callable[[object], bool]] = {
    "operation_started_at": _is_str_or_none,
    "collection_completed_at": _is_str_or_none,
    "source_start_at": _is_str_or_none,
    "source_end_at": _is_str_or_none,
    "source_time_basis": _is_str,
}
# Union of ``KubernetesTarget`` / ``ComposeTarget`` / ``IntegrationTarget``
# (the schema's ``target_catalog`` discriminated union) plus the registry
# ``target_id`` wrapper that ``context_target_catalog`` below also accepts
# and existing tests rely on -- not schema-defined, kept for compatibility.
_TARGET_CATALOG_ENTRY_FIELDS: dict[str, Callable[[object], bool]] = {
    "target_id": _is_str,
    "kind": _is_str,
    "integration_id": _is_str,
    "cluster_uid": _is_str,
    "namespace": _is_str,
    "resource_uid": _is_str,
    "revision": _is_str,
    "deployment_instance": _is_str,
    "service": _is_str,
    "container_id": _is_str,
    "image_digest": _is_str,
    "telemetry_instance": _is_str,
    "mapping_revision": _is_str,
    "config_revision": _is_str,
    "service_identity": _is_str,
    "observed_services": _is_list_of_str,
}
# ``window`` is handled the same way as the container fields above, never
# through this map.
_TIME_POLICY_FIELDS: dict[str, Callable[[object], bool]] = {
    "id": _is_str,
    "revision": _is_str,
    "integration_id": _is_str,
    "interfaces": _is_list_of_str,
    "mode": _is_str,
    "reference_rule": _is_str,
    "max_source_age_seconds": _is_int_or_none,
    "target_refs": _is_list_of_str,
    "all_authorized_targets": _is_bool,
    "scope_revision": _is_str_or_none,
}
_TIME_WINDOW_FIELDS: dict[str, Callable[[object], bool]] = {
    "start": _is_str,
    "end": _is_str,
}


def _project_fields(
    mapping: object, fields: Mapping[str, Callable[[object], bool]]
) -> dict[str, Any]:
    """Allowlist projection that also enforces each field's declared shape.

    A value that fails its field's validator is dropped, not kept
    unvalidated and not coerced -- there is no partial/best-effort
    forwarding of a malformed value.
    """
    if not isinstance(mapping, Mapping):
        return {}
    return {
        key: mapping[key]
        for key, is_valid in fields.items()
        if key in mapping and is_valid(mapping[key])
    }


def _project_view_binding(binding: object) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        return {}
    projected = _project_fields(binding, _VIEW_BINDING_FIELDS)
    timing = binding.get("timing")
    if isinstance(timing, Mapping):
        projected["timing"] = _project_fields(timing, _TIMING_FIELDS)
    return projected


def _project_target_catalog_entry(entry: object) -> dict[str, Any]:
    return _project_fields(entry, _TARGET_CATALOG_ENTRY_FIELDS)


def _project_time_policy(policy: object) -> dict[str, Any]:
    """Project one time policy, or drop it whole when any field is malformed.

    Dropping only the malformed field would *widen* the policy: an
    ``interfaces`` value like ``[{"token": "..."}]`` projected to "no
    interfaces" and ``eligible_time_policies`` then applied no interface
    restriction at all (bot review finding, PR #29). A policy whose author
    got a field wrong authorizes nothing. (Whether every v4-required field
    must also be *present* is the open F5 decision and is not settled here.)
    """
    if not isinstance(policy, Mapping):
        return {}
    for key, is_valid in _TIME_POLICY_FIELDS.items():
        if key in policy and not is_valid(policy[key]):
            return {}
    projected = _project_fields(policy, _TIME_POLICY_FIELDS)
    window = policy.get("window")
    if "window" in policy:
        if not isinstance(window, Mapping) or any(
            key in window and not is_valid(window[key])
            for key, is_valid in _TIME_WINDOW_FIELDS.items()
        ):
            return {}
        projected["window"] = _project_fields(window, _TIME_WINDOW_FIELDS)
    return projected


def evidence_context_projection(
    context: object, *, run_id: str
) -> dict[str, Any] | None:
    """Field- and shape-allowlist projection of a v4 evidence context.

    Every key the readers in this module (and the loop's prompt message)
    actually consume is listed above, together with the shape the schema
    declares for it; anything else -- an unlisted key, or a listed key
    whose value is the wrong shape -- is dropped, including inside
    ``view_bindings``/``target_catalog``/``time_policies`` entries. The
    loop calls this once, at context-assembly time, and uses only the
    projected result -- for the model prompt and for every citation check
    -- so neither an unexpected nested key nor a malformed value under an
    allowlisted one can reach either (redline P3-4).

    A context whose ``type``/``run_id`` do not match this Run's own
    identity is discarded wholesale, before any projection: ``82218ae``
    only added this check inside ``delivered_from_context()`` (pre-supplied
    evidence bindings), but ``context_target_catalog()``/
    ``eligible_time_policies()`` -- consulted here for evidence the tool
    executor freshly collects during *this* Run, not just retained
    bindings -- read the same caller-supplied context regardless of whose
    Run it actually belonged to (bot review finding, PR #29). Doing the
    check once, here, protects every consumer that reads the projected
    context the loop reassigns onto ``request.evidence_context`` up front,
    the same way the field allowlist above does.
    """
    if not isinstance(context, Mapping):
        return None
    if context.get("type") != EVIDENCE_CONTEXT_TYPE or context.get("run_id") != run_id:
        return None
    # Both are now known to be exactly these two strings -- the identity
    # gate above already confirmed it -- so they are set directly rather
    # than through a shape map that could never reject anything here.
    projected: dict[str, Any] = {"type": EVIDENCE_CONTEXT_TYPE, "run_id": run_id}
    bindings = context.get("view_bindings")
    if isinstance(bindings, Mapping):
        projected["view_bindings"] = {
            eid: _project_view_binding(binding)
            for eid, binding in bindings.items()
            if isinstance(eid, str)
        }
    catalog = context.get("target_catalog")
    if isinstance(catalog, Mapping):
        projected["target_catalog"] = {
            key: _project_target_catalog_entry(entry)
            for key, entry in catalog.items()
            if isinstance(key, str)
        }
    policies = context.get("time_policies")
    if isinstance(policies, list):
        projected["time_policies"] = [
            entry
            for entry in (_project_time_policy(policy) for policy in policies)
            if entry
        ]
    return projected


def context_target_catalog(
    context: object, *, authorized_targets: frozenset[str] = frozenset()
) -> dict[str, str | None] | None:
    """Opaque v4 target_ref -> optional registry target_id, or ``None``
    when no catalog was supplied at all.

    Catalog values may be a registry ``target_id`` wrapper, or a canonical
    v4 Target object with no ``target_id``. A unique unmapped catalog key
    binds to a unique authorized target; otherwise the mapping stays None.

    ``None`` (catalog absent or malformed) and ``{}`` (catalog genuinely
    present but empty) are kept distinct: a caller gating citation
    validation on "was an opaque catalog supplied at all" must fail closed
    for a genuinely empty catalog rather than silently falling back to
    trusting raw registry ids the same as no catalog (bot review finding,
    PR #29).
    """
    if not isinstance(context, Mapping):
        return None
    catalog = context.get("target_catalog")
    if not isinstance(catalog, Mapping):
        return None
    result: dict[str, str | None] = {}
    for key, entry in catalog.items():
        if not isinstance(key, str) or not key:
            continue
        registry = None
        if isinstance(entry, Mapping) and isinstance(entry.get("target_id"), str):
            registry = str(entry["target_id"]) or None
        result[key] = registry
    unmapped = [key for key, mapped in result.items() if mapped is None]
    if len(unmapped) == 1 and len(authorized_targets) == 1:
        result[unmapped[0]] = next(iter(authorized_targets))
    return result


def _aware(text: object) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(timezone.utc)


def eligible_time_policies(
    policies: object,
    *,
    source: object,
    tool: object,
    target_ids: frozenset[str],
    window: object,
    freshness_seconds: object,
    source_start_at: object = None,
    source_end_at: object = None,
    dispatch_started_at: object = None,
    response_received_at: object = None,
) -> frozenset[str]:
    """Policies this view is allowed to wear. Missing facts fail closed.

    ``source_start_at``/``source_end_at`` are the tool executor's verified
    source-data interval (PR #20, ``opspilot.tools.executor``); both
    ``None`` means "coverage unknown" -- a legitimate, common state for a
    view that predates this field or whose adapter simply does not report
    it -- and eligibility falls back to the checks below exactly as before
    this field existed. Present but malformed (only one end, unparseable,
    naive, or inverted) is not "unknown": the view's own interval claim is
    then internally inconsistent, so no policy in the list is eligible --
    that part alone is independent of any reference instant.

    ``dispatch_started_at``/``response_received_at`` are the two delivery
    timestamps the v4 contract's ``TimePolicy.reference_rule`` chooses
    between (this tool call's own dispatch and response-received instants,
    ``opspilot.tools.executor``'s ``operation.started_at``/``observed_at``).
    Each policy declares which one it trusts, so the reference is resolved
    *per policy*, not once for the whole view: a policy with no recognized
    ``reference_rule`` is rejected the same way a missing fact would be
    (bot review finding, PR #29 -- the previous single global reference
    could mark a source interval stale after a slow response, or accept it
    as not-yet-future-dated, when the policy asked to be judged against the
    dispatch instant instead). With the interval present, a reference after
    ``source_end_at`` (a future-dated claim relative to the instant *that
    policy* trusts) is rejected for that policy only; dispatch never
    happens after the response, so an interval genuinely in the future of
    the whole view is still rejected under every policy regardless of which
    instant it names.

    When the interval is present and consistent, it must additionally fall
    within the query window (``window``) and, per policy, within the
    policy's own bound (``historical_window``) or its max age measured from
    the interval's *older* end (``current``) -- closing the gap where a
    view spanning old and new samples passed on the newest sample's
    ``freshness_seconds`` alone (bot review finding, PR #29; PR #20's
    ``srcrange.md`` for the exact semantics reused here).
    """
    if not isinstance(policies, list):
        return frozenset()
    view_start = view_end = None
    if isinstance(window, Mapping):
        view_start, view_end = _aware(window.get("start")), _aware(window.get("end"))
    has_interval_claim = source_start_at is not None or source_end_at is not None
    verified_interval: tuple[datetime, datetime] | None = None
    if has_interval_claim:
        parsed_start = _aware(source_start_at)
        parsed_end = _aware(source_end_at)
        if parsed_start is None or parsed_end is None or parsed_start > parsed_end:
            return frozenset()
        verified_interval = (parsed_start, parsed_end)
    dispatch_reference = _aware(dispatch_started_at)
    response_reference = _aware(response_received_at)
    eligible: set[str] = set()
    for policy in policies:
        if not isinstance(policy, Mapping):
            continue
        ident = policy.get("id")
        mode = policy.get("mode")
        if not isinstance(ident, str) or not ident:
            continue
        if mode not in {"historical_window", "current"}:
            continue
        source_interval: tuple[datetime, datetime] | None = None
        reference: datetime | None = None
        if verified_interval is not None:
            interval_start, interval_end = verified_interval
            reference_rule = policy.get("reference_rule")
            if reference_rule == "dispatch_started_at":
                reference = dispatch_reference
            elif reference_rule == "response_received_at":
                reference = response_reference
            else:
                continue
            if reference is None or interval_end > reference:
                continue
            source_interval = (interval_start, interval_end)
        interfaces = policy.get("interfaces")
        if isinstance(interfaces, list) and interfaces:
            allowed = {item for item in interfaces if isinstance(item, str)}
            if source not in allowed and tool not in allowed:
                continue
        if policy.get("all_authorized_targets") is not True:
            # An explicit, intersecting target ref is required here. The old
            # ``named and named.isdisjoint(...)`` guard was vacuously false
            # for an empty/absent ``target_refs`` -- meaning a policy scoped
            # to no targets was silently attached to every target instead
            # (bot review finding, PR #29).
            refs = policy.get("target_refs")
            named = (
                {item for item in refs if isinstance(item, str) and item}
                if isinstance(refs, list)
                else set()
            )
            if named.isdisjoint(target_ids):
                continue
        if mode == "historical_window":
            bounds = policy.get("window")
            if (
                not isinstance(bounds, Mapping)
                or view_start is None
                or view_end is None
            ):
                continue
            policy_start, policy_end = (
                _aware(bounds.get("start")),
                _aware(bounds.get("end")),
            )
            if (
                policy_start is None
                or policy_end is None
                or not (policy_start <= view_start <= view_end <= policy_end)
            ):
                continue
            if source_interval is not None:
                interval_start, interval_end = source_interval
                if not (policy_start <= interval_start <= interval_end <= policy_end):
                    continue
        else:
            max_age = policy.get("max_source_age_seconds")
            if type(max_age) is not int or max_age <= 0:
                continue
            if not isinstance(freshness_seconds, (int, float)):
                continue
            # A negative age can only mean a future-dated ``data_as_of``
            # (bot review finding, PR #29): ``> max_age`` alone let it pass
            # as though it were the freshest possible reading.
            if not (0 <= float(freshness_seconds) <= max_age):
                continue
            if source_interval is not None:
                interval_start, interval_end = source_interval
                if (
                    view_start is not None
                    and view_end is not None
                    and not (view_start <= interval_start and interval_end <= view_end)
                ):
                    continue
                assert reference is not None  # set together with source_interval
                if (reference - interval_start).total_seconds() > max_age:
                    continue
        eligible.add(ident)
    return frozenset(eligible)


def context_time_policy_ids(context: object) -> tuple[str, ...]:
    """Ids from the caller-supplied v4 evidence context, if it has any."""
    if not isinstance(context, Mapping):
        return ()
    policies = context.get("time_policies")
    if not isinstance(policies, list):
        return ()
    ids: list[str] = []
    for item in policies:
        if isinstance(item, Mapping):
            ident = item.get("id")
            if isinstance(ident, str) and ident:
                ids.append(ident)
    return tuple(ids)


def unsupported_citations(
    report: ReportV2,
    *,
    views: Sequence[DeliveredView],
    authorized_targets: frozenset[str],
    time_policy_ids: Sequence[str],
    target_catalog: Mapping[str, str | None] | None = None,
) -> bool:
    """True when a claim fails the v4 evidence/target/time bind.

    Fact-like claims must cite delivered ok views, targets those views
    actually observed, and the time policy each cited view was delivered
    under. When a v4 ``target_catalog`` is present, claim ``target_refs``
    are opaque catalog keys, not registry target ids.
    """
    by_id = {view.evidence_id: view for view in views}
    policies = set(time_policy_ids)
    for claim in report.claims:
        if any(eid not in by_id for eid in claim.evidence_ids):
            return True
        if target_catalog is not None:
            if any(ref not in target_catalog for ref in claim.target_refs):
                return True
            if any(
                target_catalog[ref] is not None
                and target_catalog[ref] not in authorized_targets
                for ref in claim.target_refs
            ):
                return True
        elif any(ref not in authorized_targets for ref in claim.target_refs):
            return True
        if claim.time_scope_ref is not None and claim.time_scope_ref not in policies:
            return True
        if claim.kind not in _FACTLKE:
            continue
        if claim.time_scope_ref not in policies:
            return True
        cited = [by_id[eid] for eid in claim.evidence_ids]
        if any(view.status != "ok" for view in cited):
            return True
        if any(claim.time_scope_ref not in view.time_scope_refs for view in cited):
            return True
        observed: set[str] = set()
        for view in cited:
            observed.update(view.target_ids)
        if any(ref not in observed for ref in claim.target_refs):
            return True
    return False


def delivered_from_context(context: object, *, run_id: str) -> list[DeliveredView]:
    """Seed trusted views from a v4 evidence context's view_bindings.

    The context is caller-supplied and not yet authenticated to this Run.
    Without binding it to the Run's own identity, a context copied from
    another Run -- or a fabricated mapping with no run identity at all --
    would seed citations as if this Run had produced them, letting a report
    claim ``supported`` from provenance that was never bound to this Run
    (bot review finding, PR #29). A mismatch or missing identity discards
    the whole context rather than any single binding: an unbound context is
    not partially trustworthy.
    """
    if not isinstance(context, Mapping):
        return []
    if context.get("type") != EVIDENCE_CONTEXT_TYPE or context.get("run_id") != run_id:
        return []
    bindings = context.get("view_bindings")
    if not isinstance(bindings, Mapping):
        return []
    delivered: list[DeliveredView] = []
    for eid, binding in bindings.items():
        if not isinstance(eid, str) or not eid or not isinstance(binding, Mapping):
            continue
        status = binding.get("status")
        if status != "ok":
            continue
        refs = binding.get("target_refs")
        if isinstance(refs, list):
            targets = frozenset(item for item in refs if isinstance(item, str) and item)
        elif isinstance(binding.get("target_id"), str) and binding["target_id"]:
            targets = frozenset({str(binding["target_id"])})
        else:
            targets = frozenset()
        scopes = binding.get("time_scope_refs")
        if isinstance(scopes, list):
            time_refs = frozenset(
                item for item in scopes if isinstance(item, str) and item
            )
        else:
            time_refs = frozenset()
        delivered.append(
            DeliveredView(
                evidence_id=eid,
                target_ids=targets,
                status="ok",
                time_scope_refs=time_refs,
            )
        )
    return delivered
