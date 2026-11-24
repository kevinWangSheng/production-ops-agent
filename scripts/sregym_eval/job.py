"""The trial job the driver hands to the agent entry (#667 PR-B).

SREGym starts the entry with its own environment (admin ``KUBECONFIG`` of its
filter proxy, the judge key); the entry takes everything it needs from this
mode-600 JSON file instead, named by ``SREGYM_EVAL_JOB``. The job holds no
problem id, only its digest (R8-6, S-3).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from scripts.sregym_eval.boundary import ProblemIdDigest
from scripts.sregym_eval.ports import Ports
from scripts.sregym_eval.summary import CLUSTER_PATHS

JOB_ENV = "SREGYM_EVAL_JOB"


class JobError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Job:
    round: str
    seq: int
    trial: int
    work_dir: str
    cluster_path: str
    k8s_target_file: str
    engineer_kubeconfig: str
    secrets_file: str
    loki_env_file: str
    problem_digest: ProblemIdDigest
    ports: Ports = field(default_factory=Ports)
    trace: str = "off"
    project_name: str | None = None
    evidence_hint: None = None
    diagnosis_wait_s: float = 900.0
    submit_margin_s: float = 240.0
    done_wait_s: float = 600.0

    def as_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["problem_digest"] = self.problem_digest.as_json()
        out["ports"] = self.ports.all()
        return out


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(path, 0o600)


def write_job(path: Path, job: Job) -> None:
    _private_write(path, json.dumps(job.as_json(), indent=1) + "\n")


_STR = ("round", "work_dir", "cluster_path", "k8s_target_file",
        "engineer_kubeconfig", "secrets_file", "loki_env_file", "trace")  # fmt: skip
_NUM = ("diagnosis_wait_s", "submit_margin_s", "done_wait_s")


def read_job(path: Path) -> Job:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise JobError("JOB_INVALID") from None
    if not isinstance(raw, dict):
        raise JobError("JOB_INVALID")
    try:
        if not all(isinstance(raw[k], str) and raw[k] for k in _STR):
            raise JobError("JOB_INVALID")
        for k in ("seq", "trial"):
            if not isinstance(raw[k], int) or isinstance(raw[k], bool) or raw[k] < 1:
                raise JobError("JOB_INVALID")
        if raw["cluster_path"] not in CLUSTER_PATHS or raw["trace"] not in (
            "off",
            "lab",
        ):
            raise JobError("JOB_INVALID")
        numbers = {}
        for k in _NUM:
            value = raw.get(k, getattr(Job, k))
            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or value < 0
            ):
                raise JobError("JOB_INVALID")
            numbers[k] = float(value)
        project = raw.get("project_name")
        if project is not None and not isinstance(project, str):
            raise JobError("JOB_INVALID")
        ports_raw = raw["ports"]
        if not isinstance(ports_raw, dict):
            raise JobError("JOB_INVALID")
        known = Ports().all()
        if set(ports_raw) - set(known) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in ports_raw.values()
        ):
            raise JobError("JOB_INVALID")
        return Job(
            round=raw["round"],
            seq=raw["seq"],
            trial=raw["trial"],
            work_dir=raw["work_dir"],
            cluster_path=raw["cluster_path"],
            k8s_target_file=raw["k8s_target_file"],
            engineer_kubeconfig=raw["engineer_kubeconfig"],
            secrets_file=raw["secrets_file"],
            loki_env_file=raw["loki_env_file"],
            problem_digest=ProblemIdDigest.from_json(raw["problem_digest"]),
            ports=Ports(**{**known, **ports_raw}),
            trace=raw["trace"],
            project_name=project,
            diagnosis_wait_s=numbers["diagnosis_wait_s"],
            submit_margin_s=numbers["submit_margin_s"],
            done_wait_s=numbers["done_wait_s"],
        )
    except (KeyError, TypeError, ValueError):
        raise JobError("JOB_INVALID") from None
