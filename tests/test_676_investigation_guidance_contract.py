"""Independent contract tests for issue #676 investigation guidance.

These tests observe only the public instruction, profile and registration
surfaces.  The implementation is being developed in another worktree, so the
main checkout is expected to fail the new prompt and description assertions
until that work lands.  Existing contract tests remain unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from opspilot.instructions import discipline as d
from opspilot.investigation.context import context_policy_versions
from opspilot.investigation.loop import investigation_versions
from opspilot.investigation.reports import REPORT_CONTRACT, REPORT_SCHEMA_VERSION
from opspilot.tools import kubernetes_core, kubernetes_tabular
from opspilot.tools.profiles import PROFILE_ENV, select_profile

# Values on origin/main before the #676 release.  The contract requires
# inequality for the changed prompt/tool faces and equality for the frozen
# base tool face; it deliberately does not guess the new hashes.
BASE_PROMPT_REVISION = "prompt-replay-candidate-a6b4dfd02030"
EXT_PROMPT_REVISION = "prompt-replay-candidate-k8s-d387f77762f5"
BASE_TOOL_SCHEMA_REVISION = "otel-demo-a3a95a0f888e"
EXT_TOOL_SCHEMA_REVISION = "otel-demo-ext-d467c4b436b0"
CONTEXT_POLICY_REVISION = "ctx-ctx-policy-v1-a30e65ea52f4"

# D6-D9: each tuple describes ONE prose sentence, never a bag of words
# collected across the prompt or from the JSON report example.  Exact
# sentence counts remain the implementer's existing census test's concern.
SEMANTIC_RULES = {
    "summary_fact_and_hypothesis_wording": (
        "report",
        (
            r"\bsummary\b",
            r"\b(?:only|solely)\b",
            r"\bfacts?\b",
            r"\bclaims?\b",
            r"\b(?:support\w*|backed|grounded)\b",
            r"\bhypothes(?:is|es)\b",
            r"\b(?:as\s+hypothes(?:is|es)|hypothetical\w*|word\w*|phras\w*|label\w*)\b",
        ),
    ),
    "hypothesis_cause_requires_partial": (
        "report",
        (
            r"\b(?:if|when|whenever)\b",
            r"\bclaims?\b",
            r"\b(?:causes?|root[_ -]cause)\b",
            r"\bhypothes(?:is|es)\b",
            r"\bconclusion\b",
            r"\bpartial\b",
        ),
    ),
    "configuration_cause_requires_matching_failure_path": (
        "kubernetes_guidance",
        (
            r"\b(?:settings?|configurations?|configmaps?|switch(?:es)?|toggles?)\b",
            r"\broot[_ -]cause\b",
            r"\b(?:only|solely)\b",
            r"\b(?:declar\w*|specified|configured|stated)\b",
            r"\b(?:faults?|failures?|errors?|delays?)\b",
            r"\b(?:observed|measured|visible)\b",
            r"\bpaths?\b",
            r"\b(?:match\w*|align\w*|correspond\w*|consistent|agree\w*)\b",
            r"\b(?:otherwise|else|if\s+not)\b",
            r"\bhypothes(?:is|es)\b",
        ),
    ),
    "event_only_partial_requires_completed_investigation": (
        "kubernetes_guidance",
        (
            r"\binvestigations?\b",
            r"\b(?:complete|completed|finished|done|ended)\b",
            r"\b(?:only|solely)\b",
            r"\bevents?\b",
            r"\b(?:support\w*|backed|evidence)\b",
            r"\bpartial\b",
        ),
    ),
}

# Hand-written contract-equivalent prose for testing the assertions
# themselves on main, where the four new product sentences are still absent.
SEMANTIC_EXAMPLES = {
    "summary_fact_and_hypothesis_wording": (
        "In the summary, state as facts only what fact claims support, "
        "and phrase hypotheses as hypotheses."
    ),
    "hypothesis_cause_requires_partial": (
        "If the claim identifying the object as the cause is a hypothesis, "
        "use conclusion partial rather than supported."
    ),
    "configuration_cause_requires_matching_failure_path": (
        "Treat a setting as root cause only when its declared fault matches "
        "the observed failure path; otherwise keep it as a hypothesis."
    ),
    "event_only_partial_requires_completed_investigation": (
        "For a completed investigation with only event evidence supporting "
        "the cause, use conclusion partial."
    ),
}

SEMANTIC_WEAKENED_EXAMPLES = {
    "summary_fact_and_hypothesis_wording": (
        "In the summary, state as facts only what fact claims support."
    ),
    "hypothesis_cause_requires_partial": (
        "If the claim identifying the object as the cause is a hypothesis, "
        "use conclusion supported."
    ),
    "configuration_cause_requires_matching_failure_path": (
        "Treat a setting as root cause; otherwise keep it as a hypothesis."
    ),
    "event_only_partial_requires_completed_investigation": (
        "For an investigation with only event evidence supporting "
        "the cause, use conclusion partial."
    ),
}


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def _segments(variant: str) -> dict[str, d.Segment]:
    return {segment.key: segment for segment in d.VARIANTS[variant]}


def _guidance_text() -> str:
    return _segments("replay-candidate-k8s")["kubernetes_guidance"].text


def _prose_without_examples(text: str) -> str:
    """Mask examples without moving offsets used for sentence deletion.

    JSONDecoder handles nested objects/arrays and quoted punctuation; a
    non-greedy brace regex would leave nested claims from the report example.
    Backtick examples (inline or fenced) cannot supply normative prose either.
    """
    prose = re.sub(
        r"```.*?```|`[^`]*`", lambda match: " " * len(match[0]), text, flags=re.S
    )
    decoder = json.JSONDecoder()
    masked_until = 0
    for match in re.finditer(r"[\[{]", prose):
        start = match.start()
        if start < masked_until:
            continue
        try:
            _, length = decoder.raw_decode(prose[start:])
        except ValueError:
            continue
        masked_until = start + length
        prose = prose[:start] + " " * length + prose[masked_until:]
    return prose


def _require_sentence(text: str, patterns: tuple[str, ...]) -> tuple[int, int]:
    """Require every pattern in a single sentence, and return its raw span."""
    prose = _prose_without_examples(text)
    start = 0
    for boundary in re.finditer(r"(?<=[.!?])\s+|\Z", prose):
        end = boundary.start()
        sentence = prose[start:end].strip()
        if sentence and all(re.search(pattern, sentence, re.I) for pattern in patterns):
            return start, end
        start = boundary.end()
    raise AssertionError(f"no prose sentence satisfies {patterns!r}")


def _require_semantic_sentence(text: str, rule: str) -> tuple[int, int]:
    return _require_sentence(text, SEMANTIC_RULES[rule][1])


def _require_deletion_breaks_semantic_rule(text: str, rule: str) -> None:
    start, end = _require_semantic_sentence(text, rule)
    mutated = text[:start] + text[end:]
    assert mutated != text
    with pytest.raises(AssertionError, match="no prose sentence"):
        _require_semantic_sentence(mutated, rule)


def _rendered_section(section: str) -> str:
    variant = "replay-candidate" if section == "report" else "replay-candidate-k8s"
    rendered = d.render(variant, model_requests=5, report_contract=REPORT_CONTRACT)
    text = REPORT_CONTRACT if section == "report" else _guidance_text()
    start = rendered.index(text)
    return rendered[start : start + len(text)]


def _schema_text(tool_name: str) -> str:
    face = select_profile({PROFILE_ENV: "otel-demo-ext"}).face(_Clock())
    schema = next(
        item["function"]
        for item in face.tool_schemas
        if isinstance(item, Mapping)
        and isinstance(item.get("function"), Mapping)
        and item["function"].get("name") == tool_name
    )
    return " ".join(
        [schema["description"]]
        + [
            spec.get("description", "")
            for spec in schema["parameters"]["properties"].values()
        ]
    )


def _registered_text(registration: Any) -> str:
    description = registration.description
    prose = [
        getattr(description, field.name)
        for field in fields(description)
        if isinstance(getattr(description, field.name), str)
    ]
    prose.extend(
        spec.description
        for spec in registration.parameters.values()
        if isinstance(spec.description, str)
    )
    return " ".join(prose)


def test_revision_contract_changes_only_the_declared_faces():
    base = select_profile({PROFILE_ENV: "otel-demo"}).versions()
    extension = select_profile({PROFILE_ENV: "otel-demo-ext"}).versions()

    assert investigation_versions("replay-candidate")["prompt_revision"] != (
        BASE_PROMPT_REVISION
    )
    assert base["prompt_revision"] != BASE_PROMPT_REVISION
    assert extension["prompt_revision"] != EXT_PROMPT_REVISION
    assert extension["tool_schema_revision"] != EXT_TOOL_SCHEMA_REVISION
    assert base["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION

    assert base["context_policy_revision"] == CONTEXT_POLICY_REVISION
    assert extension["context_policy_revision"] == CONTEXT_POLICY_REVISION
    assert context_policy_versions() == {
        "context_policy_revision": CONTEXT_POLICY_REVISION
    }
    assert REPORT_SCHEMA_VERSION == "m0-report-v3"


def test_kubernetes_guidance_keeps_a7_guards_and_has_no_environment_terms():
    text = _guidance_text()
    face = select_profile({PROFILE_ENV: "otel-demo-ext"}).face(_Clock())

    for forbidden in ("kubectl", "describe"):
        assert (
            re.search(rf"(?<![a-z0-9_]){re.escape(forbidden)}(?![a-z0-9_])", text, re.I)
            is None
        )

    tool_names = {
        str(item["function"]["name"])
        for item in face.tool_schemas
        if isinstance(item, Mapping)
        and isinstance(item.get("function"), Mapping)
        and isinstance(item["function"].get("name"), str)
    }
    assert [
        name
        for name in tool_names
        if re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text, re.I)
    ] == []

    parameter_names: set[str] = set()
    for item in face.tool_schemas:
        function = item.get("function") if isinstance(item, Mapping) else None
        parameters = (
            function.get("parameters") if isinstance(function, Mapping) else None
        )
        properties = (
            parameters.get("properties") if isinstance(parameters, Mapping) else None
        )
        if isinstance(properties, Mapping):
            parameter_names.update(str(name) for name in properties)
    assert [
        name
        for name in parameter_names
        if "_" in name
        and re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text, re.I)
    ] == []

    environment_terms = (
        "chaos",
        "chaos-mesh",
        "flagd",
        "otel-demo",
        "NetworkChaos",
        "PodNetworkChaos",
        "data-recorders",
    )
    assert [
        term for term in environment_terms if re.search(re.escape(term), text, re.I)
    ] == []


@pytest.mark.parametrize("rule", SEMANTIC_RULES)
def test_d6_d9_required_semantics_are_present_in_one_prose_sentence(rule: str):
    section, _ = SEMANTIC_RULES[rule]
    _require_semantic_sentence(_rendered_section(section), rule)


@pytest.mark.parametrize("rule", SEMANTIC_RULES)
def test_d6_d9_deleting_the_required_sentence_breaks_the_same_assertion(rule: str):
    section, _ = SEMANTIC_RULES[rule]
    _require_deletion_breaks_semantic_rule(_rendered_section(section), rule)


@pytest.mark.parametrize("rule", SEMANTIC_RULES)
def test_semantic_matcher_self_check_rejects_examples_cross_sentence_and_deletion(
    rule: str,
):
    """Exercise mutation checks even before main contains the new prose.

    These are assertion self-checks, not a substitute for the red product
    checks above: each supplied sentence is contract-equivalent test data.
    """
    sentence = SEMANTIC_EXAMPLES[rule]
    # Keep all other rules when deleting this one, especially the
    # hypothesis -> partial rule when deleting the summary wording rule.
    rendered = d.render(
        "replay-candidate",
        model_requests=5,
        report_contract=" ".join(SEMANTIC_EXAMPLES.values()),
    )
    _require_semantic_sentence(rendered, rule)
    _require_deletion_breaks_semantic_rule(rendered, rule)

    # In particular, summary/claims/kind:fact in nested JSON is not a rule.
    examples = (
        json.dumps({"summary": sentence, "claims": [{"kind": "fact"}]}),
        json.dumps([{"instruction": sentence}]),
        f"`{sentence}`",
        f"```text\n{sentence}\n```",
        ". ".join(sentence.rstrip(".").split()) + ".",
    )
    for example in examples:
        with pytest.raises(AssertionError, match="no prose sentence"):
            _require_semantic_sentence(example, rule)

    with pytest.raises(AssertionError, match="no prose sentence"):
        _require_semantic_sentence(SEMANTIC_WEAKENED_EXAMPLES[rule], rule)


@pytest.mark.parametrize(
    "patterns",
    [
        (
            r"\bevents?\b",
            r"\b(?:whole|entire)\s+cluster\b|\ball\s+namespaces\b|\bcluster[- ]wide\b",
        ),
        (
            r"\bevents?\b",
            r"\btype\b",
            r"\b(?:do\s+not|never|without|not)\b",
            r"\b(?:filter|narrow|restrict|select)\w*\b",
        ),
        (
            r"\bevents?\b",
            r"\bkind\b",
            r"\bnamespaces?\b",
            r"\breasons?\b",
            r"\b(?:group|aggregat|summari[sz]|count)\w*\b",
        ),
        (
            r"\b(?:restart|rollout)\b",
            r"\btemplate\b",
            r"\b(?:unchanged|unmodified|same|not\s+changed)\b",
            r"\bworkloads?\b",
            r"\bconfigmaps?\b",
            r"\b(?:read|inspect|check)\w*\b",
        ),
    ],
    ids=["cluster_events", "no_type_filter", "group_events", "restart_configuration"],
)
def test_kubernetes_method_points_cooccur_in_one_sentence(patterns: tuple[str, ...]):
    _require_sentence(_rendered_section("kubernetes_guidance"), patterns)


def test_kubernetes_guidance_names_the_required_network_resources():
    for resource in ("networkpolicies", "services", "endpoints", "resourcequotas"):
        _require_sentence(
            _rendered_section("kubernetes_guidance"), (rf"\b{resource}\b",)
        )


def test_preexisting_stable_errors_are_background_in_the_shared_method():
    method = _segments("replay-candidate")["investigation_method"].text
    rendered = d.render(
        "replay-candidate", model_requests=5, report_contract=REPORT_CONTRACT
    )
    assert method in rendered
    _require_sentence(
        method,
        (
            r"\bbackground\b",
            r"\b(?:stable|steady|unchanged)\b",
            r"\b(?:before|prior|earlier)\b",
            r"\berrors?\b",
        ),
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        kubernetes_core.JQ_QUERY_TOOL,
        kubernetes_core.COUNT_TOOL,
        kubernetes_tabular.TABULAR_TOOL,
    ],
)
def test_kubernetes_registrations_describe_event_kind_projection_and_exception(
    tool_name: str,
):
    if tool_name == kubernetes_tabular.TABULAR_TOOL:
        registration = kubernetes_tabular.tabular_registrations(
            max_window_seconds=86400
        )[0]
    else:
        registration = next(
            item
            for item in kubernetes_core.kubernetes_registrations(
                max_window_seconds=86400
            )
            if item.name == tool_name
        )

    registration_text = _registered_text(registration)
    schema_text = _schema_text(tool_name)
    combined = f"{registration_text} {schema_text}"

    assert "involvedObject.kind" in combined
    assert re.search(r"\bevents?\b", combined, re.I)
    _require_sentence(
        combined,
        (
            r"\bevents?\b",
            r"\b(?:whole|entire)\s+cluster\b|\ball\s+namespaces\b|\bcluster[- ]wide\b",
            r"\b(?:group|aggregat|summari[sz]|count)\w*\b",
        ),
    )
