"""Deterministic entity matching (L8) against hand-written synthetic answers."""

from __future__ import annotations

import pytest

from scripts.itbench_lite import entity_match as em

# Synthetic answer: fictional services, not taken from any dataset file.
GT = {
    "groups": [
        {
            "id": "alpha-svc",
            "kind": "Service",
            "filter": [".*alpha\\b"],
            "root_cause": True,
        },
        {"id": "alpha-pod", "kind": "Pod", "filter": ["alpha-[a-z0-9]+-[a-z0-9]+"]},
        {
            "id": "beta-cm",
            "kind": "ConfigMap",
            "filter": ["^beta-settings$"],
            "root_cause": True,
        },
        {"id": "gamma-svc", "kind": "Service", "filter": [".*gamma\\b"]},
    ],
    "aliases": [["alpha-svc", "alpha-pod"]],
    "propagations": [],
}


def out(*entities):
    return {
        "entities": [{"name": n, "contributing_factor": cf} for n, cf in entities],
        "propagations": [],
        "alerts_explained": [],
    }


def test_filter_and_kind_both_required():
    assert em.entity_matches("ns/Service/alpha", GT["groups"][0])
    assert not em.entity_matches("ns/Deployment/alpha", GT["groups"][0])
    assert not em.entity_matches("ns/Service/alphabet", GT["groups"][0])
    assert em.entity_matches("ns/configmap/beta-settings", GT["groups"][2])


def test_invalid_regex_is_skipped():
    assert not em.entity_matches("ns/Service/x", {"kind": "Service", "filter": ["("]})


def test_alias_counts_as_root_cause_once_for_recall():
    result = em.score(
        out(("ns/Pod/alpha-7f9-x2", True), ("ns/Service/alpha", True)), GT
    )
    assert result["gt_count"] == 2
    assert [p["matches_gt"] for p in result["predicted"]] == [True, True]
    assert result["precision"] == 1.0
    assert result["recall"] == 0.5


def test_perfect_answer_passes():
    result = em.score(
        out(("ns/Service/alpha", True), ("ns/ConfigMap/beta-settings", True)), GT
    )
    assert result["f1"] == 1.0 and result["pass"] is True
    assert result["k@1_recall"] == 0.5


def test_non_root_cause_entities_are_ignored_and_wrong_answer_scores_zero():
    result = em.score(out(("ns/Service/alpha", False), ("ns/Service/gamma", True)), GT)
    assert [p["entity"] for p in result["predicted"]] == ["ns/Service/gamma"]
    assert result["f1"] == 0.0 and result["pass"] is False


def test_excluded_namespaces_match_official_list():
    assert "kube-system" in em.DEFAULT_EXCLUDED_NAMESPACES
    assert len(em.DEFAULT_EXCLUDED_NAMESPACES) == 9
    result = em.score(
        out(("prometheus/Service/alpha", True), ("ns/Service/alpha", True)), GT
    )
    assert [p["entity"] for p in result["predicted"]] == ["ns/Service/alpha"]


def test_empty_output_scores_zero():
    result = em.score({"entities": [], "propagations": [], "alerts_explained": []}, GT)
    assert result["predicted"] == [] and result["f1"] == 0.0


@pytest.mark.parametrize(
    ("predicted", "k", "precision", "recall"),
    [
        ([("ns/Service/gamma", True), ("ns/Service/alpha", True)], 1, 0.0, 0.0),
        ([("ns/Service/gamma", True), ("ns/Service/alpha", True)], 2, 0.5, 0.5),
    ],
)
def test_at_k(predicted, k, precision, recall):
    result = em.score(out(*predicted), GT)
    assert result[f"k@{k}_precision"] == precision
    assert result[f"k@{k}_recall"] == recall
