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
        "PYTHON_DOTENV_DISABLED": "1",
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


def test_child_env_disables_dotenv():
    env = oe.subprocess_env(oe.judge_config({"ZAI_API_KEY": KEY}), {})
    assert env["PYTHON_DOTENV_DISABLED"] == "1"


def test_redacted_strips_url_userinfo():
    config = oe.judge_config(
        {"ZAI_API_KEY": KEY, "ZAI_BASE_URL": "https://u:pw357@judge.example/v1"}
    )
    assert config.redacted()["base_url"] == "https://judge.example/v1"
    assert config.base_url == "https://u:pw357@judge.example/v1"


def test_command_paths_are_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    command = oe.build_command(Path("ev"), Path("gt"), Path("out"), Path("w/r.json"))
    assert command[0] == str(tmp_path / "ev" / ".venv" / "bin" / "python")
    for flag, rel in [
        ("--ground-truth", "gt"),
        ("--outputs", "out"),
        ("--result-file", "w/r.json"),
    ]:
        assert command[command.index(flag) + 1] == str(tmp_path / rel)


def _write(outputs, scenario, trial, present):
    from scripts.itbench_lite import answer

    conversion = answer.Conversion(answer.empty_output(), {"report_present": present})
    answer.write_trial(outputs, scenario, trial, conversion)


def test_reportless_trials_are_not_staged_for_the_judge(tmp_path):
    outputs = tmp_path / "outputs"
    _write(outputs, "Scenario-4", 1, True)
    _write(outputs, "Scenario-4", 2, False)
    _write(outputs, "Scenario-5", 1, False)
    reportless = oe.stage_judged(outputs, tmp_path / "staged")
    assert reportless == [("Scenario-4", 2), ("Scenario-5", 1)]
    staged = sorted(
        str(p.relative_to(tmp_path / "staged"))
        for p in (tmp_path / "staged").rglob("agent_output.json")
    )
    assert staged == ["Scenario-4/1/outputs/agent_output.json"]


def test_reportless_trials_score_zero_even_if_judge_scored_them(tmp_path):
    outputs = tmp_path / "outputs"
    _write(outputs, "Scenario-4", 1, True)
    _write(outputs, "Scenario-4", 2, False)
    _write(outputs, "Scenario-5", 1, False)
    results = {
        "raw_results": [
            {
                "incident_id": "4",
                "trial_id": "1",
                "scores": {
                    "root_cause_entity": {
                        "calculation_precision": 1,
                        "calculation_recall": 0.5,
                        "calculation_f1": 0.667,
                    },
                    "propagation_chain": {"calculation": 0.25},
                },
            },
            # A judged score for a reportless trial must be overridden.
            {
                "incident_id": "4",
                "trial_id": "2",
                "scores": {"root_cause_entity": {"calculation_f1": 1.0}},
            },
        ]
    }
    table = oe.trial_scores(results, outputs)
    assert table["4"][0]["root_cause_entity_f1"] == 0.667
    assert table["4"][0]["propagation_chain"] == 0.25
    assert table["4"][1] == {m: 0.0 for m in oe.METRIC_KEYS}
    assert table["5"] == [{m: 0.0 for m in oe.METRIC_KEYS}]


def test_missing_meta_is_refused(tmp_path):
    trial = tmp_path / "outputs" / "Scenario-4" / "1" / "outputs"
    trial.mkdir(parents=True)
    (trial / "agent_output.json").write_text("{}")
    with pytest.raises(oe.ConfigError, match="conversion_meta"):
        oe.stage_judged(tmp_path / "outputs", tmp_path / "staged")
