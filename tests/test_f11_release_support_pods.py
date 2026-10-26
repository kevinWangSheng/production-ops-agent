"""Regression guard for #323: ``pods_for`` names are unique within and across calls."""

from __future__ import annotations

from tests.f11_release_support import pods_for

AFTER_IMAGES = {"checkout": "sha256:" + "a" * 64}


def test_pods_for_names_are_unique_within_and_across_calls() -> None:
    first = {p.name for p in pods_for(AFTER_IMAGES, 64)}
    second = {p.name for p in pods_for(AFTER_IMAGES, 64)}

    assert len(first) == 64
    assert len(second) == 64
    assert first.isdisjoint(second)
