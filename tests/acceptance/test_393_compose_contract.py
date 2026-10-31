"""Static Compose contracts for issue #393 (B3, B6, B7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "compose" / "compose.yaml"


def _compose() -> dict[str, Any]:
    assert COMPOSE.is_file(), f"missing {COMPOSE.relative_to(REPO)}"
    try:
        import yaml
    except Exception as exc:  # pragma: no cover - environment failure
        pytest.fail(f"PyYAML is required: {exc}")
    document = yaml.safe_load(COMPOSE.read_text())
    assert isinstance(document, dict) and isinstance(document.get("services"), dict)
    return document


def _secret_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item if isinstance(item, str) else item["source"])
        for item in value
        if isinstance(item, str) or isinstance(item, dict) and item.get("source")
    }


def _environment(service: dict[str, Any]) -> dict[str, str | None]:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return {str(k): None if v is None else str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        result: dict[str, str | None] = {}
        for entry in raw:
            key, sep, value = str(entry).partition("=")
            result[key] = value if sep else None
        return result
    return {}


def test_agent_services_mount_only_their_own_secret_files() -> None:
    services = _compose()["services"]
    expected = {
        "worker": {"worker_pgpass", "worker_env"},
        "observer": {"observer_pgpass", "observer_env"},
        "web": {"web_pgpass", "web_ui_users", "web_event_tokens"},
    }
    for service_name, names in expected.items():
        assert service_name in services
        assert _secret_names(services[service_name].get("secrets")) == names

    owned = {name: _secret_names(services[name].get("secrets")) for name in expected}
    for left, left_names in owned.items():
        for right, right_names in owned.items():
            if left != right:
                assert left_names.isdisjoint(right_names), (left, right)


def test_worker_capacity_and_pool_are_explicit_with_two_connection_reserve() -> None:
    worker = _compose()["services"]["worker"]
    env = _environment(worker)
    assert env.get("OPSPILOT_WORKER_CAPACITY") not in (None, "")
    assert env.get("OPSPILOT_POOL_MAX_SIZE") not in (None, "")
    capacity = int(env["OPSPILOT_WORKER_CAPACITY"] or "0")
    pool = int(env["OPSPILOT_POOL_MAX_SIZE"] or "0")
    assert capacity >= 1
    assert pool >= capacity + 2


@pytest.mark.parametrize("service_name", ["web", "worker", "observer"])
def test_each_runtime_is_single_replica(service_name: str) -> None:
    service = _compose()["services"][service_name]
    deploy = service.get("deploy")
    assert isinstance(deploy, dict) and deploy.get("replicas") == 1
