"""Shared helpers for the #667 PR-A contract tests (SREGym Lite diagnosis track).

Written from docs/tasks/2026-11-19-sregym-667.md (PR-A public interface) and
the pre-review contract-test list (groups 1, 2, 3, 8, 9, 10), without reading
``scripts/sregym_eval/``.

Answer isolation (R8-6, S-8): the real pinned upstream checkout is only used
for ``git`` plumbing, sha256 of the pinned files and the Lite list in
``problem_sets.py``. Result CSVs, phase ledgers and app infos are synthetic.

The conductor stand-in here (:class:`StubServer`) is our own; it does not use
the implementation's ``fake_conductor``.
"""

from __future__ import annotations

import importlib
import json
import socket
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
_UPSTREAM_NAME = "sregym-ac60c64"

PINNED_COMMIT = "ac60c642d94f8342e924014ddf36f5cdf15b3327"
# Paths and digests are kept apart so no line pairs a path with a hex digest.
_PINNED_PATHS = (
    "sregym/conductor/oracles/llm_as_a_judge/judge.py",
    "sregym/conductor/oracles/llm_as_a_judge/rca_checklists.yaml",
    "sregym/conductor/problem_sets.py",
    "sregym/conductor/conductor_api.py",
    "sregym/conductor/submission.py",
    "sregym/service/agent_visibility_policy.py",
)
_PINNED_DIGESTS = (
    "116e10ca9a156c1799ab51734b9cc2c7a4fae183fdf7fd12bb95f13064d9482f",
    "4ca6522812892a3263abdc86d184a74c38769a4df0f56c29745fa2f787b10828",
    "032af668e3a1e582d29fcca2510e7eb9a2cb6d2fffe4e409d9e5a0315842c975",
    "20cdc78a92723038eaf3812079da8c2fbcd8160934a119f6d8e5833fb79addd7",
    "36cd287d8da0af308f7d4354f6e743db6b2d4e13e5d85ec3ae87a18c0eae6cd4",
    "941c3501731a60a1368eab5f6947121ceb1ae284b708c24e234af239a2c491d2",
)
PINNED_FILES = dict(zip(_PINNED_PATHS, _PINNED_DIGESTS, strict=True))
PROBLEM_SETS = "sregym/conductor/problem_sets.py"

# Upstream SREGYM_LITE_PROBLEMS at the pinned commit (problem IDs only).
LITE_17 = (
    "cronjob_sidecar_blocks_completion_hotel_reservation",
    "edge_request_filter_cpu_saturation",
    "network_policy_block",
    "env_variable_shadowing_astronomy_shop",
    "mutating_webhook_resource_limits_social_network",
    "finalizer_deadlock_controller_hotel_reservation",
    "kafka_poison_pill_hol_block",
    "internal_traffic_policy_local_astronomy_shop",
    "service_dns_resolution_failure_social_network",
    "service_wrong_pod_selection_hotel_reservation",
    "namespace_memory_limit",
    "valkey_auth_disruption",
    "secret_rotation_stale_env_credentials_astronomy_shop",
    "unschedulable_incorrect_port_assignment",
    "duplicate_pvc_mounts_social_network",
    "rolling_update_misconfigured_social_network",
    "search_rate_retry_collapse_hotel_reservation",
)

# Task record, 模板全文 (ends with a newline; hash checked below).
TEMPLATE_TEXT = """You are an SRE agent tasked with diagnosing issues in a Kubernetes application.

Application: {app_name}
{namespace_block}

{descriptions}

CRITICAL: You are running in an AUTOMATED environment. Work autonomously and make all decisions yourself. DO NOT ask for user confirmation or approval. Proceed with the best solution based on your analysis.

TASK: DIAGNOSIS
- Investigate the application to detect any anomalies or issues
- Analyze metrics, logs, and traces
- When ready, submit a natural language description of the issue you found
- Your diagnosis is evaluated on whether you correctly identify the faulty components and root cause

Important:
- You can query metrics and traces through the available observability tools
"""
NAMESPACE_SINGLE = "Namespace: {namespace}"
NAMESPACE_MULTI = (
    "Namespaces: {namespaces}\n"
    "(This scenario spans multiple namespaces; investigate all of them.)"
)
TEMPLATE_SHA256 = "b969b4e32ad6ea73dd628e88756bc75620cd23359ba0b60fc941d2bdc7ca3b8e"
QUESTION_LIMIT = 8192


def mod(name: str) -> ModuleType:
    """Import lazily so collection stays green before the package exists."""
    return importlib.import_module(f"scripts.sregym_eval.{name}")


def code_of(exc: BaseException) -> str:
    return getattr(exc, "code", None) or (exc.args[0] if exc.args else "")


def upstream_root() -> Path | None:
    """The pinned upstream checkout, if this machine has it (CI does not)."""
    candidates = [REPO / "tmp" / "upstream" / _UPSTREAM_NAME]
    try:
        common = subprocess.run(
            [
                "git",
                "-C",
                str(REPO),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        candidates.append(Path(common).parent / "tmp" / "upstream" / _UPSTREAM_NAME)
    except (OSError, subprocess.CalledProcessError):
        pass
    for c in candidates:
        if (c / PROBLEM_SETS).is_file() and (c / ".git").exists():
            return c
    return None


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=contract-test",
            "-c",
            "user.email=ct@example.invalid",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def pinned_clone(real: Path, dest: Path) -> Path:
    """A shared clone whose HEAD is the pin and whose pinned files are checked out."""
    subprocess.run(
        ["git", "clone", "-q", "--shared", "--no-checkout", str(real), str(dest)],
        check=True,
        capture_output=True,
    )
    git("update-ref", "--no-deref", "HEAD", PINNED_COMMIT, cwd=dest)
    git("checkout", PINNED_COMMIT, "--", *PINNED_FILES, cwd=dest)
    return dest


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- Local HTTP stand-in that records every request -------------------------


@dataclass
class Reply:
    status: int = 200
    body: Any = None  # dict/list -> JSON; str/bytes -> raw
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class StubServer:
    """``route(method, path) -> Reply``; records method, path, headers, body."""

    def __init__(self, route: Callable[[str, str], Reply], port: int = 0) -> None:
        self.route = route
        self.seen: list[Seen] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.seen.append(
                    Seen(
                        self.command,
                        self.path,
                        {k.lower(): v for k, v in self.headers.items()},
                        body,
                    )
                )
                reply = stub.route(self.command, self.path)
                if reply.delay_s:
                    threading.Event().wait(reply.delay_s)
                if isinstance(reply.body, (dict, list)):
                    raw = json.dumps(reply.body).encode()
                    ctype = "application/json"
                elif isinstance(reply.body, str):
                    raw = reply.body.encode()
                    ctype = "text/plain"
                else:
                    raw = reply.body or b""
                    ctype = "application/octet-stream"
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(raw)))
                    for k, v in reply.headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle

            def log_message(self, *args: Any) -> None:  # quiet
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self) -> StubServer:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class FakeClock:
    """Fixed clock: only ``sleep`` advances time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[], None] | None = None

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s
        if self.on_sleep is not None:
            self.on_sleep()
