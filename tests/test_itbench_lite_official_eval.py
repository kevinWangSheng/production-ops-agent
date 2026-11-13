"""Official evaluator wrapper: configuration only, never calls the judge."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.itbench_lite import official_eval as oe

KEY = "aaaaaaaa357"


def test_default_receiver_is_existing_zai():
    config = oe.judge_config({"ZAI_API_KEY": KEY})
    assert (config.provider, config.base_url, config.model) == (
        "zai",
        "https://api.z.ai/api/paas/v4",
        "glm-5.3-flashx",
    )
    labels = config.labels()
    assert labels["judge_differs_from_official_example"] is True
    assert labels["evaluator"] == "ITBench-Evaluations@14f026fc"
    assert labels["propagation_chain"] == "受转换限制"


def test_provider_and_model_from_environment():
    config = oe.judge_config(
        {
            "ITBENCH_JUDGE_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": KEY,
            "ITBENCH_JUDGE_MODEL": "some-model",
        }
    )
    assert (config.provider, config.model, config.api_key) == (
        "deepseek",
        "some-model",
        KEY,
    )
    assert oe.judge_config({"ITBENCH_JUDGE_PROVIDER": "openai"}).model == "gpt-5.6-luna"


def test_new_receiver_is_refused():
    with pytest.raises(oe.ConfigError):
        oe.judge_config({"ITBENCH_JUDGE_PROVIDER": "openrouter"})


def test_redacted_never_contains_key():
    assert KEY not in str(oe.judge_config({"ZAI_API_KEY": KEY}).redacted())


def test_child_env_only_carries_judge_variables():
    config = oe.judge_config({"ZAI_API_KEY": KEY})
    env = oe.subprocess_env(
        config,
        {"PATH": "/bin", "OPENAI_API_KEY": "bbbbbbbb357", "LANGSMITH_API_KEY": "c"},
    )
    assert env == {
        "PATH": "/bin",
        "JUDGE_BASE_URL": "https://api.z.ai/api/paas/v4",
        "JUDGE_API_KEY": KEY,
        "JUDGE_MODEL": "glm-5.3-flashx",
    }


def test_command_uses_evaluator_venv_and_sre_domain():
    command = oe.build_command(
        Path("/ev"),
        Path("/gt"),
        Path("/out"),
        Path("/w/r.json"),
        criteria=["ROOT_CAUSE_ENTITY"],
    )
    assert command[:3] == ["/ev/.venv/bin/python", "-m", "itbench_evaluations"]
    assert command[command.index("--domain") + 1] == "sre"
    assert command[-2:] == ["--eval-criteria", "ROOT_CAUSE_ENTITY"]


def test_run_refuses_without_key_before_any_call(tmp_path):
    config = oe.judge_config({})
    with pytest.raises(oe.ConfigError, match="ZAI_API_KEY"):
        oe.run(tmp_path, tmp_path, tmp_path, tmp_path / "w", config)
    assert not (tmp_path / "w").exists()


def test_evaluator_dir_required():
    with pytest.raises(oe.ConfigError):
        oe.evaluator_dir({})
