"""Offline provisioning and auth-boundary checks; no cluster commands."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from scripts import kind_lab as lab


def accounts() -> dict[str, str]:
    return {name: f"secret-{name}" for name in lab.PROMETHEUS_ACCOUNTS}


def test_upstream_and_read_accounts_are_disjoint() -> None:
    users = accounts()

    def hashed(_: str) -> str:
        return "$2y$10$fake"

    web = yaml.safe_load(lab.web_config_yaml(users, hasher=hashed))
    assert set(web["basic_auth_users"]) == {"collector", "proxy"}
    auth = lab.proxy_htpasswd(users, hasher=hashed)
    assert {line.split(":")[0] for line in auth.splitlines()} == {
        "investigator",
        "observer",
        "lab",
    }
    assert all(line.endswith(":$2y$10$fake") for line in auth.splitlines())


def test_old_auth_gains_only_proxy(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    old = {k: v for k, v in accounts().items() if k != "proxy"}
    lab._private_write(path, json.dumps(old))
    new = lab.prometheus_accounts(path)
    assert {k: new[k] for k in old} == old
    assert set(new) - set(old) == {"proxy"}
    assert lab.prometheus_accounts(path) == new
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    del new["lab"]
    lab._private_write(path, json.dumps(new))
    with pytest.raises(SystemExit):
        lab.prometheus_accounts(path)


def test_exact_nginx_rules_and_normalized_upstream() -> None:
    config = lab.proxy_nginx_config(accounts())
    for path in lab.READ_PATHS:
        assert f"location = {path} {{ try_files /__absent__ @read; }}" in config
    assert "location ~ ^/api/v1/label/[A-Za-z_][A-Za-z0-9_]*/values$" in config
    assert "location / { try_files /__absent__ @denied; }" in config
    assert "location @denied { return 403; }" in config
    assert "if ($request_method !~ ^(GET|HEAD)$) { return 403; }" in config
    assert "proxy_pass http://prometheus_backend$uri$is_args$args;" in config
    assert "$request_uri" not in config and "$http_authorization" not in config
    assert (
        f'proxy_set_header Authorization "{lab.basic_auth_header(accounts(), "proxy")}";'
        in config
    )
    assert "auth_basic_user_file /etc/opspilot-proxy/htpasswd;" in config
    for denied in (
        "/api/v1/otlp",
        "/api/v1/write",
        "/api/v1/admin",
        "/-/",
        "/federate",
    ):
        assert f"location {denied}" not in config
    assert "access_log off;" in config


def test_manifest_and_values() -> None:
    deployment, service = list(
        yaml.safe_load_all((lab.CONFIG / "prometheus-readonly-proxy.yaml").read_text())
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "nginx:1.28.0-alpine"
    assert container["resources"]["limits"]["memory"] == "64Mi"
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe] == {"tcpSocket": {"port": 8080}}
    assert service["spec"]["ports"][0]["nodePort"] == 30090
    upstream = yaml.safe_load((lab.CONFIG / "values.yaml").read_text())["prometheus"][
        "server"
    ]["service"]
    assert upstream["type"] == "ClusterIP"
    assert "nodePort" not in upstream


def test_proxy_secret_uses_private_files_and_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    contents: list[str] = []

    def capture(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        for arg in cmd:
            if arg.startswith("--from-file="):
                path = Path(arg.split("=", 2)[2])
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
                contents.append(path.read_text())
        if "secret" in cmd and "get" in cmd:
            return subprocess.CompletedProcess(cmd, 0, '{"data":{"config":"hash"}}')
        return subprocess.CompletedProcess(cmd, 0, "SECRET-BYTES")

    def apply(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert kw["input"] == "SECRET-BYTES"
        return subprocess.CompletedProcess(cmd, 0, "")

    monkeypatch.setattr(lab, "capture", capture)
    monkeypatch.setattr(subprocess, "run", apply)
    monkeypatch.setattr(lab, "bcrypt_hash", lambda _: "$2y$10$test")
    # Default arguments bind the real bcrypt helper; use an explicit renderer
    # to keep this test offline and independent from htpasswd availability.
    monkeypatch.setattr(
        lab,
        "proxy_htpasswd",
        lambda a: "".join(f"{r}:$2y$10$test\n" for r in lab.ROLE_ENV_VARS),
    )
    monkeypatch.setattr(
        lab, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0)
    )
    assert lab.apply_readonly_proxy(accounts()) == 0
    assert len(contents) == 2
    printed = capsys.readouterr()
    visible = repr(calls) + printed.out + printed.err
    assert "SECRET-BYTES" not in visible
    for password in accounts().values():
        assert password not in visible
    assert lab.basic_auth_header(accounts(), "proxy") not in visible


def test_bcrypt_password_only_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert "password-canary" not in repr(cmd)
        assert kw["input"] == "password-canary\n"
        return subprocess.CompletedProcess(cmd, 0, "x:$2y$10$hash")

    monkeypatch.setattr(subprocess, "run", run)
    assert lab.bcrypt_hash("password-canary") == "$2y$10$hash"


def test_nginx_syntax_if_available(tmp_path: Path) -> None:
    nginx = shutil.which("nginx")
    if nginx is None:
        pytest.skip("nginx unavailable; no container required")
    config = lab.proxy_nginx_config(accounts()).replace(
        "prometheus:9090", "127.0.0.1:9090"
    )
    config = config.replace("/etc/opspilot-proxy/htpasswd", str(tmp_path / "htpasswd"))
    config = config.replace("pid /tmp/nginx.pid;", f"pid {tmp_path}/nginx.pid;")
    path = tmp_path / "nginx.conf"
    lab._private_write(path, config)
    lab._private_write(tmp_path / "htpasswd", "lab:$2y$10$fake\n")
    result = subprocess.run(
        [nginx, "-t", "-p", str(tmp_path), "-c", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
