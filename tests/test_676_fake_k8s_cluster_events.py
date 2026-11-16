"""Independent #676 fake-Kubernetes event-read acceptance tests.

The existing ``tests.test_itbench_lite_fake_k8s`` module supplies the real
HTTPS server and ``otel-demo-ext`` executor fixture.  This module overrides
only its snapshot fixture with a synthetic, three-namespace event stream so
the assertions exercise the public executor seam rather than the fake server
directly.
"""

from __future__ import annotations

import csv
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.otel_demo import TARGET_ID
from tests.test_itbench_lite_fake_k8s import (
    AUTHORIZED_WINDOW,
    _ch,
)

pytest_plugins = ("tests.test_itbench_lite_fake_k8s",)


COLUMNS = ["Timestamp", "TimestampTime", "Body", "ResourceAttributes", "LogAttributes"]
APP_NAMESPACE = "app-space"
WORKER_NAMESPACE = "worker-space"
SYSTEM_NAMESPACE = "system-space"
CUSTOM_KIND = "SyntheticWidget"


def _watch_event(
    index: int,
    namespace: str,
    object_kind: str,
    reason: str,
    at,
) -> dict[str, str]:
    object_name = f"object-{index:04d}"
    uid = f"event-{index:04d}"
    event = {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {
            "name": f"{object_name}.{uid}",
            "namespace": namespace,
            "uid": uid,
        },
        "involvedObject": {
            "kind": object_kind,
            "name": object_name,
            "namespace": namespace,
            "uid": f"resource-{index:04d}",
        },
        "reason": reason,
        "type": "Normal",
        "eventTime": None,
        "firstTimestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastTimestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return {
        "Timestamp": _ch(at, 17),
        "TimestampTime": _ch(at)[:19],
        "Body": json.dumps({"type": "ADDED", "object": event}),
        "ResourceAttributes": "{}",
        "LogAttributes": repr({"k8s.resource.name": "events"}),
    }


def _write_events(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, delimiter="\t")
        writer.writeheader()
        base = AUTHORIZED_WINDOW.start + timedelta(seconds=30)
        rows: list[dict[str, str]] = []
        for index in range(2000):
            if index < 1000:
                namespace, kind, reason = APP_NAMESPACE, "Pod", "Observed"
            elif index < 1500:
                namespace, kind, reason = WORKER_NAMESPACE, "Pod", "Observed"
            elif index < 1750:
                namespace, kind, reason = WORKER_NAMESPACE, CUSTOM_KIND, "Configured"
            else:
                namespace, kind, reason = SYSTEM_NAMESPACE, "Pod", "Ready"
            rows.append(
                _watch_event(
                    index,
                    namespace,
                    kind,
                    reason,
                    # Keep every event inside the five-minute authorized
                    # window while retaining 2000 distinct live UIDs.
                    base + timedelta(seconds=index % 240),
                )
            )
        writer.writerows(rows)


@pytest.fixture
def snapshot_dir(tmp_path: Path) -> Path:
    root = tmp_path / "synthetic-676"
    root.mkdir()
    _write_events(root / "k8s_events_raw.tsv")
    return root


def _call(tool: str, params: dict[str, Any], index: int = 0) -> ToolRequest:
    return ToolRequest(
        step_id="676-events",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=AUTHORIZED_WINDOW.as_json(),
    )


def _ok(outcome) -> dict[str, Any]:
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    assert outcome.model_view is not None
    return outcome.model_view


def _decoded_content(view: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    for item in view.get("content", []):
        if isinstance(item, str):
            try:
                values.append(json.loads(item))
            except json.JSONDecodeError:
                values.append(item)
        else:
            values.append(item)
    return values


def test_aggregate_event_query_is_ok_and_counts_all_groups(executor):
    view = _ok(
        executor.execute(
            _call(
                "kubernetes_count",
                {
                    "kind": "events",
                    "jq_expr": (
                        "[.items[] | {kind: .involvedObject.kind, "
                        "namespace: .metadata.namespace, reason: .reason}] "
                        "| group_by(.) | map({group: .[0], count: length})[]"
                    ),
                },
            )
        )
    )

    assert view["matches_found"] == 4
    groups = _decoded_content(view)
    assert sorted(item["count"] for item in groups) == [250, 250, 500, 1000]
    assert any(item["group"]["kind"] == CUSTOM_KIND for item in groups)
    assert {item["group"]["namespace"] for item in groups} == {
        APP_NAMESPACE,
        WORKER_NAMESPACE,
        SYSTEM_NAMESPACE,
    }


def test_unscoped_event_query_sees_custom_kind_in_another_namespace(executor):
    view = _ok(
        executor.execute(
            _call(
                "kubernetes_count",
                {
                    "kind": "events",
                    "field_selector": f"involvedObject.kind={CUSTOM_KIND}",
                    "jq_expr": ".items[] | .involvedObject.kind",
                },
            )
        )
    )

    assert view["matches_found"] == 250


def test_application_namespace_scope_does_not_hide_custom_kind_contract(executor):
    view = _ok(
        executor.execute(
            _call(
                "kubernetes_count",
                {
                    "kind": "events",
                    "namespace": APP_NAMESPACE,
                    "field_selector": f"involvedObject.kind={CUSTOM_KIND}",
                    "jq_expr": (
                        "[.items[] | .involvedObject.kind] | length"
                    ),
                },
            )
        )
    )

    assert view["matches_found"] == 1
    assert _decoded_content(view) == [0]


def test_involved_object_kind_field_selector_is_supported(executor):
    view = _ok(
        executor.execute(
            _call(
                "kubernetes_count",
                {
                    "kind": "events",
                    "field_selector": "involvedObject.kind=Pod",
                    "jq_expr": ".items[] | .reason",
                },
            )
        )
    )

    assert view["matches_found"] == 1750
