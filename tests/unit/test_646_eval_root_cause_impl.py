"""Implementation-side tests for #646 PR-B (conversion, C4, #647 plumbing)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.itbench_lite import answer, driver, select_batch, vocabulary
from scripts.itbench_lite.snapshot import load_snapshot

ROOT = Path(__file__).resolve().parents[2]


def _report(summary: str, claims: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "m0-report-v3",
        "assessment_status": "completed",
        "conclusion": "partial",
        "summary": summary,
        "claims": claims,
        "gaps": [],
        "next_steps": [],
        **extra,
    }


def _fact(text: str, eid: str = "ev-a") -> dict[str, Any]:
    return {
        "kind": "fact",
        "text": text,
        "evidence_ids": [eid],
        "target_refs": ["t"],
        "time_scope_ref": "w",
    }


EVIDENCE = [
    {"evidence_id": "ev-a", "operation": {"tool": "t", "query": {"id": 1}}},
    {"evidence_id": "ev-r", "operation": {"tool": "k8s", "query": {"kind": "pods"}}},
]


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("deployments", "Deployment"),
        ("Deployment", "Deployment"),
        ("deployment", "Deployment"),
        ("NODES", "Node"),
        ("clusterroles", "ClusterRole"),
        ("  pod ", "Pod"),
        ("FlagdConfig", "FlagdConfig"),
    ],
)
def test_normalise_kind(raw: str, kind: str) -> None:
    assert answer.normalise_kind(raw) == kind


def test_structured_root_reasoning_and_evidence_come_from_its_citations() -> None:
    report = _report(
        "checkout -> payment fails.",
        [_fact("checkout fails."), _fact("payment pods crash.", "ev-r")],
        root_cause={
            "kind": "pods",
            "name": "payment",
            "evidence_ids": ["ev-r", "ev-x"],
        },
    )
    result = answer.convert(report, EVIDENCE, ["checkout", "payment"])
    root = result.agent_output["entities"][0]
    assert root["name"] == "otel-demo/Pod/payment" and root["contributing_factor"]
    assert root["reasoning"] == "payment pods crash."
    assert root["evidence"].startswith("k8s ")
    assert result.meta["unresolved_evidence_ids"] == ["ev-x"]
    assert result.meta["rules"] == answer.STRUCTURED_RULES
    # The arrow end that names the root cause points at its entity.
    assert result.agent_output["propagations"] == [
        {
            "source": "otel-demo/Service/checkout",
            "target": "otel-demo/Pod/payment",
            "condition": "",
            "effect": "",
        }
    ]


def test_cluster_scoped_root_drops_a_given_namespace() -> None:
    report = _report(
        "node is down.",
        [_fact("node is down.")],
        root_cause={
            "kind": "Node",
            "namespace": "x",
            "name": "n1",
            "evidence_ids": ["ev-a"],
        },
    )
    assert answer.convert(report, EVIDENCE, []).meta["root_cause_entity"] == "Node/n1"


def test_heuristic_path_is_unchanged_for_v2() -> None:
    report = {
        **_report("payment fails.", [_fact("payment fails.")]),
        "schema_version": "m0-report-v2",
    }
    result = answer.convert(report, EVIDENCE, ["payment"])
    assert (
        result.meta["method"] == "heuristic" and result.meta["root_cause"] == "启发式"
    )
    assert result.agent_output["entities"][0]["contributing_factor"] is True


def test_snapshot_kind_index_decides_kind_and_namespace() -> None:
    report = _report("cart fails; redis too.", [_fact("cart fails; redis too.")])
    kinds = {
        "cart": [["Deployment", "shop"]],
        "redis": [["Deployment", "shop"], ["StatefulSet", "shop"]],
    }
    result = answer.convert(
        {**report, "root_cause": None}, EVIDENCE, ["cart", "redis"], kinds=kinds
    )
    names = [e["name"] for e in result.agent_output["entities"]]
    assert names == ["shop/Deployment/cart", "otel-demo/Service/redis"]
    assert result.meta["kind_rules"]["shop/Deployment/cart"] == "snapshot"


def _snapshot(root: Path) -> None:
    rows = [
        ({"metadata": {"name": "api", "namespace": "shop"}}, "deployments"),
        ({"metadata": {"name": "api", "namespace": "shop"}}, "services"),
        ({"metadata": {"name": "n1"}}, "nodes"),
        ({"metadata": {"name": "shop"}}, "namespaces"),
    ]
    (root / "k8s_objects_raw.tsv").write_text(
        "Body\tLogAttributes\n"
        + "".join(
            json.dumps(body) + "\t" + str({"k8s.resource.name": plural}) + "\n"
            for body, plural in rows
        )
    )


def test_build_kinds_and_cli(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    _snapshot(snap)
    assert vocabulary.build_kinds(load_snapshot(snap)) == {
        "api": [["Deployment", "shop"], ["Service", "shop"]],
        "n1": [["Node", None]],
    }
    out = tmp_path / "S1.txt"
    vocabulary.main(
        [str(snap), "--out", str(out), "--kinds-out", str(driver.kinds_file(out))]
    )
    assert out.read_text() == "api\nn1\n"
    assert driver.load_kinds(out) == {
        "api": [["Deployment", "shop"], ["Service", "shop"]],
        "n1": [["Node", None]],
    }
    assert driver.load_kinds(tmp_path / "S2.txt") is None


def test_select_batch_matches_the_task_record() -> None:
    tiers = json.loads(
        (ROOT / "docs/evidence/itbench-lite-sre-lite-full-1114/tiers.json").read_text()
    )
    selected = select_batch.select(tiers, "646:", 4)
    assert selected == {
        "指标+trace": ["S12", "S5", "S14", "S34"],
        "故障窗无 trace": ["S2", "S8", "S105", "S22"],
    }
    assert select_batch.scenarios_arg(selected) == "12,5,14,34,2,8,105,22"


def test_conversion_methods_counts_round_meta(tmp_path: Path) -> None:
    cfg = driver.Config(
        round="r", dataset=Path(), ground_truth=Path(), tmp_root=tmp_path
    )
    for label, method in (
        ("S1", "structured"),
        ("S2", "heuristic"),
        ("S3", "structured"),
    ):
        meta = cfg.round_dir / label / "trial-1" / "outputs" / "Scenario-1" / "1"
        meta.mkdir(parents=True)
        (meta / "conversion_meta.json").write_text(json.dumps({"method": method}))
    assert driver.conversion_methods(cfg) == {"heuristic": 1, "structured": 2}
