"""Acceptance tests for the Cloud-OpsBench adapter (#871), from contract r1.

Written from ``docs/tasks/2026-11-26-cloudopsbench-contract.md`` (U2-U5,
A2, A6, A7) and the task record's A15/A17/A21 rows, against the public
interface of ``scripts/cloudopsbench_eval`` only. Fixtures are synthetic;
no benchmark answer appears here. The two checks marked ``needs_checkout``
compare against a local read-only Cloud-OpsBench clone (``COB_CHECKOUT``)
and skip without one; the clone's code is parsed, never executed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from opspilot.investigation import limits
from scripts.cloudopsbench_eval import (
    answer,
    catalog,
    driver,
    question,
    scoring,
    subset,
    views,
)

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "cloudopsbench-871"
_CLONE = os.environ.get("COB_CHECKOUT", "")
CHECKOUT = Path(_CLONE) if _CLONE else None
needs_checkout = pytest.mark.skipif(
    CHECKOUT is None or not (CHECKOUT / "benchmark").is_dir(),
    reason="no local Cloud-OpsBench clone",
)

# Sentinels standing in for answer fields; none is a real answer.
GT_OBJECT = "app/zz-sentinel-object"
GT_LABEL = "zz_sentinel_label"


def _metadata(
    query: str = "Service Availability Disruption.",
    namespace: str = "boutique",
    category: str = "runtime",
    label: str = GT_LABEL,
) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "query": query,
        "difficulty": "zz-sentinel-difficulty",
        "result": {
            "fault_taxonomy": category,
            "fault_object": GT_OBJECT,
            "root_cause": label,
        },
    }


def _case_dir(root: Path) -> Path:
    case = root / "benchmark" / "boutique" / "codedefect" / "7"
    (case / "raw_data").mkdir(parents=True)
    (case / "code" / "frontend").mkdir(parents=True)
    (case / "tool_cache.json").write_text('{"k": "v"}')
    (case / "raw_data" / "logs.json").write_text("[]")
    (case / "code" / "frontend" / "main.go").write_text("package main\n")
    (case / "metadata.json").write_text(json.dumps(_metadata()))
    (case / "process-label").mkdir()
    (case / "process-label" / "milestone.json").write_text("{}")
    (case / "golden-trajectory").mkdir()
    (case / "golden-trajectory" / "t.json").write_text("{}")
    (case / "notes.txt").write_text("not part of the snapshot")
    return case


def _entries(view: Path) -> set[str]:
    return {str(p.relative_to(view)) for p in view.rglob("*")}


# --- A2: snapshot view -------------------------------------------------------


def test_view_holds_only_the_snapshot(tmp_path: Path) -> None:
    case = _case_dir(tmp_path / "ds")
    view = tmp_path / "view"
    views.build_view(case, view)
    top = {p.name for p in view.iterdir()}
    assert top <= {"tool_cache.json", "raw_data", "code"}
    assert {"tool_cache.json", "raw_data", "code"} <= top
    assert "raw_data/logs.json" in _entries(view)
    assert "code/frontend/main.go" in _entries(view)
    for name in _entries(view):
        assert not (
            {"metadata.json", "milestone.json", "process-label", "golden-trajectory"}
            & set(Path(name).parts)
        ), name
    views.check_view(view)


@pytest.mark.parametrize(
    "plant",
    ["milestone_in_raw", "metadata_in_code", "symlink_in_raw", "symlinked_cache"],
)
def test_view_refuses_answer_names_and_links_inside_snapshot(
    tmp_path: Path, plant: str
) -> None:
    case = _case_dir(tmp_path / "ds")
    secret = tmp_path / "outside.json"
    secret.write_text(json.dumps(_metadata()))
    if plant == "milestone_in_raw":
        (case / "raw_data" / "milestone.json").write_text("{}")
    elif plant == "metadata_in_code":
        (case / "code" / "frontend" / "metadata.json").write_text("{}")
    elif plant == "symlink_in_raw":
        (case / "raw_data" / "extra.json").symlink_to(secret)
    else:
        (case / "tool_cache.json").unlink()
        (case / "tool_cache.json").symlink_to(secret)
    view = tmp_path / "view"
    with pytest.raises(views.ViewError):
        views.build_view(case, view)
        # If building did not refuse, the re-check must.
        views.check_view(view)


def test_check_view_refuses_planted_answer(tmp_path: Path) -> None:
    case = _case_dir(tmp_path / "ds")
    view = tmp_path / "view"
    views.build_view(case, view)
    (view / "metadata.json").write_text("{}")
    with pytest.raises(views.ViewError):
        views.check_view(view)


def test_question_built_from_query_and_namespace_only(tmp_path: Path) -> None:
    case = _case_dir(tmp_path / "ds")
    query, namespace = views.read_question_fields(case)
    assert (query, namespace) == ("Service Availability Disruption.", "boutique")
    text = question.render_question("boutique", query=query, namespace=namespace)
    for leaked in (
        GT_OBJECT,
        GT_LABEL,
        "zz-sentinel-difficulty",
        "codedefect/7",
        "metadata",
    ):
        assert leaked not in text


# --- U2: question ------------------------------------------------------------


@pytest.mark.parametrize("system", ["boutique", "train-ticket"])
def test_question_carries_closed_lists_and_fits_limit(system: str) -> None:
    ns = catalog.VALID_NAMESPACES[system][0]
    longest_query = "x" * 39  # the longest real query is 39 characters
    text = question.render_question(system, query=longest_query, namespace=ns)
    assert longest_query in text and ns in text
    assert len(catalog.LABELS) == 57
    for label in catalog.LABELS:
        assert label in text
    for name in (
        *catalog.VALID_NODES,
        *catalog.VALID_SERVICES[system],
        *catalog.VALID_NAMESPACES[system],
    ):
        assert name in text
    assert len(text) <= 8192


def test_question_template_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    assert question.template_sha256() == question.QUESTION_TEMPLATE_SHA256
    # The pin covers the template: any edit to it changes the digest.
    monkeypatch.setattr(question, "QUESTION_TEMPLATE", question.QUESTION_TEMPLATE + " ")
    assert question.template_sha256() != question.QUESTION_TEMPLATE_SHA256
    monkeypatch.undo()
    a = question.render_question("boutique", query="q.", namespace="boutique")
    assert a == question.render_question("boutique", query="q.", namespace="boutique")


@needs_checkout
def test_closed_lists_match_benchmark_verbatim() -> None:
    src = (CHECKOUT / "agents/cloudops_agent/runtime/contracts.py").read_text()
    values: dict[str, Any] = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    assert list(catalog.VALID_NODES) == values["VALID_NODES"]
    for system, names in values["SYSTEM_VALID_SERVICES"].items():
        assert list(catalog.VALID_SERVICES[system]) == names
    assert catalog.ROOT_CAUSE_LIST.strip() == values["root_cause_list_str"].strip()


# --- U3 / A15 / A7: report -> answer -----------------------------------------


def _report(
    summary: str,
    root_cause: dict[str, Any] | None = None,
    claims: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "m0-report-v3",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": summary,
        "claims": claims or [],
        "gaps": [],
        "next_steps": [],
        "root_cause": root_cause,
    }


def _rc(kind: str, name: str, namespace: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "evidence_ids": ["ev-1"],
    }


def _claim(kind: str, text: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": text,
        "evidence_ids": [],
        "target_refs": [],
        "time_scope_ref": None,
    }


@pytest.mark.parametrize(
    ("system", "rc", "expected"),
    [
        (
            "boutique",
            _rc("Pod", "checkoutservice-7d9f8b6c5d-x2k4q", "boutique"),
            "app/checkoutservice",
        ),
        (
            "boutique",
            _rc("ReplicaSet", "redis-cart-6c8f7b9d4f", "boutique"),
            "app/redis-cart",
        ),
        ("boutique", _rc("Deployment", "frontend", "boutique"), "app/frontend"),
        (
            "train-ticket",
            _rc("Pod", "ts-food-delivery-service-5f6d7c8b9a-abcde", "train-ticket"),
            "app/ts-food-delivery-service",
        ),
        (
            "train-ticket",
            _rc("Pod", "ts-travel2-service-6b8c9d7f5d-q8w2e", "train-ticket"),
            "app/ts-travel2-service",
        ),
        (
            "train-ticket",
            _rc("Service", "ts-auth-service-mongo", "train-ticket"),
            "app/ts-auth-service",
        ),
        ("boutique", _rc("Node", "worker-02"), "node/worker-02"),
        ("boutique", _rc("Namespace", "boutique"), "namespace/boutique"),
        (
            "train-ticket",
            _rc("ResourceQuota", "rq-1", "train-ticket"),
            "namespace/train-ticket",
        ),
    ],
)
def test_fault_object_from_root_cause(
    system: str, rc: dict[str, Any], expected: str
) -> None:
    out = answer.convert(_report("Root cause: pod_cpu_overload", rc), system)
    assert out.fault_object == expected


def test_fault_object_from_summary_when_no_root_cause() -> None:
    out = answer.convert(
        _report("The fault object: app/paymentservice. Root cause: pod_cpu_overload"),
        "boutique",
    )
    assert out.fault_object == "app/paymentservice"


def test_object_outside_list_b_not_found() -> None:
    out = answer.convert(
        _report(
            "Root cause: pod_cpu_overload", _rc("Pod", "unknown-svc-7d9f8b6c5d-x2k4q")
        ),
        "boutique",
    )
    assert not out.fault_object


def test_label_case_insensitive_and_from_closed_list() -> None:
    out = answer.convert(
        _report("Root cause: CODE_WRONG_RETURN", _rc("Deployment", "frontend")),
        "boutique",
    )
    assert out.root_cause.lower() == "code_wrong_return"


@pytest.mark.parametrize(
    "summary",
    [
        "Root cause: disk_full",
        "Root cause: code_busy_loops",
        "Root cause: xcode_busy_loop",
    ],
)
def test_label_must_match_whole_label_else_not_found(summary: str) -> None:
    out = answer.convert(_report(summary, _rc("Deployment", "frontend")), "boutique")
    assert not out.root_cause
    case = answer.case_file(out, [])
    preds = json.dumps(case.get("final_answer"))
    for label in catalog.LABELS:
        assert f'"{label}"' not in preds


def test_label_ignores_rejected_counter_and_recommendation_claims() -> None:
    claims = [
        _claim("rejected_hypothesis", "Not code_busy_loop."),
        _claim("counter_evidence", "Rules out code_memory_leak."),
        _claim("recommendation", "Check for pod_cpu_overload."),
        _claim("hypothesis", "Likely code_artificial_delay."),
    ]
    out = answer.convert(
        _report("Latency on frontend.", _rc("Deployment", "frontend"), claims),
        "boutique",
    )
    assert out.root_cause == "code_artificial_delay"


def test_only_ignored_claims_give_no_label() -> None:
    claims = [
        _claim("rejected_hypothesis", "Not code_busy_loop."),
        _claim("counter_evidence", "Rules out code_memory_leak."),
    ]
    out = answer.convert(
        _report("Latency on frontend.", _rc("Deployment", "frontend"), claims),
        "boutique",
    )
    assert not out.root_cause


def test_conversion_deterministic() -> None:
    rep = _report(
        "Root cause: pod_network_delay; fault object: app/cartservice",
        _rc("Pod", "cartservice-7d9f8b6c5d-x2k4q", "boutique"),
    )
    assert answer.convert(rep, "boutique") == answer.convert(
        json.loads(json.dumps(rep)), "boutique"
    )


def test_failed_run_has_no_final_answer() -> None:
    assert answer.case_file(None, [])["final_answer"] is None


def test_conversion_record_keeps_no_predicted_values() -> None:
    out = answer.convert(
        _report(
            "Root cause: pod_network_delay", _rc("Pod", "cartservice-7d9f8b6c5d-x2k4q")
        ),
        "boutique",
    )
    text = json.dumps(answer.conversion_record(out))
    assert "cartservice" not in text and "pod_network_delay" not in text


# --- U5 / A21: subset ----------------------------------------------------------


def _strata() -> dict[str, str]:
    sizes = {
        "boutique/runtime/a": 30,
        "boutique/runtime/b": 3,
        "boutique/service/c": 1,
        "trainticket/service/d": 12,
        "trainticket/startup/e": 2,
    }
    out: dict[str, str] = {}
    n = 0
    for stratum, size in sizes.items():
        system, category, _ = stratum.split("/")
        for _ in range(size):
            n += 1
            out[f"{system}/{category}/{n}"] = stratum
    return out


def test_subset_deterministic_covers_every_stratum_and_hits_target() -> None:
    strata = _strata()
    chosen = subset.select(strata, seed=871, target=20)
    reordered = dict(reversed(list(strata.items())))
    assert chosen == subset.select(reordered, seed=871, target=20)
    assert len(chosen) == 20 == len(set(chosen))
    assert set(strata.values()) == {strata[c] for c in chosen}
    for stratum in set(strata.values()):
        held = sum(1 for v in strata.values() if v == stratum)
        assert sum(1 for c in chosen if strata[c] == stratum) <= held


def test_subset_reads_only_metadata_strata(tmp_path: Path) -> None:
    for n, label in (
        (1, "pod_cpu_overload"),
        (2, "pod_cpu_overload"),
        (3, "pod_network_delay"),
    ):
        d = tmp_path / "benchmark" / "boutique" / "runtime" / str(n)
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(json.dumps(_metadata(label=label)))
    assert subset.read_strata(tmp_path) == {
        "boutique/runtime/1": "boutique/runtime/pod_cpu_overload",
        "boutique/runtime/2": "boutique/runtime/pod_cpu_overload",
        "boutique/runtime/3": "boutique/runtime/pod_network_delay",
    }


def test_committed_subset_matches_pin() -> None:
    raw = (EVIDENCE / "subset.json").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    # Task record A21: sha256 cf14c665...48bd93.
    assert digest.startswith("cf14c665") and digest.endswith("48bd93")
    cases, sha = driver.load_subset(EVIDENCE / "subset.json")
    assert sha == digest
    data = json.loads(raw)
    assert (data["seed"], data["target"]) == (871, 100)
    assert len(cases) == 100 == len(set(cases))
    for case_id in cases:
        views.CaseRef.parse(case_id)
    strata = json.loads((EVIDENCE / "strata.json").read_text())
    assert len(strata["strata"]) == 58
    assert all(row["subset"] >= 1 for row in strata["strata"])
    assert sum(row["subset"] for row in strata["strata"]) == 100
    assert sum(row["full"] for row in strata["strata"]) == strata["full_total"] == 754


@needs_checkout
def test_committed_subset_reproducible_from_benchmark() -> None:
    strata = subset.read_strata(CHECKOUT)
    cases, _ = driver.load_subset(EVIDENCE / "subset.json")
    assert sorted(subset.select(strata, seed=871, target=100)) == sorted(cases)


# --- A6 / A17: frozen summary and isolation -------------------------------------


def test_round_summary_has_no_predictions_or_answers() -> None:
    cfg = driver.Config(
        round="t1", checkout=Path("/nonexistent"), cob_python=Path("/nonexistent")
    )
    case = "boutique/runtime/1"
    predicted = answer.Converted(
        fault_object="app/zz-predicted-object",
        root_cause="zz_predicted_label",
        object_rule="root_cause",
        label_rule="summary_root_cause",
    )
    record = {
        "conversion": answer.conversion_record(predicted),
        "converted": {
            "fault_object": predicted.fault_object,
            "root_cause": predicted.root_cause,
        },
        "idempotency_key": question.idempotency_key("t1", 1),
    }
    metrics = {k: 0 for k in scoring.METRICS}
    summary = driver.round_summary(
        cfg=cfg,
        line="opspilot",
        cases=[case],
        scores={case: {"metrics": metrics, "error": None}},
        records={case: record},
        strata={case: f"boutique/runtime/{GT_LABEL}"},
        prices=None,
        subset_sha256="0" * 64,
    )
    text = json.dumps(summary)
    for leaked in ("zz-predicted-object", "zz_predicted_label", GT_LABEL, GT_OBJECT):
        assert leaked not in text


def test_idempotency_key_has_position_not_case_id() -> None:
    assert question.idempotency_key("r1", 1) == "cob-r1-c001-t1"
    assert question.idempotency_key("r1", 42, 2) == "cob-r1-c042-t2"


def test_product_env_refuses_dataset_path_or_case_id() -> None:
    forbidden = ["/home/x/cob/benchmark", "boutique/runtime/1"]
    driver.check_product_env({"A": "1", "B": "/opt/view-c001"}, forbidden)
    for env in (
        {"OPSPILOT_X": "/home/x/cob/benchmark/boutique"},
        {"Y": "case boutique/runtime/1"},
    ):
        with pytest.raises(driver.DriverError):
            driver.check_product_env(env, forbidden)


def test_leak_markers_detected_in_model_input() -> None:
    assert driver.leak_hits("read metadata.json and milestone") != []
    assert driver.leak_hits("GetResources pods -n boutique") == []


# --- U4: loop cap unchanged ------------------------------------------------------


def test_product_loop_cap_unchanged() -> None:
    import scripts.cloudopsbench_eval.product_entry  # noqa: F401
    import scripts.cloudopsbench_eval.profile  # noqa: F401

    assert limits.MAX_MODEL_REQUESTS_PER_RUN == 100
