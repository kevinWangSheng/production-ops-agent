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
        "holmes",
    }
    assert all(line.endswith(":$2y$10$fake") for line in auth.splitlines())


@pytest.mark.parametrize(
    "absent", [{"proxy", "holmes"}, {"holmes"}], ids=["four-accounts", "five-accounts"]
)
def test_old_auth_gains_only_proxy_and_holmes(tmp_path: Path, absent: set[str]) -> None:
    path = tmp_path / "auth.json"
    old = {k: v for k, v in accounts().items() if k not in absent}
    lab._private_write(path, json.dumps(old))
    new = lab.prometheus_accounts(path)
    assert {k: new[k] for k in old} == old
    assert set(new) - set(old) == absent
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


# --- #396: the holmes account (POST form bodies on query/query_range only)


def test_holmes_form_post_is_limited_to_the_query_paths() -> None:
    config = lab.proxy_nginx_config(accounts())
    # Only holmes, only POST, only the two query paths, only a form body.
    assert (
        '"~^holmes:POST:/api/v1/query(_range)?:'
        'application/x-www-form-urlencoded(;.*)?$" 1;' in config
    )
    read = config.split("location @read {", 1)[1].split("\n    }", 1)[0]
    assert "error_page 418 = @holmes_form;" in read
    assert "if ($opspilot_holmes_form) { return 418; }" in read
    # Everyone else keeps the frozen GET/HEAD rule and never forwards a body.
    assert "proxy_pass_request_body off;" in read
    form = config.split("location @holmes_form {", 1)[1].split("\n    }", 1)[0]
    assert "proxy_pass_request_body off;" not in form
    assert "proxy_pass_request_body on;" in form
    assert (
        f'proxy_set_header Authorization "{lab.basic_auth_header(accounts(), "proxy")}";'
        in form
    )
    assert "proxy_pass http://prometheus_backend$uri$is_args$args;" in form
    assert "client_max_body_size 64k;" in config
    for denied in ("/api/v1/metadata", "/api/v1/rules"):
        assert denied not in lab.READ_PATHS and denied not in config


def test_holmes_access_log_only_and_without_credentials() -> None:
    config = lab.proxy_nginx_config(accounts())
    fmt = config.split("log_format opspilot_holmes", 1)[1].split(";", 1)[0]
    assert "$remote_user" in fmt and "$uri" in fmt and "$status" in fmt
    for leaked in ("$http_authorization", "$request_body", "$request ", "$args"):
        assert leaked not in fmt
    assert "access_log /dev/stdout opspilot_holmes if=$opspilot_holmes_log;" in config
    assert "map $remote_user $opspilot_holmes_log" in config


def test_holmes_env_file_carries_only_its_own_account(tmp_path: Path) -> None:
    env = lab.role_env_file("holmes", accounts(), tmp_path / "h.env")
    assert env.read_text() == (
        "OPSPILOT_HOLMES_PROMETHEUS_USERNAME=holmes\n"
        "OPSPILOT_HOLMES_PROMETHEUS_PASSWORD=secret-holmes\n"
    )
    assert "holmes" not in lab.web_config_yaml(accounts(), hasher=lambda _: "h")


def test_proxy_command_rewrites_env_files_and_applies_only_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab, "LAB_DIR", tmp_path)
    monkeypatch.setattr(lab, "prometheus_accounts", accounts)
    applied: list[dict[str, str]] = []
    monkeypatch.setattr(lab, "apply_readonly_proxy", lambda a: applied.append(a) or 0)
    assert lab.main(["proxy"]) == 0
    assert applied == [accounts()]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        f"prometheus-{role}.env" for role in lab.ROLE_ENV_VARS
    )
