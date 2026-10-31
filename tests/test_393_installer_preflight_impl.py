"""#393 install.sh with the agent profile, without Docker: a stand-in
``docker`` on PATH records each call (as in the #392 tests), so the
preflight refusal, the remembered Observer URL and the upgrade order with
worker and Observer are observable in PR CI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "compose" / "install.sh"

FAKE_DOCKER = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "env": {{k: os.environ.get(k) for k in (
        "OPSPILOT_OBSERVER_PROMETHEUS_URL", "OPSPILOT_START_AGENT",
        "COMPOSE_PROFILES")}}}}) + "\\n")
if args[:2] == ["volume", "ls"]:
    print(os.environ.get("FAKE_DOCKER_VOLUMES", ""))
"""

KEY = "sk-test-393-not-a-real-key-7f3a"
URL = "http://prometheus-observer.example:9090"


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    secrets = tmp_path / "home" / ".config" / "opspilot"

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path / "home"),
            "FAKE_DOCKER_LOG": str(log),
            **extra,
        }
        return subprocess.run(
            ["bash", str(INSTALL), *args],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    def calls() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def reset() -> None:
        log.unlink(missing_ok=True)

    def worker_env(text: str) -> None:
        secrets.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = secrets / "worker_env"
        path.write_text(text)
        path.chmod(0o600)

    run.calls = calls  # type: ignore[attr-defined]
    run.reset = reset  # type: ignore[attr-defined]
    run.secrets = secrets  # type: ignore[attr-defined]
    run.worker_env = worker_env  # type: ignore[attr-defined]
    return run


def _compose(calls: list[dict]) -> list[dict]:
    """Compose calls with ``args`` after ``compose -p <p> -f <file>``."""
    return [
        {"args": c["args"][5:], "env": c["env"]}
        for c in calls
        if c["args"][:1] == ["compose"]
    ]


def _index(lines: list[dict], predicate) -> int:
    return next(i for i, line in enumerate(lines) if predicate(line["args"]))


@pytest.mark.parametrize("command", ["install", "upgrade"])
def test_agent_without_a_worker_env_is_refused_before_any_docker_call(host, command):
    result = host(
        command, OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL=URL
    )
    assert result.returncode != 0
    assert host.calls() == []
    assert "worker_env" in result.stderr and "DEEPSEEK_API_KEY" in result.stderr
    assert "nothing changed" in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        "# worker_env: placeholder\n",
        "DEEPSEEK_API_KEY=\n",
        "DEEPSEEK_API_KEY=''\n",
        'DEEPSEEK_API_KEY=  ""  \n',
        "DEEPSEEK_API_KEY_OLD=x\n",
        "export DEEPSEEK_API_KEY=x\n",
        # The worker reads the first DEEPSEEK_API_KEY line only.
        f"DEEPSEEK_API_KEY=\nDEEPSEEK_API_KEY={KEY}\n",
    ],
)
def test_a_worker_env_without_a_usable_model_key_is_refused(host, text):
    host.worker_env(text)
    result = host(OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL=URL)
    assert result.returncode != 0
    assert host.calls() == []
    assert str(host.secrets / "worker_env") in result.stderr
    assert "DEEPSEEK_API_KEY" in result.stderr


@pytest.mark.parametrize(
    "url", ["", "prometheus:9090", "ftp://prometheus:9090", "http://", "https://"]
)
def test_a_missing_or_non_http_observer_url_is_refused(host, url):
    host.worker_env(f"DEEPSEEK_API_KEY={KEY}\n")
    extra = {"OPSPILOT_START_AGENT": "1"}
    if url:
        extra["OPSPILOT_OBSERVER_PROMETHEUS_URL"] = url
    result = host(**extra)
    assert result.returncode != 0
    assert host.calls() == []
    assert "OPSPILOT_OBSERVER_PROMETHEUS_URL" in result.stderr
    if url:
        assert url not in result.stderr


# The Observer refuses these at start (opspilot/observer/prometheus.py:
# userinfo, an empty host, a port urlsplit cannot read); the installer must
# refuse them first, before any Docker call, without echoing the URL (a
# userinfo URL may carry a password).
@pytest.mark.parametrize(
    "url",
    [
        "http://reader:s3cretpw@prometheus:9090",
        "https://reader@prometheus:9090",
        "http://@prometheus:9090",
        "http://:9090",
        "https:///api/v1",
        "http://prometheus:abc",
        "http://prometheus:0",
        "http://prometheus:65536",
        "http://prometheus:90a0/",
    ],
)
def test_an_observer_url_the_observer_would_reject_is_refused(host, url):
    host.worker_env(f"DEEPSEEK_API_KEY={KEY}\n")
    result = host(OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL=url)
    assert result.returncode != 0
    assert host.calls() == []
    assert "OPSPILOT_OBSERVER_PROMETHEUS_URL" in result.stderr
    assert url not in result.stderr and "s3cretpw" not in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "http://prometheus",
        "http://opspilot-m1-control-plane:30090",
        "https://prometheus.example:65535/prefix",
        "http://[::1]:9090",
    ],
)
def test_an_observer_url_the_observer_accepts_starts_the_agent(host, url):
    host.worker_env(f"DEEPSEEK_API_KEY={KEY}\n")
    result = host(OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL=url)
    assert result.returncode == 0, result.stderr
    start = _compose(host.calls())[-1]
    assert {"web", "worker", "observer"} <= set(start["args"])
    assert start["env"]["OPSPILOT_OBSERVER_PROMETHEUS_URL"] == url


def test_refusals_never_print_the_model_key(host):
    host.worker_env(f"DEEPSEEK_API_KEY={KEY}\n")
    refused = host(OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL="ftp://x")
    accepted = host(OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL=URL)
    assert refused.returncode != 0 and accepted.returncode == 0, accepted.stderr
    for result in (refused, accepted):
        assert KEY not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "text",
    [
        f"DEEPSEEK_API_KEY={KEY}\n",
        f"  DEEPSEEK_API_KEY= '{KEY}'  \n",
        f'# model\nLANGSMITH_API_KEY=x\nDEEPSEEK_API_KEY="{KEY}"\n',
    ],
)
def test_a_filled_worker_env_and_http_url_start_web_worker_and_observer(host, text):
    host.worker_env(text)
    result = host(
        OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL="https://p:9090"
    )
    assert result.returncode == 0, result.stderr
    assert (host.secrets / "worker_env").read_text() == text
    lines = _compose(host.calls())
    start = lines[-1]
    assert start["args"][:2] == ["up", "--detach"]
    assert {"web", "worker", "observer"} <= set(start["args"])
    assert start["env"]["COMPOSE_PROFILES"] == "agent"
    assert start["env"]["OPSPILOT_OBSERVER_PROMETHEUS_URL"] == "https://p:9090"


def test_the_observer_url_is_remembered_for_upgrade(host):
    host.worker_env(f"DEEPSEEK_API_KEY={KEY}\n")
    assert (
        host(OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL=URL).returncode
        == 0
    )
    assert (
        f"OPSPILOT_OBSERVER_PROMETHEUS_URL={URL}\n"
        in (host.secrets / "install.env").read_text()
    )
    host.reset()
    result = host("upgrade")
    assert result.returncode == 0, result.stderr
    start = _compose(host.calls())[-1]
    assert {"web", "worker", "observer"} <= set(start["args"])
    assert start["env"]["OPSPILOT_OBSERVER_PROMETHEUS_URL"] == URL


def test_a_remembered_agent_upgrade_is_refused_once_the_key_is_gone(host):
    host.worker_env(f"DEEPSEEK_API_KEY={KEY}\n")
    assert (
        host(OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL=URL).returncode
        == 0
    )
    host.worker_env("# key removed\n")
    host.reset()
    result = host("upgrade")
    assert result.returncode != 0
    assert host.calls() == []
    assert "DEEPSEEK_API_KEY" in result.stderr


def test_upgrade_with_the_agent_stops_runtimes_migrates_then_starts_all_three(host):
    host.worker_env(f"DEEPSEEK_API_KEY={KEY}\n")
    assert (
        host(OPSPILOT_START_AGENT="1", OPSPILOT_OBSERVER_PROMETHEUS_URL=URL).returncode
        == 0
    )
    host.reset()
    assert host("upgrade").returncode == 0
    lines = _compose(host.calls())
    stop = _index(lines, lambda a: a[0] == "stop")
    postgres = _index(lines, lambda a: a[0] == "up" and "postgres" in a)
    migrate = _index(lines, lambda a: a[0] == "up" and "migrate" in a)
    roles = _index(lines, lambda a: a[0] == "up" and "init-roles" in a)
    start = _index(lines, lambda a: a[0] == "up" and "worker" in a)
    assert set(lines[stop]["args"][1:]) == {"web", "worker", "observer"}
    assert lines[stop]["env"]["COMPOSE_PROFILES"] == "agent"
    assert stop < postgres < migrate < roles < start == len(lines) - 1
    assert {"web", "worker", "observer"} <= set(lines[start]["args"])


def test_without_the_agent_nothing_is_required_and_only_the_web_starts(host):
    result = host()
    assert result.returncode == 0, result.stderr
    start = _compose(host.calls())[-1]
    assert "web" in start["args"]
    assert "worker" not in start["args"] and "observer" not in start["args"]


def test_an_empty_target_identities_file_is_written_once_and_kept(host):
    assert host().returncode == 0
    path = host.secrets / "target_identities.json"
    assert json.loads(path.read_text()) == {}
    entry = {"svc": {"integration_id": "i", "cluster_uid": "c", "namespace": "n"}}
    path.write_text(json.dumps(entry))
    assert host("upgrade").returncode == 0
    assert json.loads(path.read_text()) == entry


def test_an_install_from_before_target_identities_still_upgrades(host):
    assert host().returncode == 0
    (host.secrets / "target_identities.json").unlink()
    host.reset()
    result = host("upgrade", FAKE_DOCKER_VOLUMES="opspilot_pgdata")
    assert result.returncode == 0, result.stderr
    assert json.loads((host.secrets / "target_identities.json").read_text()) == {}
