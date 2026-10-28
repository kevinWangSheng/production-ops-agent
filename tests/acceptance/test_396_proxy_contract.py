"""Offline acceptance tests for the #396 Prometheus proxy seam."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest


def _kind_lab() -> Any:
    try:
        from scripts import kind_lab
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.fail(f"kind_lab public seam is unavailable: {exc}", pytrace=False)
    return kind_lab


def _accounts(lab: Any) -> dict[str, str]:
    return {name: f"secret-{name}" for name in lab.PROMETHEUS_ACCOUNTS}


def _block(config: str, marker: str) -> str:
    start = config.find(marker)
    assert start >= 0, f"nginx config has no block for {marker!r}"
    opening = config.find("{", start)
    assert opening >= 0, f"nginx config has no opening brace for {marker!r}"
    depth = 0
    for index in range(opening, len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return config[opening + 1 : index]
    pytest.fail(f"nginx config has no closing brace for {marker!r}")


def test_holmes_is_a_proxy_account_without_changing_existing_accounts() -> None:
    lab = _kind_lab()
    assert "holmes" in lab.PROMETHEUS_ACCOUNTS
    assert {"investigator", "observer", "lab"}.issubset(set(lab.PROMETHEUS_ACCOUNTS))
    assert lab.ROLE_ENV_VARS["holmes"] == (
        "OPSPILOT_HOLMES_PROMETHEUS_USERNAME",
        "OPSPILOT_HOLMES_PROMETHEUS_PASSWORD",
    )

    config = lab.proxy_nginx_config(_accounts(lab))
    # The existing read-only gate remains the default for every account.
    assert "if ($request_method !~ ^(GET|HEAD)$) { return 403; }" in config
    # The only write exception is named and bounded at the proxy edge.
    assert re.search(r"(?s)\$remote_user.*holmes|holmes.*\$remote_user", config)
    assert "client_max_body_size 64k;" in config
    assert "proxy_pass_request_body on;" in config
    assert "proxy_pass_request_body off;" in config
    assert "application/x-www-form-urlencoded" in config

    _block(config, "location = /api/v1/query")
    _block(config, "location = /api/v1/query_range")
    for path in ("/api/v1/query", "/api/v1/query_range"):
        assert re.search(
            rf"(?s)(?:{re.escape(path)}.*holmes|holmes.*{re.escape(path)})",
            config,
        )

    # The metadata and rules APIs remain outside the explicitly granted query
    # surface.  They must not appear in a POST grant or a read-path list.
    for denied in ("/api/v1/metadata", "/api/v1/rules"):
        assert denied not in config or re.search(
            rf"(?s){re.escape(denied)}.*?(?:return 403|@denied)", config
        )


def test_f7_probe_has_holmes_rows_and_preserves_other_account_expectations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        from scripts import f7_authz_probe as probe_module
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.fail(f"f7 probe public seam is unavailable: {exc}", pytrace=False)

    def fake_authorization(role: str, directory: Path) -> str:
        return f"auth-{role}"

    def fake_latest_samples(base_url: str, auth: str) -> tuple[int, dict[str, float]]:
        return 200, {}

    def fake_request(
        base_url: str,
        method: str,
        path: str,
        auth: str | None,
        *,
        body: str | None = None,
    ) -> tuple[int, bytes]:
        role = str(auth).removeprefix("auth-")
        clean_path = path.split("?", 1)[0]
        if (
            "//" in clean_path
            or "/./" in clean_path
            or "%2F" in clean_path
            or clean_path != clean_path.lower()
        ):
            return 400, b""
        if method == "POST":
            if role == "holmes" and body is not None:
                if len(probe_module.form_body(path, body)) > 64 * 1024:
                    return 413, b""
                if clean_path in {"/api/v1/query", "/api/v1/query_range"}:
                    return 200, b""
            return 403, b""
        if method in {"GET", "HEAD"} and (
            clean_path
            in {
                "/api/v1/query",
                "/api/v1/query_range",
                "/api/v1/series",
                "/api/v1/labels",
                "/api/v1/status/buildinfo",
                "/federate",
            }
            or clean_path.startswith("/api/v1/label/")
        ):
            if clean_path == "/federate":
                return 403, b""
            return (405 if method == "HEAD" else 200), b""
        return 403, b""

    monkeypatch.setattr(probe_module, "authorization", fake_authorization)
    monkeypatch.setattr(probe_module, "latest_samples", fake_latest_samples)
    monkeypatch.setattr(probe_module, "request", fake_request)
    report = probe_module.probe(base_url="http://probe.invalid", directory=Path("."))

    rows = report["requests"]
    holmes_rows = [row for row in rows if row["account"] == "holmes"]
    assert holmes_rows
    assert all(row["matches"] is True for row in holmes_rows)

    form_holmes_rows = [
        row
        for row in holmes_rows
        if row["method"] == "POST"
        and row["path"] in {"/api/v1/query", "/api/v1/query_range"}
        and row.get("body") is not None
        and len(str(row["body"]).encode()) <= 64 * 1024
    ]
    assert {row["path"] for row in form_holmes_rows} >= {
        "/api/v1/query",
        "/api/v1/query_range",
    }
    assert all(
        row["status"] == 200 and row["matches"] is True for row in form_holmes_rows
    )

    non_form_rows = [
        row
        for row in holmes_rows
        if row["method"] == "POST"
        and row["path"] in {"/api/v1/query", "/api/v1/query_range"}
        and row.get("body") is None
    ]
    assert non_form_rows
    assert all(row["status"] == 403 for row in non_form_rows)

    oversized_rows = [
        row
        for row in holmes_rows
        if row["method"] == "POST"
        and row.get("body") is not None
        and len(str(row["body"]).encode()) > 64 * 1024
    ]
    assert oversized_rows
    assert all(row["status"] == 413 for row in oversized_rows)

    other_path_rows = [
        row
        for row in holmes_rows
        if row["method"] == "POST"
        and row["path"] not in {"/api/v1/query", "/api/v1/query_range"}
    ]
    assert other_path_rows
    assert all(row["status"] in {403, 413} for row in other_path_rows)

    for role in ("investigator", "observer", "lab"):
        for path in ("/api/v1/query", "/api/v1/query_range"):
            matching = [
                row
                for row in rows
                if row["account"] == role
                and row["method"] == "POST"
                and row["path"] == path
            ]
            assert matching, (role, path)
            assert all(row["status"] == 403 and row["matches"] for row in matching)
