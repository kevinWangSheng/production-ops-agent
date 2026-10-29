"""#392 image entrypoint: per-service secret files become the environment
variables the runtimes already read, without the secret ever being in the
container configuration (`docker inspect`)."""

import subprocess
from pathlib import Path

import pytest

from opspilot.tools.otel_demo import OtelDemoConfig

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "docker-entrypoint.sh"


def _run(env: dict[str, str], *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(ENTRYPOINT), *command],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _environment(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def test_a_secret_file_is_imported_into_its_variable_and_the_file_variable_dropped(
    tmp_path,
):
    users = tmp_path / "ui-users"
    users.write_text("operator=pbkdf2_sha256$1$abc$def\n")
    tokens = tmp_path / "event-tokens"
    tokens.write_text("ab12=alertmanager")
    result = _run(
        {
            "OPSPILOT_UI_USERS_FILE": str(users),
            "OPSPILOT_EVENT_TOKENS_FILE": str(tokens),
        },
        "env",
    )
    assert result.returncode == 0, result.stderr
    env = _environment(result.stdout)
    assert env["OPSPILOT_UI_USERS"] == "operator=pbkdf2_sha256$1$abc$def"
    assert env["OPSPILOT_EVENT_TOKENS"] == "ab12=alertmanager"
    assert "OPSPILOT_UI_USERS_FILE" not in env
    assert "OPSPILOT_EVENT_TOKENS_FILE" not in env


def test_the_runtimes_own_env_file_variables_are_passed_through_untouched(tmp_path):
    worker_env = tmp_path / "worker_env"
    worker_env.write_text("# placeholder\n")
    observer_env = tmp_path / "observer_env"
    observer_env.write_text("OPSPILOT_OTEL_TOKEN=not-for-the-observer\n")
    result = _run(
        {
            "OPSPILOT_ENV_FILE": str(worker_env),
            "OPSPILOT_OBSERVER_ENV_FILE": str(observer_env),
        },
        "env",
    )
    assert result.returncode == 0, result.stderr
    env = _environment(result.stdout)
    assert env["OPSPILOT_ENV_FILE"] == str(worker_env)
    assert env["OPSPILOT_OBSERVER_ENV_FILE"] == str(observer_env)
    assert "OPSPILOT_ENV" not in env
    assert "OPSPILOT_OTEL_TOKEN" not in env


def test_the_workers_otel_settings_come_from_its_env_file_into_its_config(tmp_path):
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        "# worker_env\n"
        "DEEPSEEK_API_KEY=sk-model-key\n"
        "  OPSPILOT_OTEL_PROMETHEUS_URL= http://prometheus:9090 \n"
        "OPSPILOT_OTEL_JAEGER_URL=http://jaeger:16686/jaeger/ui\n"
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME='reader'\n"
        'OPSPILOT_OTEL_PROMETHEUS_PASSWORD="p=ss word"\n'
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD=second-ignored\n"
        "OPSPILOT_OTEL_TOKEN=bearer-value"
    )
    result = _run({"OPSPILOT_ENV_FILE": str(worker_env)}, "env")
    assert result.returncode == 0, result.stderr
    env = _environment(result.stdout)
    # The model key stays in the file; worker_main reads it from there.
    assert "DEEPSEEK_API_KEY" not in env
    config = OtelDemoConfig.from_env(env)
    assert config.prometheus_url == "http://prometheus:9090"
    assert config.jaeger_url == "http://jaeger:16686/jaeger/ui"
    assert config.prometheus_basic_auth == ("reader", "p=ss word")
    assert config.token == "bearer-value"
    assert "bearer-value" not in result.stderr


def test_an_otel_setting_both_inline_and_in_the_env_file_is_refused(tmp_path):
    worker_env = tmp_path / "worker_env"
    worker_env.write_text("OPSPILOT_OTEL_PROMETHEUS_PASSWORD=from-file\n")
    result = _run(
        {
            "OPSPILOT_ENV_FILE": str(worker_env),
            "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": "inline-value",
        },
        "env",
    )
    assert result.returncode != 0
    assert "OPSPILOT_OTEL_PROMETHEUS_PASSWORD" in result.stderr
    assert "from-file" not in result.stderr + result.stdout
    assert "inline-value" not in result.stderr + result.stdout


def test_an_unreadable_worker_env_file_refuses_to_start(tmp_path):
    result = _run({"OPSPILOT_ENV_FILE": str(tmp_path / "absent")}, "env")
    assert result.returncode != 0
    assert "OPSPILOT_ENV_FILE" in result.stderr


def test_a_missing_secret_file_refuses_to_start_without_printing_a_value(tmp_path):
    result = _run({"OPSPILOT_UI_USERS_FILE": str(tmp_path / "absent")}, "env")
    assert result.returncode != 0
    assert "OPSPILOT_UI_USERS_FILE" in result.stderr


@pytest.mark.parametrize("name", ["OPSPILOT_UI_USERS", "OPSPILOT_EVENT_TOKENS"])
def test_a_variable_set_both_inline_and_by_file_is_refused(tmp_path, name):
    secret = tmp_path / "s"
    secret.write_text("x=y")
    result = _run({name: "inline=1", f"{name}_FILE": str(secret)}, "env")
    assert result.returncode != 0
    assert "inline=1" not in result.stderr + result.stdout
