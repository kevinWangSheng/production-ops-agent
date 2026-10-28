"""#392 static shape of the image and Compose file (no Docker needed)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "deploy" / "compose" / "compose.yaml").read_text())
SERVICES = COMPOSE["services"]
PRODUCT = ("migrate", "init-roles", "web", "worker", "observer", "retention")
RUNTIMES = ("web", "worker", "observer")


def test_one_product_image_serves_every_role():
    assert {SERVICES[name]["image"] for name in PRODUCT} == {
        "${OPSPILOT_IMAGE:-opspilot:local}"
    }


def test_postgres_is_pinned_by_digest_and_publishes_no_port():
    assert re.fullmatch(r"postgres@sha256:[0-9a-f]{64}", SERVICES["postgres"]["image"])
    assert "ports" not in SERVICES["postgres"]
    assert all("ports" not in SERVICES[name] for name in SERVICES if name != "web")


def test_the_workbench_is_published_on_host_loopback_only():
    assert SERVICES["web"]["ports"] == [
        {
            "target": 8080,
            "host_ip": "127.0.0.1",
            "published": "${OPSPILOT_WEB_HOST_PORT:-18080}",
            "protocol": "tcp",
        }
    ]
    env = SERVICES["web"]["environment"]
    assert env["OPSPILOT_BIND"] == "0.0.0.0:8080"
    assert env["OPSPILOT_ALLOWED_ORIGINS"] == (
        "http://127.0.0.1:${OPSPILOT_WEB_HOST_PORT:-18080},"
        "http://localhost:${OPSPILOT_WEB_HOST_PORT:-18080}"
    )


def test_runtimes_start_only_after_migrate_and_init_roles_completed():
    assert SERVICES["migrate"]["depends_on"] == {
        "postgres": {"condition": "service_healthy"}
    }
    assert SERVICES["init-roles"]["depends_on"] == {
        "migrate": {"condition": "service_completed_successfully"}
    }
    for name in RUNTIMES:
        assert SERVICES[name]["depends_on"] == {
            "init-roles": {"condition": "service_completed_successfully"}
        }


def test_each_service_mounts_only_its_own_secret_files():
    mounted = {name: set(SERVICES[name].get("secrets", ())) for name in SERVICES}
    assert mounted["web"] == {"web_pgpass", "web_ui_users", "web_event_tokens"}
    assert mounted["worker"] == {"worker_pgpass", "worker_env"}
    assert mounted["observer"] == {"observer_pgpass", "observer_env"}
    assert mounted["retention"] == {"retention_pgpass"}
    assert mounted["migrate"] == {"owner_pgpass"}
    assert mounted["postgres"] == {"postgres_password"}
    assert (
        "owner_pgpass" not in mounted["web"] | mounted["worker"] | mounted["observer"]
    )


def test_secret_files_come_from_the_secrets_dir_and_nothing_secret_is_inline():
    for name, secret in COMPOSE["secrets"].items():
        assert secret["file"] == (
            "${OPSPILOT_SECRETS_DIR:?run deploy/compose/install.sh}/" + name
        )
    for name, service in SERVICES.items():
        for key, value in (service.get("environment") or {}).items():
            text = str(value).lower()
            assert "password=" not in text, (name, key)
            if key.endswith("_DSN"):
                assert "passfile=/run/secrets/" in text, (name, key)
            if key in ("OPSPILOT_UI_USERS", "OPSPILOT_EVENT_TOKENS"):
                raise AssertionError(f"{name}: {key} inline")


def test_product_containers_run_as_a_non_root_user():
    for name in PRODUCT:
        user = SERVICES[name]["user"]
        assert user.startswith("${OPSPILOT_RUNTIME_UID:?"), name
        assert SERVICES[name]["read_only"] is True


DOCKERFILE = (ROOT / "Dockerfile").read_text()
IGNORE = [
    line.strip()
    for line in (ROOT / ".dockerignore").read_text().splitlines()
    if line.strip() and not line.startswith("#")
]


def test_the_image_is_built_from_pinned_bases_and_runs_non_root():
    froms = re.findall(r"^FROM (\S+)", DOCKERFILE, re.MULTILINE)
    assert froms and all(re.search(r"@sha256:[0-9a-f]{64}$", ref) for ref in froms)
    users = re.findall(r"^USER (\S+)", DOCKERFILE, re.MULTILINE)
    assert users and users[-1].split(":")[0] not in ("0", "root")
    assert (
        "uv sync --locked --no-dev --no-default-groups --group m0 --group web"
        in DOCKERFILE
    )


def test_the_build_context_excludes_env_files_tmp_evidence_and_vcs():
    assert IGNORE[0] == "*"
    for entry in ("**/.env", "**/.env.*", "tmp/", "docs/evidence/", ".git", ".venv"):
        assert entry in IGNORE
    allowed = {line[1:] for line in IGNORE if line.startswith("!")}
    assert allowed == {
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "opspilot/",
        "deploy/docker-entrypoint.sh",
    }
