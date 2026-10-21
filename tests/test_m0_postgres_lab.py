"""The M0 lab's PostgreSQL subprocesses inherit no credentials (#272)."""

import subprocess

from scripts.m0 import postgres_lab as lab


def test_subprocesses_get_a_whitelisted_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "binary", lambda name: f"/bin/{name}")
    credentials = (
        "DEEPSEEK_API_KEY",
        "LANGSMITH_API_KEY",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD",
        "OPSPILOT_LAB_DSN",
        "PGPASSWORD",
        "PGPASSFILE",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "UNEXPECTED_VARIABLE",
    )
    for name in credentials:
        monkeypatch.setenv(name, "credential-canary")
    monkeypatch.setenv("LC_ALL", "C")
    seen = []

    def run(command, **kwargs):
        env = kwargs["env"]
        seen.append(command)
        assert env["LC_ALL"] == "C"
        assert "PATH" in env
        assert not set(credentials) & env.keys()
        assert "credential-canary" not in env.values()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(lab.subprocess, "run", run)
    lab.run("initdb", "-D", tmp_path / "data", "-U", "m0_lab", "--auth=trust")
    lab.run("pg_ctl", "-D", tmp_path / "data", "-w", "start")
    lab.run("pg_ctl", "-D", tmp_path / "data", "-m", "fast", "-w", "stop")
    assert [command[0] for command in seen] == [
        "/bin/initdb",
        "/bin/pg_ctl",
        "/bin/pg_ctl",
    ]
