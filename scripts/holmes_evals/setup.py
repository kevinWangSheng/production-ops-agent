"""Question environment setup and cleanup (#666 H-4, H-5, H-7).

Upstream's ``run_commands`` (``tests/llm/utils/commands.py:150-230`` at
a0009195): the whole ``before_test`` / ``after_test`` text goes to
``/bin/bash`` as one script, ``stdin`` closed, the question's directory as the
working directory, ``setup_timeout`` seconds (default 300). The upstream
harness is not used: it loads every test-case field, answers included, into
its process; here the script comes from the answer-free loader.

The scripts get the engineer's Elasticsearch credentials (and the kind
admin kubeconfig on a rental host) and nothing else: no model, judge, trace
or ``OPSPILOT_*`` variable (H-5). Their output goes to a git-ignored log;
only its sha256 reaches the summary.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.itbench_lite.services import base_env

if TYPE_CHECKING:
    from scripts.holmes_evals.shift import EsAdmin

DEFAULT_SETUP_TIMEOUT = 300
BASH = "/bin/bash"


def setup_env(
    es_url: str,
    engineer_key: str,
    kubeconfig: Path | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The setup/cleanup child's whole environment."""
    env = base_env(environ)
    env["ELASTICSEARCH_URL"] = es_url
    env["ELASTICSEARCH_API_KEY"] = engineer_key
    env["ES_UPDATE_SHARD_LIMIT"] = "true"
    if kubeconfig is not None:
        env["KUBECONFIG"] = str(kubeconfig)
    return {str(k): str(v) for k, v in env.items()}


@dataclass(frozen=True)
class ScriptResult:
    operation: str  # before_test / after_test
    exit_code: int | None
    timed_out: bool
    started_at: str
    ended_at: str
    seconds: float
    log_sha256: str | None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.skipped or (self.exit_code == 0 and not self.timed_out)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def run_script(
    script: str | None,
    *,
    operation: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: int,
    log_path: Path,
) -> ScriptResult:
    """Run one upstream script as upstream does; output to ``log_path``."""
    started = datetime.now(UTC)
    if not script or not script.strip():
        return ScriptResult(
            operation, None, False, _iso(started), _iso(started), 0.0, None, True
        )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    timed_out = False
    code: int | None
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [BASH, "-c", script.strip()],
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            # The started bash only, through its handle (no group signal):
            # SIGTERM, a grace, then SIGKILL of the same PID.
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            code = None
    ended = datetime.now(UTC)
    digest = hashlib.sha256(log_path.read_bytes()).hexdigest()
    return ScriptResult(
        operation,
        code,
        timed_out,
        _iso(started),
        _iso(ended),
        round(time.monotonic() - began, 3),
        digest,
    )


def purge_question_indices(admin: EsAdmin, qid: str) -> list[str]:
    """Delete what is left of ``app-<id>-*`` (H-7: only the current
    question's indices in ES); returns the names that were there."""
    from scripts.holmes_evals.shift import index_pattern

    pattern = index_pattern(qid)
    names = sorted(admin.indices(pattern))
    if names:
        admin.delete(pattern)
    return names
