"""Independent acceptance tests for #310: the L1a investigation-method and
impact-scope sections (modelled on HolmesGPT ``generic_ask``).

Written from ``tmp/prereview-310.md`` section 3 ("给独立测试作者的可观察行为",
items 1-6), with U1-U6 ruled "as recommended" (2026-10-22) and R1-R10 as the
reversible defaults. The wording of the new paragraphs is the implementer's
(U1-a fixes the draft, not the bytes), so only a few key phrases taken from
the draft are asserted, never whole sentences. Revisions are compared for
*inequality* against the frozen main values; the new values are not guessed.

Frozen values were read on main 200172c with the public
``opspilot.instructions.discipline`` and ``investigation_versions()``.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from opspilot.instructions import discipline as d
from opspilot.investigation.context import context_policy_versions
from opspilot.investigation.loop import DISCIPLINE_VARIANT, investigation_versions
from opspilot.investigation.reports import REPORT_CONTRACT, REPORT_SCHEMA_VERSION

LIVE = "replay-candidate"

# -- frozen main values (200172c) ------------------------------------------------

OLD_DISCIPLINE_REVISION = "l1a-replay-candidate-e70df72344d9"
OLD_PROMPT_REVISION = "prompt-replay-candidate-dd9018031487"
CONTEXT_POLICY_REVISION = "ctx-ctx-policy-v1-a30e65ea52f4"
OLD_LIVE_KEYS = ["replay_opening", "evidence_discipline", "missing_series"]
# sha256 of REPLAY_OPENING + EVIDENCE_DISCIPLINE + MISSING_SERIES (1064 chars):
# the bytes the live system message starts with today.
OLD_PREFIX_SHA256 = "07bc23c4fa3785cfaa80cd724e9adf6a7ff2a791f2f2765e71e02c797e4d1fe5"
OLD_PREFIX_CHARS = 1064
SEGMENT_SHA256 = {
    "replay_opening": "f617d1288e0b4382916732e2729cd78b6088bd1affc84f4e4df8c0e16960d283",
    "evidence_discipline": "ad17466e049812d0972c065b374e6dd4c586ef66dedb0e9869fbfd529bfc7ea0",
    "missing_series": "3d436dc2c53a61b6215370ec965a285e1bbd6b2c4e8e7a35d08490ba00034ac9",
}
REPORT_CONTRACT_SHA256 = (
    "98b0a4f6a5426ed151b90af54ccd3d3d844e525693b217f123915f1037fe456a"
)
BASELINE_DISCIPLINE_REVISIONS = {
    "baseline-multi-step": "l1a-baseline-multi-step-1a7fad1f7bf9",
    "baseline-final-report": "l1a-baseline-final-report-ddd43a153a7e",
}
SERVICES = ("cartservice", "checkoutservice")
# sha256(render(variant, model_requests=..., report_contract=REPORT_CONTRACT,
# authorized_services=...)) on main.
BASELINE_RENDER_SHA256 = {
    ("baseline-multi-step", 7, ()): (
        "203af214ac12701c1ecf01b8673f2c7925e5cdaeee648005d06049518c84b137"
    ),
    ("baseline-multi-step", 7, SERVICES): (
        "f886bb4ce5e3c3e2a21798e0df909f74c5e655e961e402b776d6f013051a8fa0"
    ),
    ("baseline-final-report", 1, ()): (
        "2fdaa4cbf2f71dbc0559f330054340c4abddedbc3295118561fe4b6dd6452c83"
    ),
    ("baseline-final-report", 1, SERVICES): (
        "2cb2e4e54fbab92e948e22bea3ea0320ae4431722361237ad8ecec2e587b6194"
    ),
}

# R1: keys and position of the two new sections.
NEW_KEYS = ["investigation_method", "impact_scope"]

# Key phrases, chosen from the pre-review draft (section 1) as the ideas each
# section must carry; matched case-insensitively, wording around them free.
METHOD_PHRASES = (
    "deepest cause",  # M1: investigate toward the deepest supported cause
    "other possible causes",  # M2: keep checking alternatives
)
IMPACT_PHRASES = (
    "affected",  # I1/I8: the affected group
    "checked and healthy",  # I5/I8: the cleared group is reported
    "not checked",  # I4/I8: unchecked entities are a gap
    "unverified",  # I6: unobserved consequences are stated as unverified
)

# Item 2: F3 isolation words, checkout, tool names, Kubernetes-only words.
BANNED_WORDS = (
    "payment",
    "charge",
    "f3",
    "unrelated",
    "decoy",
    "checkout",
    "metrics_range_query",
    "traces_search",
    "get_metric_names",
    "get_label_values",
    "kubelet",
    "oomkilled",
    "evicted",
    "pod",
)


def _segments(variant: str = LIVE) -> dict[str, d.Segment]:
    return {segment.key: segment for segment in d.VARIANTS[variant]}


def _new_texts() -> dict[str, str]:
    segments = _segments()
    missing = [key for key in NEW_KEYS if key not in segments]
    assert not missing, f"new sections absent: {missing}"
    return {key: segments[key].text for key in NEW_KEYS}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render(model_requests: int = 5) -> str:
    return d.render(
        LIVE, model_requests=model_requests, report_contract=REPORT_CONTRACT
    )


# -- item 1: order and the unchanged prefix ---------------------------------------


def test_item1_new_sections_sit_after_missing_series_and_before_the_report():
    keys = [segment.key for segment in d.VARIANTS[LIVE]]
    assert keys == [*OLD_LIVE_KEYS, *NEW_KEYS, "report_contract"]
    segments = _segments()
    for key in NEW_KEYS:
        assert segments[key].layer == d.LAYER_TEMPLATE
        assert segments[key].text.strip(), key
        assert segments[key].scoped is False  # present in every live Run


def test_item1_render_carries_both_sections_in_order_after_an_unchanged_prefix():
    texts = _new_texts()
    rendered = _render()

    prefix = rendered[:OLD_PREFIX_CHARS]
    assert _sha(prefix) == OLD_PREFIX_SHA256
    method_at = rendered.index(texts["investigation_method"], OLD_PREFIX_CHARS)
    impact_at = rendered.index(texts["impact_scope"], OLD_PREFIX_CHARS)
    report_at = rendered.rindex(REPORT_CONTRACT)
    assert OLD_PREFIX_CHARS <= method_at < impact_at < report_at
    assert rendered.endswith(" " + REPORT_CONTRACT)
    # Nothing else was slipped in: the render is exactly the old prefix, the
    # two sections and the report slot.
    assert rendered == (
        prefix
        + texts["investigation_method"]
        + texts["impact_scope"]
        + " "
        + REPORT_CONTRACT
    )


@pytest.mark.parametrize("key", OLD_LIVE_KEYS)
def test_item1_the_first_three_sections_are_byte_identical(key):
    assert _sha(_segments()[key].text) == SEGMENT_SHA256[key]


@pytest.mark.parametrize(
    "key, phrases",
    [("investigation_method", METHOD_PHRASES), ("impact_scope", IMPACT_PHRASES)],
)
def test_item1_each_section_carries_its_key_phrases(key, phrases):
    text = _new_texts()[key].lower()
    assert [phrase for phrase in phrases if phrase not in text] == []


# -- item 2: banned words -------------------------------------------------------


@pytest.mark.parametrize("key", NEW_KEYS)
def test_item2_new_sections_carry_no_isolation_tool_or_kubernetes_words(key):
    text = _new_texts()[key]
    found = [
        word
        for word in BANNED_WORDS
        if re.search(rf"(?<![a-z0-9_]){re.escape(word)}(?![a-z0-9_])", text, re.I)
    ]
    # ``pods`` is the same Kubernetes noun; catch the plural too.
    if re.search(r"(?<![a-z0-9_])pods(?![a-z0-9_])", text, re.I):
        found.append("pods")
    assert found == []


# -- item 3: revisions move --------------------------------------------------------


def test_item3_discipline_and_prompt_revisions_change():
    assert DISCIPLINE_VARIANT == LIVE
    assert d.discipline_revision(LIVE) != OLD_DISCIPLINE_REVISION
    assert d.discipline_revision(LIVE).startswith("l1a-replay-candidate-")
    prompt = investigation_versions()["prompt_revision"]
    assert prompt != OLD_PROMPT_REVISION
    assert prompt.startswith("prompt-replay-candidate-")


# -- item 4: what stays the same --------------------------------------------------


@pytest.mark.parametrize("variant", sorted(BASELINE_DISCIPLINE_REVISIONS))
def test_item4_baseline_variants_keep_their_revision(variant):
    assert d.discipline_revision(variant) == BASELINE_DISCIPLINE_REVISIONS[variant]
    assert not {s.key for s in d.VARIANTS[variant]} & set(NEW_KEYS)


@pytest.mark.parametrize(
    "variant, model_requests, services",
    sorted(BASELINE_RENDER_SHA256, key=repr),
)
def test_item4_baseline_variants_render_the_same_bytes(
    variant, model_requests, services
):
    rendered = d.render(
        variant,
        model_requests=model_requests,
        report_contract=REPORT_CONTRACT,
        authorized_services=services,
    )
    assert _sha(rendered) == BASELINE_RENDER_SHA256[(variant, model_requests, services)]


def test_item4_report_contract_schema_and_context_policy_are_unchanged():
    assert _sha(REPORT_CONTRACT) == REPORT_CONTRACT_SHA256
    assert REPORT_SCHEMA_VERSION == "m0-report-v2"
    assert investigation_versions()["context_policy_revision"] == (
        CONTEXT_POLICY_REVISION
    )
    assert context_policy_versions() == {
        "context_policy_revision": CONTEXT_POLICY_REVISION
    }


# -- item 5: no tool-specific section ----------------------------------------------


def test_item5_the_live_variant_has_no_tool_specific_section():
    assert not any(segment.tool_specific for segment in d.VARIANTS[LIVE])
    tool_specific = {
        s.key for variant in d.VARIANTS.values() for s in variant if s.tool_specific
    }
    assert tool_specific == {"projection_discipline"}


# -- item 6: instance values stay out of the revision ------------------------------


def test_item6_only_model_requests_differing_leaves_the_revisions_equal():
    before = (d.discipline_revision(LIVE), investigation_versions())
    small = _render(model_requests=3)
    large = _render(model_requests=41)
    after = (d.discipline_revision(LIVE), investigation_versions())

    assert before == after
    projection = json.dumps(d.template_projection(LIVE), sort_keys=True)
    assert "41" not in projection
    # The live variant states no budget figure, so both renders are the same
    # bytes; the new sections must not introduce a budget placeholder.
    assert small == large
    assert all("{steps}" not in text for text in _new_texts().values())


def test_item6_the_budgeted_baseline_differs_in_bytes_not_in_revision():
    render = lambda n: d.render(  # noqa: E731
        "baseline-multi-step", model_requests=n, report_contract=REPORT_CONTRACT
    )
    assert render(3) != render(41)
    revision = d.discipline_revision("baseline-multi-step")
    assert revision == BASELINE_DISCIPLINE_REVISIONS["baseline-multi-step"]
