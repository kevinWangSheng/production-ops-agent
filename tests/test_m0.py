"""Synthetic-only boundary tests; no real .env files or remote endpoints."""

import asyncio
import copy
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

# Match existing tests' explicit repository imports, including pytest console entry point.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.m0 import config, protocol  # noqa: E402


@pytest.fixture(autouse=True)
def deny_network():
    with protocol.no_network():
        yield


def private_file(tmp_path, text):
    p = tmp_path / "private.env"
    p.write_text(text)
    p.chmod(0o600)
    return p


def test_wire_rehearsal_and_trace_filter():
    result = asyncio.run(protocol.rehearse())
    assert result["request_count"] == 2
    assert result["evidence_id"] == "m0-evidence-a"
    assert "SYNTHETIC_" not in json.dumps(result)


@pytest.mark.parametrize(
    "text",
    [
        "UNKNOWN=secret",
        "DEEPSEEK_API_KEY=a\nDEEPSEEK_API_KEY=b",
        "DEEPSEEK_API_KEY=$(echo bad)",
        "DEEPSEEK_API_KEY=`echo bad`",
        "export DEEPSEEK_API_KEY=bad",
        'DEEPSEEK_API_KEY="unfinished',
        "OPSPILOT_MODEL=other",
        "LANGSMITH_TRACING=true",
    ],
)
def test_invalid_config_does_not_disclose_values(tmp_path, text):
    with pytest.raises(config.ConfigError) as error:
        config.load_config(private_file(tmp_path, text), environ={})
    assert text not in str(error.value)


def test_explicit_config_conflict_and_permissions(tmp_path):
    p = private_file(tmp_path, "DEEPSEEK_API_KEY=synthetic-private")
    loaded = config.load_config(p, environ={})
    assert loaded.readiness()["deepseek_key_present"]
    assert "synthetic-private" not in repr(loaded)
    assert not loaded.readiness()["live_execution_enabled"]
    with pytest.raises(config.ConfigError, match="ENV_CONFLICT"):
        config.load_config(p, environ={"DEEPSEEK_API_KEY": "other"})
    p.chmod(0o644)
    with pytest.raises(config.ConfigError, match="PRIVATE_REGULAR"):
        config.load_config(p, environ={})
    link = tmp_path / "link"
    link.symlink_to(p)
    with pytest.raises(config.ConfigError):
        config.load_config(link, environ={})


@pytest.mark.parametrize("budget", ["0", "-1", "NaN", "Infinity", "bad"])
def test_budget_missing_invalid_never_ready(budget):
    loaded = config.load_config(
        None, environ={"OPSPILOT_EXPERIMENT_BUDGET_CNY": budget}
    )
    assert not loaded.readiness()["positive_budget_configured"]
    assert not loaded.readiness()["authorization_recorded"]


def test_pairing_and_provider_boundaries():
    f = json.loads(protocol.FIXTURE.read_text())
    a = f["assistant"]
    r = protocol.tool_result(a["tool_calls"][0], f)
    for provider, run_id, results in [
        ("other", protocol.RUN_ID, [r]),
        ("deepseek", "other", [r]),
        ("deepseek", protocol.RUN_ID, []),
        ("deepseek", protocol.RUN_ID, [r, r]),
    ]:
        with pytest.raises(protocol.ProtocolError):
            protocol.continuation(a, results, provider=provider, run_id=run_id)
    broken = copy.deepcopy(a)
    del broken["reasoning_content"]
    with pytest.raises(protocol.ProtocolError, match="PRIVATE_PROTOCOL_MISSING"):
        protocol.continuation(broken, [r], provider="deepseek", run_id=protocol.RUN_ID)
    for arguments in [
        "{}",
        "{",
        '{"target":"other"}',
        '{"target":"m0-target-a","shell":"x"}',
    ]:
        call = copy.deepcopy(a["tool_calls"][0])
        call["function"]["arguments"] = arguments
        with pytest.raises(protocol.ProtocolError, match="TOOL_INPUT_DENIED"):
            protocol.tool_result(call, f)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "SYNTHETIC_SECRET"),
        ("request_count", True),
        ("request_count", 3),
        ("status", "SYNTHETIC_SECRET"),
    ],
)
def test_trace_allowlisted_values_cannot_carry_arbitrary_text(field, value):
    raw = {"run_id": protocol.RUN_ID, "request_count": 2, "status": "completed"}
    raw[field] = value
    with pytest.raises(protocol.ProtocolError, match="TRACE_DTO_INVALID"):
        protocol.trace_dto(raw)


def test_trace_sdk_does_not_add_environment(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_REVISION_ID", "SYNTHETIC_ENV_SECRET")
    raw = {
        "run_id": protocol.RUN_ID,
        "request_count": 2,
        "status": "completed",
        "reasoning_content": "SYNTHETIC_PRIVATE",
        "inputs": {"key": "SYNTHETIC_SECRET"},
    }
    result = protocol.trace_rehearsal(raw)
    assert "SYNTHETIC" not in json.dumps(result)


def test_socket_denied():
    with pytest.raises(protocol.ProtocolError, match="NETWORK_DISABLED"):
        socket.getaddrinfo("example.com", 443)


def test_cli_denies_live_and_secret_rendering(tmp_path):
    p = private_file(tmp_path, "DEEPSEEK_API_KEY=synthetic-do-not-render")
    env = {"PATH": os.environ["PATH"]}
    for args, code, expected in [
        (["live", "--env-file", str(p)], 3, "LIVE_NOT_ENABLED"),
        (["check-config", "--env-file", str(p)], 2, "deepseek_key_present"),
        (["offline", "--env-file", str(p)], 2, "OFFLINE_USES_SYNTHETIC_CONFIG_ONLY"),
    ]:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.m0", *args],
            cwd=protocol.ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == code
        assert expected in result.stdout
        assert "synthetic-do-not-render" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "assistant_role",
        "result_role",
        "result_content",
        "result_extra",
        "missing_id",
        "invalid_id",
        "invalid_calls",
        "missing_content",
    ],
)
def test_pairing_rejects_structural_corruption(mutation):
    f = json.loads(protocol.FIXTURE.read_text())
    a = f["assistant"]
    result = protocol.tool_result(a["tool_calls"][0], f)
    if mutation == "assistant_role":
        a["role"] = "user"
    elif mutation == "result_role":
        result["role"] = "system"
    elif mutation == "result_content":
        result["content"] = {"injected": "text"}
    elif mutation == "result_extra":
        result["secret"] = "synthetic"
    elif mutation == "missing_id":
        del a["tool_calls"][0]["id"]
    elif mutation == "invalid_id":
        a["tool_calls"][0]["id"] = []
    elif mutation == "invalid_calls":
        a["tool_calls"] = None
    else:
        del a["content"]
    with pytest.raises(protocol.ProtocolError, match="TOOL_PAIRING_INVALID"):
        protocol.continuation(a, [result], provider="deepseek", run_id=protocol.RUN_ID)
