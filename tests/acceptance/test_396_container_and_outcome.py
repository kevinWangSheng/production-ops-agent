"""Offline acceptance tests for #396 container, outcome and pin seams."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PIN = ROOT / "docs/evidence/f14-holmes-baseline/pin.json"
UPSTREAM_COMMIT = "a0009195468ebc55a7dff7d602b61feabe3cda1b"


def _holmes_module() -> Any:
    try:
        from scripts import holmes_baseline
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.fail(f"Holmes baseline package is unavailable: {exc}", pytrace=False)
    return holmes_baseline


def _option_values(args: list[str], option: str) -> list[str]:
    values: list[str] = []
    for index, arg in enumerate(args):
        if arg == option and index + 1 < len(args):
            values.append(args[index + 1])
        elif arg.startswith(option + "="):
            values.append(arg.split("=", 1)[1])
    return values


def _mount_specs(args: list[str]) -> list[str]:
    specs: list[str] = []
    for index, arg in enumerate(args):
        if arg in {"-v", "--volume", "--mount"} and index + 1 < len(args):
            specs.append(args[index + 1])
        elif arg.startswith(("--volume=", "--mount=")):
            specs.append(arg.split("=", 1)[1])
    return specs


def test_container_run_args_enforce_the_read_only_non_root_boundary() -> None:
    module = _holmes_module()
    args = module.container.run_args(
        "holmes:test",
        network="kind-internal",
        egress_proxy="http://egress.internal:3128",
        config_path="/tmp/holmes/config.yaml",
        output_dir="/tmp/holmes/output",
    )
    assert isinstance(args, list) and all(isinstance(arg, str) for arg in args)
    assert "holmes:test" in args
    assert "--read-only" in args
    assert "--cap-drop" in args and "ALL" in _option_values(args, "--cap-drop")
    assert "--security-opt" in args and "no-new-privileges" in _option_values(
        args, "--security-opt"
    )
    users = _option_values(args, "--user")
    assert users and all(value not in {"0", "root", "0:0"} for value in users)
    assert _option_values(args, "--network") == ["kind-internal"]

    env_values = _option_values(args, "--env") + _option_values(args, "-e")
    proxy_values = [value for value in env_values if value.startswith("HTTPS_PROXY=")]
    assert proxy_values == ["HTTPS_PROXY=http://egress.internal:3128"]
    assert not any(
        re.match(r"(?:OTEL|BRAINTRUST|SENTRY|KUBECONFIG|DOCKER|AWS)_", value)
        for value in env_values
    )

    mounts = _mount_specs(args)
    assert len(mounts) == 2, mounts
    rendered = "\n".join(mounts)
    assert "/tmp/holmes/config.yaml" in rendered
    assert "/tmp/holmes/output" in rendered
    assert any(
        "ro" in mount or "readonly" in mount
        for mount in mounts
        if "config" in mount
    )
    assert not any(
        forbidden in rendered
        for forbidden in ("docker.sock", ".kube", "/repo", str(ROOT))
    )


def test_project_outcome_projects_terminal_run_and_counts_successful_queries() -> None:
    module = _holmes_module()
    outcome = module.project.project_outcome(
        {
            "status": "completed",
            "report": "observed payment failure",
            "tool_calls": ["prometheus.query", "http.jaeger"],
            "model_calls": [{"status": "ok"}],
        },
        [
            'user=holmes method=POST path=/api/v1/query status=200',
            'user=holmes method=POST path=/api/v1/query_range status=204',
            'user=holmes method=POST path=/api/v1/query status=500',
        ],
    )
    assert set(outcome) == {
        "final_state",
        "report_available",
        "tool_calls",
        "proxied_queries_ok",
        "model_calls_ok",
        "handoff_reasons",
    }
    assert outcome["final_state"] == "completed"
    assert outcome["report_available"] is True
    assert outcome["proxied_queries_ok"] == 2
    assert outcome["model_calls_ok"] is True
    assert outcome["handoff_reasons"] == []
    assert outcome["tool_calls"]


def test_f14_remains_unpassed_until_the_whole_feature_is_complete() -> None:
    features = json.loads((ROOT / "feature_list.json").read_text())
    f14 = next(item for item in features if item["id"] == "F14")
    assert f14["passes"] is False


def test_pin_record_contains_contractual_provenance_and_is_non_reproducible() -> None:
    assert PIN.is_file(), f"pin record is missing: {PIN.relative_to(ROOT)}"
    pin = json.loads(PIN.read_text())
    assert isinstance(pin, dict)
    serialized = json.dumps(pin, sort_keys=True)
    assert UPSTREAM_COMMIT in serialized
    assert pin.get("reproducible") is False

    required_aliases = {
        "image": ("local_image_id", "image_id", "image_digest"),
        "archive": ("archive_sha256", "archive_sha256sum"),
        "base": ("base_image_digests", "base_digests"),
        "license": ("license", "license_id"),
        "config": ("config", "config_settings"),
        "model": ("model", "model_settings"),
        "budget": ("budget", "budget_settings"),
    }
    for label, aliases in required_aliases.items():
        values = [pin[name] for name in aliases if name in pin]
        assert values, f"pin is missing {label} record"
        assert all(value not in (None, "", [], {}) for value in values), label

