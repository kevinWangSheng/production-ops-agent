"""Acceptance and contract tests for Holmes container isolation (#399).

The isolation implementation is intentionally imported at test time.  This
keeps collection usable while the parallel implementation is absent, while
the assertions remain strict once the public module exists.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_baseline import container

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/f14-holmes-baseline"
CHECK_IDS = (
    "uid_non_root",
    "forbidden_paths_absent",
    "env_names_allowlisted",
    "kube_api_unreachable",
    "prometheus_backend_unreachable",
    "internet_unreachable",
    "model_only_via_proxy",
    "proxy_log_allowed_paths",
)


def _isolation():
    """Load the new public module without breaking collection before it lands."""
    return pytest.importorskip("scripts.holmes_baseline.isolation")


def _run_arg_env_names() -> tuple[str, ...]:
    args = container.run_args(
        "image:tag",
        network="network",
        egress_proxy="http://egress:8080",
        config_path="/tmp/config.yaml",
        output_dir="/tmp/out",
        no_proxy="relay",
    )
    names: list[str] = []
    for index, arg in enumerate(args):
        if arg != "-e":
            continue
        value = args[index + 1]
        names.append(value.split("=", 1)[0])
    return tuple(names)


def _clean_observation(*, isolation: Any) -> dict[str, Any]:
    return {
        "uid": 65534,
        "paths_present": [],
        "env_names": list(_run_arg_env_names()),
        "reach": {
            "kube_api": False,
            "prometheus_backend": False,
            "internet": False,
            "model_direct": False,
            "model_via_proxy": True,
        },
        "proxy_log_lines": [
            "2026-10-28T18:42:25+00:00 holmes GET /api/v1/query 200 473 582 2.256"
        ],
    }


def _checks(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["id"]: check for check in result["checks"]}


def _assert_only_check_failed(result: dict[str, Any], check_id: str) -> None:
    checks = _checks(result)
    assert [check["id"] for check in result["checks"]] == list(CHECK_IDS)
    assert checks[check_id]["passed"] is False
    assert all(
        check["passed"] is True
        for check_id_other, check in checks.items()
        if check_id_other != check_id
    )
    assert result["passed"] is False


def test_clean_observation_passes_with_checks_in_contract_order():
    isolation = _isolation()
    assert tuple(isolation.CHECK_IDS) == CHECK_IDS
    result = isolation.evaluate(_clean_observation(isolation=isolation))
    assert result["passed"] is True
    assert [check["id"] for check in result["checks"]] == list(CHECK_IDS)
    assert all(check["passed"] is True for check in result["checks"])


@pytest.mark.parametrize("name", _run_arg_env_names())
def test_every_environment_name_emitted_by_run_args_is_accepted(name: str):
    isolation = _isolation()
    observed = _clean_observation(isolation=isolation)
    observed["env_names"] = [name]
    result = isolation.evaluate(observed)
    assert _checks(result)["env_names_allowlisted"]["passed"] is True


@pytest.mark.parametrize(
    ("category", "tokens"),
    (
        (
            "repo checkout",
            ("/repo", "/workspace", "/app", "opspilot", "production-ops-agent", "checkout"),
        ),
        ("env file", (".env",)),
        ("lab auth file", ("auth",)),
        ("kube config", (".kube", "kubeconfig")),
    ),
)
def test_each_forbidden_path_category_fails_only_path_check(category: str, tokens):
    isolation = _isolation()
    paths = tuple(isolation.FORBIDDEN_PATHS)
    candidates = [
        path
        for path in paths
        if any(token in path.lower() for token in tokens)
    ]
    assert candidates, f"FORBIDDEN_PATHS has no {category} representative"
    observed = _clean_observation(isolation=isolation)
    observed["paths_present"] = [candidates[0]]
    _assert_only_check_failed(
        isolation.evaluate(observed), "forbidden_paths_absent"
    )


def test_non_allowlisted_environment_name_fails_only_allowlist_check():
    isolation = _isolation()
    observed = _clean_observation(isolation=isolation)
    observed["env_names"] = ["NOT_ALLOWLISTED_NAME_399"]
    result = isolation.evaluate(observed)
    _assert_only_check_failed(result, "env_names_allowlisted")


def test_environment_value_smuggled_into_names_is_not_echoed_in_details():
    isolation = _isolation()
    secret_value = "DO_NOT_LEAK_ENV_VALUE_399"
    observed = _clean_observation(isolation=isolation)
    observed["env_names"] = [f"SOME_TOKEN={secret_value}"]
    result = isolation.evaluate(observed)
    _assert_only_check_failed(result, "env_names_allowlisted")
    assert secret_value not in " ".join(
        check["detail"] for check in result["checks"]
    )


@pytest.mark.parametrize(
    ("reach_key", "check_id"),
    (
        ("kube_api", "kube_api_unreachable"),
        ("prometheus_backend", "prometheus_backend_unreachable"),
        ("internet", "internet_unreachable"),
        ("model_direct", "model_only_via_proxy"),
    ),
)
def test_each_forbidden_reach_target_fails_only_matching_check(
    reach_key: str, check_id: str
):
    isolation = _isolation()
    observed = _clean_observation(isolation=isolation)
    observed["reach"][reach_key] = True
    _assert_only_check_failed(isolation.evaluate(observed), check_id)


def test_model_via_proxy_false_fails_only_model_check():
    isolation = _isolation()
    observed = _clean_observation(isolation=isolation)
    observed["reach"]["model_via_proxy"] = False
    _assert_only_check_failed(
        isolation.evaluate(observed), "model_only_via_proxy"
    )


@pytest.mark.parametrize(
    "line",
    (
        "2026-10-28T18:42:25+00:00 holmes GET /api/v1/query 403 0 0 0.001",
        "2026-10-28T18:42:25+00:00 holmes GET /api/v1/metadata 200 0 0 0.001",
        "2026-10-28T18:42:25+00:00 holmes GET /api/v1/rules 200 0 0 0.001",
    ),
)
def test_forbidden_holmes_proxy_log_line_fails_only_log_check(line: str):
    isolation = _isolation()
    observed = _clean_observation(isolation=isolation)
    observed["proxy_log_lines"] = [line]
    _assert_only_check_failed(
        isolation.evaluate(observed), "proxy_log_allowed_paths"
    )


@pytest.mark.parametrize(
    ("missing_key", "check_id"),
    (
        ("uid", "uid_non_root"),
        ("paths_present", "forbidden_paths_absent"),
        ("env_names", "env_names_allowlisted"),
        ("reach", "kube_api_unreachable"),
        ("proxy_log_lines", "proxy_log_allowed_paths"),
    ),
)
def test_missing_observation_key_fails_closed(missing_key: str, check_id: str):
    isolation = _isolation()
    observed = _clean_observation(isolation=isolation)
    del observed[missing_key]
    result = isolation.evaluate(observed)
    assert result["passed"] is False
    assert _checks(result)[check_id]["passed"] is False


@pytest.mark.parametrize(
    "missing_reach_key",
    ("kube_api", "prometheus_backend", "internet", "model_direct", "model_via_proxy"),
)
def test_missing_reach_observation_key_fails_closed(missing_reach_key: str):
    isolation = _isolation()
    observed = _clean_observation(isolation=isolation)
    del observed["reach"][missing_reach_key]
    result = isolation.evaluate(observed)
    assert result["passed"] is False


def test_evidence_probe_reproduces_evaluate_result_when_present():
    isolation = _isolation()
    probe_path = EVIDENCE / "isolation-probe.json"
    if not probe_path.exists():
        pytest.skip("isolation-probe.json is produced by the opt-in live probe")
    probe = json.loads(probe_path.read_text())
    pin = json.loads((EVIDENCE / "pin.json").read_text())
    assert probe["image_id"] == pin["image_id"]
    assert probe["result"]["passed"] is True
    assert [check["id"] for check in probe["result"]["checks"]] == list(CHECK_IDS)
    env_names = probe["observed"]["env_names"]
    assert all(isinstance(name, str) and "=" not in name for name in env_names)
    assert isolation.evaluate(probe["observed"]) == probe["result"]


@pytest.mark.skipif(
    os.environ.get("OPSPILOT_HOLMES_ISOLATION_LIVE") != "1",
    reason="set OPSPILOT_HOLMES_ISOLATION_LIVE=1 for the Docker/kind probe",
)
def test_live_probe_writes_the_pinned_isolation_evidence():
    _isolation()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.holmes_baseline.isolation",
            "probe",
            "--out",
            str(EVIDENCE / "isolation-probe-live-out"),
        ],
        cwd=ROOT,
        check=True,
    )
    assert (EVIDENCE / "isolation-probe.json").exists()
