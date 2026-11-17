"""Independent contract tests for issue #676 investigation guidance.

These tests observe only the public instruction, profile and registration
surfaces.  The implementation is being developed in another worktree, so the
main checkout is expected to fail the new prompt and description assertions
until that work lands.  Existing contract tests remain unchanged.
"""

from __future__ import annotations

import hashlib
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
# Public INVESTIGATION_METHOD text read from main 83b3b968, UTF-8, including
# its leading space. D13 moves the background rule exclusively to ext.
MAIN_INVESTIGATION_METHOD_SHA256 = (
    "222e5fa74a8acd694fa369ef98e9b9e083c4e1f13036ed21952591ba26e1fcf3"
)

# D6-D9 as revised by D12-D14: each tuple describes ONE prose sentence,
# never a bag of words
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
    "supported_cause_requires_fact_not_hypothesis_alone": (
        "report",
        (
            r"\bsupported\b",
            r"\b(?:requir\w*|needs?|must)\b",
            r"\bfact\b",
            r"\bclaims?\b",
            r"\b(?:causes?|root[_ -]cause)\b",
            r"\b(?:nam\w*|identif\w*|point\w*)\b",
            r"\bobject\b",
            r"\bhypothes(?:is|es)\b",
            r"\b(?:not\s+(?:enough|sufficient)|insufficient|does\s+not\s+suffice)\b",
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
    "alert_is_entry_and_preexisting_stable_errors_are_background": (
        "kubernetes_guidance",
        (
            r"\balerts?\b",
            r"\b(?:entry|starting|start\w*|leads?|clues?)\b",
            r"\bscope\b",
            r"\b(?:stable|steady|unchanged)\b",
            r"\b(?:before|prior|earlier)\b",
            r"\b(?:deviat\w*|depart\w*|chang\w*|onset|began|started)\b",
            r"\berrors?\b",
            r"\bbackground\b",
        ),
    ),
    "non_ok_views_cannot_support_absence_claims": (
        "report",
        (
            r"\bno_data\b",
            r"\b(?:error|timeout|denied)\b",
            r"\b(?:never|cannot|does\s+not|do\s+not)\b",
            r"\bsupport\w*\b",
            r"\bfact\b",
            r"\bcounter_evidence\b",
            r"\brejected_hypothesis\b",
            r"\b(?:absent|healthy|free\s+of\s+errors)\b",
            r"\bempty\s+results?\b",
            r"\b(?:only|merely)\b",
            r"\bquer(?:y|ies)\b",
            r"\b(?:nothing|no\s+matches)\b",
        ),
    ),
    "empty_query_requires_gaps_or_direct_ok_evidence": (
        "report",
        (
            r"\bquer(?:y|ies)\b",
            r"\b(?:nothing|empty|no\s+matches)\b",
            r"\bgaps\b",
            r"\bor\b",
            r"\bcit\w*\b",
            r"\bok\b",
            r"\bviews?\b",
            r"\bdirect\w*\b",
        ),
    ),
}

# Hand-written contract-equivalent prose for testing the assertions
# themselves on main, where the new product sentences are still absent.
SEMANTIC_EXAMPLES = {
    "summary_fact_and_hypothesis_wording": (
        "In the summary, state as facts only what fact claims support, "
        "and phrase hypotheses as hypotheses."
    ),
    "supported_cause_requires_fact_not_hypothesis_alone": (
        "Supported requires a fact claim that names that object as the cause; "
        "a hypothesis alone is not enough."
    ),
    "configuration_cause_requires_matching_failure_path": (
        "Treat a setting as root cause only when its declared fault matches "
        "the observed failure path; otherwise keep it as a hypothesis."
    ),
    "event_only_partial_requires_completed_investigation": (
        "For a completed investigation with only event evidence supporting "
        "the cause, use conclusion partial."
    ),
    "alert_is_entry_and_preexisting_stable_errors_are_background": (
        "An alert is the entry point, not the scope: establish when the signal "
        "began to deviate, and treat stable errors present before that deviation "
        "as background."
    ),
    "non_ok_views_cannot_support_absence_claims": (
        "A view whose status is no_data, error, timeout or denied never supports "
        "a fact, counter_evidence or rejected_hypothesis, including a statement "
        "that something is absent, healthy or free of errors: an empty result "
        "shows only that this query matched nothing."
    ),
    "empty_query_requires_gaps_or_direct_ok_evidence": (
        "Write what the query was and that it returned nothing in gaps, "
        "or cite an ok view that shows it directly."
    ),
}

SEMANTIC_WEAKENED_EXAMPLES = {
    "summary_fact_and_hypothesis_wording": (
        "In the summary, state as facts only what fact claims support."
    ),
    "supported_cause_requires_fact_not_hypothesis_alone": (
        "Supported requires a claim naming that object as the cause; "
        "a hypothesis alone is enough."
    ),
    "configuration_cause_requires_matching_failure_path": (
        "Treat a setting as root cause; otherwise keep it as a hypothesis."
    ),
    "event_only_partial_requires_completed_investigation": (
        "For an investigation with only event evidence supporting "
        "the cause, use conclusion partial."
    ),
    "alert_is_entry_and_preexisting_stable_errors_are_background": (
        "An alert is the entry point, not the scope: stable errors are affected."
    ),
    "non_ok_views_cannot_support_absence_claims": (
        "A no_data, error, timeout or denied view never supports a fact."
    ),
    "empty_query_requires_gaps_or_direct_ok_evidence": (
        "Write what the query was and that it returned nothing in gaps."
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


def _prose_sentences(text: str) -> list[tuple[int, int, str]]:
    prose = _prose_without_examples(text)
    sentences = []
    start = 0
    for boundary in re.finditer(r"(?<=[.!?])\s+|\Z", prose):
        end = boundary.start()
        sentence = prose[start:end].strip()
        if sentence:
            sentences.append((start, end, sentence))
        start = boundary.end()
    return sentences


def _require_sentence(text: str, patterns: tuple[str, ...]) -> tuple[int, int]:
    """Require every pattern in a single sentence, and return its raw span."""
    for start, end, sentence in _prose_sentences(text):
        if all(re.search(pattern, sentence, re.I) for pattern in patterns):
            return start, end
    raise AssertionError(f"no prose sentence satisfies {patterns!r}")


def _forbid_sentence(text: str, patterns: tuple[str, ...]) -> None:
    assert not any(
        all(re.search(pattern, sentence, re.I) for pattern in patterns)
        for _, _, sentence in _prose_sentences(text)
    ), f"forbidden prose sentence satisfies {patterns!r}"


def _require_semantic_sentence(text: str, rule: str) -> tuple[int, int]:
    return _require_sentence(text, SEMANTIC_RULES[rule][1])


def _require_deletion_breaks_semantic_rule(text: str, rule: str) -> None:
    start, end = _require_semantic_sentence(text, rule)
    mutated = text[:start] + text[end:]
    assert mutated != text
    with pytest.raises(AssertionError, match="no prose sentence"):
        _require_semantic_sentence(mutated, rule)


def _require_no_data_rule(text: str) -> None:
    """D14's quoted contract is two adjacent sentences, not one long sentence.

    Also allow one sentence carrying both halves.  Unrelated prose elsewhere
    in L2 must not supply the gaps/ok disposition for the no_data rule.
    """
    restriction = _require_semantic_sentence(
        text, "non_ok_views_cannot_support_absence_claims"
    )
    disposition = _require_semantic_sentence(
        text, "empty_query_requires_gaps_or_direct_ok_evidence"
    )
    spans = [(start, end) for start, end, _ in _prose_sentences(text)]
    assert spans.index(disposition) in (
        spans.index(restriction),
        spans.index(restriction) + 1,
    ), "no_data restriction must include or immediately precede its gaps/ok disposition"


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


def test_prompt_and_ext_tool_revisions_change():
    base = select_profile({PROFILE_ENV: "otel-demo"}).versions()
    extension = select_profile({PROFILE_ENV: "otel-demo-ext"}).versions()

    assert investigation_versions("replay-candidate")["prompt_revision"] != (
        BASE_PROMPT_REVISION
    )
    assert base["prompt_revision"] != BASE_PROMPT_REVISION
    assert extension["prompt_revision"] != EXT_PROMPT_REVISION
    assert extension["tool_schema_revision"] != EXT_TOOL_SCHEMA_REVISION


def test_base_tool_context_policy_and_report_schema_are_unchanged():
    base = select_profile({PROFILE_ENV: "otel-demo"}).versions()
    extension = select_profile({PROFILE_ENV: "otel-demo-ext"}).versions()
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
def test_r2_required_semantics_are_present_in_one_prose_sentence(rule: str):
    section, _ = SEMANTIC_RULES[rule]
    _require_semantic_sentence(_rendered_section(section), rule)


@pytest.mark.parametrize("rule", SEMANTIC_RULES)
def test_r2_deleting_the_required_sentence_breaks_the_same_assertion(rule: str):
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
    # supported-needs-fact rule when deleting the summary wording rule.
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


def test_d13_base_investigation_method_is_byte_identical_to_main_83b3b968():
    method = _segments("replay-candidate")["investigation_method"].text
    rendered = d.render(
        "replay-candidate", model_requests=5, report_contract=REPORT_CONTRACT
    )
    assert method in rendered
    assert hashlib.sha256(method.encode("utf-8")).hexdigest() == (
        MAIN_INVESTIGATION_METHOD_SHA256
    )


def test_d13_base_render_omits_the_ext_background_guidance():
    rendered = d.render(
        "replay-candidate", model_requests=5, report_contract=REPORT_CONTRACT
    )
    _forbid_sentence(
        rendered,
        SEMANTIC_RULES["alert_is_entry_and_preexisting_stable_errors_are_background"][
            1
        ],
    )


def test_d13_base_background_guard_rejects_an_injected_rule():
    rendered = d.render(
        "replay-candidate", model_requests=5, report_contract=REPORT_CONTRACT
    )
    rule = "alert_is_entry_and_preexisting_stable_errors_are_background"
    patterns = SEMANTIC_RULES[rule][1]
    _forbid_sentence(rendered, patterns)
    with pytest.raises(AssertionError, match="forbidden prose sentence"):
        _forbid_sentence(rendered + " " + SEMANTIC_EXAMPLES[rule], patterns)


def test_d12_l2_does_not_bind_hypothesis_to_partial():
    _forbid_sentence(
        _rendered_section("report"), (r"\bhypothes(?:is|es)\b", r"\bpartial\b")
    )


def test_d12_old_hypothesis_partial_rule_is_rejected_and_removal_clears_it():
    patterns = (r"\bhypothes(?:is|es)\b", r"\bpartial\b")
    rendered = d.render(
        "replay-candidate",
        model_requests=5,
        report_contract=" ".join(SEMANTIC_EXAMPLES.values()),
    )
    _forbid_sentence(rendered, patterns)
    mutated = rendered + (
        " When the claim that names that object as the cause is a hypothesis, "
        "conclusion is partial, not supported."
    )
    with pytest.raises(AssertionError, match="forbidden prose sentence"):
        _forbid_sentence(mutated, patterns)
    start, end = _require_sentence(mutated, patterns)
    _forbid_sentence(mutated[:start] + mutated[end:], patterns)


def test_d14_non_ok_restriction_and_gaps_disposition_form_one_local_rule():
    _require_no_data_rule(_rendered_section("report"))


def test_d14_local_rule_self_check_rejects_deleting_either_sentence_or_separating_them():
    restriction = SEMANTIC_EXAMPLES["non_ok_views_cannot_support_absence_claims"]
    disposition = SEMANTIC_EXAMPLES["empty_query_requires_gaps_or_direct_ok_evidence"]
    _require_no_data_rule(f"{restriction} {disposition}")
    for mutated in (
        restriction,
        disposition,
        f"{restriction} This unrelated sentence separates the rules. {disposition}",
    ):
        with pytest.raises(AssertionError):
            _require_no_data_rule(mutated)


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
