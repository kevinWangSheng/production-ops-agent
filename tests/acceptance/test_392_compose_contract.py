"""Static Compose contract checks for issue #392."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "compose" / "compose.yaml"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$", re.I)


def _compose() -> dict[str, Any]:
    assert COMPOSE.is_file(), (
        f"required #392 seam is missing: {COMPOSE.relative_to(REPO)}"
    )
    try:
        import yaml
    except Exception as exc:  # pragma: no cover - environment failure is a test failure
        pytest.fail(f"PyYAML is required to parse the Compose contract: {exc}")
    document = yaml.safe_load(COMPOSE.read_text())
    assert isinstance(document, dict), "Compose document must be a mapping"
    assert isinstance(document.get("services"), dict), "Compose has no services mapping"
    return document


def _environment(service: dict[str, Any]) -> dict[str, str | None]:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return {
            str(key): None if value is None else str(value)
            for key, value in raw.items()
        }
    if isinstance(raw, list):
        result: dict[str, str | None] = {}
        for entry in raw:
            key, separator, value = str(entry).partition("=")
            result[key] = value if separator else None
        return result
    return {}


def _secret_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("source"):
            names.append(str(item["source"]))
    return names


def _published_ports(service: dict[str, Any]) -> list[tuple[str | None, str]]:
    result: list[tuple[str | None, str]] = []
    for entry in service.get("ports", []) or []:
        if isinstance(entry, str):
            # Short syntax [HOST_IP:]PUBLISHED:TARGET[/proto]; PUBLISHED may be
            # an interpolation such as ${VAR:-18080}, which itself contains ":".
            match = re.fullmatch(
                r"(?:(?P<ip>\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]):)?"
                r"(?P<published>.+):(?P<target>\d+)(?:/\w+)?",
                entry,
            )
            if match:
                result.append((match["ip"], f"{match['published']}:{match['target']}"))
            else:
                result.append((None, entry))
        elif isinstance(entry, dict):
            host_ip = entry.get("host_ip")
            published = entry.get("published")
            if published is not None:
                result.append(
                    (None if host_ip is None else str(host_ip), str(published))
                )
    return result


def test_required_services_include_one_shot_migration_and_role_initialization() -> None:
    services = _compose()["services"]
    assert {"postgres", "migrate", "init-roles", "web"} <= set(services)
    for name in ("migrate", "init-roles"):
        service = services[name]
        assert service.get("restart") == "no", f"{name} must be one-shot"
        assert service.get("command") or service.get("entrypoint"), (
            f"{name} must expose its one-shot product command"
        )


def test_all_product_services_share_web_image_and_third_party_images_are_digest_pinned() -> (
    None
):
    # The product image digest pin moves to #394 (user decision 2026-10-28):
    # Compose cannot carry the digest of an image first published by #394.
    services = _compose()["services"]
    web_image = services["web"].get("image")
    assert isinstance(web_image, str) and web_image, web_image

    product_services = {
        "migrate",
        "init-roles",
        "web",
        "worker",
        "observer",
        "retention",
    }
    for name in product_services & set(services):
        image = services[name].get("image")
        assert image == web_image, f"{name} must use the single product image"

    for name, service in services.items():
        if name in product_services:
            continue
        image = service.get("image")
        if image is not None:
            assert isinstance(image, str) and DIGEST.search(image), (name, image)


def test_postgres_has_no_host_port_and_web_is_loopback_only_with_configurable_default() -> (
    None
):
    services = _compose()["services"]
    postgres = services["postgres"]
    assert not postgres.get("ports"), "Postgres must remain Compose-internal"

    ports = _published_ports(services["web"])
    assert ports, "web must publish a host port"
    assert any(
        host_ip == "127.0.0.1"
        and "OPSPILOT_WEB_HOST_PORT" in published
        and "18080" in published
        for host_ip, published in ports
    ), ports

    environment = _environment(services["web"])
    allowed = environment.get("OPSPILOT_ALLOWED_ORIGINS")
    assert allowed is not None
    assert "OPSPILOT_WEB_HOST_PORT" in allowed and "18080" in allowed


def test_web_uses_file_secrets_without_plaintext_credentials_in_compose() -> None:
    document = _compose()
    services = document["services"]
    secrets = document.get("secrets")
    assert isinstance(secrets, dict) and secrets, (
        "Compose must declare file-backed secrets"
    )

    web_secret_names = _secret_names(services["web"].get("secrets"))
    assert len(web_secret_names) >= 3, (
        "web needs separate DB-password, UI-user-hash, and event-token secret files"
    )
    for name in web_secret_names:
        definition = secrets.get(name)
        assert isinstance(definition, dict) and definition.get("file"), name
        secret_file = str(definition["file"])
        assert "OPSPILOT_SECRETS_DIR" in secret_file
        assert not secret_file.startswith(("./", "/workspace/", "/app/"))

    environment = _environment(services["web"])
    for key in ("POSTGRES_PASSWORD", "OPSPILOT_UI_USERS", "OPSPILOT_EVENT_TOKENS"):
        if key in environment:
            value = environment[key]
            assert (
                value is None
                or value.startswith("/run/secrets/")
                or key.endswith("_FILE")
            ), f"{key} must not carry a plaintext credential in Compose"
