"""Offline acceptance tests for the #396 Holmes tool configuration seam."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

import pytest


def _holmes_module() -> Any:
    try:
        from scripts import holmes_baseline
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.fail(f"Holmes baseline package is unavailable: {exc}", pytrace=False)
    return holmes_baseline


def _toolsets(config: Mapping[str, Any]) -> Mapping[str, Any] | list[Any]:
    value = config.get("toolsets")
    assert isinstance(value, (Mapping, list)), "config has no toolsets collection"
    return value


def _named_toolsets(value: Mapping[str, Any] | list[Any]) -> Iterator[tuple[str, Any]]:
    if isinstance(value, Mapping):
        yield from ((str(name), item) for name, item in value.items())
        return
    for item in value:
        assert isinstance(item, Mapping), item
        name = item.get("name") or item.get("toolset") or item.get("id")
        assert isinstance(name, str), item
        yield name, item


def _enabled(item: Any) -> bool:
    assert isinstance(item, Mapping), item
    assert "enabled" in item, f"toolset has no explicit enabled flag: {item!r}"
    assert isinstance(item["enabled"], bool), item
    return item["enabled"]


def _first(item: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in item:
            return item[name]
    raise AssertionError(f"none of {names!r} found in {item!r}")


def _setting(item: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    try:
        return _first(item, names)
    except AssertionError:
        nested = item.get("config")
        assert isinstance(nested, Mapping), item
        return _first(nested, names)


def _as_list(value: Any) -> list[Any]:
    return [value] if isinstance(value, str) else list(value)


def _named_instance(item: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    instances = item.get("instances")
    if isinstance(instances, Mapping) and isinstance(instances.get(name), Mapping):
        return instances[name]
    if isinstance(instances, list):
        for instance in instances:
            if isinstance(instance, Mapping) and (
                instance.get("name") == name
                or instance.get("instance") == name
                or instance.get("instance_name") == name
            ):
                return instance
    direct = item.get(name)
    if isinstance(direct, Mapping):
        return direct
    if item.get("name") == name or item.get("instance") == name:
        return item
    nested = item.get("config")
    if isinstance(nested, Mapping):
        return _named_instance(nested, name)
    raise AssertionError(f"http toolset has no instance {name!r}: {item!r}")


def test_holmes_config_enables_only_metrics_and_get_only_jaeger_http() -> None:
    module = _holmes_module()
    prometheus_url = "http://prometheus.internal:9090"
    jaeger_url = "http://jaeger.internal:16686/jaeger"
    config = module.config.holmes_config(prometheus_url, jaeger_url)
    assert isinstance(config, dict)
    serialized = str(config)
    assert "{{ env.HOLMES_PROMETHEUS_AUTHORIZATION }}" in serialized
    assert "Basic " not in serialized

    named = dict(_named_toolsets(_toolsets(config)))
    metric_toolset_names = {
        "prometheus",
        "metrics",
        "prometheus_metrics",
        "prometheus/metrics",
    }
    enabled_names = {name.lower() for name, item in named.items() if _enabled(item)}
    assert "http" in enabled_names
    assert enabled_names & metric_toolset_names
    assert enabled_names <= {"http"} | metric_toolset_names

    for name, item in named.items():
        lowered = name.lower()
        if lowered in {"http"} | metric_toolset_names:
            continue
        assert _enabled(item) is False, (name, item)
    for forbidden in ("bash", "internet", "robusta"):
        matches = [item for name, item in named.items() if name.lower() == forbidden]
        assert matches and all(_enabled(item) is False for item in matches), forbidden
    kubernetes = [
        item for name, item in named.items() if name.lower().startswith("kubernetes")
    ]
    assert kubernetes and all(_enabled(item) is False for item in kubernetes)

    http = next(item for name, item in named.items() if name.lower() == "http")
    assert isinstance(http, Mapping)
    jaeger = _named_instance(http, "jaeger")
    methods = _as_list(_setting(jaeger, ("allowed_methods", "methods")))
    hosts = _as_list(_setting(jaeger, ("allowed_hosts", "hosts")))
    paths = _as_list(_setting(jaeger, ("allowed_paths", "paths")))
    assert methods == ["GET"]
    assert hosts == [urlsplit(jaeger_url).netloc]
    expected_path = urlsplit(jaeger_url).path.rstrip("/") + "/api/*"
    assert paths == [expected_path]

