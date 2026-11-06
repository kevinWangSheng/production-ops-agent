"""The soak's only side effects: HTTP, subprocesses, PostgreSQL reads.

Every network call goes through ``OPENER`` (no proxy, no redirects, like
``kind_lab.OPENER``) with a timeout; every command runs with a timeout and
never through a shell. Credentials are read from files named in the config
and are never written to a log line.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.soak.common import load_env_file


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


@dataclass(frozen=True)
class Reply:
    status: int | None
    body: bytes
    error: str | None


def http(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 10.0,
) -> Reply:
    request = urllib.request.Request(
        url, data=data, method=method, headers=dict(headers or {})
    )
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return Reply(response.status, response.read(1 << 22), None)
    except urllib.error.HTTPError as exc:
        return Reply(exc.code, exc.read(1 << 16), None)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return Reply(None, b"", type(reason).__name__)


def basic_auth(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def run(
    cmd: Sequence[str],
    *,
    timeout: float = 120.0,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``env`` is added to the inherited environment (non-secret values)."""
    merged = None if env is None else {**os.environ, **env}
    try:
        return subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=merged,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(cmd), 124, "", "timeout")
    except OSError as exc:
        return subprocess.CompletedProcess(list(cmd), 127, "", type(exc).__name__)


class Prometheus:
    """The lab Prometheus through the tunnel (the sidecar's loopback port on
    da-server), with the ``lab`` role account (``kind_lab.py env-file lab``)."""

    def __init__(self, base: str, env_file: Path | None) -> None:
        self.base = base.rstrip("/")
        self._auth: str | None = None
        if env_file is not None:
            env = load_env_file(env_file)
            user = env.get("OPSPILOT_LAB_PROMETHEUS_USERNAME")
            password = env.get("OPSPILOT_LAB_PROMETHEUS_PASSWORD")
            if user and password:
                self._auth = basic_auth(user, password)

    def _get(self, path: str, params: Mapping[str, str], timeout: float) -> Reply:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        headers = {"Authorization": self._auth} if self._auth else {}
        return http(url, headers=headers, timeout=timeout)

    def query(
        self, expr: str, *, timeout: float = 15.0
    ) -> tuple[Reply, list[Any] | None]:
        reply = self._get("/api/v1/query", {"query": expr}, timeout)
        return reply, _result(reply)

    def query_range(
        self, expr: str, start: float, end: float, step: int, *, timeout: float = 60.0
    ) -> tuple[Reply, list[Any] | None]:
        params = {
            "query": expr,
            "start": f"{start:.3f}",
            "end": f"{end:.3f}",
            "step": str(step),
        }
        reply = self._get("/api/v1/query_range", params, timeout)
        return reply, _result(reply)


def _result(reply: Reply) -> list[Any] | None:
    if reply.status != 200:
        return None
    try:
        payload = json.loads(reply.body)
    except ValueError:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, list) else None


class Database:
    """Read-only SQL through ``compose exec postgres psql`` (PostgreSQL has no
    host port, C3 §11). Each query is wrapped so psql prints one JSON array.

    ``jsonb_agg``, not ``json_agg``: PostgreSQL's ``json_agg`` puts ", \\n "
    between elements, so two or more rows span several lines (the 2026-11-06
    rehearsal, #497: the closer failed from the second incident on);
    ``jsonb`` text is one line. The parser still reads the whole array from
    its first line on, whatever the line breaks."""

    def __init__(
        self,
        compose: Sequence[str],
        user: str = "opspilot_owner",
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._compose = list(compose)
        self._user = user
        self._env = env

    def rows(self, sql: str, *, timeout: float = 60.0) -> list[dict[str, Any]]:
        wrapped = f"SELECT coalesce(jsonb_agg(t), '[]'::jsonb) FROM ({sql}) t"
        cmd = [
            *self._compose,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            self._user,
            "-d",
            "opspilot",
            "-qAtX",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "SET default_transaction_read_only = on",
            "-c",
            wrapped,
        ]
        proc = run(cmd, timeout=timeout, env=self._env)
        if proc.returncode != 0:
            raise RuntimeError(
                f"psql exit {proc.returncode}: {proc.stderr.strip()[:300]}"
            )
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("psql printed nothing")
        start = next((i for i, line in enumerate(lines) if line.startswith("[")), None)
        if start is None:
            raise RuntimeError("psql did not print a JSON array")
        try:
            rows = json.loads("\n".join(lines[start:]))
        except ValueError as exc:
            raise RuntimeError(f"psql printed invalid JSON: {exc}") from None
        if not isinstance(rows, list):
            raise RuntimeError("psql did not print a JSON array")
        return rows
