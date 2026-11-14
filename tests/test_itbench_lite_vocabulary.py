"""#605: entity vocabulary generator over a synthetic scenario copy."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.itbench_lite import answer, vocabulary
from scripts.itbench_lite.snapshot import AnswerFilePresent, load_snapshot

STAMP = "2025-12-15 17:30:00.000000000"


def _tsv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _obj(plural: str, kind: str, name: str, ns: str | None = "otel-demo") -> dict:
    metadata = {"name": name}
    if ns is not None:
        metadata["namespace"] = ns
    return {
        "Timestamp": STAMP,
        "Body": json.dumps({"apiVersion": "v1", "kind": kind, "metadata": metadata}),
        "ResourceAttributes": "{}",
        "LogAttributes": repr({"k8s.resource.name": plural}),
    }


def make_scenario(root: Path) -> Path:
    root.mkdir()
    _tsv(
        root / "k8s_objects_raw.tsv",
        [
            _obj("pods", "Pod", "checkout-7f9-a"),
            _obj("pods", "Pod", "checkout-7f9-b"),  # recreated in a later collection
            _obj("deployments", "Deployment", "checkout"),
            _obj("services", "Service", "Frontend"),
            _obj("services", "Service", "kubernetes", ns="default"),
            _obj("serviceaccounts", "ServiceAccount", "default"),
            _obj("secrets", "Secret", "db-password"),
            _obj("namespaces", "Namespace", "otel-demo", ns=None),
            _obj("nodes", "Node", "node-a", ns=None),
            _obj("pods", "Pod", "coredns-1", ns="kube-system"),
            _obj("clusterroles", "ClusterRole", "admin-role", ns=None),
            # No k8s.resource.name: the object's own kind decides.
            dict(_obj("configmaps", "ConfigMap", "flags"), LogAttributes="{}"),
            dict(_obj("secrets", "Secret", "token-x"), LogAttributes="{}"),
            dict(_obj("roles", "Role", "reader"), LogAttributes="{}"),
        ],
    )
    _tsv(
        root / "otel_traces_raw.tsv",
        [
            {"Timestamp": STAMP, "ServiceName": "frontend", "Duration": "1"},
            {"Timestamp": STAMP, "ServiceName": "payment", "Duration": "1"},
        ],
    )
    _tsv(
        root / "otel_logs_raw.tsv",
        [
            {
                "Timestamp": STAMP,
                "Body": "hi",
                "ResourceAttributes": repr(
                    {"service.name": "cart", "k8s.pod.name": "cart-1"}
                ),
            },
            {"Timestamp": STAMP, "Body": "hi", "ResourceAttributes": "{}"},
        ],
    )
    return root


EXPECTED = [
    "cart",
    "checkout",
    "checkout-7f9-a",
    "checkout-7f9-b",
    "flags",
    "Frontend",
    "node-a",
    "payment",
]


def test_build_applies_l_v6_to_l_v8(tmp_path):
    names = vocabulary.build(load_snapshot(make_scenario(tmp_path / "s")))
    assert names == EXPECTED


def test_normalise_is_case_insensitive_and_order_independent():
    assert vocabulary.normalise(
        ["b", "A", "a", " B ", "", "Default", "KUBERNETES"]
    ) == [
        "A",
        "B",
    ]
    assert vocabulary.normalise(["a", "A"]) == vocabulary.normalise(["A", "a"])


def test_cli_output_is_accepted_by_answer(tmp_path):
    out = tmp_path / "vocab.txt"
    assert vocabulary.main([str(make_scenario(tmp_path / "s")), "--out", str(out)]) == 0
    assert out.read_text() == "".join(f"{n}\n" for n in EXPECTED)
    report_out = tmp_path / "outputs"
    assert (
        answer.main(
            [
                "--vocabulary",
                str(out),
                "--out",
                str(report_out),
                "--scenario-dir",
                "Scenario-1",
                "--trial",
                "1",
            ]
        )
        == 0
    )
    matcher = answer._Matcher(out.read_text().splitlines())
    assert matcher.names_in("checkout-7f9-a failed, then frontend") == [
        "checkout-7f9-a",
        "Frontend",
    ]


def test_answer_file_is_refused(tmp_path):
    root = make_scenario(tmp_path / "s")
    (root / "assertion.json").write_text("{}")
    with pytest.raises(AnswerFilePresent):
        vocabulary.main([str(root), "--out", str(tmp_path / "v.txt")])
