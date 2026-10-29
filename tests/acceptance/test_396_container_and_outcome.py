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
        "ro" in mount or "readonly" in mount for mount in mounts if "config" in mount
    )
    assert not any(
        forbidden in rendered
        for forbidden in ("docker.sock", ".kube", "/repo", str(ROOT))
    )


def test_project_outcome_projects_terminal_run_and_counts_successful_queries() -> None:
    module = _holmes_module()
    outcome = module.project.project_outcome(
        {
            "result": "observed payment failure",
            "tool_calls": ["prometheus.query", "http.jaeger"],
            "num_llm_calls": 3,
            "exit_code": 0,
        },
        [
            "2026-10-28T10:00:00Z holmes POST /api/v1/query?query=up 200 128 64 0.012",
            "2026-10-28T10:00:01Z holmes POST "
            "/api/v1/query_range?query=up 204 128 64 0.013",
            "2026-10-28T10:00:02Z holmes GET /api/v1/query?query=up 200 64 32 0.004",
            "2026-10-28T10:00:03Z holmes POST /api/v1/query 500 128 64 0.014",
            "2026-10-28T10:00:04Z observer GET /api/v1/query 200 64 32 0.004",
            "2026-10-28T10:00:05Z holmes POST /api/v1/rules 200 64 32 0.004",
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
    assert isinstance(outcome["report_available"], bool)
    assert isinstance(outcome["tool_calls"], int)
    assert outcome["tool_calls"] == 2
    assert outcome["proxied_queries_ok"] == 3
    assert isinstance(outcome["proxied_queries_ok"], int)
    assert outcome["model_calls_ok"] == 3
    assert isinstance(outcome["model_calls_ok"], int)
    assert outcome["handoff_reasons"] == []


@pytest.mark.parametrize(
    ("holmes_output", "expected_state"),
    [
        (
            {"result": "", "tool_calls": [], "exit_code": 1, "error": "crashed"},
            "failed",
        ),
        ({"tool_calls": [], "num_llm_calls": 1}, "failed"),
        (
            {
                "result": "partial report",
                "tool_calls": ["prometheus.query"],
                "num_llm_calls": 2,
                "timed_out": True,
            },
            "budget_exhausted",
        ),
        (
            {
                "result": "partial report",
                "tool_calls": ["prometheus.query"],
                "num_llm_calls": 2,
                "error": "exceeded max_steps during run",
            },
            "budget_exhausted",
        ),
        (
            {
                "result": "run exceeded max_steps",
                "tool_calls": [],
                "num_llm_calls": 2,
            },
            "budget_exhausted",
        ),
    ],
)
def test_project_outcome_maps_failed_and_budget_exhausted_runs(
    holmes_output: dict[str, Any], expected_state: str
) -> None:
    module = _holmes_module()
    outcome = module.project.project_outcome(
        holmes_output,
        [
            "2026-10-28T10:00:00Z holmes POST /api/v1/query 200 128 64 0.012",
        ],
    )
    assert outcome["final_state"] == expected_state
    assert outcome["final_state"] in {"completed", "failed", "budget_exhausted"}
    assert set(outcome) == {
        "final_state",
        "report_available",
        "tool_calls",
        "proxied_queries_ok",
        "model_calls_ok",
        "handoff_reasons",
    }
    assert outcome["tool_calls"] == len(holmes_output.get("tool_calls", []))
    assert outcome["proxied_queries_ok"] == 1
    assert outcome["model_calls_ok"] == holmes_output.get("num_llm_calls", 0)
    assert isinstance(outcome["tool_calls"], int)
    assert isinstance(outcome["proxied_queries_ok"], int)
    assert isinstance(outcome["model_calls_ok"], int)
    assert outcome["report_available"] is bool(holmes_output.get("result"))
    assert isinstance(outcome["report_available"], bool)
    assert outcome["handoff_reasons"] == []


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
