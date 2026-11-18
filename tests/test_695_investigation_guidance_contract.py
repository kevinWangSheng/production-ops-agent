"""Independent L1 contracts for #695 + #690, authored without implementation.

The release contract's D17 requires one prose sentence per rule, deletion
mutations, and weakened counter-examples. These checks inspect public profile
renders; they make no assertion about an LLM's eventual investigation result.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from opspilot.instructions import discipline as d
from opspilot.investigation.reports import REPORT_CONTRACT
from opspilot.tools.profiles import PROFILE_ENV, select_profile

# Match semantic clauses, not complete sentences or their positions. Shared
# fragments also let K1/K3 accept "template remains unchanged" and paraphrases.
UNCHANGED_RESTART = (
    r"\brestart\w*\b[^;:]*\b(?:unchanged|unmodified|same)\s+(?:pod\s+)?template\b"
    r"|\brestart\w*\b[^;:]*\b(?:pod\s+)?template\b[^;:]*"
    r"\b(?:unchanged|unmodified|not\s+changed)\b"
    r"|\b(?:unchanged|unmodified|same)\s+(?:pod\s+)?template\b[^;:]*\brestart\w*\b"
)
UNSEEN_FAULT = (
    r"\b(?:did\s+not|have\s+not|never|didn't|haven't)\s+"
    r"(?:see|seen|observed?|witness(?:ed)?)\b[^;:]*\b(?:declared|specified|stated|configured)\s+"
    r"fault\b[^;:]*\b(?:happen|occur|manifest)\w*\b"
    r"|\b(?:declared|specified|stated|configured)\s+fault\b[^;:]*"
    r"\b(?:not|never)\s+(?:seen|observed|witnessed)\b"
)
UNKNOWN_CHANGE_TIME = (
    r"\b(?:cannot|can't|could\s+not|unable\s+to)\s+"
    r"(?:tell|determine|establish|verify|know)\b[^;:]*\b(?:when|the\s+time)\b[^;:]*"
    r"\b(?:setting|configuration)\b[^;:]*\b(?:changed|modified|altered)\b"
)
SETTING_HOLDER = (
    r"\b(?:object|resource)\b[^;:]*"
    r"\b(?:holds?|holding|contains?|containing|stores?|storing|owns?|owning)\b"
    r"[^;:]*\b(?:setting|configuration)\b"
)
NEGATED_DIRECTIVE = (
    r"\b(?:not|never|don't|cannot|can't|mustn't|shouldn't|avoid|refrain\s+from|"
    r"no\s+(?:need|reason)\s+to)\s+"
    r"(?:(?:ever|actually|explicitly|directly|necessarily)\s+)*"
)


def _affirmative(pattern: str) -> tuple[str, str]:
    """Require the action and reject a negation governing that same action.

    Negations elsewhere remain valid: K2 must not reject a persistent setting,
    and K3 must not assign a root cause when its trigger is unknown.
    """
    return pattern, rf"^(?!.*{NEGATED_DIRECTIVE}(?:{pattern}))"


# These are lexical alternatives plus the one allowed logical alternative.
# Checking each "or" in context avoids treating "or no other object" as an
# independent attribution trigger, without pinning the order of conditions.
K1_OR_CONTEXTS = (
    (r"\b(?:off|zero|0|false),?\s*", r"\s+(?:off|zero|0|false)\b"),
    (
        r"\b(?:name|description)\s*",
        r"\s+(?:(?:its|the)\s+)?(?:name|description)\b",
    ),
    (r"\b(?:workload|service)\s*", r"\s+(?:(?:the|a)\s+)?(?:workload|service)\b"),
    (
        rf"(?:{UNCHANGED_RESTART})\s*",
        r"\s+(?:(?:is\s+)?failing|fails|has\s+(?:errors|failures))\b",
    ),
    (
        r"\b(?:(?:is\s+)?failing|fails|has\s+(?:errors|failures))\s*",
        rf"\s+(?:{UNCHANGED_RESTART})",
    ),
    (r"\bchange\s*", r"\s+fault\b"),
)

RULES = {
    "K1": (
        r"\b(?:value\s+in\s+(?:effect|use)|(?:effective|current|actual)\s+value)\b"
        r"[^;:]*?\b(?:not|neither|other\s+than)\b"
        r"(?=[^;:]*\boff\b)(?=[^;:]*\b(?:zero|0)\b)(?=[^;:]*\bfalse\b)",
        r"\b(?:name\s+or\s+(?:(?:its|the)\s+)?description|"
        r"description\s+or\s+(?:(?:its|the)\s+)?name)\b[^;:]*"
        r"\b(?:refer\w*\s+to|point\w*\s+to|identif\w*|target\w*|nam\w*)\b"
        r"[^;:]*\b(?:workload|service)\b",
        rf"(?:{UNCHANGED_RESTART})",
        r"\bor\s+(?:(?:is\s+)?failing|fails|has\s+(?:errors|failures))\b"
        r"|\b(?:(?:is\s+)?failing|fails|has\s+(?:errors|failures))\s+or\b",
        r"\b(?:no\s+other\s+(?:object|resource)|no\s+evidence\s+of\s+"
        r"(?:another|any\s+other)\s+(?:object|resource))\b[^;:]*"
        r"\b(?:appl\w*|impos\w*|introduc\w*)\b[^;:]*\bchange\s+or\s+fault\b",
        *_affirmative(
            rf"\b(?:name|identify|report|cite)\b[^;:]*(?:{SETTING_HOLDER})[^;:]*"
            r"\bas\s+(?:the\s+)?root[- ]cause\b"
            r"|\b(?:name|identify|report|cite)\s+as\s+(?:the\s+)?root[- ]cause\b"
            rf"[^;:]*(?:{SETTING_HOLDER})"
        ),
        r"\b(?:each|every)\b[^;:]*\b(?:setting|configuration)\b[^;:]*"
        r"\b(?:own|separate\w*|individual|distinct)\b[^;:]*\bfact\b"
        r"|\b(?:separate|individual|distinct)\s+fact\b[^;:]*"
        r"\b(?:each|every)\b[^;:]*\b(?:setting|configuration)\b",
        r"\brestart\w*\b[^;:]*\bmechanism\b",
        r"\berrors?\b[^;:]*\bconsequences?\b",
        rf"^(?!.*{NEGATED_DIRECTIVE}\b(?:write|record|report|list|state)\b[^;:]*"
        r"(?:\b(?:each|every)\b[^;:]*\bfact\b|"
        r"\brestart\w*\b[^;:]*\bmechanism\b|\berrors?\b[^;:]*\bconsequences?\b))",
    ),
    "K2": (
        rf"(?:{UNSEEN_FAULT})[^;:]*\bor\b[^;:]*(?:{UNKNOWN_CHANGE_TIME})"
        rf"|(?:{UNKNOWN_CHANGE_TIME})[^;:]*\bor\b[^;:]*(?:{UNSEEN_FAULT})",
        *_affirmative(
            r"\b(?:set|make|mark|report|keep|use)\b[^;:]*"
            r"\b(?:conclusion\b[^;:]*\bpartial|partial\b[^;:]*\bconclusion)\b"
        ),
        r"^(?!.*\b(?:not|never)\s+partial\b)",
        *_affirmative(
            r"\b(?:record|report|write|list|note)\b[^;:]*"
            r"(?:\bboth\b[^;:]*\bgaps\b|\bgaps\b[^;:]*\bboth\b)"
        ),
        r"\b(?:still|remains?)\s+on\b",
        r"\berrors?\b[^;:]*\b(?:stopped|ceased|ended|resolved)\b",
        r"\b(?:not|no)\s+(?:a\s+)?(?:reason|grounds?|basis)\s+to\s+"
        r"(?:reject|dismiss|rule\s+out)\b"
        r"|\b(?:do\s+not|never)\s+(?:reject|dismiss|rule\s+out)\b",
        r"^(?!.*\bhypothes(?:is|es)\b)",
    ),
    "K3": (
        r"\bworkload\b",
        rf"(?:{UNCHANGED_RESTART})",
        r"\bnot\s+(?:itself\s+)?(?:the\s+)?root[- ]cause\b",
        r"\b(?:cannot|can't|unable\s+to)\s+(?:find|identify|determine|establish|locate)\b"
        r"[^;:]*\b(?:trigger\w*|cause\w*|initiat\w*)\b"
        r"|\b(?:trigger|cause)\b[^;:]*\b(?:unknown|unidentified|not\s+found)\b",
        *_affirmative(
            r"\b(?:name|report|identify|assign)\s+no\s+root[- ]cause\b"
            r"|\b(?:do\s+not|never)\s+(?:name|report|identify|assign)\s+"
            r"(?:(?:a|the|any)\s+)?root[- ]cause\b"
        ),
        *_affirmative(r"\b(?:record|report|write|note|list)\b[^;:]*\bgap\b"),
    ),
    "M1": (
        r"\bbefore\b[^;:]*\b(?:call\w*|declar\w*|label\w*|classif\w*)\b"
        r"[^;:]*\bunobservable\b",
        *_affirmative(
            r"\b(?:list|enumerate|identify)\b[^;:]*"
            r"(?:\bmetrics?\b[^;:]*\blabels?\b|\blabels?\b[^;:]*\bmetrics?\b)"
            r"[^;:]*\bnames\b"
        ),
        r"\b(?:could|can|might|may)\b[^;:]*\b(?:show|reveal|expose|indicate|detect)\w*\b",
        *_affirmative(
            r"\b(?:check|inspect|query|examine|look\s+up)\s+"
            r"(?:them|those|these|(?:the\s+)?(?:metrics?|names?|signals?))\b"
        ),
        r"\b(?:did\s+not|have\s+not|never|didn't|haven't)\s+"
        r"(?:look\w*\s+for|seek|search\w*\s+for|check|inspect|query|examine)\b"
        r"|\bnot\s+(?:sought|queried|checked|examined)\b",
        r"\b(?:is|are|remains?|as)\s+(?:unchecked|unexamined|not\s+checked)\b",
        r"\bnot\s+unobservable\b",
    ),
    "M2": (
        r"\b(?:time\s+range|interval|period|window)\b[^;:]*"
        r"\b(?:no|without|lacking)\s+samples\b"
        r"|\bunsampled\s+(?:time\s+range|interval|period|window)\b",
        r"\b(?:is|are|remains?|as)\s+unknown\b",
        r"\b(?:not|rather\s+than|instead\s+of)\s+(?:zero|0)\b",
        *_affirmative(
            r"\b(?:state|report|record|list|give|name)\b[^;:]*"
            r"\b(?:sampled\s+times|sample\s+times|sample\s+timestamps|sampling\s+times|"
            r"timestamps\s+of\s+(?:the\s+)?samples)\b"
        ),
        r"\b(?:rel(?:y|ied|ying)|depend\w*|based)\b",
    ),
}

# Reference wording is test data, never an equality assertion on product text.
EXAMPLES = {
    "K1": (
        "When such a setting is switched on, meaning its value in effect is not "
        "off, zero or false, and its name or description refers to the workload "
        "or service that was restarted with an unchanged pod template or is "
        "failing, and no other object is seen applying a change or fault to that "
        "workload, name the object that holds the setting as the root cause, "
        "write each such setting you read as its own fact, and write the restart "
        "and errors as its mechanism and consequences."
    ),
    "K2": (
        "When you did not see the declared fault itself happen, or cannot tell "
        "when the setting was changed, set the conclusion to partial and record "
        "both as gaps; a setting that is still on while the errors have stopped "
        "is not a reason to reject it."
    ),
    "K3": (
        "A workload restarted with an unchanged pod template is not itself the "
        "root cause; when you cannot find what triggered the restart, name no "
        "root cause and record that as a gap."
    ),
    "M1": (
        "Before calling something unobservable, list the metric and label names "
        "that could show it and check them; a signal you did not look for is "
        "unchecked, not unobservable."
    ),
    "M2": (
        "A time range with no samples is unknown, not zero; state the sampled "
        "times you rely on."
    ),
}

PARAPHRASES = {
    "K1": (
        "If a setting's effective value is neither off nor 0 nor false, and its "
        "description or name identifies the service or workload that restarted "
        "while its pod template remained unchanged or has errors, and no "
        "evidence of another resource applying a change or fault is present, "
        "identify as root cause the resource containing that setting, record "
        "every setting as a separate fact, and report the restart as the "
        "mechanism and errors as consequences."
    ),
    "K2": (
        "If the specified fault was not observed, or you cannot "
        "determine the time the configuration was modified, mark the conclusion "
        "partial and list both limitations as gaps; do not dismiss a setting "
        "that remains on after errors ceased."
    ),
    "K3": (
        "A workload that restarted while its pod template remained unmodified "
        "is not the root cause; if the restart trigger is unknown, "
        "do not assign any root cause and note the missing trigger as a gap."
    ),
    "M1": (
        "Before declaring a signal unobservable, enumerate the metric and "
        "label names that might reveal it and query them; a signal not sought "
        "is unexamined, not unobservable."
    ),
    "M2": (
        "An interval without samples is unknown rather than 0; report the "
        "sample timestamps on which the assessment is based."
    ),
}

# Each edit removes or reverses one necessary part while retaining the rest.
WEAKENINGS = (
    (
        "K1",
        "without_value_condition",
        "meaning its value in effect is not off, zero or false, ",
        "",
    ),
    ("K1", "without_unchanged_template", " with an unchanged pod template", ""),
    (
        "K1",
        "setting_instead_of_holder",
        "name the object that holds the setting",
        "name the setting itself",
    ),
    (
        "K1",
        "without_other_actor_guard",
        "no other object is seen applying a change or fault",
        "another object is seen applying a change or fault",
    ),
    (
        "K1",
        "without_each_fact",
        "write each such setting you read as its own fact, and ",
        "",
    ),
    (
        "K1",
        "without_mechanism",
        "and write the restart and errors as its mechanism and consequences",
        "and report errors",
    ),
    (
        "K2",
        "without_fault_gap",
        "you did not see the declared fault itself happen, or ",
        "you see errors and ",
    ),
    (
        "K2",
        "without_change_time",
        "or cannot tell when the setting was changed",
        "or see a restart",
    ),
    ("K2", "requires_both_conditions", "happen, or cannot", "happen, and cannot"),
    ("K2", "without_both_gaps", "record both as gaps", "record a gap"),
    (
        "K2",
        "rejects_persistent_setting",
        "is not a reason to reject it",
        "is a reason to reject it",
    ),
    (
        "K3",
        "restart_is_cause",
        "is not itself the root cause",
        "is itself the root cause",
    ),
    (
        "K3",
        "names_unsupported_cause",
        "name no root cause",
        "name the workload as root cause",
    ),
    ("K3", "without_trigger_gap", "and record that as a gap", "and finish the report"),
    ("M1", "without_label_names", "metric and label names", "metric names"),
    ("M1", "without_check", " and check them", ""),
    (
        "M1",
        "unsearched_is_unobservable",
        "unchecked, not unobservable",
        "unobservable, not unchecked",
    ),
    ("M2", "empty_means_zero", "unknown, not zero", "zero, not unknown"),
    (
        "M2",
        "without_sample_times",
        "state the sampled times you rely on",
        "state the time range you rely on",
    ),
)

# Reviewer mutations and the equivalent reversals of other required actions.
# K3's "is itself the root cause" is already covered by restart_is_cause.
REVIEW_WEAKENINGS = (
    (
        "K1",
        "disjunctive_other_actor_guard",
        "and no other object",
        "or no other object",
    ),
    ("K1", "disjunctive_name_condition", "false, and its name", "false, or its name"),
    (
        "K1",
        "negated_holder_attribution",
        "name the object that holds the setting",
        "do not name the object that holds the setting",
    ),
    (
        "K1",
        "negated_each_fact",
        "write each such setting",
        "do not write each such setting",
    ),
    (
        "K1",
        "negated_mechanism",
        "write the restart and errors",
        "do not write the restart and errors",
    ),
    (
        "K2",
        "negated_partial_directive",
        "set the conclusion to partial",
        "do not set the conclusion to partial",
    ),
    (
        "K2",
        "negated_partial_value",
        "conclusion to partial",
        "conclusion to not partial",
    ),
    ("K2", "negated_both_gaps", "record both as gaps", "do not record both as gaps"),
    (
        "K3",
        "negated_trigger_gap",
        "record that as a gap",
        "do not record that as a gap",
    ),
    (
        "K3",
        "negated_no_cause_directive",
        "name no root cause",
        "do not name no root cause",
    ),
    ("M1", "negated_check", "check them", "do not check them"),
    (
        "M1",
        "negated_name_listing",
        "list the metric and label names",
        "do not list the metric and label names",
    ),
    ("M1", "negated_unchecked", "is unchecked", "is not unchecked"),
    ("M2", "negated_unknown", "is unknown, not zero", "is not unknown, not zero"),
    (
        "M2",
        "negated_sample_times",
        "state the sampled times",
        "do not state the sampled times",
    ),
)


def _prose_sentences(text: str) -> list[tuple[int, int, str]]:
    """Keep raw offsets, including leading separators, for exact D16 removal.

    Quoted/code examples cannot supply normative guidance. Retaining each
    sentence's separator lets removal preserve all other bytes, including the
    constants' leading space, without whitespace normalization.
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
    sentences = []
    start = 0
    for boundary in re.finditer(r"(?<=[.!?])(?=\s)|\Z", prose):
        end = boundary.start()
        sentence = prose[start:end].strip()
        if sentence:
            sentences.append((start, end, sentence))
        start = end
    return sentences


def _matches_rule(sentence: str, rule: str) -> bool:
    # Layout whitespace cannot change negation scope; raw deletion offsets
    # remain in _prose_sentences and are never normalized.
    sentence = " ".join(sentence.split())
    if not all(re.search(pattern, sentence, re.I) for pattern in RULES[rule]):
        return False
    if rule == "K1":
        for disjunction in re.finditer(r"\bor\b", sentence, re.I):
            before, after = (
                sentence[: disjunction.start()],
                sentence[disjunction.end() :],
            )
            if not any(
                re.search(left + r"$", before, re.I) and re.match(right, after, re.I)
                for left, right in K1_OR_CONTEXTS
            ):
                return False
    return True


def _require_sentence(text: str, rule: str) -> tuple[int, int]:
    for start, end, sentence in _prose_sentences(text):
        if _matches_rule(sentence, rule):
            return start, end
    raise AssertionError(f"no prose sentence satisfies {rule}: {RULES[rule]!r}")


def remove_rule_sentences(text: str, rules: tuple[str, ...]) -> str:
    """Require one distinct match per rule, then remove exactly those bytes."""
    spans = []
    for rule in rules:
        matching = [
            (start, end)
            for start, end, sentence in _prose_sentences(text)
            if _matches_rule(sentence, rule)
        ]
        assert len(matching) == 1, f"expected one {rule} sentence, got {matching!r}"
        spans.extend(matching)
    assert len(set(spans)) == len(rules), "rules must occupy distinct sentences"
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _variant(profile: str) -> str:
    return select_profile({PROFILE_ENV: profile}).face(_Clock()).variant_id


def _render(profile: str) -> str:
    return d.render(
        _variant(profile), model_requests=5, report_contract=REPORT_CONTRACT
    )


def _rendered_section(profile: str, rule: str) -> str:
    key = "kubernetes_guidance" if rule.startswith("K") else "investigation_method"
    text = next(s.text for s in d.VARIANTS[_variant(profile)] if s.key == key)
    rendered = _render(profile)
    start = rendered.index(text)
    return rendered[start : start + len(text)]


RENDER_CASES = [
    pytest.param(profile, rule, id=f"{rule}-{profile}")
    for rule in RULES
    for profile in (
        ("otel-demo-ext",) if rule.startswith("K") else ("otel-demo", "otel-demo-ext")
    )
]


@pytest.mark.parametrize("profile,rule", RENDER_CASES)
def test_required_rule_is_present_in_one_rendered_prose_sentence(
    profile: str, rule: str
):
    _require_sentence(_rendered_section(profile, rule), rule)


@pytest.mark.parametrize("profile,rule", RENDER_CASES)
def test_deleting_the_required_sentence_breaks_the_same_assertion(
    profile: str, rule: str
):
    text = _rendered_section(profile, rule)
    start, end = _require_sentence(text, rule)
    mutated = text[:start] + text[end:]
    assert mutated != text
    with pytest.raises(AssertionError, match="no prose sentence"):
        _require_sentence(mutated, rule)


@pytest.mark.parametrize("rule", ("K1", "K2", "K3"))
def test_kubernetes_rules_are_absent_from_base_render(rule: str):
    with pytest.raises(AssertionError, match="no prose sentence"):
        _require_sentence(_render("otel-demo"), rule)


def test_kubernetes_guidance_does_not_use_enabled_as_a_value_condition():
    assert (
        re.search(r"\benabled\b", _rendered_section("otel-demo-ext", "K1"), re.I)
        is None
    )


@pytest.mark.parametrize("rule", RULES)
def test_matchers_accept_reference_and_paraphrase_but_reject_nonprose_and_split_rules(
    rule: str,
):
    for sentence in (
        EXAMPLES[rule],
        PARAPHRASES[rule],
        EXAMPLES[rule].replace(" ", "\n"),
    ):
        text = " ".join(EXAMPLES[key] if key != rule else sentence for key in RULES)
        _require_sentence(text, rule)
        with pytest.raises(AssertionError, match="no prose sentence"):
            _require_sentence(remove_rule_sentences(text, (rule,)), rule)
        for example in (
            json.dumps({"guidance": sentence, "claims": [{"kind": "fact"}]}),
            json.dumps([{"instruction": sentence}]),
            f"`{sentence}`",
            f"```text\n{sentence}\n```",
            ". ".join(sentence.rstrip(".").split()) + ".",
        ):
            with pytest.raises(AssertionError, match="no prose sentence"):
                _require_sentence(example, rule)


@pytest.mark.parametrize(
    "rule,case,before,after",
    WEAKENINGS + REVIEW_WEAKENINGS,
    ids=[case[1] for case in WEAKENINGS + REVIEW_WEAKENINGS],
)
def test_weakened_counterexamples_are_rejected(
    rule: str, case: str, before: str, after: str
):
    assert EXAMPLES[rule].count(before) == 1, case
    weakened = EXAMPLES[rule].replace(before, after)
    for text in (weakened, weakened.replace(" ", "\n")):
        with pytest.raises(AssertionError, match="no prose sentence"):
            _require_sentence(text, rule)
        # D16's hashes must use the same polarity/conjunction guard.
        with pytest.raises(AssertionError, match=f"expected one {rule} sentence"):
            remove_rule_sentences(text, (rule,))


@pytest.mark.parametrize(
    "scope,expected", [("kubernetes", 25), ("method", 10), ("candidate", 32)]
)
def test_sentence_census_uses_the_existing_template_counter(scope: str, expected: int):
    # Same denominator as test_instruction_discipline.py:279-293: 32 is the
    # candidate's L1 template sum, NOT the two baseline variants' union (30).
    def sentences(text: str) -> int:
        return len([s for s in re.split(r"(?<=\.)\s+", text.strip()) if s])

    if scope == "kubernetes":
        actual = sentences(d.KUBERNETES_GUIDANCE)
    elif scope == "method":
        actual = sentences(d.INVESTIGATION_METHOD)
    else:
        actual = sum(
            sentences(s.text)
            for s in d.VARIANTS[_variant("otel-demo")]
            if s.layer == d.LAYER_TEMPLATE
        )
    assert actual == expected
