"""Shared pieces for the evaluation-fixture import script tests (#566 C2b).

Written by an independent test author from the C2 interface (section 7 and
「C2b 签名级细节」, 导入脚本) without the implementation. The script runs
as a child process (``python -m scripts.holmes_evals.import_skills``) under
an audit hook that logs every file it opens, so "reads only ``*/SKILL.md``,
``runbook_catalog.json`` and ``runbook_content_*.json``; never
``test_case.yaml``" is observed, not inferred. The synthetic question
directory is made up here; no HolmesGPT file (and no expected answer) is
read.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

REPO = Path(__file__).resolve().parents[1]
MODULE = "scripts.holmes_evals.import_skills"
SOURCE_LABEL = "HolmesGPT 评测夹具"
SOURCE_REF_PREFIX = "holmesgpt@a0009195:"
DEFAULT_REASON = "评测题切换"
#: Files the script may open inside a question directory.
ALLOWED_NAMES = ("SKILL.md", "runbook_catalog.json")
ALLOWED_PREFIX = "runbook_content_"

#: Planted where only a forbidden read could pick it up.
ANSWER_MARKER = "expected-output-marker-566"
CONTENT_MARKER = "runbook-body-marker-566"

SKILLS = {
    "database-troubleshooting": (
        "Steps for slow or failing database queries.",
        f"# Database troubleshooting\n\n1. Check connection errors. {CONTENT_MARKER}-db\n"
        "2. Compare query latency before and after the change.\n",
    ),
    "network-check": (
        "Steps for DNS and connection timeouts between services.",
        f"# Network check\n\n1. Resolve the service name. {CONTENT_MARKER}-net\n",
    ),
}


def skill_file(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def question_dir(root: Path, skills: dict[str, tuple[str, str]] = SKILLS) -> Path:
    """A synthetic upstream question directory: one SKILL.md per skill plus
    the files the script must never open."""
    qdir = root / "94_synthetic_skill_question"
    qdir.mkdir(parents=True)
    for name, (description, body) in skills.items():
        (qdir / name).mkdir()
        (qdir / name / "SKILL.md").write_text(
            skill_file(name, description, body), encoding="utf-8"
        )
    (qdir / "test_case.yaml").write_text(
        f"user_prompt: what is wrong?\nexpected_output:\n  - {ANSWER_MARKER}\n",
        encoding="utf-8",
    )
    (qdir / "global_instructions.json").write_text(
        json.dumps({"instructions": [ANSWER_MARKER]}), encoding="utf-8"
    )
    (qdir / "manifest.yaml").write_text("kind: Namespace\n", encoding="utf-8")
    (qdir / "database-troubleshooting" / "notes.md").write_text(
        ANSWER_MARKER, encoding="utf-8"
    )
    return qdir


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_WRAPPER = """
import os, runpy, sys
log = open(os.environ["IMPORT_TEST_OPEN_LOG"], "w", encoding="utf-8")
def hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        log.write(os.path.abspath(os.fsdecode(args[0])) + "\\n")
        log.flush()
sys.addaudithook(hook)
sys.argv = ["import_skills", *sys.argv[1:]]
runpy.run_module("scripts.holmes_evals.import_skills", run_name="__main__", alter_sys=True)
"""


@dataclass
class ScriptRun:
    returncode: int
    stdout: str
    stderr: str
    opened: list[Path]

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def run_script(
    args: list[str], env: dict[str, str], *, cwd: Path, log: Path
) -> ScriptRun:
    child = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(REPO),
        "NO_PROXY": "*",
        "no_proxy": "*",
        "IMPORT_TEST_OPEN_LOG": str(log),
        **env,
    }
    result = subprocess.run(
        [sys.executable, "-c", _WRAPPER, *args],
        env=child,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    opened = (
        [Path(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
        if log.exists()
        else []
    )
    return ScriptRun(result.returncode, result.stdout, result.stderr, opened)


def opened_inside(run: ScriptRun, qdir: Path) -> list[Path]:
    root = qdir.resolve()
    inside = []
    for path in run.opened:
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        inside.append(path.resolve())
    return inside


def allowed(path: Path) -> bool:
    return path.name in ALLOWED_NAMES or (
        path.name.startswith(ALLOWED_PREFIX) and path.suffix == ".json"
    )


def manifest_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- an HTTP server in front of a handler ----------------------------------------


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    fields: dict[str, str]


@dataclass
class HttpFront:
    """``ThreadingHTTPServer`` that answers with ``respond(recorded)`` ->
    ``(status, headers, body)`` and keeps every request."""

    respond: Callable[[Recorded, bytes], tuple[int, dict[str, str], bytes]]
    requests: list[Recorded] = field(default_factory=list)

    def start(self) -> None:
        front = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _serve(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                fields: dict[str, str] = {}
                if "x-www-form-urlencoded" in (self.headers.get("content-type") or ""):
                    fields = {
                        k: v[0]
                        for k, v in parse_qs(
                            body.decode("utf-8"), keep_blank_values=True
                        ).items()
                    }
                recorded = Recorded(
                    self.command,
                    self.path,
                    {k.lower(): v for k, v in self.headers.items()},
                    fields,
                )
                front.requests.append(recorded)
                status, headers, payload = front.respond(recorded, body)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _serve
            do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def asgi_bridge(
    app: Any,
) -> Callable[[Recorded, bytes], tuple[int, dict[str, str], bytes]]:
    """Forward a recorded request to the real workbench ASGI app."""
    from tests.m1_web_support import call

    def respond(recorded: Recorded, body: bytes):
        headers = {
            k: v
            for k, v in recorded.headers.items()
            if k not in {"host", "content-length", "connection"}
        }
        response = call(app, recorded.method, recorded.path, headers=headers, body=body)
        out = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-length", "transfer-encoding"}
        }
        return response.status, out, bytes(response.body)

    return respond
