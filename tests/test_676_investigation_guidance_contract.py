"""Independent contract tests for issue #676 investigation guidance.

These tests observe only the public instruction, profile and registration
surfaces.  The implementation is being developed in another worktree, so the
main checkout is expected to fail the new prompt and description assertions
until that work lands.  Existing contract tests remain unchanged.
"""

from __future__ import annotations

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

# Main's census is a lower bound only.  #676 adds new guidance, while the
# exact sentence total is owned by the implementer and the existing census
# test is intentionally not edited by this independent test author.
MAIN_REPLAY_SENTENCE_COUNT = 30
MAIN_KUBERNETES_SENTENCE_COUNT = 12


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def _segments(variant: str) -> dict[str, d.Segment]:
    return {segment.key: segment for segment in d.VARIANTS[variant]}


def _guidance_text() -> str:
    return _segments("replay-candidate-k8s")["kubernetes_guidance"].text


def _sentence_count(text: str) -> int:
    return len([item for item in re.split(r"(?<=\.)\s+", text.strip()) if item])


def _template_sentence_count(variant: str) -> int:
    return sum(
        _sentence_count(segment.text)
        for segment in d.VARIANTS[variant]
        if segment.layer == d.LAYER_TEMPLATE
    )


def _schema_text(tool_name: str) -> str:
    face = select_profile({PROFILE_ENV: "otel-demo-ext"}).face(_Clock())
    schema = next(
        item["function"]
        for item in face.tool_schemas
        if isinstance(item, Mapping)
        and isinstance(item.get("function"), Mapping)
        and item["function"].get("name") == tool_name
    )
    return repr(schema)


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


def test_sentence_census_grows_for_the_new_guidance_without_pinning_unknown_totals():
    replay_count = _template_sentence_count("replay-candidate")
    kubernetes_count = _sentence_count(_guidance_text())

    assert MAIN_REPLAY_SENTENCE_COUNT < replay_count <= MAIN_REPLAY_SENTENCE_COUNT + 6
    assert (
        MAIN_KUBERNETES_SENTENCE_COUNT
        < kubernetes_count
        <= MAIN_KUBERNETES_SENTENCE_COUNT + 10
    )


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
        assert re.search(
            rf"(?<![a-z0-9_]){re.escape(forbidden)}(?![a-z0-9_])", text, re.I
        ) is None

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
        parameters = function.get("parameters") if isinstance(function, Mapping) else None
        properties = parameters.get("properties") if isinstance(parameters, Mapping) else None
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
    assert [term for term in environment_terms if re.search(re.escape(term), text, re.I)] == []


def test_new_guidance_and_report_contract_carry_the_decision_table_phrases():
    guidance = _guidance_text()
    method_and_scope = " ".join(
        _segments("replay-candidate")[key].text
        for key in ("investigation_method", "impact_scope")
    )

    assert re.search(
        r"\bevents?\b.{0,260}\b(?:whole|entire|all|cluster[- ]wide)\b"
        r".{0,100}\b(?:cluster|namespaces?)\b",
        guidance,
        re.I | re.S,
    ) or re.search(
        r"\b(?:whole|entire|all|cluster[- ]wide)\b.{0,100}\b(?:cluster|namespaces?)\b"
        r".{0,260}\bevents?\b",
        guidance,
        re.I | re.S,
    )
    assert re.search(
        r"(?:\bevents?\b.{0,260}\btype\b.{0,100}\b(?:filter|narrow|restrict|select)|"
        r"\bevents?\b.{0,260}\b(?:filter|narrow|restrict|select).{0,100}\btype\b)",
        guidance,
        re.I | re.S,
    )
    assert all(re.search(rf"\b{word}\b", guidance, re.I) for word in ("kind", "namespace", "reason"))
    assert re.search(r"\b(?:group|aggregate|summari[sz]e|count)\w*\b", guidance, re.I)

    assert re.search(r"\b(?:restart|rollout)\b.{0,220}\b(?:workload|template)\b", guidance, re.I | re.S)
    assert re.search(r"\b(?:workload|deployment|config(?:uration)?map)\b.{0,220}\bconfigmaps?\b", guidance, re.I | re.S)
    for resource in ("networkpolicies", "services", "endpoints", "resourcequotas"):
        assert re.search(rf"\b{resource}\b", guidance, re.I), resource

    assert re.search(r"\bsummary\b.{0,180}\b(?:fact|facts)\b", REPORT_CONTRACT, re.I | re.S)
    assert re.search(r"\b(?:fact|facts)\b.{0,140}\b(?:support|evidence)\b", REPORT_CONTRACT, re.I | re.S)
    assert re.search(r"\bhypothes\w*\b.{0,220}\bpartial\b", REPORT_CONTRACT, re.I | re.S)

    assert re.search(r"\bbackground\b", method_and_scope, re.I)
    assert re.search(r"\b(?:stable|steady|unchanged)\b", method_and_scope, re.I)
    assert re.search(r"\b(?:before|prior|earlier)\b", method_and_scope, re.I)

    assert re.search(
        r"(?:\b(?:only|solely)\b.{0,140}\bevents?\b.{0,160}\bpartial\b|"
        r"\bevents?\b.{0,160}\b(?:only|solely)\b.{0,160}\bpartial\b)",
        guidance,
        re.I | re.S,
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
            for item in kubernetes_core.kubernetes_registrations(max_window_seconds=86400)
            if item.name == tool_name
        )

    registration_text = _registered_text(registration)
    schema_text = _schema_text(tool_name)
    combined = f"{registration_text} {schema_text}"

    assert "involvedObject.kind" in combined
    assert re.search(r"\bevents?\b", combined, re.I)
    assert re.search(
        r"\b(?:whole|entire|all|all[- ]namespaces|cluster[- ]wide)\b"
        r".{0,100}\b(?:cluster|namespaces?)\b",
        combined,
        re.I | re.S,
    )
    assert re.search(
        r"\b(?:group|aggregate|summari[sz]e|count)\w*\b", combined, re.I
    )
