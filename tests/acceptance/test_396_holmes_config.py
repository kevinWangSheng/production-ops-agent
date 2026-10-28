"""Offline acceptance tests for the #396 Holmes tool configuration seam."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import pytest


def _holmes_module() -> Any:
    try:
        from scripts import holmes_baseline
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.fail(f"Holmes baseline package is unavailable: {exc}", pytrace=False)
    return holmes_baseline


def _toolsets(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("toolsets")
    assert isinstance(value, Mapping), "config has no toolsets mapping"
    return value


def test_holmes_config_enables_only_metrics_and_get_only_jaeger_http() -> None:
    module = _holmes_module()
    prometheus_url = "http://prometheus.internal:9090"
    jaeger_url = "http://jaeger.internal:16686/jaeger"
    config = module.config.holmes_config(prometheus_url, jaeger_url)
    assert isinstance(config, dict)
    serialized = str(config)
    assert "{{ env.HOLMES_PROMETHEUS_AUTHORIZATION }}" in serialized
    assert "Basic " not in serialized

    toolsets = _toolsets(config)
    metric_toolset_names = {
        "prometheus",
        "metrics",
        "prometheus_metrics",
        "prometheus/metrics",
    }
    jaeger = toolsets["jaeger"]
    assert isinstance(jaeger, Mapping)
    assert jaeger["type"] == "http"
    assert jaeger["enabled"] is True
    enabled_names = {
        str(name)
        for name, item in toolsets.items()
        if isinstance(item, Mapping) and item.get("enabled") is True
    }
    enabled_metrics = enabled_names & metric_toolset_names
    assert len(enabled_metrics) == 1
    assert enabled_names == enabled_metrics | {"jaeger"}

    for name, item in toolsets.items():
        assert isinstance(item, Mapping), (name, item)
        if name == "jaeger" or name in metric_toolset_names:
            continue
        assert "enabled" not in item or item["enabled"] is False, (name, item)

    endpoints = jaeger["config"]["endpoints"]
    assert isinstance(endpoints, list) and len(endpoints) == 1
    endpoint = endpoints[0]
    assert isinstance(endpoint, Mapping)
    assert endpoint["methods"] == ["GET"]
    assert endpoint["hosts"] == [urlsplit(jaeger_url).netloc]
    expected_path = urlsplit(jaeger_url).path.rstrip("/") + "/api/*"
    assert endpoint["paths"] == [expected_path]
