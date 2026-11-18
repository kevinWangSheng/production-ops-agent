"""The read-only gateway in front of Loki (C5-U1, C5-18; #671 / #669 smoke).

Loki has no user system: anyone who reaches its port may push, delete or
flush. The product therefore only holds an account of this nginx gateway,
which requires basic authentication and lets exactly one request through --
``GET /loki/api/v1/query_range``, the only request ``grafana_loki_query``
sends (C5-3) -- without a body and without the client's ``Authorization``
header. Every other method or path is 403 (after authentication, so a wrong
password is 401). ``X-Scope-OrgID`` passes through: the tenant is the
product's configuration (C5-0). Precedent: ``scripts/kind_lab.py``
``proxy_nginx_config`` (F7 U3).

Subcommands:

* ``render`` -- print the nginx configuration (the CI job ``log-backends``
  runs the gateway from this output, so CI tests these rules).
* ``start`` -- on a lab or rental host: write the configuration and an
  ``{SSHA}`` htpasswd with a generated password under ``--state-dir``, start
  the pinned nginx container on the host network (listening on 127.0.0.1
  only, so the lab gate's loopback rule holds), and write the worker
  settings (``OPSPILOT_LOKI_URL``, ``OPSPILOT_LOKI_USERNAME``,
  ``OPSPILOT_LOKI_PASSWORD``, optionally ``OPSPILOT_LOKI_TENANT_ID``) to a
  mode-600 file. The password is never printed or put on a command line.
* ``switch`` -- point the running gateway at another Loki (each evaluation
  case brings its own) and reload nginx; the product address, the account
  and therefore the tool schema revision stay the same, so web and worker
  keep running.
* ``stop`` -- remove the gateway container (by its fixed name only).

Usage on a rental host::

    .venv/bin/python scripts/loki_readonly_gateway.py start --upstream 127.0.0.1:3100
    set -a; . tmp/m1-kind-lab/loki-readonly.env; set +a   # web and worker
    .venv/bin/python scripts/loki_readonly_gateway.py switch --upstream 127.0.0.1:3101

The evaluation fixtures keep writing to each Loki's own port; only the
product goes through the gateway.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: kind_lab's LAB_DIR (``tmp/m1-kind-lab``), not imported so the script runs
#: without the lab's tooling on a rental host.
LAB_DIR = ROOT / "tmp" / "m1-kind-lab"
DEFAULT_OUT = LAB_DIR / "loki-readonly.env"
DEFAULT_STATE = LAB_DIR / "loki-gateway"
DEFAULT_PORT = 53100
DEFAULT_USER = "opspilot-investigator"
CONTAINER = "opspilot-loki-gateway"
#: nginx:1.30.7-alpine (stable) multi-platform index, read from Docker Hub on
#: 2026-11-18; the same digest as the CI job ``log-backends``.
NGINX_IMAGE = (
    "nginx@sha256:b24de00298eb7fb23839442d46c780991be7bc7ddb081b6c0876ac777012e352"
)
#: C5-3: the one path the gateway forwards.
READ_PATH = "/loki/api/v1/query_range"
_UPSTREAM = re.compile(r"[A-Za-z0-9.-]+:[0-9]{1,5}")
_HOST = re.compile(r"[0-9.]+|[A-Za-z0-9.-]+")


class SetupError(Exception):
    """A fixed message; never carries a credential."""


def _server(bind: str, port: int, upstream: str | None) -> str:
    head = f"""  server {{
    listen {bind}:{port};
    root /var/empty;
    auth_basic "loki read-only";
    auth_basic_user_file HTPASSWD;
"""
    if upstream is None:
        # Every authenticated request is 403 (a gateway refusing the account).
        return (
            head
            + """    location / { try_files /__absent__ @denied; }
    location @denied { return 403; }
  }
"""
        )
    return (
        head
        + f"""    location = {READ_PATH} {{ try_files /__absent__ @read; }}
    location / {{ try_files /__absent__ @denied; }}
    location @denied {{ return 403; }}
    location @read {{
      if ($request_method != GET) {{ return 403; }}
      proxy_pass_request_body off;
      proxy_set_header Content-Length "";
      proxy_set_header Authorization "";
      proxy_pass http://loki_{port}$uri$is_args$args;
    }}
  }}
"""
    )


def gateway_config(
    servers: list[tuple[int, str | None]],
    *,
    bind: str = "127.0.0.1",
    htpasswd: str = "/etc/opspilot-gateway/htpasswd",
    access_log: str = "/dev/stdout",
) -> str:
    """The nginx configuration: one server per ``(port, upstream)``; an
    upstream of ``None`` refuses every authenticated request. try_files runs
    after authentication, so an anonymous or wrong-password request is 401,
    an authenticated one outside the read rule 403. The access log has
    method, path and status only -- no query string, headers or bodies."""
    if not servers:
        raise SetupError("no server to render")
    if not _HOST.fullmatch(bind):
        raise SetupError("--bind must be a host name or IPv4 address")
    for port, upstream in servers:
        if not 1 <= port <= 65535:
            raise SetupError("ports must be 1 to 65535")
        if upstream is not None and not _UPSTREAM.fullmatch(upstream):
            raise SetupError("an upstream must be host:port")
    # Named upstream groups: resolved when nginx (re)loads, so a container
    # name works without a runtime resolver.
    groups = "".join(
        f"  upstream loki_{port} {{ server {upstream}; }}\n"
        for port, upstream in servers
        if upstream is not None
    )
    body = groups + "".join(_server(bind, port, upstream) for port, upstream in servers)
    return (
        (
            """worker_processes 1;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;
events { worker_connections 128; }
http {
  log_format opspilot '$time_iso8601 $server_port $remote_user $request_method $uri $status';
  access_log ACCESS_LOG opspilot;
  # A 4000-character LogQL query is up to ~12k once URL-encoded.
  large_client_header_buffers 4 32k;
  client_max_body_size 64k;
"""
            + body
            + "}\n"
        )
        .replace("HTPASSWD", htpasswd)
        .replace("ACCESS_LOG", access_log)
    )


def ssha_entry(user: str, password: str) -> str:
    """One htpasswd line in RFC 2307 ``{SSHA}`` form (nginx supports it)."""
    salt = secrets.token_bytes(8)
    digest = hashlib.sha1(password.encode() + salt).digest()
    return f"{user}:{{SSHA}}{base64.b64encode(digest + salt).decode('ascii')}"


def _private_write(path: Path, lines: list[str], mode: int) -> None:
    """Write a new file beside ``path`` (``O_CREAT | O_EXCL``, never a
    symlink target), then move it onto ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(temp), flags, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


_PUBLIC = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
_PRIVATE = stat.S_IRUSR | stat.S_IWUSR


def _docker(*args: str) -> None:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SetupError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")


def _write_config(state: Path, port: int, upstream: str) -> None:
    # The nginx worker (another uid in the container) reads both files; the
    # htpasswd holds a salted hash only. The plain password is only in the
    # mode-600 env file.
    config = gateway_config(
        [(port, upstream)], htpasswd="/etc/opspilot-gateway/htpasswd"
    )
    _private_write(state / "nginx.conf", config.splitlines(), _PUBLIC)
    (state / "upstream").write_text(f"{port} {upstream}\n")


def start(args: argparse.Namespace) -> str:
    state: Path = args.state_dir
    password = secrets.token_urlsafe(24)
    _private_write(state / "htpasswd", [ssha_entry(args.user, password)], _PUBLIC)
    _write_config(state, args.port, args.upstream)
    _docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        "--network",
        "host",
        "--memory=64m",
        "--cpus=0.5",
        "-v",
        f"{state.resolve()}:/etc/opspilot-gateway:ro",
        NGINX_IMAGE,
        "nginx",
        "-g",
        "daemon off;",
        "-c",
        "/etc/opspilot-gateway/nginx.conf",
    )
    lines = [
        f"OPSPILOT_LOKI_URL=http://127.0.0.1:{args.port}",
        f"OPSPILOT_LOKI_USERNAME={args.user}",
        f"OPSPILOT_LOKI_PASSWORD={password}",
    ]
    if args.tenant:
        lines.append(f"OPSPILOT_LOKI_TENANT_ID={args.tenant}")
    _private_write(args.out, lines, _PRIVATE)
    return (
        f"gateway {CONTAINER} on 127.0.0.1:{args.port} -> {args.upstream}; "
        f"account {args.user}; settings written to {args.out} (mode 600)"
    )


def switch(args: argparse.Namespace) -> str:
    state: Path = args.state_dir
    recorded = (state / "upstream").read_text().split()
    port = int(recorded[0])
    _write_config(state, port, args.upstream)
    _docker(
        "exec",
        CONTAINER,
        "nginx",
        "-c",
        "/etc/opspilot-gateway/nginx.conf",
        "-s",
        "reload",
    )
    return f"gateway {CONTAINER} on 127.0.0.1:{port} now -> {args.upstream}"


def stop(args: argparse.Namespace) -> str:
    _docker("rm", "-f", CONTAINER)
    return f"removed {CONTAINER}"


def _server_arg(value: str) -> tuple[int, str]:
    port, _, upstream = value.partition("=")
    if not port.isdigit() or not upstream:
        raise argparse.ArgumentTypeError("expected PORT=HOST:PORT")
    return int(port), upstream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render", help="print the nginx configuration")
    render.add_argument(
        "--server", type=_server_arg, action="append", default=[], metavar="PORT=UP"
    )
    render.add_argument("--deny-port", type=int, action="append", default=[])
    render.add_argument("--bind", default="127.0.0.1")
    render.add_argument("--htpasswd", default="/etc/opspilot-gateway/htpasswd")
    render.add_argument("--access-log", default="/dev/stdout")
    for name in ("start", "switch"):
        command = commands.add_parser(name)
        command.add_argument("--upstream", required=True, help="Loki host:port")
        command.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    begin = commands.choices["start"]
    begin.add_argument("--port", type=int, default=DEFAULT_PORT)
    begin.add_argument("--user", default=DEFAULT_USER)
    begin.add_argument("--tenant", help="OPSPILOT_LOKI_TENANT_ID for the worker")
    begin.add_argument("--out", type=Path, default=DEFAULT_OUT)
    commands.add_parser("stop")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            servers: list[tuple[int, str | None]] = [*args.server]
            servers += [(port, None) for port in args.deny_port]
            sys.stdout.write(
                gateway_config(
                    servers,
                    bind=args.bind,
                    htpasswd=args.htpasswd,
                    access_log=args.access_log,
                )
            )
            return 0
        if args.command in ("start", "switch") and not _UPSTREAM.fullmatch(
            args.upstream
        ):
            raise SetupError("--upstream must be host:port")
        message = {"start": start, "switch": switch, "stop": stop}[args.command](args)
    except (SetupError, OSError, ValueError, IndexError) as error:
        print(f"loki_readonly_gateway: {error}", file=sys.stderr)
        return 1
    print(f"loki_readonly_gateway: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
