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

``--backend victorialogs`` (C5b) renders and runs the same gateway in front
of VictoriaLogs: it forwards ``GET`` of the five ``/select/logsql/*`` paths
the ``victorialogs_*`` tools send, passes ``AccountID``/``ProjectID``
through (the product's configured tenant, C5-0), and ``start`` writes
``OPSPILOT_VICTORIALOGS_*``; its container, port, state directory and
settings file are separate from the Loki gateway's, so both can run on one
host. The default ``--backend loki`` is unchanged.

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
from dataclasses import dataclass
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
#: C5b: the paths the VictoriaLogs gateway forwards (``victorialogs_logs.PATHS``).
VICTORIALOGS_READ_PATHS = (
    "/select/logsql/query",
    "/select/logsql/streams",
    "/select/logsql/field_names",
    "/select/logsql/field_values",
    "/select/logsql/hits",
)


@dataclass(frozen=True)
class Backend:
    name: str
    read_paths: tuple[str, ...]
    env_prefix: str
    container: str
    port: int
    state: Path
    out: Path


BACKENDS = {
    "loki": Backend(
        "loki",
        (READ_PATH,),
        "OPSPILOT_LOKI",
        CONTAINER,
        DEFAULT_PORT,
        DEFAULT_STATE,
        DEFAULT_OUT,
    ),
    "victorialogs": Backend(
        "victorialogs",
        VICTORIALOGS_READ_PATHS,
        "OPSPILOT_VICTORIALOGS",
        "opspilot-victorialogs-gateway",
        53103,
        LAB_DIR / "victorialogs-gateway",
        LAB_DIR / "victorialogs-readonly.env",
    ),
}
_UPSTREAM = re.compile(r"[A-Za-z0-9.-]+:[0-9]{1,5}")
_HOST = re.compile(r"[0-9.]+|[A-Za-z0-9.-]+")
#: The product's own tenant rule (``loki_logs._tenant_ok``): printable, no
#: whitespace or ``|``, at most 150 characters.
_TENANT = re.compile(r"[!-{}~]{1,150}")


class SetupError(Exception):
    """A fixed message; never carries a credential."""


def _server(
    bind: str, port: int, upstream: str | None, backend: Backend = BACKENDS["loki"]
) -> str:
    head = f"""  server {{
    listen {bind}:{port};
    root /var/empty;
    auth_basic "{backend.name} read-only";
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
    reads = "".join(
        f"    location = {path} {{ try_files /__absent__ @read; }}\n"
        for path in backend.read_paths
    )
    return (
        head
        + reads
        + f"""    location / {{ try_files /__absent__ @denied; }}
    location @denied {{ return 403; }}
    location @read {{
      if ($request_method != GET) {{ return 403; }}
      proxy_pass_request_body off;
      proxy_set_header Content-Length "";
      proxy_set_header Authorization "";
      proxy_pass http://{backend.name}_{port}$uri$is_args$args;
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
    backend: str = "loki",
) -> str:
    """The nginx configuration: one server per ``(port, upstream)``; an
    upstream of ``None`` refuses every authenticated request. try_files runs
    after authentication, so an anonymous or wrong-password request is 401,
    an authenticated one outside the read rule 403. The access log has
    method, path and status only -- no query string, headers or bodies."""
    if not servers:
        raise SetupError("no server to render")
    if backend not in BACKENDS:
        raise SetupError("--backend must be loki or victorialogs")
    chosen = BACKENDS[backend]
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
        f"  upstream {chosen.name}_{port} {{ server {upstream}; }}\n"
        for port, upstream in servers
        if upstream is not None
    )
    body = groups + "".join(
        _server(bind, port, upstream, chosen) for port, upstream in servers
    )
    return (
        (
            """worker_processes 1;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;
events { worker_connections 128; }
http {
  log_format opspilot '$time_iso8601 $server_port $remote_user $request_method $uri $status';
  access_log ACCESS_LOG opspilot;
  # A 4000-character LogQL query is up to ~36k once URL-encoded (non-ASCII).
  large_client_header_buffers 4 64k;
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


def _write_config(state: Path, port: int, upstream: str, backend: str) -> None:
    # The nginx worker (another uid in the container) reads both files; the
    # htpasswd holds a salted hash only. The plain password is only in the
    # mode-600 env file.
    config = gateway_config(
        [(port, upstream)], htpasswd="/etc/opspilot-gateway/htpasswd", backend=backend
    )
    _private_write(state / "nginx.conf", config.splitlines(), _PUBLIC)
    (state / "upstream").write_text(f"{port} {upstream}\n")


def start(args: argparse.Namespace) -> str:
    backend = BACKENDS[args.backend]
    state: Path = args.state_dir or backend.state
    port: int = args.port or backend.port
    out: Path = args.out or backend.out
    password = secrets.token_urlsafe(24)
    _private_write(state / "htpasswd", [ssha_entry(args.user, password)], _PUBLIC)
    _write_config(state, port, args.upstream, backend.name)
    _docker(
        "run",
        "-d",
        "--name",
        backend.container,
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
    prefix = backend.env_prefix
    lines = [
        f"{prefix}_URL=http://127.0.0.1:{port}",
        f"{prefix}_USERNAME={args.user}",
        f"{prefix}_PASSWORD={password}",
    ]
    if args.tenant:
        lines.append(f"OPSPILOT_LOKI_TENANT_ID={args.tenant}")
    if args.account_id:
        lines.append(f"{prefix}_ACCOUNT_ID={args.account_id}")
    if args.project_id:
        lines.append(f"{prefix}_PROJECT_ID={args.project_id}")
    _private_write(out, lines, _PRIVATE)
    return (
        f"gateway {backend.container} on 127.0.0.1:{port} -> {args.upstream}; "
        f"account {args.user}; settings written to {out} (mode 600)"
    )


def switch(args: argparse.Namespace) -> str:
    backend = BACKENDS[args.backend]
    state: Path = args.state_dir or backend.state
    recorded = (state / "upstream").read_text().split()
    port = int(recorded[0])
    _write_config(state, port, args.upstream, backend.name)
    _docker(
        "exec",
        backend.container,
        "nginx",
        "-c",
        "/etc/opspilot-gateway/nginx.conf",
        "-s",
        "reload",
    )
    return f"gateway {backend.container} on 127.0.0.1:{port} now -> {args.upstream}"


def stop(args: argparse.Namespace) -> str:
    container = BACKENDS[args.backend].container
    _docker("rm", "-f", container)
    return f"removed {container}"


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
        command.add_argument("--upstream", required=True, help="backend host:port")
        command.add_argument("--state-dir", type=Path, default=None)
    begin = commands.choices["start"]
    begin.add_argument("--port", type=int, default=None)
    begin.add_argument("--user", default=DEFAULT_USER)
    begin.add_argument("--tenant", help="OPSPILOT_LOKI_TENANT_ID for the worker")
    begin.add_argument("--account-id", help="OPSPILOT_VICTORIALOGS_ACCOUNT_ID")
    begin.add_argument("--project-id", help="OPSPILOT_VICTORIALOGS_PROJECT_ID")
    begin.add_argument("--out", type=Path, default=None)
    commands.add_parser("stop")
    for command in commands.choices.values():
        command.add_argument("--backend", choices=sorted(BACKENDS), default="loki")
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
                    backend=args.backend,
                )
            )
            return 0
        if args.command in ("start", "switch") and not _UPSTREAM.fullmatch(
            args.upstream
        ):
            raise SetupError("--upstream must be host:port")
        if (
            args.command == "start"
            and args.tenant
            and not _TENANT.fullmatch(args.tenant)
        ):
            raise SetupError(
                "--tenant must be 1 to 150 printable characters without whitespace or |"
            )
        if args.command == "start":
            if args.backend == "loki" and (args.account_id or args.project_id):
                raise SetupError("--account-id/--project-id are for victorialogs")
            if args.backend == "victorialogs" and args.tenant:
                raise SetupError("--tenant is for loki; use --account-id")
            for value in (args.account_id, args.project_id):
                if value and not (value.isdigit() and int(value) <= 2**32 - 1):
                    raise SetupError("tenant ids must be unsigned 32-bit integers")
        message = {"start": start, "switch": switch, "stop": stop}[args.command](args)
    except (SetupError, OSError, ValueError, IndexError) as error:
        print(f"loki_readonly_gateway: {error}", file=sys.stderr)
        return 1
    print(f"loki_readonly_gateway: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
