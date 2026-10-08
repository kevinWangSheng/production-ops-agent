"""Read-only baseline collection for the shipped checkout HealthProfile (#83).

Engineer script, not product code: runs every signal query of
``opspilot/observer/profiles/otel-demo-checkout.json`` against the kind lab
Prometheus as instant and range queries, writes the raw responses to a
git-ignored ``tmp/`` file and prints a summary (points, min/mean/p95/max)
used to calibrate the profile's thresholds and traffic gate. It never writes
to the lab.

    .venv/bin/python docs/evidence/m1-02-health-profile/collect_baseline.py \
        --minutes 30 --out tmp/m1-02-health-profile/baseline-raw.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from opspilot.observer import PROFILE_DIRECTORY, load_health_profile  # noqa: E402
from scripts.kind_lab import OPENER, lab_authorization  # noqa: E402


def get(base: str, path: str, params: dict[str, object]) -> dict:
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    # the lab Prometheus authenticates (M1-02 step 4): the ``lab`` account
    authorization = lab_authorization()
    headers = {} if authorization is None else {"Authorization": authorization}
    request = urllib.request.Request(url, headers=headers)
    with OPENER.open(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus", default="http://127.0.0.1:19090")
    parser.add_argument("--profile", default="otel-demo-checkout")
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--step", type=int, default=15)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    profile = load_health_profile(PROFILE_DIRECTORY / f"{args.profile}.json")
    now = int(time.time())
    raw: dict[str, object] = {
        "collected_at_unix": now,
        "prometheus": args.prometheus,
        "profile_revision": profile.revision,
        "range_minutes": args.minutes,
        "step_seconds": args.step,
        "signals": {},
        "label_checks": {},
    }
    summary: dict[str, dict[str, object]] = {}
    for signal in profile.signals:
        instant = get(
            args.prometheus, "/api/v1/query", {"query": signal.query, "time": now}
        )
        rng = get(
            args.prometheus,
            "/api/v1/query_range",
            {
                "query": signal.query,
                "start": now - args.minutes * 60,
                "end": now,
                "step": args.step,
            },
        )
        raw["signals"][signal.name] = {"instant": instant, "range": rng}  # type: ignore[index]
        series = rng.get("data", {}).get("result", [])
        points = [
            float(value)
            for item in series
            for _, value in item.get("values", [])
            if value not in ("NaN", "+Inf", "-Inf")
        ]
        nan_points = sum(
            1
            for item in series
            for _, value in item.get("values", [])
            if value == "NaN"
        )
        instant_result = instant.get("data", {}).get("result", [])
        instant_value = instant_result[0]["value"][1] if instant_result else None
        summary[signal.name] = {
            "instant": instant_value,
            "series": len(series),
            "points": len(points),
            "nan_points": nan_points,
            "min": min(points) if points else None,
            "mean": statistics.fmean(points) if points else None,
            "p95": percentile(points, 0.95) if points else None,
            "max": max(points) if points else None,
            "healthy": signal.healthy.model_dump(),
        }
    for name, query in {
        "span_kind_values_checkout": 'count by (span_kind) (traces_span_metrics_calls_total{service_name="checkout"})',
        "placeorder_span_names": 'count by (span_name, span_kind) (traces_span_metrics_calls_total{service_name="checkout",span_name=~".*PlaceOrder.*"})',
        "checkout_pods": 'kube_pod_status_phase{namespace="otel-demo",pod=~"checkout-.*",phase="Running"}',
        "dependency_deployments": 'kube_deployment_status_replicas_available{namespace="otel-demo",deployment=~"payment|cart|valkey-cart|product-catalog|currency|shipping|email|flagd"}',
        "scrape_interval_ksm": 'count_over_time(kube_deployment_status_replicas_available{namespace="otel-demo",deployment="checkout"}[5m])',
        "scrape_interval_spanmetrics": 'count_over_time(traces_span_metrics_calls_total{service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m])',
    }.items():
        raw["label_checks"][name] = get(  # type: ignore[index]
            args.prometheus, "/api/v1/query", {"query": query, "time": now}
        )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(raw, indent=1, sort_keys=True).encode("utf-8")
    out.write_bytes(payload)
    print(
        json.dumps(
            {
                "raw_file": str(out),
                "raw_sha256": hashlib.sha256(payload).hexdigest(),
                "raw_bytes": len(payload),
                "collected_at_unix": now,
                "profile_revision": profile.revision,
                "summary": summary,
                "label_checks": {
                    name: [
                        (item.get("metric"), item.get("value", [None, None])[1])
                        for item in result.get("data", {}).get("result", [])
                    ]
                    for name, result in raw["label_checks"].items()  # type: ignore[union-attr]
                },
            },
            indent=1,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
