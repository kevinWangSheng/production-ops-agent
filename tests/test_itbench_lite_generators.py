"""#605 contract tests for the synthetic ITBench-Lite generators.

These tests intentionally construct only tiny snapshot trees in ``tmp_path``.
They do not inspect any dataset answer or census files.  The generator modules
are called through their public ``main(argv)`` entry points so the tests remain
useful before the implementation exists in the parallel worktree.
"""

from __future__ import annotations

import builtins
import csv
import io
import json
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from scripts.itbench_lite import answer, coverage
from scripts.itbench_lite.snapshot import AnswerFilePresent


TSV_COLUMNS = ["Timestamp", "TimestampTime", "Body", "ResourceAttributes"]
TRACE_COLUMNS = [
    "Timestamp",
    "TraceId",
    "SpanId",
    "ParentSpanId",
    "SpanName",
    "SpanKind",
    "ServiceName",
    "ResourceAttributes",
    "SpanAttributes",
    "Duration",
    "StatusCode",
    "StatusMessage",
    "Events.Timestamp",
    "Events.Name",
    "Events.Attributes",
]
START = datetime(2025, 12, 15, 17, 50, tzinfo=UTC)
START_TEXT = "2025-12-15T17:50:00Z"
ANSWER_TOKEN = "ANSWER_FILE_MUST_NOT_BE_READ_605"


def _rfc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _click(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _body_row(moment: datetime, body: dict[str, Any], resource: str = "{}") -> dict[str, str]:
    stamp = _click(moment)
    return {
        "Timestamp": stamp,
        "TimestampTime": stamp,
        "Body": json.dumps(body, ensure_ascii=False),
        "ResourceAttributes": resource,
    }


def _make_scenario(
    root: Path,
    *,
    ends: dict[str, int],
    scenario_id: str,
    include_trace: bool = True,
    trace_service_name: str = "checkout",
    objects: list[dict[str, Any]] | None = None,
    logs: list[dict[str, str]] | None = None,
) -> Path:
    """Create one minimal replay snapshot with all five coverage classes."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "metrics").mkdir()
    metric_end = START + timedelta(seconds=ends["metrics"])
    _write_tsv(
        root / "metrics" / "synthetic.tsv",
        [
            "timestamp",
            "metric_name",
            "value",
            "tags",
            "namespace",
            "pod_name",
            "service_name",
        ],
        [
            {
                "timestamp": _click(metric_end),
                "metric_name": "synthetic_value",
                "value": "1",
                "tags": "{}",
                "namespace": "otel-demo",
                "pod_name": f"pod-{scenario_id}",
                "service_name": "checkout",
            }
        ],
    )

    if include_trace:
        trace_end = START + timedelta(seconds=ends["traces"])
        _write_tsv(
            root / "otel_traces_raw.tsv",
            TRACE_COLUMNS,
            [
                {
                    "Timestamp": _click(trace_end),
                    "TraceId": "0af7651916cd43dd8448eb211c80319c",
                    "SpanId": "b7ad6b7169203331",
                    "ParentSpanId": "",
                    "SpanName": "synthetic",
                    "SpanKind": "Server",
                    "ServiceName": trace_service_name,
                    "ResourceAttributes": "{}",
                    "SpanAttributes": "{}",
                    "Duration": "0",
                    "StatusCode": "Unset",
                    "StatusMessage": "",
                    "Events.Timestamp": "[]",
                    "Events.Name": "[]",
                    "Events.Attributes": "[]",
                }
            ],
        )

    objects = objects or [
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"pod-{scenario_id}",
                "namespace": "otel-demo",
                "uid": f"uid-{scenario_id}",
            },
        }
    ]
    object_end = START + timedelta(seconds=ends["objects"])
    _write_tsv(
        root / "k8s_objects_raw.tsv",
        TSV_COLUMNS,
        [_body_row(object_end, body) for body in objects],
    )

    event_end = START + timedelta(seconds=ends["events"])
    event = {
        "type": "ADDED",
        "object": {
            "apiVersion": "v1",
            "kind": "Event",
            "metadata": {"name": f"event-{scenario_id}"},
            "eventTime": _rfc(event_end),
            "lastTimestamp": _rfc(event_end),
        },
    }
    _write_tsv(root / "k8s_events_raw.tsv", TSV_COLUMNS, [_body_row(event_end, event)])

    log_end = START + timedelta(seconds=ends["logs"])
    log_rows = logs or [_body_row(log_end, {}, '{"service.name":"checkout"}')]
    _write_tsv(root / "otel_logs_raw.tsv", TSV_COLUMNS, log_rows)

    (root / "alerts").mkdir()
    (root / "alerts" / "alerts_in_alerting_state_synthetic.json").write_text(
        json.dumps(
            {
                "data": {
                    "alerts": [
                        {
                            "labels": {
                                "alertname": "SyntheticFault",
                                "service_name": "checkout",
                            },
                            "state": "firing",
                            "activeAt": START_TEXT,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _run_cli(module_name: str, argv: list[str]) -> int:
    module = import_module(module_name)
    return int(module.main(argv))


def _tiers_dataset(root: Path) -> Path:
    """Build exactly the official 35-id shape, with boundary probes."""

    for raw_id in sorted(coverage.SCENARIO_IDS, key=int):
        scenario_id = f"S{raw_id}"
        ends = {
            "metrics": 600,
            "traces": 600,
            "objects": 600,
            "events": 600,
            "logs": 600,
        }
        include_trace = True
        if raw_id == "1":
            # Exact five-minute boundary for every class.
            ends = {kind: 300 for kind in ends}
        elif raw_id == "2":
            # No trace file: an absent trace class is not covered.
            include_trace = False
        elif raw_id == "7":
            # 0, positive-under-five, and negative deltas across classes.
            ends.update(metrics=0, objects=-1, events=1)
        elif raw_id == "4":
            ends["traces"] = 300
        elif raw_id == "5":
            ends["traces"] = 0
        elif raw_id == "6":
            ends["traces"] = -1
        _make_scenario(root / scenario_id, ends=ends, scenario_id=scenario_id)
    return root


def test_tiers_cli_emits_35_ids_and_classifies_every_boundary(tmp_path):
    dataset = _tiers_dataset(tmp_path / "dataset")
    tiers_path = tmp_path / "tiers.json"
    detail_path = tmp_path / "tiers_detail.json"
    before = _tree(dataset)

    assert (
        _run_cli(
            "scripts.itbench_lite.tiers",
            [str(dataset), "--out", str(tiers_path), "--detail", str(detail_path)],
        )
        == 0
    )

    tiers = json.loads(tiers_path.read_text(encoding="utf-8"))
    loaded = coverage.load_tiers(tiers)
    assert len(tiers) == 35
    assert set(loaded) == set(coverage.SCENARIO_IDS)
    assert len(set(loaded.values())) == 2
    # L-V4 is keyed by trace-window coverage; exact five minutes is covered,
    # while zero, negative, and absent traces are in the other tier.
    assert loaded["1"] == loaded["7"] == loaded["4"]
    assert loaded["2"] == loaded["5"] == loaded["6"]
    assert loaded["1"] != loaded["2"]

    detail = json.loads(detail_path.read_text(encoding="utf-8"))
    assert set(detail) == set(tiers)
    # Seam assumption for the detail artifact: it is keyed by scenario
    # directory and uses ``fault_start``, ``ends`` and ``coverage`` maps;
    # coverage values are the stable strings below.
    exact = detail["S1"]
    assert exact["fault_start"] == START_TEXT
    assert exact["ends"]["metrics"] == _rfc(START + timedelta(minutes=5))
    assert exact["ends"]["traces"] == _rfc(START + timedelta(minutes=5))
    assert all(value == "covered" for value in exact["coverage"].values())

    boundaries = detail["S7"]
    assert boundaries["coverage"] == {
        "metrics": "partial",
        "traces": "covered",
        "objects": "not_covered",
        "events": "partial",
        "logs": "covered",
    }
    assert detail["S2"]["ends"]["traces"] is None
    assert detail["S2"]["coverage"]["traces"] == "not_covered"
    assert detail["S5"]["coverage"]["traces"] == "partial"
    assert detail["S6"]["coverage"]["traces"] == "not_covered"

    # L-V10: no cache or other files appear under the input root.
    assert _tree(dataset) == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "dataset",
        "tiers.json",
        "tiers_detail.json",
    ]


def test_vocabulary_cli_collects_allowed_names_and_answer_cli_consumes_it(tmp_path):
    scenario = tmp_path / "clean-copy"
    objects = [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "Checkout", "namespace": "otel-demo"},
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "checkout-pod-old",
                "namespace": "otel-demo",
                "uid": "old-pod",
                "deletionTimestamp": "2025-12-15T17:55:00Z",
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "checkout-pod-new",
                "namespace": "otel-demo",
                "uid": "recreated-pod",
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "frontend", "namespace": "otel-demo"},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "must-not-appear", "namespace": "otel-demo"},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "infra-only", "namespace": "kube-system"},
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "Kubernetes", "namespace": "otel-demo"},
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "default", "namespace": "otel-demo"},
        },
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "tenant-a"},
        },
        {
            "apiVersion": "v1",
            "kind": "Node",
            "metadata": {"name": "node-a"},
        },
    ]
    objects.extend(
        {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {
                "name": f"resource-{kind.casefold()}",
                **({} if kind == "Node" else {"namespace": "otel-demo"}),
            },
        }
        for kind in answer.RESOURCE_KINDS.values()
        if kind not in {"Secret", "Namespace", "Node"}
    )
    logs = [
        _body_row(START, {}, '{"service.name":"CHECKOUT"}'),
        _body_row(START, {}, '{"service.name":"log-only"}'),
        _body_row(START, {}, '{"service.name":"DEFAULT"}'),
    ]
    _make_scenario(
        scenario,
        scenario_id="vocab",
        ends={kind: 600 for kind in ("metrics", "traces", "objects", "events", "logs")},
        trace_service_name="PAYMENT",
        objects=objects,
        logs=logs,
    )
    vocabulary_path = tmp_path / "vocabulary.txt"
    before = _tree(scenario)

    assert (
        _run_cli(
            "scripts.itbench_lite.vocabulary",
            [str(scenario), "--out", str(vocabulary_path)],
        )
        == 0
    )

    lines = vocabulary_path.read_text(encoding="utf-8").splitlines()
    folded = [line.casefold() for line in lines]
    assert lines == sorted(lines, key=str.casefold)
    assert len(folded) == len(set(folded))
    assert set(folded) == {
        "checkout",
        "checkout-pod-old",
        "checkout-pod-new",
        "frontend",
        "log-only",
        "node-a",
    } | {
        f"resource-{kind.casefold()}"
        for kind in answer.RESOURCE_KINDS.values()
        if kind not in {"Secret", "Namespace", "Node"}
    }
    assert "must-not-appear" not in folded
    assert "infra-only" not in folded
    assert "tenant-a" not in folded
    assert "kubernetes" not in folded
    assert "default" not in folded
    assert _tree(scenario) == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "clean-copy",
        "vocabulary.txt",
    ]

    # The generated file is used through answer.py's public --vocabulary CLI.
    report_path = tmp_path / "report.json"
    evidence_path = tmp_path / "evidence.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": "m0-report-v2",
                "assessment_status": "completed",
                "conclusion": "supported",
                "summary": "Checkout failed because payment is unavailable; log-only was observed.",
                "claims": [
                    {
                        "kind": "fact",
                        "text": "payment errors affected checkout",
                        "evidence_ids": ["ev-1"],
                        "target_refs": ["target-1"],
                        "time_scope_ref": "window-1",
                    }
                ],
                "gaps": [],
                "next_steps": [],
            }
        ),
        encoding="utf-8",
    )
    evidence_path.write_text(
        json.dumps(
            [
                {
                    "evidence_id": "ev-1",
                    "operation": {"tool": "metrics_query", "query": {}},
                }
            ]
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "outputs"
    assert (
        _run_cli(
            "scripts.itbench_lite.answer",
            [
                "--report",
                str(report_path),
                "--evidence",
                str(evidence_path),
                "--vocabulary",
                str(vocabulary_path),
                "--out",
                str(output_root),
                "--scenario-dir",
                "S1",
                "--trial",
                "1",
            ],
        )
        == 0
    )
    output = json.loads(
        (output_root / "S1" / "1" / "outputs" / "agent_output.json").read_text(
            encoding="utf-8"
        )
    )
    names = {item["name"].casefold() for item in output["entities"]}
    assert "otel-demo/service/checkout" in names
    assert "otel-demo/service/payment" in names
    assert "otel-demo/service/log-only" in names


def _record_open_paths(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    builtin_open = builtins.open
    io_open = io.open
    path_open = Path.open

    def record_path(path: object) -> None:
        try:
            calls.append(str(Path(path)))
        except TypeError:
            calls.append(str(path))

    def wrapped_builtin(file: object, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        record_path(file)
        return builtin_open(file, *args, **kwargs)

    def wrapped_io(file: object, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        record_path(file)
        return io_open(file, *args, **kwargs)

    def wrapped_path(self: Path, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        record_path(self)
        return path_open(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", wrapped_builtin)
    monkeypatch.setattr(io, "open", wrapped_io)
    monkeypatch.setattr(Path, "open", wrapped_path)
    return calls


def test_both_generators_reject_answer_lure_without_opening_or_emitting_it(
    tmp_path, monkeypatch
):
    # load_snapshot's fail-closed name walk is part of the public isolation
    # contract: the lure must be rejected before any file content is opened.
    vocab_input = _make_scenario(
        tmp_path / "vocab-input",
        scenario_id="lure",
        ends={kind: 600 for kind in ("metrics", "traces", "objects", "events", "logs")},
    )
    (vocab_input / "ground_truth.yaml").write_text(ANSWER_TOKEN, encoding="utf-8")
    vocab_out = tmp_path / "vocab.out"

    dataset = _tiers_dataset(tmp_path / "tiers-input")
    (dataset / "S1" / "ground_truth.yaml").write_text(ANSWER_TOKEN, encoding="utf-8")
    tiers_out = tmp_path / "tiers.out"
    detail_out = tmp_path / "detail.out"

    paths = _record_open_paths(monkeypatch)
    with pytest.raises(AnswerFilePresent):
        _run_cli(
            "scripts.itbench_lite.vocabulary",
            [str(vocab_input), "--out", str(vocab_out)],
        )
    lure = str(vocab_input / "ground_truth.yaml")
    assert lure not in paths
    if vocab_out.exists():
        assert ANSWER_TOKEN not in vocab_out.read_text(encoding="utf-8")

    with pytest.raises(AnswerFilePresent):
        _run_cli(
            "scripts.itbench_lite.tiers",
            [str(dataset), "--out", str(tiers_out), "--detail", str(detail_out)],
        )
    lure = str(dataset / "S1" / "ground_truth.yaml")
    assert lure not in paths
    for output_path in (tiers_out, detail_out):
        if output_path.exists():
            assert ANSWER_TOKEN not in output_path.read_text(encoding="utf-8")

