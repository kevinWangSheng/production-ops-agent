"""#496: the lab Jaeger's memory settings in ``scripts/kind_lab/values.yaml``.

Static checks only; whether RSS levels off is checked in the pre-soak
rehearsal (#497) on a real lab.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts import kind_lab

CONFIG = Path(__file__).resolve().parents[1] / "scripts" / "kind_lab"
UNITS = {"Mi": 2**20, "Gi": 2**30, "MiB": 2**20, "GiB": 2**30}

# The demo chart 0.37.8 sets these; the lab restates them because a values
# list replaces the chart's list instead of merging with it.
CHART_ARGS = [
    "--query.base-path=/jaeger/ui",
    "--prometheus.server-url=http://prometheus:9090",
    "--prometheus.query.normalize-calls=true",
    "--prometheus.query.normalize-duration=true",
]
CHART_ENV = {
    "METRICS_STORAGE_TYPE": "prometheus",
    "COLLECTOR_OTLP_GRPC_HOST_PORT": "0.0.0.0:4317",
    "COLLECTOR_OTLP_HTTP_HOST_PORT": "0.0.0.0:4318",
}


def test_restated_lists_match_the_pinned_chart() -> None:
    # CHART_ARGS/CHART_ENV were read from this chart version's values.yaml
    # (jaeger.allInOne.args/extraEnv); a chart bump must re-check them
    assert kind_lab.DEMO_CHART_VERSION == "0.37.8"


def _bytes(quantity: str) -> int:
    for suffix in sorted(UNITS, key=len, reverse=True):
        if quantity.endswith(suffix):
            return int(quantity[: -len(suffix)]) * UNITS[suffix]
    raise AssertionError(f"unexpected quantity {quantity!r}")


def _all_in_one() -> dict:
    values = yaml.safe_load((CONFIG / "values.yaml").read_text())
    return values["jaeger"]["allInOne"]


def test_limit_is_2gi() -> None:
    assert _all_in_one()["resources"]["limits"]["memory"] == "2Gi"


def test_go_memory_limit_sits_below_the_container_limit() -> None:
    aio = _all_in_one()
    env = {item["name"]: item["value"] for item in aio["extraEnv"]}
    soft = _bytes(env["GOMEMLIMIT"])
    hard = _bytes(aio["resources"]["limits"]["memory"])
    # headroom for non-heap memory (stacks, binary mappings) the soft limit
    # does not fully cover
    assert soft <= hard * 0.8


def test_trace_count_bound_and_chart_settings_are_kept() -> None:
    aio = _all_in_one()
    args = aio["args"]
    assert [a for a in args if a.startswith("--memory.max-traces=")] == [
        "--memory.max-traces=5000"
    ]
    assert [a for a in args if not a.startswith("--memory.")] == CHART_ARGS
    env = {item["name"]: item["value"] for item in aio["extraEnv"]}
    assert {k: env[k] for k in CHART_ENV} == CHART_ENV
