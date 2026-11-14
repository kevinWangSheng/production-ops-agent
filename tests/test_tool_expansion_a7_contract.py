"""Independent contract tests for A7 Kubernetes investigation guidance (#559).

The implementation is being written in another worktree. The new variant and
the extension-profile changes are therefore expected to be red on this main
checkout; the frozen otel-demo values must remain green.

接缝假设（实现者必须满足的最小公开接缝）：

* opspilot.instructions.discipline.VARIANTS and render remain the public L1a
  seam; the new variant is named replay-candidate-k8s and is represented by
  the same Segment records as replay-candidate plus one template segment.
* ToolProfile.face remains the public way to inspect a profile's variant_id.
  The extension face can be constructed without Kubernetes credentials, as in
  the existing A2-A6 contract tests.
* The extension face's model-visible schemas are the registered tool-name and
  parameter-name source for the prompt quarantine checks. The test does not
  hand-maintain that inventory.
* read_evidence source attribution in a later A7 summary is carried by the
  persisted evidence view's existing parent_tool field; this test does not
  invent a new summary field for it.

All synthetic values are low-entropy placeholders. No cluster, container or
full-suite pytest run is used.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from opspilot.instructions import discipline as d
from opspilot.investigation.loop import investigation_versions
from opspilot.investigation.reports import REPORT_CONTRACT
from opspilot.tools.profiles import PROFILE_ENV, select_profile

PROMPT_REVISION = "prompt-replay-candidate-bef70649a3a8"
DISCIPLINE_REVISION = "l1a-replay-candidate-9a45deb526fe"
BASE_TOOL_SCHEMA_REVISION = "otel-demo-a3a95a0f888e"
EXT_TOOL_SCHEMA_REVISION = "otel-demo-ext-d467c4b436b0"
REPLAY_RENDER_SHA256 = (
    "f38c56abc2d6745b7195e35c46d51b6b1e28d115fbb8639db92a8e1627a5c6b9"
)


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def _face(profile_name: str):
    return select_profile({PROFILE_ENV: profile_name}).face(_Clock())


def _nested_schema_names(value: object) -> tuple[str, ...]:
    """Collect every function property name from the model-visible schemas."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                found.update(str(key) for key in properties)
            for child in node.values():
                walk(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(value)
    return tuple(sorted(found))


def _registered_tool_names() -> tuple[str, ...]:
    schemas = _face("otel-demo-ext").tool_schemas
    names = {
        str(schema["function"]["name"])
        for schema in schemas
        if isinstance(schema, Mapping)
        and isinstance(schema.get("function"), Mapping)
        and isinstance(schema["function"].get("name"), str)
    }
    return tuple(sorted(names))


def _guidance() -> Any:
    variant = d.VARIANTS["replay-candidate-k8s"]
    return next(segment for segment in variant if segment.key == "kubernetes_guidance")


def test_otel_demo_frozen_prompt_and_discipline_values_remain_byte_stable() -> None:
    assert (
        investigation_versions("replay-candidate")["prompt_revision"] == PROMPT_REVISION
    )
    assert d.discipline_revision("replay-candidate") == DISCIPLINE_REVISION
    assert _face("otel-demo").variant_id == "replay-candidate"

    rendered = d.render(
        "replay-candidate",
        model_requests=3,
        report_contract=REPORT_CONTRACT,
    )
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == REPLAY_RENDER_SHA256


def test_otel_demo_tool_schema_revision_is_unchanged() -> None:
    assert select_profile({PROFILE_ENV: "otel-demo"}).versions()[
        "tool_schema_revision"
    ] == (BASE_TOOL_SCHEMA_REVISION)


def test_replay_candidate_k8s_inherits_the_base_segments_and_inserts_guidance() -> None:
    base = d.VARIANTS["replay-candidate"]
    candidate = d.VARIANTS["replay-candidate-k8s"]
    assert [segment.key for segment in candidate] == [
        *(segment.key for segment in base[:-1]),
        "kubernetes_guidance",
        base[-1].key,
    ]
    assert tuple(candidate[: len(base) - 1]) == tuple(base[:-1])
    assert candidate[-1] == base[-1]

    guidance = _guidance()
    assert guidance.layer == d.LAYER_TEMPLATE
    assert guidance.tool_specific is False
    assert guidance.text.strip()


@pytest.mark.parametrize("needle", ["kubectl", "describe"])
def test_kubernetes_guidance_omits_forbidden_cli_words(needle: str) -> None:
    assert (
        re.search(
            rf"(?<![a-z0-9_]){re.escape(needle)}(?![a-z0-9_])",
            _guidance().text,
            re.I,
        )
        is None
    )


def test_kubernetes_guidance_omits_registered_tools_and_parameters() -> None:
    text = _guidance().text
    tool_hits = [
        name
        for name in _registered_tool_names()
        if re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text, re.I)
    ]
    assert tool_hits == []

    parameter_names = _nested_schema_names(_face("otel-demo-ext").tool_schemas)
    underscored_parameter_hits = [
        name
        for name in parameter_names
        if "_" in name
        if re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text, re.I)
    ]
    assert underscored_parameter_hits == []

    plain_parameter_hits = [
        name
        for name in parameter_names
        if "_" not in name
        if re.search(
            rf"\x60{re.escape(name)}\x60|(?<![a-z0-9_]){re.escape(name)}\s*(?:=|:)",
            text,
            re.I,
        )
    ]
    assert plain_parameter_hits == []


@pytest.mark.parametrize(
    "pattern",
    [
        r"\b(?:owners?|ownership)\b.*\bdeployments?\b.*\b(?:replica\s*sets?|replicasets?)\b.*\bpods?\b",
        r"\bprevious\b.*\b(?:containers?|instances?)\b.*\blogs?\b",
        r"\b(?:pending|not\s+ready)\b.*\b(?:whys?|reasons?)\b.*\b(?:recommends?|fix(?:es)?)\b",
        r"\brepresentatives?\b.*\b(?:three|3)\b.*\bworkloads?\b",
        r"\bevicted\b.*\boomkilled\b.*\b(?:events?|terminations?)\b",
        r"\bservices?\b.*\bingress(?:es)?\b.*\blogs?\b",
        r"\baffected\b.*\b(?:evicted|oomkilled|backoffs?|failed|unhealthy|errors?)\b",
        r"\bsecrets?\b.*\b(?:cannot|could\s+not|unable|not)\b.*\bverif",
        r"\b(?:running|ready)\b.*\blogs?\b",
    ],
)
def test_kubernetes_guidance_covers_k1_to_k9_method_points(pattern: str) -> None:
    assert re.search(pattern, _guidance().text, re.I | re.S), pattern


def test_otel_demo_ext_uses_the_new_prompt_variant_without_changing_tool_schema() -> (
    None
):
    versions = select_profile({PROFILE_ENV: "otel-demo-ext"}).versions()
    assert versions["prompt_revision"].startswith("prompt-replay-candidate-k8s-")
    assert versions["prompt_revision"] != PROMPT_REVISION
    assert _face("otel-demo-ext").variant_id == "replay-candidate-k8s"
    assert versions["tool_schema_revision"] == EXT_TOOL_SCHEMA_REVISION


def test_upstream_attribution_names_the_kubernetes_generic_ask_section() -> None:
    text = Path("docs/portfolio/upstream-attribution.md").read_text(encoding="utf-8")
    assert any(
        "generic_ask.jinja2" in line and "kubernetes" in line.lower()
        for line in text.splitlines()
    )
