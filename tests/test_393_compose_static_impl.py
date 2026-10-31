"""#393 static shape of the runtimes in the Compose file and the upgrade
notes (no Docker needed)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = ROOT / "deploy" / "compose"
COMPOSE = yaml.safe_load((COMPOSE_DIR / "compose.yaml").read_text())
SERVICES = COMPOSE["services"]
RUNTIMES = ("web", "worker", "observer")


def test_worker_capacity_and_pool_size_are_explicit_with_two_spare_connections():
    env = SERVICES["worker"]["environment"]
    capacity = int(env["OPSPILOT_WORKER_CAPACITY"])
    pool = int(env["OPSPILOT_POOL_MAX_SIZE"])
    assert capacity >= 1 and pool >= capacity + 2


def test_each_runtime_runs_exactly_one_replica():
    for name in RUNTIMES:
        assert SERVICES[name]["deploy"] == {"replicas": 1}, name


def test_model_and_observer_credentials_reach_only_their_own_service():
    holders = {
        secret: {n for n, s in SERVICES.items() if secret in s.get("secrets", ())}
        for secret in COMPOSE["secrets"]
    }
    assert holders["worker_env"] == {"worker"}
    assert holders["observer_env"] == {"observer"}
    assert holders["worker_pgpass"] == {"worker", "init-roles"}
    assert holders["observer_pgpass"] == {"observer", "init-roles"}
    for name in ("web", "worker", "observer"):
        env = SERVICES[name]["environment"]
        assert not any("DEEPSEEK" in key for key in env), name
    observer_env = SERVICES["observer"]["environment"]
    assert observer_env["OPSPILOT_OBSERVER_ENV_FILE"] == "/run/secrets/observer_env"
    assert not any(k.startswith("OPSPILOT_OTEL") for k in observer_env)


def test_the_observer_url_comes_from_the_install_settings():
    env = SERVICES["observer"]["environment"]
    assert env["OPSPILOT_OBSERVER_PROMETHEUS_URL"] == (
        "${OPSPILOT_OBSERVER_PROMETHEUS_URL:-}"
    )


def test_upgrade_notes_give_the_stop_migrate_start_order():
    text = (COMPOSE_DIR / "UPGRADE.md").read_text()
    steps = re.findall(r"^\d\. (\S+)", text, re.MULTILINE)
    assert steps == ["stop", "start", "`migrate`", "`init-roles`", "start"]
    assert "install.sh upgrade" in text
    assert "OPSPILOT_START_AGENT=1" in text


def test_the_target_identities_reach_the_web_only_as_a_config_file():
    assert COMPOSE["configs"]["web_target_identities"] == {
        "file": "${OPSPILOT_SECRETS_DIR:?run deploy/compose/install.sh}"
        "/target_identities.json"
    }
    holders = {n for n, s in SERVICES.items() if "configs" in s}
    assert holders == {"web"}
    (config,) = SERVICES["web"]["configs"]
    assert config["source"] == "web_target_identities"
    assert (
        SERVICES["web"]["environment"]["OPSPILOT_TARGET_IDENTITIES"]
        == (config["target"])
    )
