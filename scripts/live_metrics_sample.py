"""Direct-sampling acceptance for the live usage tools (B3, #562, R8-21).

Reads Pod and node usage once through the tools' own transport with the
read-only identity, and at the same moment reads the same two metrics lists
with the engineer's ``kubectl get --raw`` (plus ``kubectl top`` output for the
record). It then checks that the row sets match and that rows sharing a
sample timestamp carry exactly the millicores and bytes the B3 rules give
for the raw quantities. It asserts nothing about any fault: it only compares
what the tool read with what the cluster reports, so it needs no answer.

    python3 scripts/live_metrics_sample.py --env-file tmp/m1-kind-lab/k8s-investigator.env \\
        --namespace otel-demo --out docs/evidence/b3-live-metrics/<case>/sample.json

The read-only identity comes only from the env file's
``OPSPILOT_K8S_API_URL``, ``OPSPILOT_K8S_TOKEN`` and ``OPSPILOT_K8S_CA_FILE``
lines (parsed, never executed); ``kubectl`` uses the caller's own kubeconfig
(``--context`` to pick one). The token is never written. Exit code 0 when
both comparisons agree, 1 when they do not, 2 on a setup error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opspilot.tools import kubernetes_live_metrics as lm  # noqa: E402
from opspilot.tools.executor import TransportRequest  # noqa: E402
from opspilot.tools.kubernetes_core import (  # noqa: E402
    CREDENTIAL_REF,
    ENDPOINT_SELECTOR,
    KubernetesClient,
)
from opspilot.tools.outcomes import Window  # noqa: E402

_KEYS = ("OPSPILOT_K8S_API_URL", "OPSPILOT_K8S_TOKEN", "OPSPILOT_K8S_CA_FILE")


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in _KEYS:
            values[key] = value.strip().strip("'\"")
    missing = [key for key in _KEYS if not values.get(key)]
    if missing:
        raise SystemExit(f"env file lacks {', '.join(missing)}")
    return values


def _kubectl(context: str | None, *args: str) -> str:
    cmd = ["kubectl", *(["--context", context] if context else []), *args]
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=120
    ).stdout


def _tool(
    transport: lm.LiveMetricsTransport,
    endpoint: str,
    tool: str,
    params: dict[str, object],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    response = transport.fetch(
        TransportRequest(
            operation_id=f"b3-sample-{uuid.uuid4()}",
            source="otel-demo",
            verb="list",
            endpoint=endpoint,
            selector={ENDPOINT_SELECTOR: endpoint},
            params=params,
            window=Window(now - timedelta(hours=1), now),
            timeout_seconds=lm.REQUEST_TIMEOUT_SECONDS,
            max_result_bytes=lm.MAX_RESULT_BYTES,
            credential_ref="unused",
            tool=tool,
        )
    )
    if response.source_status is not None:
        return {"error": response.source_status, "rows": []}
    return {
        "rows": json.loads(response.body)["results"],
        "view_fields": dict(response.view_fields or {}),
    }


def _stamp(raw: str) -> str:
    moment = datetime.fromisoformat(raw).astimezone(timezone.utc)
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expected_pod(item: dict[str, Any]) -> dict[str, int]:
    cpu = Decimal(0)
    memory = Decimal(0)
    for container in item["containers"]:
        cpu += lm.parse_quantity(container["usage"]["cpu"]) or Decimal(0)
        memory += lm.parse_quantity(container["usage"]["memory"]) or Decimal(0)
    return {
        "cpu_millicores": lm._ceil(cpu * 1000),
        "memory_bytes": lm._ceil(memory),
    }


def _expected_node(item: dict[str, Any]) -> dict[str, int]:
    return {
        "cpu_millicores": lm.parse_cpu_millicores(item["usage"]["cpu"]) or 0,
        "memory_bytes": lm.parse_memory_bytes(item["usage"]["memory"]) or 0,
    }


def _compare(
    rows: list[dict[str, Any]],
    raw: list[dict[str, Any]],
    key: Callable[[dict[str, Any], bool], str],
    expected: Callable[[dict[str, Any]], dict[str, int]],
) -> dict[str, Any]:
    tool_keys = {key(row, True) for row in rows}
    raw_by_key = {key(item, False): item for item in raw}
    compared = 0
    mismatches: list[dict[str, Any]] = []
    for row in rows:
        item = raw_by_key.get(key(row, True))
        if item is None or _stamp(item["timestamp"]) != row["timestamp"]:
            continue
        compared += 1
        want = expected(item)
        got = {name: row[name] for name in want}
        if got != want:
            mismatches.append({"key": key(row, True), "tool": got, "raw": want})
    return {
        "row_set_equal": tool_keys == set(raw_by_key),
        "only_in_tool": sorted(map(str, tool_keys - set(raw_by_key))),
        "only_in_raw": sorted(map(str, set(raw_by_key) - tool_keys)),
        "rows_with_same_timestamp": compared,
        "mismatches": mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--context")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        env = _env_file(args.env_file)
        image = _kubectl(
            args.context,
            "-n",
            "kube-system",
            "get",
            "deployment/metrics-server",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        )
    except (OSError, subprocess.SubprocessError, SystemExit) as error:
        print(f"setup failed: {error}", file=sys.stderr)
        return 2
    endpoint = env["OPSPILOT_K8S_API_URL"].rstrip("/")
    transport = lm.LiveMetricsTransport(
        client=KubernetesClient(
            credentials={CREDENTIAL_REF: env["OPSPILOT_K8S_TOKEN"]},
            ca_file=env["OPSPILOT_K8S_CA_FILE"],
        )
    )
    pods_path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{args.namespace}/pods"
    nodes_path = "/apis/metrics.k8s.io/v1beta1/nodes"
    taken_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pods = _tool(transport, endpoint, lm.PODS_TOOL, {"namespace": args.namespace})
    raw_pods = json.loads(_kubectl(args.context, "get", "--raw", pods_path))
    nodes = _tool(transport, endpoint, lm.NODES_TOOL, {})
    raw_nodes = json.loads(_kubectl(args.context, "get", "--raw", nodes_path))
    top_pods = _kubectl(args.context, "top", "pods", "-n", args.namespace)
    top_nodes = _kubectl(args.context, "top", "nodes")
    pods_cmp = _compare(
        pods["rows"],
        raw_pods.get("items") or [],
        lambda r, tool: r["pod"] if tool else r["metadata"]["name"],
        _expected_pod,
    )
    nodes_cmp = _compare(
        nodes["rows"],
        raw_nodes.get("items") or [],
        lambda r, tool: r["node"] if tool else r["metadata"]["name"],
        _expected_node,
    )
    agree = all(
        part["row_set_equal"] and not part["mismatches"] and "error" not in tool
        for part, tool in ((pods_cmp, pods), (nodes_cmp, nodes))
    )
    record = {
        "b3": "live usage direct sampling (R8-21); no fault is asserted",
        "taken_at": taken_at,
        "namespace": args.namespace,
        "metrics_server_image": image,
        "tool": {"kubectl_top_pods": pods, "kubectl_top_nodes": nodes},
        "raw": {pods_path: raw_pods, nodes_path: raw_nodes},
        "kubectl_top": {"pods": top_pods, "nodes": top_nodes},
        "comparison": {"pods": pods_cmp, "nodes": nodes_cmp},
        "agree": agree,
    }
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if env["OPSPILOT_K8S_TOKEN"] in text:
        print("refusing to write: the token appears in the record", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(json.dumps({"agree": agree, "pods": pods_cmp, "nodes": nodes_cmp}))
    return 0 if agree else 1


if __name__ == "__main__":
    raise SystemExit(main())
