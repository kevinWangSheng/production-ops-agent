"""#666 PR-A contract: scoring (H-17, H-18).

Independent contract tests written from the task record's PR-A public
interface and the pre-review, without reading ``scripts/holmes_evals/``.
Red until the implementation lands; collection stays green (lazy import).

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* the ``multi_cluster``, ``time_box_limited`` and ``excluding_env_broken``
  groups have the same ``passes``/``denominator`` keys as ``main``;
* a question belongs to ``time_box_limited`` when its trials carry
  ``time_box_limited=True``; to ``multi_cluster`` when it is in
  ``questions.MULTI_CLUSTER``;
* ``excluding_env_broken`` is tested only with a question whose three main
  trials are all ``env_broken`` (a partly broken question is not fixed by the
  spec).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.holmes_evals_support import mod

A = "183c_elasticsearch_log_search"
B = "185_elasticsearch_cross_region_search"  # time-box limited (H-19)
C = "262_ambiguous_cluster_reference"  # multi-cluster
D = "267_cluster_name_alias"  # multi-cluster, env broken
QUESTIONS = [A, B, C, D]


def _t(qid, index, passed, category="ok", time_box=False):
    return mod("scoring").Trial(
        qid=qid,
        index=index,
        passed=passed,
        category=category,
        time_box_limited=time_box,
    )


def _main_trials():
    return [
        _t(A, 1, True),
        _t(A, 2, True),
        _t(A, 3, True),
        _t(B, 1, True, time_box=True),
        _t(B, 2, False, time_box=True),
        _t(B, 3, False, time_box=True),
        _t(C, 1, True),
        _t(C, 2, True),
        _t(C, 3, False),
        _t(D, 1, False, "env_broken"),
        _t(D, 2, False, "env_broken"),
        _t(D, 3, False, "env_broken"),
    ]


def _reruns():
    return [
        _t(B, 4, True, time_box=True),
        _t(B, 5, True, time_box=True),
        _t(B, 6, True, time_box=True),
        _t(C, 4, True),
        _t(C, 5, False),
        _t(C, 6, True),
    ]


def test_mc_fixture_matches_constant():
    questions = mod("questions")
    assert {C, D} <= questions.MULTI_CLUSTER
    assert not {A, B} & questions.MULTI_CLUSTER


def test_main_score_uses_first_three_trials_only():
    scoring = mod("scoring")
    result = scoring.score(_main_trials() + _reruns(), QUESTIONS)
    main = result["main"]
    assert main["passes"] == 6
    assert main["denominator"] == len(QUESTIONS) * 3
    assert main["rate"] == pytest.approx(6 / 12)


def test_reruns_reported_separately_and_do_not_move_main():
    scoring = mod("scoring")
    without = scoring.score(_main_trials(), QUESTIONS)
    with_reruns = scoring.score(_main_trials() + _reruns(), QUESTIONS)
    assert with_reruns["main"] == without["main"]
    assert with_reruns["rerun"] != without["rerun"]


def test_swing_questions_pass_one_or_two_of_three():
    scoring = mod("scoring")
    result = scoring.score(_main_trials() + _reruns(), QUESTIONS)
    assert set(result["swing_questions"]) == {B, C}


def test_failure_categories_count_zero_and_stay_in_denominator():
    scoring = mod("scoring")
    trials = [
        _t(A, 1, True),
        _t(A, 2, False, "no_report"),
        _t(A, 3, False, "judge_exhausted"),
        _t(B, 1, False, "env_broken"),
        _t(B, 2, True),
        _t(B, 3, True),
    ]
    result = scoring.score(trials, [A, B])
    assert result["main"]["passes"] == 3
    assert result["main"]["denominator"] == 6


def test_denominator_is_fixed_by_question_list():
    scoring = mod("scoring")
    # C has no trial at all; D has only one.
    trials = [_t(A, 1, True), _t(A, 2, True), _t(A, 3, True), _t(D, 1, True)]
    result = scoring.score(trials, [A, C, D])
    assert result["main"]["denominator"] == 9
    assert result["main"]["passes"] == 4


def test_per_question_covers_every_question():
    scoring = mod("scoring")
    result = scoring.score(_main_trials(), QUESTIONS)
    per_q = result["per_question"]
    if isinstance(per_q, Mapping):
        assert set(per_q) == set(QUESTIONS)
    else:
        rendered = [str(item) for item in per_q]
        for qid in QUESTIONS:
            assert any(qid in r for r in rendered), qid


def test_multi_cluster_group():
    scoring = mod("scoring")
    group = scoring.score(_main_trials() + _reruns(), QUESTIONS)["multi_cluster"]
    assert group["denominator"] == 6
    assert group["passes"] == 2


def test_time_box_limited_group():
    scoring = mod("scoring")
    group = scoring.score(_main_trials() + _reruns(), QUESTIONS)["time_box_limited"]
    assert group["denominator"] == 3
    assert group["passes"] == 1


def test_excluding_env_broken_drops_broken_question():
    scoring = mod("scoring")
    result = scoring.score(_main_trials() + _reruns(), QUESTIONS)
    group = result["excluding_env_broken"]
    assert group["denominator"] == 9
    assert group["passes"] == 6
    # The main score still keeps the broken question in its denominator.
    assert result["main"]["denominator"] == 12
