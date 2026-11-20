"""#667 PR-A contract, group 2: question template (S-U1=a) and answer isolation (R8-6, S-3).

The question depends only on ``/get_app``'s app name, namespace(s) and
descriptions; the template and its hash are pinned; the problem ID never
reaches the question or the idempotency key. App names and problem IDs below
are synthetic unless they come from the pinned Lite list.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.sregym_eval_support import (
    LITE_17,
    NAMESPACE_MULTI,
    NAMESPACE_SINGLE,
    QUESTION_LIMIT,
    TEMPLATE_SHA256,
    TEMPLATE_TEXT,
    code_of,
    mod,
)


def _app(
    app_name="Zephyr Widgets",
    namespace="zephyr-ns",
    namespaces=(),
    descriptions="Zephyr Widgets is a synthetic storefront made of five microservices.",
    stages=("diagnosis",),
):
    return mod("conductor").AppInfo(
        app_name=app_name,
        namespace=namespace,
        namespaces=tuple(namespaces),
        descriptions=descriptions,
        stages=tuple(stages),
    )


def _expected(app_name, block, descriptions):
    return TEMPLATE_TEXT.format(
        app_name=app_name, namespace_block=block, descriptions=descriptions
    )


# --- pinned template -------------------------------------------------------


def test_template_text_is_the_interface_text():
    q = mod("question")
    assert q.QUESTION_TEMPLATE == TEMPLATE_TEXT
    assert q.NAMESPACE_SINGLE == NAMESPACE_SINGLE
    assert q.NAMESPACE_MULTI == NAMESPACE_MULTI


def test_template_hash_is_pinned_and_recomputable():
    q = mod("question")
    assert q.QUESTION_TEMPLATE_SHA256 == TEMPLATE_SHA256
    recomputed = hashlib.sha256(
        json.dumps(
            [q.QUESTION_TEMPLATE, q.NAMESPACE_SINGLE, q.NAMESPACE_MULTI]
        ).encode()
    ).hexdigest()
    assert recomputed == TEMPLATE_SHA256


def test_template_drops_mitigation_submission_kubectl_and_conductor_lines():
    q = mod("question")
    lowered = q.QUESTION_TEMPLATE.lower()
    for word in ("mitigat", "kubectl", "conductor", "/submit", "curl"):
        assert word not in lowered, word


# --- rendering ----------------------------------------------------------------


def test_render_single_namespace_from_namespace_field():
    q = mod("question")
    app = _app(namespaces=())
    assert q.render_question(app) == _expected(
        app.app_name, "Namespace: zephyr-ns", app.descriptions
    )


def test_render_single_item_namespaces_uses_single_form():
    q = mod("question")
    app = _app(namespace="ignored-ns", namespaces=("zephyr-ns",))
    assert q.render_question(app) == _expected(
        app.app_name, "Namespace: zephyr-ns", app.descriptions
    )


def test_render_multi_namespace_form():
    q = mod("question")
    app = _app(namespace="zephyr-a", namespaces=("zephyr-a", "zephyr-b", "zephyr-c"))
    block = (
        "Namespaces: zephyr-a, zephyr-b, zephyr-c\n"
        "(This scenario spans multiple namespaces; investigate all of them.)"
    )
    assert q.render_question(app) == _expected(app.app_name, block, app.descriptions)


def test_descriptions_are_verbatim_including_braces_and_newlines():
    q = mod("question")
    desc = "Line one {app_name} {namespace_block} {0} %s\n  indented line two\t<tab>"
    out = q.render_question(_app(descriptions=desc))
    assert out == _expected("Zephyr Widgets", "Namespace: zephyr-ns", desc)


def test_question_depends_only_on_three_fields():
    q = mod("question")
    a = q.render_question(_app(stages=("diagnosis",)))
    b = q.render_question(_app(stages=("diagnosis", "mitigation")))
    assert a == b
    assert q.render_question(_app(app_name="Other Widgets")) != a
    assert q.render_question(_app(namespace="other-ns")) != a
    assert q.render_question(_app(descriptions="Different.")) != a


def test_question_ignores_problem_id_in_environment(monkeypatch):
    q = mod("question")
    canary = "zz_canary_fault_injected_problem"
    monkeypatch.setenv("SREGYM_PROBLEM_ID", canary)
    monkeypatch.setenv("PROBLEM_ID", canary)
    out = q.render_question(_app())
    assert canary not in out.lower()
    key = q.idempotency_key("r1", 4, 2)
    assert canary not in key.lower()


def _desc_for_total(q, total: int) -> str:
    base = len(q.render_question(_app(descriptions="")))
    return "x" * (total - base)


def test_question_at_limit_passes_one_over_fails():
    q = mod("question")
    ok = q.render_question(_app(descriptions=_desc_for_total(q, QUESTION_LIMIT)))
    assert len(ok) == QUESTION_LIMIT
    with pytest.raises(q.QuestionError) as ei:
        q.render_question(_app(descriptions=_desc_for_total(q, QUESTION_LIMIT + 1)))
    assert code_of(ei.value) == "QUESTION_TOO_LONG"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"app_name": ""},
        {"namespace": "", "namespaces": ()},
    ],
)
def test_incomplete_app_info(kwargs):
    q = mod("question")
    with pytest.raises(q.QuestionError) as ei:
        q.render_question(_app(**kwargs))
    assert code_of(ei.value) == "APP_INFO_INCOMPLETE"


# --- idempotency key ------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "want"),
    [
        (("r1", 1, 1), "sregym-r1-p01-t1"),
        (("r1", 3, 2), "sregym-r1-p03-t2"),
        (("smoke2", 17, 6), "sregym-smoke2-p17-t6"),
    ],
)
def test_idempotency_key_format(args, want):
    assert mod("question").idempotency_key(*args) == want


def test_idempotency_key_never_contains_problem_id():
    q = mod("question")
    up = mod("upstream")
    for pid in LITE_17:
        key = q.idempotency_key("r1", up.problem_seq(pid), 3)
        q.check_no_problem_id(pid, {"idempotency_key": key})
        assert pid not in key


# --- check_no_problem_id --------------------------------------------------------


SYNTH_PID = "zz_widget_cache_poison_alpha"


@pytest.mark.parametrize(
    "value",
    [
        f"diagnose {SYNTH_PID} now",
        SYNTH_PID.upper(),
        f"prefix-{SYNTH_PID.title()}-suffix",
    ],
)
def test_check_no_problem_id_hits_full_id_case_insensitive(value):
    q = mod("question")
    with pytest.raises(q.AnswerLeak) as ei:
        q.check_no_problem_id(SYNTH_PID, {"question": "clean text", "project": value})
    assert code_of(ei.value) == "PROBLEM_ID_IN:project"


def test_check_no_problem_id_ignores_fragments():
    q = mod("question")
    q.check_no_problem_id(
        SYNTH_PID,
        {
            "question": "The widget cache is poisoned? alpha release of widgets.",
            "idempotency_key": "sregym-r1-p05-t1",
            "env": "zz_widget_cache",
        },
    )


@pytest.mark.parametrize(
    "pid",
    [
        "duplicate_pvc_mounts_social_network",
        "network_policy_block",
        "env_variable_shadowing_astronomy_shop",
    ],
)
def test_check_no_problem_id_does_not_flag_app_words(pid):
    """App names like social network / astronomy shop are ordinary words."""
    q = mod("question")
    assert pid in LITE_17
    q.check_no_problem_id(
        pid,
        {
            "question": q.render_question(
                _app(
                    app_name="Social Network Astronomy Shop",
                    namespace="social-network",
                    descriptions="A social_network and network policy demo for the astronomy_shop.",
                )
            ),
            "project": "opspilot-sregym-r1",
        },
    )


def test_check_no_problem_id_names_the_field():
    q = mod("question")
    with pytest.raises(q.AnswerLeak) as ei:
        q.check_no_problem_id(
            SYNTH_PID, {"clean": "nothing", "LANGSMITH_PROJECT": f"x-{SYNTH_PID}"}
        )
    assert code_of(ei.value) == "PROBLEM_ID_IN:LANGSMITH_PROJECT"
