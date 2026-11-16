"""Synthetic public-interface contract tests for ITBench-Lite #646/#647."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.itbench_lite import answer, vocabulary
from scripts.itbench_lite.snapshot import load_snapshot


def _claim(
    kind: str, text: str, evidence_ids: tuple[str, ...] = ("ev-a",)
) -> dict[str, Any]:
    has_evidence = bool(evidence_ids)
    return {
        "kind": kind,
        "text": text,
        "evidence_ids": list(evidence_ids),
        "target_refs": ["target-a"] if has_evidence else [],
        "time_scope_ref": "window-a" if has_evidence else None,
    }


def _report(
    summary: str,
    claims: list[dict[str, Any]],
    *,
    root_cause: object = "omitted",
    conclusion: str = "supported",
    assessment_status: str = "completed",
    gaps: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "m0-report-v3" if root_cause != "omitted" else "m0-report-v2",
        "assessment_status": assessment_status,
        "conclusion": conclusion,
        "summary": summary,
        "claims": claims,
        "gaps": gaps or [],
        "next_steps": [],
    }
    if root_cause != "omitted":
        result["root_cause"] = root_cause
    return result


def _root_cause(
    *,
    kind: str = "service",
    name: str = "payment",
    namespace: str | None = "ops",
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "evidence_ids": evidence_ids if evidence_ids is not None else ["ev-root"],
    }


def _evidence(*ids: str) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": evidence_id,
            "operation": {"tool": "synthetic_read", "query": {"id": evidence_id}},
        }
        for evidence_id in ids
    ]


def _entity(output: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entity for entity in output["entities"] if entity["name"] == name)


def test_structured_root_cause_overrides_l2_and_marks_only_it_contributing() -> None:
    report = _report(
        "checkout is failing; payment is the causal component.",
        [
            _claim("fact", "checkout error rate increased."),
            _claim("fact", "payment requests fail.", ("ev-root",)),
        ],
        root_cause=_root_cause(kind="SERVICE", name="payment", namespace="ops"),
    )
    result = answer.convert(
        report,
        _evidence("ev-a", "ev-root"),
        ["checkout", "payment"],
    )

    assert result.meta["method"] == "structured"
    assert result.meta["root_cause_in_vocabulary"] is True
    assert (
        _entity(result.agent_output, "ops/Service/payment")["contributing_factor"]
        is True
    )
    assert (
        _entity(result.agent_output, "otel-demo/Service/checkout")[
            "contributing_factor"
        ]
        is False
    )
    assert (
        sum(entity["contributing_factor"] for entity in result.agent_output["entities"])
        == 1
    )


def test_structured_null_root_cause_does_not_fall_back_to_l2() -> None:
    report = _report(
        "payment is the first mentioned entity and appears unhealthy.",
        [_claim("fact", "payment error rate is elevated.")],
        root_cause=None,
    )
    result = answer.convert(report, _evidence("ev-a"), ["payment"])
    assert result.meta["method"] == "structured"
    assert result.meta["root_cause_entity"] is None
    assert (
        _entity(result.agent_output, "otel-demo/Service/payment")["contributing_factor"]
        is False
    )


def test_missing_root_cause_key_uses_heuristic_l2() -> None:
    report = _report(
        "payment is the first mentioned entity and appears unhealthy.",
        [_claim("fact", "payment error rate is elevated.")],
    )
    result = answer.convert(report, _evidence("ev-a"), ["payment"])
    assert result.meta["method"] == "heuristic"
    assert (
        _entity(result.agent_output, "otel-demo/Service/payment")["contributing_factor"]
        is True
    )


def test_inconclusive_structured_root_cause_is_ignored() -> None:
    report = _report(
        "payment may be involved.",
        [_claim("hypothesis", "payment is a possible cause.", ())],
        root_cause=_root_cause(),
        conclusion="inconclusive",
        assessment_status="incomplete",
        gaps=["No synthetic trace was available."],
    )
    result = answer.convert(report, _evidence("ev-root"), ["payment"])
    assert result.meta["method"] == "structured"
    assert result.meta["root_cause_entity"] is None
    assert all(
        entity["contributing_factor"] is False
        for entity in result.agent_output["entities"]
    )


def test_unknown_structured_root_cause_is_written_and_reported_in_meta() -> None:
    report = _report(
        "checkout is failing.",
        [_claim("fact", "checkout error rate increased.")],
        root_cause=_root_cause(kind="DEPLOYMENTS", name="redis-main", namespace="ops"),
    )
    result = answer.convert(report, _evidence("ev-a", "ev-root"), ["checkout"])
    assert result.meta["method"] == "structured"
    assert result.meta["root_cause_in_vocabulary"] is False
    assert (
        _entity(result.agent_output, "ops/Deployment/redis-main")["contributing_factor"]
        is True
    )


def test_explicit_root_cause_is_kept_even_when_name_is_namespace_excluded() -> None:
    report = _report(
        "checkout is failing.",
        [_claim("fact", "checkout error rate increased.")],
        root_cause=_root_cause(name="otel-demo", namespace="ops"),
    )
    result = answer.convert(
        report, _evidence("ev-a", "ev-root"), ["checkout", "otel-demo"]
    )
    assert (
        _entity(result.agent_output, "ops/Service/otel-demo")["contributing_factor"]
        is True
    )


def test_structured_root_cause_kind_normalises_plural_case_and_deduplicates_name() -> (
    None
):
    report = _report(
        "payment is failing.",
        [_claim("fact", "payment has repeated errors.")],
        root_cause=_root_cause(kind="DEPLOYMENTS", name="payment", namespace="ops"),
    )
    result = answer.convert(report, _evidence("ev-a", "ev-root"), ["payment"])
    names = [entity["name"] for entity in result.agent_output["entities"]]
    assert names.count("ops/Deployment/payment") == 1
    assert (
        _entity(result.agent_output, "ops/Deployment/payment")["contributing_factor"]
        is True
    )


def test_empty_namespace_omits_cluster_scope_and_defaults_namespaced_kind() -> None:
    report = _report(
        "node-a is unhealthy and payment requests fail.",
        [_claim("fact", "payment error rate increased.")],
        root_cause=_root_cause(kind="NODES", name="node-a", namespace=""),
    )
    result = answer.convert(report, _evidence("ev-a", "ev-root"), ["node-a", "payment"])
    assert _entity(result.agent_output, "Node/node-a")["contributing_factor"] is True
    assert (
        _entity(result.agent_output, "otel-demo/Service/payment")["contributing_factor"]
        is False
    )


def _write_snapshot(root: Path) -> None:
    (root / "k8s_objects_raw.tsv").write_text(
        "Body\tLogAttributes\n"
        + "\n".join(
            [
                (
                    json.dumps({"metadata": {"name": "team-a"}})
                    + "\t"
                    + "{'k8s.resource.name': 'namespaces'}"
                ),
                (
                    json.dumps({"metadata": {"name": "team-a", "namespace": "ops"}})
                    + "\t"
                    + "{'k8s.resource.name': 'configmaps'}"
                ),
                (
                    json.dumps({"metadata": {"name": "api", "namespace": "team-a"}})
                    + "\t"
                    + "{'k8s.resource.name': 'deployments'}"
                ),
                (
                    json.dumps({"metadata": {"name": "ops", "namespace": "ops"}})
                    + "\t"
                    + "{'k8s.resource.name': 'services'}"
                ),
                (
                    json.dumps({"metadata": {"name": "worker", "namespace": "team-a"}})
                    + "\t"
                    + "{'k8s.resource.name': 'pods'}"
                ),
                (
                    json.dumps({"metadata": {"name": "api", "namespace": "other"}})
                    + "\t"
                    + "{'k8s.resource.name': 'services'}"
                ),
                (
                    json.dumps({"metadata": {"name": "other", "namespace": "ops"}})
                    + "\t"
                    + "{'k8s.resource.name': 'configmaps'}"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "otel_traces_raw.tsv").write_text(
        "ServiceName\nteam-a\ntrace-app\n", encoding="utf-8"
    )
    (root / "otel_logs_raw.tsv").write_text(
        "ServiceName\tResourceAttributes\nops\t{'service.name': 'log-app'}\n",
        encoding="utf-8",
    )


def test_vocabulary_excludes_namespace_names_from_every_object_type_and_source(
    tmp_path: Path,
) -> None:
    _write_snapshot(tmp_path)
    names = vocabulary.build(load_snapshot(tmp_path))
    assert names == ["api", "log-app", "trace-app", "worker"]
    assert "team-a" not in names
    assert "ops" not in names
    assert "other" not in names
