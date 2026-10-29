"""Offline provisioning and auth-boundary checks; no cluster commands."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import subprocess
import time
import urllib.parse
from collections.abc import Iterator
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
    # the 64k limit lives only on the holmes form path (review C); other
    # accounts keep nginx's default limit
    assert "client_max_body_size 64k;" in form
    assert "client_max_body_size" not in config.split("location @holmes_form", 1)[0]
    assert "if ($opspilot_holmes_form_too_large) { return 413; }" in form
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


def _proxy_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth: dict[str, str]
) -> tuple[int, list[str], dict[str, str]]:
    lab_dir = tmp_path / "lab"
    monkeypatch.setattr(lab, "LAB_DIR", lab_dir)
    monkeypatch.setattr(lab, "AUTH_FILE", lab_dir / "prometheus-auth.json")
    lab._private_write(lab.AUTH_FILE, json.dumps(auth))
    applied: list[str] = []

    def secrets(a: dict[str, str]) -> int:
        applied.append("prometheus-web-config")
        return 0

    def proxy(a: dict[str, str]) -> int:
        applied.append("proxy")
        return 0

    monkeypatch.setattr(lab, "apply_prometheus_auth_secrets", secrets)
    monkeypatch.setattr(lab, "apply_readonly_proxy", proxy)
    code = lab.main(["proxy"])
    return code, applied, json.loads(lab.AUTH_FILE.read_text())


def test_proxy_command_rewrites_env_files_and_applies_only_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, applied, _ = _proxy_command(tmp_path, monkeypatch, accounts())
    assert code == 0
    assert applied == ["proxy"]
    assert sorted(p.name for p in (tmp_path / "lab").iterdir()) == sorted(
        ["prometheus-auth.json"]
        + [f"prometheus-{role}.env" for role in lab.ROLE_ENV_VARS]
    )


def test_proxy_command_refreshes_prometheus_auth_when_proxy_account_is_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #401 bot thread: an older auth file without ``proxy`` gains it here;
    # Prometheus' web config must list it before the proxy uses it, else 401
    old = {k: v for k, v in accounts().items() if k not in {"proxy", "holmes"}}
    code, applied, auth = _proxy_command(tmp_path, monkeypatch, old)
    assert code == 0
    assert "proxy" in auth
    assert applied == ["prometheus-web-config", "proxy"]


# --- #396 review D: the rendered rules against a real nginx 1.28 container.
# CI has no nginx/docker, so this runs only when OPSPILOT_PROXY_DOCKER_CONTEXT
# names a docker context (lab: ``colima-m1-kind``, inside the kind-lab lock).

PROXY_DOCKER = os.environ.get("OPSPILOT_PROXY_DOCKER_CONTEXT")
ECHO_BACKEND = """
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _h(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        out = json.dumps({"method": self.command, "body_bytes": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    do_GET = do_POST = do_HEAD = _h
ThreadingHTTPServer(("0.0.0.0", 9090), H).serve_forever()
"""
FORM = "application/x-www-form-urlencoded"


def _docker(*args: str) -> str:
    env = {**os.environ, "DOCKER_CONTEXT": str(PROXY_DOCKER)}
    return subprocess.run(
        ["docker", *args], env=env, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def live_proxy() -> Iterator[str]:
    if not PROXY_DOCKER:
        pytest.skip("set OPSPILOT_PROXY_DOCKER_CONTEXT to run against real nginx")
    users = accounts()
    # under the repository root: the colima VM mounts the home directory only
    work = lab.ROOT / "tmp/proxy-nginx-test"
    work.mkdir(parents=True, exist_ok=True)
    config = lab.proxy_nginx_config(users).replace("prometheus:9090", "backend:9090")
    (work / "nginx.conf").write_text(config)
    (work / "htpasswd").write_text(lab.proxy_htpasswd(users))
    (work / "backend.py").write_text(ECHO_BACKEND)
    for path in work.iterdir():
        path.chmod(0o644)
    net, backend, nginx = (
        "opspilot-proxy-test",
        "opspilot-proxy-test-be",
        "opspilot-proxy-test-ng",
    )
    for name in (nginx, backend):
        subprocess.run(
            ["docker", "rm", "-f", name],
            env={**os.environ, "DOCKER_CONTEXT": str(PROXY_DOCKER)},
            capture_output=True,
        )
    subprocess.run(
        ["docker", "network", "create", net],
        env={**os.environ, "DOCKER_CONTEXT": str(PROXY_DOCKER)},
        capture_output=True,
    )
    try:
        _docker(
            "run", "-d", "--rm", "--name", backend, "--network", net,
            "--network-alias", "backend", "-v", f"{work}/backend.py:/b.py:ro",
            "python:3.11-alpine", "python", "-u", "/b.py",
        )  # fmt: skip
        _docker(
            "run", "-d", "--rm", "--name", nginx, "--network", net,
            "-p", "127.0.0.1::8080",
            "-v", f"{work}/nginx.conf:/etc/nginx/nginx.conf:ro",
            "-v", f"{work}/htpasswd:/etc/opspilot-proxy/htpasswd:ro",
            "nginx:1.28.0-alpine", "nginx", "-g", "daemon off;",
        )  # fmt: skip
        port = _docker("port", nginx, "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                _send(base, "GET", "/api/v1/query?query=up", "lab")
                break
            except OSError:
                time.sleep(0.5)
        yield base
    finally:
        for name in (nginx, backend):
            subprocess.run(
                ["docker", "rm", "-f", name],
                env={**os.environ, "DOCKER_CONTEXT": str(PROXY_DOCKER)},
                capture_output=True,
            )
        subprocess.run(
            ["docker", "network", "rm", net],
            env={**os.environ, "DOCKER_CONTEXT": str(PROXY_DOCKER)},
            capture_output=True,
        )


def _send(
    base: str,
    method: str,
    path: str,
    account: str | None,
    body: bytes | None = None,
    *,
    content_type: str = FORM,
    chunked: bool = False,
) -> tuple[int, dict[str, Any] | None]:
    parsed = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    headers = {}
    if account:
        headers["Authorization"] = lab.basic_auth_header(accounts(), account)
    if body is not None:
        headers["Content-Type"] = content_type
    try:
        conn.request(method, path, body=body, headers=headers, encode_chunked=chunked)
        response = conn.getresponse()
        raw = response.read()
    finally:
        conn.close()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, None


def _form(size: int) -> bytes:
    body = b"query=up&start=0&end=1&step=1"
    return body + b"&pad=" + b"x" * max(0, size - len(body) - 5)


def test_real_nginx_holmes_form_rules(live_proxy: str) -> None:
    results = {}
    for path in ("/api/v1/query", "/api/v1/query_range"):
        status, echo = _send(live_proxy, "POST", path, "holmes", _form(512))
        assert status == 200 and echo is not None and echo["body_bytes"] == 512
        assert echo["method"] == "POST"
        results[("holmes", path, "64k")] = _send(
            live_proxy, "POST", path, "holmes", _form(64 * 1024)
        )[0]
        results[("holmes", path, "64k+1")] = _send(
            live_proxy, "POST", path, "holmes", _form(64 * 1024 + 1)
        )[0]
        results[("holmes", path, "70k chunked")] = _send(
            live_proxy, "POST", path, "holmes", _form(70_000), chunked=True
        )[0]
        results[("holmes", path, "protobuf")] = _send(
            live_proxy,
            "POST",
            path,
            "holmes",
            b"",
            content_type="application/x-protobuf",
        )[0]
    assert results == {
        ("holmes", p, kind): code
        for p in ("/api/v1/query", "/api/v1/query_range")
        for kind, code in (
            ("64k", 200),
            ("64k+1", 413),
            ("70k chunked", 413),
            ("protobuf", 403),
        )
    }
    for path in ("/api/v1/series", "/api/v1/metadata", "/api/v1/rules"):
        assert _send(live_proxy, "POST", path, "holmes", _form(512))[0] == 403


@pytest.mark.parametrize("account", ("investigator", "observer", "lab"))
def test_real_nginx_other_accounts_keep_get_head_and_default_body_limit(
    live_proxy: str, account: str
) -> None:
    for path in ("/api/v1/query", "/api/v1/query_range"):
        # form POSTs are denied whatever their size up to nginx's default 1m
        for size in (512, 70_000, 1024 * 1024 - 1024):
            assert _send(live_proxy, "POST", path, account, _form(size))[0] == 403
        assert _send(live_proxy, "POST", path, account, _form(2_000_000))[0] == 413
    # a GET body over 64k is still accepted and never forwarded
    status, echo = _send(
        live_proxy, "GET", "/api/v1/query?query=up", account, _form(70_000)
    )
    assert status == 200 and echo == {"method": "GET", "body_bytes": 0}
    assert _send(live_proxy, "POST", "/api/v1/query", None, _form(512))[0] == 401
