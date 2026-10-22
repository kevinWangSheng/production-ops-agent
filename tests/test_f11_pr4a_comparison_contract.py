"""No-database contract tests for F11 PR-4a's pure public interface.

Written from ``docs/tasks/2026-10-21-f11-pr4-contract.md`` 第 4 节 and S1-S6
without reading the implementation. Only names the contract gives are used.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from tests.f11_release_support import AFTER_HASH, BEFORE_HASH, SERVICE
from tests.test_f11_release_evaluation import (
    AFTER_IMAGES,
    RELEASED_AT,
    WINDOW_END,
    ReleaseAuxiliary,
    _context,
)

try:
    from opspilot.observation.release_comparison import (
        build_release_comparison_plan,
        evaluate_release_comparison,
    )

    _MISSING: str | None = None
except ImportError as exc:  # implementation not merged yet: fail, do not error
    _MISSING = f"{type(exc).__name__}: {exc}"

OLD_RS = f"{SERVICE}-{BEFORE_HASH}"


def pr4a(test: Callable[..., None]) -> Callable[..., None]:
    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        if _MISSING is not None:
            pytest.fail(f"PR4A_INTERFACE_MISSING: {_MISSING}", pytrace=False)
        test(*args, **kwargs)

    return run


def _plan(context: Any, **kw: Any) -> dict[str, str]:
    kw.setdefault("released_at", RELEASED_AT)
    kw.setdefault("after_window_end", WINDOW_END)
    return build_release_comparison_plan(context, **kw)


@pr4a
def test_the_public_functions_exist() -> None:
    assert callable(build_release_comparison_plan)
    assert callable(evaluate_release_comparison)


@pr4a
def test_the_plan_is_a_deterministic_nonempty_bounded_text_map() -> None:
    context = _context()

    plan = _plan(context)

    assert plan == _plan(context)
    assert 0 < len(plan) <= 64  # S6: about 49 queries per attempt
    assert all(isinstance(k, str) and isinstance(v, str) and v for k, v in plan.items())
    assert len(set(plan.values())) >= 1


@pr4a
def test_the_plan_names_the_before_replicaset_not_the_after_one() -> None:
    plan = _plan(_context())

    texts = " ".join(plan.values())
    assert f'"{OLD_RS}"' in texts
    # after scopes are the anomaly sample's: the new ReplicaSet is not
    # re-queried by this plan's before-side
    before_side = [t for t in plan.values() if f'"{OLD_RS}"' in t]
    assert before_side
    assert all(f'"{SERVICE}-{AFTER_HASH}"' not in t for t in before_side)


@pr4a
def test_the_plan_depends_on_released_at_only_through_the_inputs_it_is_given() -> None:
    context = _context()

    first = _plan(context)
    later = _plan(context, released_at=RELEASED_AT + timedelta(hours=1))

    # the evaluation instant is the caller's (S1); the plan is a pure function
    assert first == _plan(context)
    assert set(later) == set(first)


@pr4a
def test_s3_a_missing_before_hash_plans_no_old_replicaset_query() -> None:
    context = _context(
        auxiliary=ReleaseAuxiliary(
            before_pod_template_hash=None, after_pod_template_hash=AFTER_HASH
        )
    )

    plan = _plan(context)

    assert plan, "the whole-service before queries remain"
    assert not any("kube_replicaset_owner" in t for t in plan.values())


@pr4a
def test_s3_equal_hashes_plan_no_old_replicaset_query() -> None:
    context = _context(
        auxiliary=ReleaseAuxiliary(
            before_pod_template_hash=AFTER_HASH, after_pod_template_hash=AFTER_HASH
        )
    )

    plan = _plan(context)

    assert plan
    assert not any(f'"{OLD_RS}"' in t for t in plan.values())


@pr4a
def test_s3_equal_images_with_different_hashes_still_plan_the_old_slice() -> None:
    context = _context(before_images=AFTER_IMAGES)

    plan = _plan(context)

    assert any(f'"{OLD_RS}"' in t for t in plan.values())


@pr4a
def test_evaluating_without_any_result_is_unavailable_not_an_error() -> None:
    context = _context()
    plan = _plan(context)

    try:
        comparison = evaluate_release_comparison(context, plan, {})
    except (TypeError, AttributeError):
        comparison = evaluate_release_comparison(context, plan, ())

    assert comparison.status == "unavailable"
    assert comparison.reason_codes
    assert list(comparison.reason_codes) == sorted(comparison.reason_codes)
    assert not [s for s in comparison.signals if getattr(s, "value", None) is not None]
