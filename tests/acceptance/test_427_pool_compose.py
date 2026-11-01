"""Offline pool and Compose acceptance contract for #427."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_worker_refuses_pool_smaller_than_capacity_plus_three(monkeypatch):
    from opspilot import worker_main

    monkeypatch.setenv("OPSPILOT_WORKER_CAPACITY", "2")
    monkeypatch.setenv("OPSPILOT_POOL_MAX_SIZE", "4")
    with pytest.raises((ValueError, RuntimeError, SystemExit), match="capacity|POOL_MAX_SIZE"):
        worker_main.validate_pool_capacity()


def test_worker_accepts_pool_equal_to_capacity_plus_three(monkeypatch):
    from opspilot import worker_main

    monkeypatch.setenv("OPSPILOT_WORKER_CAPACITY", "2")
    monkeypatch.setenv("OPSPILOT_POOL_MAX_SIZE", "5")
    worker_main.validate_pool_capacity()


def test_compose_worker_default_pool_is_five():
    document = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())
    worker = document["services"]["worker"]
    environment = worker.get("environment", {})
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) for item in environment if "=" in item)
    assert str(environment["OPSPILOT_POOL_MAX_SIZE"]) == "5"
